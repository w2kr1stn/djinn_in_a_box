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
