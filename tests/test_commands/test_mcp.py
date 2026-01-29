"""Tests for MCP Gateway commands.

Tests for:
- Helper functions: check_mcp_cli, require_running, get_mcp_dir
- Gateway lifecycle: start, stop, restart, status, logs
- Server management: enable, disable, servers, catalog
- Diagnostics: test, clean
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from ai_dev_base.commands import mcp
from ai_dev_base.commands.mcp import (
    AI_DEV_NETWORK,
    GATEWAY_CONTAINER,
    GATEWAY_ENDPOINT_CONTAINER,
    GATEWAY_ENDPOINT_HOST,
    MCPCliNotFoundError,
    check_mcp_cli,
    get_mcp_dir,
    require_running,
)

# =============================================================================
# Test Constants
# =============================================================================


class TestConstants:
    """Tests for module constants."""

    def test_gateway_container_name(self) -> None:
        """Verify gateway container name constant."""
        assert GATEWAY_CONTAINER == "mcp-gateway"

    def test_gateway_endpoints(self) -> None:
        """Verify gateway endpoint constants."""
        assert GATEWAY_ENDPOINT_CONTAINER == "http://mcp-gateway:8811"
        assert GATEWAY_ENDPOINT_HOST == "http://localhost:8811"

    def test_network_name(self) -> None:
        """Verify network name constant."""
        assert AI_DEV_NETWORK == "ai-dev-network"


# =============================================================================
# Test Helper Functions
# =============================================================================


class TestCheckMcpCli:
    """Tests for check_mcp_cli helper function."""

    def test_raises_when_docker_mcp_not_installed(self) -> None:
        """Test that MCPCliNotFoundError is raised when docker mcp is not available."""
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(MCPCliNotFoundError) as exc_info:
                check_mcp_cli()

            # Verify error message contains installation instructions
            assert "docker mcp" in str(exc_info.value)
            assert "github.com/docker/mcp-gateway" in str(exc_info.value)

    def test_passes_when_docker_mcp_installed(self) -> None:
        """Test that no error is raised when docker mcp is available."""
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result):
            # Should not raise
            check_mcp_cli()


class TestRequireRunning:
    """Tests for require_running helper function."""

    def test_raises_when_gateway_not_running(self) -> None:
        """Test that typer.Exit is raised when gateway is not running."""
        with patch("ai_dev_base.commands.mcp.is_container_running", return_value=False):
            with pytest.raises(typer.Exit) as exc_info:
                require_running()

            assert exc_info.value.exit_code == 1

    def test_passes_when_gateway_running(self) -> None:
        """Test that no error is raised when gateway is running."""
        with patch("ai_dev_base.commands.mcp.is_container_running", return_value=True):
            # Should not raise
            require_running()


class TestGetMcpDir:
    """Tests for get_mcp_dir helper function."""

    def test_returns_mcp_directory_path(self) -> None:
        """Test that get_mcp_dir returns the mcp/ subdirectory."""
        mock_root = Path("/fake/project/root")

        with patch("ai_dev_base.commands.mcp.get_project_root", return_value=mock_root):
            result = get_mcp_dir()

        assert result == mock_root / "mcp"


# =============================================================================
# Test Gateway Lifecycle Commands
# =============================================================================


class TestStartCommand:
    """Tests for the start command."""

    def test_start_checks_mcp_cli(self) -> None:
        """Test that start checks for docker mcp CLI first."""
        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli") as mock_check,
            patch("ai_dev_base.commands.mcp.ensure_network"),
            patch("subprocess.run"),
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("time.sleep"),
        ):
            mock_check.side_effect = MCPCliNotFoundError("not installed")

            with pytest.raises(typer.Exit):
                mcp.start()

            mock_check.assert_called_once()

    def test_start_ensures_network(self) -> None:
        """Test that start ensures the network exists."""
        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
            patch("ai_dev_base.commands.mcp.ensure_network") as mock_network,
            patch("subprocess.run") as mock_run,
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("time.sleep"),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.start()

            mock_network.assert_called_once_with(AI_DEV_NETWORK)

    def test_start_runs_compose_up(self) -> None:
        """Test that start runs docker compose up."""
        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
            patch("ai_dev_base.commands.mcp.ensure_network"),
            patch("subprocess.run") as mock_run,
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("time.sleep"),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.start()

            # First call should be docker compose up
            first_call = mock_run.call_args_list[0]
            assert first_call[0][0] == ["docker", "compose", "up", "-d"]

    def test_start_exits_on_compose_failure(self) -> None:
        """Test that start exits when compose up fails."""
        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
            patch("ai_dev_base.commands.mcp.ensure_network"),
            patch("subprocess.run") as mock_run,
            patch("time.sleep"),
        ):
            mock_run.return_value = MagicMock(returncode=1)

            with pytest.raises(typer.Exit):
                mcp.start()


class TestStopCommand:
    """Tests for the stop command."""

    def test_stop_runs_compose_down(self) -> None:
        """Test that stop runs docker compose down."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            mcp.stop()

            first_call = mock_run.call_args_list[0]
            assert first_call[0][0] == ["docker", "compose", "down"]


class TestRestartCommand:
    """Tests for the restart command."""

    def test_restart_runs_compose_restart(self) -> None:
        """Test that restart runs docker compose restart."""
        with (
            patch("subprocess.run") as mock_run,
            patch("time.sleep"),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.restart()

            first_call = mock_run.call_args_list[0]
            assert first_call[0][0] == ["docker", "compose", "restart"]


class TestStatusCommand:
    """Tests for the status command."""

    def test_status_shows_running_when_gateway_up(self) -> None:
        """Test that status shows running status when gateway is up."""
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            mcp.status()

            # Should have called docker ps and docker mcp server ls
            assert mock_run.call_count >= 2

    def test_status_shows_stopped_when_gateway_down(self) -> None:
        """Test that status shows stopped status when gateway is down."""
        with patch("ai_dev_base.commands.mcp.is_container_running", return_value=False):
            # Should not raise, just print stopped status
            mcp.status()


class TestLogsCommand:
    """Tests for the logs command."""

    def test_logs_requires_running_gateway(self) -> None:
        """Test that logs requires the gateway to be running."""
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=False),
            pytest.raises(typer.Exit),
        ):
            mcp.logs()

    def test_logs_runs_docker_logs(self) -> None:
        """Test that logs runs docker logs command."""
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
            pytest.raises(typer.Exit) as exc_info,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.logs()

        assert exc_info.value.exit_code == 0
        first_call = mock_run.call_args_list[0]
        cmd = first_call[0][0]
        assert cmd[0:2] == ["docker", "logs"]
        assert GATEWAY_CONTAINER in cmd

    def test_logs_with_follow_flag(self) -> None:
        """Test that logs with follow flag includes -f."""
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
            pytest.raises(typer.Exit) as exc_info,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.logs(follow=True)

        assert exc_info.value.exit_code == 0
        first_call = mock_run.call_args_list[0]
        cmd = first_call[0][0]
        assert "-f" in cmd

    def test_logs_with_tail_option(self) -> None:
        """Test that logs with tail option includes --tail."""
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
            pytest.raises(typer.Exit) as exc_info,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.logs(tail=50)

        assert exc_info.value.exit_code == 0
        first_call = mock_run.call_args_list[0]
        cmd = first_call[0][0]
        assert "--tail" in cmd
        assert "50" in cmd


# =============================================================================
# Test Server Management Commands
# =============================================================================


class TestEnableCommand:
    """Tests for the enable command."""

    def test_enable_checks_mcp_cli(self) -> None:
        """Test that enable checks for docker mcp CLI first."""
        with patch("ai_dev_base.commands.mcp.check_mcp_cli") as mock_check:
            mock_check.side_effect = MCPCliNotFoundError("not installed")

            with pytest.raises(typer.Exit):
                mcp.enable("duckduckgo")

            mock_check.assert_called_once()

    def test_enable_requires_running_gateway(self) -> None:
        """Test that enable requires the gateway to be running."""
        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=False),
            pytest.raises(typer.Exit),
        ):
            mcp.enable("duckduckgo")

    def test_enable_runs_docker_mcp_enable(self) -> None:
        """Test that enable runs docker mcp server enable."""
        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.enable("duckduckgo")

            first_call = mock_run.call_args_list[0]
            assert first_call[0][0] == ["docker", "mcp", "server", "enable", "duckduckgo"]


class TestDisableCommand:
    """Tests for the disable command."""

    def test_disable_checks_mcp_cli(self) -> None:
        """Test that disable checks for docker mcp CLI first."""
        with patch("ai_dev_base.commands.mcp.check_mcp_cli") as mock_check:
            mock_check.side_effect = MCPCliNotFoundError("not installed")

            with pytest.raises(typer.Exit):
                mcp.disable("duckduckgo")

            mock_check.assert_called_once()

    def test_disable_requires_running_gateway(self) -> None:
        """Test that disable requires the gateway to be running."""
        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=False),
            pytest.raises(typer.Exit),
        ):
            mcp.disable("duckduckgo")

    def test_disable_runs_docker_mcp_disable(self) -> None:
        """Test that disable runs docker mcp server disable."""
        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.disable("memory")

            first_call = mock_run.call_args_list[0]
            assert first_call[0][0] == ["docker", "mcp", "server", "disable", "memory"]


class TestServersCommand:
    """Tests for the servers command."""

    def test_servers_checks_mcp_cli(self) -> None:
        """Test that servers checks for docker mcp CLI first."""
        with patch("ai_dev_base.commands.mcp.check_mcp_cli") as mock_check:
            mock_check.side_effect = MCPCliNotFoundError("not installed")

            with pytest.raises(typer.Exit):
                mcp.servers()

            mock_check.assert_called_once()

    def test_servers_runs_docker_mcp_ls(self) -> None:
        """Test that servers runs docker mcp server ls."""
        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="duckduckgo\nmemory")

            mcp.servers()

            first_call = mock_run.call_args_list[0]
            assert first_call[0][0] == ["docker", "mcp", "server", "ls"]


class TestCatalogCommand:
    """Tests for the catalog command."""

    def test_catalog_checks_mcp_cli(self) -> None:
        """Test that catalog checks for docker mcp CLI first."""
        with patch("ai_dev_base.commands.mcp.check_mcp_cli") as mock_check:
            mock_check.side_effect = MCPCliNotFoundError("not installed")

            with pytest.raises(typer.Exit):
                mcp.catalog()

            mock_check.assert_called_once()

    def test_catalog_runs_docker_mcp_catalog(self) -> None:
        """Test that catalog runs docker mcp catalog show."""
        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="catalog data")

            mcp.catalog()

            # First call is to check if catalog is available
            first_call = mock_run.call_args_list[0]
            assert "catalog" in first_call[0][0]


# =============================================================================
# Test Diagnostic Commands
# =============================================================================


class TestTestCommand:
    """Tests for the test command."""

    def test_test_checks_container_status(self) -> None:
        """Test that test command checks container status."""
        with (
            patch(
                "ai_dev_base.commands.mcp.is_container_running", return_value=False
            ) as mock_running,
            patch("subprocess.run") as mock_run,
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
            pytest.raises(typer.Exit),
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            mcp.test()
            mock_running.assert_called_once_with(GATEWAY_CONTAINER)

    def test_test_checks_localhost_endpoint(self) -> None:
        """Test that test command checks localhost endpoint."""
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="200")

            mcp.test()

            # Should have curl call for localhost
            curl_calls = [
                c for c in mock_run.call_args_list
                if c[0][0][0] == "curl"
            ]
            assert len(curl_calls) >= 1

    def test_test_checks_docker_socket(self) -> None:
        """Test that test command checks docker socket access."""
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("ai_dev_base.commands.mcp.check_mcp_cli"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="200")

            mcp.test()

            # Should have docker exec call
            exec_calls = [
                c for c in mock_run.call_args_list
                if "exec" in c[0][0]
            ]
            assert len(exec_calls) >= 1


class TestCleanCommand:
    """Tests for the clean command."""

    def test_clean_requires_confirmation(self) -> None:
        """Test that clean requires user confirmation."""
        with patch("typer.confirm", return_value=False), pytest.raises(typer.Abort):
            mcp.clean()

    def test_clean_stops_gateway(self) -> None:
        """Test that clean stops the gateway."""
        with (
            patch("typer.confirm", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("shutil.rmtree"),
            patch("pathlib.Path.exists", return_value=False),
            patch("ai_dev_base.commands.mcp.get_mcp_dir", return_value=Path("/fake/mcp")),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.clean()

            # First call should be docker compose down
            first_call = mock_run.call_args_list[0]
            assert first_call[0][0] == ["docker", "compose", "down"]

    def test_clean_removes_network(self) -> None:
        """Test that clean removes the network."""
        with (
            patch("typer.confirm", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("shutil.rmtree"),
            patch("pathlib.Path.exists", return_value=False),
            patch("ai_dev_base.commands.mcp.get_mcp_dir", return_value=Path("/fake/mcp")),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.clean()

            # Second call should be docker network rm
            second_call = mock_run.call_args_list[1]
            assert second_call[0][0] == ["docker", "network", "rm", AI_DEV_NETWORK]

    def test_clean_removes_mcp_config_dir(self) -> None:
        """Test that clean removes the MCP config directory."""
        with (
            patch("typer.confirm", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("shutil.rmtree") as mock_rmtree,
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.home", return_value=Path("/fake/home")),
        ):
            mock_run.return_value = MagicMock(returncode=0)

            mcp.clean()

            # Should have removed ~/.docker/mcp
            mock_rmtree.assert_called_once()


# =============================================================================
# Test CLI Integration
# =============================================================================


class TestCliIntegration:
    """Tests for CLI entry point integration."""

    def test_all_commands_registered(self) -> None:
        """Test that all MCP commands are registered in the CLI."""
        from ai_dev_base.cli.mcpgateway import app

        # Get registered command names from Typer app
        # Typer stores registered commands with their callback function names
        command_names = list(app.registered_commands)
        # Extract the actual command names from the registered callbacks
        registered_names = []
        for cmd in command_names:
            if cmd.callback is not None:
                registered_names.append(cmd.callback.__name__)

        expected_commands = [
            "start",
            "stop",
            "restart",
            "status",
            "logs",
            "enable",
            "disable",
            "servers",
            "catalog",
            "test",
            "clean",
        ]

        for cmd in expected_commands:
            assert cmd in registered_names, f"Command '{cmd}' not registered in CLI"

    def test_function_imports(self) -> None:
        """Test that all MCP functions can be imported."""
        from ai_dev_base.commands.mcp import (
            catalog,
            check_mcp_cli,
            clean,
            disable,
            enable,
            get_mcp_dir,
            logs,
            require_running,
            restart,
            servers,
            start,
            status,
            stop,
            test,
        )

        # All should be callable
        assert callable(check_mcp_cli)
        assert callable(require_running)
        assert callable(get_mcp_dir)
        assert callable(start)
        assert callable(stop)
        assert callable(restart)
        assert callable(status)
        assert callable(logs)
        assert callable(enable)
        assert callable(disable)
        assert callable(servers)
        assert callable(catalog)
        assert callable(test)
        assert callable(clean)
