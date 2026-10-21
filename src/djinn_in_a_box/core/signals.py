"""Scoped signal handling for operations that must finish their cleanup."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def signals_as_interrupt() -> Iterator[None]:
    """Treat SIGTERM/SIGHUP like Ctrl-C so cleanup runs; handlers are restored afterwards."""

    def interrupt(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous: dict[int, Any] = {}
    try:
        for number in (signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, interrupt)
    except ValueError:  # not the main thread: nothing to convert
        pass
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler if handler is not None else signal.SIG_DFL)


@contextmanager
def signals_ignored() -> Iterator[None]:
    """Ignore INT/TERM/HUP during bounded cleanup and restore the prior handlers."""
    previous: dict[int, Any] = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, signal.SIG_IGN)
    except ValueError:  # not the main thread: nothing to ignore
        pass
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler if handler is not None else signal.SIG_DFL)
