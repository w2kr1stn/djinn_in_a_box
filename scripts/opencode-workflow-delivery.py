#!/usr/bin/env python3
"""Safely deliver the canonical OpenCode workflow seed into its runtime directory."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

MANIFEST_NAME = ".djinn-workflow-delivery.json"
_SCHEMA_VERSION = 1
_TOOL = "opencode"
_IGNORED_SOURCE_ROOT_FILES = frozenset({".opencode.json", ".seed-manifest"})
_PLUGIN_FILES = frozenset(
    {
        "plugins/session-start-status.js",
        "plugins/security-reminder.js",
        "plugins/ready-notify.js",
    }
)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_HEX = frozenset("0123456789abcdef")
_RENAME_NOREPLACE = 1


@dataclass(frozen=True, slots=True)
class _FileState:
    content_hash: str
    executable: bool


@dataclass(frozen=True, slots=True)
class _SourceFile:
    content: bytes
    executable: bool

    @property
    def state(self) -> _FileState:
        return _FileState(_digest(self.content), self.executable)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    content: bytes
    executable: bool

    @property
    def state(self) -> _FileState:
        return _FileState(_digest(self.content), self.executable)


@dataclass(frozen=True, slots=True)
class _Manifest:
    files: Mapping[PurePosixPath, _FileState]


@dataclass(frozen=True, slots=True)
class _StagedFile:
    relative_path: PurePosixPath
    device: int
    inode: int
    state: _FileState


@dataclass(frozen=True, slots=True)
class _StageEntry:
    device: int
    inode: int
    directory: bool


@dataclass(slots=True)
class _Stage:
    name: str
    descriptor: int
    device: int
    inode: int
    entries: dict[PurePosixPath, _StageEntry]


@dataclass(frozen=True, slots=True)
class _TrackedSourceEntry:
    path: PurePosixPath
    generation: tuple[int, int, int, int, int, int]


class _DeliveryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class _ManifestError(ValueError):
    pass


class _MissingParentError(FileNotFoundError):
    pass


def _owned(path: PurePosixPath) -> bool:
    parts = path.parts
    value = path.as_posix()
    return (
        value in {"AGENTS.md", "CLAUDE.md"}
        or (len(parts) == 2 and parts[0] == "agents" and path.suffix == ".md")
        or (len(parts) >= 3 and parts[0] == "skills" and _NAME.fullmatch(parts[1]) is not None)
        or (len(parts) == 2 and parts[0] == "commands" and path.suffix == ".md")
        or (len(parts) >= 2 and parts[0] in {"context", "scripts"})
        or value in _PLUGIN_FILES
    )


def _safe_relative(path: PurePosixPath) -> bool:
    return (
        not path.is_absolute()
        and bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _real_directory(path: Path, code: str) -> os.stat_result:
    try:
        result = os.lstat(path)
    except OSError as error:
        raise _DeliveryError(code) from error
    if not stat.S_ISDIR(result.st_mode) or stat.S_ISLNK(result.st_mode):
        raise _DeliveryError(code)
    return result


def _read_source(source: Path) -> dict[PurePosixPath, _SourceFile]:
    source_stat = _real_directory(source, "source-root-unsafe")
    try:
        source_real = source.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _DeliveryError("source-root-race", retryable=True) from error
    files: dict[PurePosixPath, _SourceFile] = {}
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise _DeliveryError("source-root-race", retryable=True) from error
    tracked_directories: list[tuple[int, tuple[int, int, int, int]]] = []
    tracked_entries: list[_TrackedSourceEntry] = []
    try:
        opened_stat = os.fstat(source_fd)
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            source_stat.st_dev,
            source_stat.st_ino,
        ):
            raise _DeliveryError("source-root-race", retryable=True)

        def traversal_error(error: OSError) -> None:
            raise _DeliveryError("source-traversal-race", retryable=True) from error

        for directory, directory_names, file_names, directory_fd in os.fwalk(
            ".",
            topdown=True,
            onerror=traversal_error,
            follow_symlinks=False,
            dir_fd=source_fd,
        ):
            relative_directory = PurePosixPath(directory)
            pinned_directory_fd = os.dup(directory_fd)
            generation = _directory_generation(os.fstat(pinned_directory_fd))
            tracked_directories.append((pinned_directory_fd, generation))
            try:
                current_names = set(os.listdir(directory_fd))
            except OSError as error:
                raise _DeliveryError("source-subtree-race", retryable=True) from error
            if current_names != set(directory_names) | set(file_names):
                raise _DeliveryError("source-subtree-race", retryable=True)
            retained_directories: list[str] = []
            for name in directory_names:
                child_relative = relative_directory / name
                if not _directory_may_contain_owned_files(child_relative):
                    continue
                try:
                    child_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as error:
                    raise _DeliveryError("source-traversal-race", retryable=True) from error
                if stat.S_ISLNK(child_stat.st_mode):
                    raise _DeliveryError("source-directory-symlink")
                if not stat.S_ISDIR(child_stat.st_mode):
                    raise _DeliveryError("source-directory-type-unsafe")
                retained_directories.append(name)
            directory_names[:] = retained_directories
            for name in file_names:
                relative = relative_directory / name
                if len(relative.parts) == 1 and relative.name in _IGNORED_SOURCE_ROOT_FILES:
                    continue
                if not _safe_relative(relative) or not _owned(relative):
                    continue
                files[relative] = _read_owned_source_file(
                    source_fd,
                    source_real,
                    relative,
                    directory_fd,
                    tracked_entries,
                )
        _validate_tracked_source_entries(source_fd, tracked_entries)
        for pinned_directory_fd, generation in tracked_directories:
            if _directory_generation(os.fstat(pinned_directory_fd)) != generation:
                raise _DeliveryError("source-subtree-race", retryable=True)
        try:
            current_source_stat = os.lstat(source)
        except OSError as error:
            raise _DeliveryError("source-root-race", retryable=True) from error
        if (current_source_stat.st_dev, current_source_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ) or not stat.S_ISDIR(current_source_stat.st_mode):
            raise _DeliveryError("source-root-race", retryable=True)
    finally:
        for pinned_directory_fd, _ in tracked_directories:
            os.close(pinned_directory_fd)
        os.close(source_fd)

    if PurePosixPath("AGENTS.md") not in files:
        raise _DeliveryError("source-agents-missing")
    return files


@contextmanager
def _source_parent_fd(root_fd: int, path: PurePosixPath) -> Iterator[int]:
    descriptor = os.dup(root_fd)
    try:
        for part in path.parts[:-1]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise _DeliveryError("source-parent-race", retryable=True) from error
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_owned_source_file(
    root_fd: int,
    source_real: Path,
    path: PurePosixPath,
    entry_parent_fd: int,
    tracked_entries: list[_TrackedSourceEntry],
) -> _SourceFile:
    try:
        first_stat = os.stat(path.name, dir_fd=entry_parent_fd, follow_symlinks=False)
        if stat.S_ISREG(first_stat.st_mode):
            return _read_regular_source_entry(
                entry_parent_fd,
                path.name,
                first_stat,
                tracked_entries,
                path,
            )
        if not stat.S_ISLNK(first_stat.st_mode):
            raise _DeliveryError("source-file-type-unsafe")
        link_target = Path(os.readlink(path.name, dir_fd=entry_parent_fd))
        unresolved = (
            link_target
            if link_target.is_absolute()
            else source_real.joinpath(*path.parent.parts) / link_target
        )
        resolved = unresolved.resolve(strict=True)
        if not resolved.is_relative_to(source_real):
            raise _DeliveryError("source-file-symlink-unsafe")
        confirmed_stat = os.stat(path.name, dir_fd=entry_parent_fd, follow_symlinks=False)
        if not stat.S_ISLNK(confirmed_stat.st_mode) or _file_generation(
            confirmed_stat
        ) != _file_generation(first_stat):
            raise _DeliveryError("source-file-race", retryable=True)
        tracked_entries.append(_TrackedSourceEntry(path, _file_generation(confirmed_stat)))
        resolved_path = PurePosixPath(resolved.relative_to(source_real).as_posix())
        return _read_regular_source_file(root_fd, resolved_path, tracked_entries)
    except _DeliveryError:
        raise
    except OSError as error:
        raise _DeliveryError("source-file-race", retryable=True) from error


def _read_regular_source_file(
    root_fd: int,
    path: PurePosixPath,
    tracked_entries: list[_TrackedSourceEntry],
) -> _SourceFile:
    with _source_parent_fd(root_fd, path) as parent_fd:
        return _read_regular_source_entry(parent_fd, path.name, None, tracked_entries, path)


def _read_regular_source_entry(
    parent_fd: int,
    name: str,
    expected: os.stat_result | None,
    tracked_entries: list[_TrackedSourceEntry],
    tracked_path: PurePosixPath,
) -> _SourceFile:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise _DeliveryError("source-file-race", retryable=True) from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise _DeliveryError("source-file-race", retryable=True)
        if expected is not None and _file_generation(before) != _file_generation(expected):
            raise _DeliveryError("source-file-race", retryable=True)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        path_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _file_generation(after) != _file_generation(before) or _file_generation(
            path_stat
        ) != _file_generation(after):
            raise _DeliveryError("source-file-race", retryable=True)
        tracked_entries.append(_TrackedSourceEntry(tracked_path, _file_generation(after)))
        return _SourceFile(b"".join(chunks), bool(after.st_mode & stat.S_IXUSR))
    except _DeliveryError:
        raise
    except OSError as error:
        raise _DeliveryError("source-file-race", retryable=True) from error
    finally:
        os.close(descriptor)


def _directory_generation(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns)


def _file_generation(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mode,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_tracked_source_entries(root_fd: int, entries: Sequence[_TrackedSourceEntry]) -> None:
    for entry in entries:
        try:
            with _source_parent_fd(root_fd, entry.path) as parent_fd:
                current = os.stat(entry.path.name, dir_fd=parent_fd, follow_symlinks=False)
        except _DeliveryError:
            raise
        except OSError as error:
            raise _DeliveryError("source-file-race", retryable=True) from error
        if _file_generation(current) != entry.generation:
            raise _DeliveryError("source-file-race", retryable=True)


def _directory_may_contain_owned_files(path: PurePosixPath) -> bool:
    parts = () if path.as_posix() == "." else path.parts
    if not parts:
        return True
    if parts[0] in {"context", "scripts"}:
        return True
    if parts[0] in {"agents", "commands", "plugins"}:
        return len(parts) == 1
    if parts[0] != "skills":
        return False
    return len(parts) == 1 or _NAME.fullmatch(parts[1]) is not None


@contextmanager
def _directory_lock(destination: Path) -> Iterator[int]:
    try:
        descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise _DeliveryError("destination-root-unsafe") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _directory_is_attached(destination: Path, descriptor: int) -> bool:
    try:
        current = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        expected_stat = os.fstat(descriptor)
        current_stat = os.fstat(current)
        return (current_stat.st_dev, current_stat.st_ino) == (
            expected_stat.st_dev,
            expected_stat.st_ino,
        )
    finally:
        os.close(current)


def _require_root_attached(destination: Path, root_fd: int) -> None:
    if not _directory_is_attached(destination, root_fd):
        raise _DeliveryError("destination-root-race", retryable=True)


@contextmanager
def _parent_fd(root_fd: int, path: PurePosixPath, *, create: bool) -> Iterator[int]:
    descriptor = os.dup(root_fd)
    try:
        for part in path.parts[:-1]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise _MissingParentError from None
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                try:
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except OSError as error:
                    raise _DeliveryError("destination-parent-unsafe") from error
            except OSError as error:
                raise _DeliveryError("destination-parent-unsafe") from error
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _parent_is_attached(root_fd: int, path: PurePosixPath, parent_fd: int) -> bool:
    try:
        with _parent_fd(root_fd, path, create=False) as current_fd:
            expected = os.fstat(parent_fd)
            current = os.fstat(current_fd)
            return (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
    except (_DeliveryError, _MissingParentError, OSError):
        return False


def _rename_noreplace(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    try:
        function = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as error:
        raise OSError(errno.ENOSYS, "renameat2 is required for safe publication") from error
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    result = function(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), destination_name)
    raise OSError(error_number, os.strerror(error_number), destination_name)


def _check_parent(root_fd: int, path: PurePosixPath) -> None:
    try:
        with _parent_fd(root_fd, path, create=False):
            pass
    except _MissingParentError:
        return


def _read_at(root_fd: int, path: PurePosixPath) -> _Snapshot | None:
    try:
        with _parent_fd(root_fd, path, create=False) as parent_fd:
            return _read_at_parent(parent_fd, path.name)
    except _MissingParentError:
        return None


def _read_at_parent(parent_fd: int, name: str) -> _Snapshot | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _DeliveryError("destination-path-unsafe") from error
    try:
        result = os.fstat(descriptor)
        if not stat.S_ISREG(result.st_mode):
            raise _DeliveryError("destination-path-unsafe")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return _Snapshot(b"".join(chunks), bool(result.st_mode & stat.S_IXUSR))
    finally:
        os.close(descriptor)


def _strict_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _ManifestError
        value[key] = item
    return value


def _as_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _ManifestError
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise _ManifestError
    return cast(dict[str, object], mapping)


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _decode_manifest(raw: bytes) -> _Manifest:
    try:
        parsed: object = json.loads(raw, object_pairs_hook=_strict_object)
        root = _as_object(parsed)
        if set(root) != {"schema_version", "tool", "files", "fragments"}:
            raise _ManifestError
        version = root["schema_version"]
        if type(version) is not int or version != _SCHEMA_VERSION:
            raise _ManifestError
        if root["tool"] != _TOOL or root["fragments"] != []:
            raise _ManifestError
        raw_files = _as_object(root["files"])
        files: dict[PurePosixPath, _FileState] = {}
        for raw_path, raw_state in raw_files.items():
            path = PurePosixPath(raw_path)
            state = _as_object(raw_state)
            if set(state) != {"content_hash", "executable"}:
                raise _ManifestError
            content_hash = state["content_hash"]
            executable = state["executable"]
            if (
                raw_path != path.as_posix()
                or not _safe_relative(path)
                or not _owned(path)
                or path in files
                or not _valid_hash(content_hash)
                or not isinstance(executable, bool)
            ):
                raise _ManifestError
            files[path] = _FileState(cast(str, content_hash), executable)
        return _Manifest(files)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        RecursionError,
        TypeError,
        _ManifestError,
    ) as error:
        raise _ManifestError from error


def _encode_manifest(files: Mapping[PurePosixPath, _SourceFile]) -> bytes:
    value: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "tool": _TOOL,
        "files": {
            path.as_posix(): {
                "content_hash": source_file.state.content_hash,
                "executable": source_file.executable,
            }
            for path, source_file in sorted(files.items())
        },
        "fragments": cast(list[object], []),
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _load_manifest(root_fd: int) -> tuple[_Manifest | None, _Snapshot | None]:
    path = PurePosixPath(MANIFEST_NAME)
    snapshot = _read_at(root_fd, path)
    if snapshot is None:
        return None, None
    try:
        return _decode_manifest(snapshot.content), snapshot
    except _ManifestError as error:
        raise _DeliveryError("manifest-malformed") from error


def _preflight(
    root_fd: int,
    desired: Mapping[PurePosixPath, _SourceFile],
    prior: _Manifest | None,
) -> dict[PurePosixPath, _Snapshot | None]:
    previous: Mapping[PurePosixPath, _FileState] = prior.files if prior is not None else {}
    paths = set(desired) | set(previous)
    for path in sorted(paths | {PurePosixPath(MANIFEST_NAME)}):
        _check_parent(root_fd, path)

    observations: dict[PurePosixPath, _Snapshot | None] = {}
    for path in sorted(paths):
        current = _read_at(root_fd, path)
        observations[path] = current
        wanted = desired.get(path)
        old = previous.get(path)
        if wanted is not None:
            wanted_state = wanted.state
            if old is None:
                if current is not None and current.state != wanted_state:
                    raise _DeliveryError("unmanaged-file-collision")
            elif current is not None and current.state not in {old, wanted_state}:
                raise _DeliveryError("managed-file-drift")
        elif current is not None and current.state != cast(_FileState, old):
            raise _DeliveryError("stale-file-drift")
    return observations


def _create_stage(root_fd: int) -> _Stage:
    for _attempt in range(32):
        name = f".djinn-opencode-stage-{secrets.token_hex(12)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        except OSError as error:
            raise _DeliveryError("stage-create-failed", retryable=True) from error
        descriptor: int | None = None
        identity: tuple[int, int] | None = None
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            opened = os.fstat(descriptor)
            identity = (opened.st_dev, opened.st_ino)
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or (current.st_dev, current.st_ino) != identity
            ):
                raise OSError("stage changed")
            return _Stage(name, descriptor, opened.st_dev, opened.st_ino, {})
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            if identity is not None:
                _remove_empty_directory_if_matching(root_fd, name, identity)
            code = "stage-create-failed" if descriptor is None else "stage-changed"
            raise _DeliveryError(code, retryable=True) from error
    raise _DeliveryError("stage-create-failed", retryable=True)


def _stage_attached(root_fd: int, stage: _Stage) -> None:
    try:
        current = os.stat(stage.name, dir_fd=root_fd, follow_symlinks=False)
    except OSError as error:
        raise _DeliveryError("stage-changed", retryable=True) from error
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or (current.st_dev, current.st_ino) != (stage.device, stage.inode)
    ):
        raise _DeliveryError("stage-changed", retryable=True)


@contextmanager
def _stage_parent_fd(stage: _Stage, path: PurePosixPath, *, create: bool) -> Iterator[int]:
    descriptor = os.dup(stage.descriptor)
    relative = PurePosixPath()
    try:
        for part in path.parts[:-1]:
            relative /= part
            created = False
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise _MissingParentError from None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    created = True
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except OSError as error:
                    raise _DeliveryError("stage-changed", retryable=True) from error
            except OSError as error:
                raise _DeliveryError("stage-changed", retryable=True) from error
            try:
                opened = os.fstat(child)
                entry = _StageEntry(opened.st_dev, opened.st_ino, True)
                if created:
                    stage.entries[relative] = entry
                current = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or stat.S_ISLNK(current.st_mode)
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                    or stage.entries.get(relative) != entry
                ):
                    raise OSError("stage changed")
            except OSError as error:
                os.close(child)
                raise _DeliveryError("stage-changed", retryable=True) from error
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _stage_file(
    root_fd: int,
    stage: _Stage,
    category: str,
    path: PurePosixPath,
    source_file: _SourceFile,
) -> _StagedFile:
    _stage_attached(root_fd, stage)
    relative = PurePosixPath(category) / path
    with _stage_parent_fd(stage, relative, create=True) as parent_fd:
        try:
            descriptor = os.open(
                relative.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o700 if source_file.executable else 0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise _DeliveryError("stage-changed", retryable=True) from error
        try:
            opened = os.fstat(descriptor)
            stage.entries[relative] = _StageEntry(opened.st_dev, opened.st_ino, False)
            current = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or opened.st_nlink != 1
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise _DeliveryError("stage-changed", retryable=True)
            remaining = memoryview(source_file.content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise _DeliveryError("stage-changed", retryable=True)
                remaining = remaining[written:]
            os.fchmod(descriptor, 0o700 if source_file.executable else 0o600)
            staged_stat = os.fstat(descriptor)
            return _StagedFile(
                relative,
                staged_stat.st_dev,
                staged_stat.st_ino,
                source_file.state,
            )
        finally:
            os.close(descriptor)


def _verify_stage(parent_fd: int, staged: _StagedFile) -> None:
    try:
        descriptor = os.open(
            staged.relative_path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise _DeliveryError("stage-changed", retryable=True) from error
    try:
        result = os.fstat(descriptor)
        if not stat.S_ISREG(result.st_mode) or result.st_nlink != 1:
            raise _DeliveryError("stage-changed", retryable=True)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        path_stat = os.stat(staged.relative_path.name, dir_fd=parent_fd, follow_symlinks=False)
        actual = _FileState(_digest(b"".join(chunks)), bool(result.st_mode & stat.S_IXUSR))
        if (
            actual != staged.state
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_nlink != 1
            or (path_stat.st_dev, path_stat.st_ino) != (result.st_dev, result.st_ino)
            or (result.st_dev, result.st_ino) != (staged.device, staged.inode)
        ):
            raise _DeliveryError("stage-changed", retryable=True)
    except OSError as error:
        raise _DeliveryError("stage-changed", retryable=True) from error
    finally:
        os.close(descriptor)


def _replace(
    root_fd: int,
    stage: _Stage,
    staged: _StagedFile,
    destination: PurePosixPath,
    expected: _Snapshot | None,
) -> None:
    _stage_attached(root_fd, stage)
    try:
        with (
            _stage_parent_fd(stage, staged.relative_path, create=False) as source_fd,
            _parent_fd(root_fd, destination, create=True) as destination_fd,
        ):
            _verify_stage(source_fd, staged)
            if _read_at_parent(destination_fd, destination.name) != expected:
                raise _DeliveryError("destination-race", retryable=True)
            if not _parent_is_attached(root_fd, destination, destination_fd):
                raise _DeliveryError("destination-parent-race", retryable=True)
            if expected is None:
                _rename_noreplace(
                    source_fd,
                    staged.relative_path.name,
                    destination_fd,
                    destination.name,
                )
            else:
                quarantine = _quarantine_destination(stage, destination_fd, destination.name)
                try:
                    quarantined = _read_at_parent(stage.descriptor, quarantine.name)
                except _DeliveryError:
                    _restore_quarantine(stage, quarantine, destination_fd, destination.name)
                    raise _DeliveryError("destination-race", retryable=True) from None
                if quarantined != expected:
                    _restore_quarantine(stage, quarantine, destination_fd, destination.name)
                    raise _DeliveryError("destination-race", retryable=True)
                if not _parent_is_attached(root_fd, destination, destination_fd):
                    _restore_quarantine(stage, quarantine, destination_fd, destination.name)
                    raise _DeliveryError("destination-parent-race", retryable=True)
                try:
                    _rename_noreplace(
                        source_fd,
                        staged.relative_path.name,
                        destination_fd,
                        destination.name,
                    )
                except OSError:
                    _preserve_quarantine(stage, quarantine)
                    raise _DeliveryError("quarantine-preserved", retryable=True) from None
                if not _parent_is_attached(root_fd, destination, destination_fd):
                    _preserve_quarantine(stage, quarantine)
                    raise _DeliveryError("quarantine-preserved", retryable=True)
            if not _parent_is_attached(root_fd, destination, destination_fd):
                raise _DeliveryError("destination-parent-race", retryable=True)
    except _DeliveryError:
        raise
    except OSError as error:
        raise _DeliveryError("publication-failed", retryable=True) from error


def _quarantine_destination(
    stage: _Stage,
    destination_fd: int,
    destination_name: str,
) -> PurePosixPath:
    quarantine = PurePosixPath(f".quarantine-{secrets.token_hex(16)}")
    _rename_noreplace(destination_fd, destination_name, stage.descriptor, quarantine.name)
    try:
        current = os.stat(quarantine.name, dir_fd=stage.descriptor, follow_symlinks=False)
        stage.entries[quarantine] = _StageEntry(
            current.st_dev,
            current.st_ino,
            stat.S_ISDIR(current.st_mode),
        )
    except (OSError, MemoryError):
        _preserve_quarantine(stage, quarantine)
        raise _DeliveryError("quarantine-preserved", retryable=True) from None
    return quarantine


def _restore_quarantine(
    stage: _Stage,
    quarantine: PurePosixPath,
    destination_fd: int,
    destination_name: str,
) -> None:
    try:
        _rename_noreplace(stage.descriptor, quarantine.name, destination_fd, destination_name)
    except OSError:
        _preserve_quarantine(stage, quarantine)
        raise _DeliveryError("quarantine-preserved", retryable=True) from None
    stage.entries.pop(quarantine, None)


def _preserve_quarantine(stage: _Stage, quarantine: PurePosixPath) -> None:
    stage.entries.pop(quarantine, None)


def _remove(root_fd: int, path: PurePosixPath, expected: _Snapshot) -> None:
    try:
        with _parent_fd(root_fd, path, create=False) as parent_fd:
            if _read_at_parent(parent_fd, path.name) != expected:
                raise _DeliveryError("destination-race", retryable=True)
            if not _parent_is_attached(root_fd, path, parent_fd):
                raise _DeliveryError("destination-parent-race", retryable=True)
            quarantine_stage = _create_stage(root_fd)
            try:
                quarantine = _quarantine_destination(quarantine_stage, parent_fd, path.name)
                try:
                    quarantined = _read_at_parent(
                        quarantine_stage.descriptor,
                        quarantine.name,
                    )
                except _DeliveryError:
                    _restore_quarantine(quarantine_stage, quarantine, parent_fd, path.name)
                    raise _DeliveryError("destination-race", retryable=True) from None
                if quarantined != expected:
                    _restore_quarantine(quarantine_stage, quarantine, parent_fd, path.name)
                    raise _DeliveryError("destination-race", retryable=True)
                if not _parent_is_attached(root_fd, path, parent_fd):
                    _restore_quarantine(quarantine_stage, quarantine, parent_fd, path.name)
                    raise _DeliveryError("destination-parent-race", retryable=True)
            finally:
                _cleanup_stage(root_fd, quarantine_stage)
    except (OSError, _MissingParentError) as error:
        raise _DeliveryError("publication-failed", retryable=True) from error


def _cleanup_stage(root_fd: int, stage: _Stage) -> None:
    try:
        files = sorted(
            (path for path, entry in stage.entries.items() if not entry.directory),
            key=lambda path: (len(path.parts), path.as_posix()),
            reverse=True,
        )
        directories = sorted(
            (path for path, entry in stage.entries.items() if entry.directory),
            key=lambda path: (len(path.parts), path.as_posix()),
            reverse=True,
        )
        for path in (*files, *directories):
            entry = stage.entries[path]
            try:
                with _stage_parent_fd(stage, path, create=False) as parent_fd:
                    current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                    if (
                        (current.st_dev, current.st_ino) != (entry.device, entry.inode)
                        or stat.S_ISDIR(current.st_mode) != entry.directory
                        or stat.S_ISLNK(current.st_mode)
                    ):
                        continue
                    if entry.directory:
                        os.rmdir(path.name, dir_fd=parent_fd)
                    else:
                        os.unlink(path.name, dir_fd=parent_fd)
            except (OSError, _MissingParentError, _DeliveryError):
                continue
    finally:
        os.close(stage.descriptor)

    _remove_empty_directory_if_matching(
        root_fd,
        stage.name,
        (stage.device, stage.inode),
    )


def _remove_empty_directory_if_matching(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISDIR(current.st_mode)
            and not stat.S_ISLNK(current.st_mode)
            and (current.st_dev, current.st_ino) == identity
        ):
            os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        pass


@contextmanager
def _absolute_parent_fd(path: Path, code: str) -> Iterator[tuple[int, str]]:
    absolute = Path(os.path.abspath(path))
    if not absolute.name:
        raise _DeliveryError(code)
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise _DeliveryError(code) from error
    try:
        for part in absolute.parent.parts[1:]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise _DeliveryError(code) from error
            os.close(descriptor)
            descriptor = child
        yield descriptor, absolute.name
    finally:
        os.close(descriptor)


def _read_settings_at(
    parent_fd: int, name: str, code: str, *, missing_ok: bool
) -> _Snapshot | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise _DeliveryError(code) from None
    except OSError as error:
        raise _DeliveryError(code) from error
    try:
        result = os.fstat(descriptor)
        if not stat.S_ISREG(result.st_mode):
            raise _DeliveryError(code)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return _Snapshot(b"".join(chunks), bool(result.st_mode & stat.S_IXUSR))
    finally:
        os.close(descriptor)


def copy_settings(source: Path, destination: Path, *, missing_ok: bool) -> None:
    """Atomically copy one personal settings file without following links."""
    with _absolute_parent_fd(source, "settings-source-unsafe") as (
        source_parent,
        source_name,
    ):
        source_snapshot = _read_settings_at(
            source_parent,
            source_name,
            "settings-source-unsafe",
            missing_ok=missing_ok,
        )
    if source_snapshot is None:
        return

    with _absolute_parent_fd(destination, "settings-destination-unsafe") as (
        destination_parent,
        destination_name,
    ):
        observation = _read_settings_at(
            destination_parent,
            destination_name,
            "settings-destination-unsafe",
            missing_ok=True,
        )
        if observation is not None and observation.content == source_snapshot.content:
            return
        temporary_name = f".{destination_name}.djinn-{secrets.token_hex(8)}"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_parent,
            )
            remaining = memoryview(source_snapshot.content)
            while remaining:
                written = os.write(descriptor, remaining)
                remaining = remaining[written:]
            os.fsync(descriptor)
            if (
                _read_settings_at(
                    destination_parent,
                    destination_name,
                    "settings-destination-unsafe",
                    missing_ok=True,
                )
                != observation
            ):
                raise _DeliveryError("settings-destination-race")
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            staged_stat = os.fstat(descriptor)
            path_stat = os.stat(
                temporary_name,
                dir_fd=destination_parent,
                follow_symlinks=False,
            )
            if (
                b"".join(chunks) != source_snapshot.content
                or not stat.S_ISREG(staged_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or staged_stat.st_mode & stat.S_IXUSR
                or staged_stat.st_nlink != 1
                or path_stat.st_nlink != 1
                or (path_stat.st_dev, path_stat.st_ino) != (staged_stat.st_dev, staged_stat.st_ino)
            ):
                raise _DeliveryError("settings-stage-unsafe")
            os.replace(
                temporary_name,
                destination_name,
                src_dir_fd=destination_parent,
                dst_dir_fd=destination_parent,
            )
        except _DeliveryError:
            raise
        except OSError as error:
            raise _DeliveryError("settings-copy-failed") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=destination_parent)


def _publish(
    root_fd: int,
    stage: _Stage,
    desired: Mapping[PurePosixPath, _SourceFile],
    prior: _Manifest | None,
    manifest_observation: _Snapshot | None,
    observations: Mapping[PurePosixPath, _Snapshot | None],
    destination: Path,
) -> None:
    _require_root_attached(destination, root_fd)
    previous: Mapping[PurePosixPath, _FileState] = prior.files if prior is not None else {}
    expected_final: dict[PurePosixPath, _Snapshot | None] = {
        path: _Snapshot(source_file.content, source_file.executable)
        for path, source_file in desired.items()
    }
    for path, source_file in sorted(desired.items()):
        current = _read_at(root_fd, path)
        if current is not None and current.state == source_file.state:
            continue
        if current != observations.get(path):
            raise _DeliveryError("destination-race", retryable=True)
        staged = _stage_file(root_fd, stage, "files", path, source_file)
        _replace(root_fd, stage, staged, path, current)

    for path, old in sorted(previous.items()):
        if path in desired:
            continue
        expected_final[path] = None
        current = _read_at(root_fd, path)
        if current is None:
            continue
        if current.state != old or current != observations.get(path):
            raise _DeliveryError("destination-race", retryable=True)
        _remove(root_fd, path, current)

    manifest_content = _encode_manifest(desired)
    _require_root_attached(destination, root_fd)
    _verify_final_snapshots(root_fd, expected_final)
    manifest_path = PurePosixPath(MANIFEST_NAME)
    current_manifest = _read_at(root_fd, manifest_path)
    if current_manifest is not None and current_manifest.content == manifest_content:
        _require_root_attached(destination, root_fd)
        _verify_final_snapshots(root_fd, expected_final)
        return
    if current_manifest != manifest_observation:
        raise _DeliveryError("manifest-race", retryable=True)
    staged_manifest = _stage_file(
        root_fd,
        stage,
        "metadata",
        manifest_path,
        _SourceFile(manifest_content, False),
    )
    _replace(root_fd, stage, staged_manifest, manifest_path, current_manifest)
    _require_root_attached(destination, root_fd)
    _verify_final_snapshots(root_fd, expected_final)


def _verify_final_snapshots(
    root_fd: int,
    expected: Mapping[PurePosixPath, _Snapshot | None],
) -> None:
    for path, snapshot in expected.items():
        if _read_at(root_fd, path) != snapshot:
            raise _DeliveryError("destination-race", retryable=True)


def deliver(source: Path, destination: Path) -> None:
    desired = _read_source(source)
    destination_stat = _real_directory(destination, "destination-root-unsafe")
    with _directory_lock(destination) as root_fd:
        stage: _Stage | None = None
        try:
            locked_stat = os.fstat(root_fd)
            if (locked_stat.st_dev, locked_stat.st_ino) != (
                destination_stat.st_dev,
                destination_stat.st_ino,
            ):
                raise _DeliveryError("destination-root-race", retryable=True)
            _require_root_attached(destination, root_fd)
            prior, manifest_observation = _load_manifest(root_fd)
            observations = _preflight(root_fd, desired, prior)
            desired_manifest = _Manifest(
                {path: source_file.state for path, source_file in desired.items()}
            )
            if prior == desired_manifest and all(
                observations.get(path) is not None
                and cast(_Snapshot, observations[path]).state == source_file.state
                for path, source_file in desired.items()
            ):
                _require_root_attached(destination, root_fd)
                return
            _require_root_attached(destination, root_fd)
            stage = _create_stage(root_fd)
            _publish(
                root_fd,
                stage,
                desired,
                prior,
                manifest_observation,
                observations,
                destination,
            )
            _require_root_attached(destination, root_fd)
        finally:
            if stage is not None:
                _cleanup_stage(root_fd, stage)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deliver the OpenCode workflow seed.")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--copy-settings", nargs=2, type=Path, metavar=("SOURCE", "DESTINATION"))
    parser.add_argument("--missing-ok", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        settings_paths = cast(list[Path] | None, arguments.copy_settings)
        source = cast(Path | None, arguments.source)
        destination = cast(Path | None, arguments.destination)
        if settings_paths is not None:
            if source is not None or destination is not None:
                parser.error("settings copy cannot be combined with workflow delivery")
            copy_settings(
                settings_paths[0],
                settings_paths[1],
                missing_ok=cast(bool, arguments.missing_ok),
            )
        else:
            if source is None or destination is None:
                parser.error("workflow delivery requires --source and --destination")
            deliver(source, destination)
    except _DeliveryError as error:
        print(f"opencode workflow delivery failed: {error.code}", file=sys.stderr)
        return 1
    except (OSError, RecursionError, TypeError, ValueError):
        print("opencode workflow delivery failed: invalid-data", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
