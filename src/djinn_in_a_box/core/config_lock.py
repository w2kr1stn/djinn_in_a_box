"""Process coordination for project-local workflow configuration."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ConfigDirectoryLockError(OSError):
    """The config directory could not be opened without following links."""


@contextmanager
def config_directory_lock(config_dir: Path, *, exclusive: bool) -> Iterator[int]:
    try:
        descriptor = os.open(config_dir, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise ConfigDirectoryLockError(
            "Configuration directory cannot be locked safely."
        ) from error
    locked = False
    try:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, operation)
        locked = True
        yield descriptor
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
