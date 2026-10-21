"""Read-only Linux flock holder diagnostics, including inherited descriptors."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


def describe_holders(path: str | os.PathLike[str], *, exclusive_only: bool = False) -> str | None:
    """Describe visible fd owners; the lock record's original locker may have exited."""
    try:
        target = os.stat(path)
        boot = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        ticks = os.sysconf("SC_CLK_TCK")
        processes = sorted(Path("/proc").glob("[0-9]*"), key=lambda p: int(p.name))
    except (OSError, ValueError, StopIteration):
        return None

    holders: list[str] = []
    for process in processes:
        try:
            for link in (process / "fd").iterdir():
                try:
                    if not os.path.samestat(target, os.stat(link)):
                        continue
                    lines = (process / "fdinfo" / link.name).read_text().splitlines()
                    if not any(
                        line.startswith("lock:")
                        and "FLOCK" in line.split()
                        and (
                            "WRITE" in line.split()
                            or (not exclusive_only and "READ" in line.split())
                        )
                        for line in lines
                    ):
                        continue
                except OSError:
                    continue
                command = (process / "comm").read_text().removesuffix("\n")
                command = "".join(c if c.isprintable() else "?" for c in command)
                fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
                since = datetime.fromtimestamp(boot + int(fields[19]) / ticks).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                pid = int(process.name)
                holders.append(
                    f"held by PID {pid} ({command}) since {since}; wait for it to finish, "
                    f"or stop exactly that process (kill {pid}), then retry"
                )
                break
        except (OSError, ValueError, IndexError, OverflowError):
            continue
    return "; ".join(holders) or None
