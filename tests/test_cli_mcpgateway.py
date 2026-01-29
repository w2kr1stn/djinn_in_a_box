"""Tests for the mcpgateway CLI entry point.

Tests for:
- Version callback (--version)
- Help display (--help)
- Command registration
"""

from typer.testing import CliRunner

from ai_dev_base import __version__
from ai_dev_base.cli.mcpgateway import app

runner = CliRunner()


# =============================================================================
# CLI Basic Tests
# =============================================================================


class TestMcpgatewayVersion:
    """Tests for the --version flag."""

    def test_version_short_flag(self) -> None:
        """Test -V shows version and exits."""
        result = runner.invoke(app, ["-V"])
        assert result.exit_code == 0
        assert f"mcpgateway {__version__}" in result.stdout

    def test_version_long_flag(self) -> None:
        """Test --version shows version and exits."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert f"mcpgateway {__version__}" in result.stdout


class TestMcpgatewayHelp:
    """Tests for the --help flag."""

    def test_help_shows_all_commands(self) -> None:
        """Test --help shows all registered commands."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

        # Gateway lifecycle commands
        assert "start" in result.stdout
        assert "stop" in result.stdout
        assert "restart" in result.stdout
        assert "status" in result.stdout
        assert "logs" in result.stdout

        # Server management commands
        assert "enable" in result.stdout
        assert "disable" in result.stdout
        assert "servers" in result.stdout
        assert "catalog" in result.stdout

        # Diagnostic commands
        assert "test" in result.stdout
        assert "clean" in result.stdout

    def test_help_shows_description(self) -> None:
        """Test --help shows application description."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "MCP Gateway CLI" in result.stdout


class TestNoArgsIsHelp:
    """Tests for no_args_is_help behavior."""

    def test_no_args_shows_usage(self) -> None:
        """Test invoking without arguments shows usage info."""
        result = runner.invoke(app, [])
        # With no_args_is_help=True, Typer shows help or usage
        combined = result.stdout + result.output
        assert "start" in combined or "Usage" in combined or "Options" in combined


# =============================================================================
# Command Registration Tests
# =============================================================================


class TestCommandRegistration:
    """Tests for proper command registration."""

    def test_start_command_registered(self) -> None:
        """Test start command is registered."""
        result = runner.invoke(app, ["start", "--help"])
        assert result.exit_code == 0
        assert "Start" in result.stdout or "Gateway" in result.stdout

    def test_stop_command_registered(self) -> None:
        """Test stop command is registered."""
        result = runner.invoke(app, ["stop", "--help"])
        assert result.exit_code == 0
        assert "Stop" in result.stdout or "stop" in result.stdout.lower()

    def test_restart_command_registered(self) -> None:
        """Test restart command is registered."""
        result = runner.invoke(app, ["restart", "--help"])
        assert result.exit_code == 0
        assert "Restart" in result.stdout or "restart" in result.stdout.lower()

    def test_status_command_registered(self) -> None:
        """Test status command is registered."""
        result = runner.invoke(app, ["status", "--help"])
        assert result.exit_code == 0
        assert "status" in result.stdout.lower()

    def test_logs_command_registered(self) -> None:
        """Test logs command is registered."""
        result = runner.invoke(app, ["logs", "--help"])
        assert result.exit_code == 0
        assert "log" in result.stdout.lower()

    def test_enable_command_registered(self) -> None:
        """Test enable command is registered."""
        result = runner.invoke(app, ["enable", "--help"])
        assert result.exit_code == 0
        assert "Enable" in result.stdout or "server" in result.stdout.lower()

    def test_disable_command_registered(self) -> None:
        """Test disable command is registered."""
        result = runner.invoke(app, ["disable", "--help"])
        assert result.exit_code == 0
        assert "Disable" in result.stdout or "server" in result.stdout.lower()

    def test_servers_command_registered(self) -> None:
        """Test servers command is registered."""
        result = runner.invoke(app, ["servers", "--help"])
        assert result.exit_code == 0
        assert "server" in result.stdout.lower() or "list" in result.stdout.lower()

    def test_catalog_command_registered(self) -> None:
        """Test catalog command is registered."""
        result = runner.invoke(app, ["catalog", "--help"])
        assert result.exit_code == 0
        assert "catalog" in result.stdout.lower()

    def test_test_command_registered(self) -> None:
        """Test test command is registered."""
        result = runner.invoke(app, ["test", "--help"])
        assert result.exit_code == 0
        assert "Test" in result.stdout or "connectivity" in result.stdout.lower()

    def test_clean_command_registered(self) -> None:
        """Test clean command is registered."""
        result = runner.invoke(app, ["clean", "--help"])
        assert result.exit_code == 0
        assert "clean" in result.stdout.lower() or "remove" in result.stdout.lower()
