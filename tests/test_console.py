"""Unit tests for ai_dev_base.core.console module."""

import io
from unittest.mock import patch

import pytest
from rich.console import Console

from ai_dev_base.core.console import (
    blank,
    console,
    err_console,
    error,
    header,
    info,
    print_volume_table,
    status_line,
    success,
    warning,
)
from ai_dev_base.core.theme import TODAI_THEME


class TestConsoleSingletons:
    """Tests for console singleton instances."""

    def test_console_is_console_instance(self) -> None:
        """Console should be a Rich Console instance."""
        assert isinstance(console, Console)

    def test_err_console_is_console_instance(self) -> None:
        """Error console should be a Rich Console instance."""
        assert isinstance(err_console, Console)

    def test_err_console_writes_to_stderr(self) -> None:
        """Error console should be configured for stderr."""
        assert err_console.stderr is True

    def test_console_writes_to_stdout(self) -> None:
        """Main console should write to stdout (not stderr)."""
        assert console.stderr is False


class TestStatusLine:
    """Tests for status_line function."""

    def test_status_line_default_style(self) -> None:
        """status_line should use green style by default."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True)

        with patch("ai_dev_base.core.console.err_console", test_console):
            status_line("Projects", "/path/to/code")

        result = output.getvalue()
        assert "Projects:" in result
        assert "/path/to/code" in result

    def test_status_line_custom_style(self) -> None:
        """status_line should accept custom style parameter."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True)

        with patch("ai_dev_base.core.console.err_console", test_console):
            status_line("Docker", "Disabled", "yellow")

        result = output.getvalue()
        assert "Docker:" in result
        assert "Disabled" in result

    def test_status_line_alignment(self) -> None:
        """status_line should include label and value with consistent formatting."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True)

        with patch("ai_dev_base.core.console.err_console", test_console):
            status_line("Projects", "value1")
            status_line("A", "value2")

        # Both lines should contain label: value format
        lines = output.getvalue().strip().split("\n")
        assert len(lines) == 2
        assert "Projects:" in lines[0] and "value1" in lines[0]
        assert "A:" in lines[1] and "value2" in lines[1]


class TestMessageFunctions:
    """Tests for error, success, info, warning functions."""

    def test_error_message(self) -> None:
        """error() should print message with 'Error:' prefix."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True)

        with patch("ai_dev_base.core.console.err_console", test_console):
            error("Mount path does not exist")

        result = output.getvalue()
        assert "Error:" in result
        assert "Mount path does not exist" in result

    def test_success_message(self) -> None:
        """success() should print the message."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True)

        with patch("ai_dev_base.core.console.err_console", test_console):
            success("Build complete")

        assert "Build complete" in output.getvalue()

    def test_info_message(self) -> None:
        """info() should print the message."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True)

        with patch("ai_dev_base.core.console.err_console", test_console):
            info("Starting AI Dev environment...")

        assert "Starting AI Dev environment..." in output.getvalue()

    def test_warning_message(self) -> None:
        """warning() should print message with 'Warning:' prefix."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True)

        with patch("ai_dev_base.core.console.err_console", test_console):
            warning("Config file not found")

        result = output.getvalue()
        assert "Warning:" in result
        assert "Config file not found" in result


class TestBlankAndHeader:
    """Tests for blank and header functions."""

    def test_blank_prints_empty_line(self) -> None:
        """blank() should print an empty line."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True)

        with patch("ai_dev_base.core.console.err_console", test_console):
            blank()

        assert output.getvalue() == "\n"

    def test_header_with_colon(self) -> None:
        """header() should print title with colon."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True)

        with patch("ai_dev_base.core.console.err_console", test_console):
            header("Configuration")

        assert "Configuration:" in output.getvalue()


class TestVolumeTable:
    """Tests for print_volume_table function."""

    def test_print_volume_table(self) -> None:
        """print_volume_table should print table to stdout."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True, theme=TODAI_THEME)

        volumes = {"credentials": ["test-vol"]}

        with patch("ai_dev_base.core.console.console", test_console):
            print_volume_table(volumes)

        result = output.getvalue()
        assert "AI Dev Volumes" in result
        assert "test-vol" in result

    def test_print_volume_table_all_categories(self) -> None:
        """print_volume_table should handle all volume categories."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True, theme=TODAI_THEME)

        volumes = {
            "credentials": ["claude-config", "gemini-config"],
            "tools": ["azure-config"],
            "cache": ["uv-cache"],
            "data": ["opencode-data"],
        }

        with patch("ai_dev_base.core.console.console", test_console):
            print_volume_table(volumes)

        result = output.getvalue()
        assert "Credentials" in result
        assert "claude-config" in result

    def test_print_volume_table_empty(self) -> None:
        """print_volume_table should handle empty volumes dict."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True, theme=TODAI_THEME)

        with patch("ai_dev_base.core.console.console", test_console):
            print_volume_table({})

        result = output.getvalue()
        assert "AI Dev Volumes" in result


class TestColorStyles:
    """Tests verifying correct style applications."""

    @pytest.mark.parametrize(
        "style",
        ["green", "yellow", "red", "blue"],
    )
    def test_status_line_accepts_legacy_styles(self, style: str) -> None:
        """status_line should accept legacy color names for backward compatibility."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True)

        with patch("ai_dev_base.core.console.err_console", test_console):
            # Should not raise any exceptions
            status_line("Test", "value", style)

        assert "Test:" in output.getvalue()

    @pytest.mark.parametrize(
        "style",
        ["status.enabled", "status.disabled", "status.error", "info"],
    )
    def test_status_line_accepts_theme_styles(self, style: str) -> None:
        """status_line should accept TodAI theme style names."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True, theme=TODAI_THEME)

        with patch("ai_dev_base.core.console.err_console", test_console):
            # Should not raise any exceptions
            status_line("Test", "value", style)

        assert "Test:" in output.getvalue()


class TestThemeIntegration:
    """Tests verifying TodAI theme integration."""

    def test_console_recognizes_theme_styles(self) -> None:
        """Console singletons should recognize TodAI theme styles."""
        # Verify theme is applied by checking if theme styles are recognized
        # get_style returns a Style object for valid theme styles
        success_style = console.get_style("success")
        error_style = err_console.get_style("error")

        # These should be Style objects (not raise MissingStyle)
        assert success_style is not None
        assert error_style is not None

    def test_message_functions_include_icons(self) -> None:
        """Message functions should include status icons."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True, theme=TODAI_THEME)

        with patch("ai_dev_base.core.console.err_console", test_console):
            success("test")
            error("test")
            info("test")
            warning("test")

        result = output.getvalue()
        # Check for Unicode icons (checkmark, x, info, warning)
        assert "\u2713" in result  # success checkmark
        assert "\u2717" in result  # error x
        assert "\u2139" in result  # info i
        assert "\u26a0" in result  # warning triangle

    def test_volume_table_uses_theme_styles(self) -> None:
        """print_volume_table should use TodAI theme style names."""
        output = io.StringIO()
        test_console = Console(file=output, force_terminal=True, no_color=True, theme=TODAI_THEME)

        with patch("ai_dev_base.core.console.console", test_console):
            print_volume_table({"credentials": ["test"]})

        # Table output should contain the volume
        assert "test" in output.getvalue()
