"""Transactional delivery of one already-rendered workflow configuration view."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import stat
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import tomli_w

from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core.atomic_mutation import rename_noreplace
from djinn_in_a_box.core.config_lock import (
    ConfigDirectoryLockError,
    config_directory_lock,
    directory_is_attached,
)
from djinn_in_a_box.core.config_sync_adapters import (
    RenderedFile,
    SettingsFragment,
    fragment_is_owned,
    is_safe_relative_path,
    path_is_owned,
    validate_rendered_workflow,
)

DELIVERY_MANIFEST_NAME = ".djinn-workflow-delivery.json"
_SCHEMA_VERSION = 1
_TOOLS: frozenset[str] = frozenset({"claude", "codex", "opencode"})
_HEX_DIGITS = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class DeliveryView:
    """Exact, canonical output supplied by the rendering layer."""

    tool: ConfigSyncSource
    files: tuple[RenderedFile, ...]
    settings_fragments: tuple[SettingsFragment, ...]


@dataclass(frozen=True, slots=True)
class DeliveryProblem:
    """A content-free delivery failure descriptor."""

    identifier: str
    tool: str
    relative_path: PurePosixPath | None = None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Sanitized delivery outcome."""

    success: bool
    changed_paths: tuple[PurePosixPath, ...] = ()
    removed_paths: tuple[PurePosixPath, ...] = ()
    problems: tuple[DeliveryProblem, ...] = ()
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class _FileRecord:
    content_hash: str
    executable: bool


@dataclass(frozen=True, slots=True)
class _FragmentRecord:
    carrier_path: PurePosixPath
    key_path: tuple[str, ...]
    value_hash: str


@dataclass(frozen=True, slots=True)
class _Manifest:
    tool: ConfigSyncSource
    files: Mapping[PurePosixPath, _FileRecord]
    fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    content: bytes
    executable: bool

    @property
    def record(self) -> _FileRecord:
        return _FileRecord(_digest(self.content), self.executable)


@dataclass(frozen=True, slots=True)
class _StagedFile:
    relative_path: PurePosixPath
    device: int
    inode: int
    record: _FileRecord


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
class _Desired:
    files: Mapping[PurePosixPath, RenderedFile]
    fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], SettingsFragment]
    manifest: _Manifest


@dataclass(frozen=True, slots=True)
class _Observations:
    manifest: _Snapshot | None
    files: Mapping[PurePosixPath, _Snapshot | None]
    carriers: Mapping[PurePosixPath, _Snapshot | None]


class _ManifestError(ValueError):
    pass


class _UnsafePathError(OSError):
    pass


class _RaceError(OSError):
    pass


class _QuarantinePreservedError(_RaceError):
    pass


class _StageCreateError(OSError):
    pass


class _MissingParentError(FileNotFoundError):
    pass


def deliver_config_view(
    view: DeliveryView,
    destination_root: Path,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> DeliveryResult:
    """Validate and atomically deliver ``view`` into an existing real directory."""
    tool = _safe_tool(view.tool)
    desired, problems = _validate_view(view)
    if desired is None:
        return _failure(tool, problems)

    try:
        with config_directory_lock(destination_root, exclusive=True) as root_fd:
            root_stat = os.fstat(root_fd)
            if (
                expected_root_identity is not None
                and (
                    root_stat.st_dev,
                    root_stat.st_ino,
                )
                != expected_root_identity
            ):
                return _failure(
                    tool,
                    (_problem("destination-root-race", tool),),
                    retryable=True,
                )
            _require_root_attached(destination_root, root_fd)
            prior, manifest_snapshot, manifest_problem = _load_manifest(root_fd, view.tool)
            if manifest_problem is not None:
                return _failure(tool, (manifest_problem,))

            observations, preflight_problems = _preflight(root_fd, view.tool, desired, prior)
            observations = _Observations(
                manifest_snapshot, observations.files, observations.carriers
            )
            if preflight_problems:
                return _failure(tool, preflight_problems)

            if not _mutation_needed(desired, prior, observations):
                _require_root_attached(destination_root, root_fd)
                return DeliveryResult(success=True)

            _require_root_attached(destination_root, root_fd)
            stage = _create_stage(root_fd)
            try:
                changed, removed = _publish(
                    root_fd,
                    stage,
                    view.tool,
                    desired,
                    prior,
                    observations,
                    destination_root,
                )
                _require_root_attached(destination_root, root_fd)
                return DeliveryResult(
                    success=True,
                    changed_paths=tuple(sorted(set(changed))),
                    removed_paths=tuple(sorted(set(removed))),
                )
            finally:
                _cleanup_stage(root_fd, stage)
    except ConfigDirectoryLockError:
        identifier = (
            "destination-root-race"
            if expected_root_identity is not None
            else "destination-root-unsafe"
        )
        return _failure(
            tool,
            (_problem(identifier, tool),),
            retryable=expected_root_identity is not None,
        )
    except _StageCreateError:
        return _failure(tool, (_problem("stage-create-failed", tool),), retryable=True)
    except _QuarantinePreservedError:
        return _failure(tool, (_problem("quarantine-preserved", tool),), retryable=True)
    except _RaceError:
        return _failure(tool, (_problem("concurrent-change", tool),), retryable=True)
    except (_UnsafePathError, OSError):
        return _failure(tool, (_problem("publication-failed", tool),), retryable=True)
    except (RecursionError, TypeError, ValueError):
        return _failure(tool, (_problem("invalid-delivery-data", tool),))


def _validate_view(view: DeliveryView) -> tuple[_Desired | None, tuple[DeliveryProblem, ...]]:
    tool = _safe_tool(view.tool)
    if tool == "unknown":
        return None, (_problem("unsupported-tool", tool),)

    problems: list[DeliveryProblem] = []
    files: dict[PurePosixPath, RenderedFile] = {}
    fragments: dict[tuple[PurePosixPath, tuple[str, ...]], SettingsFragment] = {}

    for item in view.files:
        path = item.relative_path
        if not is_safe_relative_path(path):
            problems.append(_problem("unsafe-file-path", tool))
            continue
        if not path_is_owned(view.tool, path):
            problems.append(_problem("unowned-file-path", tool, path))
        if path in files:
            problems.append(_problem("duplicate-file-path", tool, path))
        files[path] = item

    for item in view.settings_fragments:
        key = (item.carrier_path, item.key_path)
        if not is_safe_relative_path(item.carrier_path):
            problems.append(_problem("unsafe-carrier-path", tool))
            continue
        if not item.key_path or any(not part for part in item.key_path):
            problems.append(_problem("unsafe-fragment-key", tool, item.carrier_path))
        if not fragment_is_owned(view.tool, *key):
            problems.append(_problem("unowned-fragment", tool, item.carrier_path))
        if key in fragments:
            problems.append(_problem("duplicate-fragment", tool, item.carrier_path))
        fragments[key] = item

    if not problems:
        try:
            if validate_rendered_workflow(view.tool, view.files, view.settings_fragments):
                problems.append(_problem("invalid-rendered-workflow", tool))
        except (RecursionError, TypeError, ValueError):
            problems.append(_problem("invalid-rendered-workflow", tool))
    if problems:
        return None, tuple(sorted(set(problems), key=_problem_key))

    file_records = {
        path: _FileRecord(_digest(item.content), item.executable) for path, item in files.items()
    }
    fragment_records: dict[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord] = {}
    try:
        for key, item in fragments.items():
            value = _load_fragment_value(item.value_json)
            _validate_fragment_serializable(item, value)
            fragment_records[key] = _FragmentRecord(
                item.carrier_path, item.key_path, _digest_json(value)
            )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        return None, (_problem("invalid-fragment-value", tool),)

    manifest = _Manifest(view.tool, file_records, fragment_records)
    return _Desired(files, fragments, manifest), ()


def _load_manifest(
    root_fd: int, tool: ConfigSyncSource
) -> tuple[_Manifest | None, _Snapshot | None, DeliveryProblem | None]:
    path = PurePosixPath(DELIVERY_MANIFEST_NAME)
    try:
        snapshot = _read_snapshot(root_fd, path)
    except _UnsafePathError:
        return None, None, _problem("manifest-unsafe", tool, path)
    if snapshot is None:
        return None, None, None
    try:
        manifest = _decode_manifest(snapshot.content)
    except _ManifestError:
        return None, snapshot, _problem("manifest-malformed", tool, path)
    if manifest.tool != tool:
        return None, snapshot, _problem("manifest-tool-mismatch", tool, path)
    return manifest, snapshot, None


def _preflight(
    root_fd: int,
    tool: ConfigSyncSource,
    desired: _Desired,
    prior: _Manifest | None,
) -> tuple[_Observations, tuple[DeliveryProblem, ...]]:
    prior_files: Mapping[PurePosixPath, _FileRecord] = prior.files if prior is not None else {}
    prior_fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord] = (
        prior.fragments if prior is not None else {}
    )
    file_paths = set(desired.files) | set(prior_files)
    carrier_paths = {key[0] for key in desired.fragments} | {key[0] for key in prior_fragments}
    all_paths = file_paths | carrier_paths | {PurePosixPath(DELIVERY_MANIFEST_NAME)}
    problems: list[DeliveryProblem] = []

    for path in sorted(all_paths):
        try:
            _check_parents(root_fd, path)
        except _UnsafePathError:
            problems.append(_problem("unsafe-parent", tool, path))
    if problems:
        return _Observations(None, {}, {}), tuple(problems)

    files: dict[PurePosixPath, _Snapshot | None] = {}
    carriers: dict[PurePosixPath, _Snapshot | None] = {}
    for path in sorted(file_paths):
        try:
            current = _read_snapshot(root_fd, path)
        except _UnsafePathError:
            problems.append(_problem("unsafe-file-target", tool, path))
            continue
        files[path] = current
        wanted = desired.manifest.files.get(path)
        previous = prior_files.get(path)
        if wanted is not None:
            problem = _file_update_problem(current, wanted, previous)
        else:
            problem = _file_removal_problem(current, cast(_FileRecord, previous))
        if problem is not None:
            problems.append(_problem(problem, tool, path))

    desired_by_carrier = _group_fragments(desired.manifest.fragments)
    desired_settings_by_carrier = _group_settings_fragments(desired.fragments)
    prior_by_carrier = _group_fragments(prior_fragments)
    for path in sorted(carrier_paths):
        try:
            current = _read_snapshot(root_fd, path)
            carriers[path] = current
            carrier_problems = _carrier_problems(
                tool,
                path,
                current,
                desired_by_carrier.get(path, {}),
                prior_by_carrier.get(path, {}),
            )
            problems.extend(carrier_problems)
            if not carrier_problems:
                try:
                    _merge_carrier(
                        path,
                        current.content if current is not None else None,
                        desired_settings_by_carrier.get(path, {}),
                        prior_by_carrier.get(path, {}),
                    )
                except (RecursionError, TypeError, ValueError):
                    problems.append(_problem("carrier-serialization-failed", tool, path))
        except _UnsafePathError:
            problems.append(_problem("unsafe-carrier-target", tool, path))

    return _Observations(None, files, carriers), tuple(sorted(problems, key=_problem_key))


def _file_update_problem(
    current: _Snapshot | None, wanted: _FileRecord, previous: _FileRecord | None
) -> str | None:
    if previous is None:
        if current is None or current.record == wanted:
            return None
        return "unmanaged-file-collision"
    if current is None or current.record in {previous, wanted}:
        return None
    return "managed-file-drift"


def _file_removal_problem(current: _Snapshot | None, previous: _FileRecord) -> str | None:
    if current is None or current.record == previous:
        return None
    return "stale-file-drift"


def _carrier_problems(
    tool: ConfigSyncSource | str,
    path: PurePosixPath,
    current: _Snapshot | None,
    wanted: Mapping[tuple[str, ...], _FragmentRecord],
    previous: Mapping[tuple[str, ...], _FragmentRecord],
) -> list[DeliveryProblem]:
    try:
        data = _parse_carrier(path, current.content if current is not None else None)
    except (RecursionError, TypeError, ValueError):
        return [_problem("carrier-malformed", _safe_tool(tool), path)]

    problems: list[DeliveryProblem] = []
    for key, record in wanted.items():
        found, value, blocked = _nested_get(data, key)
        if blocked:
            problems.append(_problem("fragment-parent-collision", _safe_tool(tool), path))
            continue
        value_hash = _digest_json(value) if found else None
        prior = previous.get(key)
        if prior is None:
            if found and value_hash != record.value_hash:
                problems.append(_problem("unmanaged-fragment-collision", _safe_tool(tool), path))
        elif found and value_hash not in {prior.value_hash, record.value_hash}:
            problems.append(_problem("managed-fragment-drift", _safe_tool(tool), path))

    for key, record in previous.items():
        if key in wanted:
            continue
        found, value, blocked = _nested_get(data, key)
        if blocked:
            problems.append(_problem("fragment-parent-collision", _safe_tool(tool), path))
        elif found and _digest_json(value) != record.value_hash:
            problems.append(_problem("stale-fragment-drift", _safe_tool(tool), path))
    return problems


def _mutation_needed(
    desired: _Desired, prior: _Manifest | None, observations: _Observations
) -> bool:
    if prior != desired.manifest:
        return True
    for path, record in desired.manifest.files.items():
        current = observations.files.get(path)
        if current is None or current.record != record:
            return True
    for path in {key[0] for key in desired.fragments}:
        current = observations.carriers.get(path)
        output = _merge_carrier(
            path,
            current.content if current is not None else None,
            _fragments_for(path, desired.fragments),
            {},
        )
        if output is not None and (current is None or output != current.content):
            return True
    return False


def _publish(
    root_fd: int,
    stage: _Stage,
    tool: ConfigSyncSource,
    desired: _Desired,
    prior: _Manifest | None,
    observations: _Observations,
    destination_root: Path,
) -> tuple[list[PurePosixPath], list[PurePosixPath]]:
    _require_root_attached(destination_root, root_fd)
    changed: list[PurePosixPath] = []
    removed: list[PurePosixPath] = []
    expected_final: dict[PurePosixPath, _Snapshot | None] = {}
    prior_files: Mapping[PurePosixPath, _FileRecord] = prior.files if prior is not None else {}
    prior_fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord] = (
        prior.fragments if prior is not None else {}
    )

    for path, item in sorted(desired.files.items()):
        wanted = desired.manifest.files[path]
        expected_final[path] = _Snapshot(item.content, item.executable)
        current = _read_snapshot(root_fd, path)
        if current is not None and current.record == wanted:
            continue
        if current != observations.files.get(path):
            raise _RaceError
        staged = _stage_bytes(root_fd, stage, "files", path, item.content, item.executable)
        _replace_staged(root_fd, stage, staged, path, current)
        changed.append(path)

    desired_by_carrier = _group_settings_fragments(desired.fragments)
    prior_by_carrier = _group_fragments(prior_fragments)
    carrier_paths = set(desired_by_carrier) | set(prior_by_carrier)
    for path in sorted(carrier_paths):
        initial = observations.carriers.get(path)
        current = _read_snapshot(root_fd, path)
        if current != initial:
            race_problems = _carrier_problems(
                tool,
                path,
                current,
                _group_fragment_records(desired_by_carrier.get(path, {})),
                prior_by_carrier.get(path, {}),
            )
            if race_problems:
                raise _RaceError
        output = _merge_carrier(
            path,
            current.content if current is not None else None,
            desired_by_carrier.get(path, {}),
            prior_by_carrier.get(path, {}),
        )
        expected_final[path] = (
            None
            if output is None
            else _Snapshot(output, current.executable if current is not None else False)
        )
        if output is None or (current is not None and output == current.content):
            continue
        staged = _stage_bytes(
            root_fd,
            stage,
            "carriers",
            path,
            output,
            current.executable if current is not None else False,
        )
        _replace_staged(root_fd, stage, staged, path, current)
        changed.append(path)

    for path, previous in sorted(prior_files.items()):
        if path in desired.files:
            continue
        expected_final[path] = None
        current = _read_snapshot(root_fd, path)
        if current is None:
            continue
        if current.record != previous or current != observations.files.get(path):
            raise _RaceError
        _unlink_at(root_fd, path, current)
        removed.append(path)

    manifest_content = _encode_manifest(desired.manifest)
    _require_root_attached(destination_root, root_fd)
    _verify_final_snapshots(root_fd, expected_final)
    current_manifest = _read_snapshot(root_fd, PurePosixPath(DELIVERY_MANIFEST_NAME))
    if current_manifest is not None and current_manifest.content == manifest_content:
        _require_root_attached(destination_root, root_fd)
        _verify_final_snapshots(root_fd, expected_final)
        return changed, removed
    if current_manifest != observations.manifest:
        raise _RaceError
    manifest_path = PurePosixPath(DELIVERY_MANIFEST_NAME)
    staged = _stage_bytes(root_fd, stage, "metadata", manifest_path, manifest_content, False)
    _replace_staged(root_fd, stage, staged, manifest_path, current_manifest)
    _require_root_attached(destination_root, root_fd)
    _verify_final_snapshots(root_fd, expected_final)
    return changed, removed


def _verify_final_snapshots(
    root_fd: int,
    expected: Mapping[PurePosixPath, _Snapshot | None],
) -> None:
    for path, snapshot in expected.items():
        if _read_snapshot(root_fd, path) != snapshot:
            raise _RaceError


def _merge_carrier(
    path: PurePosixPath,
    raw: bytes | None,
    wanted: Mapping[tuple[str, ...], SettingsFragment],
    previous: Mapping[tuple[str, ...], _FragmentRecord],
) -> bytes | None:
    data = _parse_carrier(path, raw)
    original = copy.deepcopy(data)
    for key, fragment in wanted.items():
        _nested_set(data, key, _load_fragment_value(fragment.value_json))
    for key in previous:
        if key not in wanted:
            _nested_remove(data, key)
    if data == original:
        return raw
    if path.suffix == ".json":
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    return tomli_w.dumps(data).encode()


def _parse_carrier(path: PurePosixPath, raw: bytes | None) -> dict[str, object]:
    if raw is None:
        return {}
    try:
        parsed: object
        if path.suffix == ".json":
            parsed = json.loads(raw)
        elif path.suffix == ".toml":
            parsed = tomllib.loads(raw.decode())
        else:
            raise ValueError
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ValueError from error
    if not isinstance(parsed, dict):
        raise ValueError
    return cast(dict[str, object], parsed)


def _nested_get(data: Mapping[str, object], keys: tuple[str, ...]) -> tuple[bool, object, bool]:
    current = data
    for key in keys[:-1]:
        if key not in current:
            return False, None, False
        child = current[key]
        if not isinstance(child, dict):
            return False, None, True
        current = cast(dict[str, object], child)
    if not keys or keys[-1] not in current:
        return False, None, False
    return True, current[keys[-1]], False


def _nested_set(data: dict[str, object], keys: tuple[str, ...], value: object) -> None:
    current = data
    for key in keys[:-1]:
        if key not in current:
            child = {}
            current[key] = child
        else:
            child = current[key]
        if not isinstance(child, dict):
            raise _RaceError
        current = cast(dict[str, object], child)
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
            break


def _decode_manifest(raw: bytes) -> _Manifest:
    try:
        parsed: object = json.loads(raw, object_pairs_hook=_strict_object)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        RecursionError,
        TypeError,
        _ManifestError,
    ) as error:
        raise _ManifestError from error
    root = _object(parsed)
    _exact_keys(root, {"schema_version", "tool", "files", "fragments"})
    schema_version = root["schema_version"]
    if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
        raise _ManifestError
    tool_value = root["tool"]
    if not isinstance(tool_value, str) or tool_value not in _TOOLS:
        raise _ManifestError
    tool = cast(ConfigSyncSource, tool_value)

    file_values = _object(root["files"])
    files: dict[PurePosixPath, _FileRecord] = {}
    for raw_path, raw_record in file_values.items():
        path = PurePosixPath(raw_path)
        record = _object(raw_record)
        _exact_keys(record, {"content_hash", "executable"})
        content_hash = record["content_hash"]
        executable = record["executable"]
        if (
            raw_path != path.as_posix()
            or not is_safe_relative_path(path)
            or not path_is_owned(tool, path)
            or not _valid_digest(content_hash)
            or not isinstance(executable, bool)
            or path in files
        ):
            raise _ManifestError
        files[path] = _FileRecord(cast(str, content_hash), executable)

    fragment_values = root["fragments"]
    if not isinstance(fragment_values, list):
        raise _ManifestError
    fragments: dict[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord] = {}
    for raw_fragment in cast(list[object], fragment_values):
        fragment = _object(raw_fragment)
        _exact_keys(fragment, {"carrier_path", "key_path", "value_hash"})
        carrier_value = fragment["carrier_path"]
        key_value = fragment["key_path"]
        value_hash = fragment["value_hash"]
        if not isinstance(carrier_value, str) or not isinstance(key_value, list):
            raise _ManifestError
        carrier = PurePosixPath(carrier_value)
        key_parts = cast(list[object], key_value)
        if not key_parts or not all(isinstance(part, str) and part for part in key_parts):
            raise _ManifestError
        key_path = tuple(cast(list[str], key_parts))
        key = (carrier, key_path)
        if (
            carrier_value != carrier.as_posix()
            or not is_safe_relative_path(carrier)
            or not fragment_is_owned(tool, carrier, key_path)
            or not _valid_digest(value_hash)
            or key in fragments
        ):
            raise _ManifestError
        fragments[key] = _FragmentRecord(carrier, key_path, cast(str, value_hash))
    return _Manifest(tool, files, fragments)


def _encode_manifest(manifest: _Manifest) -> bytes:
    value = {
        "schema_version": _SCHEMA_VERSION,
        "tool": manifest.tool,
        "files": {
            path.as_posix(): {
                "content_hash": record.content_hash,
                "executable": record.executable,
            }
            for path, record in sorted(manifest.files.items())
        },
        "fragments": [
            {
                "carrier_path": record.carrier_path.as_posix(),
                "key_path": list(record.key_path),
                "value_hash": record.value_hash,
            }
            for _, record in sorted(manifest.fragments.items())
        ],
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _strict_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _ManifestError
        value[key] = item
    return value


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _ManifestError
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise _ManifestError
    return cast(dict[str, object], mapping)


def _exact_keys(value: Mapping[str, object], keys: set[str]) -> None:
    if set(value) != keys:
        raise _ManifestError


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _group_fragments(
    fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord],
) -> dict[PurePosixPath, dict[tuple[str, ...], _FragmentRecord]]:
    grouped: dict[PurePosixPath, dict[tuple[str, ...], _FragmentRecord]] = {}
    for (path, key), record in fragments.items():
        grouped.setdefault(path, {})[key] = record
    return grouped


def _group_settings_fragments(
    fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], SettingsFragment],
) -> dict[PurePosixPath, dict[tuple[str, ...], SettingsFragment]]:
    grouped: dict[PurePosixPath, dict[tuple[str, ...], SettingsFragment]] = {}
    for (path, key), fragment in fragments.items():
        grouped.setdefault(path, {})[key] = fragment
    return grouped


def _fragments_for(
    path: PurePosixPath,
    fragments: Mapping[tuple[PurePosixPath, tuple[str, ...]], SettingsFragment],
) -> dict[tuple[str, ...], SettingsFragment]:
    return {key: item for (carrier, key), item in fragments.items() if carrier == path}


def _group_fragment_records(
    fragments: Mapping[tuple[str, ...], SettingsFragment],
) -> dict[tuple[str, ...], _FragmentRecord]:
    result: dict[tuple[str, ...], _FragmentRecord] = {}
    for key, fragment in fragments.items():
        result[key] = _FragmentRecord(
            fragment.carrier_path,
            fragment.key_path,
            _digest_json(_load_fragment_value(fragment.value_json)),
        )
    return result


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
                    raise _UnsafePathError from error
            except OSError as error:
                raise _UnsafePathError from error
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _require_root_attached(path: Path, root_fd: int) -> None:
    if not directory_is_attached(path, root_fd):
        raise _RaceError


def _parent_is_attached(root_fd: int, path: PurePosixPath, parent_fd: int) -> bool:
    try:
        with _parent_fd(root_fd, path, create=False) as current_fd:
            expected = os.fstat(parent_fd)
            current = os.fstat(current_fd)
            return (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
    except (_MissingParentError, _UnsafePathError, OSError):
        return False


def _check_parents(root_fd: int, path: PurePosixPath) -> None:
    try:
        with _parent_fd(root_fd, path, create=False):
            pass
    except _MissingParentError:
        return


def _read_snapshot(root_fd: int, path: PurePosixPath) -> _Snapshot | None:
    try:
        with _parent_fd(root_fd, path, create=False) as parent_fd:
            return _read_snapshot_at(parent_fd, path.name)
    except _MissingParentError:
        return None


def _read_snapshot_at(parent_fd: int, name: str) -> _Snapshot | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _UnsafePathError from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise _UnsafePathError
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return _Snapshot(b"".join(chunks), bool(file_stat.st_mode & stat.S_IXUSR))
    finally:
        os.close(descriptor)


def _create_stage(root_fd: int) -> _Stage:
    for _attempt in range(32):
        name = f".djinn-delivery-stage-{secrets.token_hex(12)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        except OSError as error:
            raise _StageCreateError from error
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
                raise _RaceError
            return _Stage(name, descriptor, opened.st_dev, opened.st_ino, {})
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            if identity is not None:
                _remove_empty_directory_if_matching(root_fd, name, identity)
            if descriptor is None:
                raise _StageCreateError from error
            raise _RaceError from error
    raise _StageCreateError


def _stage_attached(root_fd: int, stage: _Stage) -> None:
    try:
        current = os.stat(stage.name, dir_fd=root_fd, follow_symlinks=False)
    except OSError as error:
        raise _RaceError from error
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or (current.st_dev, current.st_ino) != (stage.device, stage.inode)
    ):
        raise _RaceError


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
                    raise _RaceError from error
            except OSError as error:
                raise _RaceError from error
            try:
                opened = os.fstat(child)
                entry = _StageEntry(opened.st_dev, opened.st_ino, True)
                if created:
                    stage.entries[relative] = entry
                current = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                recorded = stage.entries.get(relative)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or stat.S_ISLNK(current.st_mode)
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                    or recorded != entry
                ):
                    raise _RaceError
            except OSError as error:
                os.close(child)
                raise _RaceError from error
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _stage_bytes(
    root_fd: int,
    stage: _Stage,
    category: str,
    path: PurePosixPath,
    content: bytes,
    executable: bool,
) -> _StagedFile:
    _stage_attached(root_fd, stage)
    relative = PurePosixPath(category) / path
    with _stage_parent_fd(stage, relative, create=True) as parent_fd:
        try:
            descriptor = os.open(
                relative.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o700 if executable else 0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise _RaceError from error
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
                raise _RaceError
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written == 0:
                    raise _RaceError
                remaining = remaining[written:]
            os.fchmod(descriptor, 0o700 if executable else 0o600)
            staged_stat = os.fstat(descriptor)
            return _StagedFile(
                relative,
                staged_stat.st_dev,
                staged_stat.st_ino,
                _FileRecord(_digest(content), executable),
            )
        except OSError as error:
            raise _RaceError from error
        finally:
            os.close(descriptor)


def _replace_staged(
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
            _verify_staged_file(source_fd, staged)
            if _read_snapshot_at(destination_fd, destination.name) != expected:
                raise _RaceError
            if not _parent_is_attached(root_fd, destination, destination_fd):
                raise _RaceError
            if expected is None:
                rename_noreplace(
                    source_fd,
                    staged.relative_path.name,
                    destination_fd,
                    destination.name,
                )
            else:
                quarantine = _quarantine_destination(
                    stage,
                    destination_fd,
                    destination.name,
                )
                try:
                    quarantined = _read_snapshot_at(stage.descriptor, quarantine.name)
                except _UnsafePathError:
                    _restore_quarantine(stage, quarantine, destination_fd, destination.name)
                    raise _RaceError from None
                if quarantined != expected:
                    _restore_quarantine(stage, quarantine, destination_fd, destination.name)
                    raise _RaceError
                if not _parent_is_attached(root_fd, destination, destination_fd):
                    _restore_quarantine(stage, quarantine, destination_fd, destination.name)
                    raise _RaceError
                try:
                    rename_noreplace(
                        source_fd,
                        staged.relative_path.name,
                        destination_fd,
                        destination.name,
                    )
                except OSError:
                    _preserve_quarantine(stage, quarantine)
                    raise _QuarantinePreservedError from None
                if not _parent_is_attached(root_fd, destination, destination_fd):
                    _preserve_quarantine(stage, quarantine)
                    raise _QuarantinePreservedError
            if not _parent_is_attached(root_fd, destination, destination_fd):
                raise _RaceError
    except _QuarantinePreservedError:
        raise
    except (_MissingParentError, OSError) as error:
        raise _RaceError from error


def _quarantine_destination(
    stage: _Stage,
    destination_fd: int,
    destination_name: str,
) -> PurePosixPath:
    quarantine = PurePosixPath(f".quarantine-{secrets.token_hex(16)}")
    rename_noreplace(destination_fd, destination_name, stage.descriptor, quarantine.name)
    try:
        current = os.stat(quarantine.name, dir_fd=stage.descriptor, follow_symlinks=False)
        stage.entries[quarantine] = _StageEntry(
            current.st_dev,
            current.st_ino,
            stat.S_ISDIR(current.st_mode),
        )
    except (OSError, MemoryError) as error:
        _preserve_quarantine(stage, quarantine)
        raise _QuarantinePreservedError from error
    return quarantine


def _restore_quarantine(
    stage: _Stage,
    quarantine: PurePosixPath,
    destination_fd: int,
    destination_name: str,
) -> None:
    try:
        rename_noreplace(
            stage.descriptor,
            quarantine.name,
            destination_fd,
            destination_name,
        )
    except OSError:
        _preserve_quarantine(stage, quarantine)
        raise _QuarantinePreservedError from None
    stage.entries.pop(quarantine, None)


def _preserve_quarantine(stage: _Stage, quarantine: PurePosixPath) -> None:
    stage.entries.pop(quarantine, None)


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
            except (OSError, _MissingParentError, _RaceError):
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


def _verify_staged_file(parent_fd: int, staged: _StagedFile) -> None:
    try:
        descriptor = os.open(
            staged.relative_path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise _RaceError from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise _RaceError
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        actual = _FileRecord(_digest(b"".join(chunks)), bool(file_stat.st_mode & stat.S_IXUSR))
        path_stat = os.stat(staged.relative_path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            actual != staged.record
            or not stat.S_ISREG(path_stat.st_mode)
            or file_stat.st_nlink != 1
            or path_stat.st_nlink != 1
            or (path_stat.st_dev, path_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino)
            or (file_stat.st_dev, file_stat.st_ino) != (staged.device, staged.inode)
        ):
            raise _RaceError
    except OSError as error:
        raise _RaceError from error
    finally:
        os.close(descriptor)


def _unlink_at(root_fd: int, path: PurePosixPath, expected: _Snapshot) -> None:
    try:
        with _parent_fd(root_fd, path, create=False) as parent_fd:
            if _read_snapshot_at(parent_fd, path.name) != expected:
                raise _RaceError
            if not _parent_is_attached(root_fd, path, parent_fd):
                raise _RaceError
            quarantine_stage = _stage_for_quarantine(root_fd)
            try:
                quarantine = _quarantine_destination(
                    quarantine_stage,
                    parent_fd,
                    path.name,
                )
                try:
                    quarantined = _read_snapshot_at(
                        quarantine_stage.descriptor,
                        quarantine.name,
                    )
                except _UnsafePathError:
                    _restore_quarantine(quarantine_stage, quarantine, parent_fd, path.name)
                    raise _RaceError from None
                if quarantined != expected:
                    _restore_quarantine(quarantine_stage, quarantine, parent_fd, path.name)
                    raise _RaceError
                if not _parent_is_attached(root_fd, path, parent_fd):
                    _restore_quarantine(quarantine_stage, quarantine, parent_fd, path.name)
                    raise _RaceError
            finally:
                _cleanup_stage(root_fd, quarantine_stage)
    except _QuarantinePreservedError:
        raise
    except (FileNotFoundError, _MissingParentError, OSError) as error:
        raise _RaceError from error


def _stage_for_quarantine(root_fd: int) -> _Stage:
    return _create_stage(root_fd)


def _load_fragment_value(raw: bytes) -> object:
    return json.loads(raw)


def _validate_fragment_serializable(fragment: SettingsFragment, value: object) -> None:
    if fragment.carrier_path.suffix == ".json":
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return
    if fragment.carrier_path.suffix == ".toml":
        scratch: dict[str, object] = {}
        _nested_set(scratch, fragment.key_path, value)
        tomli_w.dumps(scratch)
        return
    raise ValueError


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _digest_json(value: object) -> str:
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _safe_tool(tool: object) -> str:
    return tool if isinstance(tool, str) and tool in _TOOLS else "unknown"


def _problem(
    identifier: str, tool: ConfigSyncSource | str, path: PurePosixPath | None = None
) -> DeliveryProblem:
    safe_path = path if path is not None and is_safe_relative_path(path) else None
    return DeliveryProblem(identifier, _safe_tool(tool), safe_path)


def _problem_key(problem: DeliveryProblem) -> tuple[str, str]:
    return (problem.identifier, problem.relative_path.as_posix() if problem.relative_path else "")


def _failure(
    tool: str,
    problems: Sequence[DeliveryProblem],
    *,
    retryable: bool = False,
) -> DeliveryResult:
    return DeliveryResult(
        success=False,
        problems=tuple(sorted(set(problems), key=_problem_key)),
        retryable=retryable,
    )
