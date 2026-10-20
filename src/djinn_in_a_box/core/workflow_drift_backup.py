"""Admission and durable directory descriptors for workflow drift backups."""

from __future__ import annotations

import os
import pwd
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from djinn_in_a_box.config.declarations import BindDeclaration
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.docker import resolve_zone_roots
from djinn_in_a_box.core.host_sealing import canonical, protected_overlap

_CHAIN = (".local", "state", "djinn", "workflow-drift")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class BackupAdmission:
    home: Path
    path: Path
    protected: tuple[tuple[str, Path], ...]

    def check_overlap(self, path: Path) -> None:
        physical = canonical(path)
        for name, root in self.protected:
            if protected_overlap(physical, root):
                raise ValueError(
                    f"Backup component {path} overlaps {name} {root}. "
                    f"Move or reconfigure {name} outside the workflow backup location."
                )


def _check_directory(path: Path, entry: os.stat_result, *, private: bool) -> None:
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.geteuid():
        raise ValueError(
            f"Backup component {path} must be a real directory owned by you. "
            "Replace symlinks/non-directories and correct its ownership before retrying."
        )
    if private and entry.st_mode & 0o077:
        raise ValueError(
            f"Backup component {path} must be private. Set its mode to 0700 and retry."
        )


def admit_backup_root(project_root: Path, config: AppConfig) -> BackupAdmission:
    """Step 0: inspect existing entries and prospective overlap without creating anything."""
    home = Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
    _check_directory(home, home.lstat(), private=False)
    current = home
    for index, name in enumerate(_CHAIN):
        current /= name
        try:
            entry = current.lstat()
        except FileNotFoundError:
            break
        _check_directory(current, entry, private=index >= 2)
    roots = resolve_zone_roots(config)
    protected = [
        ("Djinn installation", project_root),
        ("config root", roots.config_root),
        ("shared zone root", roots.shared_root),
        ("local zone root", roots.local_root),
        ("code_dir", config.code_dir),
        ("sessions directory", Path.home() / ".djinn" / "sessions"),
    ]
    protected.extend(
        (f"declared bind source {name}", Path(mount.source).expanduser())
        for name, mount in config.mounts.items() if isinstance(mount, BindDeclaration)
    )
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{os.getpid()}"
    admission = BackupAdmission(
        home, home.joinpath(*_CHAIN, stamp),
        tuple((name, canonical(root)) for name, root in protected),
    )
    admission.check_overlap(home.joinpath(*_CHAIN))
    return admission


class BackupDirectories:
    def __init__(self, admission: BackupAdmission, stack: ExitStack) -> None:
        self.admission = admission
        self.stack = stack
        self.chain: list[int] = []
        self.roots: dict[str, int] = {}
        home_fd = os.open(admission.home, _DIRECTORY_FLAGS)
        stack.callback(os.close, home_fd)
        _check_directory(admission.home, os.fstat(home_fd), private=False)
        self.chain.append(home_fd)
        for index, name in enumerate((*_CHAIN, admission.path.name)):
            parent = self.chain[-1]
            if name == admission.path.name:
                admission.check_overlap(self._physical(parent))
            child = self._open_child(parent, name, private=index >= 2, new=index == 4)
            self.chain.append(child)
        admission.check_overlap(self._physical(self.chain[-1]))

    @staticmethod
    def _physical(fd: int) -> Path:
        return Path(os.path.realpath(f"/proc/self/fd/{fd}"))

    def _open_child(self, parent: int, name: str, *, private: bool, new: bool = False) -> int:
        path = self._physical(parent) / name
        try:
            if new:
                # Stamps are never reused, including after a partial failure.
                raise FileNotFoundError
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            path = self._physical(parent) / name
            self.admission.check_overlap(path)
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
        self.stack.callback(os.close, child)
        _check_directory(path, os.fstat(child), private=private)
        if new:
            self.admission.check_overlap(self._physical(child))
        return child

    def root(self, label: str) -> int:
        if label not in self.roots:
            self.roots[label] = self._open_child(self.chain[-1], label, private=True, new=True)
        return self.roots[label]

    def barrier(self) -> None:
        # Include ancestors created by the lifecycle guard, not just our own mkdirs.
        for fd in (*reversed(tuple(self.roots.values())), *reversed(self.chain)):
            os.fsync(fd)


@contextmanager
def open_backup_root(admission: BackupAdmission) -> Iterator[BackupDirectories]:
    with ExitStack() as stack:
        yield BackupDirectories(admission, stack)
