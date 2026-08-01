"""Volume and sync path backup/restore commands for Djinn in a Box."""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
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
_AGE_HEADER = b"age-encryption.org/v1"
_BACKUP_GLOBS = ("djinn-backup-*.tar.gz", "djinn-backup-*.tar.gz.age")


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


def _publish_archive(archive_path: Path, write_temp: Callable[[Path], None]) -> None:
    """Write an archive into BACKUPS_DIR and atomically publish it."""
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".djinn-backup-", dir=BACKUPS_DIR)
        temp_path = Path(temp_name)
        os.close(fd)
        write_temp(temp_path)
        os.replace(temp_path, archive_path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError as exc:
                warning(f"Failed to clean up temporary backup file: {exc}")


def _age_encrypt_argv(archive_source: Path, temp_path: Path) -> list[str]:
    """Build the age invocation without ever handling a passphrase."""
    return ["age", "--passphrase", "-o", str(temp_path), str(archive_source)]


def _age_decrypt_argv(archive_source: Path, output_path: Path) -> list[str]:
    """Build the age decryption invocation without handling a passphrase."""
    return ["age", "--decrypt", "-o", str(output_path), str(archive_source)]


def _list_backups() -> list[Path]:
    if not BACKUPS_DIR.exists():
        return []
    return sorted(archive for pattern in _BACKUP_GLOBS for archive in BACKUPS_DIR.glob(pattern))


def _remove_stale_temp_archives() -> None:
    for temp_archive in BACKUPS_DIR.glob(".djinn-backup-*"):
        try:
            temp_archive.unlink()
        except OSError as exc:
            warning(f"Failed to remove stale temporary backup {temp_archive.name}: {exc}")


def _has_age_header(archive_path: Path) -> bool:
    with archive_path.open("rb") as archive:
        return archive.read(len(_AGE_HEADER)) == _AGE_HEADER


def _is_plausible_age_archive(archive_path: Path) -> bool:
    try:
        return archive_path.stat().st_size > 0 and _has_age_header(archive_path)
    except OSError:
        return False


def _has_controlling_terminal() -> bool:
    try:
        with open("/dev/tty", encoding="utf-8"):
            pass
    except OSError:
        return False
    return True


def _require_age(*, restore: bool = False) -> None:
    if shutil.which("age") is None:
        if restore:
            error("age is required to restore an encrypted backup. Install age, then retry.")
        else:
            error("age is required for encrypted backups. Install age or use --no-encrypt.")
        raise typer.Exit(1)


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
    no_encrypt: Annotated[
        bool,
        typer.Option(
            "--no-encrypt",
            help="Create an unencrypted backup archive.",
        ),
    ] = False,
) -> None:
    """Back up Docker volumes and sync paths to a single archive.

    Creates a compressed archive covering both named volumes (cache/data categories)
    and sync paths (credentials/repo-dotfiles under ${DJINN_CONFIG_ROOT}).
    The archive is encrypted with an age passphrase and replaces any previous backup.
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
        BACKUPS_DIR.chmod(0o700)
        _remove_stale_temp_archives()
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        archive_path = BACKUPS_DIR / f"djinn-backup-{date_str}.tar.gz"
        staged_archive = staging_dir / archive_path.name

        with tarfile.open(staged_archive, "w:gz") as tar:
            for archive_file in staging_dir.iterdir():
                if archive_file != staged_archive:
                    tar.add(str(archive_file), arcname=archive_file.name)

        if no_encrypt:
            warning("Unencrypted backup can contain credentials.")

            def copy_to_temp(temp_path: Path) -> None:
                shutil.copyfile(staged_archive, temp_path)

            _publish_archive(
                archive_path,
                copy_to_temp,
            )
        else:
            _require_age()
            if not _has_controlling_terminal():
                error(
                    "A controlling terminal is required for the age passphrase prompt. "
                    "Use an interactive terminal or --no-encrypt."
                )
                raise typer.Exit(1)
            warning(
                "age will prompt for a passphrase; empty input generates a passphrase shown once. "
                "After success, an older unencrypted backup is removed."
            )

            def encrypt_to_temp(temp_path: Path) -> None:
                result = subprocess.run(
                    _age_encrypt_argv(staged_archive, temp_path),
                    check=False,
                )
                if result.returncode != 0 or not _is_plausible_age_archive(temp_path):
                    error("Backup encryption failed; the previous backup is unchanged.")
                    raise typer.Exit(1)

            archive_path = Path(f"{archive_path}.age")
            _publish_archive(archive_path, encrypt_to_temp)

        blank()
        success(f"Backup saved: {archive_path}")

        for old in _list_backups():
            if old != archive_path:
                try:
                    old.unlink()
                    if old.suffix != ".age":
                        info(f"Removed unencrypted backup: {old.name}")
                except OSError as exc:
                    warning(f"Failed to remove old backup {old.name}: {exc}")

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

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.chmod(0o700)
    backups = _list_backups()

    if not backups:
        error(f"No backup found in {BACKUPS_DIR}")
        raise typer.Exit(1)

    backup_file = backups[-1]

    try:
        encrypted = _has_age_header(backup_file)
    except OSError as exc:
        error(f"Backup archive cannot be read: {exc}")
        raise typer.Exit(1) from exc

    if backup_file.suffix == ".age" and not encrypted:
        error(
            f"Backup archive {backup_file.name} ends in .age but lacks an age encryption header. "
            "Use an intact age-encrypted archive, then retry."
        )
        raise typer.Exit(1)

    blank()
    info(f"Restoring from: {backup_file.name}")
    blank()

    typer.confirm(
        "This will overwrite current volume and sync path contents. Continue?", abort=True
    )

    staging_dir = Path(tempfile.mkdtemp(prefix="djinn-restore-"))
    decrypt_dir: Path | None = None

    try:
        archive_to_extract = backup_file
        if encrypted:
            _require_age(restore=True)
            if not _has_controlling_terminal():
                error(
                    "A controlling terminal is required for the age passphrase prompt. "
                    "Use an interactive terminal to enter the passphrase, then retry."
                )
                raise typer.Exit(1)
            decrypt_dir = staging_dir / "decrypted"
            decrypt_dir.mkdir(mode=0o700)
            archive_to_extract = decrypt_dir / "backup.tar.gz"
            result = subprocess.run(
                _age_decrypt_argv(backup_file, archive_to_extract),
                check=False,
            )
            if result.returncode != 0:
                error("Backup decryption failed; check the passphrase and archive integrity.")
                raise typer.Exit(1)

        try:
            with tarfile.open(archive_to_extract, "r:gz") as tar:
                tar.extractall(path=staging_dir, filter="data")
        except (tarfile.TarError, EOFError, gzip.BadGzipFile, OSError) as exc:
            error(
                f"Backup is neither a valid age archive nor a readable gzip tar: {exc}. "
                "Use an intact backup archive, then retry."
            )
            raise typer.Exit(1) from exc

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
        if decrypt_dir is not None:
            try:
                shutil.rmtree(decrypt_dir)
            except OSError as exc:
                warning(f"Failed to clean up decrypted backup archive: {exc}")
        try:
            shutil.rmtree(staging_dir)
        except OSError as e:
            warning(f"Failed to clean up staging directory: {e}")
