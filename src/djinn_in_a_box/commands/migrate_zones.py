"""Explicit, locked migration of agent config paths into their assigned zones."""

from __future__ import annotations

import typer

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.zones import load_zone_assignments
from djinn_in_a_box.core.config_lock import (
    ConfigDirectoryLockBusyError,
    config_directory_lock,
)
from djinn_in_a_box.core.console import error, info, success
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.docker import ensure_zone_roots, get_running_containers
from djinn_in_a_box.core.zone_migration import reconcile_zone_assignments


def _guard_no_containers_running() -> None:
    running = get_running_containers()
    if running is None:
        error("Could not determine whether Djinn containers are running.")
        error("Restore Docker access before migrating zones, then retry.")
        raise typer.Exit(1)
    if running:
        error(f"Containers are running: {', '.join(running)}")
        error("Stop all containers before migrating zones (djinn clean)")
        raise typer.Exit(1)


@handle_config_errors
def migrate_zones() -> None:
    """Move assigned agent paths out of the backed-up config root."""
    config = load_config()
    roots = ensure_zone_roots(config)
    try:
        with config_directory_lock(roots.local_root, exclusive=True, blocking=False):
            _guard_no_containers_running()
            assignments = load_zone_assignments(config)
            typer.confirm(
                "Migration removes paths from the config root, which your mirroring layer "
                "will see as deletions. Add the shared root to mirroring (keep the local "
                "root out), or pause mirroring before continuing. Continue?",
                abort=True,
            )
            result = reconcile_zone_assignments(
                config,
                assignments=assignments,
                before_publish=_guard_no_containers_running,
            )
    except ConfigDirectoryLockBusyError:
        error(f"Zone migration is already in progress at {roots.local_root}. Wait, then retry.")
        raise typer.Exit(1) from None

    if result.collisions:
        for collision in result.collisions:
            locations = ", ".join(str(path) for path in collision.populated_paths)
            error(
                "Unresolved zone collision; no paths were moved: "
                f"{locations}. Run `djinn doctor` to inspect the copies."
            )
        raise typer.Exit(1)
    if not result.moves:
        success("Zone migration is already complete.")
        return
    for move in result.moves:
        method = "copied and published" if move.copied_across_filesystems else "renamed"
        info(f"{method}: {move.source} -> {move.destination}")
    success(f"Zone migration complete: {len(result.moves)} path(s) moved.")
