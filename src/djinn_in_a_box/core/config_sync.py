"""Canonical workflow synchronization backed by the shared publisher."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core.config_delivery import DeliveryView
from djinn_in_a_box.core.config_sync_adapters import (
    OWNERSHIP_MATRIX,
    AdapterReadResult,
    RenderedFile,
    SettingsFragment,
    is_safe_relative_path,
    read_native_workflow,
    render_native_workflow,
    validate_rendered_workflow,
)
from djinn_in_a_box.core.workflow_publisher import (
    CANONICAL_MANIFEST_NAME,
    CarrierFragment,
    DriftClass,
    PublishedFile,
    WorkflowView,
    canonical_lock,
    publish_workflow_view,
    snapshot_file_view,
)

MANIFEST_NAME = CANONICAL_MANIFEST_NAME
CANONICAL_REMEDY = (
    "Author or edit the artifact natively in the target tool's view, "
    "or make the source form portable."
)
_TOOLS: tuple[ConfigSyncSource, ...] = ("claude", "codex", "opencode")


@dataclass(frozen=True, slots=True)
class DriftItem:
    kind: DriftClass
    message: str
    tool: ConfigSyncSource | None = None
    relative_path: PurePosixPath | None = None


@dataclass(frozen=True, slots=True)
class SyncProblem:
    identifier: str
    message: str
    tool: ConfigSyncSource | None = None
    relative_path: PurePosixPath | None = None


@dataclass(frozen=True, slots=True)
class ConfigSyncAudit:
    configured_source: ConfigSyncSource
    manifest_source: ConfigSyncSource | None
    drifts: tuple[DriftItem, ...] = ()
    problems: tuple[SyncProblem, ...] = ()

    @property
    def drift_classes(self) -> tuple[DriftClass, ...]:
        if not self.drifts:
            return (DriftClass.CLEAN,)
        return tuple(dict.fromkeys(item.kind for item in self.drifts))

    @property
    def clean(self) -> bool:
        return not self.drifts and not self.problems


@dataclass(frozen=True, slots=True)
class ConfigSyncResult:
    success: bool
    audit: ConfigSyncAudit
    changed_paths: tuple[PurePosixPath, ...] = ()
    removed_paths: tuple[PurePosixPath, ...] = ()
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class CanonicalDeliveryViewResult:
    success: bool
    audit: ConfigSyncAudit
    view: DeliveryView | None = None
    revision: str | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class _Build:
    source: ConfigSyncSource
    fingerprint: str
    canonical: WorkflowView
    views: Mapping[ConfigSyncSource, DeliveryView]
    problems: tuple[SyncProblem, ...]


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    path: PurePosixPath
    content_hash: str
    executable: bool
    key_path: tuple[str, ...] | None = None


class _BuildError(ValueError):
    def __init__(self, drift: DriftClass) -> None:
        self.drift = drift


def audit_config_sync(
    project_root: Path,
    *,
    config_path: Path | None = None,
) -> ConfigSyncAudit:
    source = load_config(config_path).config_sync.source
    config_root = project_root / "config"
    try:
        with canonical_lock(config_root, exclusive=False):
            return _audit_locked(project_root, source)
    except OSError:
        return _audit_for(source, DriftClass.INVALID_OR_SEMANTIC)


def sync_config(
    project_root: Path,
    *,
    config_path: Path | None = None,
) -> ConfigSyncResult:
    source = load_config(config_path).config_sync.source
    config_root = project_root / "config"
    try:
        with canonical_lock(config_root, exclusive=True) as lease:
            build = _snapshot_build(project_root, source)
            if build.problems:
                return ConfigSyncResult(False, _invalid_audit(source, build.problems))
            manifest_path = config_root / MANIFEST_NAME
            legacy = _legacy_manifest(manifest_path)
            if legacy is not None:
                if _fingerprint(project_root / "config" / source) != build.fingerprint:
                    return ConfigSyncResult(
                        False, _audit_for(source, DriftClass.SOURCE_CHANGED), retryable=True
                    )
                try:
                    _migrate_legacy(config_root, legacy)
                except ValueError:
                    return ConfigSyncResult(
                        False, _audit_for(source, DriftClass.INVALID_OR_SEMANTIC)
                    )
            else:
                _release_selected_source_records(
                    manifest_path, source, build.fingerprint, config_root, build.canonical
                )
            result = publish_workflow_view(
                build.canonical,
                config_root,
                config_root,
                manifest_path,
                canonical_lease=lease,
                source_root=config_root / source,
            )
            audit = _audit_locked(project_root, source)
            return ConfigSyncResult(
                result.success,
                audit,
                result.changed_paths,
                result.removed_paths,
                result.drift_class is DriftClass.SOURCE_CHANGED,
            )
    except _BuildError as error:
        return ConfigSyncResult(
            False,
            _audit_for(source, error.drift),
            retryable=error.drift is DriftClass.SOURCE_CHANGED,
        )
    except (OSError, ValueError):
        return ConfigSyncResult(False, _audit_for(source, DriftClass.INVALID_OR_SEMANTIC))


def load_canonical_delivery_view(
    project_root: Path,
    tool: ConfigSyncSource,
    *,
    config_path: Path | None = None,
) -> CanonicalDeliveryViewResult:
    source = load_config(config_path).config_sync.source
    config_root = project_root / "config"
    try:
        with canonical_lock(config_root, exclusive=False):
            audit = _audit_locked(project_root, source)
            if not audit.clean:
                return CanonicalDeliveryViewResult(False, audit)
            build = _snapshot_build(project_root, source)
            view = build.views[tool]
            manifest = (config_root / MANIFEST_NAME).read_bytes()
            revision = _digest(manifest + build.fingerprint.encode())
            return CanonicalDeliveryViewResult(True, audit, view, revision)
    except _BuildError as error:
        return CanonicalDeliveryViewResult(
            False,
            _audit_for(source, error.drift),
            retryable=error.drift is DriftClass.SOURCE_CHANGED,
        )
    except (KeyError, OSError):
        return CanonicalDeliveryViewResult(
            False, _audit_for(source, DriftClass.INVALID_OR_SEMANTIC)
        )


def _audit_locked(project_root: Path, source: ConfigSyncSource) -> ConfigSyncAudit:
    try:
        build = _snapshot_build(project_root, source)
    except _BuildError as error:
        return _audit_for(source, error.drift)
    if build.problems:
        return _invalid_audit(source, build.problems)
    manifest_path = project_root / "config" / MANIFEST_NAME
    if not manifest_path.exists():
        drifts = _compare_manifest(project_root / "config", build.canonical, ())
        kind = _worst_drift(drifts)
        filtered = [item for item in drifts if item.kind is kind] if kind is not None else []
        return ConfigSyncAudit(source, None, tuple(_deduplicate(filtered)))
    legacy = _legacy_manifest(manifest_path)
    if legacy is not None:
        try:
            source_name, _items = _legacy_items(legacy, project_root / "config")
        except ValueError:
            return _audit_for(source, DriftClass.INVALID_OR_SEMANTIC)
        return _audit_for(source, DriftClass.SOURCE_CHANGED, source_name)
    try:
        manifest_source, items = _lean_items(manifest_path.read_bytes())
    except (OSError, ValueError):
        return _audit_for(source, DriftClass.INVALID_OR_SEMANTIC)
    drifts = _compare_manifest(project_root / "config", build.canonical, items)
    if manifest_source != source:
        drifts.insert(0, DriftItem(DriftClass.SOURCE_CHANGED, "Selected source changed.", source))
    kind = _worst_drift(drifts)
    filtered = [item for item in drifts if item.kind is kind] if kind is not None else []
    return ConfigSyncAudit(source, manifest_source, tuple(_deduplicate(filtered)))


def _snapshot_build(project_root: Path, source: ConfigSyncSource) -> _Build:
    source_root = project_root / "config" / source
    if source_root.is_symlink() or not source_root.is_dir():
        raise _BuildError(DriftClass.INVALID_OR_SEMANTIC)
    try:
        before = snapshot_file_view(source_root, source=source).source_fingerprint
        if before is None:
            raise _BuildError(DriftClass.INVALID_OR_SEMANTIC)
        with tempfile.TemporaryDirectory(prefix="djinn-sync-") as temporary:
            snapshot_root = Path(temporary) / source
            shutil.copytree(source_root, snapshot_root)
            after = _fingerprint(source_root)
            snapshot_fingerprint = _fingerprint(snapshot_root)
            if before != after or before != snapshot_fingerprint:
                raise _BuildError(DriftClass.SOURCE_CHANGED)
            return _build_views(snapshot_root, source, before)
    except _BuildError:
        raise
    except (OSError, RuntimeError) as error:
        raise _BuildError(DriftClass.INVALID_OR_SEMANTIC) from error


def _build_views(snapshot_root: Path, source: ConfigSyncSource, fingerprint: str) -> _Build:
    read = read_native_workflow(snapshot_root, source)
    problems = [
        _problem_from_issue(issue.identifier, source, issue.relative_path)
        for issue in read.validation_issues
    ]
    source_files, source_fragments, source_problems = _source_view(snapshot_root, read)
    problems.extend(source_problems)
    views: dict[ConfigSyncSource, DeliveryView] = {
        source: DeliveryView(source, source_files, source_fragments)
    }
    for target in _TOOLS:
        if target == source:
            continue
        rendered = render_native_workflow(read, target)
        for issue in rendered.validation_issues:
            problems.append(_problem_from_issue(issue.identifier, target, issue.relative_path))
        for item in rendered.unresolved:
            problems.append(
                SyncProblem(
                    item.identifier, "Artifact is not portable.", item.target_tool, item.source_path
                )
            )
        views[target] = DeliveryView(target, rendered.files, rendered.settings_fragments)
    for tool, view in views.items():
        for issue in validate_rendered_workflow(tool, view.files, view.settings_fragments):
            problems.append(_problem_from_issue(issue.identifier, tool, issue.relative_path))
    files: list[PublishedFile] = []
    fragments: list[CarrierFragment] = []
    for tool, view in views.items():
        prefix = PurePosixPath(tool)
        companion = OWNERSHIP_MATRIX[source].instruction_companion
        publish_files = (
            view.files
            if tool != source
            else tuple(item for item in view.files if item.relative_path == companion)
        )
        files.extend(
            PublishedFile(prefix / item.relative_path, item.content, item.executable)
            for item in publish_files
        )
        fragments.extend(
            CarrierFragment(prefix / item.carrier_path, item.key_path, item.value_json)
            for item in view.settings_fragments
            if tool != source
        )
    return _Build(
        source,
        fingerprint,
        WorkflowView(source, tuple(files), tuple(fragments), source_fingerprint=fingerprint),
        views,
        tuple(_deduplicate_problems(problems)),
    )


def _source_view(
    root: Path, read: AdapterReadResult
) -> tuple[tuple[RenderedFile, ...], tuple[SettingsFragment, ...], list[SyncProblem]]:
    files = {
        item.source_path: RenderedFile(
            item.source_path, item.content, item.identifier, item.executable
        )
        for item in read.artifacts
    }
    instruction = next((item for item in read.artifacts if item.kind.value == "instructions"), None)
    if instruction is not None:
        companion = OWNERSHIP_MATRIX[read.tool].instruction_companion
        files[companion] = RenderedFile(
            companion, instruction.content, instruction.identifier, instruction.executable
        )
    fragments: list[SettingsFragment] = []
    problems: list[SyncProblem] = []
    for hook in OWNERSHIP_MATRIX[read.tool].hooks:
        if hook.carrier_path is None or hook.event is None:
            continue
        value, issue = _carrier_value(root / hook.carrier_path, ("hooks", hook.event))
        if issue:
            problems.append(
                SyncProblem(
                    "source-carrier",
                    "Source settings carrier is invalid.",
                    read.tool,
                    hook.carrier_path,
                )
            )
        elif value is not None:
            fragments.append(
                SettingsFragment(
                    hook.carrier_path,
                    ("hooks", hook.event),
                    _json_value(value),
                    f"source:{hook.name}",
                )
            )
    if read.tool == "codex":
        carrier = PurePosixPath("config.toml")
        value, issue = _carrier_value(root / carrier, ("project_doc_fallback_filenames",))
        if issue:
            problems.append(
                SyncProblem(
                    "source-carrier", "Source settings carrier is invalid.", read.tool, carrier
                )
            )
        elif value is not None:
            fragments.append(
                SettingsFragment(
                    carrier,
                    ("project_doc_fallback_filenames",),
                    _json_value(value),
                    "source:bridge",
                )
            )
    return (
        tuple(sorted(files.values(), key=lambda item: item.relative_path)),
        tuple(sorted(fragments, key=lambda item: (item.carrier_path, item.key_path))),
        problems,
    )


def _compare_manifest(
    config_root: Path, desired: WorkflowView, current_items: tuple[_ManifestItem, ...]
) -> list[DriftItem]:
    wanted_files = {item.relative_path: _file_item(item) for item in desired.files}
    wanted_fragments = {
        (item.carrier_path, item.key_path): _fragment_item(item) for item in desired.fragments
    }
    recorded_files = {item.path: item for item in current_items if item.key_path is None}
    recorded_fragments = {
        (item.path, item.key_path): item for item in current_items if item.key_path is not None
    }
    drifts: list[DriftItem] = []
    for path in sorted(set(wanted_files) | set(recorded_files)):
        wanted = wanted_files.get(path)
        recorded = recorded_files.get(path)
        actual = _file_item_at(config_root / path, path)
        drifts.extend(_item_drift(wanted, recorded, actual, path))
    for key in sorted(set(wanted_fragments) | set(recorded_fragments)):
        wanted = wanted_fragments.get(key)
        recorded = recorded_fragments.get(key)
        path, key_path = key
        actual, issue = _carrier_item_at(config_root / path, path, key_path)
        if issue:
            drifts.append(_drift(DriftClass.COLLISION, path))
        else:
            drifts.extend(_item_drift(wanted, recorded, actual, path))
    return drifts


def _item_drift(
    wanted: _ManifestItem | None,
    recorded: _ManifestItem | None,
    actual: _ManifestItem | None,
    path: PurePosixPath,
) -> list[DriftItem]:
    if wanted is None:
        if actual is None or actual == recorded:
            return [_drift(DriftClass.SOURCE_CHANGED, path)]
        return [_drift(DriftClass.TARGET_DRIFT, path)]
    if recorded is None:
        if actual is None or actual == wanted:
            return [_drift(DriftClass.SOURCE_CHANGED, path)]
        return [_drift(DriftClass.COLLISION, path)]
    if actual is None:
        return [_drift(DriftClass.SOURCE_CHANGED, path)]
    if actual != recorded and actual != wanted:
        return [_drift(DriftClass.TARGET_DRIFT, path)]
    if wanted != recorded or actual != wanted:
        return [_drift(DriftClass.SOURCE_CHANGED, path)]
    return []


def _legacy_manifest(path: Path) -> dict[str, object] | None:
    try:
        raw = _json_load(path.read_bytes())
        data = _object_mapping(raw)
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return data if "schema_version" in data else None


def _migrate_legacy(config_root: Path, legacy: Mapping[str, object]) -> None:
    source, items = _legacy_items(legacy, config_root)
    companion = OWNERSHIP_MATRIX[source].instruction_companion
    prefix = PurePosixPath(source)
    managed = tuple(
        item for item in items if item.path.parts[0] != source or item.path == prefix / companion
    )
    _atomic_write(config_root / MANIFEST_NAME, _encode_lean(source, managed))


def _release_selected_source_records(
    manifest_path: Path,
    source: ConfigSyncSource,
    fingerprint: str,
    config_root: Path,
    desired: WorkflowView,
) -> None:
    if not manifest_path.exists():
        return
    manifest_source, items = _lean_items(manifest_path.read_bytes())
    if manifest_source == source:
        return
    if _fingerprint(config_root / source) != fingerprint:
        raise _BuildError(DriftClass.SOURCE_CHANGED)
    companion = OWNERSHIP_MATRIX[source].instruction_companion
    prefix = PurePosixPath(source)
    kept = tuple(
        item for item in items if item.path.parts[0] != source or item.path == prefix / companion
    )
    known = {(item.path, item.key_path) for item in kept}
    adopted: list[_ManifestItem] = list(kept)
    for file in desired.files:
        if file.relative_path.parts[0] != manifest_source:
            continue
        actual = _file_item_at(config_root / file.relative_path, file.relative_path)
        if actual is not None and (actual.path, None) not in known:
            adopted.append(actual)
    for fragment in desired.fragments:
        if fragment.carrier_path.parts[0] != manifest_source:
            continue
        key = (fragment.carrier_path, fragment.key_path)
        actual, issue = _carrier_item_at(config_root / fragment.carrier_path, *key)
        if not issue and actual is not None and key not in known:
            adopted.append(actual)
    _atomic_write(manifest_path, _encode_lean(manifest_source, tuple(adopted)))


def _legacy_items(
    data: Mapping[str, object], config_root: Path
) -> tuple[ConfigSyncSource, tuple[_ManifestItem, ...]]:
    required = {
        "schema_version",
        "adapter_revision",
        "active_source",
        "source_hash",
        "source_files",
        "managed",
        "semantic",
    }
    active_source = data.get("active_source")
    managed_raw = data.get("managed")
    if set(data) != required or active_source not in _TOOLS:
        raise ValueError
    source: ConfigSyncSource = active_source
    items: dict[tuple[PurePosixPath, tuple[str, ...] | None], _ManifestItem] = {}
    _legacy_file_map(data["source_files"], PurePosixPath(source), config_root, items)
    managed = _object_mapping(managed_raw)
    if set(managed) != set(_TOOLS):
        raise ValueError
    for tool in _TOOLS:
        entry = managed[tool]
        values = _object_mapping(entry)
        if set(values) != {"files", "native_only", "fragments"}:
            raise ValueError
        prefix = PurePosixPath(tool)
        _legacy_file_map(values["files"], prefix, config_root, items)
        _legacy_file_map(values["native_only"], prefix, config_root, items)
        _legacy_fragments(values["fragments"], prefix, config_root, items)
    semantic = data["semantic"]
    if not isinstance(semantic, list):
        raise ValueError
    for record in cast(list[object], semantic):
        semantic_record = _object_mapping(record)
        if set(semantic_record) != {
            "fingerprint",
            "adapter_revision",
            "source_tool",
            "target_tool",
            "artifact_id",
            "source_path",
            "files",
            "fragments",
        }:
            raise ValueError
        if (
            semantic_record.get("source_tool") != source
            or semantic_record.get("target_tool") not in _TOOLS
        ):
            raise ValueError
        target = cast(ConfigSyncSource, semantic_record["target_tool"])
        _verify_legacy_semantic_outputs(semantic_record["files"], target, items)
        _verify_legacy_semantic_fragments(semantic_record["fragments"], target, items)
    return source, tuple(sorted(items.values(), key=lambda item: (item.path, item.key_path or ())))


def _verify_legacy_semantic_outputs(
    raw: object,
    target: ConfigSyncSource,
    items: Mapping[tuple[PurePosixPath, tuple[str, ...] | None], _ManifestItem],
) -> None:
    if not isinstance(raw, list):
        raise ValueError
    for value in cast(list[object], raw):
        data = _object_mapping(value)
        if set(data) != {"path", "hash", "executable"} or not isinstance(data["path"], str):
            raise ValueError
        path = PurePosixPath(data["path"])
        if not is_safe_relative_path(path):
            raise ValueError
        expected = _ManifestItem(
            PurePosixPath(target) / path,
            _valid_hash(data["hash"]),
            _bool(data["executable"]),
        )
        if items.get((expected.path, None)) != expected:
            raise ValueError


def _verify_legacy_semantic_fragments(
    raw: object,
    target: ConfigSyncSource,
    items: Mapping[tuple[PurePosixPath, tuple[str, ...] | None], _ManifestItem],
) -> None:
    if not isinstance(raw, list):
        raise ValueError
    for value in cast(list[object], raw):
        data = _object_mapping(value)
        if set(data) != {"carrier_path", "key_path", "value_hash"}:
            raise ValueError
        carrier = data["carrier_path"]
        keys = data["key_path"]
        if not isinstance(carrier, str) or not isinstance(keys, list):
            raise ValueError
        key_path = tuple(cast(str, key) for key in cast(list[object], keys))
        if not key_path or any(not key for key in key_path):
            raise ValueError
        expected = _ManifestItem(
            PurePosixPath(target) / PurePosixPath(carrier),
            _valid_hash(data["value_hash"]),
            False,
            key_path,
        )
        if items.get((expected.path, expected.key_path)) != expected:
            raise ValueError


def _legacy_file_map(
    raw: object,
    prefix: PurePosixPath,
    config_root: Path,
    result: dict[tuple[PurePosixPath, tuple[str, ...] | None], _ManifestItem],
) -> None:
    for raw_path, raw_state in _object_mapping(raw).items():
        relative = PurePosixPath(raw_path)
        state = _object_mapping(raw_state)
        if not is_safe_relative_path(relative) or set(state) != {"hash", "executable"}:
            raise ValueError
        item = _ManifestItem(
            prefix / relative, _valid_hash(state["hash"]), _bool(state["executable"])
        )
        if _file_item_at(config_root / item.path, item.path) != item:
            raise ValueError
        _add_item(result, item)


def _legacy_fragments(
    raw: object,
    prefix: PurePosixPath,
    config_root: Path,
    result: dict[tuple[PurePosixPath, tuple[str, ...] | None], _ManifestItem],
) -> None:
    if not isinstance(raw, list):
        raise ValueError
    for value in cast(list[object], raw):
        data = _object_mapping(value)
        if set(data) != {"carrier_path", "key_path", "value_hash"}:
            raise ValueError
        if not isinstance(data["carrier_path"], str) or not isinstance(data["key_path"], list):
            raise ValueError
        path = PurePosixPath(data["carrier_path"])
        keys = tuple(cast(str, key) for key in cast(list[object], data["key_path"]))
        if not is_safe_relative_path(path) or not keys or any(not key for key in keys):
            raise ValueError
        item = _ManifestItem(prefix / path, _valid_hash(data["value_hash"]), False, keys)
        actual, issue = _carrier_item_at(config_root / item.path, item.path, keys)
        if issue or actual != item:
            raise ValueError
        _add_item(result, item)


def _lean_items(raw: bytes) -> tuple[ConfigSyncSource, tuple[_ManifestItem, ...]]:
    try:
        data = _json_load(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError from error
    values = _object_mapping(data)
    if set(values) != {"source", "items"}:
        raise ValueError
    source = values.get("source")
    raw_items = values.get("items")
    if source not in _TOOLS or not isinstance(raw_items, list):
        raise ValueError
    items: dict[tuple[PurePosixPath, tuple[str, ...] | None], _ManifestItem] = {}
    for raw_item in cast(list[object], raw_items):
        item = _object_mapping(raw_item)
        keys = set(item)
        if keys not in (
            {"path", "content_hash", "executable"},
            {"path", "key_path", "content_hash", "executable"},
        ):
            raise ValueError
        if not isinstance(item.get("path"), str):
            raise ValueError
        path = PurePosixPath(cast(str, item["path"]))
        key_path: tuple[str, ...] | None = None
        if "key_path" in item:
            if not isinstance(item["key_path"], list):
                raise ValueError
            key_path = tuple(cast(str, key) for key in cast(list[object], item["key_path"]))
            if not key_path or any(not key for key in key_path):
                raise ValueError
        if not is_safe_relative_path(path):
            raise ValueError
        record = _ManifestItem(
            path, _valid_hash(item.get("content_hash")), _bool(item.get("executable")), key_path
        )
        _add_item(items, record)
    return source, tuple(sorted(items.values(), key=lambda item: (item.path, item.key_path or ())))


def _encode_lean(source: ConfigSyncSource, items: tuple[_ManifestItem, ...]) -> bytes:
    values: list[dict[str, object]] = []
    for item in items:
        value: dict[str, object] = {
            "path": item.path.as_posix(),
            "content_hash": item.content_hash,
            "executable": item.executable,
        }
        if item.key_path is not None:
            value["key_path"] = list(item.key_path)
        values.append(value)
    return (
        json.dumps({"source": source, "items": values}, indent=2, sort_keys=True) + "\n"
    ).encode()


def _carrier_value(path: Path, keys: tuple[str, ...]) -> tuple[object | None, bool]:
    if not path.exists():
        return None, False
    try:
        raw = path.read_bytes()
        decoded = _json_load(raw) if path.suffix == ".json" else _toml_load(raw)
        value: object = _object_mapping(decoded)
        current: object = value
        for key in keys:
            if not isinstance(current, Mapping) or key not in current:
                return None, False
            current = _object_mapping(cast(object, current))[key]
        return current, False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        return None, True


def _carrier_item_at(
    path: Path, manifest_path: PurePosixPath, keys: tuple[str, ...]
) -> tuple[_ManifestItem | None, bool]:
    value, issue = _carrier_value(path, keys)
    if issue:
        return None, True
    if value is None:
        return None, False
    return _ManifestItem(manifest_path, _digest(_json_value(value)), False, keys), False


def _file_item(item: PublishedFile) -> _ManifestItem:
    return _ManifestItem(item.relative_path, _digest(item.content), item.executable)


def _fragment_item(item: CarrierFragment) -> _ManifestItem:
    try:
        value: object = json.loads(item.value_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _BuildError(DriftClass.INVALID_OR_SEMANTIC) from error
    return _ManifestItem(item.carrier_path, _digest(_json_value(value)), False, item.key_path)


def _file_item_at(path: Path, manifest_path: PurePosixPath) -> _ManifestItem | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        return _ManifestItem(
            manifest_path, _digest(path.read_bytes()), bool(info.st_mode & stat.S_IXUSR)
        )
    except OSError:
        return None


def _fingerprint(root: Path) -> str:
    view = snapshot_file_view(root, source="snapshot")
    if view.source_fingerprint is None:
        raise _BuildError(DriftClass.INVALID_OR_SEMANTIC)
    return view.source_fingerprint


def _valid_hash(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError
    int(value, 16)
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def _object_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise ValueError
    return {cast(str, key): item for key, item in mapping.items()}


def _json_load(raw: bytes) -> object:
    return cast(object, json.loads(raw))


def _toml_load(raw: bytes) -> object:
    return cast(object, tomllib.loads(raw.decode()))


def _add_item(
    items: dict[tuple[PurePosixPath, tuple[str, ...] | None], _ManifestItem], item: _ManifestItem
) -> None:
    key = (item.path, item.key_path)
    if key in items and items[key] != item:
        raise ValueError
    items[key] = item


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".djinn-migrate-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _problem_from_issue(
    identifier: str, tool: ConfigSyncSource, path: PurePosixPath | None
) -> SyncProblem:
    return SyncProblem(identifier, "Workflow artifact is invalid.", tool, path)


def _invalid_audit(
    source: ConfigSyncSource, problems: tuple[SyncProblem, ...] | list[SyncProblem]
) -> ConfigSyncAudit:
    return ConfigSyncAudit(
        source,
        None,
        (DriftItem(DriftClass.INVALID_OR_SEMANTIC, "Workflow artifact is invalid."),),
        tuple(problems),
    )


def _audit_for(
    source: ConfigSyncSource, kind: DriftClass, manifest_source: ConfigSyncSource | None = None
) -> ConfigSyncAudit:
    return ConfigSyncAudit(
        source,
        manifest_source,
        () if kind is DriftClass.CLEAN else (DriftItem(kind, _message(kind)),),
    )


def _drift(kind: DriftClass, path: PurePosixPath) -> DriftItem:
    tool = path.parts[0] if path.parts and path.parts[0] in _TOOLS else None
    relative = PurePosixPath(*path.parts[1:]) if tool is not None and len(path.parts) > 1 else None
    return DriftItem(kind, _message(kind), tool, relative)


def _message(kind: DriftClass) -> str:
    return {
        DriftClass.SOURCE_CHANGED: "Source projection changed or needs synchronization.",
        DriftClass.TARGET_DRIFT: "Managed target was edited.",
        DriftClass.COLLISION: "Unmanaged item occupies a managed path.",
        DriftClass.INVALID_OR_SEMANTIC: "Workflow artifact is invalid or not portable.",
        DriftClass.CLEAN: "",
    }[kind]


def _json_value(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _deduplicate(items: list[DriftItem]) -> list[DriftItem]:
    return list(dict.fromkeys(items))


def _deduplicate_problems(items: list[SyncProblem]) -> list[SyncProblem]:
    return list(dict.fromkeys(items))


def _worst_drift(items: list[DriftItem]) -> DriftClass | None:
    classes = {item.kind for item in items}
    for kind in (
        DriftClass.INVALID_OR_SEMANTIC,
        DriftClass.COLLISION,
        DriftClass.TARGET_DRIFT,
        DriftClass.SOURCE_CHANGED,
    ):
        if kind in classes:
            return kind
    return None
