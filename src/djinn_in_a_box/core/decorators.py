"""Decorators for common error handling patterns in CLI commands."""

from __future__ import annotations

import errno
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import typer

from djinn_in_a_box.core.config_lock import ConfigDirectoryLockError
from djinn_in_a_box.core.console import error, warning
from djinn_in_a_box.core.exceptions import ConfigNotFoundError, ConfigValidationError

P = ParamSpec("P")
R = TypeVar("R")


def handle_config_errors(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator to handle config loading errors uniformly.

    Catches ConfigNotFoundError, ConfigValidationError, and
    ConfigDirectoryLockError, converting them to a typer.Exit(1) with an
    actionable message instead of a traceback.
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except ConfigDirectoryLockError as e:
            error(str(e))
            cause = e.__cause__
            if isinstance(cause, OSError) and cause.errno == errno.ENOENT:
                warning("Run `djinn init` to create it, then retry.")
            else:
                warning(
                    "Check that the configuration directory is readable and lockable, "
                    "then retry."
                )
            raise typer.Exit(1) from None
        except (ConfigNotFoundError, ConfigValidationError) as e:
            error(str(e))
            raise typer.Exit(1) from None

    return wrapper
