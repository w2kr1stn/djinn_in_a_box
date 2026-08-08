"""Tests for container lifecycle commands."""

import io
import re
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
)
from djinn_in_a_box.core.exceptions import (
    ConfigNotFoundError,
    ConfigValidationError,
    RuntimeMountSpecificationError,
)
from djinn_in_a_box.core.theme import DJINN_THEME


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
            patch("djinn_in_a_box.commands.container.cleanup_docker_proxy") as mock_cleanup,
            patch("djinn_in_a_box.commands.container.get_shell_mount_args", return_value=[]),
            patch("djinn_in_a_box.commands.container.get_audio_mount_args", return_value=[]),
            patch("djinn_in_a_box.commands.container.get_dbus_mount_args", return_value=[]),
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
            yield {
                "load": mock_load,
                "run": mock_run,
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
        start_mocks["cleanup"].assert_called_once_with(DockerMode.PROXY, start_mocks["config"])

    def test_start_with_firewall_flag(self, start_mocks: dict[str, Any]) -> None:
        with pytest.raises(typer.Exit):
            container.start(firewall=True)
        options = start_mocks["run"].call_args[0][1]
        assert options.firewall_enabled is True

    def test_start_passes_each_precomputed_runtime_mount_list(
        self, start_mocks: dict[str, Any]
    ) -> None:
        shell_args = ["-v", "/host/.zshrc:/home/dev/.zshrc.local:ro"]
        audio_args = ["-v", "/host/pulse:/run/user/1000/pulse/native"]
        dbus_args = ["-v", "/host/bus:/run/user/1000/bus:ro"]
        with (
            patch(
                "djinn_in_a_box.commands.container.get_shell_mount_args",
                return_value=shell_args,
            ),
            patch(
                "djinn_in_a_box.commands.container.get_audio_mount_args",
                return_value=audio_args,
            ),
            patch(
                "djinn_in_a_box.commands.container.get_dbus_mount_args",
                return_value=dbus_args,
            ),
            pytest.raises(typer.Exit),
        ):
            container.start()

        kwargs = start_mocks["run"].call_args.kwargs
        assert kwargs["shell_mount_args"] is shell_args
        assert kwargs["audio_mount_args"] is audio_args
        assert kwargs["dbus_mount_args"] is dbus_args

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
        start_mocks["cleanup"].assert_called_once_with(DockerMode.NONE, start_mocks["config"])

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

    def test_start_prints_compose_stderr(
        self, start_mocks: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        start_mocks["run"].return_value = RunResult(returncode=127, stderr="docker missing\n")

        with pytest.raises(typer.Exit) as exc_info:
            container.start()

        assert exc_info.value.exit_code == 127
        assert "docker missing" in capsys.readouterr().err

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
        start_mocks["cleanup"].assert_called_once_with(DockerMode.DIRECT, start_mocks["config"])

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
            assert mock_vol.call_count == 2
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
        (configured_root / "gemini").mkdir(parents=True)
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
                {"credentials": ["claude", "gemini"]},
                clear=True,
            ),
            patch.dict(
                "djinn_in_a_box.core.docker.SYNC_PATHS",
                {"credentials": ["claude", "gemini"]},
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

            mock_get.assert_called_with("cache")
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
        with patch("typer.confirm", return_value=False), pytest.raises(typer.Exit):
            container.clean_all()

    def test_clean_all_with_force_skips_confirmation(self, mock_app_config: AppConfig) -> None:
        """Test clean all --force skips confirmation and clears both volumes and sync paths."""
        with (
            patch("djinn_in_a_box.commands.container.load_config", return_value=mock_app_config),
            patch("djinn_in_a_box.commands.container.compose_down") as mock_down,
            patch("djinn_in_a_box.commands.container.VOLUME_CATEGORIES", {}),
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
        (configured_root / "gemini").mkdir(parents=True)
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
                {"credentials": ["claude", "gemini"]},
                clear=True,
            ),
            patch.dict(
                "djinn_in_a_box.core.docker.SYNC_PATHS",
                {"credentials": ["claude", "gemini"]},
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
            assert "docker" in call_args
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

    def test_enter_opens_shell(self) -> None:
        """Test enter opens zsh shell in running container."""
        with (
            patch("djinn_in_a_box.commands.container.sys") as mock_sys,
            patch("djinn_in_a_box.commands.container.get_running_containers") as mock_get,
            patch("subprocess.run") as mock_run,
        ):
            mock_sys.stdin.isatty.return_value = True
            mock_get.return_value = ["djinn-12345"]
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(typer.Exit) as exc_info:
                container.enter()

            assert exc_info.value.exit_code == 0
            call_args = mock_run.call_args[0][0]
            assert "docker" in call_args
            assert "exec" in call_args
            assert "-it" in call_args
            assert "zsh" in call_args
            assert "djinn-12345" in call_args


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
