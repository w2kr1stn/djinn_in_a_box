"""Host detection helpers for first-run configuration prefills."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError, available_timezones

from djinn_in_a_box.config.models import ResourceLimits

LOCALTIME_PATH: Path = Path("/etc/localtime")
MEMINFO_PATH: Path = Path("/proc/meminfo")


def detect_timezone() -> str:
    """Detect the host IANA timezone from /etc/localtime, falling back to UTC."""
    try:
        if not LOCALTIME_PATH.is_symlink():
            return "UTC"

        resolved = LOCALTIME_PATH.resolve(strict=True)
        parts = resolved.parts
        zoneinfo_index = len(parts) - 1 - parts[::-1].index("zoneinfo")
        zone_name = "/".join(parts[zoneinfo_index + 1 :])
        if zone_name in available_timezones():
            return zone_name
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return "UTC"

    return "UTC"


def suggest_resources() -> ResourceLimits:
    """Suggest Docker resource limits from host CPU and memory, falling back to defaults."""
    try:
        meminfo = MEMINFO_PATH.read_text()
        memtotal_line = next(
            (line for line in meminfo.splitlines() if line.startswith("MemTotal:")),
            None,
        )
        if memtotal_line is None:
            return ResourceLimits()
        memtotal_kib = int(memtotal_line.split()[1])
    except (OSError, ValueError, IndexError):
        return ResourceLimits()

    total_gib = memtotal_kib // 1024 // 1024
    # Clamp to the ResourceLimits field bound (le=128) — >256-thread hosts
    # would otherwise make the construction below raise ValidationError.
    cpu_limit = min(128, max(1, (os.cpu_count() or 4) // 2))
    limit_gib = max(2, total_gib // 2)

    return ResourceLimits(
        cpu_limit=cpu_limit,
        memory_limit=f"{limit_gib}G",
        cpu_reservation=max(1, cpu_limit // 4),
        memory_reservation=f"{max(1, limit_gib // 4)}G",
    )
