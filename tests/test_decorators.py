"""Tests for core decorators."""

from pathlib import Path

import pytest
import typer

from ai_dev_base.core.decorators import handle_config_errors
from ai_dev_base.core.exceptions import ConfigNotFoundError, ConfigValidationError


class TestHandleConfigErrors:
    """Tests for @handle_config_errors decorator."""

    def test_catches_config_not_found_error(self) -> None:
        """Test decorator catches ConfigNotFoundError and exits with code 1."""

        @handle_config_errors
        def failing_func() -> None:
            raise ConfigNotFoundError(Path("/nonexistent/config.toml"))

        with pytest.raises(typer.Exit) as exc_info:
            failing_func()

        assert exc_info.value.exit_code == 1

    def test_catches_config_validation_error(self) -> None:
        """Test decorator catches ConfigValidationError and exits with code 1."""

        @handle_config_errors
        def invalid_config_func() -> None:
            raise ConfigValidationError("Invalid config: bad timezone format")

        with pytest.raises(typer.Exit) as exc_info:
            invalid_config_func()

        assert exc_info.value.exit_code == 1

    def test_does_not_suppress_other_exceptions(self) -> None:
        """Test decorator does not catch non-config exceptions."""

        @handle_config_errors
        def other_error_func() -> None:
            raise ValueError("Other error")

        with pytest.raises(ValueError, match="Other error"):
            other_error_func()
