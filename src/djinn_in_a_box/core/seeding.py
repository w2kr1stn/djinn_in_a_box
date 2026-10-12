"""Host-side first-run config seeding."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core.contained_paths import (
    PathContainmentError,
    install_file,
    install_tree,
    open_root,
    remove_entry,
)

SeedKind = Literal["file", "directory"]


class SeedingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SeedEntry:
    source: Path
    target: Path
    kind: SeedKind


WORKFLOW_ROOT_NAMES: tuple[ConfigSyncSource, ...] = ("claude", "codex", "opencode")

CLAUDE_BASELINE_SEEDS: tuple[SeedEntry, ...] = (
    SeedEntry(Path("config/claude/AGENTS.md"), Path("config/claude/AGENTS.md"), "file"),
    SeedEntry(Path("config/claude/settings.json"), Path("config/claude/settings.json"), "file"),
    SeedEntry(Path("config/claude/skills"), Path("config/claude/skills"), "directory"),
    SeedEntry(Path("config/claude/commands"), Path("config/claude/commands"), "directory"),
    SeedEntry(Path("config/claude/agents"), Path("config/claude/agents"), "directory"),
    SeedEntry(Path("config/claude/context"), Path("config/claude/context"), "directory"),
    SeedEntry(Path("config/claude/scripts"), Path("config/claude/scripts"), "directory"),
)

SHARED_SEEDS: tuple[SeedEntry, ...] = (
    SeedEntry(Path("config/mcp-servers.json"), Path("config/mcp-servers.json"), "file"),
    SeedEntry(
        Path("config/agents.toml.example"),
        Path("config/agents.toml.example"),
        "file",
    ),
    SeedEntry(Path("tools.txt"), Path("tools/tools.txt"), "file"),
    SeedEntry(Path("packages.txt"), Path("packages.txt"), "file"),
)

SEED_MANIFEST: tuple[SeedEntry, ...] = CLAUDE_BASELINE_SEEDS + SHARED_SEEDS


def seed_config(
    project_root: Path,
    *,
    source: ConfigSyncSource = "claude",
) -> list[Path]:
    seed_root = project_root / "templates" / "seed"
    if not seed_root.is_dir():
        msg = (
            f"Installation is missing templates/seed at {seed_root}. "
            "Reinstall djinn_in_a_box or clone the repository again, then rerun."
        )
        raise SeedingError(msg)

    claude_root = project_root / "config" / "claude"
    seed_claude_baseline = source == "claude" and workflow_root_is_uninitialized(claude_root)

    created = _ensure_workflow_roots(project_root)
    if seed_claude_baseline:
        created.extend(_seed_claude_baseline(seed_root, claude_root))
    for entry in SHARED_SEEDS:
        seed_source = seed_root / entry.source
        target = project_root / entry.target
        if not _source_exists(seed_source, entry.kind):
            msg = (
                f"Installation is missing seed source {seed_source}. "
                "Reinstall djinn_in_a_box or clone the repository again, then rerun."
            )
            raise SeedingError(msg)

        # is_symlink() covers dangling symlinks, which exists() reports as absent —
        # they must go through wrong-type repair, not a doomed copy.
        if target.exists() or target.is_symlink():
            if _target_has_expected_type(target, entry.kind):
                continue
            _repair_wrong_type(target)

        _copy_seed(seed_source, target, entry.kind)
        created.append(target)

    return created


def _seed_claude_baseline(seed_root: Path, claude_root: Path) -> list[Path]:
    prepared_files: dict[SeedEntry, tuple[bytes, bool]] = {}
    for entry in CLAUDE_BASELINE_SEEDS:
        source = seed_root / entry.source
        if not _source_exists(source, entry.kind):
            msg = (
                f"Installation is missing seed source {source}. "
                "Reinstall djinn_in_a_box or clone the repository again, then rerun."
            )
            raise SeedingError(msg)
        try:
            if entry.kind == "file":
                prepared_files[entry] = (
                    source.read_bytes(), bool(source.stat().st_mode & stat.S_IXUSR)
                )
            else:
                _preflight_seed_tree(source)
        except OSError as exc:
            cause = exc.strerror or str(exc)
            filename = os.fsdecode(exc.filename) if exc.filename is not None else None
            detail = f" Failing path: {filename}." if filename and Path(filename) != source else ""
            msg = (
                f"Cannot read baseline seed source {source}: {cause}.{detail} "
                "Reinstall djinn_in_a_box or clone the repository again, then rerun."
            )
            raise SeedingError(msg) from exc

    target = claude_root
    wrong_type_target: Path | None = None
    try:
        descriptor = open_root(claude_root)
        try:
            created: list[Path] = []
            for entry in CLAUDE_BASELINE_SEEDS:
                target = claude_root / entry.target.name
                try:
                    info = os.stat(target.name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    expected_type = stat.S_ISREG if entry.kind == "file" else stat.S_ISDIR
                    if expected_type(info.st_mode):
                        continue
                    wrong_type_target = target
                    remove_entry(descriptor, target.name)
                    wrong_type_target = None
                if entry.kind == "file":
                    content, executable = prepared_files[entry]
                    install_file(descriptor, target.name, content, executable)
                else:
                    install_tree(descriptor, target.name, seed_root / entry.source)
                created.append(target)
            return created
        finally:
            os.close(descriptor)
    except (PathContainmentError, OSError) as exc:
        if isinstance(exc, PathContainmentError):
            cause = exc.reason
            detail = ""
            remedy = "Move or remove the conflicting path, then rerun."
        else:
            cause = exc.strerror or str(exc)
            filename = os.fsdecode(exc.filename) if exc.filename is not None else None
            if filename:
                filename_path = Path(filename)
                same_target = filename_path == target or (
                    not filename_path.is_absolute() and filename_path == Path(target.name)
                )
                detail = f" Failing path: {filename}." if not same_target else ""
            else:
                detail = ""
            if wrong_type_target is not None:
                remedy = f"Remove it with `sudo rm -rf {wrong_type_target}`, then rerun."
            elif isinstance(exc, PermissionError):
                remedy = (
                    f'Fix ownership with `sudo chown -R "$(id -u):$(id -g)" '
                    f"{claude_root}`, then rerun."
                )
            else:
                remedy = f"Check that {claude_root} is writable, then rerun."
        msg = f"Cannot install baseline seed target {target}: {cause}.{detail} {remedy}"
        raise SeedingError(msg) from exc


def _preflight_seed_tree(source: Path) -> None:
    for entry in sorted(source.iterdir()):
        if entry.name == ".gitkeep":
            continue
        if entry.is_dir():
            _preflight_seed_tree(entry)
        else:
            entry.read_bytes()
            entry.stat()


def pristine_workflow_seed_digest(project_root: Path, relative_path: Path) -> str | None:
    """Return the shipped digest for a known pristine workflow seed file."""
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    if not relative_path.parts or relative_path.parts[0] not in WORKFLOW_ROOT_NAMES:
        return None
    if relative_path.name == ".gitkeep":
        return None

    source = project_root / "templates" / "seed" / "config" / relative_path
    if not source.is_file():
        return None
    return hashlib.sha256(source.read_bytes()).hexdigest()


def workflow_root_is_uninitialized(path: Path) -> bool:
    if path.is_symlink():
        return False
    if not path.exists():
        return True
    if not path.is_dir():
        return True
    return next(path.iterdir(), None) is None
def _ensure_workflow_roots(project_root: Path) -> list[Path]:
    targets = tuple(project_root / "config" / name for name in WORKFLOW_ROOT_NAMES)
    for target in targets:
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            msg = (
                f"Workflow config root {target} must be a real directory. "
                "Move or remove the conflicting path, then rerun."
            )
            raise SeedingError(msg)

    created: list[Path] = []
    for target in targets:
        if target.is_dir():
            continue
        try:
            target.mkdir(parents=True)
        except OSError as e:
            msg = (
                f"Cannot create workflow config root {target}: {e}. "
                f"Check that {target.parent} is writable, then rerun."
            )
            raise SeedingError(msg) from e
        created.append(target)
    return created


def _source_exists(path: Path, kind: SeedKind) -> bool:
    if kind == "file":
        return path.is_file()
    return path.is_dir()


def _target_has_expected_type(path: Path, kind: SeedKind) -> bool:
    if kind == "file":
        return path.is_file()
    return path.is_dir()


def _repair_wrong_type(path: Path) -> None:
    try:
        _remove_path(path)
    except OSError as e:
        msg = (
            f"Cannot repair wrong-type seed target {path}: {e}. "
            f"Remove it with `sudo rm -rf {path}`, then rerun."
        )
        raise SeedingError(msg) from e


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return
    path.unlink()


def _blocking_ancestor(path: Path) -> Path | None:
    # Nearest existing non-directory in the chain — the path a removal remedy
    # must name. None when nothing blocks (the failure is permissions, not type).
    # is_symlink() covers dangling symlinks, which exists() would miss — those
    # must get the removal remedy, not a misdiagnosed permission remedy.
    for candidate in (path, *path.parents):
        if (candidate.exists() or candidate.is_symlink()) and not candidate.is_dir():
            return candidate
    return None


def _existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


def _copy_seed(source: Path, target: Path, kind: SeedKind) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Two distinct causes need two distinct remedies: a non-directory blocking
        # the chain (remove it) vs. a permission failure on an existing ancestor
        # (e.g. root-owned, created by the Docker daemon — chown it). A removal
        # remedy for a nonexistent path would trap the user in a retry loop.
        blocking = _blocking_ancestor(target.parent)
        if blocking is not None:
            msg = (
                f"Cannot create parent directory {target.parent} for seed target "
                f"{target}: {e}. Remove the blocking path with "
                f"`sudo rm -rf {blocking}`, then rerun."
            )
        else:
            owner_hint = _existing_ancestor(target.parent)
            msg = (
                f"Cannot create parent directory {target.parent} for seed target "
                f"{target}: {e}. The directory {owner_hint} is not writable "
                f"(likely created root-owned by the Docker daemon) — fix with "
                f'`sudo chown -R "$(id -u):$(id -g)" {owner_hint}` '
                f"or `sudo rm -rf {owner_hint}`, then rerun."
            )
        raise SeedingError(msg) from e
    # Atomic in both branches: an interrupted copy must not leave a partial
    # target that the cp-if-absent contract would then treat as user content
    # forever. Temp artifacts are cleaned up on failure (the error stays loud).
    tmp = target.parent / f".{target.name}.seed-tmp"
    if kind == "file":
        try:
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return
    shutil.rmtree(tmp, ignore_errors=True)  # stale temp from a previous crash
    try:
        shutil.copytree(source, tmp, ignore=_ignore_gitkeep)
        os.replace(tmp, target)  # target is absent/repaired in this branch
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _ignore_gitkeep(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == ".gitkeep"}
