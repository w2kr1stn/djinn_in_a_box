"""Shared migration locks and refusal messages for zone-sensitive commands."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import typer

from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import load_zone_assignments
from djinn_in_a_box.core.config_lock import (
    ConfigDirectoryLockBusyError,
    config_directory_lock,
)
from djinn_in_a_box.core.console import error
from djinn_in_a_box.core.docker import ensure_zone_roots
from djinn_in_a_box.core.zone_migration import (
    ensure_zone_targets,
    find_unmigrated_assignments,
    find_zone_collisions,
)

type GatedCommand = Literal["start", "run", "backup", "restore", "clean"]


@contextmanager
def zone_command_gate(
    config: AppConfig | None, command: GatedCommand, *, exclusive: bool = False
) -> Iterator[None]:
    roots = ensure_zone_roots(config)
    try:
        with config_directory_lock(roots.local_root, exclusive=exclusive, blocking=False):
            assignments = load_zone_assignments(config)
            if command in {"start", "run"}:
                ensure_zone_targets(assignments, roots)
            collisions = find_zone_collisions(assignments, roots)
            if collisions:
                _refuse_collision(collisions[0].populated_paths, collisions[0].destination)
            unmigrated = find_unmigrated_assignments(assignments, roots)
            if unmigrated and command in {"start", "run"}:
                _refuse_unmigrated(unmigrated[0].agent, unmigrated[0].relative_path)
            yield
    except ConfigDirectoryLockBusyError:
        error(
            "Zone migration is in progress at "
            f"{roots.local_root}. Wait for it to finish, then retry."
        )
        raise typer.Exit(1) from None


def _refuse_unmigrated(agent: str, relative_path: Path) -> None:
    error(
        f"Zone data remains in the config root at {agent}/{relative_path}. "
        "Run `djinn migrate-zones` before starting an agent."
    )
    raise typer.Exit(1)


def _refuse_collision(paths: tuple[Path, ...], destination: Path) -> None:
    locations = ", ".join(str(path) for path in paths)
    error(
        "Unresolved zone collision between "
        f"{locations} (destination: {destination}). Run `djinn doctor` to inspect and "
        "resolve the copies manually."
    )
    raise typer.Exit(1)
