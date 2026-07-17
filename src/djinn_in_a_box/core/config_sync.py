"""Canonical workflow synchronization backed by the shared publisher."""

from __future__ import annotations

import json
import re
import shutil
import stat
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core.config_sync_adapters import (
    OWNERSHIP_MATRIX,
    AdapterReadResult,
    NativeOnlyReadResult,
    RenderedFile,
    SettingsFragment,
    fragment_is_owned,
    is_safe_relative_path,
    native_only_file_is_owned,
    native_only_fragment_is_owned,
    path_is_owned,
    read_native_only_workflow,
    read_native_workflow,
    render_native_workflow,
    validate_rendered_workflow,
)
from djinn_in_a_box.core.workflow_publisher import (
    CANONICAL_MANIFEST_NAME,
    CanonicalLockLease,
    CarrierFragment,
    DriftClass,
    ManifestError,
    PublishedFile,
    PublishError,
    WorkflowView,
    canonical_lock,
    decode_lean_manifest,
    load_strict_json,
    publish_workflow_view,
    snapshot_file_view,
)

MANIFEST_NAME = CANONICAL_MANIFEST_NAME
CANONICAL_REMEDY = (
    "Author or edit the artifact natively in the target tool's view, "
    "or make the source form portable."
)
_TOOLS: tuple[ConfigSyncSource, ...] = ("claude", "codex", "opencode")
_LEGACY_ARTIFACT_KINDS = frozenset(
    {"instructions", "agent", "skill", "command", "context", "hook"}
)
_LEGACY_ARTIFACT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


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
    view: WorkflowView | None = None
    revision: str | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class _Build:
    source: ConfigSyncSource
    fingerprint: str
    canonical: WorkflowView
    views: Mapping[ConfigSyncSource, WorkflowView]
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
    except (OSError, PublishError):
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
            source = load_config(config_path).config_sync.source
            build = _snapshot_build(project_root, source)
            if build.problems:
                return ConfigSyncResult(False, _invalid_audit(source, build.problems))
            manifest_path = config_root / MANIFEST_NAME
            legacy = _legacy_manifest(manifest_path)
            preflight_manifest: bytes | None
            if legacy is not None:
                if _fingerprint(project_root / "config" / source) != build.fingerprint:
                    return ConfigSyncResult(
                        False, _audit_for(source, DriftClass.SOURCE_CHANGED), retryable=True
                    )
                try:
                    preflight_manifest = _migrate_legacy(
                        config_root, legacy, source, build.canonical
                    )
                except ValueError:
                    return ConfigSyncResult(
                        False, _audit_for(source, DriftClass.INVALID_OR_SEMANTIC)
                    )
            else:
                preflight_manifest = _release_manifest_records(
                    manifest_path, source, build.fingerprint, config_root
                )
            result = publish_workflow_view(
                build.canonical,
                config_root,
                config_root,
                manifest_path,
                canonical_lease=lease,
                source_root=config_root / source,
                preflight_manifest=preflight_manifest,
            )
            audit = (
                _audit_for(source, DriftClass.SOURCE_CHANGED)
                if result.drift_class is DriftClass.SOURCE_CHANGED
                else _audit_locked(project_root, source)
            )
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
    except (OSError, PublishError, ValueError):
        return ConfigSyncResult(False, _audit_for(source, DriftClass.INVALID_OR_SEMANTIC))


def load_canonical_delivery_view(
    project_root: Path,
    tool: ConfigSyncSource,
    *,
    config_path: Path | None = None,
    canonical_lease: CanonicalLockLease | None = None,
) -> CanonicalDeliveryViewResult:
    source = load_config(config_path).config_sync.source
    config_root = project_root / "config"
    try:
        if canonical_lease is not None:
            return _load_canonical_delivery_view_locked(project_root, tool, source)
        with canonical_lock(config_root, exclusive=False):
            return _load_canonical_delivery_view_locked(project_root, tool, source)
    except _BuildError as error:
        return CanonicalDeliveryViewResult(
            False,
            _audit_for(source, error.drift),
            retryable=error.drift is DriftClass.SOURCE_CHANGED,
        )
    except (KeyError, OSError, PublishError):
        return CanonicalDeliveryViewResult(
            False, _audit_for(source, DriftClass.INVALID_OR_SEMANTIC)
        )


def _load_canonical_delivery_view_locked(
    project_root: Path, tool: ConfigSyncSource, source: ConfigSyncSource
) -> CanonicalDeliveryViewResult:
    audit = _audit_locked(project_root, source)
    if not audit.clean:
        return CanonicalDeliveryViewResult(False, audit)
    build = _snapshot_build(project_root, source)
    if build.problems:
        return CanonicalDeliveryViewResult(False, _invalid_audit(source, build.problems))
    view = build.views[tool]
    manifest = (project_root / "config" / MANIFEST_NAME).read_bytes()
    revision = _digest(manifest + build.fingerprint.encode())
    return CanonicalDeliveryViewResult(True, audit, view, revision)


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
            source_name, _items = _legacy_items(
                legacy,
                project_root / "config",
                _migration_stale_residue(build.canonical, source),
            )
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
            return _build_views(snapshot_root, source, before, project_root / "config")
    except _BuildError:
        raise
    except (OSError, RuntimeError) as error:
        raise _BuildError(DriftClass.INVALID_OR_SEMANTIC) from error


def _build_views(
    snapshot_root: Path,
    source: ConfigSyncSource,
    fingerprint: str,
    config_root: Path,
) -> _Build:
    read = read_native_workflow(snapshot_root, source)
    problems = [
        _problem_from_issue(issue.identifier, source, issue.relative_path)
        for issue in read.validation_issues
    ]
    source_files, source_fragments, source_problems = _source_view(snapshot_root, read)
    problems.extend(source_problems)
    projection_views: dict[
        ConfigSyncSource, tuple[tuple[RenderedFile, ...], tuple[SettingsFragment, ...]]
    ] = {source: (source_files, source_fragments)}
    delivery_views = dict(projection_views)
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
        projection_views[target] = (rendered.files, rendered.settings_fragments)
        native_only = read_native_only_workflow(config_root / target, target)
        for issue in native_only.validation_issues:
            problems.append(_problem_from_issue(issue.identifier, target, issue.relative_path))
        delivery_views[target] = _with_native_only(
            rendered.files, rendered.settings_fragments, native_only
        )
    for tool, (rendered_files, rendered_fragments) in delivery_views.items():
        for issue in validate_rendered_workflow(tool, rendered_files, rendered_fragments):
            problems.append(_problem_from_issue(issue.identifier, tool, issue.relative_path))
    views: dict[ConfigSyncSource, WorkflowView] = {}
    for tool, (rendered_files, rendered_fragments) in delivery_views.items():
        views[tool] = WorkflowView(
            source,
            tuple(
                PublishedFile(item.relative_path, item.content, item.executable)
                for item in rendered_files
            ),
            tuple(
                CarrierFragment(item.carrier_path, item.key_path, item.value_json)
                for item in rendered_fragments
            ),
            source_fingerprint=fingerprint,
            target_tool=tool,
        )
    published_files: list[PublishedFile] = []
    published_fragments: list[CarrierFragment] = []
    for tool, (rendered_files, rendered_fragments) in projection_views.items():
        prefix = PurePosixPath(tool)
        companion = OWNERSHIP_MATRIX[source].instruction_companion
        publish_files = (
            rendered_files
            if tool != source
            else tuple(item for item in rendered_files if item.relative_path == companion)
        )
        published_files.extend(
            PublishedFile(prefix / item.relative_path, item.content, item.executable)
            for item in publish_files
        )
        published_fragments.extend(
            CarrierFragment(prefix / item.carrier_path, item.key_path, item.value_json)
            for item in rendered_fragments
            if tool != source
        )
    return _Build(
        source,
        fingerprint,
        WorkflowView(
            source,
            tuple(published_files),
            tuple(published_fragments),
            source_fingerprint=fingerprint,
        ),
        views,
        tuple(_deduplicate_problems(problems)),
    )


def _with_native_only(
    files: tuple[RenderedFile, ...],
    fragments: tuple[SettingsFragment, ...],
    native_only: NativeOnlyReadResult,
) -> tuple[tuple[RenderedFile, ...], tuple[SettingsFragment, ...]]:
    rendered_files = {item.relative_path: item for item in files}
    rendered_fragments = {(item.carrier_path, item.key_path): item for item in fragments}
    for artifact in native_only.artifacts:
        item = RenderedFile(
            artifact.source_path,
            artifact.content,
            artifact.identifier,
            artifact.executable,
        )
        if item.relative_path in rendered_files:
            raise _BuildError(DriftClass.INVALID_OR_SEMANTIC)
        rendered_files[item.relative_path] = item
    for item in native_only.settings_fragments:
        key = item.carrier_path, item.key_path
        if key in rendered_fragments:
            raise _BuildError(DriftClass.INVALID_OR_SEMANTIC)
        rendered_fragments[key] = item
    return (
        tuple(sorted(rendered_files.values(), key=lambda item: item.relative_path)),
        tuple(
            sorted(
                rendered_fragments.values(), key=lambda item: (item.carrier_path, item.key_path)
            )
        ),
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
        return [_drift(DriftClass.TARGET_DRIFT, path)]
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


def _migrate_legacy(
    config_root: Path,
    legacy: Mapping[str, object],
    selected_source: ConfigSyncSource,
    desired: WorkflowView,
) -> bytes:
    manifest_source, items = _legacy_items(
        legacy,
        config_root,
        _migration_stale_residue(desired, selected_source),
    )
    managed = tuple(
        item
        for item in items
        if not _release_canonical_manifest_item(item, selected_source)
    )
    return _encode_lean(manifest_source, managed)


def _release_manifest_records(
    manifest_path: Path,
    source: ConfigSyncSource,
    fingerprint: str,
    config_root: Path,
) -> bytes | None:
    if not manifest_path.exists():
        return
    manifest_source, items = _lean_items(manifest_path.read_bytes())
    kept = tuple(
        item for item in items if not _release_canonical_manifest_item(item, source)
    )
    if kept == items:
        return None
    if manifest_source != source and _fingerprint(config_root / source) != fingerprint:
        raise _BuildError(DriftClass.SOURCE_CHANGED)
    return _encode_lean(manifest_source, kept)


def _release_canonical_manifest_item(item: _ManifestItem, source: ConfigSyncSource) -> bool:
    tool = cast(ConfigSyncSource, item.path.parts[0])
    relative_path = PurePosixPath(*item.path.parts[1:])
    if tool == source:
        companion = OWNERSHIP_MATRIX[source].instruction_companion
        return item.key_path is not None or relative_path != companion
    if item.key_path is None:
        return native_only_file_is_owned(tool, relative_path)
    return native_only_fragment_is_owned(tool, relative_path, item.key_path)


def _migration_stale_residue(
    desired: WorkflowView, selected_source: ConfigSyncSource
) -> Callable[[_ManifestItem], bool]:
    wanted = {
        (item.relative_path, None)
        for item in desired.files
    } | {
        (item.carrier_path, item.key_path)
        for item in desired.fragments
    }

    def is_stale(item: _ManifestItem) -> bool:
        return _release_canonical_manifest_item(item, selected_source) or (
            item.path.parts[0] != selected_source and (item.path, item.key_path) not in wanted
        )

    return is_stale


def _legacy_items(
    data: Mapping[str, object],
    config_root: Path,
    allow_missing: Callable[[_ManifestItem], bool] | None = None,
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
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError
    _legacy_adapter_revision(data["adapter_revision"])
    _valid_hash(data["source_hash"])
    source: ConfigSyncSource = active_source
    items: dict[tuple[PurePosixPath, tuple[str, ...] | None], _ManifestItem] = {}
    _legacy_file_map(data["source_files"], PurePosixPath(source), config_root, items, allow_missing)
    managed = _object_mapping(managed_raw)
    if set(managed) != set(_TOOLS):
        raise ValueError
    for tool in _TOOLS:
        entry = managed[tool]
        values = _object_mapping(entry)
        if set(values) != {"files", "native_only", "fragments"}:
            raise ValueError
        prefix = PurePosixPath(tool)
        _legacy_file_map(values["files"], prefix, config_root, items, allow_missing)
        _legacy_file_map(values["native_only"], prefix, config_root, items, allow_missing)
        _legacy_fragments(values["fragments"], prefix, config_root, items, allow_missing)
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
        _legacy_adapter_revision(semantic_record["adapter_revision"])
        _valid_hash(semantic_record["fingerprint"])
        source_path = _legacy_semantic_source_path(semantic_record["source_path"])
        _legacy_semantic_artifact_id(semantic_record["artifact_id"], source_path)
        semantic_files = semantic_record["files"]
        semantic_fragments = semantic_record["fragments"]
        if (
            semantic_record.get("source_tool") != source
            or semantic_record.get("target_tool") not in _TOOLS
            or semantic_record.get("target_tool") == source
            or not isinstance(semantic_files, list)
            or not isinstance(semantic_fragments, list)
            or not semantic_files and not semantic_fragments
        ):
            raise ValueError
        target = cast(ConfigSyncSource, semantic_record["target_tool"])
        _verify_legacy_semantic_outputs(cast(list[object], semantic_files), target, items)
        _verify_legacy_semantic_fragments(cast(list[object], semantic_fragments), target, items)
    return source, tuple(sorted(items.values(), key=lambda item: (item.path, item.key_path or ())))


def _legacy_semantic_source_path(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise ValueError
    path = PurePosixPath(value)
    if value != path.as_posix() or not is_safe_relative_path(path):
        raise ValueError
    return path


def _legacy_semantic_artifact_id(value: object, source_path: PurePosixPath) -> None:
    if not isinstance(value, str):
        raise ValueError
    parts = value.split(":", 2)
    if len(parts) != 3:
        raise ValueError
    kind, name, path_value = parts
    if kind not in _LEGACY_ARTIFACT_KINDS or _LEGACY_ARTIFACT_NAME.fullmatch(name) is None:
        raise ValueError
    if _legacy_semantic_source_path(path_value) != source_path:
        raise ValueError


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
        if not is_safe_relative_path(path) or not path_is_owned(target, path):
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
        if (
            not key_path
            or any(not key for key in key_path)
            or not fragment_is_owned(target, PurePosixPath(carrier), key_path)
        ):
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
    allow_missing: Callable[[_ManifestItem], bool] | None,
) -> None:
    for raw_path, raw_state in _object_mapping(raw).items():
        relative = PurePosixPath(raw_path)
        state = _object_mapping(raw_state)
        tool = prefix.parts[0] if len(prefix.parts) == 1 else ""
        if (
            tool not in _TOOLS
            or not is_safe_relative_path(relative)
            or not path_is_owned(tool, relative)
            or set(state) != {"hash", "executable"}
        ):
            raise ValueError
        item = _ManifestItem(
            prefix / relative, _valid_hash(state["hash"]), _bool(state["executable"])
        )
        actual = _file_item_at(config_root / item.path, item.path)
        missing_stale_residue = (
            actual is None
            and allow_missing is not None
            and allow_missing(item)
            and _is_missing(config_root / item.path)
        )
        if actual != item and not missing_stale_residue:
            raise ValueError
        _add_item(result, item)


def _legacy_fragments(
    raw: object,
    prefix: PurePosixPath,
    config_root: Path,
    result: dict[tuple[PurePosixPath, tuple[str, ...] | None], _ManifestItem],
    allow_missing: Callable[[_ManifestItem], bool] | None,
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
        tool = prefix.parts[0] if len(prefix.parts) == 1 else ""
        if (
            tool not in _TOOLS
            or not is_safe_relative_path(path)
            or not keys
            or any(not key for key in keys)
            or not fragment_is_owned(tool, path, keys)
        ):
            raise ValueError
        item = _ManifestItem(prefix / path, _valid_hash(data["value_hash"]), False, keys)
        actual, issue = _carrier_item_at(config_root / item.path, item.path, keys)
        if issue or (
            actual != item
            and not (actual is None and allow_missing is not None and allow_missing(item))
        ):
            raise ValueError
        _add_item(result, item)


def _lean_items(raw: bytes) -> tuple[ConfigSyncSource, tuple[_ManifestItem, ...]]:
    try:
        manifest = decode_lean_manifest(raw, canonical_target=True, target_tool=None)
    except ManifestError as error:
        raise ValueError from error
    source = cast(ConfigSyncSource, manifest.source)
    return source, tuple(
        _ManifestItem(item.path, item.content_hash, item.executable, item.key_path)
        for item in manifest.items
    )


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
    except (ManifestError, OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
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
        value = load_strict_json(item.value_json)
    except ManifestError as error:
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


def _is_missing(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


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


def _legacy_adapter_revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 3:
        raise ValueError
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
    return load_strict_json(raw)


def _toml_load(raw: bytes) -> object:
    return cast(object, tomllib.loads(raw.decode()))


def _add_item(
    items: dict[tuple[PurePosixPath, tuple[str, ...] | None], _ManifestItem], item: _ManifestItem
) -> None:
    key = (item.path, item.key_path)
    if key in items:
        raise ValueError
    items[key] = item


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
