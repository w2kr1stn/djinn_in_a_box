#!/usr/bin/env python3
"""Make empty declared-volume roots writable without changing existing data."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def repair_targets(encoded: str) -> None:
    try:
        targets = json.loads(encoded)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "DJINN_DECLARED_VOLUME_TARGETS must be a JSON array of absolute paths"
        ) from exc
    if not isinstance(targets, list) or any(
        not isinstance(target, str) or not Path(target).is_absolute() or "\x00" in target
        for target in targets
    ):
        raise ValueError("DJINN_DECLARED_VOLUME_TARGETS must be a JSON array of absolute paths")
    for target in targets:
        root = Path(target)
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"Declared volume root '{target}' must be a directory, not a symlink")
        if os.access(root, os.W_OK | os.X_OK):
            continue
        try:
            with os.scandir(root) as entries:
                populated = next(entries, None) is not None
        except PermissionError:
            listing = subprocess.run(
                ["sudo", "ls", "-A", "--", target],
                check=True,
                capture_output=True,
            )
            populated = bool(listing.stdout)
        if populated:
            print(
                f"Warning: declared volume '{target}' is not writable and contains data; "
                "repair its ownership manually before using it.",
                file=sys.stderr,
            )
        else:
            subprocess.run(
                ["sudo", "chown", "-h", f"{os.getuid()}:{os.getgid()}", "--", target],
                check=True,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", required=True)
    args = parser.parse_args()
    try:
        repair_targets(args.targets)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Declared volume ownership repair failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
