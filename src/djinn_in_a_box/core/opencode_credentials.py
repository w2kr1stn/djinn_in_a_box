"""OpenCode credentials as a monotone CRDT over the config root: model, host state, files, lock.

Standalone by design: stdlib only, no imports from ``djinn_in_a_box``, Python 3.11 compatible.
The dev image copies this file and runs it as a script; the host imports it as a module.

Nothing in an exception message or a log line may carry a credential value. Errors name an
operation, a count or an ID, never a label or a value.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import NamedTuple, cast

FILE_LIMIT = 1024 * 1024
EXPORT_LIMIT = 16 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 30.0

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
