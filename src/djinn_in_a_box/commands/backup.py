"""Volume and sync path backup/restore commands for Djinn in a Box."""

from __future__ import annotations

import re
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from djinn_in_a_box.config.defaults import SYNC_PATHS, VOLUME_CATEGORIES
from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.console import blank, error, info, success, warning
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.docker import (
    backup_sync_path,
    backup_volume,
    extract_sync_path_name,
    get_existing_sync_paths_by_category,
    get_existing_volumes_by_category,
    get_running_containers,
    is_sync_archive,
    restore_sync_path,
    restore_volume,
)
from djinn_in_a_box.core.paths import BACKUPS_DIR

# "cache" excluded: uv-cache/tools-cache/vscode-server are large and rebuildable
DEFAULT_CATEGORIES: list[str] = ["credentials", "repo-dotfiles", "data"]
_VOLUME_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")


def _guard_no_containers_running() -> None:
    running = get_running_containers()
    if running:
        error(f"Containers are running: {', '.join(running)}")
        error("Stop all containers before backup/restore (djinn clean)")
        raise typer.Exit(1)


def _known_categories() -> set[str]:
    return set(VOLUME_CATEGORIES) | set(SYNC_PATHS)


def _collect_items(
    categories: list[str], config: AppConfig | None = None
) -> tuple[list[str], list[Path]]:
    """Return (volume_names, sync_paths) for the given categories."""
    known = _known_categories()
    volumes: list[str] = []
    sync_paths: list[Path] = []
    for cat in categories:
        if cat not in known:
            error(f"Unknown category: '{cat}'. Valid: {', '.join(sorted(known))}")
            raise typer.Exit(1)
        volumes.extend(get_existing_volumes_by_category(cat))
        sync_paths.extend(get_existing_sync_paths_by_category(cat, config))
    return volumes, sync_paths


@handle_config_errors
def backup(
    categories: Annotated[
        list[str] | None,
        typer.Option(
            "--categories",
            "-c",
            help="Categories to backup (default: credentials, repo-dotfiles, data)",
        ),
    ] = None,
) -> None:
    """Back up Docker volumes and sync paths to a single archive.

    Creates a compressed archive covering both named volumes (cache/data categories)
    and sync paths (credentials/repo-dotfiles under ${DJINN_CONFIG_ROOT}).
    The archive is stored in ~/.djinn/backups/ and replaces any previous backup.
    """
    _guard_no_containers_running()

    config = load_config()
    selected = categories or DEFAULT_CATEGORIES
    volumes, sync_paths = _collect_items(selected, config)

    if not volumes and not sync_paths:
        warning("No existing volumes or sync paths found for selected categories")
        raise typer.Exit(0)

    blank()
    info(f"Backing up {len(volumes)} volume(s) and {len(sync_paths)} sync path(s)...")
    blank()

    staging_dir = Path(tempfile.mkdtemp(prefix="djinn-backup-"))
    failed = False

    try:
        for vol in volumes:
            info(f"  {vol}")
            result = backup_volume(vol, staging_dir)
            if result.success:
                success(f"  {vol}")
            else:
                error(f"  {vol}: {result.stderr.strip()}")
                failed = True

        for path in sync_paths:
            label = f"sync/{path.name}"
            info(f"  {label}")
            result = backup_sync_path(path, staging_dir)
            if result.success:
                success(f"  {label}")
            else:
                error(f"  {label}: {result.stderr.strip()}")
                failed = True

        if failed:
            error("Backup aborted due to errors")
            raise typer.Exit(1)

        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        archive_path = BACKUPS_DIR / f"djinn-backup-{date_str}.tar.gz"

        with tarfile.open(archive_path, "w:gz") as tar:
            for archive_file in staging_dir.iterdir():
                tar.add(str(archive_file), arcname=archive_file.name)

        for old in BACKUPS_DIR.glob("djinn-backup-*.tar.gz"):
            if old != archive_path:
                old.unlink()

        blank()
        success(f"Backup saved: {archive_path}")

    finally:
        try:
            shutil.rmtree(staging_dir)
        except OSError as e:
            warning(f"Failed to clean up staging directory: {e}")


@handle_config_errors
def restore() -> None:
    """Restore Docker volumes and sync paths from a backup archive.

    Finds the backup archive in ~/.djinn/backups/ and restores all contained
    volumes and sync paths. Existing contents are overwritten.
    """
    _guard_no_containers_running()
    config = load_config()

    backups = sorted(BACKUPS_DIR.glob("djinn-backup-*.tar.gz")) if BACKUPS_DIR.exists() else []

    if not backups:
        error(f"No backup found in {BACKUPS_DIR}")
        raise typer.Exit(1)

    backup_file = backups[-1]

    blank()
    info(f"Restoring from: {backup_file.name}")
    blank()

    typer.confirm(
        "This will overwrite current volume and sync path contents. Continue?", abort=True
    )

    staging_dir = Path(tempfile.mkdtemp(prefix="djinn-restore-"))

    try:
        with tarfile.open(backup_file, "r:gz") as tar:
            tar.extractall(path=staging_dir, filter="data")

        inner_archives = sorted(staging_dir.glob("*.tar.gz"))

        if not inner_archives:
            error("Backup archive contains no volume archives")
            raise typer.Exit(1)

        failed = False
        for archive in inner_archives:
            if is_sync_archive(archive.name):
                path_name = extract_sync_path_name(archive.name)
                label = f"sync/{path_name}"
                info(f"  {label}")
                result = restore_sync_path(path_name, staging_dir, config)
            else:
                vol_name = archive.name.removesuffix(".tar.gz")
                if not _VOLUME_NAME_RE.fullmatch(vol_name):
                    error(f"  Skipping invalid volume name: {vol_name!r}")
                    failed = True
                    continue
                label = vol_name
                info(f"  {label}")
                result = restore_volume(vol_name, staging_dir)

            if result.success:
                success(f"  {label}")
            else:
                error(f"  {label}: {result.stderr.strip()}")
                failed = True

        blank()
        if failed:
            error("Restore completed with errors")
            raise typer.Exit(1)
        success(f"Restore complete: {len(inner_archives)} item(s)")

    finally:
        try:
            shutil.rmtree(staging_dir)
        except OSError as e:
            warning(f"Failed to clean up staging directory: {e}")
