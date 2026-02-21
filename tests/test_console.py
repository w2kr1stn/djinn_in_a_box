"""Unit tests for ai_dev_base.core.console module."""

import io
from collections.abc import Generator
from unittest.mock import patch

import pytest
from rich.console import Console

from ai_dev_base.core.console import (
    blank,
    error,
    header,
    info,
    status_line,
    success,
    warning,
)
from ai_dev_base.core.theme import TODAI_THEME


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


class TestThemeIntegration:
    """Tests verifying TodAI theme integration."""

    @pytest.mark.parametrize("style", ["green", "yellow", "red", "blue"])
    def test_status_line_accepts_legacy_styles(self, style: str, capture_err: io.StringIO) -> None:
        """status_line should accept legacy color names for backward compatibility."""
        status_line("Test", "value", style)
        assert "Test:" in capture_err.getvalue()

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
