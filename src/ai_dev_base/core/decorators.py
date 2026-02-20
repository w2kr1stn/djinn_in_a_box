"""Decorators for common error handling patterns in CLI commands."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import typer

P = ParamSpec("P")
R = TypeVar("R")


def handle_config_errors(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator to handle config loading errors uniformly.

    Catches ConfigNotFoundError and ConfigValidationError, converts them
    to a typer.Exit(1) with an appropriate error message.

    Example:
        >>> @handle_config_errors
        ... def my_command():
        ...     config = load_config()  # May raise ConfigNotFoundError/ConfigValidationError
        ...     # ... rest of command
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # Import here to avoid circular import
        from ai_dev_base.config import ConfigNotFoundError, ConfigValidationError
        from ai_dev_base.core.console import error

        try:
            return func(*args, **kwargs)
        except (ConfigNotFoundError, ConfigValidationError) as e:
            error(str(e))
            raise typer.Exit(1) from None

    return wrapper
