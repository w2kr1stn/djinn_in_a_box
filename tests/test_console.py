"""Unit tests for ai_dev_base.core.console module."""

import io
from collections.abc import Generator
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

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def capture_err() -> Generator[io.StringIO]:
    """Capture err_console output (no theme)."""
    output = io.StringIO()
    test_console = Console(file=output, force_terminal=True, no_color=True)
    with patch("ai_dev_base.core.console.err_console", test_console):
        yield output


@pytest.fixture
def capture_err_themed() -> Generator[io.StringIO]:
    """Capture err_console output (with TodAI theme)."""
    output = io.StringIO()
    test_console = Console(file=output, force_terminal=True, no_color=True, theme=TODAI_THEME)
    with patch("ai_dev_base.core.console.err_console", test_console):
        yield output


@pytest.fixture
def capture_stdout() -> Generator[io.StringIO]:
    """Capture console (stdout) output (with TodAI theme)."""
    output = io.StringIO()
    test_console = Console(file=output, force_terminal=True, no_color=True, theme=TODAI_THEME)
    with patch("ai_dev_base.core.console.console", test_console):
        yield output


# =============================================================================
# Tests
# =============================================================================


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

    def test_status_line_default_style(self, capture_err: io.StringIO) -> None:
        """status_line should use green style by default."""
        status_line("Projects", "/path/to/code")
        result = capture_err.getvalue()
        assert "Projects:" in result
        assert "/path/to/code" in result

    def test_status_line_custom_style(self, capture_err: io.StringIO) -> None:
        """status_line should accept custom style parameter."""
        status_line("Docker", "Disabled", "yellow")
        result = capture_err.getvalue()
        assert "Docker:" in result
        assert "Disabled" in result

    def test_status_line_alignment(self, capture_err: io.StringIO) -> None:
        """status_line should include label and value with consistent formatting."""
        status_line("Projects", "value1")
        status_line("A", "value2")
        lines = capture_err.getvalue().strip().split("\n")
        assert len(lines) == 2
        assert "Projects:" in lines[0] and "value1" in lines[0]
        assert "A:" in lines[1] and "value2" in lines[1]


class TestMessageFunctions:
    """Tests for error, success, info, warning functions."""

    def test_error_message(self, capture_err: io.StringIO) -> None:
        """error() should print message with 'Error:' prefix."""
        error("Mount path does not exist")
        result = capture_err.getvalue()
        assert "Error:" in result
        assert "Mount path does not exist" in result

    def test_success_message(self, capture_err: io.StringIO) -> None:
        """success() should print the message."""
        success("Build complete")
        assert "Build complete" in capture_err.getvalue()

    def test_info_message(self, capture_err: io.StringIO) -> None:
        """info() should print the message."""
        info("Starting AI Dev environment...")
        assert "Starting AI Dev environment..." in capture_err.getvalue()

    def test_warning_message(self, capture_err: io.StringIO) -> None:
        """warning() should print message with 'Warning:' prefix."""
        warning("Config file not found")
        result = capture_err.getvalue()
        assert "Warning:" in result
        assert "Config file not found" in result


class TestBlankAndHeader:
    """Tests for blank and header functions."""

    def test_blank_prints_empty_line(self, capture_err: io.StringIO) -> None:
        """blank() should print an empty line."""
        blank()
        assert capture_err.getvalue() == "\n"

    def test_header_with_colon(self, capture_err: io.StringIO) -> None:
        """header() should print title with colon."""
        header("Configuration")
        assert "Configuration:" in capture_err.getvalue()


class TestVolumeTable:
    """Tests for print_volume_table function."""

    def test_print_volume_table(self, capture_stdout: io.StringIO) -> None:
        """print_volume_table should print table to stdout."""
        print_volume_table({"credentials": ["test-vol"]})
        result = capture_stdout.getvalue()
        assert "AI Dev Volumes" in result
        assert "test-vol" in result

    def test_print_volume_table_all_categories(self, capture_stdout: io.StringIO) -> None:
        """print_volume_table should handle all volume categories."""
        volumes = {
            "credentials": ["claude-config", "gemini-config"],
            "tools": ["azure-config"],
            "cache": ["uv-cache"],
            "data": ["opencode-data"],
        }
        print_volume_table(volumes)
        result = capture_stdout.getvalue()
        assert "Credentials" in result
        assert "claude-config" in result

    def test_print_volume_table_empty(self, capture_stdout: io.StringIO) -> None:
        """print_volume_table should handle empty volumes dict."""
        print_volume_table({})
        assert "AI Dev Volumes" in capture_stdout.getvalue()


class TestColorStyles:
    """Tests verifying correct style applications."""

    @pytest.mark.parametrize("style", ["green", "yellow", "red", "blue"])
    def test_status_line_accepts_legacy_styles(self, style: str, capture_err: io.StringIO) -> None:
        """status_line should accept legacy color names for backward compatibility."""
        status_line("Test", "value", style)
        assert "Test:" in capture_err.getvalue()

    @pytest.mark.parametrize("style", ["status.enabled", "status.disabled", "status.error", "info"])
    def test_status_line_accepts_theme_styles(
        self, style: str, capture_err_themed: io.StringIO
    ) -> None:
        """status_line should accept TodAI theme style names."""
        status_line("Test", "value", style)
        assert "Test:" in capture_err_themed.getvalue()


class TestThemeIntegration:
    """Tests verifying TodAI theme integration."""

    def test_console_recognizes_theme_styles(self) -> None:
        """Console singletons should recognize TodAI theme styles."""
        success_style = console.get_style("success")
        error_style = err_console.get_style("error")
        assert success_style is not None
        assert error_style is not None

    def test_message_functions_include_icons(self, capture_err_themed: io.StringIO) -> None:
        """Message functions should include status icons."""
        success("test")
        error("test")
        info("test")
        warning("test")
        result = capture_err_themed.getvalue()
        assert "\u2713" in result  # success checkmark
        assert "\u2717" in result  # error x
        assert "\u2139" in result  # info i
        assert "\u26a0" in result  # warning triangle

    def test_volume_table_uses_theme_styles(self, capture_stdout: io.StringIO) -> None:
        """print_volume_table should use TodAI theme style names."""
        print_volume_table({"credentials": ["test"]})
        assert "test" in capture_stdout.getvalue()
