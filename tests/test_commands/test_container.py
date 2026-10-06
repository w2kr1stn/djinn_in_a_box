"""Tests for container lifecycle commands."""

import io
import re
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import container
from djinn_in_a_box.config.defaults import VOLUME_CATEGORIES
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.config_workflow import (
    WorkflowPreparationProblem,
    WorkflowPreparationResult,
)
from djinn_in_a_box.core.docker import (
    ContainerMount,
    DockerMode,
    MountCollisionError,
    MountSpecificationError,
    RunResult,
    WorkflowImageCompatibility,
)
from djinn_in_a_box.core.exceptions import (
    ConfigNotFoundError,
    ConfigValidationError,
    RuntimeMountSpecificationError,
)
from djinn_in_a_box.core.theme import DJINN_THEME

runner = CliRunner()


@pytest.mark.parametrize("command", ["status", "listing"])
@pytest.mark.parametrize("configured", [True, False], ids=["configured", "missing-config"])
def test_status_declared_volume_categories(
    command: str, configured: bool, declared_app_config: AppConfig,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    expected = {
        "cache": ["djinn-uv-cache", "djinn-tools-cache", "djinn-vscode-server"],
        "data": ["djinn-opencode-data", "djinn-vscode-workspaces"],
        "none": ["djinn-desktop-dbus", "djinn-desktop-audio"],
        "protected (only clean all)": ["djinn-hostctl-state"],
    }
    if configured:
        expected["data"].append("djinn-journal")
        expected["cache"].append("djinn-scratch (not created)")
        expected["none"].append("djinn-worker")
    with (
        patch.object(container, "load_config", return_value=declared_app_config) as load,
        patch.object(container.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, stdout="",
        )),
        patch("djinn_in_a_box.core.docker.volume_exists",
              side_effect=lambda name: name != "djinn-scratch"),
        patch.object(container, "_print_resource_table",
                     wraps=container._print_resource_table) as table,
        patch.object(container, "get_existing_sync_paths_by_category", return_value=[]),
        patch.object(container, "is_container_running", return_value=False),
        patch("djinn_in_a_box.core.docker.resolve_declared_entries") as resolve,
    ):
        if not configured:
            load.side_effect = ConfigNotFoundError(tmp_path / "missing-config.toml")
        result = runner.invoke(app, ["status"] if command == "status" else ["clean", "volumes"])
    assert result.exit_code == 0, result.output
    load.assert_called_once()
    table.assert_called_once_with("Djinn Volumes", "Volume", expected)
    resolve.assert_not_called()
    for names in expected.values():
        for name in names:
            assert name in result.output
    if configured:
        assert "None" in result.output


@pytest.mark.parametrize(
    "operation",
    ["cache", "data", "credentials", "repo-dotfiles", "all", "default", "name"],
)
def test_declared_cleanup_sets(
    operation: str, declared_app_config: AppConfig, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    config = declared_app_config
    builtins_cache = {"djinn-uv-cache", "djinn-tools-cache", "djinn-vscode-server"}
    builtins_data = {"djinn-opencode-data", "djinn-vscode-workspaces"}
    expected_volumes = {
        "cache": {"djinn-uv-cache", "djinn-tools-cache", "djinn-scratch"},
        "data": {"djinn-opencode-data", "djinn-journal"},
        "all": builtins_cache | builtins_data | {"djinn-journal", "djinn-scratch", "djinn-worker"},
        "name": {"djinn-worker"},
    }.get(operation, set())
    sync_names = {"claude", "codex", "opencode", "gh", "age", "repo-dotfiles"}
    expected_sync = {
        "credentials": sync_names - {"repo-dotfiles"},
        "repo-dotfiles": {"repo-dotfiles"},
        "all": sync_names,
    }.get(operation, set())
    args = {
        "all": ["clean", "all", "--force"], "default": ["clean"],
        "name": ["clean", "volumes", "djinn-worker"],
    }.get(operation, ["clean", "volumes", f"--{operation}", "--force"])
    with (
        patch.object(container, "load_config", return_value=config) as load,
        patch.object(container, "compose_down", return_value=RunResult(0)) as down,
        patch("djinn_in_a_box.core.docker.volume_exists", side_effect=lambda name: name not in {
            "djinn-vscode-server", "djinn-vscode-workspaces",
        }),
        patch.object(container, "volume_exists", return_value=True),
        patch.object(container, "delete_volumes",
                     side_effect=lambda names: dict.fromkeys(names, True)) as delete,
        patch.object(container, "delete_volume", return_value=True) as delete_one,
        patch.object(container, "clear_sync_path", wraps=container.clear_sync_path) as clear,
        patch.object(container, "network_exists", return_value=False),
    ):
        if operation in {"name", "default"}:
            load.side_effect = AssertionError("This cleanup path must remain config-free")
        result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    actual_volumes = [name for call in delete.call_args_list for name in call.args[0]]
    actual_volumes.extend(call.args[0] for call in delete_one.call_args_list)
    assert set(actual_volumes) == expected_volumes
    assert len(actual_volumes) == len(expected_volumes)
    assert {call.args[0] for call in clear.call_args_list} == {
        config.config_root / name for name in expected_sync
    }
    assert len(clear.call_args_list) == len(expected_sync)
    if operation in {"name", "default"}:
        load.assert_not_called()
    else:
        load.assert_called_once()
    if operation in {"all", "default"}:
        down.assert_called_once_with()
    else:
        down.assert_not_called()
    for name in sync_names:
        assert (config.config_root / name).is_dir()
        assert (config.config_root / name / "sentinel").exists() == (name not in expected_sync)
    for name in ("external", "shared", "local"):
        assert (tmp_path / name / "sentinel").read_text() == name
    assert (tmp_path / "external" / ".drive-ready").is_file()


@pytest.mark.parametrize("operation", ["cache", "all"])
def test_declared_cleanup_refuses_builtin_volume_collision(
    operation: str, declared_app_config: AppConfig,
) -> None:
    config = AppConfig.model_validate({
        **declared_app_config.model_dump(),
        "mounts": {"uv-cache": {"volume": True, "target": "/home/dev/extra", "backup": "data"}},
    })
    original = {category: list(names) for category, names in VOLUME_CATEGORIES.items()}
    with (
        patch.object(container, "load_config", return_value=config),
        patch.object(container, "compose_down") as down,
        patch.object(container, "delete_volumes") as delete,
        patch.object(container, "clear_sync_path") as clear,
        patch.object(container, "network_exists", return_value=False),
        patch("djinn_in_a_box.core.docker.volume_exists") as exists,
    ):
        result = runner.invoke(app, ["clean", "all", "--force"] if operation == "all" else [
            "clean", "volumes", "--cache", "--force",
        ])
    assert result.exit_code == 1, result.output
    assert "uv-cache" in result.output and "built-in volume" in result.output
    down.assert_not_called()
    delete.assert_not_called()
    clear.assert_not_called()
    exists.assert_not_called()
    assert original == VOLUME_CATEGORIES


def test_declared_cleanup_has_no_none_selector(declared_app_config: AppConfig) -> None:
    with (
        patch.object(container, "load_config", return_value=declared_app_config),
        patch.object(container, "get_existing_volumes_by_category", return_value=[]),
    ):
        result = runner.invoke(app, ["clean", "volumes", "--none"])
    assert result.exit_code == 2
    assert "No such option" in result.output


class TestBuildCommand:
    """Tests for the build command."""

    def test_build_exits_on_failure(self) -> None:
        """Test build exits with error code on failure."""
        with (
            patch("djinn_in_a_box.commands.container.load_config"),
            patch("djinn_in_a_box.commands.container.preflight"),
            patch("djinn_in_a_box.commands.container._sync_build_files"),
            patch("djinn_in_a_box.commands.container.compose_build") as mock_build,
        ):
            mock_build.return_value = RunResult(returncode=1, stderr="Build failed")

            with pytest.raises(typer.Exit) as exc_info:
                container.build()

            assert exc_info.value.exit_code == 1

    def test_build_failure_keeps_the_buildkit_stage_tags(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`djinn build` is the command that emits bracketed BuildKit tags.

        Rich reads them as markup and deletes them, taking with it the one token
        that says which stage failed.
        """
        with (
            patch("djinn_in_a_box.commands.container.load_config"),
            patch("djinn_in_a_box.commands.container.preflight"),
            patch("djinn_in_a_box.commands.container._sync_build_files"),
            patch("djinn_in_a_box.commands.container.compose_build") as mock_build,
        ):
            mock_build.return_value = RunResult(
                returncode=1,
                stderr="#8 [dev 3/25] RUN apt-get update\n#1 [internal] load metadata\n",
            )

            with pytest.raises(typer.Exit):
                container.build()

        captured = capsys.readouterr().err
        assert "[dev 3/25]" in captured
        assert "[internal]" in captured

    def test_build_failure_without_captured_output_points_at_the_streamed_log(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A streamed build has already printed its log; there is nothing to reprint.

        The empty `stderr` is the normal case now, so the failure message must send
        the reader upwards instead of trailing off after the exit code.
        """
        with (
            patch("djinn_in_a_box.commands.container.load_config"),
            patch("djinn_in_a_box.commands.container.preflight"),
            patch("djinn_in_a_box.commands.container._sync_build_files"),
            patch("djinn_in_a_box.commands.container.compose_build") as mock_build,
        ):
            mock_build.return_value = RunResult(returncode=1)

            with pytest.raises(typer.Exit):
                container.build()

        captured = capsys.readouterr().err
        assert "exit code 1" in captured
        assert "above" in captured

    @pytest.mark.parametrize("failing", ["none", "compose", "supervisor", "install"])
    @pytest.mark.parametrize("no_cache", [False, True])
    def test_build_also_builds_and_installs_the_hostctl_supervisor(
        self, failing: str, no_cache: bool
    ) -> None:
        """The supervisor is no Compose service; build adds it and installs it after bake."""
        steps: list[str] = []

        def compose(config: object, *, no_cache: bool) -> RunResult:
            steps.append(f"compose:{no_cache}")
            return RunResult(returncode=2 if failing == "compose" else 0)

        def supervisor(*, no_cache: bool) -> RunResult:
            steps.append(f"supervisor:{no_cache}")
            return RunResult(returncode=3 if failing == "supervisor" else 0)

        def install() -> Path:
            steps.append("install")
            if failing == "install":
                raise container.hostctl.HostctlError("Docker cp failed (exit 1)")
            return Path("/state/bin/supervisor")

        with (
            patch("djinn_in_a_box.commands.container.load_config"),
            patch("djinn_in_a_box.commands.container.preflight"),
            patch("djinn_in_a_box.commands.container._sync_build_files"),
            patch("djinn_in_a_box.commands.container.compose_build", side_effect=compose),
            patch.object(container.hostctl, "build_supervisor", side_effect=supervisor),
            patch.object(container.hostctl, "install_supervisor", side_effect=install),
        ):
            if failing == "none":
                container.build(no_cache=no_cache)
            else:
                with pytest.raises(typer.Exit) as exc_info:
                    container.build(no_cache=no_cache)
                assert exc_info.value.exit_code == {"compose": 2, "supervisor": 3, "install": 1}[
                    failing
                ]

        expected = [f"compose:{no_cache}", f"supervisor:{no_cache}", "install"]
        stop = {"compose": 1, "supervisor": 2, "install": 3, "none": 3}[failing]
        assert steps == expected[:stop]

    def test_sync_build_files_uses_config_root_from_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        code_dir = tmp_path / "projects"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        repo_dotfiles = configured_root / "repo-dotfiles"
        repo_dotfiles.mkdir(parents=True)
        (repo_dotfiles / "packages.txt").write_text("ripgrep\n")
        (repo_dotfiles / "tools.txt").write_text("codex\n")
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)
        config = load_config(config_file)

        project_root = tmp_path / "repo"
        (project_root / "tools").mkdir(parents=True)
        with patch("djinn_in_a_box.commands.container.get_project_root", return_value=project_root):
            container._sync_build_files(config)

        assert (project_root / "packages.txt").read_text() == "ripgrep\n"
        assert (project_root / "tools" / "tools.txt").read_text() == "codex\n"


class TestStartCommand:
    """Tests for the start command."""

    @pytest.fixture
    def start_mocks(self) -> Generator[dict[str, Any]]:
        """Common mocks for start command tests."""
        err_output = io.StringIO()
        test_err_console = Console(
            file=err_output,
            force_terminal=True,
            no_color=True,
            width=100,
            theme=DJINN_THEME,
        )
        with (
            patch("djinn_in_a_box.commands.container.load_config") as mock_load,
            patch("djinn_in_a_box.commands.container.preflight"),
            patch(
                "djinn_in_a_box.commands.container.ensure_network", return_value=True
            ) as mock_network,
            patch("djinn_in_a_box.commands.container.compose_run") as mock_run,
            patch("djinn_in_a_box.commands.container.compose_up_detached") as mock_detached,
            patch(
                "djinn_in_a_box.commands.container.is_container_running", return_value=False
            ) as mock_running,
            patch("djinn_in_a_box.commands.container.cleanup_docker_proxy") as mock_cleanup,
            patch("djinn_in_a_box.commands.container.get_shell_mount_args", return_value=[]),
            patch("djinn_in_a_box.commands.container.banner") as mock_banner,
            patch(
                "djinn_in_a_box.commands.container.prepare_config_workflow",
                return_value=WorkflowPreparationResult(True),
            ) as mock_workflow,
            patch(
                "djinn_in_a_box.commands.container.get_project_root",
                return_value=Path("/project"),
            ),
            patch("djinn_in_a_box.core.console.err_console", test_err_console),
        ):
            mock_config = MagicMock()
            mock_config.code_dir = Path("/projects")
            mock_config.config_root = Path("/runtime")
            mock_config.shell.skip_mounts = False
            mock_load.return_value = mock_config
            mock_run.return_value = RunResult(returncode=0)
            mock_detached.return_value = RunResult(returncode=0)
            # `is_container_running` is consulted twice on the detached path: the
            # collision guard before starting (nothing there yet) and the liveness
            # check afterwards (the container is up).
            running_calls = {"count": 0}

            def _running(_name: str) -> bool:
                running_calls["count"] += 1
                return running_calls["count"] > 1

            mock_running.side_effect = _running
            yield {
                "load": mock_load,
                "run": mock_run,
                "detached": mock_detached,
                "running": mock_running,
                "cleanup": mock_cleanup,
                "config": mock_config,
                "banner": mock_banner,
                "workflow": mock_workflow,
                "network": mock_network,
                "err_output": err_output,
            }

    def test_start_prepares_both_runtime_views_before_network(
        self, start_mocks: dict[str, Any]
    ) -> None:
        with pytest.raises(typer.Exit):
            container.start()

        project_root, targets = start_mocks["workflow"].call_args.args
        assert project_root == Path("/project")
        assert [(target.tool, target.destination_root) for target in targets] == [
            ("claude", Path("/runtime/claude")),
            ("codex", Path("/runtime/codex")),
        ]
        assert start_mocks["workflow"].call_args.kwargs == {
            "config_snapshot": start_mocks["config"],
            "require_compose_host_env": True,
        }
        start_mocks["network"].assert_called_once_with()

    def test_blocked_workflow_stops_before_network_and_compose(
        self, start_mocks: dict[str, Any]
    ) -> None:
        start_mocks["workflow"].return_value = WorkflowPreparationResult(
            False,
            (WorkflowPreparationProblem("blocked", "Workflow blocked.", "Resolve drift."),),
        )

        with pytest.raises(typer.Exit) as exc_info:
            container.start()

        assert exc_info.value.exit_code == 1
        start_mocks["network"].assert_not_called()
        start_mocks["run"].assert_not_called()

    def test_start_with_docker_flag(self, start_mocks: dict[str, Any]) -> None:
        with pytest.raises(typer.Exit):
            container.start(docker=True)
        options = start_mocks["run"].call_args[0][1]
        assert options.docker_mode is DockerMode.PROXY
        start_mocks["banner"].assert_called_once_with()
        start_mocks["cleanup"].assert_called_once_with(
            DockerMode.PROXY, start_mocks["config"], owner=None
        )

    def test_start_with_firewall_flag(self, start_mocks: dict[str, Any]) -> None:
        with pytest.raises(typer.Exit):
            container.start(firewall=True)
        options = start_mocks["run"].call_args[0][1]
        assert options.firewall_enabled is True

    def test_start_detached_uses_up_instead_of_a_foreground_client(
        self, start_mocks: dict[str, Any]
    ) -> None:
        """The point of --detach: no compose client is left attached to a TTY."""
        with pytest.raises(typer.Exit) as exc_info:
            container.start(detach=True)

        assert exc_info.value.exit_code == 0
        start_mocks["detached"].assert_called_once()
        start_mocks["run"].assert_not_called()

    def test_start_detached_forwards_every_argument(self, start_mocks: dict[str, Any]) -> None:
        """`assert_called_once` alone lets --here/--firewall/mounts be dropped silently.

        That is how the audio regression reached production: the override handling
        was pinned, the delivery of the arguments to it was not.
        """
        shell_args = ["-v", "/host/.zshrc:/home/dev/.zshrc.local:ro"]
        with (
            patch(
                "djinn_in_a_box.commands.container.get_shell_mount_args", return_value=shell_args
            ),
            patch(
                "djinn_in_a_box.commands.container.resolve_container_mounts",
                return_value=(ContainerMount(Path("/host/here"), Path("/home/dev/workspace")),),
            ),
            pytest.raises(typer.Exit),
        ):
            container.start(detach=True, firewall=True, here=True)

        options = start_mocks["detached"].call_args[0][1]
        kwargs = start_mocks["detached"].call_args.kwargs
        assert options.firewall_enabled is True
        assert options.mounts[0].target == Path("/home/dev/workspace")
        assert kwargs["shell_mount_args"] == shell_args

    def test_start_detached_stays_silent_when_up_failed(
        self, start_mocks: dict[str, Any]
    ) -> None:
        start_mocks["detached"].return_value = RunResult(returncode=1, stderr="boom\n")

        with pytest.raises(typer.Exit) as exc_info:
            container.start(detach=True)

        assert exc_info.value.exit_code == 1
        assert "started in the background" not in start_mocks["err_output"].getvalue()

    def test_start_detached_keeps_the_docker_proxy_alive(
        self, start_mocks: dict[str, Any]
    ) -> None:
        """A detached container outlives this process, so its proxy must survive too."""
        with pytest.raises(typer.Exit):
            container.start(docker=True, detach=True)

        start_mocks["cleanup"].assert_not_called()

    def test_start_detached_refuses_when_a_container_already_runs(
        self, start_mocks: dict[str, Any]
    ) -> None:
        start_mocks["running"].side_effect = None
        start_mocks["running"].return_value = True

        with pytest.raises(typer.Exit) as exc_info:
            container.start(detach=True)

        assert exc_info.value.exit_code == 1
        start_mocks["detached"].assert_not_called()
        start_mocks["run"].assert_not_called()

    def test_start_detached_reports_a_container_that_died_on_startup(
        self, start_mocks: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`up -d` exits 0 once the container is created — it observes nothing after."""
        start_mocks["running"].side_effect = None
        start_mocks["running"].return_value = False

        with pytest.raises(typer.Exit) as exc_info:
            container.start(detach=True)

        assert exc_info.value.exit_code == 1
        # Both reach stderr in production; the fixture patches console.err_console,
        # so error() lands in err_output while the direct err_console.print does not.
        assert "not running after start" in start_mocks["err_output"].getvalue()
        assert "docker logs djinn" in capsys.readouterr().err

    def test_start_keeps_bracketed_docker_tokens(
        self, start_mocks: dict[str, Any]
    ) -> None:
        """Rich markup would eat the bracketed BuildKit tags that locate a failure."""
        start_mocks["detached"].return_value = RunResult(
            returncode=1, stderr="#8 [dev 3/25] RUN apt-get update\n#1 [internal] load metadata\n"
        )

        with pytest.raises(typer.Exit):
            container.start(detach=True)

        captured = start_mocks["err_output"].getvalue()
        assert "[dev 3/25]" in captured
        assert "[internal]" in captured

    def test_start_detached_says_how_to_attach(self, start_mocks: dict[str, Any]) -> None:
        with pytest.raises(typer.Exit):
            container.start(detach=True)

        assert "djinn enter" in start_mocks["err_output"].getvalue()

    def test_start_passes_each_precomputed_runtime_mount_list(
        self, start_mocks: dict[str, Any]
    ) -> None:
        shell_args = ["-v", "/host/.zshrc:/home/dev/.zshrc.local:ro"]
        with (
            patch(
                "djinn_in_a_box.commands.container.get_shell_mount_args",
                return_value=shell_args,
            ),
            pytest.raises(typer.Exit),
        ):
            container.start()

        kwargs = start_mocks["run"].call_args.kwargs
        assert kwargs["shell_mount_args"] is shell_args

    def test_start_with_here_flag(
        self, start_mocks: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(typer.Exit):
            container.start(here=True)
        options = start_mocks["run"].call_args[0][1]
        assert options.mounts == (
            ContainerMount(tmp_path, Path("/home/dev/workspace")),
        )

    def test_start_keeps_here_mount_first_with_additional_mount(
        self, start_mocks: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        additional = tmp_path / "additional"
        additional.mkdir()

        with pytest.raises(typer.Exit):
            container.start(here=True, mount=[str(additional)])

        options = start_mocks["run"].call_args[0][1]
        assert options.mounts == (
            ContainerMount(tmp_path, Path("/home/dev/workspace")),
            ContainerMount(additional, Path("/home/dev/mount/additional")),
        )

    def test_start_collects_repeatable_mounts(
        self, start_mocks: dict[str, Any], tmp_path: Path
    ) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        with pytest.raises(typer.Exit):
            container.start(mount=[str(first), f"{second}:/opt/second:ro"])
        options = start_mocks["run"].call_args[0][1]
        assert options.mounts == (
            ContainerMount(first, Path("/home/dev/mount/first")),
            ContainerMount(second, Path("/opt/second"), read_only=True),
        )

    def test_start_cli_preserves_repeated_mount_options(
        self, start_mocks: dict[str, Any], tmp_path: Path
    ) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()

        result = CliRunner().invoke(
            app,
            ["start", "--mount", str(first), "--mount", str(second)],
        )

        assert result.exit_code == 0, result.output
        options = start_mocks["run"].call_args[0][1]
        assert [mount.source for mount in options.mounts] == [first, second]

    def test_start_lists_each_resolved_mount(
        self, start_mocks: dict[str, Any], tmp_path: Path
    ) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()

        with (
            patch("djinn_in_a_box.commands.container.status_line") as status_line,
            pytest.raises(typer.Exit),
        ):
            container.start(mount=[str(first), f"{second}:/opt/second:ro"])

        mount_lines = [
            call.args for call in status_line.call_args_list if call.args[0] == "Mount"
        ]
        assert mount_lines == [
            ("Mount", f"{first} -> /home/dev/mount/first (rw)"),
            ("Mount", f"{second} -> /opt/second (ro)"),
        ]

    def test_start_reports_a_mount_collision_from_the_core(
        self, start_mocks: dict[str, Any]
    ) -> None:
        start_mocks["run"].side_effect = MountCollisionError("mount collision detail")

        with pytest.raises(typer.Exit) as exc_info:
            container.start()

        assert exc_info.value.exit_code == 1
        assert "mount collision detail" in start_mocks["err_output"].getvalue()
        start_mocks["cleanup"].assert_called_once_with(
            DockerMode.NONE, start_mocks["config"], owner=None
        )

    def test_start_reports_a_mount_specification_error_from_the_core(
        self, start_mocks: dict[str, Any]
    ) -> None:
        start_mocks["run"].side_effect = MountSpecificationError("bad volume arguments")

        with pytest.raises(typer.Exit) as exc_info:
            container.start()

        assert exc_info.value.exit_code == 1
        assert "bad volume arguments" in start_mocks["err_output"].getvalue()

    def test_start_reports_a_runtime_mount_builder_failure(
        self, start_mocks: dict[str, Any]
    ) -> None:
        start_mocks["run"].side_effect = RuntimeMountSpecificationError("bad builder output")

        with pytest.raises(typer.Exit) as exc_info:
            container.start()

        assert exc_info.value.exit_code == 1
        assert "Internal runtime mount construction failed" in start_mocks["err_output"].getvalue()

    def test_start_prints_compose_stderr(self, start_mocks: dict[str, Any]) -> None:
        start_mocks["run"].return_value = RunResult(returncode=127, stderr="docker missing\n")

        with pytest.raises(typer.Exit) as exc_info:
            container.start()

        assert exc_info.value.exit_code == 127
        assert "docker missing" in start_mocks["err_output"].getvalue()

    def test_start_uses_the_common_mount_path_validation(
        self, start_mocks: dict[str, Any], tmp_path: Path
    ) -> None:
        missing = tmp_path / "missing"

        with (
            patch("djinn_in_a_box.commands.container.error") as error,
            pytest.raises(typer.Exit) as exc_info,
        ):
            container.start(mount=[str(missing)])

        assert exc_info.value.exit_code == 1
        error.assert_called_once_with(f"Mount path does not exist: {missing}")
        start_mocks["run"].assert_not_called()

    @pytest.mark.parametrize("mount", ["~nosuchuser/x", "\x00"])
    def test_start_cli_reports_unresolvable_mount_without_traceback(
        self, start_mocks: dict[str, Any], mount: str
    ) -> None:
        result = CliRunner().invoke(app, ["start", "--mount", mount])

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Mount path cannot be resolved" in start_mocks["err_output"].getvalue()
        start_mocks["run"].assert_not_called()

    def test_start_cli_reports_unresolvable_mount_target_without_traceback(
        self, start_mocks: dict[str, Any], tmp_path: Path
    ) -> None:
        result = CliRunner().invoke(
            app,
            ["start", "--mount", f"{tmp_path}:/work\x00bad"],
        )

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Mount target cannot contain a NUL byte" in start_mocks["err_output"].getvalue()
        start_mocks["run"].assert_not_called()

    def test_start_exits_on_config_not_found(self, tmp_path: Path) -> None:
        from djinn_in_a_box.core.exceptions import ConfigNotFoundError

        with patch("djinn_in_a_box.commands.container.load_config") as mock_load:
            mock_load.side_effect = ConfigNotFoundError(tmp_path / "config.toml")
            with pytest.raises(typer.Exit) as exc_info:
                container.start()
            assert exc_info.value.exit_code == 1

    def test_start_with_docker_direct_flag(self, start_mocks: dict[str, Any]) -> None:
        with pytest.raises(typer.Exit):
            container.start(docker_direct=True)
        options = start_mocks["run"].call_args[0][1]
        assert options.docker_mode is DockerMode.DIRECT
        start_mocks["cleanup"].assert_called_once_with(
            DockerMode.DIRECT, start_mocks["config"], owner=None
        )

    def test_start_renders_environment_and_container_rules(
        self, start_mocks: dict[str, Any]
    ) -> None:
        with pytest.raises(typer.Exit):
            container.start()

        rendered = start_mocks["err_output"].getvalue()
        assert "Environment" in rendered
        assert "Container" in rendered
        rendered_plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        assert "\n\n─ Container" in rendered_plain
        assert "\n\n\n─ Container" not in rendered_plain
        start_mocks["run"].assert_called_once()
        assert start_mocks["run"].call_args.args[0] is start_mocks["config"]
        assert start_mocks["run"].call_args.kwargs["interactive"] is True

    def test_start_docker_and_direct_mutually_exclusive(self) -> None:
        with pytest.raises(typer.Exit) as exc_info:
            container.start(docker=True, docker_direct=True)
        assert exc_info.value.exit_code == 1


class TestStatusCommand:
    """Tests for the status command."""

    def test_status_handles_missing_config(self, tmp_path: Path) -> None:
        """Test status handles missing configuration gracefully."""
        from djinn_in_a_box.core.exceptions import ConfigNotFoundError

        config_file = tmp_path / "nonexistent" / "config.toml"

        with (
            patch("djinn_in_a_box.commands.container.load_config") as mock_load,
            patch("subprocess.run") as mock_run,
            patch(
                "djinn_in_a_box.commands.container.get_existing_volumes_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.container.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch("djinn_in_a_box.commands.container.network_exists", return_value=True),
            patch("djinn_in_a_box.commands.container.is_container_running", return_value=False),
        ):
            mock_load.side_effect = ConfigNotFoundError(config_file)
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            # Should not raise
            container.status()


class TestCleanDefaultCommand:
    """Tests for the clean default behavior."""

    def test_clean_default_runs_compose_down(self, mock_home: Path) -> None:
        """Test clean without subcommand runs compose down."""
        from typer import Context

        assert mock_home.is_dir()

        with patch("djinn_in_a_box.commands.container.compose_down") as mock_down:
            mock_down.return_value = RunResult(returncode=0)

            # Create a mock context with no invoked subcommand
            mock_ctx = MagicMock(spec=Context)
            mock_ctx.invoked_subcommand = None

            container.clean_default(mock_ctx)

            mock_down.assert_called_once()


class TestZoneRootErrors:
    def test_start_renders_a_regular_file_zone_root_as_a_cli_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        projects = tmp_path / "projects"
        projects.mkdir()
        config_root = tmp_path / "config"
        config_root.write_text("not a directory")
        config = AppConfig(code_dir=projects, config_root=config_root)
        monkeypatch.setattr(container, "load_config", lambda: config)
        monkeypatch.setattr(container, "preflight", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(container, "get_project_root", lambda: tmp_path / "project")
        monkeypatch.setattr(
            "djinn_in_a_box.core.config_workflow.workflow_image_compatible",
            lambda: WorkflowImageCompatibility.COMPATIBLE,
        )

        with pytest.raises(typer.Exit) as exc_info:
            container.start()

        assert exc_info.value.exit_code == 1
        assert config_root.read_text() == "not a directory"


class TestCleanVolumesCommand:
    """Tests for the clean volumes command."""

    def test_clean_volumes_lists_without_flags(
        self, tmp_path: Path, mock_app_config: AppConfig
    ) -> None:
        """Test clean volumes without flags lists volumes and sync paths."""
        with (
            patch("djinn_in_a_box.commands.container.load_config", return_value=mock_app_config),
            patch(
                "djinn_in_a_box.commands.container.get_existing_volumes_by_category",
                return_value=["djinn-uv-cache"],
            ) as mock_vol,
            patch(
                "djinn_in_a_box.commands.container.get_existing_sync_paths_by_category",
                return_value=[tmp_path / "claude"],
            ) as mock_sync,
        ):
            container.clean_volumes()

            # Volume categories: cache, data (2)
            assert mock_vol.call_count == 3
            # Sync categories: credentials, repo-dotfiles (2)
            assert mock_sync.call_count == 2

    def test_clean_volumes_lists_sync_paths_from_config_file(
        self, tmp_path: Path, mock_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """clean volumes list mode honors general.config_root from config.toml."""
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        code_dir = tmp_path / "projects"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        default_root = mock_home / ".djinn" / "config"
        (configured_root / "claude").mkdir(parents=True)
        (default_root / "claude").mkdir(parents=True)
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)

        with (
            patch(
                "djinn_in_a_box.commands.container.load_config",
                side_effect=lambda: load_config(config_file),
            ),
            patch.dict("djinn_in_a_box.commands.container.VOLUME_CATEGORIES", {}, clear=True),
            patch.dict(
                "djinn_in_a_box.commands.container.SYNC_PATHS",
                {"credentials": ["claude"]},
                clear=True,
            ),
            patch.dict(
                "djinn_in_a_box.core.docker.SYNC_PATHS",
                {"credentials": ["claude"]},
                clear=True,
            ),
            patch("djinn_in_a_box.commands.container._print_resource_table") as mock_table,
        ):
            container.clean_volumes()

        mock_table.assert_called_once()
        entries = mock_table.call_args.args[2]
        assert entries == {"credentials": [str(configured_root / "claude")]}

    def test_clean_volumes_clears_credentials(
        self, tmp_path: Path, mock_app_config: AppConfig
    ) -> None:
        """Test clean volumes --credentials clears credential sync paths."""
        fake_path = tmp_path / "claude"
        with (
            patch("djinn_in_a_box.commands.container.load_config", return_value=mock_app_config),
            patch(
                "djinn_in_a_box.commands.container.get_existing_sync_paths_by_category",
                return_value=[fake_path],
            ) as mock_get,
            patch(
                "djinn_in_a_box.commands.container.clear_sync_path",
                return_value=True,
            ) as mock_clear,
        ):
            container.clean_volumes(credentials=True, force=True)

            mock_get.assert_called_with("credentials", mock_app_config)
            mock_clear.assert_called_once_with(fake_path)

    def test_clean_volumes_clears_sync_paths_from_config_file(
        self, tmp_path: Path, mock_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """clean volumes --credentials clears the configured-root sync path."""
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        code_dir = tmp_path / "projects"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        default_root = mock_home / ".djinn" / "config"
        (configured_root / "claude").mkdir(parents=True)
        (default_root / "claude").mkdir(parents=True)
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)

        with (
            patch(
                "djinn_in_a_box.commands.container.load_config",
                side_effect=lambda: load_config(config_file),
            ),
            patch.dict(
                "djinn_in_a_box.commands.container.SYNC_PATHS",
                {"credentials": ["claude"]},
                clear=True,
            ),
            patch.dict(
                "djinn_in_a_box.core.docker.SYNC_PATHS",
                {"credentials": ["claude"]},
                clear=True,
            ),
            patch(
                "djinn_in_a_box.commands.container.clear_sync_path",
                return_value=True,
            ) as mock_clear,
        ):
            container.clean_volumes(credentials=True, force=True)

        mock_clear.assert_called_once_with(configured_root / "claude")

    def test_clean_volumes_env_root_wins_over_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DJINN_CONFIG_ROOT still takes precedence during clean volumes."""
        code_dir = tmp_path / "projects"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        env_root = tmp_path / "env-root"
        (configured_root / "unused-agent").mkdir(parents=True)
        (env_root / "claude").mkdir(parents=True)
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(env_root))
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)

        with (
            patch(
                "djinn_in_a_box.commands.container.load_config",
                side_effect=lambda: load_config(config_file),
            ),
            patch.dict(
                "djinn_in_a_box.commands.container.SYNC_PATHS",
                {"credentials": ["claude", "unused-agent"]},
                clear=True,
            ),
            patch.dict(
                "djinn_in_a_box.core.docker.SYNC_PATHS",
                {"credentials": ["claude", "unused-agent"]},
                clear=True,
            ),
            patch(
                "djinn_in_a_box.commands.container.clear_sync_path",
                return_value=True,
            ) as mock_clear,
        ):
            container.clean_volumes(credentials=True, force=True)

        mock_clear.assert_called_once_with(env_root / "claude")

    def test_clean_volumes_missing_config_uses_default_root(
        self, tmp_path: Path, mock_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing config keeps the existing default-root clean behavior."""
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        default_root = mock_home / ".djinn" / "config"
        (default_root / "claude").mkdir(parents=True)

        with (
            patch(
                "djinn_in_a_box.commands.container.load_config",
                side_effect=ConfigNotFoundError(tmp_path / "missing.toml"),
            ),
            patch.dict(
                "djinn_in_a_box.commands.container.SYNC_PATHS",
                {"credentials": ["claude"]},
                clear=True,
            ),
            patch.dict(
                "djinn_in_a_box.core.docker.SYNC_PATHS",
                {"credentials": ["claude"]},
                clear=True,
            ),
            patch(
                "djinn_in_a_box.commands.container.clear_sync_path",
                return_value=True,
            ) as mock_clear,
        ):
            container.clean_volumes(credentials=True, force=True)

        mock_clear.assert_called_once_with(default_root / "claude")

    def test_clean_volumes_credentials_requires_confirmation(
        self, tmp_path: Path, mock_app_config: AppConfig
    ) -> None:
        """--credentials without --force prompts for the destructive-clear confirmation."""
        fake_path = tmp_path / "claude"
        with (
            patch("djinn_in_a_box.commands.container.load_config", return_value=mock_app_config),
            patch(
                "djinn_in_a_box.commands.container.get_existing_sync_paths_by_category",
                return_value=[fake_path],
            ),
            patch(
                "djinn_in_a_box.commands.container.clear_sync_path",
                return_value=True,
            ),
            patch("typer.confirm") as mock_confirm,
        ):
            container.clean_volumes(credentials=True)
            mock_confirm.assert_called_once()

    def test_clean_volumes_deletes_cache_volumes(self, mock_app_config: AppConfig) -> None:
        """Test clean volumes --cache deletes cache named volumes."""
        with (
            patch("djinn_in_a_box.commands.container.load_config", return_value=mock_app_config),
            patch(
                "djinn_in_a_box.commands.container.get_existing_volumes_by_category",
                return_value=["djinn-uv-cache"],
            ) as mock_get,
            patch(
                "djinn_in_a_box.commands.container.delete_volumes",
                return_value={"djinn-uv-cache": True},
            ) as mock_delete,
        ):
            container.clean_volumes(cache=True)

            mock_get.assert_called_with("cache", mock_app_config)
            mock_delete.assert_called_once()

    def test_clean_volumes_deletes_specific_volume(self) -> None:
        """Test clean volumes <name> deletes specific volume."""
        with (
            patch("djinn_in_a_box.commands.container.volume_exists", return_value=True),
            patch("djinn_in_a_box.commands.container.delete_volume") as mock_delete,
        ):
            mock_delete.return_value = True

            container.clean_volumes(name="djinn-test-volume")

            mock_delete.assert_called_once_with("djinn-test-volume")

    def test_clean_volumes_errors_on_nonexistent(self) -> None:
        """Test clean volumes <name> errors if volume doesn't exist."""
        with patch("djinn_in_a_box.commands.container.volume_exists", return_value=False):
            with pytest.raises(typer.Exit) as exc_info:
                container.clean_volumes(name="nonexistent-volume")

            assert exc_info.value.exit_code == 1


class TestCleanAllCommand:
    """Tests for the clean all command."""

    def test_clean_all_requires_confirmation(self) -> None:
        """Test clean all requires user confirmation."""
        with (
            patch("typer.confirm", return_value=False) as confirm,
            pytest.raises(typer.Exit),
        ):
            container.clean_all()
        assert "Shared and local zone data are not removed" in confirm.call_args.args[0]

    def test_clean_all_help_excludes_zone_data(self) -> None:
        result = runner.invoke(app, ["clean", "all", "--help"])

        assert result.exit_code == 0, result.output
        assert "Shared and local" in result.output
        assert "not removed" in result.output

    def test_clean_all_with_force_skips_confirmation(self, mock_app_config: AppConfig) -> None:
        """Test clean all --force skips confirmation and clears both volumes and sync paths."""
        with (
            patch("djinn_in_a_box.commands.container.load_config", return_value=mock_app_config),
            patch("djinn_in_a_box.commands.container.compose_down") as mock_down,
            patch("djinn_in_a_box.commands.container.volume_categories", return_value={}),
            patch("djinn_in_a_box.commands.container.SYNC_PATHS", {}),
            patch("djinn_in_a_box.commands.container.network_exists", return_value=False),
        ):
            mock_down.return_value = RunResult(returncode=0)

            container.clean_all(force=True)

            mock_down.assert_called_once()

    def test_clean_all_clears_sync_paths_from_config_file(
        self, tmp_path: Path, mock_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """clean all targets sync paths under general.config_root."""
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        code_dir = tmp_path / "projects"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        default_root = mock_home / ".djinn" / "config"
        (configured_root / "claude").mkdir(parents=True)
        (default_root / "claude").mkdir(parents=True)
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)

        with (
            patch(
                "djinn_in_a_box.commands.container.load_config",
                side_effect=lambda: load_config(config_file),
            ),
            patch(
                "djinn_in_a_box.commands.container.compose_down",
                return_value=RunResult(returncode=0),
            ),
            patch.dict("djinn_in_a_box.commands.container.VOLUME_CATEGORIES", {}, clear=True),
            patch.dict(
                "djinn_in_a_box.commands.container.SYNC_PATHS",
                {"credentials": ["claude"]},
                clear=True,
            ),
            patch.dict(
                "djinn_in_a_box.core.docker.SYNC_PATHS",
                {"credentials": ["claude"]},
                clear=True,
            ),
            patch("djinn_in_a_box.commands.container.delete_volumes", return_value={}),
            patch(
                "djinn_in_a_box.commands.container.clear_sync_path",
                return_value=True,
            ) as mock_clear,
            patch("djinn_in_a_box.commands.container.network_exists", return_value=False),
        ):
            container.clean_all(force=True)

        mock_clear.assert_called_once_with(configured_root / "claude")

    def test_clean_all_env_root_wins_over_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DJINN_CONFIG_ROOT still takes precedence during clean all."""
        code_dir = tmp_path / "projects"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        env_root = tmp_path / "env-root"
        (configured_root / "unused-agent").mkdir(parents=True)
        (env_root / "claude").mkdir(parents=True)
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(env_root))
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)

        with (
            patch(
                "djinn_in_a_box.commands.container.load_config",
                side_effect=lambda: load_config(config_file),
            ),
            patch(
                "djinn_in_a_box.commands.container.compose_down",
                return_value=RunResult(returncode=0),
            ),
            patch.dict("djinn_in_a_box.commands.container.VOLUME_CATEGORIES", {}, clear=True),
            patch.dict(
                "djinn_in_a_box.commands.container.SYNC_PATHS",
                {"credentials": ["claude", "unused-agent"]},
                clear=True,
            ),
            patch.dict(
                "djinn_in_a_box.core.docker.SYNC_PATHS",
                {"credentials": ["claude", "unused-agent"]},
                clear=True,
            ),
            patch("djinn_in_a_box.commands.container.delete_volumes", return_value={}),
            patch(
                "djinn_in_a_box.commands.container.clear_sync_path",
                return_value=True,
            ) as mock_clear,
            patch("djinn_in_a_box.commands.container.network_exists", return_value=False),
        ):
            container.clean_all(force=True)

        mock_clear.assert_called_once_with(env_root / "claude")

    def test_clean_all_missing_config_uses_default_root(
        self, tmp_path: Path, mock_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing config keeps clean all on the default root."""
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        default_root = mock_home / ".djinn" / "config"
        (default_root / "claude").mkdir(parents=True)

        with (
            patch(
                "djinn_in_a_box.commands.container.load_config",
                side_effect=ConfigNotFoundError(tmp_path / "missing.toml"),
            ),
            patch(
                "djinn_in_a_box.commands.container.compose_down",
                return_value=RunResult(returncode=0),
            ),
            patch.dict("djinn_in_a_box.commands.container.VOLUME_CATEGORIES", {}, clear=True),
            patch.dict(
                "djinn_in_a_box.commands.container.SYNC_PATHS",
                {"credentials": ["claude"]},
                clear=True,
            ),
            patch.dict(
                "djinn_in_a_box.core.docker.SYNC_PATHS",
                {"credentials": ["claude"]},
                clear=True,
            ),
            patch("djinn_in_a_box.commands.container.delete_volumes", return_value={}),
            patch(
                "djinn_in_a_box.commands.container.clear_sync_path",
                return_value=True,
            ) as mock_clear,
            patch("djinn_in_a_box.commands.container.network_exists", return_value=False),
        ):
            container.clean_all(force=True)

        mock_clear.assert_called_once_with(default_root / "claude")

    def test_clean_all_invalid_config_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An invalid config must abort clean, never fall back to the default root."""
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)

        with (
            patch(
                "djinn_in_a_box.commands.container.load_config",
                side_effect=ConfigValidationError("broken config"),
            ),
            patch("djinn_in_a_box.commands.container.clear_sync_path") as mock_clear,
            pytest.raises(typer.Exit) as exc_info,
        ):
            container.clean_all(force=True)

        assert exc_info.value.exit_code == 1
        mock_clear.assert_not_called()


class TestAuditCommand:
    """Tests for the audit command."""

    def test_audit_requires_proxy_running(self) -> None:
        """Test audit requires docker proxy to be running."""
        with patch("djinn_in_a_box.commands.container.is_container_running", return_value=False):
            with pytest.raises(typer.Exit) as exc_info:
                container.audit()

            assert exc_info.value.exit_code == 1

    def test_audit_shows_logs(self) -> None:
        """Test audit shows proxy logs."""
        with (
            patch("djinn_in_a_box.commands.container.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            # Successful audit returns normally (no exit)
            container.audit()

            # Should have called docker logs
            call_args = mock_run.call_args[0][0]
            assert container.DOCKER_EXECUTABLE in call_args
            assert "logs" in call_args

    def test_audit_with_tail_option(self) -> None:
        """Test audit -n option sets tail count."""
        with (
            patch("djinn_in_a_box.commands.container.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            # Successful audit returns normally (no exit)
            container.audit(tail=100)

            call_args = mock_run.call_args[0][0]
            assert "--tail" in call_args
            assert "100" in call_args

    def test_audit_propagates_error_exit_code(self) -> None:
        """Test audit propagates error exit code from docker logs."""
        with (
            patch("djinn_in_a_box.commands.container.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1)

            with pytest.raises(typer.Exit) as exc_info:
                container.audit()

            assert exc_info.value.exit_code == 1


class TestUpdateCommand:
    """Tests for the update command."""

    def test_update_runs_script(self, tmp_path: Path) -> None:
        """Test update runs update-agents.sh script."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        script_path = scripts_dir / "update-agents.sh"
        script_path.write_text("#!/bin/bash\necho 'update'")

        with (
            patch("djinn_in_a_box.commands.container.get_project_root", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            container.update()

            call_args = mock_run.call_args[0][0]
            assert str(script_path) in call_args

    def test_update_errors_if_script_missing(self, tmp_path: Path) -> None:
        """Test update errors if script doesn't exist."""
        with patch("djinn_in_a_box.commands.container.get_project_root", return_value=tmp_path):
            with pytest.raises(typer.Exit) as exc_info:
                container.update()

            assert exc_info.value.exit_code == 1


class TestEnterCommand:
    """Tests for the enter command."""

    def test_enter_requires_tty(self) -> None:
        """Test enter requires a TTY."""
        with patch("djinn_in_a_box.commands.container.sys") as mock_sys:
            mock_sys.stdin.isatty.return_value = False
            with pytest.raises(typer.Exit) as exc_info:
                container.enter()

            assert exc_info.value.exit_code == 1

    def test_enter_requires_running_container(self) -> None:
        """Test enter requires a running djinn container."""
        with (
            patch("djinn_in_a_box.commands.container.sys") as mock_sys,
            patch("djinn_in_a_box.commands.container.get_running_containers", return_value=[]),
        ):
            mock_sys.stdin.isatty.return_value = True
            with pytest.raises(typer.Exit) as exc_info:
                container.enter()

            assert exc_info.value.exit_code == 1

    def test_enter_refuses_an_unknown_container_state(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("djinn_in_a_box.commands.container.sys") as mock_sys,
            patch(
                "djinn_in_a_box.core.docker.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["docker", "ps"], 1, stdout="", stderr="daemon unavailable"
                ),
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            mock_sys.stdin.isatty.return_value = True
            container.enter()

        assert exc_info.value.exit_code == 1
        output = capsys.readouterr().err
        assert "Could not determine whether a Djinn container is running." in output

    def test_enter_opens_shell(self) -> None:
        """Test enter opens zsh shell in running container."""
        with (
            patch("djinn_in_a_box.commands.container.sys") as mock_sys,
            patch("djinn_in_a_box.commands.container.get_running_containers") as mock_get,
            patch("subprocess.run") as mock_run,
        ):
            mock_sys.stdin.isatty.return_value = True
            mock_get.return_value = ["djinn"]
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(typer.Exit) as exc_info:
                container.enter()

            assert exc_info.value.exit_code == 0
            call_args = mock_run.call_args[0][0]
            assert container.DOCKER_EXECUTABLE in call_args
            assert "exec" in call_args
            assert "-it" in call_args
            assert "zsh" in call_args
            assert "djinn" in call_args

    @pytest.mark.parametrize("dev_running", [True, False])
    def test_enter_targets_dev_not_helpers(self, dev_running: bool) -> None:
        """Desktop and hostctl helpers share the name prefix; only dev has a shell."""
        names = ["djinn-in-a-box-audio-helper-1", "djinn-in-a-box-dbus-helper-1", "djinn-hostctl"]
        if dev_running:
            names.append("djinn")

        def docker_ps(cmd: list[str]) -> list[str]:
            # Docker's name filter is an unanchored regular-expression search.
            pattern = cmd[cmd.index("--filter") + 1].removeprefix("name=")
            return [name for name in names if re.search(pattern, name)]

        with (
            patch("djinn_in_a_box.commands.container.sys") as mock_sys,
            patch("djinn_in_a_box.core.docker._docker_list", side_effect=docker_ps),
            patch("subprocess.run") as mock_run,
        ):
            mock_sys.stdin.isatty.return_value = True
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(typer.Exit) as exc_info:
                container.enter()

            if dev_running:
                assert exc_info.value.exit_code == 0
                call_args = mock_run.call_args[0][0]
                assert call_args[call_args.index("-it") + 1] == "djinn"
            else:
                assert exc_info.value.exit_code == 1
                mock_run.assert_not_called()


class TestResourceTable:
    """Tests for _print_resource_table function."""

    @pytest.fixture
    def capture_container_stdout(self) -> Generator[io.StringIO]:
        """Capture container module's console (stdout) output."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True, theme=DJINN_THEME)
        with patch("djinn_in_a_box.commands.container.console", test_console):
            yield output

    def test_print_resource_table_volumes(self, capture_container_stdout: io.StringIO) -> None:
        """_print_resource_table renders volumes by category."""
        entries = {
            "cache": ["djinn-uv-cache", "djinn-tools-cache"],
            "data": ["djinn-opencode-data"],
        }
        container._print_resource_table("Djinn Volumes", "Volume", entries)
        result = capture_container_stdout.getvalue()
        assert "Cache" in result
        assert "djinn-uv-cache" in result
        assert "djinn-opencode-data" in result

    def test_print_resource_table_sync_paths(self, capture_container_stdout: io.StringIO) -> None:
        """_print_resource_table also renders sync paths with custom header."""
        entries = {
            "credentials": ["/home/user/.djinn/sync/claude"],
        }
        container._print_resource_table("Djinn Sync Paths", "Path", entries)
        result = capture_container_stdout.getvalue()
        assert "Credentials" in result
        assert "/home/user/.djinn/sync/claude" in result


def test_enter_does_not_inject_declarations(monkeypatch):
    from djinn_in_a_box.core import docker

    forbidden = MagicMock(side_effect=AssertionError("exec must inherit"))
    monkeypatch.setattr(docker, "resolve_declared_entries", forbidden)
    monkeypatch.setattr(container, "load_config", forbidden)
    monkeypatch.setattr(container.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(container, "get_running_containers", lambda prefix: ["djinn"])
    run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(container.subprocess, "run", run)
    with pytest.raises(typer.Exit) as exc:
        container.enter()
    assert exc.value.exit_code == 0
    assert run.call_args.args[0] == [container.DOCKER_EXECUTABLE, "exec", "-it", "djinn", "zsh"]
    forbidden.assert_not_called()
