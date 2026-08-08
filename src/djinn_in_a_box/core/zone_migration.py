"""Migration and reconciliation for assigned agent config zones."""

from __future__ import annotations

import errno
import filecmp
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import ZoneAssignment, ZoneAssignments, load_zone_assignments
from djinn_in_a_box.core.docker import ZoneRoots, ensure_zone_roots, resolve_zone_roots
from djinn_in_a_box.core.exceptions import ZoneConfigurationError

type BeforePublish = Callable[[], None]


@dataclass(frozen=True)
class ZoneCollision:
    assignment: ZoneAssignment
    destination: Path
    populated_paths: tuple[Path, ...]


@dataclass(frozen=True)
class ZoneMove:
    assignment: ZoneAssignment
    source: Path
    destination: Path
    copied_across_filesystems: bool


@dataclass(frozen=True)
class ZoneReconciliation:
    moves: tuple[ZoneMove, ...]
    collisions: tuple[ZoneCollision, ...]


def path_has_content(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        msg = f"Cannot inspect zone path {path}: {error}"
        raise ZoneConfigurationError(msg) from error
    if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
        return True
    if not stat.S_ISDIR(info.st_mode):
        return False
    try:
        return next(path.iterdir(), None) is not None
    except OSError as error:
        msg = f"Cannot inspect zone path {path}: {error}"
        raise ZoneConfigurationError(msg) from error


def find_unmigrated_assignments(
    assignments: ZoneAssignments, roots: ZoneRoots
) -> tuple[ZoneAssignment, ...]:
    return tuple(
        assignment
        for assignment in _iter_assignments(assignments)
        if path_has_content(roots.config_root / assignment.agent / assignment.relative_path)
    )


def find_zone_collisions(
    assignments: ZoneAssignments, roots: ZoneRoots
) -> tuple[ZoneCollision, ...]:
    collisions: list[ZoneCollision] = []
    for assignment in _iter_assignments(assignments):
        destination = _zone_path(roots, assignment.zone, assignment)
        populated_paths = tuple(
            path for path in _assignment_paths(roots, assignment) if path_has_content(path)
        )
        if len(populated_paths) > 1:
            collisions.append(ZoneCollision(assignment, destination, populated_paths))
    return tuple(collisions)


def reconcile_zone_assignments(
    config: AppConfig | None = None,
    *,
    assignments: ZoneAssignments | None = None,
    before_publish: BeforePublish | None = None,
    stop_on_collision: bool = True,
) -> ZoneReconciliation:
    roots = ensure_zone_roots(config)
    resolved_assignments = assignments or load_zone_assignments(config)
    collisions = find_zone_collisions(resolved_assignments, roots)
    if collisions and stop_on_collision:
        return ZoneReconciliation((), collisions)

    moves: list[ZoneMove] = []
    for assignment in _iter_assignments(resolved_assignments):
        if any(collision.assignment == assignment for collision in collisions):
            continue
        destination = _zone_path(roots, assignment.zone, assignment)
        _ensure_directory(_zone_root(roots, assignment.zone), destination)
        source = next(
            (
                path
                for path in _assignment_paths(roots, assignment)
                if path != destination and path_has_content(path)
            ),
            None,
        )
        if source is None:
            _remove_empty_source_directories(roots, assignment, destination)
            continue
        moves.append(
            ZoneMove(
                assignment,
                source,
                destination,
                _move_directory(source, destination, before_publish),
            )
        )
    return ZoneReconciliation(tuple(moves), collisions)


def adopt_archive_collision(
    collision: ZoneCollision,
    config: AppConfig | None = None,
    *,
    before_publish: BeforePublish | None = None,
) -> Path:
    roots = resolve_zone_roots(config)
    config_path = (
        roots.config_root / collision.assignment.agent / collision.assignment.relative_path
    )
    destination = collision.destination
    if (
        collision.destination not in collision.populated_paths
        or config_path not in collision.populated_paths
        or len(collision.populated_paths) != 2
    ):
        msg = (
            "Archive adoption requires exactly the config-root and destination zone "
            f"trees to be populated for {collision.assignment.agent}/"
            f"{collision.assignment.relative_path}"
        )
        raise ZoneConfigurationError(msg)

    displaced = _timestamped_sibling(destination)
    os.replace(destination, displaced)
    try:
        _ensure_directory(_zone_root(roots, collision.assignment.zone), destination)
        _move_directory(config_path, destination, before_publish)
    except (OSError, ZoneConfigurationError):
        os.replace(displaced, destination)
        raise
    return displaced


def _iter_assignments(assignments: ZoneAssignments) -> Iterable[ZoneAssignment]:
    for agent, by_zone in assignments.by_agent.items():
        for zone in ("local", "shared"):
            for relative_path in by_zone[zone]:
                yield ZoneAssignment(agent, zone, relative_path)


def _zone_path(roots: ZoneRoots, zone: str, assignment: ZoneAssignment) -> Path:
    return _zone_root(roots, zone) / assignment.agent / assignment.relative_path


def _zone_root(roots: ZoneRoots, zone: str) -> Path:
    if zone == "local":
        return roots.local_root
    if zone == "shared":
        return roots.shared_root
    msg = f"Unsupported zone: {zone}"
    raise ZoneConfigurationError(msg)


def _assignment_paths(roots: ZoneRoots, assignment: ZoneAssignment) -> tuple[Path, ...]:
    suffix = Path(assignment.agent) / assignment.relative_path
    roots_by_zone = (roots.config_root, roots.shared_root, roots.local_root)
    paths = tuple(root / suffix for root in roots_by_zone)
    for root, path in zip(roots_by_zone, paths, strict=True):
        _reject_symlinked_components(root, path)
    return paths


def _reject_symlinked_components(root: Path, path: Path) -> None:
    current = root
    if current.is_symlink():
        msg = f"Zone migration source has a symlinked component: {current}"
        raise ZoneConfigurationError(msg)
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            msg = f"Zone migration source has a symlinked component: {current}"
            raise ZoneConfigurationError(msg)


def _ensure_directory(root: Path, path: Path) -> None:
    _ensure_private_directory(root)
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        _ensure_private_directory(current)


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        msg = f"Zone directory is not a directory: {path}"
        raise ZoneConfigurationError(msg)
    path.mkdir(exist_ok=True, mode=0o700)
    path.chmod(0o700)


def ensure_zone_targets(assignments: ZoneAssignments, roots: ZoneRoots) -> None:
    for assignment in _iter_assignments(assignments):
        destination = _zone_path(roots, assignment.zone, assignment)
        _ensure_directory(_zone_root(roots, assignment.zone), destination)


def _remove_empty_source_directories(
    roots: ZoneRoots, assignment: ZoneAssignment, destination: Path
) -> None:
    for path in _assignment_paths(roots, assignment):
        if (
            path != destination
            and path.is_dir()
            and not path.is_symlink()
            and not path_has_content(path)
        ):
            path.rmdir()


def _move_directory(source: Path, destination: Path, before_publish: BeforePublish | None) -> bool:
    if not source.is_dir() or source.is_symlink():
        msg = f"Zone migration source is not a directory: {source}"
        raise ZoneConfigurationError(msg)
    if not destination.is_dir() or path_has_content(destination):
        msg = f"Zone migration destination is not empty: {destination}"
        raise ZoneConfigurationError(msg)
    if before_publish is not None:
        before_publish()
    try:
        os.replace(source, destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        _copy_then_publish(source, destination, before_publish)
        return True
    _harden_published_directories(destination)
    return False


def _copy_then_publish(
    source: Path, destination: Path, before_publish: BeforePublish | None
) -> None:
    staging_parent = Path(tempfile.mkdtemp(prefix=".djinn-zone-migrate-", dir=destination.parent))
    staged_tree = staging_parent / "tree"
    try:
        shutil.copytree(source, staged_tree, symlinks=True)
        if not _trees_match(source, staged_tree):
            msg = f"Zone migration verification failed for {source}"
            raise ZoneConfigurationError(msg)
        if before_publish is not None:
            before_publish()
        os.replace(staged_tree, destination)
        _harden_published_directories(destination)
        shutil.rmtree(source)
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def _trees_match(source: Path, copied: Path) -> bool:
    source_entries = {entry.name: entry for entry in os.scandir(source)}
    copied_entries = {entry.name: entry for entry in os.scandir(copied)}
    if source_entries.keys() != copied_entries.keys():
        return False
    for name, source_entry in source_entries.items():
        copied_entry = copied_entries[name]
        source_path = Path(source_entry.path)
        copied_path = Path(copied_entry.path)
        if source_entry.is_symlink() or copied_entry.is_symlink():
            if not source_entry.is_symlink() or not copied_entry.is_symlink():
                return False
            if os.readlink(source_path) != os.readlink(copied_path):
                return False
        elif source_entry.is_dir(follow_symlinks=False):
            if not copied_entry.is_dir(follow_symlinks=False) or not _trees_match(
                source_path, copied_path
            ):
                return False
        elif not copied_entry.is_file(follow_symlinks=False) or not filecmp.cmp(
            source_path, copied_path, shallow=False
        ):
            return False
    return True


def _harden_published_directories(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        msg = f"Published zone path is not a directory: {path}"
        raise ZoneConfigurationError(msg)
    os.chmod(path, 0o700, follow_symlinks=False)
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                _harden_published_directories(Path(entry.path))


def _timestamped_sibling(path: Path) -> Path:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.pre-restore-{timestamp}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.pre-restore-{timestamp}-{index}")
        index += 1
    return candidate
