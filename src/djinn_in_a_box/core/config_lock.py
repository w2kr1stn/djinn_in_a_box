"""Process coordination for project-local workflow configuration."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ConfigDirectoryLockError(OSError):
    """A config-directory lock operation failed."""


class ConfigDirectoryLockBusyError(ConfigDirectoryLockError):
    """A non-blocking config-directory lock could not be acquired."""


def _lock_error(config_dir: Path, error: OSError) -> ConfigDirectoryLockError:
    return ConfigDirectoryLockError(
        "Configuration directory lock failed at "
        f"{config_dir}: {error.strerror or 'OS error'}"
    )


@contextmanager
def config_directory_lock(
    config_dir: Path, *, exclusive: bool, blocking: bool = True
) -> Iterator[int]:
    try:
        descriptor = os.open(config_dir, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise _lock_error(config_dir, error) from error
    locked = False
    try:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as error:
            raise ConfigDirectoryLockBusyError(
                f"Configuration directory lock is held at {config_dir}"
            ) from error
        except OSError as error:
            raise _lock_error(config_dir, error) from error
        locked = True
        yield descriptor
    finally:
        _release_config_directory_lock(config_dir, descriptor, locked)


def _release_config_directory_lock(config_dir: Path, descriptor: int, locked: bool) -> None:
    release_error: OSError | None = None
    if locked:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as error:
            release_error = error
    try:
        os.close(descriptor)
    except OSError as error:
        if release_error is None:
            release_error = error
    if release_error is not None:
        raise _lock_error(config_dir, release_error) from release_error
