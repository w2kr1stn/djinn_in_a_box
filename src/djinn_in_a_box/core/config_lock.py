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
    """Lock an existing config directory without creating lock artifacts."""
    try:
        descriptor = _open_real_directory(config_dir)
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


def directory_is_attached(path: Path, descriptor: int) -> bool:
    """Return whether ``path`` still resolves to the pinned directory descriptor."""
    try:
        current = _open_real_directory(path)
    except OSError:
        return False
    try:
        expected_stat = os.fstat(descriptor)
        current_stat = os.fstat(current)
        return (current_stat.st_dev, current_stat.st_ino) == (
            expected_stat.st_dev,
            expected_stat.st_ino,
        )
    finally:
        os.close(current)


def _open_real_directory(path: Path) -> int:
    """Open every absolute path component without following symbolic links."""
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise
