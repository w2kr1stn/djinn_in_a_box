"""Small Linux primitives for lossless, descriptor-relative file mutation."""

from __future__ import annotations

import ctypes
import errno
import os
from functools import cache
from typing import Any

_RENAME_NOREPLACE = 1


@cache
def _renameat2() -> Any | None:
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        return None
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    return function


def rename_noreplace(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    """Atomically rename without replacing an existing destination name."""
    function = _renameat2()
    if function is None:
        raise OSError(errno.ENOSYS, "renameat2 is required for safe publication")
    result = function(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), destination_name)
    raise OSError(error_number, os.strerror(error_number), destination_name)
