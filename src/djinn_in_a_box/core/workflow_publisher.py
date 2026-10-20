from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import tomllib
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import cast

CANONICAL_MANIFEST_NAME = ".djinn-config-sync.json"
RUNTIME_MANIFEST_NAME = ".djinn-workflow-state.json"
PUBLISHER_WRITE_ERROR_PREFIX = "workflow publisher write-error: "
PUBLISHER_LOCK_ERROR_PREFIX = "workflow publisher lock-error: "
PUBLISHER_DRIFT_ITEMS_PREFIX = "workflow publisher drift-items: "
PUBLISHER_ITEM_PREFIX = "workflow publisher item: "
MAX_DIAGNOSTIC_ITEMS = 200
CHANGED_DURING_PUBLISH = "changed-during-publish"
_SEALED_FILE_MODE = 0o444
_SEALED_EXECUTABLE_MODE = 0o555


def opencode_plugin_has_default_export(content: bytes) -> bool:
    """Check the v2 entrypoint marker; JavaScript execution is OpenCode's job."""
    try:
        return re.search(
            r"\bexport(?:\s+default\b|\s*\{[^}]*\bas\s+default\s*[,}])", content.decode()
        ) is not None
    except UnicodeDecodeError:
        return False


class DriftClass(StrEnum):
    CLEAN = "clean"
    SOURCE_CHANGED = "source-changed"
    TARGET_DRIFT = "target-drift"
    COLLISION = "collision"
    INVALID_OR_SEMANTIC = "invalid-or-semantic"


EXIT_CODES: Mapping[DriftClass, int] = {
    DriftClass.CLEAN: 0,
    DriftClass.SOURCE_CHANGED: 10,
    DriftClass.TARGET_DRIFT: 11,
    DriftClass.COLLISION: 12,
    DriftClass.INVALID_OR_SEMANTIC: 13,
}


@dataclass(frozen=True, slots=True)
class DriftEntry:
    """One named offender. `path` is relative to the root of its `origin`:
    "target" and "target-manifest" the published root, "seed" the --view tree,
    "canonical-manifest" the canonical root. `key_path` names a carrier key."""

    kind: DriftClass
    path: PurePosixPath
    key_path: tuple[str, ...] | None = None
    origin: str = "target"
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class BackedUpState:
    """State of an item when it was backed up: content (file bytes or carrier
    key value) hash and owner-exec bit; `content_hash` None means missing."""

    content_hash: str | None
    executable: bool = False


@dataclass(frozen=True, slots=True)
class BackedUpEntry:
    entry: DriftEntry
    state: BackedUpState
    backup_path: PurePosixPath | None
    """Relative to the backup directory; None when nothing existed to copy."""


@dataclass(frozen=True, slots=True)
class PublishedFile:
    relative_path: PurePosixPath
    content: bytes
    executable: bool = False


@dataclass(frozen=True, slots=True)
class CarrierFragment:
    carrier_path: PurePosixPath
    key_path: tuple[str, ...]
    value_json: bytes


@dataclass(frozen=True, slots=True)
class WorkflowView:
    source: str
    files: tuple[PublishedFile, ...]
    fragments: tuple[CarrierFragment, ...] = ()
    source_fingerprint: str | None = None
    target_tool: str | None = None
    native_only_paths: tuple[PurePosixPath, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeOnlySpec:
    kind: str
    name: str
    script_path: PurePosixPath
    carrier_path: PurePosixPath | None = None
    event: str | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    drift_class: DriftClass
    changed_paths: tuple[PurePosixPath, ...] = ()
    removed_paths: tuple[PurePosixPath, ...] = ()
    write_error: OSError | None = None
    lock_error: OSError | None = None
    """Separate from write_error on purpose: a lease failure means nothing was
    written, so reporting it as a publish failure would name the wrong operation
    and offer a remedy about writability that does not apply."""
    drift_items: tuple[DriftEntry, ...] = ()
    discarded: tuple[DriftEntry, ...] = ()
    """Accepted target-drift entries a successful publish overwrote or removed."""

    @property
    def success(self) -> bool:
        return self.drift_class is DriftClass.CLEAN


@dataclass(frozen=True, slots=True)
class CanonicalLockLease:
    root: Path
    descriptor: int
    exclusive: bool


@dataclass(frozen=True, slots=True)
class _FileState:
    content_hash: str
    executable: bool


@dataclass(frozen=True, slots=True)
class _FragmentState:
    carrier_path: PurePosixPath
    key_path: tuple[str, ...]
    content_hash: str
    executable: bool = False


@dataclass(frozen=True, slots=True)
class _Manifest:
    source: str
    files: Mapping[PurePosixPath, _FileState]
    fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], _FragmentState]


@dataclass(frozen=True, slots=True)
class ManifestItem:
    path: PurePosixPath
    content_hash: str
    executable: bool
    key_path: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class LeanManifest:
    source: str
    items: tuple[ManifestItem, ...]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    content: bytes
    executable: bool
    mode: int = field(default=0, compare=False)

    @property
    def state(self) -> _FileState:
        return _FileState(_digest(self.content), self.executable)


@dataclass(frozen=True, slots=True)
class _Desired:
    files: Mapping[PurePosixPath, PublishedFile]
    fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], CarrierFragment]
    manifest: _Manifest


@dataclass(frozen=True, slots=True)
class _Preflight:
    files: Mapping[PurePosixPath, _Snapshot | None]
    carrier_outputs: Mapping[PurePosixPath, bytes | None]
    manifest_snapshot: _Snapshot | None
    carrier_raw: Mapping[PurePosixPath, _Snapshot | None]
    accepted: tuple[DriftEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class _Finding:
    entry: DriftEntry
    state: BackedUpState


class PublishError(RuntimeError):
    def __init__(
        self,
        drift_class: DriftClass,
        *,
        lock_error: OSError | None = None,
        items: Sequence[DriftEntry] = (),
    ) -> None:
        super().__init__(drift_class)
        self.drift_class = drift_class
        self.lock_error = lock_error
        self.items = tuple(items)


class ManifestError(ValueError):
    pass


class _ManifestError(ManifestError):
    pass


class _CarrierError(ValueError):
    def __init__(self, key: tuple[str, ...] | None = None) -> None:
        super().__init__()
        self.key = key


_TOML_HEADER = re.compile(rb"^[ \t]*\[\[?")
_TOML_ASSIGNMENT = re.compile(
    rb'^[ \t]*(?P<key>[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')[ \t]*='
)
_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_HEX = frozenset("0123456789abcdef")
_TOOLS = frozenset({"claude", "codex", "opencode"})
NATIVE_ONLY_SPEC_MATRIX: Mapping[str, tuple[NativeOnlySpec, ...]] = {
    "claude": (
        NativeOnlySpec(
            "hook",
            "startup",
            PurePosixPath("scripts/session-start-status.py"),
            PurePosixPath("settings.json"),
            "SessionStart",
        ),
        NativeOnlySpec(
            "hook",
            "security",
            PurePosixPath("security_reminder_hook.py"),
            PurePosixPath("settings.json"),
            "PreToolUse",
        ),
        NativeOnlySpec(
            "hook",
            "ready",
            PurePosixPath("ready_notify_hook.py"),
            PurePosixPath("settings.json"),
            "Stop",
        ),
        NativeOnlySpec("command", "codex-review", PurePosixPath("commands/codex-review.md")),
    ),
    "codex": (
        NativeOnlySpec(
            "hook",
            "startup",
            PurePosixPath("scripts/session-start-status.py"),
            PurePosixPath("hooks.json"),
            "SessionStart",
        ),
        NativeOnlySpec(
            "hook",
            "security",
            PurePosixPath("hooks/security_guard.py"),
            PurePosixPath("hooks.json"),
            "PreToolUse",
        ),
        NativeOnlySpec(
            "hook",
            "ready",
            PurePosixPath("hooks/ready_notify.py"),
            PurePosixPath("hooks.json"),
            "Stop",
        ),
    ),
    "opencode": (
        NativeOnlySpec("hook", "startup", PurePosixPath("plugins/session-start-status.js")),
        NativeOnlySpec("hook", "security", PurePosixPath("plugins/security-reminder.js")),
        NativeOnlySpec("hook", "ready", PurePosixPath("plugins/ready-notify.js")),
    ),
}
_NATIVE_ONLY_FRAGMENTS: Mapping[str, frozenset[tuple[PurePosixPath, tuple[str, ...]]]] = {
    tool: frozenset(
        (item.carrier_path, ("hooks", item.event))
        for item in specs
        if item.carrier_path is not None and item.event is not None
    )
    for tool, specs in NATIVE_ONLY_SPEC_MATRIX.items()
}
_BRIDGE_FRAGMENTS: Mapping[str, frozenset[tuple[PurePosixPath, tuple[str, ...]]]] = {
    "claude": frozenset(),
    "codex": frozenset(
        {(PurePosixPath("config.toml"), ("project_doc_fallback_filenames",))}
    ),
    "opencode": frozenset(),
}
_NATIVE_ONLY_PATHS: Mapping[str, frozenset[PurePosixPath]] = {
    tool: frozenset(item.script_path for item in specs)
    for tool, specs in NATIVE_ONLY_SPEC_MATRIX.items()
}
# Subtrees a tool writes into its own source root at runtime. They are not
# workflow sources: they must never be read, decoded, fingerprinted or
# projected, and they must never be deleted either — the writing tool owns them.
# Claude Code syncs account skills into skills/synced/<bucket-id>/<skill>/.
_RUNTIME_RESIDUE_PREFIXES: Mapping[str, frozenset[PurePosixPath]] = {
    "claude": frozenset({PurePosixPath("skills/synced")}),
    "codex": frozenset(),
    "opencode": frozenset(),
}
_PYCACHE = "__pycache__"


def runtime_residue_prefixes(tool: str) -> frozenset[PurePosixPath]:
    """Return the tool-owned runtime subtrees inside that tool's source root."""
    return _RUNTIME_RESIDUE_PREFIXES.get(tool, frozenset())


def is_runtime_residue(relative: PurePosixPath, prefixes: Collection[PurePosixPath]) -> bool:
    """Report whether a source-relative path is generated runtime state.

    `__pycache__` is excluded unconditionally: the interpreter defines it as a
    regenerable bytecode cache, so it is never an authored source anywhere in a
    tree. A deliberately shipped extension module lives outside it and stays.
    Everything else must be named by `prefixes`, so the exclusion stays bound to
    the tool context that proved it.
    """
    return _PYCACHE in relative.parts or any(
        relative == prefix or relative.is_relative_to(prefix) for prefix in prefixes
    )


@contextmanager
def canonical_lock(root: Path, *, exclusive: bool) -> Iterator[CanonicalLockLease]:
    descriptor = _open_directory(root)
    locked = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError as error:
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC, lock_error=error) from error
        locked = True
        yield CanonicalLockLease(root.resolve(), descriptor, exclusive)
    finally:
        _release_directory_lock(descriptor, locked)


def _release_directory_lock(descriptor: int, locked: bool) -> None:
    release_error: OSError | None = None
    if locked:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as error:
            release_error = error
    try:
        os.close(descriptor)
    except OSError as error:
        if release_error is None:
            release_error = error
    if release_error is not None:
        raise PublishError(
            DriftClass.INVALID_OR_SEMANTIC, lock_error=release_error
        ) from release_error


class _ViewEntryError(OSError):
    """A view entry that cannot be published; `relative` names it."""

    def __init__(self, message: str, relative: PurePosixPath) -> None:
        super().__init__(message)
        self.relative = relative


def snapshot_file_view(
    view_root: Path,
    *,
    source: str,
    ignored_paths: Collection[PurePosixPath] = (),
    profile: str | None = None,
    target_tool: str | None = None,
    native_only_paths: Collection[PurePosixPath] = (),
    residue_prefixes: Collection[PurePosixPath] = (),
    item_origin: str | None = None,
) -> WorkflowView:
    """Read a view tree; with `item_origin`, a refused entry is named as an item."""
    try:
        files, fingerprint = _read_file_tree(view_root, ignored_paths, profile, residue_prefixes)
    except OSError as error:
        items = (
            (DriftEntry(DriftClass.INVALID_OR_SEMANTIC, error.relative, origin=item_origin),)
            if isinstance(error, _ViewEntryError) and item_origin is not None
            else ()
        )
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC, items=items) from error
    if not files:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    return WorkflowView(
        source,
        tuple(files),
        source_fingerprint=fingerprint,
        target_tool=target_tool,
        native_only_paths=tuple(native_only_paths),
    )


def publish_workflow_view(
    view: WorkflowView,
    canonical_root: Path,
    target_root: Path,
    manifest_path: Path,
    *,
    canonical_lease: CanonicalLockLease | None = None,
    source_root: Path | None = None,
    source_inputs: Collection[Path] = (),
    ignored_source_paths: Collection[PurePosixPath] = (),
    source_profile: str | None = None,
    source_residue_prefixes: Collection[PurePosixPath] = (),
    preflight_manifest: bytes | None = None,
    accept_drift: Mapping[tuple[PurePosixPath, tuple[str, ...] | None], BackedUpState]
    | None = None,
) -> PublishResult:
    """Publish `view`; `accept_drift` maps (path, key_path) of a backed-up
    target-drift entry to its backed-up state and lets exactly those entries,
    still in that state, be overwritten or removed."""
    try:
        desired = _validate_view(view)
        manifest_relative = _manifest_relative(target_root, manifest_path)
        canonical_target = _same_directory(canonical_root, target_root)
        expected_manifest = CANONICAL_MANIFEST_NAME if canonical_target else RUNTIME_MANIFEST_NAME
        if manifest_relative != PurePosixPath(expected_manifest):
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
        with _canonical_lease(canonical_root, canonical_lease, exclusive=canonical_target) as lease:
            return _publish_with_lease(
                desired,
                view,
                target_root,
                manifest_relative,
                canonical_target,
                lease,
                source_root,
                source_inputs,
                ignored_source_paths,
                source_profile,
                source_residue_prefixes,
                preflight_manifest,
                accept_drift,
            )
    except PublishError as error:
        return PublishResult(
            error.drift_class, lock_error=error.lock_error, drift_items=error.items
        )
    except OSError as error:
        return PublishResult(DriftClass.INVALID_OR_SEMANTIC, write_error=error)
    except (UnicodeError, ValueError):
        return PublishResult(DriftClass.INVALID_OR_SEMANTIC)


publish_view = publish_workflow_view


@contextmanager
def _canonical_lease(
    canonical_root: Path, lease: CanonicalLockLease | None, *, exclusive: bool
) -> Iterator[CanonicalLockLease]:
    """Validate a given lease or take one; a canonical target needs the exclusive one."""
    if lease is not None:
        _validate_lease(lease, canonical_root, exclusive)
        yield lease
        return
    with canonical_lock(canonical_root, exclusive=exclusive) as taken:
        yield taken


def inspect_workflow_view(
    view: WorkflowView,
    canonical_root: Path,
    target_root: Path,
    manifest_path: Path,
    *,
    canonical_lease: CanonicalLockLease | None = None,
) -> PublishResult:
    """Classify a runtime target against `view` exactly as a publish would.

    Never writes, creates a directory, fingerprints the source or commits. A
    missing target root is CLEAN (nothing delivered yet). The canonical tree is
    audited by config sync, so a canonical/target alias is INVALID here.
    """
    try:
        desired = _validate_view(view)
        manifest_relative = _manifest_relative(target_root, manifest_path)
        if (
            _same_directory(canonical_root, target_root)
            or manifest_relative != PurePosixPath(RUNTIME_MANIFEST_NAME)
        ):
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
        with (
            _canonical_lease(canonical_root, canonical_lease, exclusive=False),
            _present_target_lock(target_root, exclusive=False) as target,
        ):
            if target is None:
                return PublishResult(DriftClass.CLEAN)
            target_tool = _target_tool(view)
            prior, snapshot = _load_manifest(
                target, manifest_relative, canonical_target=False, target_tool=target_tool
            )
            _preflight(
                target,
                desired,
                prior,
                snapshot,
                manifest_relative=manifest_relative,
                canonical_target=False,
                target_tool=target_tool,
            )
        return PublishResult(DriftClass.CLEAN)
    except PublishError as error:
        return PublishResult(
            error.drift_class, lock_error=error.lock_error, drift_items=error.items
        )
    except OSError as error:
        return PublishResult(DriftClass.INVALID_OR_SEMANTIC, write_error=error)
    except (UnicodeError, ValueError):
        return PublishResult(DriftClass.INVALID_OR_SEMANTIC)


def backup_target_drift(
    view: WorkflowView,
    canonical_root: Path,
    target_root: Path,
    manifest_path: Path,
    backup_dir_fd: int,
    *,
    canonical_lease: CanonicalLockLease | None = None,
    preflight_manifest: bytes | None = None,
) -> tuple[BackedUpEntry, ...]:
    """Copy the bytes the following publish would overwrite into `backup_dir_fd`.

    Classifies with the prior manifest and desired view that publish will use;
    raises PublishError (nothing written) unless every non-clean entry is
    target drift. File entries are copied as the file, key entries as the whole
    carrier file once, a missing file is recorded without a copy. Never writes
    to the target; any backup write or fsync error propagates as OSError.
    A canonical tree needs an exclusive lease (`preflight_manifest` as for
    publish); a runtime root is read under shared canonical and target locks.
    """
    desired = _validate_view(view)
    manifest_relative = _manifest_relative(target_root, manifest_path)
    canonical_target = _same_directory(canonical_root, target_root)
    expected_manifest = CANONICAL_MANIFEST_NAME if canonical_target else RUNTIME_MANIFEST_NAME
    if manifest_relative != PurePosixPath(expected_manifest):
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    _require_private_directory(backup_dir_fd)
    with _canonical_lease(canonical_root, canonical_lease, exclusive=canonical_target) as lease:
        if canonical_target:
            if not _holds_directory(lease.descriptor, target_root):
                raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
            return _backup_locked(
                desired,
                view,
                lease.descriptor,
                manifest_relative,
                backup_dir_fd,
                preflight_manifest,
                canonical_target=True,
            )
        with _present_target_lock(target_root, exclusive=False) as target:
            if target is None:
                return ()
            return _backup_locked(
                desired,
                view,
                target,
                manifest_relative,
                backup_dir_fd,
                preflight_manifest,
                canonical_target=False,
            )


def _backup_locked(
    desired: _Desired,
    view: WorkflowView,
    target_root: int,
    manifest_relative: PurePosixPath,
    backup_dir: int,
    preflight_manifest: bytes | None,
    *,
    canonical_target: bool,
) -> tuple[BackedUpEntry, ...]:
    target_tool = None if canonical_target else _target_tool(view)
    prior, manifest_snapshot = _load_prior(
        target_root, manifest_relative, preflight_manifest, canonical_target, target_tool
    )
    preflight, findings = _classify(
        target_root,
        desired,
        prior,
        manifest_snapshot,
        manifest_relative=manifest_relative,
        canonical_target=canonical_target,
        target_tool=target_tool,
    )
    _raise_worst([item for item in findings if item.entry.kind is not DriftClass.TARGET_DRIFT])
    touched: list[tuple[int, int]] = []
    backed_up: list[BackedUpEntry] = []
    copied: set[PurePosixPath] = set()
    try:
        for finding in sorted(findings, key=lambda item: _entry_order(item.entry)):
            entry = finding.entry
            source = (
                preflight.files.get(entry.path)
                if entry.key_path is None
                else preflight.carrier_raw.get(entry.path)
            )
            if source is None:
                backed_up.append(BackedUpEntry(entry, finding.state, None))
                continue
            if entry.path not in copied:
                _backup_write(backup_dir, entry.path, source.content, touched)
                copied.add(entry.path)
            backed_up.append(BackedUpEntry(entry, finding.state, entry.path))
        for _depth, directory in sorted(touched, key=lambda item: -item[0]):
            os.fsync(directory)
        if touched or copied:
            os.fsync(backup_dir)
    finally:
        for _depth, directory in touched:
            os.close(directory)
    return tuple(backed_up)


def _entry_order(entry: DriftEntry) -> tuple[str, tuple[str, ...]]:
    return entry.path.as_posix(), entry.key_path or ()


def _require_private_directory(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise PermissionError(
            errno.EACCES, "backup directory must be a private directory owned by this user"
        )


def _backup_write(
    backup_dir: int, relative: PurePosixPath, content: bytes, touched: list[tuple[int, int]]
) -> None:
    parent = backup_dir
    for depth, part in enumerate(relative.parts[:-1], start=1):
        _require_entry_name(part)
        try:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            os.mkdir(part, 0o700, dir_fd=parent)
            os.fsync(parent)
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
        touched.append((depth, child))
        _require_private_directory(child)
        parent = child
    _require_entry_name(relative.name)
    descriptor = os.open(
        relative.name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent,
    )
    try:
        remaining = memoryview(content)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_with_lease(
    desired: _Desired,
    view: WorkflowView,
    target_root: Path,
    manifest_relative: PurePosixPath,
    canonical_target: bool,
    lease: CanonicalLockLease,
    source_root: Path | None,
    source_inputs: Collection[Path],
    ignored_source_paths: Collection[PurePosixPath],
    source_profile: str | None,
    source_residue_prefixes: Collection[PurePosixPath],
    preflight_manifest: bytes | None,
    accept_drift: Mapping[tuple[PurePosixPath, tuple[str, ...] | None], BackedUpState] | None,
) -> PublishResult:
    if canonical_target:
        # The held lease descriptor is the I/O anchor; it must be the target.
        if not _holds_directory(lease.descriptor, target_root):
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
        return _publish_locked(
            desired,
            view,
            lease.descriptor,
            manifest_relative,
            source_root,
            source_inputs,
            ignored_source_paths,
            source_profile,
            source_residue_prefixes,
            preflight_manifest,
            accept_drift,
            canonical_target=True,
        )
    with _target_lock(target_root) as target_descriptor:
        return _publish_locked(
            desired,
            view,
            target_descriptor,
            manifest_relative,
            source_root,
            source_inputs,
            ignored_source_paths,
            source_profile,
            source_residue_prefixes,
            preflight_manifest,
            accept_drift,
            canonical_target=False,
        )


def _load_prior(
    target_root: int,
    manifest_relative: PurePosixPath,
    preflight_manifest: bytes | None,
    canonical_target: bool,
    target_tool: str | None,
) -> tuple[_Manifest | None, _Snapshot | None]:
    if preflight_manifest is None:
        return _load_manifest(
            target_root,
            manifest_relative,
            canonical_target=canonical_target,
            target_tool=target_tool,
        )
    if not canonical_target:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    item = DriftEntry(DriftClass.INVALID_OR_SEMANTIC, manifest_relative, origin=_MANIFEST_ORIGIN)
    manifest_snapshot = _read_item(target_root, manifest_relative, _MANIFEST_ORIGIN)
    if manifest_snapshot is None:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC, items=(item,))
    try:
        prior = _decode_manifest(preflight_manifest, canonical_target=True, target_tool=None)
    except _ManifestError as error:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC, items=(item,)) from error
    return prior, manifest_snapshot


def _publish_locked(
    desired: _Desired,
    view: WorkflowView,
    target_root: int,
    manifest_relative: PurePosixPath,
    source_root: Path | None,
    source_inputs: Collection[Path],
    ignored_source_paths: Collection[PurePosixPath],
    source_profile: str | None,
    source_residue_prefixes: Collection[PurePosixPath],
    preflight_manifest: bytes | None,
    accept_drift: Mapping[tuple[PurePosixPath, tuple[str, ...] | None], BackedUpState] | None,
    *,
    canonical_target: bool,
) -> PublishResult:
    target_tool = None if canonical_target else _target_tool(view)
    prior, manifest_snapshot = _load_prior(
        target_root, manifest_relative, preflight_manifest, canonical_target, target_tool
    )
    expected_fingerprint = view.source_fingerprint
    if source_root is not None:
        current_fingerprint = fingerprint_source_inputs(
            source_root,
            source_inputs,
            ignored_paths=ignored_source_paths,
            profile=source_profile,
            residue_prefixes=source_residue_prefixes,
        )
        if expected_fingerprint is None:
            expected_fingerprint = current_fingerprint
        elif current_fingerprint != expected_fingerprint:
            raise PublishError(DriftClass.SOURCE_CHANGED)
    preflight = _preflight(
        target_root,
        desired,
        prior,
        manifest_snapshot,
        manifest_relative=manifest_relative,
        canonical_target=canonical_target,
        target_tool=target_tool,
        accept_drift=accept_drift,
    )
    return _commit(
        target_root,
        manifest_relative,
        desired,
        preflight,
        prior,
        canonical_target=canonical_target,
        source_root=source_root,
        source_inputs=source_inputs,
        ignored_source_paths=ignored_source_paths,
        source_profile=source_profile,
        source_residue_prefixes=source_residue_prefixes,
        expected_fingerprint=expected_fingerprint,
    )


@contextmanager
def _target_lock(root: Path, *, exclusive: bool = True) -> Iterator[int]:
    with _locked_directory(_open_directory(root), exclusive) as descriptor:
        yield descriptor


@contextmanager
def _present_target_lock(root: Path, *, exclusive: bool) -> Iterator[int | None]:
    """Like `_target_lock`, but an absent root yields None (nothing to lock)."""
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        yield None
        return
    except OSError as error:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC, lock_error=error) from error
    with _locked_directory(descriptor, exclusive) as locked:
        yield locked


@contextmanager
def _locked_directory(descriptor: int, exclusive: bool) -> Iterator[int]:
    locked = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError as error:
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC, lock_error=error) from error
        locked = True
        yield descriptor
    finally:
        _release_directory_lock(descriptor, locked)


def _validate_lease(
    lease: CanonicalLockLease, canonical_root: Path, canonical_target: bool
) -> None:
    if not _holds_directory(lease.descriptor, canonical_root):
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    if canonical_target and not lease.exclusive:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)


def _validate_view(view: WorkflowView) -> _Desired:
    if view.source not in _TOOLS or (
        view.target_tool is not None and view.target_tool not in _TOOLS
    ):
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    files: dict[PurePosixPath, PublishedFile] = {}
    fragments: dict[tuple[PurePosixPath, tuple[str, ...]], CarrierFragment] = {}
    for item in view.files:
        if not _safe_relative(item.relative_path) or item.relative_path in files:
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
        files[item.relative_path] = item
    for fragment in view.fragments:
        key = (fragment.carrier_path, fragment.key_path)
        if (
            not _safe_relative(fragment.carrier_path)
            or fragment.carrier_path.suffix not in {".json", ".toml"}
            or not fragment.key_path
            or any(not part for part in fragment.key_path)
            or key in fragments
            or fragment.carrier_path in files
            or (fragment.carrier_path.suffix == ".toml" and len(fragment.key_path) != 1)
        ):
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
        _fragment_value(fragment)
        fragments[key] = fragment
    file_states = {
        path: _FileState(_digest(item.content), item.executable) for path, item in files.items()
    }
    fragment_states = {
        key: _FragmentState(
            fragment.carrier_path,
            fragment.key_path,
            _value_digest(_fragment_value(fragment)),
        )
        for key, fragment in fragments.items()
    }
    return _Desired(files, fragments, _Manifest(view.source, file_states, fragment_states))


def _manifest_relative(target_root: Path, manifest_path: Path) -> PurePosixPath:
    candidate = manifest_path if manifest_path.is_absolute() else target_root / manifest_path
    try:
        relative = candidate.absolute().relative_to(target_root.absolute())
    except ValueError as error:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC) from error
    result = PurePosixPath(relative.as_posix())
    if not _safe_relative(result):
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    return result


_MANIFEST_ORIGIN = "target-manifest"


def _read_item(
    root: int, path: PurePosixPath, origin: str = "target"
) -> _Snapshot | None:
    """`_read_relative` whose known-path refusals carry the path as an item."""
    try:
        return _read_relative(root, path)
    except PublishError as error:
        if error.items:
            raise
        raise PublishError(
            error.drift_class,
            lock_error=error.lock_error,
            items=(DriftEntry(error.drift_class, path, origin=origin),),
        ) from error


def _file_state_of(current: _Snapshot | None) -> BackedUpState:
    if current is None:
        return BackedUpState(None)
    return BackedUpState(current.state.content_hash, current.executable)


def _preflight(
    target_root: int,
    desired: _Desired,
    prior: _Manifest | None,
    manifest_snapshot: _Snapshot | None,
    *,
    manifest_relative: PurePosixPath | None = None,
    canonical_target: bool = False,
    target_tool: str | None = None,
    accept_drift: Mapping[tuple[PurePosixPath, tuple[str, ...] | None], BackedUpState]
    | None = None,
) -> _Preflight:
    preflight, findings = _classify(
        target_root,
        desired,
        prior,
        manifest_snapshot,
        manifest_relative=manifest_relative,
        canonical_target=canonical_target,
        target_tool=target_tool,
    )
    accepted: list[DriftEntry] = []
    remaining: list[_Finding] = []
    for finding in findings:
        entry = finding.entry
        if (
            entry.kind is DriftClass.TARGET_DRIFT
            and accept_drift is not None
            and accept_drift.get((entry.path, entry.key_path)) == finding.state
        ):
            accepted.append(entry)
        else:
            remaining.append(finding)
    _raise_worst(remaining)
    return replace(preflight, accepted=tuple(accepted))


def _raise_worst(findings: Sequence[_Finding]) -> None:
    failure = _worst_class([finding.entry.kind for finding in findings])
    if failure is not None:
        raise PublishError(
            failure,
            items=tuple(item.entry for item in findings if item.entry.kind is failure),
        )


def _classify(
    target_root: int,
    desired: _Desired,
    prior: _Manifest | None,
    manifest_snapshot: _Snapshot | None,
    *,
    manifest_relative: PurePosixPath | None,
    canonical_target: bool,
    target_tool: str | None,
) -> tuple[_Preflight, list[_Finding]]:
    prior_files: Mapping[PurePosixPath, _FileState] = prior.files if prior else {}
    prior_fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], _FragmentState] = (
        prior.fragments if prior else {}
    )
    files: dict[PurePosixPath, _Snapshot | None] = {}
    findings: list[_Finding] = []
    for path in sorted(set(desired.files) | set(prior_files)):
        if _is_claude_instruction_position(path, canonical_target) and not _manifest_file_is_owned(
            path, canonical_target, target_tool
        ):
            raise PublishError(
                DriftClass.INVALID_OR_SEMANTIC,
                items=(DriftEntry(DriftClass.INVALID_OR_SEMANTIC, path),),
            )
        current = _read_item(target_root, path)
        files[path] = current
        wanted = desired.manifest.files.get(path)
        previous = prior_files.get(path)
        kind: DriftClass | None = None
        if wanted is not None:
            kind = _file_update_class(current, wanted, previous)
        elif previous is not None:
            kind = _file_removal_class(current, previous)
        if kind is not None:
            findings.append(_Finding(DriftEntry(kind, path), _file_state_of(current)))

    carrier_paths = {key[0] for key in desired.fragments} | {key[0] for key in prior_fragments}
    carrier_outputs: dict[PurePosixPath, bytes | None] = {}
    carrier_raw: dict[PurePosixPath, _Snapshot | None] = {}
    for path in sorted(carrier_paths):
        current = _read_item(target_root, path)
        carrier_raw[path] = current
        if current is not None and path.suffix not in {".json", ".toml"}:
            raise PublishError(
                DriftClass.INVALID_OR_SEMANTIC,
                items=(DriftEntry(DriftClass.INVALID_OR_SEMANTIC, path),),
            )
        desired_for_path = _fragments_for(path, desired.fragments)
        prior_for_path = _fragment_states_for(path, prior_fragments)
        raw = current.content if current is not None else None
        try:
            findings.extend(_carrier_findings(path, raw, desired_for_path, prior_for_path))
            carrier_outputs[path] = _merge_carrier(path, raw, desired_for_path, prior_for_path)
        except _CarrierError as error:
            findings.append(
                _Finding(
                    DriftEntry(DriftClass.INVALID_OR_SEMANTIC, path, error.key),
                    _file_state_of(current),
                )
            )

    if manifest_snapshot is not None and prior is None:
        raise PublishError(
            DriftClass.INVALID_OR_SEMANTIC,
            items=()
            if manifest_relative is None
            else (
                DriftEntry(
                    DriftClass.INVALID_OR_SEMANTIC, manifest_relative, origin=_MANIFEST_ORIGIN
                ),
            ),
        )
    return _Preflight(files, carrier_outputs, manifest_snapshot, carrier_raw), findings


def _file_update_class(
    current: _Snapshot | None,
    wanted: _FileState,
    previous: _FileState | None,
) -> DriftClass | None:
    if previous is None:
        if current is None or current.state == wanted:
            return None
        return DriftClass.COLLISION
    if current is None:
        return DriftClass.TARGET_DRIFT
    if current.state in {previous, wanted}:
        return None
    return DriftClass.TARGET_DRIFT


def _file_removal_class(current: _Snapshot | None, previous: _FileState) -> DriftClass | None:
    if current is None or current.state == previous:
        return None
    return DriftClass.TARGET_DRIFT


def _carrier_findings(
    path: PurePosixPath,
    raw: bytes | None,
    desired: Mapping[tuple[str, ...], CarrierFragment],
    previous: Mapping[tuple[str, ...], _FragmentState],
) -> list[_Finding]:
    data = _parse_carrier(path, raw)
    findings: list[_Finding] = []

    def add(kind: DriftClass, key: tuple[str, ...], state: BackedUpState) -> None:
        findings.append(_Finding(DriftEntry(kind, path, key), state))

    for key, fragment in desired.items():
        found, value, blocked = _nested_get(data, key)
        if blocked:
            add(DriftClass.COLLISION, key, BackedUpState(_BLOCKED))
            continue
        wanted = _value_digest(_fragment_value(fragment))
        prior = previous.get(key)
        current = _value_digest(value) if found else None
        if prior is None:
            if found and current != wanted:
                add(DriftClass.COLLISION, key, BackedUpState(current))
        elif not found or current not in {prior.content_hash, wanted}:
            add(DriftClass.TARGET_DRIFT, key, BackedUpState(current))
    for key, state in previous.items():
        if key in desired:
            continue
        found, value, blocked = _nested_get(data, key)
        if blocked:
            add(DriftClass.TARGET_DRIFT, key, BackedUpState(_BLOCKED))
        elif found and _value_digest(value) != state.content_hash:
            add(DriftClass.TARGET_DRIFT, key, BackedUpState(_value_digest(value)))
    return findings


_BLOCKED = "blocked"


def _changed_during_publish(path: PurePosixPath, origin: str = "target") -> PublishError:
    return PublishError(
        DriftClass.TARGET_DRIFT,
        items=(
            DriftEntry(DriftClass.TARGET_DRIFT, path, origin=origin, reason=CHANGED_DURING_PUBLISH),
        ),
    )


def _recheck(
    target_root: int, path: PurePosixPath, before: _Snapshot | None, origin: str = "target"
) -> _Snapshot | None:
    """Re-read a path at its mutation boundary; refuse if it moved since preflight.

    A path absent at preflight must still be absent. This narrows, but cannot
    close, the window between this read and the rename against a writer that
    ignores Djinn's locks.
    """
    current = _read_item(target_root, path, origin)
    if (current is None) != (before is None) or (
        current is not None and before is not None and current.state != before.state
    ):
        raise _changed_during_publish(path, origin)
    return current


def _write_mode(canonical_target: bool, executable: bool = False) -> int | None:
    """Runtime targets are sealed; the canonical tree keeps the default modes."""
    if canonical_target:
        return None
    return _SEALED_EXECUTABLE_MODE if executable else _SEALED_FILE_MODE


def _needs_reseal(canonical_target: bool, current: _Snapshot) -> bool:
    return not canonical_target and bool(current.mode & 0o222)


def _commit(
    target_root: int,
    manifest_relative: PurePosixPath,
    desired: _Desired,
    preflight: _Preflight,
    prior: _Manifest | None,
    *,
    canonical_target: bool = False,
    source_root: Path | None,
    source_inputs: Collection[Path],
    ignored_source_paths: Collection[PurePosixPath],
    source_profile: str | None,
    source_residue_prefixes: Collection[PurePosixPath],
    expected_fingerprint: str | None,
) -> PublishResult:
    changed: list[PurePosixPath] = []
    removed: list[PurePosixPath] = []
    mutation_count = 0
    commit_checked = False

    def allow_mutation() -> None:
        nonlocal commit_checked
        if commit_checked:
            return
        _before_target_commit()
        if source_root is not None and (
            expected_fingerprint is None
            or fingerprint_source_inputs(
                source_root,
                source_inputs,
                ignored_paths=ignored_source_paths,
                profile=source_profile,
                residue_prefixes=source_residue_prefixes,
            )
            != expected_fingerprint
        ):
            raise PublishError(DriftClass.SOURCE_CHANGED)
        commit_checked = True

    for path, item in sorted(desired.files.items()):
        before = preflight.files[path]
        current = _recheck(target_root, path, before)
        if (
            current is not None
            and current.state == desired.manifest.files[path]
            and not _needs_reseal(canonical_target, current)
        ):
            continue
        allow_mutation()
        _recheck(target_root, path, before)
        _replace_relative(
            target_root,
            path,
            item.content,
            item.executable,
            mode=_write_mode(canonical_target, item.executable),
        )
        changed.append(path)
        mutation_count += 1
        _after_target_mutation(mutation_count)
    for path, output in sorted(preflight.carrier_outputs.items()):
        if output is None:
            continue
        before = preflight.carrier_raw[path]
        current = _recheck(target_root, path, before)
        if current is not None and current.content == output:
            continue
        allow_mutation()
        _recheck(target_root, path, before)
        _replace_relative(target_root, path, output, current.executable if current else False)
        changed.append(path)
        mutation_count += 1
        _after_target_mutation(mutation_count)
    prior_files: Mapping[PurePosixPath, _FileState] = prior.files if prior is not None else {}
    for path in sorted(prior_files):
        if path in desired.files:
            continue
        before = preflight.files[path]
        if _recheck(target_root, path, before) is None:
            continue
        allow_mutation()
        _recheck(target_root, path, before)
        # Reaching here, the file is either in its prior state or accepted drift.
        _unlink_relative(target_root, path)
        removed.append(path)
        mutation_count += 1
        _after_target_mutation(mutation_count)
    manifest = _encode_manifest(desired.manifest)
    before = preflight.manifest_snapshot
    current_manifest = _recheck(target_root, manifest_relative, before, _MANIFEST_ORIGIN)
    if (
        current_manifest is None
        or current_manifest.content != manifest
        or _needs_reseal(canonical_target, current_manifest)
    ):
        allow_mutation()
        _recheck(target_root, manifest_relative, before, _MANIFEST_ORIGIN)
        _replace_relative(
            target_root, manifest_relative, manifest, False, mode=_write_mode(canonical_target)
        )
        mutation_count += 1
        _after_target_mutation(mutation_count)
    return PublishResult(
        DriftClass.CLEAN, tuple(changed), tuple(removed), discarded=preflight.accepted
    )


def _unlink_relative(root: int, path: PurePosixPath) -> None:
    parent = _open_parent(root, path, create=False)
    if parent is None:
        return
    try:
        with suppress(FileNotFoundError):
            os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def _merge_carrier(
    path: PurePosixPath,
    raw: bytes | None,
    desired: Mapping[tuple[str, ...], CarrierFragment],
    previous: Mapping[tuple[str, ...], _FragmentState],
) -> bytes | None:
    if path.suffix == ".json":
        return _merge_json(raw, desired, previous)
    return _merge_toml(raw, desired, previous)


def _merge_json(
    raw: bytes | None,
    desired: Mapping[tuple[str, ...], CarrierFragment],
    previous: Mapping[tuple[str, ...], _FragmentState],
) -> bytes | None:
    data = _parse_json(raw)
    original = copy.deepcopy(data)
    for key, fragment in desired.items():
        try:
            _nested_set(data, key, _fragment_value(fragment))
        except _CarrierError:
            raise _CarrierError(key) from None
    for key in previous:
        if key not in desired:
            _nested_remove(data, key)
    if data == original:
        return raw
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _merge_toml(
    raw: bytes | None,
    desired: Mapping[tuple[str, ...], CarrierFragment],
    previous: Mapping[tuple[str, ...], _FragmentState],
) -> bytes | None:
    if raw is None and not desired:
        return None
    output = raw if raw is not None else b""
    data = _parse_toml(output)
    for key, fragment in desired.items():
        name = key[0]
        wanted = _fragment_value(fragment)
        found = name in data
        try:
            if not found or _value_digest(data[name]) != _value_digest(wanted):
                output = _splice_toml(output, name, _toml_literal(wanted))
                data = _parse_toml(output)
        except _CarrierError:
            raise _CarrierError(key) from None
        if name not in data or _value_digest(data[name]) != _value_digest(wanted):
            raise _CarrierError(key)
    for key in previous:
        if key in desired:
            continue
        name = key[0]
        try:
            if name in data:
                output = _splice_toml(output, name, None)
                data = _parse_toml(output)
        except _CarrierError:
            raise _CarrierError(key) from None
        if name in data:
            raise _CarrierError(key)
    return output if output != raw else raw


def _splice_toml(raw: bytes, key: str, value: str | None) -> bytes:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _CarrierError from error
    lines = raw.splitlines(keepends=True)
    header_index = next(
        (index for index, line in enumerate(lines) if _TOML_HEADER.match(line)),
        len(lines),
    )
    assignment_index = next(
        (index for index, line in enumerate(lines[:header_index]) if _toml_line_key(line) == key),
        None,
    )
    assignment = f"{_toml_key(key)} = {value}\n".encode() if value is not None else None
    if assignment_index is None:
        if assignment is None:
            return raw
        if header_index and lines[header_index - 1] and not lines[header_index - 1].endswith(b"\n"):
            lines[header_index - 1] += b"\n"
        lines.insert(header_index, assignment)
    elif assignment is None:
        del lines[assignment_index]
    else:
        lines[assignment_index] = assignment
    output = b"".join(lines)
    try:
        _parse_toml(output)
    except _CarrierError:
        raise
    return output


def _toml_line_key(line: bytes) -> str | None:
    match = _TOML_ASSIGNMENT.match(line)
    if match is None:
        return None
    raw = match.group("key")
    if raw.startswith(b'"'):
        try:
            value: object = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, str) else None
    if raw.startswith(b"'"):
        try:
            return raw[1:-1].decode("utf-8")
        except UnicodeDecodeError:
            return None
    return raw.decode("ascii")


def _toml_key(key: str) -> str:
    return key if _TOML_BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_literal(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _CarrierError
        return repr(value)
    if isinstance(value, list):
        items = cast(list[object], value)
        return "[" + ", ".join(_toml_literal(item) for item in items) + "]"
    if isinstance(value, dict):
        values: list[str] = []
        mapping = cast(dict[object, object], value)
        for key, child in mapping.items():
            if not isinstance(key, str):
                raise _CarrierError
            values.append(f"{_toml_key(key)} = {_toml_literal(child)}")
        return "{ " + ", ".join(values) + " }"
    raise _CarrierError


def _parse_carrier(path: PurePosixPath, raw: bytes | None) -> dict[str, object]:
    return _parse_json(raw) if path.suffix == ".json" else _parse_toml(raw)


def _parse_json(raw: bytes | None) -> dict[str, object]:
    if raw is None:
        return {}
    try:
        parsed = load_strict_json(raw)
    except ManifestError as error:
        raise _CarrierError from error
    return _string_object_mapping(parsed, _CarrierError)


def _parse_toml(raw: bytes | None) -> dict[str, object]:
    if raw is None:
        return {}
    try:
        parsed: object = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise _CarrierError from error
    return _string_object_mapping(parsed, _CarrierError)


def _fragment_value(fragment: CarrierFragment) -> object:
    try:
        value = load_strict_json(fragment.value_json)
        json.dumps(value, allow_nan=False)
    except (ManifestError, TypeError, ValueError) as error:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC) from error
    return value


def _nested_get(data: Mapping[str, object], keys: tuple[str, ...]) -> tuple[bool, object, bool]:
    current = data
    for key in keys[:-1]:
        child = current.get(key)
        if child is None:
            return False, None, False
        if not isinstance(child, dict):
            return False, None, True
        current = cast(dict[str, object], child)
    if not keys or keys[-1] not in current:
        return False, None, False
    return True, current[keys[-1]], False


def _nested_set(data: dict[str, object], keys: tuple[str, ...], value: object) -> None:
    current = data
    for key in keys[:-1]:
        child = current.get(key)
        if child is None:
            created: dict[str, object] = {}
            current[key] = created
            current = created
        elif isinstance(child, dict):
            current = cast(dict[str, object], child)
        else:
            raise _CarrierError
    current[keys[-1]] = value


def _nested_remove(data: dict[str, object], keys: tuple[str, ...]) -> None:
    parents: list[tuple[dict[str, object], str]] = []
    current = data
    for key in keys[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            return
        parents.append((current, key))
        current = cast(dict[str, object], child)
    if keys:
        current.pop(keys[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            return


def _fragments_for(
    path: PurePosixPath,
    fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], CarrierFragment],
) -> dict[tuple[str, ...], CarrierFragment]:
    return {
        key_path: value
        for (carrier_path, key_path), value in fragments.items()
        if carrier_path == path
    }


def _fragment_states_for(
    path: PurePosixPath,
    fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], _FragmentState],
) -> dict[tuple[str, ...], _FragmentState]:
    return {
        key_path: value
        for (carrier_path, key_path), value in fragments.items()
        if carrier_path == path
    }


def _load_manifest(
    target_root: int,
    manifest_relative: PurePosixPath,
    *,
    canonical_target: bool,
    target_tool: str | None,
    origin: str = _MANIFEST_ORIGIN,
) -> tuple[_Manifest | None, _Snapshot | None]:
    snapshot = _read_item(target_root, manifest_relative, origin)
    if snapshot is None:
        return None, None
    try:
        return (
            _decode_manifest(
                snapshot.content,
                canonical_target=canonical_target,
                target_tool=target_tool,
            ),
            snapshot,
        )
    except _ManifestError as error:
        raise PublishError(
            DriftClass.INVALID_OR_SEMANTIC,
            items=(DriftEntry(DriftClass.INVALID_OR_SEMANTIC, manifest_relative, origin=origin),),
        ) from error


def _strict_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError
        result[key] = value
    return result


def load_strict_json(raw: bytes) -> object:
    try:
        return cast(object, json.loads(raw, object_pairs_hook=_strict_object))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
        raise ManifestError from error


def decode_lean_manifest(
    raw: bytes,
    *,
    canonical_target: bool,
    target_tool: str | None,
) -> LeanManifest:
    try:
        parsed = load_strict_json(raw)
    except ManifestError as error:
        raise ManifestError from error
    root = _object(parsed)
    if set(root) != {"source", "items"}:
        raise ManifestError
    source = root["source"]
    values = root["items"]
    if not isinstance(source, str) or source not in _TOOLS or not isinstance(values, list):
        raise ManifestError
    manifest_items = cast(list[object], values)
    items: list[ManifestItem] = []
    files: set[PurePosixPath] = set()
    fragments: set[tuple[PurePosixPath, tuple[str, ...]]] = set()
    for value in manifest_items:
        item = _object(value)
        path_value = item.get("path")
        content_hash = item.get("content_hash")
        executable = item.get("executable")
        if (
            not isinstance(path_value, str)
            or not isinstance(content_hash, str)
            or not isinstance(executable, bool)
            or not _valid_hash(content_hash)
        ):
            raise ManifestError
        path = PurePosixPath(path_value)
        if path_value != path.as_posix() or not _safe_relative(path):
            raise ManifestError
        if "key_path" not in item:
            if (
                set(item) != {"path", "content_hash", "executable"}
                or path in files
                or not _manifest_file_is_owned(path, canonical_target, target_tool)
            ):
                raise ManifestError
            files.add(path)
            items.append(ManifestItem(path, content_hash, executable))
            continue
        key_values = item["key_path"]
        if (
            set(item) != {"path", "key_path", "content_hash", "executable"}
            or not isinstance(key_values, list)
            or not key_values
            or not all(isinstance(key, str) and key for key in cast(list[object], key_values))
            or path.suffix not in {".json", ".toml"}
            or (path.suffix == ".toml" and len(cast(list[object], key_values)) != 1)
            or executable
        ):
            raise ManifestError
        key_path = tuple(cast(list[str], cast(list[object], key_values)))
        if not _manifest_fragment_is_owned(path, key_path, canonical_target, target_tool):
            raise ManifestError
        key = (path, key_path)
        if key in fragments:
            raise ManifestError
        fragments.add(key)
        items.append(ManifestItem(path, content_hash, False, key_path))
    if files & {path for path, _keys in fragments}:
        raise ManifestError
    return LeanManifest(source, tuple(items))


def _decode_manifest(
    raw: bytes,
    *,
    canonical_target: bool,
    target_tool: str | None,
) -> _Manifest:
    try:
        lean = decode_lean_manifest(
            raw,
            canonical_target=canonical_target,
            target_tool=target_tool,
        )
    except ManifestError as error:
        raise _ManifestError from error
    files = {
        item.path: _FileState(item.content_hash, item.executable)
        for item in lean.items
        if item.key_path is None
    }
    fragments = {
        (item.path, item.key_path): _FragmentState(
            item.path,
            item.key_path,
            item.content_hash,
        )
        for item in lean.items
        if item.key_path is not None
    }
    return _Manifest(lean.source, files, fragments)


def _encode_manifest(manifest: _Manifest) -> bytes:
    items: list[dict[str, object]] = []
    for path, state in sorted(manifest.files.items()):
        items.append(
            {
                "path": path.as_posix(),
                "content_hash": state.content_hash,
                "executable": state.executable,
            }
        )
    for (_path, _key), state in sorted(manifest.fragments.items()):
        items.append(
            {
                "path": state.carrier_path.as_posix(),
                "key_path": list(state.key_path),
                "content_hash": state.content_hash,
                "executable": False,
            }
        )
    value = {"source": manifest.source, "items": items}
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _object(value: object) -> dict[str, object]:
    return _string_object_mapping(value, _ManifestError)


def _string_object_mapping(value: object, error_type: type[Exception]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise error_type
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise error_type
    return {cast(str, key): item for key, item in mapping.items()}


# Every access below a root goes through an opened directory descriptor and
# never follows a symlink: the container can write these trees, so a link or a
# swapped name there must not steer a host-side read or write elsewhere.
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_REFUSED_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR})
_TEMPORARY_PREFIX = ".djinn-publisher-"


def _require_entry_name(name: str) -> None:
    if not name or "/" in name or name in {".", ".."}:
        raise PublishError(DriftClass.COLLISION)


def _refuse_link_or_non_directory(error: OSError) -> None:
    if error.errno in _REFUSED_ERRNOS:
        raise PublishError(DriftClass.COLLISION) from error


def _open_child_directory(
    parent: int, name: str, *, create: bool, mode: int = 0o777
) -> int | None:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            return None
    except OSError as error:
        _refuse_link_or_non_directory(error)
        raise
    with suppress(FileExistsError):
        os.mkdir(name, mode, dir_fd=parent)
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as error:
        _refuse_link_or_non_directory(error)
        raise


def _open_parent(root: int, path: PurePosixPath, *, create: bool) -> int | None:
    """Open the parent directory of `path` below `root` component by component.

    Returns None when a component is missing and `create` is false. The caller
    owns the returned descriptor; `root` stays open.
    """
    if not _safe_relative(path):
        raise PublishError(DriftClass.COLLISION)
    descriptor = os.dup(root)
    try:
        for part in path.parts[:-1]:
            child = _open_child_directory(descriptor, part, create=create)
            if child is None:
                return None
            os.close(descriptor)
            descriptor = child
        opened, descriptor = descriptor, -1
        return opened
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_at(directory: int, name: str, *, limit: int | None = None) -> _Snapshot | None:
    """Read a regular file without opening anything else for I/O.

    The entry is pinned with O_PATH, which runs no driver open and does not
    block on a FIFO; only a regular inode is then reopened through /proc, so a
    name swapped after the pin is never read.
    """
    _require_entry_name(name)
    try:
        pin = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(pin)
        if not stat.S_ISREG(info.st_mode):
            raise PublishError(DriftClass.COLLISION)
        if limit is not None and info.st_size > limit:
            raise _PersonalOversizeError
        try:
            content = _read_pinned(pin) if limit is None else _read_pinned(pin, limit=limit)
        except OSError as error:
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC) from error
    finally:
        os.close(pin)
    return _Snapshot(content, bool(info.st_mode & stat.S_IXUSR), info.st_mode & 0o7777)


def _read_pinned(pin: int, *, limit: int | None = None) -> bytes:
    descriptor = os.open(f"/proc/self/fd/{pin}", os.O_RDONLY | os.O_CLOEXEC)
    try:
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(
            descriptor, 1 << 20 if limit is None else min(1 << 20, limit + 1 - total)
        ):
            chunks.append(chunk)
            total += len(chunk)
            if limit is not None and total > limit:
                raise _PersonalOversizeError
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_relative(root: int, path: PurePosixPath) -> _Snapshot | None:
    parent = _open_parent(root, path, create=False)
    if parent is None:
        return None
    try:
        return _read_at(parent, path.name)
    finally:
        os.close(parent)


def _replace_relative(
    root: int, path: PurePosixPath, content: bytes, executable: bool, *, mode: int | None = None
) -> None:
    parent = _open_parent(root, path, create=True)
    if parent is None:  # pragma: no cover - create=True never reports a missing parent
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    try:
        _replace_at(parent, path.name, content, executable, mode=mode)
    finally:
        os.close(parent)


def _replace_at(
    directory: int, name: str, content: bytes, executable: bool, *, mode: int | None = None
) -> None:
    _require_entry_name(name)
    temporary = _TEMPORARY_PREFIX + os.urandom(8).hex()
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    try:
        try:
            remaining = memoryview(content)
            while remaining:
                remaining = remaining[os.write(descriptor, remaining) :]
            os.fchmod(descriptor, mode if mode is not None else (0o755 if executable else 0o644))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)
        raise


def _open_root(path: Path) -> int | None:
    try:
        return os.open(path, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as error:
        _refuse_link_or_non_directory(error)
        raise


def read_regular_file(root: Path, relative: PurePosixPath) -> tuple[bytes, bool] | None:
    """Read `root/relative` without following a symlink at or below `root`.

    Returns the content and owner-executable bit, or None when an entry on the
    path is absent. Raises PublishError: COLLISION for an unsafe relative path,
    a symlinked or non-directory component or a non-regular leaf, and
    INVALID_OR_SEMANTIC when reading the regular file fails. Other OSErrors
    from opening the root or a component propagate.
    """
    snapshot = _read_beneath(root, relative)
    return None if snapshot is None else (snapshot.content, snapshot.executable)


def _read_beneath(root: Path, relative: PurePosixPath) -> _Snapshot | None:
    if not _safe_relative(relative):
        raise PublishError(DriftClass.COLLISION)
    descriptor = _open_root(root)
    if descriptor is None:
        return None
    try:
        return _read_relative(descriptor, relative)
    finally:
        os.close(descriptor)


def _read_file_tree(
    root: Path,
    ignored_paths: Collection[PurePosixPath] = (),
    profile: str | None = None,
    residue_prefixes: Collection[PurePosixPath] = (),
) -> tuple[list[PublishedFile], str]:
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
    except OSError as error:
        raise OSError("View root is not a directory") from error
    files: list[PublishedFile] = []
    # Depth-first: only the directories on the current path stay open, so wide
    # trees cost no descriptors and deep ones no recursion.
    open_directories: list[tuple[int, PurePosixPath, Iterator[str]]] = []
    try:
        directory, prefix = descriptor, PurePosixPath()
        while True:
            open_directories.append((directory, prefix, iter(())))
            subdirectories = _read_directory(
                directory, prefix, files, ignored_paths, profile, residue_prefixes
            )
            open_directories[-1] = (directory, prefix, iter(subdirectories))
            following = _next_subdirectory(open_directories)
            if following is None:
                break
            parent, prefix = following
            try:
                directory = os.open(prefix.name, _DIRECTORY_FLAGS, dir_fd=parent)
            except OSError as error:
                raise _ViewEntryError(
                    "View root contains an unreadable or non-regular entry", prefix
                ) from error
    finally:
        for directory, _prefix, _remaining in open_directories:
            os.close(directory)
    files.sort(key=lambda item: item.relative_path)
    return files, _fingerprint_files(files)


def _next_subdirectory(
    open_directories: list[tuple[int, PurePosixPath, Iterator[str]]],
) -> tuple[int, PurePosixPath] | None:
    """Return the parent fd and path of the next directory; close finished ones."""
    while open_directories:
        directory, prefix, remaining = open_directories[-1]
        name = next(remaining, None)
        if name is not None:
            return directory, prefix / name
        open_directories.pop()
        os.close(directory)
    return None


def _read_directory(
    directory: int,
    prefix: PurePosixPath,
    files: list[PublishedFile],
    ignored_paths: Collection[PurePosixPath],
    profile: str | None,
    residue_prefixes: Collection[PurePosixPath],
) -> list[str]:
    """Read the regular files of one directory; return its subdirectory names."""
    with os.scandir(directory) as entries:
        names = sorted(entry.name for entry in entries)
    subdirectories: list[str] = []
    for name in names:
        relative = prefix / name
        # Skipped before any stat/read so a tool rewriting its own runtime tree
        # cannot make an unrelated view unreadable or shift its fingerprint.
        if is_runtime_residue(relative, residue_prefixes):
            continue
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            subdirectories.append(name)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise _ViewEntryError("View root contains a non-regular file", relative)
        if not _safe_relative(relative):
            raise _ViewEntryError("View root contains an unsafe path", relative)
        if relative in ignored_paths:
            continue
        if profile == "opencode" and not _opencode_owned(relative):
            raise _ViewEntryError("OpenCode view contains an unowned path", relative)
        try:
            snapshot = _read_at(directory, name)
        except PublishError as error:
            raise _ViewEntryError(
                "View root contains an unreadable or non-regular file", relative
            ) from error
        if snapshot is None:
            raise _ViewEntryError("View root contains a non-regular file", relative)
        content = snapshot.content
        if profile == "opencode" and relative in _NATIVE_ONLY_PATHS["opencode"]:
            try:
                content.decode()
            except UnicodeDecodeError as error:
                raise _ViewEntryError("OpenCode plugin is not UTF-8", relative) from error
            if not opencode_plugin_has_default_export(content):
                raise _ViewEntryError(
                    "OpenCode v2 plugin default export marker is missing", relative
                )
        files.append(PublishedFile(relative, content, snapshot.executable))
    return subdirectories


def _fingerprint_tree(
    root: Path,
    ignored_paths: Collection[PurePosixPath] = (),
    profile: str | None = None,
    residue_prefixes: Collection[PurePosixPath] = (),
) -> str:
    try:
        files, fingerprint = _read_file_tree(root, ignored_paths, profile, residue_prefixes)
    except OSError as error:
        raise PublishError(DriftClass.SOURCE_CHANGED) from error
    del files
    return fingerprint


def fingerprint_source_inputs(
    source_root: Path,
    source_inputs: Collection[Path] = (),
    *,
    ignored_paths: Collection[PurePosixPath] = (),
    profile: str | None = None,
    residue_prefixes: Collection[PurePosixPath] = (),
) -> str:
    """Fingerprint the source view and every separately-read native input.

    Native inputs must lie below the canonical root (the parent of
    `source_root`); each one is read from there without following a symlink.
    """
    if not source_inputs:
        return _fingerprint_tree(source_root, ignored_paths, profile, residue_prefixes)
    digest = hashlib.sha256()
    digest.update(b"source-root\0")
    digest.update(
        _fingerprint_tree(source_root, ignored_paths, profile, residue_prefixes).encode()
    )
    anchor = source_root.absolute().parent
    for path in sorted({item.absolute() for item in source_inputs}, key=str):
        digest.update(str(path).encode())
        digest.update(b"\0")
        try:
            relative = PurePosixPath(path.relative_to(anchor).as_posix())
            snapshot = _read_beneath(anchor, relative)
        except (OSError, ValueError, PublishError) as error:
            raise PublishError(DriftClass.SOURCE_CHANGED) from error
        if snapshot is None:
            digest.update(b"missing\0")
            continue
        digest.update(b"file\0")
        digest.update(b"1" if snapshot.executable else b"0")
        digest.update(b"\0")
        digest.update(_digest(snapshot.content).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _fingerprint_files(files: Sequence[PublishedFile]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.relative_path.as_posix().encode())
        digest.update(b"\0")
        digest.update(b"1" if item.executable else b"0")
        digest.update(b"\0")
        digest.update(_digest(item.content).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _open_directory(path: Path) -> int:
    try:
        return os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC, lock_error=error) from error


def _same_directory(first: Path, second: Path) -> bool:
    """Compare two roots without following a final symlink: a link is never a root."""
    try:
        first_info = first.lstat()
        second_info = second.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(first_info.st_mode) and os.path.samestat(first_info, second_info)


def _holds_directory(descriptor: int, path: Path) -> bool:
    try:
        return os.path.samestat(os.fstat(descriptor), path.lstat())
    except OSError:
        return False


def _safe_relative(path: PurePosixPath) -> bool:
    return (
        not path.is_absolute()
        and bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _target_tool(view: WorkflowView) -> str:
    target_tool = view.target_tool or view.source
    if target_tool not in _TOOLS:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    return target_tool


def _manifest_file_is_owned(
    path: PurePosixPath, canonical_target: bool, target_tool: str | None
) -> bool:
    if _is_claude_instruction_position(path, canonical_target):
        return not canonical_target and target_tool == "claude"
    if canonical_target:
        if len(path.parts) < 2 or path.parts[0] not in _TOOLS:
            return False
        return _path_is_owned(path.parts[0], PurePosixPath(*path.parts[1:]))
    return target_tool is not None and _path_is_owned(target_tool, path)


def _is_claude_instruction_position(path: PurePosixPath, canonical_target: bool) -> bool:
    if canonical_target:
        return len(path.parts) == 2 and path.parts[1] == "CLAUDE.md"
    return path == PurePosixPath("CLAUDE.md")


def _manifest_fragment_is_owned(
    path: PurePosixPath,
    key_path: tuple[str, ...],
    canonical_target: bool,
    target_tool: str | None,
) -> bool:
    if canonical_target:
        if len(path.parts) < 2 or path.parts[0] not in _TOOLS:
            return False
        return _fragment_is_owned(path.parts[0], PurePosixPath(*path.parts[1:]), key_path)
    return target_tool is not None and _fragment_is_owned(target_tool, path, key_path)


def _path_is_owned(tool: str, path: PurePosixPath) -> bool:
    value = path.as_posix()
    return _safe_relative(path) and (
        value == "AGENTS.md"
        or len(path.parts) == 2
        and path.parts[0] == "agents"
        and path.suffix == (".toml" if tool == "codex" else ".md")
        or len(path.parts) >= 3
        and path.parts[0] == "skills"
        or tool != "codex"
        and len(path.parts) == 2
        and path.parts[0] == "commands"
        and path.suffix == ".md"
        and (tool == "claude" or path.stem != "codex-review")
        or len(path.parts) >= 2
        and path.parts[0] in {"context", "scripts"}
        or path in _NATIVE_ONLY_PATHS[tool]
    )


def _fragment_is_owned(tool: str, path: PurePosixPath, key_path: tuple[str, ...]) -> bool:
    return (path, key_path) in _NATIVE_ONLY_FRAGMENTS[tool] | _BRIDGE_FRAGMENTS[tool]


def _opencode_owned(path: PurePosixPath) -> bool:
    return _path_is_owned("opencode", path)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _value_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise _CarrierError from error
    return _digest(encoded.encode())


def _valid_hash(value: str) -> bool:
    return len(value) == 64 and all(character in _HEX for character in value)


def _worst_class(classes: Sequence[DriftClass | None]) -> DriftClass | None:
    values = {item for item in classes if item is not None}
    if DriftClass.INVALID_OR_SEMANTIC in values:
        return DriftClass.INVALID_OR_SEMANTIC
    if DriftClass.COLLISION in values:
        return DriftClass.COLLISION
    if DriftClass.TARGET_DRIFT in values:
        return DriftClass.TARGET_DRIFT
    return None


def _after_target_mutation(_count: int) -> None:
    return None


def _before_target_commit() -> None:
    return None


def _canonical_manifest(canonical_root: int) -> _Manifest:
    relative = PurePosixPath(CANONICAL_MANIFEST_NAME)
    manifest, _snapshot = _load_manifest(
        canonical_root,
        relative,
        canonical_target=True,
        target_tool=None,
        origin=_CANONICAL_MANIFEST_ORIGIN,
    )
    if manifest is None:
        raise PublishError(
            DriftClass.INVALID_OR_SEMANTIC,
            items=(
                DriftEntry(
                    DriftClass.INVALID_OR_SEMANTIC, relative, origin=_CANONICAL_MANIFEST_ORIGIN
                ),
            ),
        )
    return manifest


_CANONICAL_MANIFEST_ORIGIN = "canonical-manifest"


def _verify_seed_against_canonical_manifest(
    view: WorkflowView, manifest: _Manifest, profile: str | None
) -> None:
    if profile != "opencode":
        return
    expected = {
        PurePosixPath(*path.parts[1:]): state
        for path, state in manifest.files.items()
        if len(path.parts) > 1 and path.parts[0] == profile
    }
    actual = {
        item.relative_path: _FileState(_digest(item.content), item.executable)
        for item in view.files
        if item.relative_path not in view.native_only_paths
    }
    if view.source != manifest.source or actual != expected:
        items = [
            DriftEntry(DriftClass.SOURCE_CHANGED, path, origin="seed")
            for path in sorted(set(actual) | set(expected))
            if actual.get(path) != expected.get(path)
        ]
        if view.source != manifest.source:
            items.append(
                DriftEntry(
                    DriftClass.SOURCE_CHANGED,
                    PurePosixPath(CANONICAL_MANIFEST_NAME),
                    origin=_CANONICAL_MANIFEST_ORIGIN,
                )
            )
        raise PublishError(DriftClass.SOURCE_CHANGED, items=items)


# Personal OpenCode files are deliberately outside workflow ownership/manifests.
_PERSONAL_LIMIT = 1 << 20


class _PersonalOversizeError(ValueError):
    pass


def _personal_relative(value: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    relative.name.encode("utf-8")  # surrogate-escaped filesystem names are not allowlisted
    if value in {"opencode.json", "cli.json"} or (
        len(relative.parts) == 2
        and relative.parts[0] == "themes"
        and not relative.name.startswith(".")
        and relative.name.endswith(".json")
        and str(relative) == value
    ):
        return relative
    raise ValueError("not a personal carrier path")


def _personal_reason(error: Exception) -> str:
    if isinstance(error, _PersonalOversizeError):
        return "larger than 1 MiB"
    if isinstance(error, PublishError):
        if error.drift_class == DriftClass.COLLISION:
            return "symlink or non-regular entry"
        return "read failed"
    return str(error)


def _personal_root(path: Path, *, create: bool = False) -> int:
    descriptor = _open_root(path)
    if descriptor is None and create:
        path.parent.mkdir(parents=True, exist_ok=True)  # ancestors above the root are trusted
        path.mkdir(exist_ok=True)
        descriptor = _open_root(path)
    if descriptor is None:
        raise FileNotFoundError(f"root does not exist: {path}")
    return descriptor


def _personal_paths(root: int) -> tuple[list[PurePosixPath], list[tuple[str, Exception]]]:
    paths = [PurePosixPath("opencode.json"), PurePosixPath("cli.json")]
    refused: list[tuple[str, Exception]] = []
    try:
        themes = _open_child_directory(root, "themes", create=False)
        if themes is not None:
            try:
                for name in sorted(os.listdir(themes)):
                    try:
                        paths.append(_personal_relative(f"themes/{name}"))
                    except ValueError:
                        continue
            finally:
                os.close(themes)
    except (PublishError, OSError) as error:
        refused.append(("themes", error))
    return paths, refused


def _personal_admit(root: int, relative: PurePosixPath) -> None:
    parent = _open_parent(root, relative, create=False)
    if parent is None:
        return
    try:
        try:
            pin = os.open(relative.name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            return
        try:
            if not stat.S_ISREG(os.fstat(pin).st_mode):
                raise PublishError(DriftClass.COLLISION)
        finally:
            os.close(pin)
    finally:
        os.close(parent)


def _personal_read(root: int, relative: PurePosixPath) -> bytes | None:
    parent = _open_parent(root, relative, create=False)
    if parent is None:
        return None
    try:
        snapshot = _read_at(parent, relative.name, limit=_PERSONAL_LIMIT)
        return None if snapshot is None else snapshot.content
    finally:
        os.close(parent)


def _personal_write(root: int, relative: PurePosixPath, content: bytes) -> None:
    _personal_admit(root, relative)
    parent = _open_parent(root, relative, create=True)
    assert parent is not None
    try:
        _replace_at(parent, relative.name, content, False, mode=0o600)
    finally:
        os.close(parent)


def _personal_refusals(refused: Sequence[tuple[str, Exception]]) -> None:
    for relative, error in refused:
        print(f"refused {relative}: {_personal_reason(error)}", file=sys.stderr)


def _personal_copy(carrier: Path, runtime: Path) -> int:
    # Admission precedes even reading the carrier: a restarted runtime may be unsafe.
    with _personal_opened_root(runtime, create=True) as target:
        paths, refused = _personal_paths(target)
        for relative in paths:
            try:
                _personal_admit(target, relative)
            except (PublishError, OSError) as error:
                refused.append((str(relative), error))
        if refused:
            _personal_refusals(refused)
            return 1
        with _personal_opened_root(carrier) as source:
            paths, refused = _personal_paths(source)
            protected: list[str] = []
            for relative in paths:
                try:
                    content = _personal_read(source, relative)
                    if content is not None:
                        _personal_write(target, relative, content)
                except _PersonalOversizeError:
                    protected.append(str(relative))
                    print(f"skipped {relative}: larger than 1 MiB", file=sys.stderr)
                except (PublishError, OSError) as error:
                    refused.append((str(relative), error))
            _personal_refusals(refused)
            print(json.dumps(protected))
            return int(bool(refused))


@contextmanager
def _personal_opened_root(path: Path, *, create: bool = False) -> Iterator[int]:
    try:
        descriptor = _personal_root(path, create=create)
    except (PublishError, OSError) as error:
        raise ValueError(f"root {path}: {_personal_reason(error)}") from error
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _personal_refusal_kind(error: Exception) -> str:
    if isinstance(error, _PersonalOversizeError) or (
        isinstance(error, PublishError) and error.drift_class == DriftClass.COLLISION
    ):
        return "entry"
    return "io"


def _personal_snapshot(runtime: Path, capture: Path) -> int:
    with _personal_opened_root(runtime) as source:
        capture.mkdir(mode=0o700)  # require a fresh directory; never reuse stale captures
        with _personal_opened_root(capture) as target:
            paths, refused = _personal_paths(source)
            kinds = {rel: _personal_refusal_kind(error) for rel, error in refused}
            for relative in paths:
                try:
                    content = _personal_read(source, relative)
                except (PublishError, OSError, _PersonalOversizeError) as error:
                    refused.append((str(relative), error))
                    kinds[str(relative)] = _personal_refusal_kind(error)
                    continue
                if content is not None:
                    try:
                        _personal_write(target, relative, content)
                    except (PublishError, OSError) as error:
                        refused.append((str(relative), error))
                        kinds[str(relative)] = "io"  # capture storage failures degrade the session
            _personal_refusals(refused)
            print(json.dumps([
                {"relative": rel, "reason": _personal_reason(error), "kind": kinds[rel]}
                for rel, error in refused
            ]))
    return 0


def _personal_reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _personal_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="workflow-publisher personal-carrier")
    operations = parser.add_subparsers(dest="operation", required=True)
    copy_parser = operations.add_parser("copy")
    copy_parser.add_argument("--carrier", required=True, type=Path)
    copy_parser.add_argument("--runtime", required=True, type=Path)
    snapshot_parser = operations.add_parser("snapshot")
    snapshot_parser.add_argument("--runtime", required=True, type=Path)
    snapshot_parser.add_argument("--capture", required=True, type=Path)
    install_parser = operations.add_parser("install")
    install_parser.add_argument("--root", required=True, type=Path)
    install_parser.add_argument("--relative", required=True)
    install_parser.add_argument("--source", required=True, type=Path)
    validate_parser = operations.add_parser("validate")
    validate_parser.add_argument("--source", required=True, type=Path)
    arguments = parser.parse_args(argv)
    relative = "."
    try:
        if arguments.operation == "copy":
            return _personal_copy(arguments.carrier, arguments.runtime)
        if arguments.operation == "snapshot":
            return _personal_snapshot(arguments.runtime, arguments.capture)
        relative = arguments.relative if arguments.operation == "install" else "."
        with _personal_opened_root(arguments.source.parent) as source:
            content = _personal_read(source, PurePosixPath(arguments.source.name))
        if content is None:
            raise ValueError("capture is missing")
        if arguments.operation == "validate":
            json.loads(content.decode("utf-8"), parse_constant=_personal_reject_constant)
            return 0
        path = _personal_relative(relative)
        with _personal_opened_root(arguments.root) as target:
            _personal_write(target, path, content)
        return 0
    except (PublishError, OSError, ValueError, RecursionError) as error:
        _personal_refusals([(relative, error)])
        return 1


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="workflow-publisher")
    parser.add_argument("--view", "--view-dir", dest="view", required=True)
    parser.add_argument("--canonical-root", required=True)
    parser.add_argument("--target", "--target-root", dest="target", required=True)
    parser.add_argument("--manifest", "--manifest-path", dest="manifest", required=True)
    parser.add_argument("--fragment", action="append", default=[])
    parser.add_argument("--ignore", action="append", default=[])
    parser.add_argument("--profile", choices=("opencode",))
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def _write_error_diagnostic(destination: Path, error: OSError) -> str:
    return PUBLISHER_WRITE_ERROR_PREFIX + json.dumps(
        {
            "destination": str(destination),
            "error": error.strerror or "OS error",
        },
        separators=(",", ":"),
    )


def _lock_error_diagnostic(root: Path, error: OSError) -> str:
    return PUBLISHER_LOCK_ERROR_PREFIX + json.dumps(
        {
            "root": str(root),
            "error": error.strerror or "OS error",
        },
        separators=(",", ":"),
    )


def _item_root(entry: DriftEntry, arguments: argparse.Namespace) -> str:
    if entry.origin == "seed":
        return str(arguments.view)
    if entry.origin == _CANONICAL_MANIFEST_ORIGIN:
        return str(arguments.canonical_root)
    return str(arguments.target)


def _drift_item_lines(items: Sequence[DriftEntry], arguments: argparse.Namespace) -> list[str]:
    """The machine-readable line (hosts parse only this) and one human line per item.

    Everything is JSON-escaped, so a control character in a name cannot forge a line.
    """
    listed = items[:MAX_DIAGNOSTIC_ITEMS]
    records = [
        {
            "class": entry.kind.value,
            "origin": entry.origin,
            "root": _item_root(entry, arguments),
            "path": entry.path.as_posix(),
            "key": list(entry.key_path) if entry.key_path is not None else None,
            "reason": entry.reason,
        }
        for entry in listed
    ]
    payload = {"items": records, "total": len(items), "truncated": len(items) > len(listed)}
    lines = [PUBLISHER_DRIFT_ITEMS_PREFIX + json.dumps(payload, separators=(",", ":"))]
    for record in records:
        key = record["key"]
        label = f"{record['root']}/{record['path']}"
        if isinstance(key, list):
            label += "[" + ".".join(key) + "]"
        lines.append(f"{PUBLISHER_ITEM_PREFIX}{record['class']} {json.dumps(label)}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    personal_args = list(sys.argv[1:] if argv is None else argv)
    if personal_args[:1] == ["personal-carrier"]:
        return _personal_main(personal_args[1:])
    arguments = _parse_args(argv)
    lock_error: OSError | None = None
    items: tuple[DriftEntry, ...] = ()
    if arguments.fragment:
        result = PublishResult(DriftClass.INVALID_OR_SEMANTIC)
    else:
        canonical_root = Path(arguments.canonical_root)
        target_root = Path(arguments.target)
        manifest_path = Path(arguments.manifest)
        try:
            canonical_target = _same_directory(canonical_root, target_root)
            ignored_paths = tuple(PurePosixPath(item) for item in arguments.ignore)
            if not all(_safe_relative(item) for item in ignored_paths):
                raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
            with canonical_lock(canonical_root, exclusive=canonical_target) as lease:
                manifest = _canonical_manifest(lease.descriptor)
                view = snapshot_file_view(
                    Path(arguments.view),
                    source=manifest.source,
                    ignored_paths=ignored_paths,
                    profile=arguments.profile,
                    target_tool=arguments.profile,
                    native_only_paths=(
                        _NATIVE_ONLY_PATHS[arguments.profile]
                        if arguments.profile is not None
                        else ()
                    ),
                    item_origin="seed",
                )
                _verify_seed_against_canonical_manifest(view, manifest, arguments.profile)
                if arguments.check:
                    result = inspect_workflow_view(
                        view,
                        canonical_root,
                        target_root,
                        manifest_path,
                        canonical_lease=lease,
                    )
                else:
                    result = publish_workflow_view(
                        view,
                        canonical_root,
                        target_root,
                        manifest_path,
                        canonical_lease=lease,
                        source_root=Path(arguments.view),
                        ignored_source_paths=ignored_paths,
                        source_profile=arguments.profile,
                    )
        except PublishError as error:
            lock_error = error.lock_error
            items = error.items
            result = PublishResult(error.drift_class)
    if not result.success:
        print(f"workflow publisher: {result.drift_class.value}", file=sys.stderr)
        if result.write_error is not None:
            print(
                _write_error_diagnostic(Path(arguments.target), result.write_error),
                file=sys.stderr,
            )
        # Two sources: a PublishError that reached this frame, and one
        # publish_workflow_view already converted into the result field.
        lock_error = lock_error or result.lock_error
        if lock_error is not None:
            print(
                _lock_error_diagnostic(Path(arguments.canonical_root), lock_error),
                file=sys.stderr,
            )
        for line in _drift_item_lines(items or result.drift_items, arguments):
            print(line, file=sys.stderr)
    return EXIT_CODES[result.drift_class]


if __name__ == "__main__":
    raise SystemExit(main())
