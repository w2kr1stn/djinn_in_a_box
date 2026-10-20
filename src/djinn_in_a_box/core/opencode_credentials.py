"""OpenCode credentials as a monotone CRDT over the config root: model, host state, files, lock.

Standalone by design: stdlib only, no imports from ``djinn_in_a_box``, Python 3.11 compatible.
The dev image copies this file and runs it as a script; the host imports it as a module.

Nothing in an exception message or a log line may carry a credential value. Errors name an
operation, a count or an ID, never a label or a value.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import NamedTuple, NoReturn, cast

FILE_LIMIT = 1024 * 1024
EXPORT_LIMIT = 16 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 30.0
COMMAND_TIMEOUT_SECONDS = 60.0
STALE_TEMP_SECONDS = 60.0
PUBLISH_ATTEMPTS = 3
CREDENTIALS_NAME = "credentials.json"
TOMBSTONES_NAME = "credentials.removed.json"
MODES = ("start", "checkpoint", "final")

_ID_PATTERN = re.compile(r"[A-Za-z0-9._:@/-]{1,200}")
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")
_TEMP_RANDOM_LENGTH = 16
_LOCK_POLL_SECONDS = 0.05
_ENTRY_KEYS = frozenset({"id", "integrationID", "label", "active", "value"})
_VALUE_KEYS: dict[str, frozenset[str]] = {
    "key": frozenset({"type", "key", "configuration", "metadata"}),
    "oauth": frozenset({"type", "methodID", "refresh", "access", "expires", "metadata"}),
    "external": frozenset({"type", "methodID", "metadata"}),
}
_VALUE_REQUIRED: dict[str, tuple[str, ...]] = {
    "key": ("key",),
    "oauth": ("methodID", "refresh", "access", "expires"),
    "external": ("methodID",),
}
_STATE_KEYS = frozenset({"database", "seen", "hidden", "reactivate"})


class CredentialsError(Exception):
    """Base error. ``reason`` is value-free: an operation, a count or an ID, never content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NotUnderstood(CredentialsError):  # noqa: N818
    """Content, schema, value variant or size is not understood (helper exit 1)."""


class Refused(CredentialsError):  # noqa: N818
    """Path type or ownership is refused: symlink, FIFO, directory, foreign owner (exit 3)."""


class ChangedOnDisk(CredentialsError):  # noqa: N818
    """The destination no longer holds the bytes the writer expected."""


class LockTimeout(CredentialsError):  # noqa: N818
    """The carrier lock was not obtained within the timeout."""


class LockReentered(CredentialsError):  # noqa: N818
    """The carrier lock is not re-entrant; this process already holds it for that inode."""


class OpenCodeFailed(CredentialsError):  # noqa: N818
    """An OpenCode command failed, timed out or could not start (helper exit 1)."""


class ApplyFailed(CredentialsError):  # noqa: N818
    """A postcondition of the database adjustment was missed (helper exit 1)."""


# ---------------------------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """A validated credential. Equality and hash ignore ``active`` and follow the canonical form."""

    id: str
    integration_id: str
    label: str = field(compare=False)
    active: bool = field(compare=False)
    value: Mapping[str, object] = field(compare=False)
    canonical: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical.encode("utf-8")).hexdigest()

    @property
    def order(self) -> tuple[int, str]:
        """Total order per ID: a later OAuth ``expires`` wins, then the canonical JSON."""
        if self.value.get("type") == "oauth":
            return (cast(int, self.value["expires"]), self.canonical)
        return (-1, self.canonical)


def _reject_constant(_: str) -> object:
    raise NotUnderstood("non-finite number")


def _parse_float(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise NotUnderstood("non-finite number")
    return number


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NotUnderstood("duplicate object member")
        result[key] = value
    return result


def load_strict_json(raw: bytes, limit: int = FILE_LIMIT) -> object:
    """Parse UTF-8 JSON, rejecting NaN/Infinity, duplicate members and more than ``limit`` bytes."""
    if len(raw) > limit:
        raise NotUnderstood("content exceeds the size limit")
    try:
        return cast(
            object,
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
                parse_float=_parse_float,
            ),
        )
    except CredentialsError:
        raise
    except (ValueError, RecursionError) as error:
        raise NotUnderstood("invalid JSON or UTF-8") from error


def _is_id(candidate: object) -> bool:
    return isinstance(candidate, str) and _ID_PATTERN.fullmatch(candidate) is not None


def _is_plain_int(candidate: object) -> bool:
    return isinstance(candidate, int) and not isinstance(candidate, bool)


def _form_answer_ok(candidate: object) -> bool:
    if not isinstance(candidate, dict):
        return False
    for item in cast(dict[object, object], candidate).values():
        if isinstance(item, list):
            if not all(isinstance(part, str) for part in cast(list[object], item)):
                return False
        elif not isinstance(item, str | int | float | bool):
            return False
    return True


def _validate_value(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise NotUnderstood(f"{where}: value is not an object")
    fields = cast(dict[str, object], value)
    kind = fields.get("type")
    if not isinstance(kind, str) or kind not in _VALUE_KEYS:
        raise NotUnderstood(f"{where}: unknown value variant")
    if not set(fields) <= _VALUE_KEYS[kind]:
        raise NotUnderstood(f"{where}: unknown value field")
    for name in _VALUE_REQUIRED[kind]:
        if name not in fields:
            raise NotUnderstood(f"{where}: missing value field {name}")
        if name == "expires":
            ok = _is_plain_int(fields[name]) and cast(int, fields[name]) >= 0
        else:
            ok = isinstance(fields[name], str)
        if not ok:
            raise NotUnderstood(f"{where}: value field {name} has the wrong type")
    if "metadata" in fields and not isinstance(fields["metadata"], dict):
        raise NotUnderstood(f"{where}: metadata is not an object")
    if "configuration" in fields and not _form_answer_ok(fields["configuration"]):
        raise NotUnderstood(f"{where}: configuration has the wrong shape")
    return fields


def canonical_json(entry_without_active: Mapping[str, object]) -> str:
    try:
        text = json.dumps(
            entry_without_active, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        text.encode("utf-8")
    except (ValueError, TypeError) as error:
        raise NotUnderstood("entry is not representable as UTF-8 JSON") from error
    return text


def validate_entry(candidate: object, index: int = 0) -> Entry:
    where = f"entry {index}"
    if not isinstance(candidate, dict):
        raise NotUnderstood(f"{where}: not an object")
    fields = cast(dict[str, object], candidate)
    if frozenset(fields) != _ENTRY_KEYS:
        raise NotUnderstood(f"{where}: fields differ from the export format")
    if not _is_id(fields["id"]):
        raise NotUnderstood(f"{where}: id is not valid")
    if not _is_id(fields["integrationID"]):
        raise NotUnderstood(f"{where}: integrationID is not valid")
    if not isinstance(fields["label"], str):
        raise NotUnderstood(f"{where}: label is not a string")
    if not isinstance(fields["active"], bool):
        raise NotUnderstood(f"{where}: active is not a boolean")
    value = _validate_value(fields["value"], where)
    identity = cast(str, fields["id"])
    canonical = canonical_json({key: item for key, item in fields.items() if key != "active"})
    return Entry(
        id=identity,
        integration_id=cast(str, fields["integrationID"]),
        label=fields["label"],
        active=fields["active"],
        value=value,
        canonical=canonical,
    )


def parse_entries(raw: bytes, limit: int = FILE_LIMIT) -> list[Entry]:
    document = load_strict_json(raw, limit)
    if not isinstance(document, list):
        raise NotUnderstood("credentials are not an array")
    entries: list[Entry] = []
    seen: set[str] = set()
    for index, item in enumerate(cast(list[object], document)):
        entry = validate_entry(item, index)
        if entry.id in seen:
            raise NotUnderstood(f"duplicate id {entry.id}")
        seen.add(entry.id)
        entries.append(entry)
    return entries


def parse_tombstones(raw: bytes, limit: int = FILE_LIMIT) -> list[str]:
    """Return the sorted, unique IDs. Order and repeats are tolerated, content is not."""
    document = load_strict_json(raw, limit)
    if not isinstance(document, list):
        raise NotUnderstood("tombstones are not an array")
    identities = cast(list[object], document)
    if not all(_is_id(item) for item in identities):
        raise NotUnderstood("a tombstone is not a valid id")
    return sorted(set(cast(list[str], identities)))


# ---------------------------------------------------------------------------------------------
# Merge (pure)
# ---------------------------------------------------------------------------------------------


class MergeResult(NamedTuple):
    entries: dict[str, Entry]
    tombstones: frozenset[str]
    conflicts: tuple[str, ...]


def merge(adds: Iterable[Sequence[Entry]], tombstones: Iterable[Iterable[str]]) -> MergeResult:
    """Union of all candidates minus the tombstones; the maximum of the order wins per ID.

    ``conflicts`` lists IDs whose winner was decided by the canonical JSON between differing
    values, that is without an OAuth ``expires`` precedence.
    """
    dead = frozenset(identity for group in tombstones for identity in group)
    candidates: dict[str, list[Entry]] = {}
    for group in adds:
        for entry in group:
            if entry.id not in dead:
                candidates.setdefault(entry.id, []).append(entry)
    winners: dict[str, Entry] = {}
    conflicts: list[str] = []
    for identity, options in candidates.items():
        winners[identity] = max(options, key=lambda option: option.order)
        leading = max(option.order[0] for option in options)
        if len({o.canonical for o in options if o.order[0] == leading}) > 1:
            conflicts.append(identity)
    return MergeResult(winners, dead, tuple(sorted(conflicts)))


def _dump(document: object, limit: int) -> bytes:
    raw = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(raw) > limit:
        raise NotUnderstood("result exceeds the size limit")
    return raw


def materialize_entries(
    entries: Iterable[Entry], selection: Mapping[str, str], limit: int = FILE_LIMIT
) -> bytes:
    """Export-format bytes, sorted by ID; ``active`` comes from ``{integrationID: id}``."""
    document = [
        {
            "id": entry.id,
            "integrationID": entry.integration_id,
            "label": entry.label,
            "active": selection.get(entry.integration_id) == entry.id,
            "value": entry.value,
        }
        for entry in sorted(entries, key=lambda item: item.id)
    ]
    return _dump(document, limit)


def materialize_tombstones(identities: Iterable[str], limit: int = FILE_LIMIT) -> bytes:
    return _dump(sorted(set(identities)), limit)


# ---------------------------------------------------------------------------------------------
# Host state (pure, plus read and write through the file helpers)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HostState:
    """Host-local, value-free memory bound to the inode of the OpenCode database."""

    inode: int | None = None
    seen: Mapping[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))
    hidden: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: cast(dict[str, tuple[str, ...]], {})
    )
    reactivate: Mapping[str, str] = field(default_factory=lambda: cast(dict[str, str], {}))


def bind(state: HostState, db_inode: int | None) -> HostState:
    """Keep the state only for the same database file; a missing or another inode resets it."""
    if state.inode != db_inode:
        return HostState(inode=db_inode)
    return state


def vanished(seen: Mapping[str, str], export_entries: Iterable[Entry]) -> list[tuple[str, str]]:
    present = {entry.id for entry in export_entries}
    return sorted((identity, fp) for identity, fp in seen.items() if identity not in present)


def hide(state: HostState, pairs: Iterable[tuple[str, str]]) -> HostState:
    hidden = {identity: set(fps) for identity, fps in state.hidden.items()}
    for identity, fp in pairs:
        hidden.setdefault(identity, set()).add(fp)
    return HostState(
        state.inode,
        state.seen,
        {identity: tuple(sorted(fps)) for identity, fps in hidden.items()},
        state.reactivate,
    )


def unhide_present(state: HostState, export_entries: Iterable[Entry]) -> HostState:
    """A hidden (id, fp) that is back in the database leaves ``hidden``."""
    hidden = {identity: set(fps) for identity, fps in state.hidden.items()}
    for entry in export_entries:
        if entry.id in hidden:
            hidden[entry.id].discard(entry.fingerprint)
    return HostState(
        state.inode,
        state.seen,
        {identity: tuple(sorted(fps)) for identity, fps in hidden.items() if fps},
        state.reactivate,
    )


def is_hidden(state: HostState, identity: str, fp: str) -> bool:
    return fp in state.hidden.get(identity, ())


def set_reactivate(state: HostState, integration_id: str, identity: str) -> HostState:
    """At most one target per integration; the first one wins."""
    if integration_id in state.reactivate:
        return state
    return HostState(
        state.inode, state.seen, state.hidden, {**state.reactivate, integration_id: identity}
    )


def confirm(state: HostState, export_entries: Iterable[Entry]) -> HostState:
    return HostState(
        state.inode,
        {entry.id: entry.fingerprint for entry in export_entries},
        state.hidden,
        {},
    )


def _require_mapping(candidate: object, what: str) -> dict[str, object]:
    if not isinstance(candidate, dict):
        raise NotUnderstood(f"host state: {what} is not an object")
    return cast(dict[str, object], candidate)


def _require_fingerprint(candidate: object) -> str:
    if not isinstance(candidate, str) or _FINGERPRINT_PATTERN.fullmatch(candidate) is None:
        raise NotUnderstood("host state: fingerprint is not 64 lowercase hex characters")
    return candidate


def _require_id(candidate: object, what: str) -> str:
    if not _is_id(candidate):
        raise NotUnderstood(f"host state: {what} is not a valid id")
    return cast(str, candidate)


def parse_host_state(raw: bytes, limit: int = FILE_LIMIT) -> HostState:
    document = _require_mapping(load_strict_json(raw, limit), "document")
    if frozenset(document) != _STATE_KEYS:
        raise NotUnderstood("host state: fields differ from the format")
    database = _require_mapping(document["database"], "database")
    inode = database.get("inode")
    if set(database) != {"inode"} or not _is_plain_int(inode) or cast(int, inode) < 0:
        raise NotUnderstood("host state: database inode is not valid")
    seen = {
        _require_id(identity, "seen id"): _require_fingerprint(fp)
        for identity, fp in _require_mapping(document["seen"], "seen").items()
    }
    hidden: dict[str, tuple[str, ...]] = {}
    for identity, fps in _require_mapping(document["hidden"], "hidden").items():
        if not isinstance(fps, list) or not fps:
            raise NotUnderstood("host state: hidden entry is not a non-empty array")
        hidden[_require_id(identity, "hidden id")] = tuple(
            sorted({_require_fingerprint(fp) for fp in cast(list[object], fps)})
        )
    reactivate = {
        _require_id(integration, "reactivate integration"): _require_id(target, "reactivate id")
        for integration, target in _require_mapping(document["reactivate"], "reactivate").items()
    }
    return HostState(cast(int, inode), seen, hidden, reactivate)


def dump_host_state(state: HostState) -> bytes:
    if state.inode is None:
        raise NotUnderstood("host state: no database identity to bind to")
    document = {
        "database": {"inode": state.inode},
        "seen": dict(sorted(state.seen.items())),
        "hidden": {identity: list(fps) for identity, fps in sorted(state.hidden.items())},
        "reactivate": dict(sorted(state.reactivate.items())),
    }
    return _dump(document, FILE_LIMIT)


def read_host_state(
    dir_fd: int, name: str, *, euid: int | None = None
) -> tuple[HostState, bytes | None]:
    """The state (empty when absent) plus the raw bytes to pass as ``expected`` when writing."""
    snapshot = read_private(dir_fd, name, FILE_LIMIT, euid=euid)
    if snapshot is None:
        return HostState(), None
    return parse_host_state(snapshot.data), snapshot.data


def write_host_state(dir_fd: int, name: str, state: HostState, expected: bytes | None) -> bytes:
    data = dump_host_state(state)
    if data != expected:
        write_atomic(dir_fd, name, data, expected)
    return data


# ---------------------------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------------------------


class FileRead(NamedTuple):
    data: bytes
    inode: int
    mtime_ns: int
    size: int


class _Ops:
    """The os calls whose order matters. Tests replace these attributes to record or fail."""

    @staticmethod
    def open(name: str, flags: int, mode: int, dir_fd: int) -> int:
        return os.open(name, flags, mode, dir_fd=dir_fd)

    @staticmethod
    def write(fd: int, data: memoryview) -> int:
        return os.write(fd, data)

    @staticmethod
    def fsync_file(fd: int) -> None:
        os.fsync(fd)

    @staticmethod
    def fsync_dir(fd: int) -> None:
        os.fsync(fd)

    @staticmethod
    def rename(source: str, target: str, dir_fd: int) -> None:
        os.replace(source, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)

    @staticmethod
    def unlink(name: str, dir_fd: int) -> None:
        os.unlink(name, dir_fd=dir_fd)


ops = _Ops()

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _require_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise Refused("not a plain entry name")


def open_dir(path: str | os.PathLike[str]) -> int:
    """Open a directory without following a symlink at the final component."""
    try:
        return os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise Refused("directory is a symlink or not a directory") from error
        raise


def read_private(dir_fd: int, name: str, limit: int, *, euid: int | None = None) -> FileRead | None:
    """Read a regular, own, size-bounded file below ``dir_fd``; None when absent.

    A looser mode is fixed with fchmod 0600 on the descriptor, without rewriting the content.
    Raises Refused for a symlink, FIFO, directory or foreign owner, NotUnderstood for size.
    """
    _require_name(name)
    owner = os.geteuid() if euid is None else euid
    try:
        descriptor = ops.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0, dir_fd
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise Refused("entry is a symlink") from error
        raise
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise Refused("entry is not a regular file")
        if status.st_uid != owner:
            raise Refused("entry belongs to another user")
        if status.st_size > limit:
            raise NotUnderstood("file exceeds the size limit")
        if status.st_mode & 0o177:
            os.fchmod(descriptor, 0o600)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise NotUnderstood("file exceeds the size limit")
        return FileRead(b"".join(chunks), status.st_ino, status.st_mtime_ns, status.st_size)
    finally:
        os.close(descriptor)


def _temp_name(name: str) -> str:
    return f".{name}.{os.urandom(_TEMP_RANDOM_LENGTH // 2).hex()}.tmp"


def _unchanged(dir_fd: int, name: str, expected: bytes | None) -> bool:
    limit = 0 if expected is None else len(expected)
    try:
        current = read_private(dir_fd, name, limit)
    except NotUnderstood:
        return False
    if expected is None:
        return current is None
    return current is not None and current.data == expected


def write_atomic(dir_fd: int, name: str, data: bytes, expected: bytes | None) -> None:
    """Replace ``name`` atomically if it still holds ``expected`` (None means absent).

    Order: exclusive temp 0600, full write, fsync(file), compare, rename, fsync(directory).
    A failure removes the writer's own temp file. A mismatch raises ChangedOnDisk.
    """
    _require_name(name)
    temp = _temp_name(name)
    descriptor = ops.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(data)
            while remaining:
                written = ops.write(descriptor, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, "write made no progress")
                remaining = remaining[written:]
            ops.fsync_file(descriptor)
        finally:
            os.close(descriptor)
        if not _unchanged(dir_fd, name, expected):
            raise ChangedOnDisk("destination changed since it was read")
        ops.rename(temp, name, dir_fd)
        ops.fsync_dir(dir_fd)
    except BaseException:
        with suppress(OSError):
            ops.unlink(temp, dir_fd)
        raise


def _list_dir(dir_fd: int) -> list[str]:
    """List through a fresh descriptor: on overlayfs a long-open one may miss newer entries."""
    fresh = os.open(".", _DIRECTORY_FLAGS, dir_fd=dir_fd)
    try:
        return sorted(os.listdir(fresh))
    finally:
        os.close(fresh)


def remove_stale_own_temps(
    dir_fd: int, names: Iterable[str], older_than_seconds: float, *, euid: int | None = None
) -> list[str]:
    """Remove this module's own temp files of ``names`` that are older than the threshold."""
    patterns = [
        re.compile(rf"\.{re.escape(name)}\.[0-9a-f]{{{_TEMP_RANDOM_LENGTH}}}\.tmp")
        for name in names
    ]
    cutoff = time.time() - older_than_seconds
    owner = os.geteuid() if euid is None else euid
    removed: list[str] = []
    for entry in _list_dir(dir_fd):
        if not any(pattern.fullmatch(entry) for pattern in patterns):
            continue
        try:
            status = os.stat(entry, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(status.st_mode) or status.st_uid != owner:
            continue
        if status.st_mtime > cutoff:
            continue
        with suppress(FileNotFoundError):
            ops.unlink(entry, dir_fd)
        removed.append(entry)
    return removed


# ---------------------------------------------------------------------------------------------
# Carrier lock
# ---------------------------------------------------------------------------------------------

_held_guard = threading.Lock()
_held: set[tuple[int, int]] = set()


class Lease:
    """Proof that the carrier lock is held. Later host APIs take it as a parameter."""

    def __init__(self, descriptor: int, inode: int) -> None:
        self._descriptor = descriptor
        self.inode = inode
        self.held = True

    @property
    def dir_fd(self) -> int:
        """The locked directory, for ``dir_fd``-relative file access while the lease is held."""
        if not self.held:
            raise CredentialsError("lease is no longer held")
        return self._descriptor


@contextmanager
def carrier_lock(
    dir_path: str | os.PathLike[str], timeout: float = LOCK_TIMEOUT_SECONDS
) -> Iterator[Lease]:
    """flock(LOCK_EX) on the carrier directory itself; no lock file, not re-entrant."""
    descriptor = open_dir(dir_path)
    key: tuple[int, int] | None = None
    lease: Lease | None = None
    try:
        status = os.fstat(descriptor)
        candidate = (status.st_dev, status.st_ino)
        with _held_guard:
            if candidate in _held:
                raise LockReentered("carrier lock is already held by this process")
            _held.add(candidate)
            key = candidate
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout("carrier lock not obtained in time") from None
                time.sleep(_LOCK_POLL_SECONDS)
        lease = Lease(descriptor, status.st_ino)
        yield lease
    finally:
        if lease is not None:
            lease.held = False
        if key is not None:
            with _held_guard:
                _held.discard(key)
        os.close(descriptor)


# ---------------------------------------------------------------------------------------------
# OpenCode runner
# ---------------------------------------------------------------------------------------------

_DISABLE_ENVIRONMENT = {
    "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
    "OPENCODE_DISABLE_AUTOUPDATE": "true",
    "OPENCODE_DISABLE_MODELS_FETCH": "true",
    "DO_NOT_TRACK": "1",
}
_FORBIDDEN_ENVIRONMENT = ("OPENCODE_DB", "XDG_DATA_HOME")
_READ_CHUNK = 65536


def check_environment() -> None:
    """Refuse an environment that moves the database away from the data volume's default path."""
    for name in _FORBIDDEN_ENVIRONMENT:
        if name in os.environ:
            raise NotUnderstood(f"{name} is set")


def isolated_environment(config_home: str) -> dict[str, str]:
    """The inherited base without ``OPENCODE_CONFIG*``, plus the private config home."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("OPENCODE_CONFIG")
    }
    environment["XDG_CONFIG_HOME"] = config_home
    environment.update(_DISABLE_ENVIRONMENT)
    return environment


def _failed(operation: str, code: int) -> OpenCodeFailed:
    return OpenCodeFailed(f"{operation} failed (rc {code})")


def _timed_out(operation: str) -> OpenCodeFailed:
    return OpenCodeFailed(f"{operation} timed out")


def _not_started(operation: str) -> OpenCodeFailed:
    return OpenCodeFailed(f"{operation} could not start")


def _kill(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(ProcessLookupError, PermissionError):
        process.kill()
    process.wait()


class OpenCode:
    """Runs ``opencode`` from PATH, isolated: no plugins, no MCP connections, no project config.

    argv carries the command, flags and credential IDs only; stdin carries import JSON.
    OpenCode's stderr is discarded, so a failure names the operation and the return code.
    """

    def __init__(
        self, timeout: float = COMMAND_TIMEOUT_SECONDS, executable: str = "opencode"
    ) -> None:
        self.timeout = timeout
        self.executable = executable

    def run(
        self,
        operation: str,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        capture: bool = False,
    ) -> bytes:
        # /tmp is only the parent: mkdtemp creates a fresh 0700 directory under a random name.
        parent = os.environ.get("TMPDIR") or "/tmp"  # nosec B108
        config_home = tempfile.mkdtemp(prefix="djinn-opencode-", dir=parent)
        try:
            return self._run(operation, argv, stdin, capture, config_home)
        finally:
            shutil.rmtree(config_home, ignore_errors=True)

    def _run(
        self,
        operation: str,
        argv: Sequence[str],
        stdin: bytes | None,
        capture: bool,
        config_home: str,
    ) -> bytes:
        try:
            process = subprocess.Popen(
                [self.executable, *argv],
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=config_home,
                env=isolated_environment(config_home),
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            raise _not_started(operation) from None
        deadline = time.monotonic() + self.timeout
        output = bytearray()
        try:
            self._pump(process, operation, stdin, capture, deadline, output)
            try:
                code = process.wait(timeout=max(deadline - time.monotonic(), 0.01))
            except subprocess.TimeoutExpired:
                raise _timed_out(operation) from None
        except BaseException:
            _kill(process)
            raise
        if code != 0:
            raise _failed(operation, code)
        return bytes(output)

    @staticmethod
    def _pump(
        process: subprocess.Popen[bytes],
        operation: str,
        stdin: bytes | None,
        capture: bool,
        deadline: float,
        output: bytearray,
    ) -> None:
        sink = process.stdin
        source = process.stdout if capture else None
        pending = memoryview(stdin) if stdin is not None else None
        if sink is not None:
            os.set_blocking(sink.fileno(), False)
            if not pending:
                sink.close()
                sink = None
        if source is not None:
            os.set_blocking(source.fileno(), False)
        while sink is not None or source is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _timed_out(operation)
            readable = [source] if source is not None else []
            writable = [sink] if sink is not None else []
            ready_read, ready_write, _ = select.select(readable, writable, [], min(remaining, 1.0))
            if sink is not None and ready_write and pending is not None:
                try:
                    written = os.write(sink.fileno(), pending[:_READ_CHUNK])
                except BlockingIOError:
                    written = 0
                except BrokenPipeError:
                    written = len(pending)
                pending = pending[written:]
                if not pending:
                    sink.close()
                    sink = None
            if source is not None and ready_read:
                try:
                    chunk = os.read(source.fileno(), _READ_CHUNK)
                except BlockingIOError:
                    continue
                if not chunk:
                    source.close()
                    source = None
                    continue
                output += chunk
                if len(output) > EXPORT_LIMIT:
                    raise NotUnderstood("export exceeds the size limit")

    def export(self) -> list[Entry]:
        raw = self.run("export", ["auth", "export", "--standalone"], capture=True)
        return parse_entries(raw, EXPORT_LIMIT)


# ---------------------------------------------------------------------------------------------
# The single apply path
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One database change: remove, update (``before`` to ``entry``), add or activate."""

    kind: str
    identity: str
    before: Entry | None = None
    entry: Entry | None = None


class StateKeeper:
    """The host state with its raw bytes, so every write is compare-before-rename."""

    def __init__(self, dir_fd: int, name: str, state: HostState, raw: bytes | None) -> None:
        self.dir_fd = dir_fd
        self.name = name
        self.state = state
        self.raw = raw

    def save(self, state: HostState) -> None:
        self.raw = write_host_state(self.dir_fd, self.name, state, self.raw)
        self.state = state


def _find(export: Iterable[Entry], identity: str) -> Entry | None:
    for entry in export:
        if entry.id == identity:
            return entry
    return None


def _missed(operation: str, identity: str) -> ApplyFailed:
    return ApplyFailed(f"{operation} postcondition missed for {identity}")


def _invalid_step() -> NoReturn:
    raise ApplyFailed("unknown or incomplete step")


def _apply_step(
    runner: OpenCode, keeper: StateKeeper, step: Step, export: Sequence[Entry]
) -> list[Entry]:
    """The only place that changes the OpenCode database; returns the verified new export.

    Each command is followed by a fresh export and a postcondition on the fingerprint without
    ``active`` (for activate: the ID is active). rc 0 with a missed postcondition is a failure.
    """
    identity = step.identity
    if step.kind == "activate":
        runner.run(
            "activate",
            ["api", "--standalone", "credential.activate", "--param", f"credentialID={identity}"],
        )
        after = runner.export()
        activated = _find(after, identity)
        if activated is None or not activated.active:
            raise _missed("activate", identity)
        return after
    if step.kind == "remove":
        runner.run(
            "remove",
            ["api", "--standalone", "credential.remove", "--param", f"credentialID={identity}"],
        )
        after = runner.export()
        if _find(after, identity) is not None:
            raise _missed("remove", identity)
        return after
    winner = step.entry
    if step.kind not in {"update", "add"} or winner is None:
        _invalid_step()
    current_export = list(export)
    if step.kind == "update":
        expected = step.before
        current = _find(current_export, identity)
        if expected is None or current is None or current.fingerprint != expected.fingerprint:
            raise _missed("update", identity)
        if current.active:
            keeper.save(set_reactivate(keeper.state, current.integration_id, identity))
        runner.run(
            "remove",
            ["api", "--standalone", "credential.remove", "--param", f"credentialID={identity}"],
        )
        current_export = runner.export()
        if _find(current_export, identity) is not None:
            raise _missed("remove", identity)
    alone = all(item.integration_id != winner.integration_id for item in current_export)
    payload = materialize_entries([winner], {winner.integration_id: identity} if alone else {})
    runner.run("import", ["auth", "import", "--standalone"], stdin=payload)
    after = runner.export()
    imported = _find(after, identity)
    if imported is None or imported.fingerprint != winner.fingerprint:
        raise _missed(step.kind, identity)
    return after


# ---------------------------------------------------------------------------------------------
# Carrier reading and publication
# ---------------------------------------------------------------------------------------------

_COPY_STAMP = r"[0-9]{8}-[0-9]{6}-[A-Z2-7]{0,7}"
_ENTRIES_COPY = re.compile(rf"credentials\.sync-conflict-{_COPY_STAMP}\.json")
_TOMBSTONES_COPY = re.compile(rf"credentials\.removed\.sync-conflict-{_COPY_STAMP}\.json")


class ConflictCopy(NamedTuple):
    name: str
    inode: int
    data: bytes
    entries: tuple[Entry, ...]
    tombstones: tuple[str, ...]


class CarrierRead(NamedTuple):
    file_raw: bytes | None
    entries: list[Entry]
    removed_raw: bytes | None
    tombstones: list[str]
    copies: list[ConflictCopy]
    invalid: list[str]


def read_carrier(dir_fd: int) -> CarrierRead:
    """Both files plus their valid conflict copies. An invalid copy is reported, not fatal."""
    file_snapshot = read_private(dir_fd, CREDENTIALS_NAME, FILE_LIMIT)
    removed_snapshot = read_private(dir_fd, TOMBSTONES_NAME, FILE_LIMIT)
    entries = parse_entries(file_snapshot.data) if file_snapshot is not None else []
    tombstones = parse_tombstones(removed_snapshot.data) if removed_snapshot is not None else []
    copies: list[ConflictCopy] = []
    invalid: list[str] = []
    for name in _list_dir(dir_fd):
        holds_entries = _ENTRIES_COPY.fullmatch(name) is not None
        if not holds_entries and _TOMBSTONES_COPY.fullmatch(name) is None:
            continue
        try:
            snapshot = read_private(dir_fd, name, FILE_LIMIT)
            if snapshot is None:
                continue
            copies.append(
                ConflictCopy(
                    name,
                    snapshot.inode,
                    snapshot.data,
                    tuple(parse_entries(snapshot.data)) if holds_entries else (),
                    () if holds_entries else tuple(parse_tombstones(snapshot.data)),
                )
            )
        except CredentialsError:
            invalid.append(name)
    return CarrierRead(
        file_snapshot.data if file_snapshot is not None else None,
        entries,
        removed_snapshot.data if removed_snapshot is not None else None,
        tombstones,
        copies,
        invalid,
    )


class Published(NamedTuple):
    result: MergeResult
    read: CarrierRead
    wrote_tombstones: bool
    wrote_entries: bool


def _fingerprints(entries: Iterable[Entry]) -> dict[str, str]:
    return {entry.id: entry.fingerprint for entry in entries}


def publish(
    dir_fd: int, export: Sequence[Entry], *, keep_file_selection: bool = False
) -> Published:
    """Merge the export, the file and the valid copies, then make the result durable.

    Tombstones first, then entries, each only on a change outside ``active``. A destination that
    changed under us (a mirror that ignores the lock) is re-read and re-merged, at most 3 times.
    ``keep_file_selection`` lets the file's own ``active`` flags win over the export's.
    """
    exported = {entry.integration_id: entry.id for entry in export if entry.active}
    for _ in range(PUBLISH_ATTEMPTS):
        read = read_carrier(dir_fd)
        selection = exported
        if keep_file_selection:
            selection = {
                **exported,
                **{entry.integration_id: entry.id for entry in read.entries if entry.active},
            }
        result = merge(
            [
                list(export),
                read.entries,
                *[list(copy.entries) for copy in read.copies],
            ],
            [read.tombstones, *[copy.tombstones for copy in read.copies]],
        )
        tombstone_data = materialize_tombstones(result.tombstones)
        entry_data = materialize_entries(result.entries.values(), selection)
        write_tombstones = set(read.tombstones) != set(result.tombstones)
        write_entries = _fingerprints(read.entries) != _fingerprints(result.entries.values())
        try:
            if write_tombstones:
                write_atomic(dir_fd, TOMBSTONES_NAME, tombstone_data, read.removed_raw)
            if write_entries:
                write_atomic(dir_fd, CREDENTIALS_NAME, entry_data, read.file_raw)
        except ChangedOnDisk:
            continue
        return Published(result, read, write_tombstones, write_entries)
    raise ChangedOnDisk("carrier changed during publication in every attempt")


# ---------------------------------------------------------------------------------------------
# Plan and selection
# ---------------------------------------------------------------------------------------------


def plan(result: MergeResult, export: Sequence[Entry], state: HostState) -> list[Step]:
    """remove, then update, then add; a value hidden as (id, fp) is never added or updated."""
    present = {entry.id: entry for entry in export}
    steps = [
        Step("remove", identity) for identity in sorted(present) if identity in result.tombstones
    ]
    winners = sorted(result.entries.items())
    for identity, winner in winners:
        current = present.get(identity)
        if (
            current is not None
            and current.fingerprint != winner.fingerprint
            and not is_hidden(state, identity, winner.fingerprint)
        ):
            steps.append(Step("update", identity, current, winner))
    for identity, winner in winners:
        if identity not in present and not is_hidden(state, identity, winner.fingerprint):
            steps.append(Step("add", identity, None, winner))
    return steps


def next_activation(state: HostState, export: Sequence[Entry]) -> str | None:
    """The ID to activate next: a present ``reactivate`` target first, else the largest ID of an
    integration without an active account."""
    by_id = {entry.id: entry for entry in export}
    for _integration, identity in sorted(state.reactivate.items()):
        target = by_id.get(identity)
        if target is not None and not target.active:
            return identity
    members: dict[str, list[Entry]] = {}
    for entry in export:
        members.setdefault(entry.integration_id, []).append(entry)
    for integration in sorted(members):
        if not any(entry.active for entry in members[integration]):
            return max(entry.id for entry in members[integration])
    return None


def confirmation_matches(result: MergeResult, export: Sequence[Entry], state: HostState) -> bool:
    """The export equals the result minus the hidden values, per ID and without ``active``."""
    present = _fingerprints(export)
    if not set(present) <= set(result.entries):
        return False
    for identity, winner in result.entries.items():
        if is_hidden(state, identity, winner.fingerprint):
            continue
        if present.get(identity) != winner.fingerprint:
            return False
    return True


# ---------------------------------------------------------------------------------------------
# Conflict copies: private backup, then removal
# ---------------------------------------------------------------------------------------------

_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")


def mount_points(mountinfo: str | os.PathLike[str] = "/proc/self/mountinfo") -> frozenset[str]:
    points: set[str] = set()
    with open(mountinfo, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            fields = line.split(" ")
            if len(fields) > 4:
                points.add(
                    _MOUNTINFO_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), fields[4])
                )
    return frozenset(points)


def open_backup_dir(path: str | os.PathLike[str]) -> int:
    """The private backup directory (0700), opened without following a symlink at its end."""
    absolute = os.path.abspath(path)
    parent, leaf = os.path.split(absolute)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        try:
            descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
            ops.fsync_dir(parent_fd)
            descriptor = os.open(leaf, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise Refused("backup directory is a symlink or not a directory") from error
            raise
    finally:
        os.close(parent_fd)
    try:
        status = os.fstat(descriptor)
        if status.st_uid != os.geteuid():
            raise Refused("backup directory belongs to another user")
        if status.st_mode & 0o077:
            os.fchmod(descriptor, 0o700)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def backup_copy(backup_fd: int, copy: ConflictCopy) -> None:
    """Make the copy durable in the backup directory; the name carries a content digest."""
    digest = hashlib.sha256(copy.data).hexdigest()[:16]
    name = f"{copy.name[: -len('.json')]}.{digest}.json"
    existing = read_private(backup_fd, name, FILE_LIMIT)
    if existing is None:
        write_atomic(backup_fd, name, copy.data, None)
    elif existing.data == copy.data:
        ops.fsync_dir(backup_fd)
    else:
        raise ChangedOnDisk("a different backup holds the name")


def remove_copy(
    dir_fd: int,
    carrier_path: str,
    copy: ConflictCopy,
    points: frozenset[str] | None,
) -> bool:
    """Unlink a backed-up copy: matching name, no mount point, still the inode that was read."""
    name = copy.name
    if _ENTRIES_COPY.fullmatch(name) is None and _TOMBSTONES_COPY.fullmatch(name) is None:
        return False
    if points is None or os.path.join(carrier_path, name) in points:
        return False
    try:
        status = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if status.st_dev != os.fstat(dir_fd).st_dev:
        return False
    if not stat.S_ISREG(status.st_mode) or status.st_ino != copy.inode:
        return False
    ops.unlink(name, dir_fd)
    ops.fsync_dir(dir_fd)
    return True


# ---------------------------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------------------------


@dataclass
class SyncReport:
    mode: str = ""
    wrote_tombstones: bool = False
    wrote_entries: bool = False
    hidden: int = 0
    removed: int = 0
    updated: int = 0
    added: int = 0
    activated: int = 0
    copies_removed: int = 0
    copies_kept: list[str] = field(default_factory=lambda: cast(list[str], []))
    copies_invalid: list[str] = field(default_factory=lambda: cast(list[str], []))
    conflicts: list[str] = field(default_factory=lambda: cast(list[str], []))

    def lines(self) -> list[str]:
        lines = [
            f"sync {self.mode}: tombstones written={int(self.wrote_tombstones)}"
            f" entries written={int(self.wrote_entries)} hidden={self.hidden}"
            f" removed={self.removed} updated={self.updated} added={self.added}"
            f" activated={self.activated} conflict copies removed={self.copies_removed}"
        ]
        lines += [f"conflicting values for {item}" for item in self.conflicts]
        lines += [f"conflict copy is invalid and stays: {name}" for name in self.copies_invalid]
        lines += [f"conflict copy stays, backup retried next time: {n}" for n in self.copies_kept]
        return lines


def _database_inode(path: str | os.PathLike[str]) -> int | None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode):
        raise Refused("database is not a regular file")
    return status.st_ino


def _open_state_dir(path: str) -> int:
    """The host-state directory. A fresh data volume may lack it; OpenCode would create it too."""
    try:
        return open_dir(path)
    except FileNotFoundError:
        os.makedirs(path, 0o700, exist_ok=True)
        return open_dir(path)


def _adjust(
    runner: OpenCode,
    keeper: StateKeeper,
    published: Published,
    export: list[Entry],
    report: SyncReport,
) -> list[Entry]:
    current = export
    for step in plan(published.result, export, keeper.state):
        current = _apply_step(runner, keeper, step, current)
        report.removed += step.kind == "remove"
        report.updated += step.kind == "update"
        report.added += step.kind == "add"
    while (target := next_activation(keeper.state, current)) is not None:
        current = _apply_step(runner, keeper, Step("activate", target), current)
        report.activated += 1
    confirmed = runner.export()
    if not confirmation_matches(published.result, confirmed, keeper.state):
        raise ApplyFailed("confirming export differs from the merged result")
    keeper.save(confirm(keeper.state, confirmed))
    return confirmed


def _consume_copies(
    carrier: str,
    copies: Sequence[ConflictCopy],
    backup: str,
    mountinfo: str,
    lock_timeout: float,
    report: SyncReport,
) -> None:
    if not copies:
        return
    with carrier_lock(carrier, lock_timeout) as lease:
        real_carrier = os.path.realpath(carrier)
        try:
            points: frozenset[str] | None = mount_points(mountinfo)
        except OSError:
            points = None
        backup_fd: int | None = None
        try:
            for copy in copies:
                try:
                    if backup_fd is None:
                        backup_fd = open_backup_dir(backup)
                    backup_copy(backup_fd, copy)
                    if remove_copy(lease.dir_fd, real_carrier, copy, points):
                        report.copies_removed += 1
                    else:
                        report.copies_kept.append(copy.name)
                except (CredentialsError, OSError):
                    report.copies_kept.append(copy.name)
        finally:
            if backup_fd is not None:
                os.close(backup_fd)


def sync(
    carrier: str,
    database: str,
    host_state: str,
    conflict_backup: str,
    mode: str,
    *,
    executable: str = "opencode",
    command_timeout: float = COMMAND_TIMEOUT_SECONDS,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    mountinfo: str = "/proc/self/mountinfo",
) -> SyncReport:
    """One adjustment between the OpenCode database and the carrier."""
    if mode not in MODES:
        raise NotUnderstood("unknown mode")
    check_environment()
    report = SyncReport(mode=mode)
    runner = OpenCode(command_timeout, executable)
    state_dir, state_name = os.path.split(os.path.abspath(host_state))
    _database_inode(database)
    carrier_fd = open_dir(carrier)
    try:
        state_fd = _open_state_dir(state_dir)
        try:
            read_carrier(carrier_fd)
            state, raw = read_host_state(state_fd, state_name)
            export = runner.export()
            inode = _database_inode(database)
            if inode is None:
                raise NotUnderstood("database is missing after the export")
            state = unhide_present(bind(state, inode), export)
            lost = vanished(state.seen, export)
            report.hidden = len(lost)
            keeper = StateKeeper(state_fd, state_name, state, raw)
            keeper.save(hide(state, lost))
            with carrier_lock(carrier, lock_timeout) as lease:
                remove_stale_own_temps(
                    lease.dir_fd, [CREDENTIALS_NAME, TOMBSTONES_NAME], STALE_TEMP_SECONDS
                )
                published = publish(lease.dir_fd, export)
            report.wrote_tombstones = published.wrote_tombstones
            report.wrote_entries = published.wrote_entries
            report.copies_invalid = list(published.read.invalid)
            report.conflicts = [
                f"{identity} ({published.result.entries[identity].integration_id})"
                for identity in published.result.conflicts
            ]
            if mode != "final":
                _adjust(runner, keeper, published, export, report)
        finally:
            os.close(state_fd)
    finally:
        os.close(carrier_fd)
    _consume_copies(
        carrier, published.read.copies, conflict_backup, mountinfo, lock_timeout, report
    )
    return report


# ---------------------------------------------------------------------------------------------
# Host APIs: seed, union-only return, tombstones, findings
# ---------------------------------------------------------------------------------------------


def _check_lease(carrier_dir: str | os.PathLike[str], lease: Lease) -> int:
    """The directory descriptor of ``lease``, after proving that it locks ``carrier_dir``."""
    dir_fd = lease.dir_fd
    held = os.fstat(dir_fd)
    named = os.stat(carrier_dir)
    if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
        raise CredentialsError("lease belongs to another directory")
    return dir_fd


def _own_temp_patterns() -> list[re.Pattern[str]]:
    return [
        re.compile(rf"\.{re.escape(name)}\.[0-9a-f]{{{_TEMP_RANDOM_LENGTH}}}\.tmp")
        for name in (CREDENTIALS_NAME, TOMBSTONES_NAME)
    ]


def _begin_write(carrier_dir: str | os.PathLike[str], lease: Lease) -> int:
    dir_fd = _check_lease(carrier_dir, lease)
    remove_stale_own_temps(dir_fd, [CREDENTIALS_NAME, TOMBSTONES_NAME], STALE_TEMP_SECONDS)
    return dir_fd


def _merged_carrier(read: CarrierRead) -> MergeResult:
    return merge(
        [read.entries, *[list(copy.entries) for copy in read.copies]],
        [read.tombstones, *[copy.tombstones for copy in read.copies]],
    )


def materialize_seed(carrier_dir: str | os.PathLike[str], lease: Lease) -> bytes:
    """The join of the file and its valid copies minus all tombstones, in export format.

    Raises NotUnderstood for an invalid carrier file; the caller then starts without a seed.
    """
    read = read_carrier(_check_lease(carrier_dir, lease))
    selection = {entry.integration_id: entry.id for entry in read.entries if entry.active}
    return materialize_entries(_merged_carrier(read).entries.values(), selection)


class MergeBackSummary(NamedTuple):
    """Counts only."""

    added: int
    updated: int
    tombstoned: int
    wrote_entries: bool
    wrote_tombstones: bool


def merge_back(
    carrier_dir: str | os.PathLike[str], final_export: bytes, lease: Lease
) -> MergeBackSummary:
    """Union of ``final_export`` with the carrier state; tombstones are respected, never created."""
    dir_fd = _begin_write(carrier_dir, lease)
    incoming = parse_entries(final_export, EXPORT_LIMIT)
    published = publish(dir_fd, incoming, keep_file_selection=True)
    before = _fingerprints(published.read.entries)
    after = _fingerprints(published.result.entries.values())
    return MergeBackSummary(
        added=sum(1 for identity in after if identity not in before),
        updated=sum(
            1 for identity, fp in after.items() if identity in before and before[identity] != fp
        ),
        tombstoned=sum(1 for item in incoming if item.id in published.result.tombstones),
        wrote_entries=published.wrote_entries,
        wrote_tombstones=published.wrote_tombstones,
    )


class TombstoneReport(NamedTuple):
    """IDs only: ``added`` are new tombstones, ``unknown`` were found in neither file nor copy."""

    added: tuple[str, ...]
    unknown: tuple[str, ...]
    total: int


def _known_ids(read: CarrierRead) -> set[str]:
    return {entry.id for entry in read.entries} | {
        entry.id for copy in read.copies for entry in copy.entries
    }


def _require_ids(identities: Iterable[str]) -> set[str]:
    result = set(identities)
    if not all(_is_id(identity) for identity in result):
        raise NotUnderstood("an id is not valid")
    return result


def _add_tombstones(dir_fd: int, select: Callable[[CarrierRead], set[str]]) -> TombstoneReport:
    """Union ``select(read)`` into the tombstone file, atomically; the entries file stays."""
    for _ in range(PUBLISH_ATTEMPTS):
        read = read_carrier(dir_fd)
        existing = set(read.tombstones)
        wanted = existing | {item for copy in read.copies for item in copy.tombstones}
        wanted |= select(read)
        try:
            if wanted != existing:
                write_atomic(
                    dir_fd, TOMBSTONES_NAME, materialize_tombstones(wanted), read.removed_raw
                )
        except ChangedOnDisk:
            continue
        known = _known_ids(read)
        return TombstoneReport(
            added=tuple(sorted(wanted - existing)),
            unknown=tuple(sorted(item for item in select(read) if item not in known)),
            total=len(wanted),
        )
    raise ChangedOnDisk("carrier changed during publication in every attempt")


def tombstone_ids(
    carrier_dir: str | os.PathLike[str], identities: Iterable[str], lease: Lease
) -> TombstoneReport:
    """Report the given IDs as removed everywhere. Unknown IDs are allowed and reported."""
    wanted = _require_ids(identities)
    dir_fd = _check_lease(carrier_dir, lease)
    read_carrier(dir_fd)
    remove_stale_own_temps(dir_fd, [CREDENTIALS_NAME, TOMBSTONES_NAME], STALE_TEMP_SECONDS)
    return _add_tombstones(dir_fd, lambda _read: wanted)


def tombstone_all(
    carrier_dir: str | os.PathLike[str], extra_ids: Iterable[str], lease: Lease
) -> TombstoneReport:
    """Tombstones := old tombstones, all IDs of the file and its copies, and ``extra_ids``."""
    extra = _require_ids(extra_ids)
    dir_fd = _check_lease(carrier_dir, lease)
    read_carrier(dir_fd)
    remove_stale_own_temps(dir_fd, [CREDENTIALS_NAME, TOMBSTONES_NAME], STALE_TEMP_SECONDS)
    return _add_tombstones(dir_fd, lambda read: _known_ids(read) | extra)


def clear_carrier_except_tombstones(carrier_dir: str | os.PathLike[str], lease: Lease) -> list[str]:
    """The names to remove from the carrier so that only the tombstone file stays.

    This module deletes nothing: files, conflict copies, temporary files, subdirectories and
    mount points are left to the caller's descriptor-contained clearer, which runs under the same
    lease. An invalid carrier file raises NotUnderstood before any name is returned.
    """
    dir_fd = _check_lease(carrier_dir, lease)
    read_carrier(dir_fd)
    return [name for name in _list_dir(dir_fd) if name != TOMBSTONES_NAME]


# ---------------------------------------------------------------------------------------------
# Audit sessions
# ---------------------------------------------------------------------------------------------

SESSION_PREFIX = "djinn-assistant-"
_SESSION_ID = re.compile(r"[0-9a-f]{32}")
_CREATING = ".creating-"
_DELETING = ".deleting-"
_DJINN_STATE = (".local", "state", "djinn")


def default_audit_root() -> str:
    return os.path.join(os.path.expanduser("~"), *_DJINN_STATE, "opencode-audit")


@dataclass(frozen=True)
class RootPolicy:
    """Where the path rules start. Tests inject a temporary home and a foreign euid."""

    home: str = field(default_factory=lambda: os.path.expanduser("~"))
    euid: int = field(default_factory=os.geteuid)


def _check_ancestor(status: os.stat_result, policy: RootPolicy) -> None:
    if status.st_uid not in {policy.euid, 0}:
        raise Refused("an ancestor belongs to another user")
    if status.st_mode & 0o022:
        raise Refused("an ancestor is writable by group or others")


def _check_private(descriptor: int, status: os.stat_result, policy: RootPolicy) -> None:
    if status.st_uid != policy.euid:
        raise Refused("a private directory belongs to another user")
    if status.st_mode & 0o077:
        os.fchmod(descriptor, 0o700)


def _open_root(root: str | os.PathLike[str], policy: RootPolicy, *, create: bool) -> int | None:
    """Walk from the home directory to ``root`` without following a symlink.

    Ancestors must be the user's or root's and not group- or world-writable. From
    ``~/.local/state/djinn`` on (else from the root itself) directories are the user's, 0700.
    Returns None for a missing root when ``create`` is false.
    """
    home = os.path.abspath(policy.home)
    absolute = os.path.abspath(root)
    relative = os.path.relpath(absolute, home)
    if relative == "." or relative == ".." or relative.startswith(".." + os.sep):
        raise Refused("root is not below the home directory")
    parts = relative.split(os.sep)
    private_at = len(parts) - 1
    if tuple(parts[: len(_DJINN_STATE)]) == _DJINN_STATE:
        private_at = len(_DJINN_STATE) - 1
    descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _check_ancestor(os.fstat(descriptor), policy)
        for index, part in enumerate(parts):
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                os.mkdir(part, 0o700, dir_fd=descriptor)
                ops.fsync_dir(descriptor)
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise Refused("a path component is a symlink or not a directory") from error
                raise
            os.close(descriptor)
            descriptor = child
            status = os.fstat(descriptor)
            if index < private_at:
                _check_ancestor(status, policy)
            else:
                _check_private(descriptor, status, policy)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class Session:
    """A private audit session. Holds the lease (flock on the session directory) until closed."""

    def __init__(self, root: str, identity: str, descriptor: int) -> None:
        self.root = root
        self.id = identity
        self._descriptor = descriptor

    @property
    def path(self) -> str:
        return os.path.join(self.root, self.id)

    @property
    def io_path(self) -> str:
        return os.path.join(self.path, "io")

    @property
    def data_path(self) -> str:
        return os.path.join(self.path, "data")

    @property
    def container_name(self) -> str:
        return SESSION_PREFIX + self.id

    @property
    def held(self) -> bool:
        return self._descriptor >= 0

    def descriptor(self) -> int:
        if self._descriptor < 0:
            raise CredentialsError("session lease is no longer held")
        return self._descriptor

    def close(self) -> None:
        if self._descriptor >= 0:
            descriptor, self._descriptor = self._descriptor, -1
            os.close(descriptor)

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def create_audit_session(
    root: str | os.PathLike[str] | None = None, *, policy: RootPolicy | None = None
) -> Session:
    """New session ``<id>`` with ``io/`` and ``data/``; the lease is taken before the rename."""
    effective = policy or RootPolicy()
    root_text = os.fspath(root) if root is not None else default_audit_root()
    root_fd = _open_root(root_text, effective, create=True)
    if root_fd is None:
        raise CredentialsError("session root could not be created")
    try:
        for _ in range(PUBLISH_ATTEMPTS):
            identity = os.urandom(16).hex()
            temporary = _CREATING + identity
            os.mkdir(temporary, 0o700, dir_fd=root_fd)
            descriptor = os.open(temporary, _DIRECTORY_FLAGS, dir_fd=root_fd)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(descriptor)
                    continue
                os.mkdir("io", 0o700, dir_fd=descriptor)
                os.mkdir("data", 0o700, dir_fd=descriptor)
                ops.fsync_dir(descriptor)
                ops.rename(temporary, identity, root_fd)
                ops.fsync_dir(root_fd)
            except BaseException:
                os.close(descriptor)
                raise
            return Session(os.path.abspath(root_text), identity, descriptor)
        raise CredentialsError("session directory is held by another process")
    finally:
        os.close(root_fd)


def write_seed(session: Session, seed: bytes) -> None:
    """``io/seed.json`` as 0600, fsynced."""
    if len(seed) > FILE_LIMIT:
        raise NotUnderstood("seed exceeds the size limit")
    _write_io_file(session, "seed.json", seed)


def _write_io_file(session: Session, name: str, data: bytes) -> None:
    io_fd = os.open("io", _DIRECTORY_FLAGS, dir_fd=session.descriptor())
    try:
        current = read_private(io_fd, name, EXPORT_LIMIT)
        write_atomic(io_fd, name, data, current.data if current is not None else None)
    finally:
        os.close(io_fd)


def _remove_tree(parent_fd: int, name: str) -> None:
    """Remove ``name`` below ``parent_fd`` without following a symlink."""
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        for child in os.listdir(descriptor):
            status = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode):
                _remove_tree(descriptor, child)
            else:
                os.unlink(child, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)


def _discard_session(root_fd: int, identity: str) -> None:
    """Rename to ``.deleting-<id>`` (atomic, so a kill leaves a sweepable name), then remove."""
    marked = _DELETING + identity
    ops.rename(identity, marked, root_fd)
    ops.fsync_dir(root_fd)
    _remove_tree(root_fd, marked)
    ops.fsync_dir(root_fd)


@dataclass
class RecoveryReport:
    """Session IDs only. ``kept`` pairs an ID with a fixed, value-free reason."""

    recovered: list[str] = field(default_factory=lambda: cast(list[str], []))
    kept: list[tuple[str, str]] = field(default_factory=lambda: cast(list[tuple[str, str]], []))
    removed: list[str] = field(default_factory=lambda: cast(list[str], []))


class _Pending(NamedTuple):
    identity: str
    descriptor: int
    owned: bool
    export: bytes


def _try_lease(root_fd: int, name: str) -> int:
    """Open a session directory and take its lease; BlockingIOError when it is held."""
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.fstat(descriptor).st_nlink == 0:
            raise FileNotFoundError(name)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _data_state(session_fd: int) -> str:
    """absent, empty, database or other, by lstat; the open refuses a symlink or a file."""
    try:
        os.stat("data", dir_fd=session_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "absent"
    descriptor = os.open("data", _DIRECTORY_FLAGS, dir_fd=session_fd)
    try:
        names = os.listdir(descriptor)
        if not names:
            return "empty"
        if "opencode.db" in names and stat.S_ISREG(
            os.stat("opencode.db", dir_fd=descriptor, follow_symlinks=False).st_mode
        ):
            return "database"
        return "other"
    finally:
        os.close(descriptor)


def _read_final(session_fd: int) -> tuple[bool, bytes | None]:
    """(final.json exists, its bytes when valid)."""
    try:
        io_fd = os.open("io", _DIRECTORY_FLAGS, dir_fd=session_fd)
    except FileNotFoundError:
        return False, None
    try:
        try:
            snapshot = read_private(io_fd, "final.json", EXPORT_LIMIT)
        except CredentialsError:
            return True, None
        if snapshot is None:
            return False, None
        try:
            parse_entries(snapshot.data, EXPORT_LIMIT)
        except CredentialsError:
            return True, None
        return True, snapshot.data
    finally:
        os.close(io_fd)


def _recover_one(
    session_fd: int,
    path: str,
    container: str,
    export_db: Callable[[str], bytes],
    container_exists: Callable[[str], bool],
    stop_container: Callable[[str], None] | None,
) -> tuple[bytes | None, str | None]:
    """(export to merge, None) / (None, None) for an empty session / (None, reason) to keep."""
    try:
        exists = container_exists(container)
        if exists and stop_container is not None:
            stop_container(container)
            exists = container_exists(container)
    except Exception:
        return None, "container state unknown"
    if exists:
        return None, "container exists"
    try:
        has_final, final = _read_final(session_fd)
        state = _data_state(session_fd)
    except (CredentialsError, OSError):
        return None, "session layout refused"
    if final is not None:
        return final, None
    if state == "database":
        try:
            exported = export_db(os.path.join(path, "data"))
            parse_entries(exported, EXPORT_LIMIT)
        except Exception:
            return None, "export failed"
        return exported, None
    if has_final:
        return None, "final export invalid"
    if state in {"absent", "empty"}:
        return None, None
    return None, "data holds no database"


def _lock_reason(error: BaseException) -> str:
    if isinstance(error, LockTimeout):
        return "carrier lock busy"
    if isinstance(error, NotUnderstood):
        return "carrier not understood"
    if isinstance(error, Refused):
        return "carrier refused"
    return "merge failed"


def recover_audit_sessions(
    root: str | os.PathLike[str] | None,
    carrier_dir: str | os.PathLike[str],
    export_db: Callable[[str], bytes],
    container_exists: Callable[[str], bool],
    own_session: Session | None = None,
    *,
    stop_container: Callable[[str], None] | None = None,
    policy: RootPolicy | None = None,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> RecoveryReport:
    """The single recovery function: merge the leftovers of audit sessions, then delete them.

    A session whose lease is held (except ``own_session``, whose lease the caller holds) is
    skipped. Exports (``export_db``) run before the carrier lock, which this function takes once
    for all merges; the caller must not hold it. A session is deleted only after its merge was
    published durably.
    """
    effective = policy or RootPolicy()
    report = RecoveryReport()
    root_text = os.fspath(root) if root is not None else default_audit_root()
    root_fd = _open_root(root_text, effective, create=False)
    if root_fd is None:
        return report
    absolute_root = os.path.abspath(root_text)
    leases: list[int] = []
    pending: list[_Pending] = []
    try:
        for name in sorted(os.listdir(root_fd)):
            identity = name.removeprefix(_CREATING).removeprefix(_DELETING)
            if _SESSION_ID.fullmatch(identity) is None:
                continue
            status = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(status.st_mode):
                report.kept.append((identity, "not a directory"))
                continue
            owned = own_session is not None and name == own_session.id and own_session.held
            try:
                descriptor = (
                    own_session.descriptor() if owned and own_session else _try_lease(root_fd, name)
                )
            except BlockingIOError:
                report.kept.append((identity, "in use"))
                continue
            except FileNotFoundError:
                continue
            except (OSError, CredentialsError):
                report.kept.append((identity, "session layout refused"))
                continue
            if not owned:
                leases.append(descriptor)
            if name.startswith(_DELETING):
                _sweep(root_fd, name, report, identity)
                continue
            if name.startswith(_CREATING):
                _sweep_creating(root_fd, name, descriptor, report, identity)
                continue
            exported, reason = _recover_one(
                descriptor,
                os.path.join(absolute_root, name),
                SESSION_PREFIX + name,
                export_db,
                container_exists,
                stop_container,
            )
            if reason is not None:
                report.kept.append((identity, reason))
            elif exported is None:
                _finish(root_fd, name, report, removed=True)
            else:
                pending.append(_Pending(identity, descriptor, owned, exported))
        if pending:
            _merge_pending(root_fd, carrier_dir, pending, report, lock_timeout)
    finally:
        for descriptor in leases:
            os.close(descriptor)
        os.close(root_fd)
    return report


def _finish(root_fd: int, name: str, report: RecoveryReport, *, removed: bool) -> None:
    try:
        _discard_session(root_fd, name)
    except OSError:
        report.kept.append((name, "removal failed"))
        return
    (report.removed if removed else report.recovered).append(name)


def _sweep(root_fd: int, name: str, report: RecoveryReport, identity: str) -> None:
    try:
        _remove_tree(root_fd, name)
        ops.fsync_dir(root_fd)
    except OSError:
        report.kept.append((identity, "removal failed"))
        return
    report.removed.append(identity)


def _sweep_creating(
    root_fd: int, name: str, descriptor: int, report: RecoveryReport, identity: str
) -> None:
    try:
        empty = _data_state(descriptor) in {"absent", "empty"} and not _read_final(descriptor)[0]
    except (CredentialsError, OSError):
        empty = False
    if not empty:
        report.kept.append((identity, "unfinished session holds data"))
        return
    _sweep(root_fd, name, report, identity)


def _merge_pending(
    root_fd: int,
    carrier_dir: str | os.PathLike[str],
    pending: list[_Pending],
    report: RecoveryReport,
    lock_timeout: float,
) -> None:
    try:
        with carrier_lock(carrier_dir, lock_timeout) as lease:
            for item in pending:
                try:
                    merge_back(carrier_dir, item.export, lease)
                except (CredentialsError, OSError) as error:
                    report.kept.append((item.identity, _lock_reason(error)))
                    continue
                _finish(root_fd, item.identity, report, removed=False)
    except (CredentialsError, OSError) as error:
        done = set(report.recovered) | {name for name, _ in report.kept}
        for item in pending:
            if item.identity not in done:
                report.kept.append((item.identity, _lock_reason(error)))


def park_for_restore(
    root: str | os.PathLike[str] | None,
    carrier_dir: str | os.PathLike[str],
    volume_export: bytes,
    lease: Lease,
    *,
    policy: RootPolicy | None = None,
) -> str:
    """Write volume export plus current carrier entries as ``io/final.json`` of a new session.

    Runs before the first destructive step of a restore. Tombstones in effect are respected, so
    a login that was reported removed is not parked. The lease on the session is released again:
    ``recover_audit_sessions`` merges and deletes it later. Returns the session ID.
    """
    dir_fd = _check_lease(carrier_dir, lease)
    incoming = parse_entries(volume_export, EXPORT_LIMIT)
    read = read_carrier(dir_fd)
    selection = {entry.integration_id: entry.id for entry in read.entries if entry.active}
    union = merge(
        [incoming, read.entries, *[list(copy.entries) for copy in read.copies]],
        [read.tombstones, *[copy.tombstones for copy in read.copies]],
    )
    data = materialize_entries(union.entries.values(), selection, EXPORT_LIMIT)
    session = create_audit_session(root, policy=policy)
    try:
        _write_io_file(session, "final.json", data)
    except BaseException:
        with suppress(OSError):
            _abandon(session, policy)
        session.close()
        raise
    identity = session.id
    session.close()
    return identity


def _abandon(session: Session, policy: RootPolicy | None) -> None:
    root_fd = _open_root(session.root, policy or RootPolicy(), create=False)
    if root_fd is not None:
        try:
            _discard_session(root_fd, session.id)
        finally:
            os.close(root_fd)


# ---------------------------------------------------------------------------------------------
# Doctor findings
# ---------------------------------------------------------------------------------------------


class Finding(NamedTuple):
    """Value-free: ``kind`` is fixed; ``subject`` is a file name, an ID or a session ID."""

    kind: str
    subject: str
    detail: str = ""


def _try_parse(dir_fd: int, name: str, entries: bool) -> tuple[list[Entry] | list[str], str | None]:
    try:
        snapshot = read_private(dir_fd, name, FILE_LIMIT)
        if snapshot is None:
            return [], None
        parsed = parse_entries(snapshot.data) if entries else parse_tombstones(snapshot.data)
        return parsed, None
    except CredentialsError as error:
        return [], error.reason


def carrier_findings(
    carrier_dir: str | os.PathLike[str],
    sessions_root: str | os.PathLike[str] | None = None,
    *,
    policy: RootPolicy | None = None,
) -> list[Finding]:
    """Read-only, value-free findings for doctor."""
    findings: list[Finding] = []
    try:
        dir_fd = open_dir(carrier_dir)
    except (OSError, CredentialsError):
        dir_fd = -1
    if dir_fd >= 0:
        try:
            findings += _carrier_findings(dir_fd)
        finally:
            os.close(dir_fd)
    findings += _session_findings(sessions_root, policy or RootPolicy())
    return findings


def _carrier_findings(dir_fd: int) -> list[Finding]:
    findings: list[Finding] = []
    main, reason = _try_parse(dir_fd, CREDENTIALS_NAME, True)
    if reason is not None:
        findings.append(Finding("invalid-file", CREDENTIALS_NAME, reason))
    _, reason = _try_parse(dir_fd, TOMBSTONES_NAME, False)
    if reason is not None:
        findings.append(Finding("invalid-file", TOMBSTONES_NAME, reason))
    by_id = {entry.id: entry for entry in cast(list[Entry], main)}
    temps = _own_temp_patterns()
    for name in _list_dir(dir_fd):
        holds_entries = _ENTRIES_COPY.fullmatch(name) is not None
        if holds_entries or _TOMBSTONES_COPY.fullmatch(name) is not None:
            findings.append(Finding("conflict-copy", name))
            parsed, reason = _try_parse(dir_fd, name, holds_entries)
            if reason is not None:
                findings.append(Finding("invalid-file", name, reason))
            for candidate in parsed if holds_entries else []:
                known = by_id.get(cast(Entry, candidate).id)
                if known is not None and known.fingerprint != cast(Entry, candidate).fingerprint:
                    findings.append(Finding("differing-value", known.id, known.integration_id))
        elif any(pattern.fullmatch(name) for pattern in temps):
            findings.append(Finding("stale-temp", name))
    return findings


def _session_findings(root: str | os.PathLike[str] | None, policy: RootPolicy) -> list[Finding]:
    root_text = os.fspath(root) if root is not None else default_audit_root()
    try:
        root_fd = _open_root(root_text, policy, create=False)
    except (OSError, CredentialsError) as error:
        reason = error.reason if isinstance(error, CredentialsError) else "root not readable"
        return [Finding("audit-root", "", reason)]
    if root_fd is None:
        return []
    try:
        findings: list[Finding] = []
        for name in sorted(os.listdir(root_fd)):
            identity = name.removeprefix(_CREATING).removeprefix(_DELETING)
            if _SESSION_ID.fullmatch(identity) is None:
                continue
            detail = "unfinished" if name != identity else "retained"
            findings.append(Finding("audit-session", identity, detail))
        return findings
    finally:
        os.close(root_fd)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise NotUnderstood("usage error")


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 done, 3 refused (path type or ownership), 1 everything else."""
    parser = _Parser(prog="opencode-credentials")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    sync_parser = commands.add_parser("sync")
    for option in ("carrier", "database", "host-state", "conflict-backup"):
        sync_parser.add_argument(f"--{option}", required=True)
    sync_parser.add_argument("--mode", required=True, choices=MODES)
    try:
        arguments = parser.parse_args(argv)
        report = sync(
            arguments.carrier,
            arguments.database,
            arguments.host_state,
            arguments.conflict_backup,
            arguments.mode,
        )
    except Refused as error:
        _warn(f"refused: {error.reason}")
        return 3
    except CredentialsError as error:
        _warn(f"failed: {error.reason}")
        return 1
    except Exception as error:  # the message of an unexpected error may carry data
        _warn(f"unexpected error: {type(error).__name__}")
        return 1
    for line in report.lines():
        _warn(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
