"""Unit tests for ai_dev_base.core.console module."""

import io
from collections.abc import Generator
from unittest.mock import patch

import pytest
from rich.console import Console

from ai_dev_base.core.console import (
    error,
    info,
    status_line,
    success,
    warning,
)


@pytest.fixture
def capture_err() -> Generator[io.StringIO]:
    """Capture err_console output (no theme)."""
    output = io.StringIO()
    test_console = Console(file=output, force_terminal=True, no_color=True)
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
