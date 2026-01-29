"""Tests for core decorators."""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from ai_dev_base.config import ConfigNotFoundError
from ai_dev_base.core.decorators import handle_config_errors


class TestHandleConfigErrors:
    """Tests for @handle_config_errors decorator."""

    def test_passes_through_normal_execution(self) -> None:
        """Test decorator allows normal execution."""
        @handle_config_errors
        def normal_func() -> str:
            return "success"

        assert normal_func() == "success"

    def test_catches_config_not_found_error(self) -> None:
        """Test decorator catches ConfigNotFoundError and exits with code 1."""
        @handle_config_errors
        def failing_func() -> None:
            raise ConfigNotFoundError(Path("/nonexistent/config.toml"))

        with pytest.raises(typer.Exit) as exc_info:
            failing_func()

        assert exc_info.value.exit_code == 1

    def test_does_not_suppress_other_exceptions(self) -> None:
        """Test decorator only handles ConfigNotFoundError."""
        @handle_config_errors
        def other_error_func() -> None:
            raise ValueError("Other error")

        with pytest.raises(ValueError, match="Other error"):
            other_error_func()

    def test_preserves_function_metadata(self) -> None:
        """Test decorator preserves function name and docstring."""
        @handle_config_errors
        def documented_func() -> None:
            """This is a docstring."""
            pass

        assert documented_func.__name__ == "documented_func"
        assert documented_func.__doc__ == "This is a docstring."

    def test_passes_arguments_correctly(self) -> None:
        """Test decorator passes args and kwargs to wrapped function."""
        @handle_config_errors
        def func_with_args(a: int, b: str, c: bool = False) -> tuple[int, str, bool]:
            return (a, b, c)

        result = func_with_args(1, "test", c=True)
        assert result == (1, "test", True)


class TestRequireMcpCli:
    """Tests for @require_mcp_cli decorator."""

    def test_passes_when_mcp_cli_installed(self) -> None:
        """Test decorator allows execution when MCP CLI is installed."""
        from ai_dev_base.commands.mcp import require_mcp_cli

        @require_mcp_cli
        def mcp_func() -> str:
            return "mcp success"

        with patch("ai_dev_base.commands.mcp.check_mcp_cli"):
            assert mcp_func() == "mcp success"

    def test_exits_when_mcp_cli_not_installed(self) -> None:
        """Test decorator exits with code 1 when MCP CLI is missing."""
        from ai_dev_base.commands.mcp import MCPCliNotFoundError, require_mcp_cli

        @require_mcp_cli
        def mcp_func() -> str:
            return "should not reach"

        with (
            patch("ai_dev_base.commands.mcp.check_mcp_cli") as mock_check,
            pytest.raises(typer.Exit) as exc_info,
        ):
            mock_check.side_effect = MCPCliNotFoundError("MCP CLI not installed")
            mcp_func()

        assert exc_info.value.exit_code == 1

    def test_preserves_function_metadata(self) -> None:
        """Test decorator preserves function name and docstring."""
        from ai_dev_base.commands.mcp import require_mcp_cli

        @require_mcp_cli
        def documented_mcp_func() -> None:
            """MCP docstring."""
            pass

        assert documented_mcp_func.__name__ == "documented_mcp_func"
        assert documented_mcp_func.__doc__ == "MCP docstring."
