#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path


def copy_settings(source: Path, destination: Path, *, missing_ok: bool = False) -> bool:
    try:
        content = source.read_bytes()
    except FileNotFoundError:
        return missing_ok
    except OSError:
        return False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".djinn-settings-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
    except OSError:
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="settings-copy")
    parser.add_argument("--copy-settings", nargs=2, type=Path, required=True)
    parser.add_argument("--missing-ok", action="store_true")
    arguments = parser.parse_args(argv)
    source, destination = arguments.copy_settings
    if copy_settings(source, destination, missing_ok=arguments.missing_ok):
        return 0
    print("settings copy failed", file=os.sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
