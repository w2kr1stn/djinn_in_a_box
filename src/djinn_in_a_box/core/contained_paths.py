"""Host writes anchored to opened roots, without following links below them."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from djinn_in_a_box.core.workflow_publisher import (
    DriftClass,
    PublishError,
    _open_child_directory,  # pyright: ignore[reportPrivateUsage]
    _open_root,  # pyright: ignore[reportPrivateUsage]
    _read_at,  # pyright: ignore[reportPrivateUsage]
    _replace_at,  # pyright: ignore[reportPrivateUsage]
    _require_entry_name,  # pyright: ignore[reportPrivateUsage]
)

type RefusalReason = Literal["symlink", "not a directory", "not a regular file", "substituted"]


class PathContainmentError(Exception):
    def __init__(self, path: Path, reason: RefusalReason) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def _reason(parent: int, name: str) -> RefusalReason:
    # Diagnostic only: the no-follow open, never this snapshot, decides admission.
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError:
        return "not a directory"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    return "not a directory"


@contextmanager
def _os_errors(path: Path) -> Iterator[None]:
    try:
        yield
    except OSError as error:
        raise type(error)(error.errno, error.strerror, str(path)) from error


@contextmanager
def _translate(parent: int, name: str, path: Path) -> Iterator[None]:
    try:
        with _os_errors(path):
            yield
    except PublishError as error:
        if error.drift_class is not DriftClass.COLLISION:
            raise
        raise PathContainmentError(path, _reason(parent, name)) from error


def open_root(path: Path) -> int:
    """Open an existing root. The caller owns the descriptor."""
    try:
        descriptor = _open_root(path)
    except PublishError as error:
        if error.drift_class is not DriftClass.COLLISION:
            raise
        with _os_errors(path):
            parent = os.open(path.parent, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                reason = _reason(parent, path.name)
            finally:
                os.close(parent)
        raise PathContainmentError(path, reason) from error
    except OSError as error:
        raise type(error)(error.errno, error.strerror, str(path)) from error
    if descriptor is None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
    return descriptor


def _repair_unreadable_directory(
    path: Path, mode: int, *, parent: int | None = None
) -> int:
    with _os_errors(path):
        name = path if parent is None else path.name
        pin = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            info = os.fstat(pin)
            if not stat.S_ISDIR(info.st_mode):
                reason: RefusalReason = (
                    "symlink" if stat.S_ISLNK(info.st_mode) else "not a directory"
                )
                raise PathContainmentError(path, reason)
            os.chmod(f"/proc/self/fd/{pin}", mode)
            descriptor = (
                open_root(path) if parent is None
                else _open_child_directory(parent, path.name, create=False)
            )
            if descriptor is None:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise PathContainmentError(path, "substituted")
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor
        finally:
            os.close(pin)


def _open_root_to_secure(path: Path, mode: int) -> int:
    try:
        return open_root(path)
    except PermissionError:
        return _repair_unreadable_directory(path, mode)


def secure_root(path: Path, *, mode: int) -> None:
    try:
        with _os_errors(path):
            os.lstat(path)
    except FileNotFoundError:
        path.mkdir(parents=True, mode=mode, exist_ok=True)
    with _os_errors(path):
        descriptor = _open_root_to_secure(path, mode)
        try:
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)


@contextmanager
def _directory(
    root: Path, relative: PurePosixPath, *, create: bool, mode: int, secure: bool = False,
    tighten: bool = False,
) -> Iterator[int]:
    with _os_errors(root):
        descriptor = (
            _open_root_to_secure(root, mode) if secure or (tighten and not relative.parts)
            else open_root(root)
        )
    path = root
    try:
        if relative.is_absolute():
            with _translate(descriptor, str(relative), root / relative):
                _require_entry_name(str(relative))
        for part in relative.parts:
            path /= part
            with _translate(descriptor, part, path):
                _require_entry_name(part)
                try:
                    child = _open_child_directory(descriptor, part, create=create, mode=mode)
                except PermissionError:
                    if not (secure or (tighten and path == root / relative)):
                        raise
                    child = _repair_unreadable_directory(path, mode, parent=descriptor)
            if child is None:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
            with _os_errors(path):
                os.close(descriptor)
            descriptor = child
            if secure:
                with _os_errors(path):
                    os.fchmod(descriptor, mode)
        yield descriptor
    finally:
        with _os_errors(path):
            os.close(descriptor)


def ensure_directory(root: Path, relative: PurePosixPath, *, mode: int, secure: bool) -> None:
    with _directory(root, relative, create=True, mode=mode, secure=secure):
        pass


def ensure_empty_file(
    root: Path, relative: PurePosixPath, *, directory_mode: int, file_mode: int
) -> None:
    with _directory(root, relative.parent, create=True, mode=directory_mode) as parent:
        with _translate(parent, relative.name, root / relative):
            _require_entry_name(relative.name)
        with _os_errors(root / relative):
            try:
                descriptor = os.open(
                    relative.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    file_mode,
                    dir_fd=parent,
                )
            except FileExistsError:
                info = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    reason: RefusalReason = (
                        "symlink" if stat.S_ISLNK(info.st_mode) else "not a regular file"
                    )
                    raise PathContainmentError(root / relative, reason) from None
            else:
                os.close(descriptor)


def tighten_directory(root: Path, relative: PurePosixPath, *, mode: int) -> None:
    with (
        _directory(root, relative, create=False, mode=mode, tighten=True) as descriptor,
        _os_errors(root / relative),
    ):
        os.fchmod(descriptor, mode)


def _entry_path(parent: int, name: str) -> Path:
    return Path(os.readlink(f"/proc/self/fd/{parent}")) / name


@dataclass
class _RemovalFrame:
    parent: int
    name: str
    path: Path
    directory: int
    children: Iterator[str]
    preserved: bool = False


def remove_entry(parent_fd: int, name: str) -> bool:
    """Remove every child; False means only empty placeholders survive."""
    stack: list[_RemovalFrame] = []
    parent = parent_fd
    try:
        while True:
            path = _entry_path(parent, name)
            with _translate(parent, name, path):
                _require_entry_name(name)
            removed = True
            directory = None
            try:
                with _translate(parent, name, path):
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        os.unlink(name, dir_fd=parent)
                    else:
                        directory = _open_child_directory(parent, name, create=False)
            except FileNotFoundError:
                pass
            if directory is not None:
                frame = _RemovalFrame(parent, name, path, directory, iter(()))
                stack.append(frame)
                with _os_errors(path):
                    frame.children = iter(sorted(os.listdir(directory)))

            while stack:
                frame = stack[-1]
                child = next(frame.children, None)
                if child is not None:
                    parent, name = frame.directory, child
                    break
                stack.pop()
                with _os_errors(frame.path):
                    os.close(frame.directory)
                removed = not frame.preserved
                if removed:
                    try:
                        with _os_errors(frame.path):
                            os.rmdir(frame.name, dir_fd=frame.parent)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        if error.errno not in {errno.EACCES, errno.EBUSY, errno.EPERM}:
                            raise
                        with _translate(frame.parent, frame.name, frame.path):
                            directory = _open_child_directory(
                                frame.parent, frame.name, create=False
                            )
                        if directory is not None:
                            try:
                                with _os_errors(frame.path):
                                    if os.listdir(directory):
                                        raise error
                            finally:
                                with _os_errors(frame.path):
                                    os.close(directory)
                            removed = False
                if not removed and stack:
                    stack[-1].preserved = True
            else:
                return removed
    finally:
        for frame in reversed(stack):
            with _os_errors(frame.path):
                os.close(frame.directory)


def clear_directory(root: Path, keep: frozenset[str] = frozenset()) -> None:
    descriptor = open_root(root)
    try:
        with _os_errors(root):
            names = sorted(os.listdir(descriptor))
        for name in names:
            if name not in keep:
                remove_entry(descriptor, name)
    finally:
        with _os_errors(root):
            os.close(descriptor)


def install_file(parent_fd: int, name: str, content: bytes, executable: bool) -> None:
    path = _entry_path(parent_fd, name)
    with _translate(parent_fd, name, path):
        _replace_at(parent_fd, name, content, executable)
    try:
        committed = _read_at(parent_fd, name)
    except PublishError as error:
        if error.drift_class is not DriftClass.COLLISION:
            if isinstance(error.__cause__, OSError):
                cause = error.__cause__
                raise type(cause)(cause.errno, cause.strerror, str(path)) from cause
            raise PathContainmentError(path, "substituted") from error
        reason: RefusalReason = (
            "symlink" if _reason(parent_fd, name) == "symlink" else "not a regular file"
        )
        raise PathContainmentError(path, reason) from error
    if committed is None:
        raise PathContainmentError(path, "not a regular file")
    if committed.content != content or committed.executable != executable:
        raise PathContainmentError(path, "substituted")


def _populate(directory: int, source: Path) -> None:
    for entry in sorted(source.iterdir()):
        if entry.name == ".gitkeep":
            continue
        if entry.is_dir():
            with _translate(directory, entry.name, _entry_path(directory, entry.name)):
                child = _open_child_directory(directory, entry.name, create=True)
            assert child is not None
            try:
                _populate(child, entry)
            finally:
                with _os_errors(_entry_path(directory, entry.name)):
                    os.close(child)
        else:
            install_file(
                directory, entry.name, entry.read_bytes(), bool(entry.stat().st_mode & stat.S_IXUSR)
            )


def install_tree(parent_fd: int, name: str, source: Path) -> None:
    path = _entry_path(parent_fd, name)
    with _translate(parent_fd, name, path):
        _require_entry_name(name)
    while True:
        with _os_errors(path):
            temporary = ".djinn-seed-" + os.urandom(8).hex()
        try:
            with _os_errors(_entry_path(parent_fd, temporary)):
                os.mkdir(temporary, dir_fd=parent_fd)
        except FileExistsError:
            continue
        break
    try:
        with _translate(parent_fd, temporary, _entry_path(parent_fd, temporary)):
            directory = _open_child_directory(parent_fd, temporary, create=False)
        if directory is None:
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), str(_entry_path(parent_fd, temporary))
            )
        try:
            _populate(directory, source)
            with _os_errors(path):
                os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                populated = os.fstat(directory)
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
                populated.st_dev, populated.st_ino
            ):
                reason: RefusalReason = (
                    "symlink" if stat.S_ISLNK(info.st_mode)
                    else "substituted" if stat.S_ISDIR(info.st_mode)
                    else "not a directory"
                )
                raise PathContainmentError(_entry_path(parent_fd, name), reason)
        finally:
            with _os_errors(path):
                os.close(directory)
    except BaseException as error:
        try:
            remove_entry(parent_fd, temporary)
        except FileNotFoundError:
            pass
        except Exception as cleanup_error:
            error.add_note(f"Could not remove temporary seed tree: {cleanup_error}")
        raise
