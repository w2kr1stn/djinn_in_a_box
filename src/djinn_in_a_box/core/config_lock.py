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
        # Name the directory: the most common cause is a clone that has never
        # run `djinn init`, since config/ is git-ignored blank space. A generic
        # message leaves that user with nothing to act on.
        raise ConfigDirectoryLockError(
            f"Configuration directory cannot be locked safely: {config_dir} ({error.strerror})"
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
