from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import tomllib
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import cast

CANONICAL_MANIFEST_NAME = ".djinn-config-sync.json"
RUNTIME_MANIFEST_NAME = ".djinn-workflow-state.json"
LEGACY_DELIVERY_MANIFEST_NAME = ".djinn-workflow-delivery.json"


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


@dataclass(frozen=True, slots=True)
class PublishResult:
    drift_class: DriftClass
    changed_paths: tuple[PurePosixPath, ...] = ()
    removed_paths: tuple[PurePosixPath, ...] = ()

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


class PublishError(RuntimeError):
    def __init__(self, drift_class: DriftClass) -> None:
        super().__init__(drift_class)
        self.drift_class = drift_class


class ManifestError(ValueError):
    pass


class _ManifestError(ManifestError):
    pass


class _CarrierError(ValueError):
    pass


_TOML_HEADER = re.compile(rb"^[ \t]*\[\[?")
_TOML_ASSIGNMENT = re.compile(
    rb'^[ \t]*(?P<key>[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')[ \t]*='
)
_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_HEX = frozenset("0123456789abcdef")
_TOOLS = frozenset({"claude", "codex", "opencode"})
_NATIVE_ONLY_FRAGMENTS: Mapping[str, frozenset[tuple[PurePosixPath, tuple[str, ...]]]] = {
    "claude": frozenset(
        {
            (PurePosixPath("settings.json"), ("hooks", "SessionStart")),
            (PurePosixPath("settings.json"), ("hooks", "PreToolUse")),
            (PurePosixPath("settings.json"), ("hooks", "Stop")),
        }
    ),
    "codex": frozenset(
        {
            (PurePosixPath("hooks.json"), ("hooks", "SessionStart")),
            (PurePosixPath("hooks.json"), ("hooks", "PreToolUse")),
            (PurePosixPath("hooks.json"), ("hooks", "Stop")),
        }
    ),
    "opencode": frozenset(),
}
_BRIDGE_FRAGMENTS: Mapping[str, frozenset[tuple[PurePosixPath, tuple[str, ...]]]] = {
    "claude": frozenset(),
    "codex": frozenset(
        {(PurePosixPath("config.toml"), ("project_doc_fallback_filenames",))}
    ),
    "opencode": frozenset(),
}
_NATIVE_ONLY_PATHS: Mapping[str, frozenset[PurePosixPath]] = {
    "claude": frozenset(
        {
            PurePosixPath("scripts/session-start-status.py"),
            PurePosixPath("security_reminder_hook.py"),
            PurePosixPath("ready_notify_hook.py"),
            PurePosixPath("commands/codex-review.md"),
        }
    ),
    "codex": frozenset(
        {
            PurePosixPath("scripts/session-start-status.py"),
            PurePosixPath("hooks/security_guard.py"),
            PurePosixPath("hooks/ready_notify.py"),
        }
    ),
    "opencode": frozenset(
        {
            PurePosixPath("plugins/session-start-status.js"),
            PurePosixPath("plugins/security-reminder.js"),
            PurePosixPath("plugins/ready-notify.js"),
        }
    ),
}


@contextmanager
def canonical_lock(root: Path, *, exclusive: bool) -> Iterator[CanonicalLockLease]:
    descriptor = _open_directory(root)
    locked = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        locked = True
        yield CanonicalLockLease(root.resolve(), descriptor, exclusive)
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def snapshot_file_view(
    view_root: Path,
    *,
    source: str,
    ignored_paths: Collection[PurePosixPath] = (),
    profile: str | None = None,
    target_tool: str | None = None,
) -> WorkflowView:
    try:
        files, fingerprint = _read_file_tree(view_root, ignored_paths, profile)
    except OSError as error:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC) from error
    if not files:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    return WorkflowView(
        source,
        tuple(files),
        source_fingerprint=fingerprint,
        target_tool=target_tool,
    )


def publish_workflow_view(
    view: WorkflowView,
    canonical_root: Path,
    target_root: Path,
    manifest_path: Path,
    *,
    canonical_lease: CanonicalLockLease | None = None,
    source_root: Path | None = None,
    ignored_source_paths: Collection[PurePosixPath] = (),
    source_profile: str | None = None,
    preflight_manifest: bytes | None = None,
) -> PublishResult:
    try:
        desired = _validate_view(view)
        manifest_relative = _manifest_relative(target_root, manifest_path)
        canonical_target = _same_directory(canonical_root, target_root)
        expected_manifest = CANONICAL_MANIFEST_NAME if canonical_target else RUNTIME_MANIFEST_NAME
        if manifest_relative != PurePosixPath(expected_manifest):
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
        if canonical_lease is not None:
            _validate_lease(canonical_lease, canonical_root, canonical_target)
            return _publish_with_lease(
                desired,
                view,
                target_root,
                manifest_relative,
                canonical_target,
                canonical_lease,
                source_root,
                ignored_source_paths,
                source_profile,
                preflight_manifest,
            )
        with canonical_lock(canonical_root, exclusive=canonical_target) as lease:
            return _publish_with_lease(
                desired,
                view,
                target_root,
                manifest_relative,
                canonical_target,
                lease,
                source_root,
                ignored_source_paths,
                source_profile,
                preflight_manifest,
            )
    except PublishError as error:
        return PublishResult(error.drift_class)
    except (OSError, UnicodeError, ValueError):
        return PublishResult(DriftClass.INVALID_OR_SEMANTIC)


publish_view = publish_workflow_view


def _publish_with_lease(
    desired: _Desired,
    view: WorkflowView,
    target_root: Path,
    manifest_relative: PurePosixPath,
    canonical_target: bool,
    lease: CanonicalLockLease,
    source_root: Path | None,
    ignored_source_paths: Collection[PurePosixPath],
    source_profile: str | None,
    preflight_manifest: bytes | None,
) -> PublishResult:
    if canonical_target:
        return _publish_locked(
            desired,
            view,
            target_root,
            manifest_relative,
            source_root,
            ignored_source_paths,
            source_profile,
            preflight_manifest,
            canonical_target=True,
        )
    with _target_lock(target_root):
        return _publish_locked(
            desired,
            view,
            target_root,
            manifest_relative,
            source_root,
            ignored_source_paths,
            source_profile,
            preflight_manifest,
            canonical_target=False,
        )


def _publish_locked(
    desired: _Desired,
    view: WorkflowView,
    target_root: Path,
    manifest_relative: PurePosixPath,
    source_root: Path | None,
    ignored_source_paths: Collection[PurePosixPath],
    source_profile: str | None,
    preflight_manifest: bytes | None,
    *,
    canonical_target: bool,
) -> PublishResult:
    target_tool = None if canonical_target else _target_tool(view)
    if preflight_manifest is None:
        prior, manifest_snapshot = _load_manifest(
            target_root,
            manifest_relative,
            canonical_target=canonical_target,
            target_tool=target_tool,
        )
    else:
        if not canonical_target:
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
        manifest_snapshot = _read_snapshot(target_root / manifest_relative)
        if manifest_snapshot is None:
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
        try:
            prior = _decode_manifest(
                preflight_manifest,
                canonical_target=True,
                target_tool=None,
            )
        except _ManifestError as error:
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC) from error
    legacy_relative: PurePosixPath | None = None
    legacy_residue = False
    if not canonical_target:
        legacy_relative = PurePosixPath(LEGACY_DELIVERY_MANIFEST_NAME)
        if prior is None:
            legacy = _load_legacy_delivery_manifest(
                target_root,
                legacy_relative,
                target_tool=target_tool,
            )
            if legacy is not None:
                prior = legacy
        else:
            legacy_residue = (target_root / legacy_relative).exists()
    expected_fingerprint = view.source_fingerprint
    if source_root is not None:
        current_fingerprint = _fingerprint_tree(source_root, ignored_source_paths, source_profile)
        if expected_fingerprint is None:
            expected_fingerprint = current_fingerprint
        elif current_fingerprint != expected_fingerprint:
            raise PublishError(DriftClass.SOURCE_CHANGED)
    try:
        preflight = _preflight(
            target_root,
            desired,
            prior,
            manifest_snapshot,
            canonical_target=canonical_target,
            target_tool=target_tool,
        )
    except PublishError:
        if legacy_residue:
            _commit(
                target_root,
                manifest_relative,
                desired,
                None,
                prior,
                legacy_relative,
                source_root=source_root,
                ignored_source_paths=ignored_source_paths,
                source_profile=source_profile,
                expected_fingerprint=expected_fingerprint,
            )
        raise
    return _commit(
        target_root,
        manifest_relative,
        desired,
        preflight,
        prior,
        legacy_relative,
        source_root=source_root,
        ignored_source_paths=ignored_source_paths,
        source_profile=source_profile,
        expected_fingerprint=expected_fingerprint,
    )


@contextmanager
def _target_lock(root: Path) -> Iterator[None]:
    descriptor = _open_directory(root)
    locked = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validate_lease(
    lease: CanonicalLockLease, canonical_root: Path, canonical_target: bool
) -> None:
    if not _same_directory(lease.root, canonical_root):
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


def _preflight(
    target_root: Path,
    desired: _Desired,
    prior: _Manifest | None,
    manifest_snapshot: _Snapshot | None,
    *,
    canonical_target: bool,
    target_tool: str | None,
) -> _Preflight:
    prior_files: Mapping[PurePosixPath, _FileState] = prior.files if prior else {}
    prior_fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], _FragmentState] = (
        prior.fragments if prior else {}
    )
    files: dict[PurePosixPath, _Snapshot | None] = {}
    classes: list[DriftClass] = []
    for path in sorted(set(desired.files) | set(prior_files)):
        _check_parent_paths(target_root, path)
        current = _read_snapshot(target_root / path)
        files[path] = current
        wanted = desired.manifest.files.get(path)
        previous = prior_files.get(path)
        if wanted is not None:
            classification = _file_update_class(
                path,
                current,
                wanted,
                previous,
                canonical_target=canonical_target,
                target_tool=target_tool,
            )
            if classification is not None:
                classes.append(classification)
        elif previous is not None:
            classification = _file_removal_class(current, previous)
            if classification is not None:
                classes.append(classification)

    carrier_paths = {key[0] for key in desired.fragments} | {key[0] for key in prior_fragments}
    carrier_outputs: dict[PurePosixPath, bytes | None] = {}
    for path in sorted(carrier_paths):
        _check_parent_paths(target_root, path)
        current = _read_snapshot(target_root / path)
        if current is not None and path.suffix not in {".json", ".toml"}:
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
        desired_for_path = _fragments_for(path, desired.fragments)
        prior_for_path = _fragment_states_for(path, prior_fragments)
        raw = current.content if current is not None else None
        try:
            classes.extend(_carrier_classes(path, raw, desired_for_path, prior_for_path))
            carrier_outputs[path] = _merge_carrier(path, raw, desired_for_path, prior_for_path)
        except _CarrierError:
            raise PublishError(DriftClass.INVALID_OR_SEMANTIC) from None

    if manifest_snapshot is not None and prior is None:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    failure = _worst_class(classes)
    if failure is not None:
        raise PublishError(failure)
    return _Preflight(files, carrier_outputs, manifest_snapshot)


def _file_update_class(
    path: PurePosixPath,
    current: _Snapshot | None,
    wanted: _FileState,
    previous: _FileState | None,
    *,
    canonical_target: bool,
    target_tool: str | None,
) -> DriftClass | None:
    if previous is None:
        if current is None or current.state == wanted:
            return None
        if _zero_byte_claude_companion(path, current, canonical_target, target_tool):
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


def _zero_byte_claude_companion(
    path: PurePosixPath,
    current: _Snapshot | None,
    canonical_target: bool,
    target_tool: str | None,
) -> bool:
    return (
        current is not None
        and (
            path == PurePosixPath("claude/AGENTS.md")
            if canonical_target
            else target_tool == "claude" and path == PurePosixPath("AGENTS.md")
        )
        and current.content == b""
        and not current.executable
    )


def _carrier_classes(
    path: PurePosixPath,
    raw: bytes | None,
    desired: Mapping[tuple[str, ...], CarrierFragment],
    previous: Mapping[tuple[str, ...], _FragmentState],
) -> list[DriftClass]:
    data = _parse_carrier(path, raw)
    classes: list[DriftClass] = []
    for key, fragment in desired.items():
        found, value, blocked = _nested_get(data, key)
        if blocked:
            classes.append(DriftClass.COLLISION)
            continue
        wanted = _value_digest(_fragment_value(fragment))
        prior = previous.get(key)
        if prior is None:
            if found and _value_digest(value) != wanted:
                classes.append(DriftClass.COLLISION)
        elif not found or _value_digest(value) not in {prior.content_hash, wanted}:
            classes.append(DriftClass.TARGET_DRIFT)
    for key, state in previous.items():
        if key in desired:
            continue
        found, value, blocked = _nested_get(data, key)
        if blocked or (found and _value_digest(value) != state.content_hash):
            classes.append(DriftClass.TARGET_DRIFT)
    return classes


def _commit(
    target_root: Path,
    manifest_relative: PurePosixPath,
    desired: _Desired,
    preflight: _Preflight | None,
    prior: _Manifest | None,
    legacy_relative: PurePosixPath | None,
    *,
    source_root: Path | None,
    ignored_source_paths: Collection[PurePosixPath],
    source_profile: str | None,
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
            or _fingerprint_tree(source_root, ignored_source_paths, source_profile)
            != expected_fingerprint
        ):
            raise PublishError(DriftClass.SOURCE_CHANGED)
        commit_checked = True

    if preflight is None:
        if legacy_relative is not None:
            legacy = target_root / legacy_relative
            if legacy.exists():
                allow_mutation()
                legacy.unlink()
                _fsync_directory(target_root)
                _after_target_mutation(1)
        return PublishResult(DriftClass.CLEAN)

    for path, item in sorted(desired.files.items()):
        current = _read_snapshot(target_root / path)
        if current is not None and current.state == desired.manifest.files[path]:
            continue
        allow_mutation()
        _atomic_replace(target_root / path, item.content, item.executable)
        changed.append(path)
        mutation_count += 1
        _after_target_mutation(mutation_count)
    for path, output in sorted(preflight.carrier_outputs.items()):
        if output is None:
            continue
        current = _read_snapshot(target_root / path)
        if current is not None and current.content == output:
            continue
        allow_mutation()
        _atomic_replace(target_root / path, output, current.executable if current else False)
        changed.append(path)
        mutation_count += 1
        _after_target_mutation(mutation_count)
    prior_files: Mapping[PurePosixPath, _FileState] = prior.files if prior is not None else {}
    for path, state in sorted(prior_files.items()):
        if path in desired.files:
            continue
        current = _read_snapshot(target_root / path)
        if current is not None and current.state == state:
            allow_mutation()
            (target_root / path).unlink()
            removed.append(path)
            mutation_count += 1
            _after_target_mutation(mutation_count)
    manifest = _encode_manifest(desired.manifest)
    current_manifest = _read_snapshot(target_root / manifest_relative)
    if current_manifest is None or current_manifest.content != manifest:
        allow_mutation()
        _atomic_replace(target_root / manifest_relative, manifest, False)
        mutation_count += 1
        _after_target_mutation(mutation_count)
        _after_runtime_manifest_write()
    if legacy_relative is not None:
        legacy = target_root / legacy_relative
        if legacy.exists():
            allow_mutation()
            legacy.unlink()
            _fsync_directory(target_root)
            mutation_count += 1
            _after_target_mutation(mutation_count)
    return PublishResult(DriftClass.CLEAN, tuple(changed), tuple(removed))


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
        _nested_set(data, key, _fragment_value(fragment))
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
        if not found or _value_digest(data[name]) != _value_digest(wanted):
            output = _splice_toml(output, name, _toml_literal(wanted))
            data = _parse_toml(output)
        if name not in data or _value_digest(data[name]) != _value_digest(wanted):
            raise _CarrierError
    for key in previous:
        if key in desired:
            continue
        name = key[0]
        if name in data:
            output = _splice_toml(output, name, None)
            data = _parse_toml(output)
        if name in data:
            raise _CarrierError
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
    target_root: Path,
    manifest_relative: PurePosixPath,
    *,
    canonical_target: bool,
    target_tool: str | None,
) -> tuple[_Manifest | None, _Snapshot | None]:
    snapshot = _read_snapshot(target_root / manifest_relative)
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
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC) from error


def retire_legacy_delivery_manifest(target_root: Path) -> PublishResult:
    """Verify an old runtime manifest, then remove it without publishing a view."""
    try:
        with _target_lock(target_root):
            legacy = target_root / LEGACY_DELIVERY_MANIFEST_NAME
            if (
                _load_legacy_delivery_manifest(
                    target_root,
                    PurePosixPath(LEGACY_DELIVERY_MANIFEST_NAME),
                    target_tool="claude",
                )
                is None
            ):
                return PublishResult(DriftClass.CLEAN)
            _after_legacy_delivery_verified()
            legacy.unlink()
            _fsync_directory(target_root)
            return PublishResult(DriftClass.CLEAN)
    except PublishError as error:
        return PublishResult(error.drift_class)
    except OSError:
        return PublishResult(DriftClass.INVALID_OR_SEMANTIC)


def _load_legacy_delivery_manifest(
    target_root: Path, relative_path: PurePosixPath, *, target_tool: str | None = None
) -> _Manifest | None:
    snapshot = _read_snapshot(target_root / relative_path)
    if snapshot is None:
        return None
    try:
        return _decode_legacy_delivery_manifest(
            target_root, snapshot.content, target_tool=target_tool
        )
    except (_CarrierError, _ManifestError):
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC) from None


def _decode_legacy_delivery_manifest(
    target_root: Path, raw: bytes, *, target_tool: str | None = None
) -> _Manifest:
    try:
        parsed = load_strict_json(raw)
    except ManifestError as error:
        raise _ManifestError from error
    root = _object(parsed)
    if set(root) != {"schema_version", "tool", "files", "fragments"}:
        raise _ManifestError
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise _ManifestError
    tool = root["tool"]
    if not isinstance(tool, str) or tool not in _TOOLS or target_tool not in {None, tool}:
        raise _ManifestError
    file_values = _object(root["files"])
    files: dict[PurePosixPath, _FileState] = {}
    for raw_path, raw_state in file_values.items():
        path = PurePosixPath(raw_path)
        state = _object(raw_state)
        content_hash = state.get("content_hash")
        executable = state.get("executable")
        if (
            raw_path != path.as_posix()
            or not _safe_relative(path)
            or set(state) != {"content_hash", "executable"}
            or not isinstance(content_hash, str)
            or not _valid_hash(content_hash)
            or not isinstance(executable, bool)
            or path in files
            or not _path_is_owned(tool, path)
        ):
            raise _ManifestError
        current = _read_snapshot(target_root / path)
        if current is None or current.state != _FileState(content_hash, executable):
            raise _ManifestError
        files[path] = current.state

    raw_fragments = root["fragments"]
    if not isinstance(raw_fragments, list):
        raise _ManifestError
    fragments: dict[tuple[PurePosixPath, tuple[str, ...]], _FragmentState] = {}
    for raw_fragment in cast(list[object], raw_fragments):
        fragment = _object(raw_fragment)
        carrier_value = fragment.get("carrier_path")
        keys_value = fragment.get("key_path")
        value_hash = fragment.get("value_hash")
        if (
            set(fragment) != {"carrier_path", "key_path", "value_hash"}
            or not isinstance(carrier_value, str)
            or not isinstance(keys_value, list)
            or not isinstance(value_hash, str)
            or not _valid_hash(value_hash)
        ):
            raise _ManifestError
        carrier = PurePosixPath(carrier_value)
        if (
            carrier_value != carrier.as_posix()
            or not _safe_relative(carrier)
            or carrier.suffix not in {".json", ".toml"}
            or not keys_value
            or not all(isinstance(key, str) and key for key in cast(list[object], keys_value))
        ):
            raise _ManifestError
        keys = tuple(cast(list[str], cast(list[object], keys_value)))
        if (
            carrier.suffix == ".toml"
            and len(keys) != 1
            or not _fragment_is_owned(tool, carrier, keys)
        ):
            raise _ManifestError
        key = (carrier, keys)
        if key in fragments or carrier in files:
            raise _ManifestError
        carrier_snapshot = _read_snapshot(target_root / carrier)
        if carrier_snapshot is None:
            raise _ManifestError
        found, value, blocked = _nested_get(_parse_carrier(carrier, carrier_snapshot.content), keys)
        if blocked or not found or _legacy_value_digest(value) != value_hash:
            raise _ManifestError
        fragments[key] = _FragmentState(carrier, keys, _value_digest(value))
    return _Manifest("legacy", files, fragments)


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


def _legacy_value_digest(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise _ManifestError from error
    return _digest(encoded.encode())


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


def _check_parent_paths(root: Path, path: PurePosixPath) -> None:
    current = root
    for part in path.parts[:-1]:
        current = current / part
        if current.exists() and not current.is_dir():
            raise PublishError(DriftClass.COLLISION)


def _read_snapshot(path: Path) -> _Snapshot | None:
    try:
        result = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(result.st_mode):
        raise PublishError(DriftClass.COLLISION)
    try:
        return _Snapshot(path.read_bytes(), bool(result.st_mode & stat.S_IXUSR))
    except OSError as error:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC) from error


def _atomic_replace(path: Path, content: bytes, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".djinn-publisher-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o755 if executable else 0o644)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_file_tree(
    root: Path,
    ignored_paths: Collection[PurePosixPath] = (),
    profile: str | None = None,
) -> tuple[list[PublishedFile], str]:
    if not root.is_dir():
        raise OSError("View root is not a directory")
    files: list[PublishedFile] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if not path.is_file() or path.is_symlink():
            raise OSError("View root contains a non-regular file")
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if not _safe_relative(relative):
            raise OSError("View root contains an unsafe path")
        if relative in ignored_paths:
            continue
        if profile == "opencode" and not _opencode_owned(relative):
            raise OSError("OpenCode view contains an unowned path")
        info = path.stat()
        files.append(PublishedFile(relative, path.read_bytes(), bool(info.st_mode & stat.S_IXUSR)))
    return files, _fingerprint_files(files)


def _fingerprint_tree(
    root: Path,
    ignored_paths: Collection[PurePosixPath] = (),
    profile: str | None = None,
) -> str:
    try:
        files, fingerprint = _read_file_tree(root, ignored_paths, profile)
    except OSError as error:
        raise PublishError(DriftClass.SOURCE_CHANGED) from error
    del files
    return fingerprint


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
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC) from error


def _same_directory(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
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
    if canonical_target:
        if len(path.parts) < 2 or path.parts[0] not in _TOOLS:
            return False
        return _path_is_owned(path.parts[0], PurePosixPath(*path.parts[1:]))
    return target_tool is not None and _path_is_owned(target_tool, path)


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
        value in {"AGENTS.md", "CLAUDE.md"}
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


def _after_runtime_manifest_write() -> None:
    return None


def _after_legacy_delivery_verified() -> None:
    return None


def _canonical_manifest(canonical_root: Path) -> _Manifest:
    manifest, _snapshot = _load_manifest(
        canonical_root,
        PurePosixPath(CANONICAL_MANIFEST_NAME),
        canonical_target=True,
        target_tool=None,
    )
    if manifest is None:
        raise PublishError(DriftClass.INVALID_OR_SEMANTIC)
    return manifest

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
    }
    if view.source != manifest.source or actual != expected:
        raise PublishError(DriftClass.SOURCE_CHANGED)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="workflow-publisher")
    parser.add_argument("--view", "--view-dir", dest="view", required=True)
    parser.add_argument("--canonical-root", required=True)
    parser.add_argument("--target", "--target-root", dest="target", required=True)
    parser.add_argument("--manifest", "--manifest-path", dest="manifest", required=True)
    parser.add_argument("--fragment", action="append", default=[])
    parser.add_argument("--ignore", action="append", default=[])
    parser.add_argument("--profile", choices=("opencode",))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(argv)
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
                manifest = _canonical_manifest(canonical_root)
                view = snapshot_file_view(
                    Path(arguments.view),
                    source=manifest.source,
                    ignored_paths=ignored_paths,
                    profile=arguments.profile,
                    target_tool=arguments.profile,
                )
                _verify_seed_against_canonical_manifest(view, manifest, arguments.profile)
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
            result = PublishResult(error.drift_class)
    if not result.success:
        print(f"workflow publisher: {result.drift_class.value}", file=sys.stderr)
    return EXIT_CODES[result.drift_class]


if __name__ == "__main__":
    raise SystemExit(main())
