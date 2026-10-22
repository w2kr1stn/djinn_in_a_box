"""Tests for container lifecycle commands."""

import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from rich.console import Console
from rich.text import Text
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


@pytest.fixture(autouse=True)
def _isolate_opencode_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_home: Path
) -> None:
    from djinn_in_a_box.core import assistant
    from djinn_in_a_box.core import opencode_credentials as oc

    monkeypatch.setattr(assistant, "container_exists", lambda _: False)
    monkeypatch.setattr(container, "recover_leftover_sessions", assistant.recover_leftover_sessions)
    monkeypatch.setattr(
        oc, "default_audit_root", lambda: str(mock_home / ".local/state/djinn/opencode-audit")
    )
    monkeypatch.setattr(container, "export_opencode_data_credentials", lambda: b"[]")


@pytest.mark.parametrize("command", ["volumes", "all"])
@pytest.mark.parametrize("name", ["claude", "repo-dotfiles"])
@pytest.mark.parametrize("kind", ["dangling", "file"])
def test_clean_reports_refused_sync_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, name: str, kind: str
) -> None:
    code = tmp_path / "code"
    code.mkdir()
    root = tmp_path / "config"
    root.mkdir()
    target = root / name
    if kind == "dangling":
        target.symlink_to(tmp_path / "missing")
    else:
        target.write_bytes(b"wrong type")
    config = AppConfig(code_dir=code, config_root=root)
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    args = ["clean", command, "--force"]
    if command == "volumes":
        args.append("--credentials" if name == "claude" else "--repo-dotfiles")
    with (
        patch.object(container, "load_config", return_value=config),
        patch.object(container, "compose_down", return_value=RunResult(0)),
        patch.object(container, "delete_volumes", return_value={}),
        patch.object(container, "network_exists", return_value=False),
        patch.object(container, "clear_sync_path", wraps=container.clear_sync_path) as clear,
    ):
        result = runner.invoke(app, args)
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "Cleanup complete." not in result.output
    assert f"Failed: {target}" in result.output.replace("\n", ""), result.output
    assert f"Cleared: {target}" not in result.output.replace("\n", "")
    clear.assert_called_once_with(target)
    assert target.is_symlink() if kind == "dangling" else target.read_bytes() == b"wrong type"


@pytest.mark.parametrize("command", ["volumes", "all"])
def test_clean_sync_failure_continues_all_paths_and_remaining_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    code = tmp_path / "code"
    code.mkdir()
    root = tmp_path / "config"
    root.mkdir()
    (root / "claude").symlink_to(tmp_path / "missing")
    for name in ("codex", "repo-dotfiles"):
        (root / name).mkdir()
        (root / name / "delete").write_bytes(b"delete")
    config = AppConfig(code_dir=code, config_root=root)
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    calls: list[str] = []
    real_clear = container.clear_sync_path

    def clear(path: Path) -> bool:
        calls.append(path.name)
        return real_clear(path)

    def network(_name: str) -> bool:
        calls.append("network")
        return True

    args = ["clean", command, "--force"]
    if command == "volumes":
        args.extend(["--credentials", "--repo-dotfiles"])
    with (
        patch.object(container, "load_config", return_value=config),
        patch.object(container, "compose_down", return_value=RunResult(0)),
        patch.object(container, "delete_volumes", return_value={}),
        patch.object(container, "network_exists", side_effect=network),
        patch.object(container, "delete_network", return_value=True) as delete_network,
        patch.object(container, "clear_sync_path", side_effect=clear),
        patch.object(container, "error", wraps=container.error) as error,
    ):
        result = runner.invoke(app, args)
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert calls == ["claude", "codex", "repo-dotfiles"] + (["network"] if command == "all" else [])
    assert f"Failed: {root / 'claude'}" in result.output.replace("\n", "")
    assert "Cleanup complete." not in result.output
    error.assert_called_once_with(f"  Failed: {root / 'claude'}")
    if command == "all":
        delete_network.assert_called_once()
    assert list((root / "codex").iterdir()) == []
    assert list((root / "repo-dotfiles").iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses filesystem EACCES")
def test_clean_volumes_discovery_permission_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = tmp_path / "code"
    code.mkdir()
    root = tmp_path / "config"
    (root / "claude").mkdir(parents=True)
    sentinel = root / "claude/keep"
    sentinel.write_bytes(b"preserved")
    config = AppConfig(code_dir=code, config_root=root)
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    root.chmod(0)
    try:
        with patch.object(container, "load_config", return_value=config):
            result = runner.invoke(app, ["clean", "volumes", "--credentials", "--force"])
        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        assert "OpenCode credentials refused" in result.output
        assert "Cleanup complete." not in result.output
    finally:
        root.chmod(0o700)
    assert sentinel.read_bytes() == b"preserved"


@pytest.mark.parametrize("command", ["status", "listing"])
@pytest.mark.parametrize("configured", [True, False], ids=["configured", "missing-config"])
def test_status_declared_volume_categories(
    command: str, configured: bool, declared_app_config: AppConfig,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    expected = {
        "cache": ["djinn-agent-docker", "djinn-uv-cache", "djinn-tools-cache",
                  "djinn-vscode-server"],
        "data": ["djinn-opencode-data", "djinn-vscode-workspaces", "djinn-packages"],
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
    builtins_cache = {"djinn-agent-docker", "djinn-uv-cache", "djinn-tools-cache",
                      "djinn-vscode-server"}
    builtins_data = {"djinn-opencode-data", "djinn-vscode-workspaces", "djinn-packages"}
    expected_volumes = {
        "cache": {"djinn-uv-cache", "djinn-tools-cache", "djinn-scratch", "djinn-agent-docker"},
        "data": {"djinn-opencode-data", "djinn-packages", "djinn-journal"},
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

    @pytest.mark.parametrize(
        "failing", ["none", "compose", "supervisor", "install", "pull", "present"]
    )
    @pytest.mark.parametrize("no_cache", [False, True])
    def test_build_also_builds_and_installs_the_hostctl_supervisor(
        self, failing: str, no_cache: bool, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Build installs hostctl, then pulls the pinned companion image if absent."""
        steps: list[str] = []

        def compose(config: object, *, no_cache: bool, agent_args: object) -> RunResult:
            assert agent_args == {}
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

        def inspect(name: str, docker_path: str, resource: str) -> dict[str, str] | None:
            steps.append("inspect-image")
            assert name == container.agent_docker.IMAGE
            assert docker_path == container.DOCKER_EXECUTABLE
            assert resource == "image"
            return {"Id": "sha256:pinned"} if failing == "present" else None

        def pull(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            steps.append("pull")
            assert argv == [container.DOCKER_EXECUTABLE, "pull", container.agent_docker.IMAGE]
            assert kwargs["stdin"] is subprocess.DEVNULL
            assert kwargs["timeout"] == 120
            assert kwargs["cwd"] == "/"
            return subprocess.CompletedProcess(
                argv,
                17 if failing == "pull" else 0,
                "",
                "registry unavailable" if failing == "pull" else "",
            )

        with (
            patch("djinn_in_a_box.commands.container.load_config"),
            patch("djinn_in_a_box.commands.container.preflight"),
            patch("djinn_in_a_box.commands.container._sync_build_files"),
            patch("djinn_in_a_box.commands.container.compose_build", side_effect=compose),
            patch.object(container.hostctl, "build_supervisor", side_effect=supervisor),
            patch.object(container.hostctl, "install_supervisor", side_effect=install),
            patch.object(container.host_runtime, "inspect_object", side_effect=inspect),
            patch.object(container.subprocess, "run", side_effect=pull),
        ):
            if failing in ("none", "present"):
                container.build(no_cache=no_cache)
            else:
                with pytest.raises(typer.Exit) as exc_info:
                    container.build(no_cache=no_cache)
                assert exc_info.value.exit_code == {
                    "compose": 2,
                    "supervisor": 3,
                    "install": 1,
                    "pull": 17,
                }[failing]

        expected = [f"compose:{no_cache}", f"supervisor:{no_cache}", "install"]
        if failing in ("none", "pull", "present"):
            expected.append("inspect-image")
        if failing in ("none", "pull"):
            expected.append("pull")
        stop = {"compose": 1, "supervisor": 2, "install": 3}.get(failing, len(expected))
        assert steps == expected[:stop]
        if failing == "pull":
            assert "pinned agent Docker image failed" in capsys.readouterr().err

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


    @pytest.mark.parametrize("failure", ["toml", "unknown", "value", "read"])
    def test_build_invalid_record_stops_before_preflight(
        self, tmp_path, mock_app_config, monkeypatch, failure,
    ):
        from djinn_in_a_box.core import paths

        record = paths.AGENT_VERSIONS_FILE
        record.parent.mkdir(parents=True)
        record.write_text({"toml": "[", "unknown": 'DOCKER_VERSION = "29.0.0"',
                           "value": 'CODEX_VERSION = "beta"', "read": ""}[failure])
        if failure == "read":
            original = Path.open

            def opened(self, *args, **kwargs):
                if self == record:
                    raise OSError("record permission denied")
                return original(self, *args, **kwargs)

            monkeypatch.setattr(Path, "open", opened)
        monkeypatch.setattr(container, "load_config", lambda: mock_app_config)
        monkeypatch.setattr(container, "get_project_root", lambda: tmp_path)
        stopped = {}
        for name in ("preflight", "_sync_build_files", "compose_build",
                     "_pull_agent_docker_image_if_missing"):
            stopped[name] = MagicMock(side_effect=AssertionError(f"unexpected {name}"))
            monkeypatch.setattr(container, name, stopped[name])
        for name in ("build_supervisor", "install_supervisor"):
            stopped[name] = MagicMock(side_effect=AssertionError(f"unexpected {name}"))
            monkeypatch.setattr(container.hostctl, name, stopped[name])
        result = runner.invoke(app, ["build"])
        assert result.exit_code == 1
        assert str(record) in result.output.replace("\n", "")
        for called in stopped.values():
            called.assert_not_called()

    @pytest.mark.parametrize("recorded", [False, True])
    def test_build_passes_resolved_agent_args(
        self, tmp_path, mock_app_config, monkeypatch, recorded,
    ):
        from djinn_in_a_box.core import agent_versions

        (tmp_path / "Dockerfile").write_text(
            "ARG CLAUDE_CODE_VERSION=2.1.288\nARG CODEX_VERSION=0.160.0\n"
        )
        if recorded:
            agent_versions.save_versions({
                "CLAUDE_CODE_VERSION": "2.1.300", "CODEX_VERSION": "0.1.0",
            })
        monkeypatch.setattr(container, "load_config", lambda: mock_app_config)
        monkeypatch.setattr(container, "get_project_root", lambda: tmp_path)
        monkeypatch.setattr(container, "preflight", MagicMock())
        monkeypatch.setattr(container, "_sync_build_files", MagicMock())
        compose = MagicMock(return_value=RunResult(returncode=0))
        monkeypatch.setattr(container, "compose_build", compose)
        monkeypatch.setattr(container.hostctl, "build_supervisor",
                            MagicMock(return_value=RunResult(returncode=0)))
        monkeypatch.setattr(container.hostctl, "install_supervisor", MagicMock())
        monkeypatch.setattr(container, "_pull_agent_docker_image_if_missing", MagicMock())
        load = MagicMock(wraps=container.load_versions)
        monkeypatch.setattr(container, "load_versions", load)
        result = runner.invoke(app, ["build", "--no-cache"])
        assert result.exit_code == 0, result.output
        load.assert_called_once_with()
        compose.assert_called_once_with(
            mock_app_config, no_cache=True,
            agent_args={"CLAUDE_CODE_VERSION": "2.1.300"} if recorded else {},
        )


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
            patch("djinn_in_a_box.commands.container.validate_managed_bind_sources"),
            patch("djinn_in_a_box.commands.container.preflight"),
            patch(
                "djinn_in_a_box.commands.container.ensure_network", return_value=True
            ) as mock_network,
            patch("djinn_in_a_box.commands.container.compose_run") as mock_run,
            patch("djinn_in_a_box.commands.container.compose_up_detached") as mock_detached,
            patch(
                "djinn_in_a_box.commands.container.is_container_running", return_value=False
            ) as mock_running,
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
            patch("djinn_in_a_box.commands.container.recover_leftover_sessions") as mock_recover,
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
                "config": mock_config,
                "banner": mock_banner,
                "workflow": mock_workflow,
                "network": mock_network,
                "err_output": err_output,
                "recover": mock_recover,
            }

    @pytest.mark.parametrize("detach", [False, True])
    def test_start_reports_nested_declarations_before_creation(
        self, start_mocks, tmp_path, monkeypatch, detach,
    ):
        config = AppConfig(code_dir=tmp_path, mounts={
            "bind": {"source": str(tmp_path), "target": "//home/dev/workspace/old/../bind/"},
            "volume": {"volume": True, "target": "/home/dev/workspace/data", "backup": "none"},
            "control": {"source": str(tmp_path), "target": "/srv/data"},
        })
        start_mocks["load"].return_value = config
        monkeypatch.chdir(tmp_path)

        def launch(*args, **kwargs):
            output = start_mocks["err_output"].getvalue()
            assert output.count("Skipped") == 2
            assert "mounts.bind -> /home/dev/workspace/bind (inside /home/dev/workspace)" in output
            expected = "mounts.volume -> /home/dev/workspace/data (inside /home/dev/workspace)"
            assert expected in output
            assert "mounts.control" not in output
            assert output.index("Mount") < output.index("Skipped")
            return RunResult(0)

        start_mocks["detached" if detach else "run"].side_effect = launch
        result = runner.invoke(app, ["start", "--here"] + (["--detach"] if detach else []))
        assert result.exit_code == 0, result.output
        start_mocks["detached" if detach else "run"].assert_called_once()

    @pytest.mark.parametrize("detach", [False, True])
    @pytest.mark.parametrize("here", [False, True])
    def test_invalid_declared_target_keeps_named_start_diagnostic(
        self, start_mocks, tmp_path, monkeypatch, detach, here,
    ):
        from djinn_in_a_box.core import docker

        config = AppConfig(code_dir=tmp_path, mounts={
            "bad": {"volume": True, "target": "/proc/x", "backup": "none"},
        })
        start_mocks["load"].return_value = config
        start_mocks["detached"].side_effect = docker.compose_up_detached
        start_mocks["run"].side_effect = docker.compose_run
        monkeypatch.setattr(docker, "get_sops_age_key_mount_args", lambda *a: [])
        monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *a: ([], ()))
        monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
        creation = MagicMock(side_effect=AssertionError("container created"))
        monkeypatch.setattr(docker.subprocess, "run", creation)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app, ["start"] + (["--detach"] if detach else []) + (["--here"] if here else []),
        )
        assert result.exit_code == 1, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        output = start_mocks["err_output"].getvalue()
        assert "Declared mount 'bad'" in output and "/proc" in output
        assert "Traceback" not in result.output
        creation.assert_not_called()

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

    def test_start_recovers_audit_sessions_before_compose(
        self, start_mocks: dict[str, Any]
    ) -> None:
        order: list[str] = []
        start_mocks["recover"].side_effect = lambda *args: order.append("recover")
        start_mocks["run"].side_effect = lambda *args, **kwargs: (
            order.append("compose") or RunResult(returncode=0)
        )

        with pytest.raises(typer.Exit):
            container.start()

        assert order == ["recover", "compose"]
        start_mocks["recover"].assert_called_once_with(Path("/project"), start_mocks["config"])

    def test_start_recovers_before_a_detached_start_too(self, start_mocks: dict[str, Any]) -> None:
        order: list[str] = []
        start_mocks["recover"].side_effect = lambda *args: order.append("recover")
        start_mocks["detached"].side_effect = lambda *args, **kwargs: (
            order.append("compose") or RunResult(returncode=0)
        )

        with pytest.raises(typer.Exit):
            container.start(detach=True)

        assert order == ["recover", "compose"]

    def test_start_continues_when_recovery_fails(
        self, start_mocks: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from djinn_in_a_box.core import assistant, opencode_credentials

        def refused(*args: Any, **kwargs: Any) -> None:
            raise opencode_credentials.Refused("an ancestor belongs to another user")

        # The real helper, with only the merge function failing underneath it.
        monkeypatch.setattr(
            container, "recover_leftover_sessions", assistant.recover_leftover_sessions
        )
        monkeypatch.setattr(assistant.credentials, "recover_audit_sessions", refused)
        monkeypatch.setattr(
            assistant.credentials, "default_audit_root", lambda: str(tmp_path / "none")
        )
        start_mocks["config"].build.network = "default"

        with pytest.raises(typer.Exit) as exc_info:
            container.start()

        assert exc_info.value.exit_code == 0
        start_mocks["run"].assert_called_once()
        assert "recovery failed" in start_mocks["err_output"].getvalue()

    def test_blocked_workflow_stops_before_network_and_compose(
        self, start_mocks: dict[str, Any]
    ) -> None:
        start_mocks["workflow"].return_value = WorkflowPreparationResult(
            False,
            (WorkflowPreparationProblem(
                "blocked", "Workflow blocked in a.toml[error].", "Resolve drift in a.toml[error]."
            ),),
            ("Synchronized source changes into the canonical workflow: 2 changed, 1 removed.",),
        )

        with pytest.raises(typer.Exit) as exc_info:
            container.start()

        assert exc_info.value.exit_code == 1
        assert "Workflow blocked in a.toml[error]." in (
            " ".join(Text.from_ansi(start_mocks["err_output"].getvalue()).plain.split())
        )
        assert "Synchronized source changes into the canonical workflow: 2 changed, 1 removed." in (
            " ".join(Text.from_ansi(start_mocks["err_output"].getvalue()).plain.split())
        )
        assert "Resolve drift in a.toml[error]." in (
            " ".join(Text.from_ansi(start_mocks["err_output"].getvalue()).plain.split())
        )
        start_mocks["network"].assert_not_called()
        start_mocks["run"].assert_not_called()

    def test_start_with_docker_flag(self, start_mocks: dict[str, Any]) -> None:
        with pytest.raises(typer.Exit):
            container.start(docker=True)
        options = start_mocks["run"].call_args[0][1]
        assert options.docker_mode is DockerMode.AGENT
        start_mocks["banner"].assert_called_once_with()

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

    @pytest.mark.parametrize(
        ("port", "stderr", "returncode", "detach", "hint"),
        [
            (49380, "Bind for 127.0.0.1:49380 failed: port is already allocated", 1, True, True),
            (49381, "Bind for 127.0.0.1:49381 failed: port is already allocated", 1, True, True),
            (49381, "Bind for 127.0.0.1:49380 failed: port is already allocated", 1, True, False),
            (49380, "Bind for 0.0.0.0:49380 failed: port is already allocated", 1, True, False),
            (49380, "Bind for 127.0.0.1:49380 failed: permission denied", 1, True, False),
            (49380, "Bind for 127.0.0.1:49380 failed: port is already allocated", 0, True, False),
            (49380, "Bind for 127.0.0.1:49380 failed: port is already allocated", 1, False, False),
            (
                49380,
                "Error response from daemon: driver failed programming external connectivity "
                "on endpoint djinn (abc123): failed to bind host port for "
                "127.0.0.1:49380:172.18.0.2:4096/tcp: address already in use",
                1, True, True,
            ),
            (
                49381,
                "Error response from daemon: driver failed programming external connectivity "
                "on endpoint djinn (abc123): failed to bind host port for "
                "127.0.0.1:49381:172.18.0.2:4096/tcp: address already in use",
                1, True, True,
            ),
            (
                49380,
                "Error response from daemon: driver failed programming external connectivity "
                "on endpoint djinn (abc123): failed to bind host port for "
                "127.0.0.1:49380:172.18.0.2:4096/tcp: permission denied",
                1, True, False,
            ),
        ],
        ids=[
            "default", "configured", "other-port", "other-ip", "other-error",
            "success", "foreground",
            "host-process-default", "host-process-configured", "host-process-permission",
        ],
    )
    def test_start_port_collision_hint(
        self, start_mocks: dict[str, Any], port: int, stderr: str,
        returncode: int, detach: bool, hint: bool,
    ) -> None:
        start_mocks["config"].serve.opencode_host_port = port
        start_mocks["detached" if detach else "run"].return_value = RunResult(
            returncode=returncode, stderr=stderr,
        )

        with (
            patch.object(container, "info", wraps=container.info) as mock_info,
            pytest.raises(typer.Exit) as exc_info,
        ):
            container.start(detach=detach)

        assert exc_info.value.exit_code == returncode
        captured = re.sub(r"\x1b\[[0-9;]*m", "", start_mocks["err_output"].getvalue())
        output = " ".join(captured.split())
        expected = (
            f"Port {port} is in use; choose another with: "
            "djinn config set serve.opencode_host_port <n>"
        )
        assert (expected in output) is hint
        assert ("choose another with:" in output) is hint
        if hint:
            mock_info.assert_any_call(expected)


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

    @pytest.mark.parametrize(
        ("flags", "root", "mode"),
        [
            ([], "/home/dev/.cache/uv", "rw"),
            (["--detach"], "/home/dev/.vscode-server", "ro"),
        ],
        ids=["foreground-rw", "detached-ro"],
    )
    def test_start_cli_refuses_managed_mount_before_creation(
        self, mock_app_config: AppConfig, tmp_path: Path, flags: list[str], root: str, mode: str
    ) -> None:
        target = f"{root}/x"
        with (
            patch("djinn_in_a_box.commands.container.load_config", return_value=mock_app_config),
            patch("djinn_in_a_box.commands.container.preflight"),
            patch("djinn_in_a_box.commands.container.ensure_network", return_value=True),
            patch("djinn_in_a_box.commands.container.is_container_running", return_value=False),
            patch(
                "djinn_in_a_box.commands.container.prepare_config_workflow",
                return_value=WorkflowPreparationResult(True),
            ),
            patch("djinn_in_a_box.commands.container.get_shell_mount_args", return_value=[]),
            patch("djinn_in_a_box.core.docker.get_shell_mount_args", return_value=[]),
            patch("djinn_in_a_box.core.docker.get_sops_age_key_mount_args", return_value=[]),
            patch(
                "djinn_in_a_box.core.docker._zone_overlay_mount_args_and_targets",
                return_value=([], ()),
            ),
            patch("djinn_in_a_box.core.docker.is_background_process_group", return_value=False),
            patch("djinn_in_a_box.core.docker.subprocess.run") as docker_run,
            patch("djinn_in_a_box.core.docker.subprocess.Popen") as docker_popen,
            patch("djinn_in_a_box.core.docker.tempfile.mkstemp") as override,
            patch("djinn_in_a_box.core.console.err_console", Console(width=1000, no_color=True)),
        ):
            result = runner.invoke(app, ["start", *flags, "--mount", f"{tmp_path}:{target}:{mode}"])

        assert result.exit_code == 1, result.output
        assert (
            f"Mount {tmp_path} -> {target} conflicts with Djinn-managed path {root} "
            f"(conflict path: {root})"
        ) in result.output
        docker_run.assert_not_called()
        docker_popen.assert_not_called()
        override.assert_not_called()

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
            patch("djinn_in_a_box.commands.container.volume_exists", return_value=False),
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
            patch("djinn_in_a_box.commands.container.volume_exists", return_value=False),
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
                "djinn_in_a_box.commands.container.get_sync_paths_to_clear_by_category",
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
                "djinn_in_a_box.commands.container.get_sync_paths_to_clear_by_category",
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


UPDATE_OUTPUT = (
    "CLAUDE_CODE_VERSION=2.1.300\nCODEX_VERSION=0.161.0\nOPENCODE_VERSION=1.18.40\n"
)
UPDATE_NPM_ERROR = (
    "npm error code EAI_AGAIN\nnpm error syscall getaddrinfo\n"
    "npm error request to https://registry.npmjs.org/@openai%2fcodex failed, "
    "reason: getaddrinfo EAI_AGAIN registry.npmjs.org\n"
)


class UpdateProcess:
    def __init__(self, result, waiting_error=None):
        self.result = result
        self.returncode = result.returncode
        self.pid = 4194305  # above pid_max: no real process can match
        self.waiting_error = waiting_error
        self.waits = []

    def communicate(self, timeout=None):
        self.waits.append(timeout)
        if timeout is not None and self.waiting_error is not None:
            raise self.waiting_error
        return self.result.stdout, self.result.stderr


@pytest.fixture
def update_project(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts/update-agents.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    (tmp_path / "Dockerfile").write_text(
        "ARG CLAUDE_CODE_VERSION=2.1.288\nARG CODEX_VERSION=0.160.0\nARG OPENCODE_VERSION=1.18.34\n"
    )
    monkeypatch.setattr(container, "get_project_root", lambda: tmp_path)
    return tmp_path


class TestUpdateCommand:
    """Tests for local, all-or-nothing discovery and persistence."""

    def test_update_runs_script(self, update_project, monkeypatch):
        from djinn_in_a_box.core import agent_versions, paths

        agent_versions.save_versions({"CLAUDE_CODE_VERSION": "2.1.1", "CODEX_VERSION": "0.162.0"})
        proc = UpdateProcess(subprocess.CompletedProcess([], 0, UPDATE_OUTPUT, ""))
        spawn = MagicMock(return_value=proc)
        monkeypatch.setattr(container.subprocess, "Popen", spawn)
        config = MagicMock(side_effect=AssertionError("update must not load config"))
        monkeypatch.setattr(container, "load_config", config)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 0, result.output
        spawn.assert_called_once_with(
            [str(update_project / "scripts/update-agents.sh"), "--print"], cwd=update_project,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        assert proc.waits == [120]
        config.assert_not_called()
        assert agent_versions.load_versions() == {
            "CLAUDE_CODE_VERSION": "2.1.300", "CODEX_VERSION": "0.161.0",
            "OPENCODE_VERSION": "1.18.40",
        }
        assert "CLAUDE_CODE_VERSION: 2.1.288 -> 2.1.300" in result.output
        assert "CODEX_VERSION: 0.162.0 -> 0.161.0" in result.output
        assert "OPENCODE_VERSION: 1.18.34 -> 1.18.40" in result.output
        assert str(paths.AGENT_VERSIONS_FILE) in result.output.replace("\n", "")
        assert "djinn build" in result.output

    def test_update_errors_if_script_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(container, "get_project_root", lambda: tmp_path)
        spawn = MagicMock()
        save = MagicMock()
        load = MagicMock()
        monkeypatch.setattr(container.subprocess, "Popen", spawn)
        monkeypatch.setattr(container, "save_versions", save)
        monkeypatch.setattr(container, "load_versions", load)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        assert "Update script not found" in result.output
        spawn.assert_not_called()
        save.assert_not_called()
        load.assert_not_called()

    @pytest.mark.parametrize("existing", [False, True])
    @pytest.mark.parametrize("failure", [
        "npm", "bad-version", "timeout", "malformed", "blank", "incomplete", "duplicate",
        "extra", "unknown", "spawn", "encoding", "wait-error",
    ])
    def test_update_failure_preserves_record(self, update_project, monkeypatch, existing, failure):
        from djinn_in_a_box.core import paths

        record = paths.AGENT_VERSIONS_FILE
        old = b'# previous\nCODEX_VERSION = "0.162.0"\n'
        if existing:
            record.parent.mkdir(parents=True)
            record.write_bytes(old)
        outputs = {
            "bad-version": UPDATE_OUTPUT.replace("0.161.0", "0.161.0-beta"),
            "malformed": UPDATE_OUTPUT + "resolved all agents\n",
            "blank": UPDATE_OUTPUT + "\n",
            "incomplete": "CLAUDE_CODE_VERSION=2.1.300\nCODEX_VERSION=0.161.0\n",
            "duplicate": UPDATE_OUTPUT + "CODEX_VERSION=0.161.0\n",
            "extra": UPDATE_OUTPUT + "DOCKER_VERSION=29.0.0\n",
            "unknown": UPDATE_OUTPUT.replace("CODEX_VERSION", "OTHER_VERSION"),
        }
        waiting_error = {
            "timeout": subprocess.TimeoutExpired(["update-agents.sh", "--print"], 120),
            "encoding": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
            "wait-error": OSError("pipe read failed"),
        }.get(failure)
        proc = UpdateProcess(subprocess.CompletedProcess(
            [], 1 if failure == "npm" else 0,
            "CLAUDE_CODE_VERSION=2.1.300\n" if failure == "npm"
            else outputs.get(failure, UPDATE_OUTPUT),
            UPDATE_NPM_ERROR if failure == "npm" else "",
        ), waiting_error)
        spawn = MagicMock(return_value=proc)
        if failure == "spawn":
            spawn.side_effect = OSError("script is not executable")
        monkeypatch.setattr(container.subprocess, "Popen", spawn)
        monkeypatch.setattr(container.os, "killpg", MagicMock())
        save = MagicMock()
        monkeypatch.setattr(container, "save_versions", save)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit), result.exception
        assert "Error" in result.output
        save.assert_not_called()
        assert "Agent versions saved" not in result.output
        if failure == "unknown":
            assert "Invalid update script output" in result.output
            assert "OTHER_VERSION=0.161.0" in result.output
        if failure == "npm":
            assert "npm error code EAI_AGAIN" in result.output
            assert "getaddrinfo EAI_AGAIN registry.npmjs.org" in result.output
        if existing:
            assert record.read_bytes() == old
        else:
            assert not record.exists()

    @pytest.mark.parametrize("failure", ["record", "default", "read"])
    def test_update_invalid_inputs_abort_before_spawn(self, update_project, monkeypatch, failure):
        from djinn_in_a_box.core import paths

        record = paths.AGENT_VERSIONS_FILE
        record.parent.mkdir(parents=True)
        record.write_text("[" if failure == "record" else 'CODEX_VERSION = "0.160.0"')
        if failure == "default":
            (update_project / "Dockerfile").write_text("ARG CLAUDE_CODE_VERSION=beta\n")
        if failure == "read":
            original = Path.open

            def opened(self, *args, **kwargs):
                if self == record:
                    raise OSError("record permission denied")
                return original(self, *args, **kwargs)

            monkeypatch.setattr(Path, "open", opened)
        old = record.read_bytes() if failure != "read" else None
        spawn = MagicMock()
        save = MagicMock()
        monkeypatch.setattr(container.subprocess, "Popen", spawn)
        monkeypatch.setattr(container, "save_versions", save)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit), result.exception
        assert "Error" in result.output
        expected_file = "Dockerfile" if failure == "default" else "agent-versions.toml"
        assert expected_file in result.output.replace("\n", "")
        spawn.assert_not_called()
        save.assert_not_called()
        if old is not None:
            assert record.read_bytes() == old

    def test_update_write_failure_exits_nonzero(self, update_project, monkeypatch):
        from djinn_in_a_box.core import agent_versions, paths

        agent_versions.save_versions({"CODEX_VERSION": "0.160.0"})
        record = paths.AGENT_VERSIONS_FILE
        old = record.read_bytes()
        proc = UpdateProcess(subprocess.CompletedProcess([], 0, UPDATE_OUTPUT, ""))
        monkeypatch.setattr(container.subprocess, "Popen", MagicMock(return_value=proc))
        monkeypatch.setattr(
            agent_versions.os, "replace", MagicMock(side_effect=OSError("disk full")),
        )
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit), result.exception
        assert "Error" in result.output
        assert "Agent versions saved" not in result.output
        assert "agent-versions.toml" in result.output.replace("\n", "")
        assert record.read_bytes() == old
        assert list(record.parent.iterdir()) == [record]

    def test_update_resolves_effective_results_before_saving(self, update_project, monkeypatch):
        from djinn_in_a_box.core import agent_versions, paths

        agent_versions.save_versions({"CODEX_VERSION": "0.162.0"})
        old = paths.AGENT_VERSIONS_FILE.read_bytes()

        class ChangedDefaults(UpdateProcess):
            def communicate(self, timeout=None):
                (update_project / "Dockerfile").write_text("ARG CLAUDE_CODE_VERSION=beta\n")
                return super().communicate(timeout)

        proc = ChangedDefaults(subprocess.CompletedProcess([], 0, UPDATE_OUTPUT, ""))
        monkeypatch.setattr(container.subprocess, "Popen", MagicMock(return_value=proc))
        save = MagicMock(wraps=container.save_versions)
        monkeypatch.setattr(container, "save_versions", save)
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1
        # Rich wraps long temporary paths; the file name may span a line break.
        assert "Dockerfile" in result.output.replace("\n", "")
        save.assert_not_called()
        assert paths.AGENT_VERSIONS_FILE.read_bytes() == old

    @pytest.mark.parametrize("interrupted", [False, True])
    @pytest.mark.parametrize("vanished", [False, True])
    def test_update_interrupt_and_vanished_group_still_clean_up(
        self, update_project, monkeypatch, interrupted, vanished,
    ):
        import signal

        steps = []
        waiting_error = KeyboardInterrupt() if interrupted else subprocess.TimeoutExpired([], 120)

        class InterruptedProcess(UpdateProcess):
            def communicate(self, timeout=None):
                steps.append("wait" if timeout is not None else "reap")
                return super().communicate(timeout)

        proc = InterruptedProcess(subprocess.CompletedProcess([], -9, "", ""), waiting_error)
        monkeypatch.setattr(container.subprocess, "Popen", MagicMock(return_value=proc))

        def killpg(pid, sig):
            assert pid == proc.pid and sig == signal.SIGKILL
            steps.append("killpg")
            if vanished:
                raise ProcessLookupError

        monkeypatch.setattr(container.os, "killpg", killpg)
        save = MagicMock()
        monkeypatch.setattr(container, "save_versions", save)
        if interrupted:
            with pytest.raises(KeyboardInterrupt):
                container.update()
        else:
            with pytest.raises(typer.Exit) as exc:
                container.update()
            assert exc.value.exit_code == 1
        assert steps == ["wait", "killpg", "reap"]
        assert proc.waits == [120, None]
        save.assert_not_called()

    def test_update_timeout_kills_the_whole_group(self, update_project, monkeypatch):
        import contextlib
        import os
        import signal
        import threading
        import time

        from djinn_in_a_box.core import paths

        pidfile = update_project.parent / "grandchild-pid"
        script = update_project / "scripts/update-agents.sh"
        script.write_text(
            "#!/bin/bash\n"
            "echo 'npm error code EAI_AGAIN' >&2\n"
            f"sleep 30 &\necho $! > '{pidfile}'\nwait\n"
        )
        script.chmod(0o755)
        record = paths.AGENT_VERSIONS_FILE
        record.parent.mkdir(parents=True)
        record.write_text('CODEX_VERSION = "0.160.0"\n')
        old = record.read_bytes()
        monkeypatch.setattr(container, "UPDATE_TIMEOUT_SECONDS", 0.2)
        shown = []
        monkeypatch.setattr(container, "print_captured", shown.append)
        processes = []
        original_popen = subprocess.Popen

        def popen(*args, **kwargs):
            proc = original_popen(*args, **kwargs)
            processes.append(proc)
            return proc

        monkeypatch.setattr(container.subprocess, "Popen", popen)
        outcomes = []

        def invoke():
            try:
                container.update()
            except BaseException as exc:
                outcomes.append(exc)

        thread = threading.Thread(target=invoke, daemon=True)
        started = time.monotonic()
        thread.start()
        try:
            thread.join(timeout=3)
            assert not thread.is_alive(), "update did not finish within its outer deadline"
            assert time.monotonic() - started < 3
            assert len(outcomes) == 1 and isinstance(outcomes[0], typer.Exit)
            assert outcomes[0].exit_code == 1
            assert any("npm error code EAI_AGAIN" in text for text in shown)
            assert processes[0].poll() is not None
            assert processes[0].stdout.closed and processes[0].stderr.closed
            pid = int(pidfile.read_text())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                stat = Path(f"/proc/{pid}/stat")
                if not stat.exists() or stat.read_text().split(")", 1)[1].split()[0] == "Z":
                    break
                time.sleep(0.02)
            else:
                pytest.fail("npm grandchild remains running after timeout")
            assert record.read_bytes() == old
        finally:
            # Only the session update() created for this test's own script: each
            # recorded Popen leads its group. Nothing else is ever signalled here.
            for proc in processes:
                with contextlib.suppress(ProcessLookupError):
                    if proc.poll() is None and os.getpgid(proc.pid) != proc.pid:
                        continue
                    os.killpg(proc.pid, signal.SIGKILL)
            thread.join(timeout=3)
            assert not thread.is_alive(), "cleanup failed to release captured pipes"


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

        def docker_ps(cmd: list[str], *, timeout: float | None = None) -> list[str]:
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
            "cache": ["djinn-agent-docker", "djinn-uv-cache", "djinn-tools-cache"],
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


@pytest.fixture
def status_serve_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Isolate the advisory serve block from Docker and the other status sections."""
    from djinn_in_a_box.core import serve as serve_core

    monkeypatch.setattr(container, "load_config", lambda: AppConfig(code_dir=tmp_path))
    monkeypatch.setattr(container.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, stdout=""))
    monkeypatch.setattr(container, "is_container_running", lambda _: False)
    monkeypatch.setattr(container, "get_existing_volumes_by_category", lambda *a, **k: [])
    monkeypatch.setattr(container, "get_existing_sync_paths_by_category", lambda *a, **k: [])
    captured = MagicMock(return_value=RunResult(
        1, stderr="Error response from daemon: No such container: djinn"))
    monkeypatch.setattr(serve_core, "_run_captured", captured)
    return serve_core, captured


def test_status_serve_block_without_container(status_serve_host) -> None:
    _, captured = status_serve_host
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Serve:" in result.output and "Container not running" in result.output
    assert all(call.args[0][1] != "exec" for call in captured.call_args_list)


def test_status_serve_block_without_config(status_serve_host, monkeypatch, tmp_path) -> None:
    serve_core, _ = status_serve_host

    def missing():
        raise ConfigNotFoundError(tmp_path / "missing.toml")

    monkeypatch.setattr(container, "load_config", missing)
    real = serve_core.collect_snapshot
    spy = MagicMock(wraps=real)
    monkeypatch.setattr(serve_core, "collect_snapshot", spy)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Container not running" in result.output
    spy.assert_called_once_with(None, quick=True)


@pytest.mark.parametrize("stdout", ["", "{broken", "[]", "{}"])
def test_status_serve_block_unparsable(status_serve_host, stdout) -> None:
    _, captured = status_serve_host
    captured.return_value = RunResult(0, stdout)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert result.output.count("Unknown (Docker error)") == 1
    assert all(call.args[0][1] != "exec" for call in captured.call_args_list)


def test_status_serve_block_failure(status_serve_host, monkeypatch) -> None:
    serve_core, _ = status_serve_host
    monkeypatch.setattr(serve_core, "collect_snapshot", MagicMock(
        side_effect=serve_core.ServeError("invalid helper reply")))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert result.output.count("Unknown (Docker error)") == 1


@pytest.mark.parametrize("state,text", [
    ("not-detached", "Not available (container not started with --detach)"),
    ("busy", "busy (start/stop in progress)"),
    ("degraded", "Degraded: not registered"),
    ("exited", "Exited: [/x]\\x1b[31m"),
    ("stopped", "Not running"),
    ("running", "Running - http://127.0.0.1:49380"),
    ("unpublished", "port not published (recreate once)"),
])
def test_status_serve_block_states(status_serve_host, monkeypatch, state, text) -> None:
    serve_core, _ = status_serve_host
    info = serve_core.ContainerInfo(
        serve_core.ContainerState.RUNNING, "a" * 64,
        () if state == "not-detached" else ("DJINN_DETACHED=true",))
    servers = {
        name: {"state": state if state in serve_core.STATES else "running",
               "reason": "not registered", "last_line": "[/x]\x1b[31m",
               "foreign_listener": False} for name in serve_core.NAMES
    }
    snapshot = serve_core.Snapshot(info, 49380, state != "unpublished", state == "busy", servers)
    monkeypatch.setattr(serve_core, "collect_snapshot", lambda *a, **k: snapshot)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert text in result.output
    assert "\x1b" not in result.output


def test_status_serve_funnel_foreground_warning(status_serve_host, monkeypatch) -> None:
    serve_core, _ = status_serve_host
    info = serve_core.ContainerInfo(serve_core.ContainerState.RUNNING, "a" * 64,
                                   ("DJINN_DETACHED=true",))
    servers = {name: {"state": "running", "foreign_listener": False} for name in serve_core.NAMES}
    snapshot = serve_core.Snapshot(info, 49380, True, False, servers, funnel=True)
    monkeypatch.setattr(serve_core, "collect_snapshot", lambda *a, **k: snapshot)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert serve_core.FUNNEL_WARNING in result.output


@pytest.mark.parametrize("site", ["last-line", "degraded-reason"])
@pytest.mark.parametrize(
    "payload", ["[/x]", "[link=https://example.com]literal[/link]", "\x1b[31mred"],
)
def test_status_serve_untrusted_text_is_printed_literally(
    status_serve_host, monkeypatch, site, payload,
) -> None:
    from djinn_in_a_box.core.console import escape_nonprintable

    serve_core, _ = status_serve_host
    info = serve_core.ContainerInfo(serve_core.ContainerState.RUNNING, "a" * 64,
                                   ("DJINN_DETACHED=true",))
    servers = {name: {"state": "exited" if site == "last-line" else "degraded",
                      "reason": payload, "last_line": payload, "foreign_listener": False}
               for name in serve_core.NAMES}
    snapshot = serve_core.Snapshot(info, 49380, True, False, servers)
    monkeypatch.setattr(serve_core, "collect_snapshot", lambda *a, **k: snapshot)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert escape_nonprintable(payload) in result.output
    assert "\x1b" not in result.output


@pytest.fixture
def credential_cleanup(mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    from djinn_in_a_box.core import opencode_credentials as oc

    config = mock_app_config
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    monkeypatch.setattr(container, "load_config", lambda: config)
    monkeypatch.setattr(container, "compose_down", lambda: RunResult(0))
    monkeypatch.setattr(container, "network_exists", lambda _: False)
    carrier = config.config_root / "opencode"
    carrier.mkdir(parents=True, mode=0o700)

    def entry(identity):
        return {
            "id": identity,
            "integrationID": "provider",
            "label": os.urandom(8).hex(),
            "active": True,
            "value": {"type": "key", "key": os.urandom(24).hex()},
        }

    for name, document in (
        ("credentials.json", [entry("file")]),
        ("credentials.sync-conflict-20261020-120000-ABCDEFG.json", [entry("copy")]),
        ("credentials.removed.json", ["older"]),
        ("credentials.removed.sync-conflict-20261020-120000-ABCDEFG.json", ["copy-removed"]),
    ):
        (carrier / name).write_text(json.dumps(document))
        (carrier / name).chmod(0o600)
    (carrier / "settings").mkdir()
    (carrier / "settings" / "config").touch()
    for name in ("claude", "codex", "gh", "age"):
        other = config.config_root / name
        other.mkdir()
        (other / "keep-until-cleaned").touch()
    exported = json.dumps([entry("database")]).encode()
    monkeypatch.setattr(container, "export_opencode_data_credentials", lambda: exported)
    return config, carrier, exported, oc


@pytest.mark.parametrize("command", ["volumes", "all"])
def test_global_logout_refuses_running_audits_before_destructive_work(
    credential_cleanup, monkeypatch, command
):
    config, carrier, _, oc = credential_cleanup
    spies = {}
    for module, name in (
        (oc, "merge_back"),
        (oc, "tombstone_all"),
        (container, "clear_sync_path"),
        (container, "export_opencode_data_credentials"),
        (container, "compose_down"),
        (container, "delete_volumes"),
        (container, "delete_network"),
        (container.hostctl, "delete_state_locked"),
    ):
        spy = (
            MagicMock(return_value={})
            if name == "delete_volumes"
            else MagicMock(wraps=getattr(module, name))
        )
        monkeypatch.setattr(module, name, spy)
        spies[name] = spy

    def snapshot():
        return {
            str(p.relative_to(config.config_root)): (
                p.lstat().st_mode,
                p.read_bytes() if p.is_file() else None,
            )
            for p in config.config_root.rglob("*")
        }

    with oc.create_audit_session() as first, oc.create_audit_session() as second:
        os.utime(first.path, (1_700_000_000, 1_700_000_000))
        os.utime(second.path, (1_710_000_000, 1_710_000_000))
        before = snapshot()
        carrier_before = {p.name: p.read_bytes() for p in carrier.iterdir() if p.is_file()}
        args = ["clean", command, "--force"]
        if command == "volumes":
            args.append("--credentials")
        result = runner.invoke(app, args)
        assert result.exit_code == 1, result.output
        assert "OpenCode credentials refused" in result.output
        for session, epoch in ((first, 1_700_000_000), (second, 1_710_000_000)):
            started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
            assert f"{session.id} (running since {started})" in result.output
            assert Path(session.path).is_dir()
        assert "finish that audit, then run the command again" in result.output
        assert "djinn clean volumes --credentials --opencode-id <credential-id>" in result.output
        for spy in spies.values():
            spy.assert_not_called()
        assert snapshot() == before
        assert {p.name: p.read_bytes() for p in carrier.iterdir() if p.is_file()} == carrier_before


@pytest.mark.parametrize("command", ["volumes", "all"])
def test_global_logout_rechecks_sessions_after_recovery(credential_cleanup, monkeypatch, command):
    _, _, _, oc = credential_cleanup
    sessions = []

    def recover(*_):
        sessions.append(oc.create_audit_session())

    monkeypatch.setattr(container, "recover_leftover_sessions", recover)
    export = MagicMock(side_effect=AssertionError("export must wait for the audit to finish"))
    monkeypatch.setattr(container, "export_opencode_data_credentials", export)
    try:
        args = ["clean", command, "--force"]
        if command == "volumes":
            args.append("--credentials")
        result = runner.invoke(app, args)
        assert result.exit_code == 1, result.output
        assert len(sessions) == 1
        assert sessions[0].id in result.output
        assert "--opencode-id" in result.output
        export.assert_not_called()
    finally:
        for session in sessions:
            session.close()


def test_selected_logout_during_audit_survives_its_final_merge(credential_cleanup):
    _, carrier, exported, oc = credential_cleanup
    with oc.create_audit_session() as session:
        result = runner.invoke(
            app, ["clean", "volumes", "--credentials", "--opencode-id", "X", "--force"]
        )
        assert result.exit_code == 0, result.output
        assert "X" in json.loads((carrier / "credentials.removed.json").read_bytes())
        final = Path(session.io_path) / "final.json"
        final.write_bytes(exported.replace(b'"database"', b'"X"'))
        final.chmod(0o600)
        report = oc.recover_audit_sessions(
            None,
            carrier,
            lambda _: pytest.fail("the final export should be used"),
            lambda _: False,
            own_session=session,
        )
        assert report.recovered == [session.id]
    assert "X" not in {
        item["id"] for item in json.loads((carrier / "credentials.json").read_bytes())
    }
    assert "X" in json.loads((carrier / "credentials.removed.json").read_bytes())


def test_global_logout_recovers_finished_audit_then_logs_out(credential_cleanup):
    _, carrier, exported, oc = credential_cleanup
    with oc.create_audit_session() as session:
        final = Path(session.io_path) / "final.json"
        final.write_bytes(exported.replace(b'"database"', b'"finished"'))
        final.chmod(0o600)
    result = runner.invoke(app, ["clean", "volumes", "--credentials", "--force"])
    assert result.exit_code == 0, result.output
    assert not Path(session.path).exists()
    assert not (carrier / "credentials.json").exists()
    assert "finished" in json.loads((carrier / "credentials.removed.json").read_bytes())


@pytest.mark.parametrize("existing_tombstones", [False, True])
def test_selected_logout_is_refused_while_restore_runs(
    credential_cleanup, monkeypatch, tmp_path, existing_tombstones
):
    _, carrier, _, oc = credential_cleanup
    hostctl = container.hostctl
    removed = carrier / "credentials.removed.json"
    if not existing_tombstones:
        removed.unlink()
    before = removed.read_bytes() if removed.exists() else None
    tombstones = MagicMock(wraps=oc.tombstone_ids)
    monkeypatch.setattr(oc, "tombstone_ids", tombstones)
    lock = hostctl.state_root() / "control.lock"
    ready = tmp_path / "restore-holds-the-lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(sys.argv[2], 'w').close()\n"
            "time.sleep(60)\n",
            str(lock),
            str(ready),
        ],
        stdin=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert holder.poll() is None and time.monotonic() < deadline
            time.sleep(0.02)
        clock = iter(range(0, 10_000, 10))
        monkeypatch.setattr(hostctl.time, "monotonic", lambda: float(next(clock)))
        monkeypatch.setattr(hostctl.time, "sleep", lambda _: None)
        result = runner.invoke(
            app, ["clean", "volumes", "--credentials", "--opencode-id", "X", "--force"]
        )
    finally:
        holder.kill()
        holder.wait()
    assert result.exit_code == 1, result.output
    assert "host control is busy" in result.output
    assert "recorded as logged out" not in result.output
    assert (removed.read_bytes() if removed.exists() else None) == before
    tombstones.assert_not_called()


@pytest.mark.parametrize("exception", [container.hostctl.HostctlError, OSError])
def test_clean_volumes_reports_control_guard_acquisition_failure(monkeypatch, exception):
    @contextmanager
    def refused():
        raise exception("control lock unavailable")
        yield

    monkeypatch.setattr(container.hostctl, "control_guard", refused)
    result = runner.invoke(app, ["clean", "volumes"])
    assert result.exit_code == 1
    assert "control lock unavailable" in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("exception", [container.hostctl.HostctlError, OSError])
def test_clean_all_reports_busy_control_guard(monkeypatch, exception):
    @contextmanager
    def refused():
        raise exception("host control is busy")
        yield

    monkeypatch.setattr(container.hostctl, "control_guard", refused)
    result = runner.invoke(app, ["clean", "all", "--force"])
    assert result.exit_code == 1
    assert "host control is busy" in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("exception", [container.hostctl.HostctlError, OSError])
def test_clean_all_preserves_body_exceptions(monkeypatch, exception):
    failure = exception("command body failed")

    def failed():
        assert container.hostctl._held.active
        raise failure

    monkeypatch.setattr(container, "_load_optional_config", failed)
    result = runner.invoke(app, ["clean", "all", "--force"])
    assert result.exception is failure
    assert "command body failed" not in result.output
    assert not container.hostctl._held.active


def test_clean_all_confirms_before_acquiring_control_guard(monkeypatch):
    def unexpected_guard():
        pytest.fail("control guard acquired before confirmation")

    monkeypatch.setattr(container.hostctl, "control_guard", unexpected_guard)
    result = runner.invoke(app, ["clean", "all"], input="n\n")
    assert result.exit_code == 0
    assert "Aborted." in result.output


@pytest.mark.parametrize("exception", [container.hostctl.HostctlError, OSError])
def test_clean_volumes_preserves_body_exceptions(monkeypatch, exception):
    failure = exception("command body failed")

    def failed():
        assert container.hostctl._held.active
        raise failure

    monkeypatch.setattr(container, "_load_optional_config", failed)
    result = runner.invoke(app, ["clean", "volumes"])
    assert result.exception is failure
    assert "command body failed" not in result.output
    assert not container.hostctl._held.active


def test_credentials_logout_orders_recovery_export_and_single_lease(
    credential_cleanup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, carrier, exported, oc = credential_cleanup
    calls = []
    lock = oc.carrier_lock
    original_recovery = container.recover_leftover_sessions
    merge_back, tombstone, clear = oc.merge_back, oc.tombstone_all, container.clear_sync_path

    def recover(*_):
        calls.append("recover")
        session = oc.create_audit_session()
        oc.write_seed(session, b"[]")
        final = Path(session.io_path) / "final.json"
        final.write_bytes(exported.replace(b'"database"', b'"recovered"'))
        final.chmod(0o600)
        session.close()
        original_recovery(container.get_project_root(), config)
        assert not (Path(session.root) / session.id).exists()

    def export():
        calls.append("export")
        return exported

    @contextmanager
    def locked(path):
        calls.append("lock")
        with lock(path) as lease:
            yield lease
        calls.append("unlock")

    def merge(path, data, lease):
        assert lease.held
        calls.append("merge")
        return merge_back(path, data, lease)

    def tombstones(path, ids, lease):
        assert lease.held
        calls.append("tombstones")
        return tombstone(path, ids, lease)

    def clearing(path, **kwargs):
        if path.name == "opencode":
            assert calls[-1] == "tombstones"
            calls.append("clear")
        else:
            assert "unlock" in calls
        return clear(path, **kwargs)

    # Recovery owns its own leases; instrument only the cleanup lease after it returns.
    def recovering(*args):
        recover(*args)
        monkeypatch.setattr(oc, "carrier_lock", locked)
        monkeypatch.setattr(oc, "merge_back", merge)

    monkeypatch.setattr(container, "recover_leftover_sessions", recovering)
    monkeypatch.setattr(container, "export_opencode_data_credentials", export)
    monkeypatch.setattr(oc, "tombstone_all", tombstones)
    monkeypatch.setattr(container, "clear_sync_path", clearing)
    result = runner.invoke(app, ["clean", "volumes", "--credentials", "--force"])
    assert result.exit_code == 0, result.output
    assert calls == ["recover", "export", "lock", "merge", "tombstones", "clear", "unlock"]
    assert {p.name for p in carrier.iterdir()} == {"credentials.removed.json"}
    assert set(json.loads((carrier / "credentials.removed.json").read_text())) == {
        "file",
        "copy",
        "database",
        "recovered",
        "older",
        "copy-removed",
    }
    assert all(
        not list((config.config_root / name).iterdir()) for name in ("claude", "codex", "gh", "age")
    )


@pytest.mark.parametrize("filename", ["credentials.json", "credentials.removed.json"])
@pytest.mark.parametrize("kind", ["invalid", "symlink", "directory"])
@pytest.mark.parametrize("loose", [False, True])
@pytest.mark.parametrize("command", ["volumes", "all"])
def test_invalid_credentials_refuse_before_any_changes(
    credential_cleanup,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    kind: str,
    loose: bool,
    command: str,
) -> None:
    import hashlib

    config, carrier, _, _ = credential_cleanup
    target = carrier / filename
    if kind == "invalid":
        target.write_text("{}")
    else:
        target.unlink()
        if kind == "symlink":
            target.symlink_to(carrier / "missing")
        else:
            target.mkdir()
    if loose:
        for path in carrier.iterdir():
            if path.is_file() and not path.is_symlink():
                path.chmod(0o644)

    def snapshot():
        return {
            str(p): (p.stat().st_mode, hashlib.sha256(p.read_bytes()).digest())
            for p in config.config_root.rglob("*")
            if p.is_file()
        }

    before = snapshot()

    def forbidden(*args, **kwargs):
        pytest.fail("validation must precede recovery, export and deletion")

    for name in (
        "recover_leftover_sessions",
        "export_opencode_data_credentials",
        "delete_volumes",
        "compose_down",
    ):
        monkeypatch.setattr(container, name, forbidden)
    args = ["clean", command, "--force"] + (["--credentials"] if command == "volumes" else [])
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "OpenCode credentials refused" in result.output
    assert snapshot() == before


def test_credentials_export_failure_warns_and_clears_rest(credential_cleanup, monkeypatch):
    config, carrier, _, _ = credential_cleanup

    def failed():
        raise container.CredentialExportError("image unavailable")

    monkeypatch.setattr(container, "export_opencode_data_credentials", failed)
    result = runner.invoke(app, ["clean", "volumes", "--credentials", "--force"])
    assert result.exit_code == 0, result.output
    assert "image unavailable" in result.output
    assert "logins only in the database are not covered" in result.output
    assert {p.name for p in carrier.iterdir()} == {"credentials.removed.json"}
    assert set(json.loads((carrier / "credentials.removed.json").read_text())) == {
        "file",
        "copy",
        "older",
        "copy-removed",
    }
    assert not list((config.config_root / "claude").iterdir())


@pytest.mark.parametrize("ids", [["file"], ["file", "copy"], ["unknown"]])
def test_selected_ids_only_add_tombstones(credential_cleanup, monkeypatch, ids):
    import hashlib

    config, carrier, _, _ = credential_cleanup

    def snapshot():
        return {
            str(p): hashlib.sha256(p.read_bytes()).digest()
            for p in config.config_root.rglob("*")
            if p.is_file() and p.name != "credentials.removed.json"
        }

    before = snapshot()

    def forbidden(*args, **kwargs):
        pytest.fail("selective logout must only record tombstones")

    for name in (
        "clear_sync_path",
        "export_opencode_data_credentials",
        "recover_leftover_sessions",
        "delete_volume",
        "delete_volumes",
    ):
        monkeypatch.setattr(container, name, forbidden)
    args = ["clean", "volumes", "--credentials", "--data", "--force"]
    for identity in ids:
        args.extend(["--opencode-id", identity])
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert snapshot() == before
    assert set(json.loads((carrier / "credentials.removed.json").read_text())) == {
        *ids,
        "older",
        "copy-removed",
    }
    assert ("Unknown OpenCode credential ID unknown" in result.output) == (ids == ["unknown"])


def _plain(output: str) -> str:
    """CLI text without ANSI codes, panel borders or wrapping; CI forces a colored terminal."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


@pytest.mark.parametrize(
    "args,code",
    [
        (["--opencode-id", "file"], 2),
        (["--credentials", "--opencode-id", "file", "--opencode-id", "bad id"], 1),
        (["--credentials", "--opencode-id", ""], 1),
        (["--credentials", "--opencode-id", "x" * 201], 1),
        (["--credentials", "--opencode-id", "bad\nline"], 1),
    ],
)
def test_selected_ids_require_credentials_and_valid_charset(
    credential_cleanup, monkeypatch, args, code
):
    _, carrier, _, oc = credential_cleanup
    if code == 1:

        def forbidden(*args, **kwargs):
            pytest.fail("invalid IDs must be rejected before locking or changing the carrier")

        monkeypatch.setattr(oc, "carrier_lock", forbidden)
    before = (carrier / "credentials.removed.json").read_text()
    result = runner.invoke(app, ["clean", "volumes", "--force", *args])
    assert result.exit_code == code
    assert (carrier / "credentials.removed.json").read_text() == before
    assert (
        "requires --credentials" in _plain(result.output)
        if code == 2
        else "id is not valid" in _plain(result.output)
    )


def test_selected_ids_confirmation_and_help(credential_cleanup, monkeypatch):
    import typer.rich_utils

    monkeypatch.setattr(typer.rich_utils, "MAX_WIDTH", 1000)
    _, carrier, _, _ = credential_cleanup
    result = runner.invoke(
        app, ["clean", "volumes", "--credentials", "--opencode-id", "file"], input="n\n"
    )
    assert result.exit_code == 1
    assert "every host" in result.output
    assert "file" not in json.loads((carrier / "credentials.removed.json").read_text())
    help_text = _plain(runner.invoke(app, ["clean", "volumes", "--help"]).output)
    for text in ("opencode auth list --format json", "jq -r", "integrationID", "credentials.json"):
        assert text in help_text


def test_logout_tombstones_are_applied_by_real_dev_sync(credential_cleanup, tmp_path, monkeypatch):
    _, carrier, exported, oc = credential_cleanup
    result = runner.invoke(app, ["clean", "volumes", "--credentials", "--force"])
    assert result.exit_code == 0, result.output
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = Path(__file__).parents[1] / "fake_opencode.py"
    shutil.copyfile(fake, binary / "opencode")
    (binary / "opencode").chmod(0o700)
    database = tmp_path / "opencode.db"
    database.write_text(json.dumps({"entries": json.loads(exported), "selection": {}}))
    env = {
        key: val
        for key, val in os.environ.items()
        if not key.startswith(("OPENCODE", "XDG_", "FAKE_"))
    }
    env.update(
        PATH=f"{binary}:{os.environ['PATH']}",
        FAKE_OPENCODE_STATE=str(database),
        TMPDIR=str(tmp_path),
    )
    sync = subprocess.run(
        [
            sys.executable,
            str(Path(oc.__file__)),
            "sync",
            "--carrier",
            str(carrier),
            "--database",
            str(database),
            "--host-state",
            str(tmp_path / "host.json"),
            "--conflict-backup",
            str(tmp_path / "conflicts"),
            "--mode",
            "start",
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    assert sync.returncode == 0
    assert len(json.loads(database.read_text())["entries"]) == 0


@pytest.mark.parametrize("selection", ["data", "name"])
@pytest.mark.parametrize("failure", [None, "export", "merge"])
@pytest.mark.parametrize("discard", [False, True])
def test_opencode_volume_protection(credential_cleanup, monkeypatch, selection, failure, discard):
    _, carrier, exported, oc = credential_cleanup
    calls = []

    def export():
        calls.append("export")
        if failure == "export":
            raise container.CredentialExportError("image unavailable")
        return exported

    real_merge = oc.merge_back

    def merge(*args):
        calls.append("merge")
        if failure == "merge":
            raise oc.NotUnderstood("publication failed")
        return real_merge(*args)

    def delete(names):
        calls.append("delete")
        if failure is None:
            assert "database" in {
                e["id"] for e in json.loads((carrier / "credentials.json").read_text())
            }
        return dict.fromkeys(names, True)

    monkeypatch.setattr(container, "export_opencode_data_credentials", export)
    monkeypatch.setattr(oc, "merge_back", merge)
    monkeypatch.setattr(container, "volume_exists", lambda _: True)
    monkeypatch.setattr(
        container,
        "get_existing_volumes_by_category",
        lambda *_: ["djinn-opencode-data", "djinn-packages"],
    )
    monkeypatch.setattr(container, "delete_volumes", delete)
    monkeypatch.setattr(container, "delete_volume", lambda name: delete([name])[name])
    args = ["clean", "volumes", "--data" if selection == "data" else "djinn-opencode-data"]
    if discard:
        args.append("--discard-unexported-credentials")
    result = runner.invoke(app, args)
    refused = failure is not None and not discard
    assert result.exit_code == (1 if refused else 0), result.output
    assert ("delete" in calls) != refused
    assert calls[:2] == (
        ["export", "merge"] if failure != "export" else ["export"] + ([] if refused else ["delete"])
    )
    if refused:
        assert "--discard-unexported-credentials" in result.output
        assert (
            "image unavailable" if failure == "export" else "publication failed"
        ) in result.output
    elif failure:
        assert "unexported logins are lost" in result.output


def test_other_volume_deletion_does_not_export_credentials(monkeypatch):
    monkeypatch.setattr(container, "volume_exists", lambda _: True)
    monkeypatch.setattr(container, "delete_volume", lambda _: True)

    def forbidden():
        pytest.fail("other volumes must not export OpenCode credentials")

    monkeypatch.setattr(container, "export_opencode_data_credentials", forbidden)
    assert runner.invoke(app, ["clean", "volumes", "djinn-other"]).exit_code == 0


@pytest.mark.parametrize("failed", [False, True])
def test_clean_all_exports_before_delete_and_keeps_tombstones(
    credential_cleanup, monkeypatch, failed
):
    _, carrier, exported, _ = credential_cleanup
    calls = []

    def export():
        calls.append("export")
        if failed:
            raise container.CredentialExportError("image unavailable")
        return exported

    def delete(names):
        calls.append("delete")
        ids = set(json.loads((carrier / "credentials.removed.json").read_text()))
        assert {"file", "copy", "older", "copy-removed"} <= ids
        assert ("database" in ids) != failed
        return dict.fromkeys(names, True)

    monkeypatch.setattr(container, "export_opencode_data_credentials", export)
    monkeypatch.setattr(container, "delete_volumes", delete)
    result = runner.invoke(app, ["clean", "all", "--force"])
    assert result.exit_code == 0, result.output
    assert calls == ["export", "delete"]
    assert {p.name for p in carrier.iterdir()} == {"credentials.removed.json"}
    assert ("logins only in the database are not covered" in result.output) == failed


def test_credentials_cleanup_names_each_running_audit_before_recovery(credential_cleanup):
    _, _, _, oc = credential_cleanup
    first = oc.create_audit_session()
    second = oc.create_audit_session()
    try:
        result = runner.invoke(app, ["clean", "volumes", "--credentials", "--force"])
        assert result.exit_code == 1, result.output
        for session in (first, second):
            assert f"{session.id} (running since" in result.output
        assert "was kept: in use" not in result.output
        assert "OpenCode credentials refused" in result.output
    finally:
        first.close()
        second.close()


def test_credentials_revalidate_before_clearing(credential_cleanup, monkeypatch):
    config, carrier, _, oc = credential_cleanup
    original = oc.tombstone_all

    def changed(*args):
        result = original(*args)
        (carrier / "credentials.json").write_text("{}")
        return result

    monkeypatch.setattr(oc, "tombstone_all", changed)
    result = runner.invoke(app, ["clean", "volumes", "--credentials", "--force"])
    assert result.exit_code == 1
    assert "credentials are not an array" in result.output
    assert (carrier / "credentials.json").read_text() == "{}"
    assert (config.config_root / "claude/keep-until-cleaned").exists()


@pytest.mark.parametrize("kind", ["foreign-owner", "oversized", "directory"])
def test_credentials_preflight_refuses_without_recovery(credential_cleanup, monkeypatch, kind):
    _, carrier, _, oc = credential_cleanup
    target = carrier / "credentials.json"
    if kind == "foreign-owner":
        original = os.fstat

        def foreign(descriptor):
            result = original(descriptor)
            if os.readlink(f"/proc/self/fd/{descriptor}") == str(target):
                fields = list(result)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return result

        monkeypatch.setattr(os, "fstat", foreign)
        reason = "entry belongs to another user"
    elif kind == "oversized":
        target.write_bytes(b"[]" + b" " * (oc.FILE_LIMIT - 1))
        reason = "size limit"
    else:
        target.unlink()
        target.mkdir()
        reason = "entry is not a regular file"

    def forbidden(*args, **kwargs):
        pytest.fail("preflight must refuse before recovery or exporting")

    monkeypatch.setattr(container, "recover_leftover_sessions", forbidden)
    monkeypatch.setattr(container, "export_opencode_data_credentials", forbidden)
    result = runner.invoke(app, ["clean", "volumes", "--credentials", "--force"])
    assert result.exit_code == 1
    assert reason in result.output
    assert target.exists()
    assert target.is_dir() if kind == "directory" else stat.S_ISREG(target.stat().st_mode)
