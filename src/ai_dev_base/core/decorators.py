"""Decorators for common error handling patterns in CLI commands."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import typer

from ai_dev_base.config.loader import ConfigNotFoundError, ConfigValidationError
from ai_dev_base.core.console import error

P = ParamSpec("P")
R = TypeVar("R")


def handle_config_errors(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator to handle config loading errors uniformly.

    Catches ConfigNotFoundError and ConfigValidationError, converts them
    to a typer.Exit(1) with an appropriate error message.
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except (ConfigNotFoundError, ConfigValidationError) as e:
            error(str(e))
            raise typer.Exit(1) from None

    return wrapper
