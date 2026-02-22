"""Tests for container lifecycle commands."""

import io
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from rich.console import Console

from ai_dev_base.commands import container
from ai_dev_base.core.docker import RunResult
from ai_dev_base.core.theme import TODAI_THEME


class TestBuildCommand:
    """Tests for the build command."""

    def test_build_calls_compose_build(self) -> None:
        """Test build calls compose_build function."""
        with patch("ai_dev_base.commands.container.compose_build") as mock_build:
            mock_build.return_value = RunResult(returncode=0)

            container.build()

            mock_build.assert_called_once_with(no_cache=False)

    def test_build_with_no_cache(self) -> None:
        """Test build --no-cache passes flag to compose_build."""
        with patch("ai_dev_base.commands.container.compose_build") as mock_build:
            mock_build.return_value = RunResult(returncode=0)

            container.build(no_cache=True)

            mock_build.assert_called_once_with(no_cache=True)

    def test_build_exits_on_failure(self) -> None:
        """Test build exits with error code on failure."""
        with patch("ai_dev_base.commands.container.compose_build") as mock_build:
            mock_build.return_value = RunResult(returncode=1, stderr="Build failed")

            with pytest.raises(typer.Exit) as exc_info:
                container.build()

            assert exc_info.value.exit_code == 1


class TestStartCommand:
    """Tests for the start command."""

    @pytest.fixture
    def start_mocks(self) -> Generator[dict[str, Any]]:
        """Common mocks for start command tests."""
        with (
            patch("ai_dev_base.commands.container.load_config") as mock_load,
            patch("ai_dev_base.commands.container.ensure_network", return_value=True),
            patch("ai_dev_base.commands.container.compose_run") as mock_run,
            patch("ai_dev_base.commands.container.cleanup_docker_proxy") as mock_cleanup,
            patch("ai_dev_base.commands.container.get_shell_mount_args", return_value=[]),
        ):
            mock_config = MagicMock()
            mock_config.code_dir = Path("/projects")
            mock_config.shell.skip_mounts = False
            mock_load.return_value = mock_config
            mock_run.return_value = RunResult(returncode=0)
            yield {
                "load": mock_load,
                "run": mock_run,
                "cleanup": mock_cleanup,
                "config": mock_config,
            }

    def test_start_with_docker_flag(self, start_mocks: dict[str, Any]) -> None:
        with pytest.raises(typer.Exit):
            container.start(docker=True)
        options = start_mocks["run"].call_args[0][1]
        assert options.docker_enabled is True
        start_mocks["cleanup"].assert_called_once_with(True)

    def test_start_with_firewall_flag(self, start_mocks: dict[str, Any]) -> None:
        with pytest.raises(typer.Exit):
            container.start(firewall=True)
        options = start_mocks["run"].call_args[0][1]
        assert options.firewall_enabled is True

    def test_start_with_here_flag(
        self, start_mocks: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(typer.Exit):
            container.start(here=True)
        options = start_mocks["run"].call_args[0][1]
        assert options.mount_path == tmp_path

    def test_start_with_mount_path(self, start_mocks: dict[str, Any], tmp_path: Path) -> None:
        with (
            patch("ai_dev_base.commands.container.resolve_mount_path", return_value=tmp_path),
            pytest.raises(typer.Exit),
        ):
            container.start(mount=tmp_path)
        options = start_mocks["run"].call_args[0][1]
        assert options.mount_path == tmp_path

    def test_start_exits_on_config_not_found(self, tmp_path: Path) -> None:
        from ai_dev_base.core.exceptions import ConfigNotFoundError

        with patch("ai_dev_base.commands.container.load_config") as mock_load:
            mock_load.side_effect = ConfigNotFoundError(tmp_path / "config.toml")
            with pytest.raises(typer.Exit) as exc_info:
                container.start()
            assert exc_info.value.exit_code == 1

    def test_start_with_docker_direct_flag(self, start_mocks: dict[str, Any]) -> None:
        with pytest.raises(typer.Exit):
            container.start(docker_direct=True)
        options = start_mocks["run"].call_args[0][1]
        assert options.docker_direct is True
        assert options.docker_enabled is False
        start_mocks["cleanup"].assert_called_once_with(False)

    def test_start_docker_and_direct_mutually_exclusive(self) -> None:
        with pytest.raises(typer.Exit) as exc_info:
            container.start(docker=True, docker_direct=True)
        assert exc_info.value.exit_code == 1


class TestAuthCommand:
    """Tests for the auth command."""

    def test_auth_uses_compose_run_with_profile(self) -> None:
        """Test auth uses compose_run with profile='auth' and service='dev-auth'."""
        with (
            patch("ai_dev_base.commands.container.load_config") as mock_load,
            patch("ai_dev_base.commands.container.compose_run") as mock_run,
            patch("ai_dev_base.commands.container.cleanup_docker_proxy"),
        ):
            mock_config = MagicMock()
            mock_load.return_value = mock_config
            mock_run.return_value = RunResult(returncode=0)

            with pytest.raises(typer.Exit):
                container.auth()

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            assert call_kwargs.kwargs["service"] == "dev-auth"
            assert call_kwargs.kwargs["profile"] == "auth"
            assert call_kwargs.kwargs["interactive"] is True

    def test_auth_with_docker_starts_proxy(self) -> None:
        """Test auth --docker starts docker proxy separately."""
        with (
            patch("ai_dev_base.commands.container.load_config") as mock_load,
            patch("ai_dev_base.commands.container.compose_up") as mock_up,
            patch("ai_dev_base.commands.container.compose_run") as mock_run,
            patch("ai_dev_base.commands.container.cleanup_docker_proxy"),
            patch("time.sleep"),
        ):
            mock_config = MagicMock()
            mock_load.return_value = mock_config
            mock_run.return_value = RunResult(returncode=0)
            mock_up.return_value = RunResult(returncode=0)

            with pytest.raises(typer.Exit):
                container.auth(docker=True)

            mock_up.assert_called_once_with(services=["docker-proxy"], docker_enabled=True)

    def test_auth_with_docker_direct_skips_proxy(self) -> None:
        """Test auth --docker-direct does not start proxy."""
        with (
            patch("ai_dev_base.commands.container.load_config") as mock_load,
            patch("ai_dev_base.commands.container.compose_run") as mock_run,
            patch("ai_dev_base.commands.container.cleanup_docker_proxy") as mock_cleanup,
            patch("ai_dev_base.commands.container.compose_up") as mock_up,
        ):
            mock_config = MagicMock()
            mock_load.return_value = mock_config
            mock_run.return_value = RunResult(returncode=0)

            with pytest.raises(typer.Exit):
                container.auth(docker_direct=True)

            mock_up.assert_not_called()
            mock_cleanup.assert_called_once_with(False)

    def test_auth_docker_and_direct_mutually_exclusive(self) -> None:
        """Test auth --docker --docker-direct raises error."""
        with pytest.raises(typer.Exit) as exc_info:
            container.auth(docker=True, docker_direct=True)

        assert exc_info.value.exit_code == 1


class TestStatusCommand:
    """Tests for the status command."""

    def test_status_handles_missing_config(self, tmp_path: Path) -> None:
        """Test status handles missing configuration gracefully."""
        from ai_dev_base.core.exceptions import ConfigNotFoundError

        config_file = tmp_path / "nonexistent" / "config.toml"

        with (
            patch("ai_dev_base.commands.container.load_config") as mock_load,
            patch("subprocess.run") as mock_run,
            patch(
                "ai_dev_base.commands.container._get_existing_volumes_by_category", return_value=[]
            ),
            patch("ai_dev_base.commands.container.network_exists", return_value=True),
            patch("ai_dev_base.commands.container.is_container_running", return_value=False),
        ):
            mock_load.side_effect = ConfigNotFoundError(config_file)
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            # Should not raise
            container.status()


class TestCleanDefaultCommand:
    """Tests for the clean default behavior."""

    def test_clean_default_runs_compose_down(self) -> None:
        """Test clean without subcommand runs compose down."""
        from typer import Context

        with patch("ai_dev_base.commands.container.compose_down") as mock_down:
            mock_down.return_value = RunResult(returncode=0)

            # Create a mock context with no invoked subcommand
            mock_ctx = MagicMock(spec=Context)
            mock_ctx.invoked_subcommand = None

            container.clean_default(mock_ctx)

            mock_down.assert_called_once()


class TestCleanVolumesCommand:
    """Tests for the clean volumes command."""

    def test_clean_volumes_lists_without_flags(self) -> None:
        """Test clean volumes without flags lists volumes."""
        with patch("ai_dev_base.commands.container._get_existing_volumes_by_category") as mock_get:
            mock_get.return_value = ["ai-dev-claude-config"]

            container.clean_volumes()

            # Should have queried categories
            assert mock_get.call_count >= 1

    def test_clean_volumes_deletes_credentials(self) -> None:
        """Test clean volumes --credentials deletes credential volumes."""
        with (
            patch("ai_dev_base.commands.container._get_existing_volumes_by_category") as mock_get,
            patch("ai_dev_base.commands.container.delete_volumes") as mock_delete,
        ):
            mock_get.return_value = ["ai-dev-claude-config"]
            mock_delete.return_value = {"ai-dev-claude-config": True}

            container.clean_volumes(credentials=True)

            mock_get.assert_called_with("credentials")
            mock_delete.assert_called_once()

    def test_clean_volumes_deletes_specific_volume(self) -> None:
        """Test clean volumes <name> deletes specific volume."""
        with (
            patch("ai_dev_base.commands.container.volume_exists", return_value=True),
            patch("ai_dev_base.commands.container.delete_volume") as mock_delete,
        ):
            mock_delete.return_value = True

            container.clean_volumes(name="ai-dev-test-volume")

            mock_delete.assert_called_once_with("ai-dev-test-volume")

    def test_clean_volumes_errors_on_nonexistent(self) -> None:
        """Test clean volumes <name> errors if volume doesn't exist."""
        with patch("ai_dev_base.commands.container.volume_exists", return_value=False):
            with pytest.raises(typer.Exit) as exc_info:
                container.clean_volumes(name="nonexistent-volume")

            assert exc_info.value.exit_code == 1


class TestCleanAllCommand:
    """Tests for the clean all command."""

    def test_clean_all_requires_confirmation(self) -> None:
        """Test clean all requires user confirmation."""
        with patch("typer.confirm", return_value=False), pytest.raises(typer.Exit):
            container.clean_all()

    def test_clean_all_with_force_skips_confirmation(self) -> None:
        """Test clean all --force skips confirmation."""
        with (
            patch("ai_dev_base.commands.container.compose_down") as mock_down,
            patch("ai_dev_base.commands.container.VOLUME_CATEGORIES", {}),
            patch("ai_dev_base.commands.container.network_exists", return_value=False),
        ):
            mock_down.return_value = RunResult(returncode=0)

            container.clean_all(force=True)

            mock_down.assert_called_once()


class TestAuditCommand:
    """Tests for the audit command."""

    def test_audit_requires_proxy_running(self) -> None:
        """Test audit requires docker proxy to be running."""
        with patch("ai_dev_base.commands.container.is_container_running", return_value=False):
            with pytest.raises(typer.Exit) as exc_info:
                container.audit()

            assert exc_info.value.exit_code == 1

    def test_audit_shows_logs(self) -> None:
        """Test audit shows proxy logs."""
        with (
            patch("ai_dev_base.commands.container.is_container_running", return_value=True),
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
            patch("ai_dev_base.commands.container.is_container_running", return_value=True),
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
            patch("ai_dev_base.commands.container.is_container_running", return_value=True),
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
            patch("ai_dev_base.commands.container.get_project_root", return_value=tmp_path),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            container.update()

            call_args = mock_run.call_args[0][0]
            assert str(script_path) in call_args

    def test_update_errors_if_script_missing(self, tmp_path: Path) -> None:
        """Test update errors if script doesn't exist."""
        with patch("ai_dev_base.commands.container.get_project_root", return_value=tmp_path):
            with pytest.raises(typer.Exit) as exc_info:
                container.update()

            assert exc_info.value.exit_code == 1


class TestEnterCommand:
    """Tests for the enter command."""

    def test_enter_requires_tty(self) -> None:
        """Test enter requires a TTY."""
        with patch("ai_dev_base.commands.container.sys") as mock_sys:
            mock_sys.stdin.isatty.return_value = False
            with pytest.raises(typer.Exit) as exc_info:
                container.enter()

            assert exc_info.value.exit_code == 1

    def test_enter_requires_running_container(self) -> None:
        """Test enter requires a running ai-dev container."""
        with (
            patch("ai_dev_base.commands.container.sys") as mock_sys,
            patch("ai_dev_base.commands.container.get_running_containers", return_value=[]),
        ):
            mock_sys.stdin.isatty.return_value = True
            with pytest.raises(typer.Exit) as exc_info:
                container.enter()

            assert exc_info.value.exit_code == 1

    def test_enter_opens_shell(self) -> None:
        """Test enter opens zsh shell in running container."""
        with (
            patch("ai_dev_base.commands.container.sys") as mock_sys,
            patch("ai_dev_base.commands.container.get_running_containers") as mock_get,
            patch("subprocess.run") as mock_run,
        ):
            mock_sys.stdin.isatty.return_value = True
            mock_get.return_value = ["ai-dev-base-dev-12345"]
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(typer.Exit) as exc_info:
                container.enter()

            assert exc_info.value.exit_code == 0
            call_args = mock_run.call_args[0][0]
            assert "docker" in call_args
            assert "exec" in call_args
            assert "-it" in call_args
            assert "zsh" in call_args
            assert "ai-dev-base-dev-12345" in call_args


class TestVolumeTable:
    """Tests for _print_volume_table function."""

    @pytest.fixture
    def capture_container_stdout(self) -> Generator[io.StringIO]:
        """Capture container module's console (stdout) output."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True, theme=TODAI_THEME)
        with patch("ai_dev_base.commands.container.console", test_console):
            yield output

    def test_print_volume_table_all_categories(self, capture_container_stdout: io.StringIO) -> None:
        """_print_volume_table should handle all volume categories."""
        volumes = {
            "credentials": ["claude-config", "gemini-config"],
            "tools": ["azure-config"],
            "cache": ["uv-cache"],
            "data": ["opencode-data"],
        }
        container._print_volume_table(volumes)
        result = capture_container_stdout.getvalue()
        assert "Credentials" in result
        assert "claude-config" in result
