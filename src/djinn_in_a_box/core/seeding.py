"""Host-side first-run config seeding."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SeedKind = Literal["file", "directory"]


class SeedingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SeedEntry:
    source: Path
    target: Path
    kind: SeedKind


SEED_MANIFEST: tuple[SeedEntry, ...] = (
    SeedEntry(Path("config/claude/CLAUDE.md"), Path("config/claude/CLAUDE.md"), "file"),
    SeedEntry(Path("config/claude/settings.json"), Path("config/claude/settings.json"), "file"),
    SeedEntry(Path("config/claude/skills"), Path("config/claude/skills"), "directory"),
    SeedEntry(Path("config/claude/commands"), Path("config/claude/commands"), "directory"),
    SeedEntry(Path("config/claude/agents"), Path("config/claude/agents"), "directory"),
    SeedEntry(Path("config/claude/context"), Path("config/claude/context"), "directory"),
    SeedEntry(Path("config/claude/scripts"), Path("config/claude/scripts"), "directory"),
    SeedEntry(Path("config/gemini"), Path("config/gemini"), "directory"),
    SeedEntry(Path("config/opencode"), Path("config/opencode"), "directory"),
    SeedEntry(Path("config/mcp-servers.json"), Path("config/mcp-servers.json"), "file"),
    SeedEntry(
        Path("config/agents.toml.example"),
        Path("config/agents.toml.example"),
        "file",
    ),
    SeedEntry(Path("tools.txt"), Path("tools/tools.txt"), "file"),
    SeedEntry(Path("packages.txt"), Path("packages.txt"), "file"),
)


def seed_config(project_root: Path) -> list[Path]:
    seed_root = project_root / "templates" / "seed"
    if not seed_root.is_dir():
        msg = (
            f"Installation is missing templates/seed at {seed_root}. "
            "Reinstall djinn_in_a_box or clone the repository again, then rerun."
        )
        raise SeedingError(msg)

    created: list[Path] = []
    for entry in SEED_MANIFEST:
        source = seed_root / entry.source
        target = project_root / entry.target
        if not _source_exists(source, entry.kind):
            msg = (
                f"Installation is missing seed source {source}. "
                "Reinstall djinn_in_a_box or clone the repository again, then rerun."
            )
            raise SeedingError(msg)

        # is_symlink() covers dangling symlinks, which exists() reports as absent —
        # they must go through wrong-type repair, not a doomed copy.
        if target.exists() or target.is_symlink():
            if _target_has_expected_type(target, entry.kind):
                continue
            _repair_wrong_type(target)

        _copy_seed(source, target, entry.kind)
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
