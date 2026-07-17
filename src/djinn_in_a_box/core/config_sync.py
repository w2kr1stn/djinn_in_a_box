"""Deterministic audit and publication for managed agent workflow views."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import cast

import tomli_w

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncSource
from djinn_in_a_box.core.atomic_mutation import rename_noreplace
from djinn_in_a_box.core.config_delivery import DeliveryView, deliver_config_view
from djinn_in_a_box.core.config_lock import (
    ConfigDirectoryLockError,
    config_directory_lock,
    directory_is_attached,
)
from djinn_in_a_box.core.config_sync_adapters import (
    ADAPTER_REVISION,
    OWNERSHIP_MATRIX,
    AdapterReadResult,
    ArtifactKind,
    ArtifactOutputContract,
    RenderedFile,
    SettingsFragment,
    UnresolvedItem,
    ValidationIssue,
    allowed_outputs_for_unresolved,
    fragment_is_owned,
    is_safe_relative_path,
    native_only_path_is_owned,
    path_is_owned,
    read_native_workflow,
    render_native_workflow,
    validate_rendered_workflow,
)
from djinn_in_a_box.core.config_sync_agent import (
    SemanticFailure,
    resolve_unresolved_item,
    semantic_artifact_fingerprint,
    start_semantic_deadline,
)
from djinn_in_a_box.core.docker import ensure_host_env, get_config_root
from djinn_in_a_box.core.seeding import SeedingError, pristine_workflow_seed_digest, seed_config

MANIFEST_NAME = ".djinn-config-sync.json"
_SCHEMA_VERSION = 1
_TOOLS: tuple[ConfigSyncSource, ...] = ("claude", "codex", "opencode")
_HASH_LENGTH = 64


class _RaceError(OSError):
    """A managed path changed after it was observed."""


class _QuarantinePreservedError(_RaceError):
    """A concurrent writer prevented lossless restoration of quarantined bytes."""


class _StageCreateError(OSError):
    """A private publication stage could not be created or opened."""


class DriftClass(StrEnum):
    """Public drift categories without workflow content."""

    CLEAN = "clean"
    SOURCE_ONLY = "source-only"
    MANAGED_TARGET = "managed-target-drift"
    UNMANAGED_COLLISION = "unmanaged-collision"
    INVALID_VIEW = "invalid-view"
    SOURCE_SWITCH = "source-switch"
    SEMANTIC_REQUIRED = "semantic-agent-required"
    MANIFEST_INVALID = "manifest-invalid"
    SOURCE_CHANGED = "source-changed"


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
    """Sanitized, compare-and-swap protected canonical delivery view."""

    success: bool
    audit: ConfigSyncAudit
    view: DeliveryView | None = None
    revision: str | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class _FragmentRecord:
    carrier_path: PurePosixPath
    key_path: tuple[str, ...]
    value_hash: str


@dataclass(frozen=True, slots=True)
class _FileState:
    content_hash: str
    executable: bool


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
class _CarrierState:
    content: bytes
    executable: bool


@dataclass(frozen=True, slots=True)
class _SemanticRecord:
    fingerprint: str
    adapter_revision: int
    source_tool: ConfigSyncSource
    target_tool: ConfigSyncSource
    artifact_id: str
    source_path: PurePosixPath
    files: dict[PurePosixPath, _FileState]
    fragments: dict[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord]


_SemanticKey = tuple[ConfigSyncSource, ConfigSyncSource, str, PurePosixPath]


@dataclass(frozen=True, slots=True)
class _ToolManifest:
    files: dict[PurePosixPath, _FileState] = field(default_factory=lambda: {})
    native_only: dict[PurePosixPath, _FileState] = field(default_factory=lambda: {})
    fragments: dict[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord] = field(
        default_factory=lambda: {}
    )


@dataclass(frozen=True, slots=True)
class _Manifest:
    active_source: ConfigSyncSource
    source_hash: str
    source_files: dict[PurePosixPath, _FileState]
    managed: dict[ConfigSyncSource, _ToolManifest]
    semantic: dict[_SemanticKey, _SemanticRecord]


@dataclass(frozen=True, slots=True)
class _DesiredTool:
    files: dict[PurePosixPath, RenderedFile]
    fragments: dict[tuple[PurePosixPath, tuple[str, ...]], SettingsFragment]


@dataclass(frozen=True, slots=True)
class _DesiredState:
    source: ConfigSyncSource
    source_hash: str
    source_files: dict[PurePosixPath, _FileState]
    native_only: dict[ConfigSyncSource, dict[PurePosixPath, _FileState]]
    tools: dict[ConfigSyncSource, _DesiredTool]
    problems: tuple[SyncProblem, ...]
    unresolved: tuple[UnresolvedItem, ...] = ()
    semantic: dict[_SemanticKey, _SemanticRecord] = field(default_factory=lambda: {})


@dataclass(slots=True)
class _Observations:
    files: dict[tuple[ConfigSyncSource, PurePosixPath], _FileState | None] = field(
        default_factory=lambda: {}
    )
    carriers: dict[tuple[ConfigSyncSource, PurePosixPath], _CarrierState | None] = field(
        default_factory=lambda: {}
    )


@dataclass(frozen=True, slots=True)
class _Analysis:
    audit: ConfigSyncAudit
    observations: _Observations


class _SemanticPreflightError(Exception):
    def __init__(self, audit: ConfigSyncAudit) -> None:
        self.audit = audit


def audit_config_sync(
    project_root: Path,
    *,
    config_path: Path | None = None,
) -> ConfigSyncAudit:
    """Audit deterministic workflow drift under a shared, artifact-free lock."""
    config_dir = project_root / "config"
    if config_dir.is_symlink() or not config_dir.is_dir():
        source = load_config(config_path).config_sync.source
        return _config_directory_failure(source)
    try:
        with config_directory_lock(config_dir, exclusive=False):
            source = load_config(config_path).config_sync.source
            manifest, manifest_problem = _load_manifest(config_dir / MANIFEST_NAME)
            if manifest_problem is not None:
                return _manifest_failure(source, manifest_problem)
            desired = _read_audit_desired(project_root, config_dir / source, source, manifest)
            audit = _analyze(project_root, desired, manifest).audit
            return _with_drifts(audit, _publication_layout_drifts(config_dir, desired, manifest))
    except ConfigDirectoryLockError:
        source = load_config(config_path).config_sync.source
        return _config_directory_failure(source)


def load_canonical_delivery_view(
    project_root: Path,
    tool: ConfigSyncSource,
    *,
    config_path: Path | None = None,
) -> CanonicalDeliveryViewResult:
    """Load one exact canonical runtime view without invoking an agent."""
    config_dir = project_root / "config"
    source = load_config(config_path).config_sync.source
    if config_dir.is_symlink() or not config_dir.is_dir():
        return CanonicalDeliveryViewResult(False, _config_directory_failure(source))
    try:
        with config_directory_lock(config_dir, exclusive=False):
            source = load_config(config_path).config_sync.source
            manifest, manifest_problem = _load_manifest(config_dir / MANIFEST_NAME)
            if manifest_problem is not None:
                return CanonicalDeliveryViewResult(
                    False, _manifest_failure(source, manifest_problem)
                )
            manifest_state, manifest_issue = _file_state(config_dir, PurePosixPath(MANIFEST_NAME))
            if manifest_issue is not None:
                return CanonicalDeliveryViewResult(False, _manifest_failure(source, manifest_issue))
            source_root = config_dir / source
            source_issue = _source_link_problem(source_root, source)
            if source_issue is not None:
                message, relative = source_issue
                invalid = _invalid_source_root(source, message, relative)
                return CanonicalDeliveryViewResult(
                    False, _analyze(project_root, invalid, manifest).audit
                )
            before_hash, before_material = _source_fingerprints(source_root, source)
            with tempfile.TemporaryDirectory(prefix="djinn-canonical-view-") as temporary:
                snapshot_root = Path(temporary) / source
                _snapshot_source(source_root, snapshot_root, source)
                after_hash, after_material = _source_fingerprints(source_root, source)
                _, snapshot_material = _source_fingerprints(snapshot_root, source)
                if (
                    after_hash != before_hash
                    or after_material != before_material
                    or snapshot_material != before_material
                ):
                    return CanonicalDeliveryViewResult(
                        False,
                        _source_changed_audit(source, manifest),
                        retryable=True,
                    )
                desired = _read_desired(
                    project_root, snapshot_root, source, source_hash=before_hash
                )
                if not desired.problems:
                    desired = _resolve_semantic_desired(
                        project_root, desired, manifest, allow_agent=False
                    )
                analysis = _analyze(project_root, desired, manifest)
                audit = _with_drifts(
                    analysis.audit,
                    _publication_layout_drifts(config_dir, desired, manifest),
                )
                if not audit.clean:
                    return CanonicalDeliveryViewResult(False, audit)
                view, view_problems = _delivery_view_from_desired(snapshot_root, desired, tool)
                if view is None:
                    failed_audit = ConfigSyncAudit(
                        source,
                        manifest.active_source if manifest is not None else None,
                        (DriftItem(DriftClass.INVALID_VIEW, "Canonical view is invalid."),),
                        view_problems,
                    )
                    return CanonicalDeliveryViewResult(False, failed_audit)
                if (
                    not _source_matches_snapshot(
                        config_path,
                        source_root,
                        source,
                        before_hash,
                        before_material,
                    )
                    or _file_state(config_dir, PurePosixPath(MANIFEST_NAME))[0] != manifest_state
                    or not _semantic_targets_unchanged(project_root, analysis.observations)
                ):
                    return CanonicalDeliveryViewResult(
                        False,
                        _source_changed_audit(
                            source,
                            manifest,
                            message="Canonical configuration changed while loading its view.",
                        ),
                        retryable=True,
                    )
                if manifest_state is None:
                    return CanonicalDeliveryViewResult(
                        False, _manifest_failure(source, "Synchronization manifest is missing.")
                    )
                revision = _digest(
                    b"\x00".join(
                        (
                            source.encode(),
                            before_hash.encode(),
                            before_material.encode(),
                            manifest_state.content_hash.encode(),
                        )
                    )
                )
                return CanonicalDeliveryViewResult(True, audit, view, revision)
    except ConfigDirectoryLockError:
        return CanonicalDeliveryViewResult(False, _config_directory_failure(source))
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        audit = ConfigSyncAudit(
            source,
            None,
            (DriftItem(DriftClass.INVALID_VIEW, "Canonical view could not be loaded safely."),),
            (SyncProblem("canonical-view", f"Canonical view failed: {type(error).__name__}."),),
        )
        return CanonicalDeliveryViewResult(False, audit, retryable=True)


def sync_config(
    project_root: Path,
    *,
    config_path: Path | None = None,
    allow_agent: bool = False,
) -> ConfigSyncResult:
    """Explicitly publish deterministic managed views and a schema-v1 manifest."""
    try:
        return _sync_config(project_root, config_path=config_path, allow_agent=allow_agent)
    except ConfigDirectoryLockError:
        source = load_config(config_path).config_sync.source
        return ConfigSyncResult(False, _config_directory_failure(source))


def _sync_config(
    project_root: Path,
    *,
    config_path: Path | None,
    allow_agent: bool,
) -> ConfigSyncResult:
    """Explicitly publish deterministic managed views and a schema-v1 manifest."""
    config_dir = project_root / "config"
    source = load_config(config_path).config_sync.source
    if config_dir.is_symlink() or not config_dir.is_dir():
        return ConfigSyncResult(False, _config_directory_failure(source))

    with config_directory_lock(config_dir, exclusive=True) as config_fd:
        _require_config_root_attached(config_dir, config_fd)
        config = load_config(config_path)
        source = config.config_sync.source
        manifest, manifest_problem = _load_manifest(config_dir / MANIFEST_NAME)
        if manifest_problem is not None:
            return ConfigSyncResult(False, _manifest_failure(source, manifest_problem))
        manifest_observed, manifest_issue = _file_state(config_dir, PurePosixPath(MANIFEST_NAME))
        if manifest_issue is not None:
            return ConfigSyncResult(False, _manifest_failure(source, manifest_issue))

        stage: _Stage | None = None
        try:
            _require_config_root_attached(config_dir, config_fd)
            stage = _create_stage(config_fd)
            source_root = config_dir / source
            source_issue = _source_link_problem(source_root, source)
            if source_issue is not None:
                message, relative = source_issue
                desired = _invalid_source_root(source, message, relative)
                return ConfigSyncResult(False, _analyze(project_root, desired, manifest).audit)
            before_hash, before_material = _source_fingerprints(source_root, source)
            _snapshot_source_at(config_fd, stage, source_root, source)
            _stage_attached(config_fd, stage)
            snapshot_root = Path("/proc/self/fd") / str(stage.descriptor) / "source"
            _, snapshot_material = _source_fingerprints(snapshot_root, source)
            _stage_attached(config_fd, stage)
            after_hash, after_material = _source_fingerprints(source_root, source)
            if (
                before_hash != after_hash
                or before_material != after_material
                or before_material != snapshot_material
            ):
                audit = _source_changed_audit(source, manifest)
                return ConfigSyncResult(False, audit, retryable=True)

            desired = _read_desired(project_root, snapshot_root, source, source_hash=before_hash)
            if desired.problems:
                return ConfigSyncResult(False, _analyze(project_root, desired, manifest).audit)
            pre_analysis = _analyze(project_root, desired, manifest)
            pre_layout_drifts = _publication_layout_drifts(config_dir, desired, manifest)
            if pre_layout_drifts:
                return ConfigSyncResult(False, _with_drifts(pre_analysis.audit, pre_layout_drifts))
            if _blocks_sync(pre_analysis.audit, manifest):
                return ConfigSyncResult(False, pre_analysis.audit)
            desired = _resolve_semantic_desired(
                project_root,
                desired,
                manifest,
                allow_agent=allow_agent,
                source_guard=lambda: _source_matches_snapshot(
                    config_path,
                    source_root,
                    source,
                    before_hash,
                    before_material,
                ),
                agent_preflight=lambda: _prepare_semantic_runtime(
                    project_root,
                    config,
                    snapshot_root,
                    desired,
                ),
            )
            analysis = _analyze(project_root, desired, manifest)
            layout_drifts = _publication_layout_drifts(config_dir, desired, manifest)
            if layout_drifts:
                return ConfigSyncResult(False, _with_drifts(analysis.audit, layout_drifts))
            if _blocks_sync(analysis.audit, manifest):
                return ConfigSyncResult(False, analysis.audit)
            if analysis.audit.clean:
                _require_config_root_attached(config_dir, config_fd)
                return ConfigSyncResult(True, analysis.audit)

            _require_config_root_attached(config_dir, config_fd)
            staged_files = _stage_files(config_fd, stage, desired)
            if not _source_matches_snapshot(
                config_path, source_root, source, before_hash, before_material
            ):
                audit = _source_changed_audit(source, manifest)
                return ConfigSyncResult(False, audit, retryable=True)
            if not _files_unchanged(project_root, analysis.observations):
                audit = _source_changed_audit(
                    source, manifest, message="Managed target changed during synchronization."
                )
                return ConfigSyncResult(False, audit, retryable=True)

            changed, removed = _publish(
                stage,
                desired,
                manifest,
                analysis.observations,
                config_fd,
                staged_files,
            )
            _require_config_root_attached(config_dir, config_fd)
            final_manifest = _manifest_from_desired(desired, manifest)
            if not _analyze(project_root, desired, final_manifest).audit.clean:
                raise _RaceError("Published configuration detached before manifest publication.")
            manifest_stage = _stage_bytes(
                config_fd,
                stage,
                PurePosixPath("manifest.json"),
                _serialize_manifest(final_manifest),
                False,
            )
            _replace_file_at(
                config_fd,
                stage,
                manifest_stage,
                PurePosixPath(MANIFEST_NAME),
                manifest_observed,
            )
            _require_config_root_attached(config_dir, config_fd)

            after = _analyze(project_root, desired, final_manifest).audit
            if not after.clean:
                raise _RaceError("Published configuration changed after manifest publication.")
            return ConfigSyncResult(
                True,
                after,
                tuple(changed),
                tuple(removed),
            )
        except _SemanticPreflightError as error:
            return ConfigSyncResult(False, error.audit)
        except _StageCreateError:
            audit = ConfigSyncAudit(
                source,
                manifest.active_source if manifest is not None else None,
                problems=(SyncProblem("stage-create-failed", "Stage creation failed."),),
            )
            return ConfigSyncResult(False, audit, retryable=True)
        except _QuarantinePreservedError:
            base = _source_changed_audit(
                source,
                manifest,
                message="Managed configuration changed during synchronization.",
            )
            audit = ConfigSyncAudit(
                base.configured_source,
                base.manifest_source,
                base.drifts,
                (
                    *base.problems,
                    SyncProblem("quarantine-preserved", "Recovery data was preserved."),
                ),
            )
            return ConfigSyncResult(False, audit, retryable=True)
        except _RaceError:
            audit = _source_changed_audit(
                source,
                manifest,
                message="Managed configuration changed during synchronization.",
            )
            return ConfigSyncResult(False, audit, retryable=True)
        except (OSError, ValueError, TypeError) as error:
            problem = SyncProblem("publication", f"Synchronization failed: {type(error).__name__}.")
            audit = ConfigSyncAudit(
                source,
                manifest.active_source if manifest else None,
                (DriftItem(DriftClass.INVALID_VIEW, "Synchronization did not publish safely."),),
                (problem,),
            )
            return ConfigSyncResult(False, audit, retryable=True)
        finally:
            if stage is not None:
                _cleanup_stage(config_fd, stage)


def _prepare_semantic_host(project_root: Path, config: AppConfig) -> SyncProblem | None:
    try:
        ensure_host_env(config)
        seed_config(project_root, source=config.config_sync.source)
    except (OSError, SeedingError, ValueError, TypeError) as error:
        return SyncProblem(
            "semantic-runtime-bootstrap",
            f"Semantic runtime bootstrap failed: {type(error).__name__}.",
            config.config_sync.source,
        )
    return None


def _prepare_semantic_runtime(
    project_root: Path,
    config: AppConfig,
    source_snapshot_root: Path,
    desired: _DesiredState,
) -> SyncProblem | None:
    problem = _prepare_semantic_host(project_root, config)
    if problem is not None:
        return problem
    if desired.source == "codex":
        return _deliver_codex_semantic_source(config, source_snapshot_root, desired)
    return None


def _deliver_codex_semantic_source(
    config: AppConfig,
    source_snapshot_root: Path,
    desired: _DesiredState,
) -> SyncProblem | None:
    view, problems = _delivery_view_from_desired(source_snapshot_root, desired, "codex")
    if view is None:
        return (
            problems[0]
            if problems
            else SyncProblem("codex-source-view", "Codex source view is invalid.", "codex")
        )
    result = deliver_config_view(view, get_config_root(config) / "codex")
    if not result.success:
        identifier = result.problems[0].identifier if result.problems else "delivery-failed"
        return SyncProblem(
            f"codex-source-{identifier}",
            "Codex source runtime delivery failed.",
            "codex",
        )
    return None


def _read_desired(
    project_root: Path,
    source_root: Path,
    source: ConfigSyncSource,
    *,
    source_hash: str | None = None,
) -> _DesiredState:
    source_issue = _source_link_problem(source_root, source)
    if source_issue is not None:
        message, relative = source_issue
        return _invalid_source_root(source, message, relative)
    problems: list[SyncProblem] = []
    read_result = read_native_workflow(source_root, source)
    problems.extend(_validation_problems(read_result.validation_issues, source))
    native_only_ids = {
        artifact.identifier
        for artifact in read_result.artifacts
        if artifact.native_only_for is not None
    }
    problems.extend(
        SyncProblem(
            item.identifier,
            "Native-only source artifact requires valid native metadata.",
            source,
            item.source_path,
        )
        for item in read_result.unresolved
        if item.identifier in native_only_ids
    )

    tools: dict[ConfigSyncSource, _DesiredTool] = {tool: _DesiredTool({}, {}) for tool in _TOOLS}
    unresolved: dict[tuple[str, ConfigSyncSource, PurePosixPath], UnresolvedItem] = {}
    instruction = next(
        (item for item in read_result.artifacts if item.kind is ArtifactKind.INSTRUCTIONS), None
    )
    if instruction is not None:
        companion = OWNERSHIP_MATRIX[source].instruction_companion
        tools[source].files[companion] = RenderedFile(
            companion, instruction.content, instruction.identifier, instruction.executable
        )

    for target in _TOOLS:
        if target == source:
            continue
        rendered = render_native_workflow(read_result, target)
        semantic_ids = {item.identifier for item in rendered.unresolved}
        replaceable_agent_paths = {
            item.relative_path for item in rendered.files if item.artifact_id in semantic_ids
        }
        immediate_issues = tuple(
            issue
            for issue in rendered.validation_issues
            if not (
                issue.identifier.startswith("invalid-agent:")
                and issue.relative_path in replaceable_agent_paths
            )
        )
        problems.extend(_validation_problems(immediate_issues, target))
        for item in rendered.unresolved:
            if item.target_tool is None:
                continue
            key = (item.identifier, item.target_tool, item.source_path)
            existing = unresolved.get(key)
            if existing is not None and (
                existing.source_bytes != item.source_bytes
                or existing.metadata != item.metadata
                or existing.executable != item.executable
            ):
                problems.append(
                    SyncProblem(
                        item.identifier,
                        "Semantic artifact inputs are inconsistent.",
                        item.target_tool,
                        item.source_path,
                    )
                )
                continue
            unresolved[key] = item
        tools[target].files.update((item.relative_path, item) for item in rendered.files)
        tools[target].fragments.update(
            ((item.carrier_path, item.key_path), item) for item in rendered.settings_fragments
        )

    actual_hash = source_hash or _source_fingerprint(source_root, source)
    source_files = _source_file_hashes(source_root, read_result)
    native_only: dict[ConfigSyncSource, dict[PurePosixPath, _FileState]] = {
        tool: {} for tool in _TOOLS
    }
    native_only[source] = _native_only_file_states(read_result, source)
    return _DesiredState(
        source,
        actual_hash,
        source_files,
        native_only,
        tools,
        tuple(problems),
        tuple(
            sorted(
                unresolved.values(),
                key=lambda item: (
                    item.target_tool or "",
                    item.identifier,
                    item.source_path,
                ),
            )
        ),
    )


def _delivery_view_from_desired(
    source_snapshot_root: Path,
    desired: _DesiredState,
    tool: ConfigSyncSource,
) -> tuple[DeliveryView | None, tuple[SyncProblem, ...]]:
    if tool != desired.source:
        wanted = desired.tools[tool]
        files = tuple(sorted(wanted.files.values(), key=lambda item: item.relative_path))
        fragments = tuple(
            sorted(
                wanted.fragments.values(),
                key=lambda item: (item.carrier_path, item.key_path),
            )
        )
    else:
        source_files: dict[PurePosixPath, RenderedFile] = dict(desired.tools[tool].files)
        problems: list[SyncProblem] = []
        for path, state in sorted(desired.source_files.items()):
            content = _read_cached_file(source_snapshot_root, path, state)
            if content is None:
                problems.append(
                    SyncProblem(
                        "source-view-file",
                        "A recorded source file changed while building its runtime view.",
                        tool,
                        path,
                    )
                )
                continue
            source_files[path] = RenderedFile(
                path, content, f"source:{path.as_posix()}", state.executable
            )
        fragments, fragment_problems = _source_owned_fragments(source_snapshot_root, tool)
        problems.extend(fragment_problems)
        if problems:
            return None, tuple(problems)
        files = tuple(sorted(source_files.values(), key=lambda item: item.relative_path))
    issues = validate_rendered_workflow(tool, files, fragments)
    if issues:
        return None, tuple(
            SyncProblem(issue.identifier, issue.message, tool, issue.relative_path)
            for issue in issues
        )
    return DeliveryView(tool, files, fragments), ()


def _source_owned_fragments(
    source_root: Path,
    tool: ConfigSyncSource,
) -> tuple[tuple[SettingsFragment, ...], tuple[SyncProblem, ...]]:
    keys_by_carrier: dict[PurePosixPath, set[tuple[str, ...]]] = {}
    for hook in OWNERSHIP_MATRIX[tool].hooks:
        if hook.carrier_path is not None and hook.event is not None:
            keys_by_carrier.setdefault(hook.carrier_path, set()).add(("hooks", hook.event))
    if tool == "codex":
        keys_by_carrier.setdefault(PurePosixPath("config.toml"), set()).add(
            ("project_doc_fallback_filenames",)
        )

    fragments: list[SettingsFragment] = []
    problems: list[SyncProblem] = []
    for carrier, keys in sorted(keys_by_carrier.items()):
        state, issue = _read_carrier(source_root, carrier)
        if issue is not None:
            problems.append(SyncProblem("source-carrier", issue, tool, carrier))
            continue
        if state is None:
            continue
        parsed, parse_issue = _parse_carrier(carrier, state.content)
        if parse_issue is not None:
            problems.append(SyncProblem("source-carrier", parse_issue, tool, carrier))
            continue
        for key in sorted(keys):
            found, value, nested_issue = _nested_get(parsed, key)
            if nested_issue is not None:
                problems.append(SyncProblem("source-fragment", nested_issue, tool, carrier))
                continue
            if not found:
                continue
            fragments.append(
                SettingsFragment(
                    carrier,
                    key,
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode(),
                    f"source-fragment:{carrier.as_posix()}:{'.'.join(key)}",
                )
            )
    return (
        tuple(sorted(fragments, key=lambda item: (item.carrier_path, item.key_path))),
        tuple(problems),
    )


def _read_audit_desired(
    project_root: Path,
    source_root: Path,
    source: ConfigSyncSource,
    manifest: _Manifest | None,
) -> _DesiredState:
    source_issue = _source_link_problem(source_root, source)
    if source_issue is not None:
        message, relative = source_issue
        return _invalid_source_root(source, message, relative)
    before_hash, before_material = _source_fingerprints(source_root, source)
    with tempfile.TemporaryDirectory(prefix="djinn-config-sync-audit-") as temporary:
        snapshot_root = Path(temporary) / source
        _snapshot_source(source_root, snapshot_root, source)
        _, snapshot_material = _source_fingerprints(snapshot_root, source)
        after_hash, after_material = _source_fingerprints(source_root, source)
        if (
            before_hash != after_hash
            or before_material != after_material
            or before_material != snapshot_material
        ):
            return _invalid_source_root(source, "Authoritative source changed during audit.")
        desired = _read_desired(
            project_root,
            snapshot_root,
            source,
            source_hash=before_hash,
        )
        if desired.problems:
            return desired
        return _resolve_semantic_desired(project_root, desired, manifest, allow_agent=False)


def _resolve_semantic_desired(
    project_root: Path,
    desired: _DesiredState,
    manifest: _Manifest | None,
    *,
    allow_agent: bool,
    source_guard: Callable[[], bool] | None = None,
    agent_preflight: Callable[[], SyncProblem | None] | None = None,
) -> _DesiredState:
    if not desired.unresolved:
        return desired
    tools: dict[ConfigSyncSource, _DesiredTool] = {
        tool: _DesiredTool(dict(wanted.files), dict(wanted.fragments))
        for tool, wanted in desired.tools.items()
    }
    problems = list(desired.problems)
    semantic: dict[_SemanticKey, _SemanticRecord] = {}
    prepared: list[tuple[UnresolvedItem, ArtifactOutputContract, str]] = []
    claimed_files: dict[tuple[ConfigSyncSource, PurePosixPath], str] = {}
    claimed_fragments: dict[tuple[ConfigSyncSource, PurePosixPath, tuple[str, ...]], str] = {}

    for item in desired.unresolved:
        target = item.target_tool
        if target is None:
            problems.append(_semantic_problem(item, SemanticFailure.INVALID_REQUEST))
            continue
        try:
            contract = allowed_outputs_for_unresolved(item)
            fingerprint = semantic_artifact_fingerprint(desired.source, item)
        except (TypeError, ValueError):
            problems.append(_semantic_problem(item, SemanticFailure.INVALID_REQUEST))
            continue
        conflict = False
        for path in contract.file_paths:
            claim = (target, path)
            owner = claimed_files.get(claim)
            static = tools[target].files.get(path)
            if (owner is not None and owner != item.identifier) or (
                static is not None and static.artifact_id != item.identifier
            ):
                problems.append(_semantic_conflict_problem(item, path))
                conflict = True
            else:
                claimed_files[claim] = item.identifier
        for fragment in contract.settings_fragments:
            claim = (target, fragment.carrier_path, fragment.key_path)
            owner = claimed_fragments.get(claim)
            static = tools[target].fragments.get((fragment.carrier_path, fragment.key_path))
            if (owner is not None and owner != item.identifier) or (
                static is not None and static.artifact_id != item.identifier
            ):
                problems.append(_semantic_conflict_problem(item, fragment.carrier_path))
                conflict = True
            else:
                claimed_fragments[claim] = item.identifier
        if not conflict:
            prepared.append((item, contract, fingerprint))

    if problems:
        return _DesiredState(
            desired.source,
            desired.source_hash,
            desired.source_files,
            desired.native_only,
            tools,
            tuple(problems),
            desired.unresolved,
            semantic,
        )

    pending: list[tuple[UnresolvedItem, ArtifactOutputContract, str]] = []
    for item, contract, fingerprint in prepared:
        target = cast(ConfigSyncSource, item.target_tool)
        _remove_contract_outputs(tools[target], contract)
        key = _semantic_key(desired.source, item)
        record = manifest.semantic.get(key) if manifest is not None else None
        cached = (
            _read_cached_semantic_outputs(project_root, item, contract, record, fingerprint)
            if record is not None
            else None
        )
        if cached is None:
            pending.append((item, contract, fingerprint))
            continue
        assert record is not None
        _apply_semantic_outputs(tools[target], *cached)
        semantic[key] = record

    if pending and allow_agent:
        preflight_drifts = _semantic_pending_preflight(project_root, pending, manifest)
        if preflight_drifts:
            partial = _DesiredState(
                desired.source,
                desired.source_hash,
                desired.source_files,
                desired.native_only,
                tools,
                (),
                desired.unresolved,
                semantic,
            )
            raise _SemanticPreflightError(
                _with_drifts(_analyze(project_root, partial, manifest).audit, preflight_drifts)
            )
        runtime_problem = agent_preflight() if agent_preflight is not None else None
        if runtime_problem is not None:
            partial = _DesiredState(
                desired.source,
                desired.source_hash,
                desired.source_files,
                desired.native_only,
                tools,
                (runtime_problem,),
                desired.unresolved,
                semantic,
            )
            raise _SemanticPreflightError(_analyze(project_root, partial, manifest).audit)
        target_snapshot = _observe_semantic_targets(project_root, desired, manifest)
        deadline = start_semantic_deadline()
        for item, _contract, fingerprint in pending:
            result = resolve_unresolved_item(
                desired.source,
                item,
                deadline=deadline,
            )
            if source_guard is not None and not source_guard():
                raise _RaceError("Authoritative source changed during semantic adaptation.")
            if not _semantic_targets_unchanged(project_root, target_snapshot):
                raise _RaceError("Managed target changed during semantic adaptation.")
            if (
                not result.success
                or result.fingerprint != fingerprint
                or result.fingerprint is None
            ):
                failure = result.failure or SemanticFailure.RESPONSE_MISMATCH
                problems.append(_semantic_problem(item, failure))
                break
            target = item.target_tool
            assert target is not None
            _apply_semantic_outputs(tools[target], result.files, result.settings_fragments)
            record = _semantic_record(
                desired.source,
                item,
                fingerprint,
                result.files,
                result.settings_fragments,
            )
            semantic[_semantic_key(desired.source, item)] = record
    elif pending:
        problems.extend(_semantic_problem(item, None) for item, _contract, _fingerprint in pending)

    for target in _TOOLS:
        if target == desired.source:
            continue
        wanted = tools[target]
        problems.extend(
            _validation_problems(
                validate_rendered_workflow(
                    target, wanted.files.values(), wanted.fragments.values()
                ),
                target,
            )
        )
    return _DesiredState(
        desired.source,
        desired.source_hash,
        desired.source_files,
        desired.native_only,
        tools,
        tuple(problems),
        desired.unresolved,
        semantic,
    )


def _semantic_key(source: ConfigSyncSource, item: UnresolvedItem) -> _SemanticKey:
    target = item.target_tool
    if target is None:
        raise ValueError("Semantic artifact target is missing.")
    return source, target, item.identifier, item.source_path


def _observe_semantic_targets(
    project_root: Path,
    desired: _DesiredState,
    manifest: _Manifest | None,
) -> _Observations:
    observations = _Observations()
    for tool in _TOOLS:
        wanted = desired.tools[tool]
        prior = manifest.managed[tool] if manifest is not None else _ToolManifest()
        paths = set(wanted.files) | set(prior.files)
        carriers = {key[0] for key in wanted.fragments} | {key[0] for key in prior.fragments}
        for item in desired.unresolved:
            if item.target_tool != tool:
                continue
            try:
                contract = allowed_outputs_for_unresolved(item)
            except (TypeError, ValueError):
                continue
            paths.update(contract.file_paths)
            carriers.update(fragment.carrier_path for fragment in contract.settings_fragments)
        root = project_root / "config" / tool
        for path in paths:
            state, _issue = _file_state(root, path)
            observations.files[(tool, path)] = state
        for carrier in carriers:
            state, _issue = _read_carrier(root, carrier)
            observations.carriers[(tool, carrier)] = state
    return observations


def _semantic_targets_unchanged(project_root: Path, observations: _Observations) -> bool:
    if not _files_unchanged(project_root, observations):
        return False
    for (tool, carrier), expected in observations.carriers.items():
        current, issue = _read_carrier(project_root / "config" / tool, carrier)
        if issue is not None or current != expected:
            return False
    return True


def _semantic_pending_preflight(
    project_root: Path,
    pending: list[tuple[UnresolvedItem, ArtifactOutputContract, str]],
    manifest: _Manifest | None,
) -> tuple[DriftItem, ...]:
    drifts: list[DriftItem] = []
    for item, contract, _fingerprint in pending:
        target = item.target_tool
        if target is None:
            continue
        root = project_root / "config" / target
        prior = manifest.managed[target] if manifest is not None else _ToolManifest()
        prior_authority = (
            manifest.source_files
            if manifest is not None and manifest.active_source == target
            else {}
        )
        for path in contract.file_paths:
            parent_issue = _semantic_contract_parent_issue(root, path)
            current, issue = _file_state(root, path)
            if parent_issue is not None or issue is not None:
                drifts.append(
                    DriftItem(
                        DriftClass.INVALID_VIEW,
                        parent_issue or cast(str, issue),
                        target,
                        path,
                    )
                )
            elif (
                path not in prior.files
                and current is not None
                and prior_authority.get(path) != current
            ):
                drifts.append(
                    DriftItem(
                        DriftClass.UNMANAGED_COLLISION,
                        "Unmanaged file occupies a semantic output path.",
                        target,
                        path,
                    )
                )
        for fragment in contract.settings_fragments:
            carrier = fragment.carrier_path
            parent_issue = _semantic_contract_parent_issue(root, carrier)
            state, issue = _read_carrier(root, carrier)
            if parent_issue is not None or issue is not None:
                drifts.append(
                    DriftItem(
                        DriftClass.INVALID_VIEW,
                        parent_issue or cast(str, issue),
                        target,
                        carrier,
                    )
                )
                continue
            raw = state.content if state is not None else None
            data, parse_issue = _parse_carrier(carrier, raw)
            found, _value, nested_issue = _nested_get(data, fragment.key_path)
            if parse_issue is not None or nested_issue is not None:
                drifts.append(
                    DriftItem(
                        DriftClass.INVALID_VIEW,
                        parse_issue or cast(str, nested_issue),
                        target,
                        carrier,
                    )
                )
            elif (carrier, fragment.key_path) not in prior.fragments and found:
                drifts.append(
                    DriftItem(
                        DriftClass.UNMANAGED_COLLISION,
                        "Unmanaged settings occupy a semantic output key.",
                        target,
                        carrier,
                    )
                )
    return _deduplicate_drifts(drifts)


def _semantic_contract_parent_issue(root: Path, path: PurePosixPath) -> str | None:
    current = root
    for part in path.parts[:-1]:
        current /= part
        if not current.exists() and not current.is_symlink():
            return None
        if current.is_symlink() or not current.is_dir():
            return "Semantic output parent must be a real directory."
    return None


def _semantic_problem(item: UnresolvedItem, failure: SemanticFailure | None) -> SyncProblem:
    suffix = "" if failure is None else f" ({failure.value})"
    return SyncProblem(
        item.identifier,
        f"Semantic adaptation required{suffix}.",
        item.target_tool,
        item.source_path,
    )


def _semantic_conflict_problem(item: UnresolvedItem, path: PurePosixPath) -> SyncProblem:
    return SyncProblem(
        item.identifier,
        "Semantic output contract conflicts with another artifact.",
        item.target_tool,
        path,
    )


def _remove_contract_outputs(wanted: _DesiredTool, contract: ArtifactOutputContract) -> None:
    for path in contract.file_paths:
        wanted.files.pop(path, None)
    for fragment in contract.settings_fragments:
        wanted.fragments.pop((fragment.carrier_path, fragment.key_path), None)


def _apply_semantic_outputs(
    wanted: _DesiredTool,
    files: tuple[RenderedFile, ...],
    fragments: tuple[SettingsFragment, ...],
) -> None:
    wanted.files.update((item.relative_path, item) for item in files)
    wanted.fragments.update(((item.carrier_path, item.key_path), item) for item in fragments)


def _read_cached_semantic_outputs(
    project_root: Path,
    item: UnresolvedItem,
    contract: ArtifactOutputContract,
    record: _SemanticRecord,
    fingerprint: str,
) -> tuple[tuple[RenderedFile, ...], tuple[SettingsFragment, ...]] | None:
    target = item.target_tool
    if (
        target is None
        or record.adapter_revision != ADAPTER_REVISION
        or record.fingerprint != fingerprint
    ):
        return None
    root = project_root / "config" / target
    files: list[RenderedFile] = []
    for path, expected in record.files.items():
        content = _read_cached_file(root, path, expected)
        if content is None:
            return None
        files.append(RenderedFile(path, content, item.identifier, expected.executable))
    fragments: list[SettingsFragment] = []
    carriers: dict[PurePosixPath, tuple[dict[str, object], str | None]] = {}
    for key, expected in record.fragments.items():
        carrier, keys = key
        if carrier not in carriers:
            state, issue = _read_carrier(root, carrier)
            raw = state.content if state is not None else None
            carriers[carrier] = _parse_carrier(carrier, raw) if issue is None else ({}, issue)
        parsed, issue = carriers[carrier]
        found, value, nested_issue = _nested_get(parsed, keys)
        if (
            issue is not None
            or nested_issue is not None
            or not found
            or _digest_json(value) != expected.value_hash
        ):
            return None
        value_json = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        fragments.append(SettingsFragment(carrier, keys, value_json, item.identifier))
    rendered_files = tuple(sorted(files, key=lambda value: value.relative_path))
    rendered_fragments = tuple(
        sorted(fragments, key=lambda value: (value.carrier_path, value.key_path))
    )
    if _semantic_output_issues(target, contract, rendered_files, rendered_fragments):
        return None
    return rendered_files, rendered_fragments


def _read_cached_file(root: Path, path: PurePosixPath, expected: _FileState) -> bytes | None:
    current, issue = _file_state(root, path)
    if issue is not None or current != expected:
        return None
    try:
        target = _target_path(root, path)
        content = target.read_bytes()
        if _path_file_state(target) != expected:
            return None
        return content
    except (OSError, ValueError):
        return None


def _semantic_output_issues(
    target: ConfigSyncSource,
    contract: ArtifactOutputContract,
    files: tuple[RenderedFile, ...],
    fragments: tuple[SettingsFragment, ...],
) -> tuple[ValidationIssue, ...]:
    probe_files = list(files)
    present = {item.relative_path for item in probe_files}
    ownership = OWNERSHIP_MATRIX[target]
    for path in (ownership.instruction_path, ownership.instruction_companion):
        if path not in present:
            probe_files.append(RenderedFile(path, b"", "semantic-validation"))
    allowed_files = set(contract.file_paths)
    allowed_fragments = {(item.carrier_path, item.key_path) for item in contract.settings_fragments}
    if any(item.relative_path not in allowed_files for item in files) or any(
        (item.carrier_path, item.key_path) not in allowed_fragments for item in fragments
    ):
        return (ValidationIssue("semantic-contract", "Semantic output is outside its contract."),)
    return validate_rendered_workflow(target, probe_files, fragments)


def _semantic_record(
    source: ConfigSyncSource,
    item: UnresolvedItem,
    fingerprint: str,
    files: tuple[RenderedFile, ...],
    fragments: tuple[SettingsFragment, ...],
) -> _SemanticRecord:
    target = item.target_tool
    if target is None:
        raise ValueError("Semantic artifact target is missing.")
    return _SemanticRecord(
        fingerprint,
        ADAPTER_REVISION,
        source,
        target,
        item.identifier,
        item.source_path,
        {value.relative_path: _rendered_state(value) for value in files},
        {
            (value.carrier_path, value.key_path): _FragmentRecord(
                value.carrier_path,
                value.key_path,
                _digest(value.value_json),
            )
            for value in fragments
        },
    )


def _invalid_source_root(
    source: ConfigSyncSource,
    message: str = "Source workflow root must not be a symlink.",
    relative_path: PurePosixPath | None = None,
) -> _DesiredState:
    tools: dict[ConfigSyncSource, _DesiredTool] = {tool: _DesiredTool({}, {}) for tool in _TOOLS}
    problem = SyncProblem("source-root", message, source, relative_path)
    native_only: dict[ConfigSyncSource, dict[PurePosixPath, _FileState]] = {
        tool: {} for tool in _TOOLS
    }
    return _DesiredState(source, _digest(b""), {}, native_only, tools, (problem,))


def _validation_problems(
    issues: tuple[ValidationIssue, ...], tool: ConfigSyncSource
) -> list[SyncProblem]:
    return [SyncProblem(item.identifier, item.message, tool, item.relative_path) for item in issues]


def _publication_layout_drifts(
    config_dir: Path,
    desired: _DesiredState,
    manifest: _Manifest | None,
) -> tuple[DriftItem, ...]:
    drifts: list[DriftItem] = []
    for tool in _TOOLS:
        root = config_dir / tool
        if root.is_symlink() or not root.is_dir():
            drifts.append(
                DriftItem(
                    DriftClass.INVALID_VIEW,
                    "Workflow root must be a real directory.",
                    tool,
                )
            )
            continue
        wanted = desired.tools[tool]
        prior = manifest.managed[tool] if manifest is not None else _ToolManifest()
        paths = set(wanted.files) | {key[0] for key in wanted.fragments}
        paths.update(prior.files)
        paths.update(key[0] for key in prior.fragments)
        for relative in sorted(paths):
            current = root
            for part in relative.parts[:-1]:
                current /= part
                if not current.exists() and not current.is_symlink():
                    break
                if current.is_symlink() or not current.is_dir():
                    drifts.append(
                        DriftItem(
                            DriftClass.INVALID_VIEW,
                            "Managed parent must be a real directory.",
                            tool,
                            relative,
                        )
                    )
                    break
    return _deduplicate_drifts(drifts)


def _with_drifts(audit: ConfigSyncAudit, extra: tuple[DriftItem, ...]) -> ConfigSyncAudit:
    if not extra:
        return audit
    return ConfigSyncAudit(
        audit.configured_source,
        audit.manifest_source,
        _deduplicate_drifts([*audit.drifts, *extra]),
        audit.problems,
    )


def _analyze(
    project_root: Path,
    desired: _DesiredState,
    manifest: _Manifest | None,
) -> _Analysis:
    drifts: list[DriftItem] = []
    problems = list(desired.problems)
    observations = _Observations()
    manifest_source = manifest.active_source if manifest else None
    if desired.problems:
        for problem in desired.problems:
            kind = (
                DriftClass.SEMANTIC_REQUIRED
                if "adapt" in problem.message.lower() or "semantic" in problem.message.lower()
                else DriftClass.INVALID_VIEW
            )
            drifts.append(DriftItem(kind, problem.message, problem.tool, problem.relative_path))

    if manifest is None:
        drifts.append(DriftItem(DriftClass.SOURCE_ONLY, "Synchronization manifest is missing."))
    elif manifest.active_source != desired.source:
        drifts.append(
            DriftItem(
                DriftClass.SOURCE_SWITCH,
                f"Configured source changed from {manifest.active_source} to {desired.source}.",
                desired.source,
            )
        )
        _check_released_source(project_root, desired, manifest, drifts, observations)
    elif manifest.source_hash != desired.source_hash:
        drifts.append(DriftItem(DriftClass.SOURCE_ONLY, "Authoritative source changed."))

    for tool in _TOOLS:
        root = project_root / "config" / tool
        if root.is_symlink():
            drifts.append(
                DriftItem(DriftClass.INVALID_VIEW, "Workflow root must not be a symlink.", tool)
            )
            continue
        prior = manifest.managed[tool] if manifest else _ToolManifest()
        wanted = desired.tools[tool]
        for path, rendered in wanted.files.items():
            current, issue = _file_state(root, path)
            observations.files[(tool, path)] = current
            if issue is not None:
                drifts.append(DriftItem(DriftClass.INVALID_VIEW, issue, tool, path))
                continue
            expected = _rendered_state(rendered)
            recorded = prior.files.get(path)
            if recorded is not None:
                if current != recorded and current != expected:
                    drifts.append(
                        DriftItem(DriftClass.MANAGED_TARGET, "Managed file was edited.", tool, path)
                    )
                elif current != expected:
                    drifts.append(
                        DriftItem(
                            DriftClass.SOURCE_ONLY,
                            "Managed file needs rendering.",
                            tool,
                            path,
                        )
                    )
            elif current is None:
                drifts.append(
                    DriftItem(DriftClass.SOURCE_ONLY, "Managed file is missing.", tool, path)
                )
            elif current != expected and not _adoptable_file(
                project_root, tool, path, current, manifest
            ):
                drifts.append(
                    DriftItem(
                        DriftClass.UNMANAGED_COLLISION,
                        "Unmanaged file occupies a managed path.",
                        tool,
                        path,
                    )
                )
            elif manifest is None or recorded is None:
                drifts.append(
                    DriftItem(DriftClass.SOURCE_ONLY, "Existing output can be adopted.", tool, path)
                )

        authority_paths: set[PurePosixPath] = (
            set(desired.source_files) if tool == desired.source else set()
        )
        for path, recorded in prior.files.items():
            if path in wanted.files or path in authority_paths:
                continue
            current, issue = _file_state(root, path)
            observations.files[(tool, path)] = current
            if issue is not None:
                drifts.append(DriftItem(DriftClass.INVALID_VIEW, issue, tool, path))
            elif current is None:
                drifts.append(
                    DriftItem(
                        DriftClass.SOURCE_ONLY,
                        "Owned stale file is already absent.",
                        tool,
                        path,
                    )
                )
            elif current != recorded:
                drifts.append(
                    DriftItem(
                        DriftClass.MANAGED_TARGET,
                        "Managed stale file was edited.",
                        tool,
                        path,
                    )
                )
            else:
                drifts.append(
                    DriftItem(DriftClass.SOURCE_ONLY, "Owned stale file needs removal.", tool, path)
                )

        _analyze_fragments(project_root, tool, wanted, prior, desired, drifts, observations)

    _analyze_native_only(project_root, desired, manifest, drifts, problems)
    unique = _deduplicate_drifts(drifts)
    return _Analysis(
        ConfigSyncAudit(desired.source, manifest_source, unique, tuple(problems)), observations
    )


def _analyze_native_only(
    project_root: Path,
    desired: _DesiredState,
    manifest: _Manifest | None,
    drifts: list[DriftItem],
    problems: list[SyncProblem],
) -> None:
    for tool in _TOOLS:
        if tool == desired.source:
            continue
        root = project_root / "config" / tool
        prior = manifest.managed[tool].native_only if manifest is not None else {}
        discovered = _discover_native_only(root, tool)
        for path in sorted(set(prior) | set(discovered)):
            current, issue = _file_state(root, path)
            recorded = prior.get(path)
            if issue is None and recorded is not None and current == recorded:
                continue
            if issue is not None:
                message = issue
            elif recorded is None:
                message = "New dormant native-only file requires its owning source."
            elif current is None:
                message = "Dormant native-only file is missing."
            else:
                message = "Dormant native-only file was edited."
            drifts.append(DriftItem(DriftClass.MANAGED_TARGET, message, tool, path))
            problems.append(
                SyncProblem(f"dormant-native-only:{tool}:{path.as_posix()}", message, tool, path)
            )


def _discover_native_only(root: Path, tool: ConfigSyncSource) -> dict[PurePosixPath, _FileState]:
    if root.is_symlink() or not root.is_dir():
        return {}
    return _native_only_file_states(read_native_workflow(root, tool), tool)


def _native_only_file_states(
    read_result: AdapterReadResult, owner: ConfigSyncSource
) -> dict[PurePosixPath, _FileState]:
    return {
        artifact.source_path: _FileState(_digest(artifact.content), artifact.executable)
        for artifact in read_result.artifacts
        if artifact.native_only_for == owner
    }


def _analyze_fragments(
    project_root: Path,
    tool: ConfigSyncSource,
    wanted: _DesiredTool,
    prior: _ToolManifest,
    desired: _DesiredState,
    drifts: list[DriftItem],
    observations: _Observations,
) -> None:
    root = project_root / "config" / tool
    carriers = {key[0] for key in wanted.fragments} | {key[0] for key in prior.fragments}
    for carrier in carriers:
        carrier_state, issue = _read_carrier(root, carrier)
        observations.carriers[(tool, carrier)] = carrier_state
        if issue is not None:
            drifts.append(DriftItem(DriftClass.INVALID_VIEW, issue, tool, carrier))
            continue
        raw = carrier_state.content if carrier_state is not None else None
        parsed, issue = _parse_carrier(carrier, raw)
        if issue is not None:
            drifts.append(DriftItem(DriftClass.INVALID_VIEW, issue, tool, carrier))
            continue
        for key, fragment in wanted.fragments.items():
            if key[0] != carrier:
                continue
            found, value, nested_issue = _nested_get(parsed, key[1])
            if nested_issue is not None:
                drifts.append(DriftItem(DriftClass.INVALID_VIEW, nested_issue, tool, carrier))
                continue
            current = _digest_json(value) if found else None
            expected = _digest(fragment.value_json)
            recorded = prior.fragments.get(key)
            if recorded is not None:
                if current != recorded.value_hash and current != expected:
                    drifts.append(
                        DriftItem(
                            DriftClass.MANAGED_TARGET,
                            "Managed settings fragment was edited.",
                            tool,
                            carrier,
                        )
                    )
                elif current != expected:
                    drifts.append(
                        DriftItem(
                            DriftClass.SOURCE_ONLY,
                            "Managed settings fragment needs rendering.",
                            tool,
                            carrier,
                        )
                    )
            elif current is None or current == expected:
                drifts.append(
                    DriftItem(
                        DriftClass.SOURCE_ONLY,
                        "Settings fragment needs ownership or rendering.",
                        tool,
                        carrier,
                    )
                )
            else:
                drifts.append(
                    DriftItem(
                        DriftClass.UNMANAGED_COLLISION,
                        "Unmanaged settings value occupies an owned key.",
                        tool,
                        carrier,
                    )
                )
        if tool == desired.source:
            continue
        for key, recorded in prior.fragments.items():
            if key[0] != carrier or key in wanted.fragments:
                continue
            found, value, nested_issue = _nested_get(parsed, key[1])
            if nested_issue is not None:
                drifts.append(DriftItem(DriftClass.INVALID_VIEW, nested_issue, tool, carrier))
            elif found and _digest_json(value) != recorded.value_hash:
                drifts.append(
                    DriftItem(
                        DriftClass.MANAGED_TARGET,
                        "Managed stale settings fragment was edited.",
                        tool,
                        carrier,
                    )
                )
            elif found:
                drifts.append(
                    DriftItem(
                        DriftClass.SOURCE_ONLY,
                        "Owned stale settings fragment needs removal.",
                        tool,
                        carrier,
                    )
                )


def _check_released_source(
    project_root: Path,
    desired: _DesiredState,
    manifest: _Manifest,
    drifts: list[DriftItem],
    observations: _Observations,
) -> None:
    source = desired.source
    root = project_root / "config" / source
    prior = manifest.managed[source]
    for path, recorded in prior.files.items():
        current, issue = _file_state(root, path)
        observations.files[(source, path)] = current
        if issue is not None or current != recorded:
            drifts.append(
                DriftItem(
                    DriftClass.MANAGED_TARGET,
                    issue or "Candidate source contains managed drift.",
                    source,
                    path,
                )
            )
    allowed_source_paths = set(prior.files) | {
        path for path in desired.native_only[source] if native_only_path_is_owned(source, path)
    }
    for path in sorted(set(desired.source_files) - allowed_source_paths):
        drifts.append(
            DriftItem(
                DriftClass.MANAGED_TARGET,
                "Candidate source contains a new shared artifact.",
                source,
                path,
            )
        )
    for record in prior.fragments.values():
        carrier_state, issue = _read_carrier(root, record.carrier_path)
        observations.carriers[(source, record.carrier_path)] = carrier_state
        if issue is not None:
            drifts.append(DriftItem(DriftClass.MANAGED_TARGET, issue, source, record.carrier_path))
            continue
        raw = carrier_state.content if carrier_state is not None else None
        parsed, issue = _parse_carrier(record.carrier_path, raw)
        found, value, nested_issue = _nested_get(parsed, record.key_path)
        if issue is not None or nested_issue is not None or not found:
            drifts.append(
                DriftItem(
                    DriftClass.MANAGED_TARGET,
                    issue or nested_issue or "Candidate source is missing a managed fragment.",
                    source,
                    record.carrier_path,
                )
            )
        elif _digest_json(value) != record.value_hash:
            drifts.append(
                DriftItem(
                    DriftClass.MANAGED_TARGET,
                    "Candidate source contains managed settings drift.",
                    source,
                    record.carrier_path,
                )
            )


def _blocks_sync(audit: ConfigSyncAudit, manifest: _Manifest | None) -> bool:
    blocking = {
        DriftClass.UNMANAGED_COLLISION,
        DriftClass.INVALID_VIEW,
        DriftClass.SEMANTIC_REQUIRED,
        DriftClass.MANIFEST_INVALID,
    }
    if any(item.kind in blocking for item in audit.drifts):
        return True
    if any(
        item.kind is DriftClass.MANAGED_TARGET
        and item.message == "Managed stale settings fragment was edited."
        for item in audit.drifts
    ):
        return True
    if any(item.identifier.startswith("dormant-native-only:") for item in audit.problems):
        return True
    if manifest is not None and manifest.active_source != audit.configured_source:
        return any(
            item.kind is DriftClass.MANAGED_TARGET and item.tool == audit.configured_source
            for item in audit.drifts
        )
    return False


def _publish(
    stage: _Stage,
    desired: _DesiredState,
    manifest: _Manifest | None,
    observations: _Observations,
    config_fd: int,
    staged_files: dict[tuple[ConfigSyncSource, PurePosixPath], _StagedFile],
) -> tuple[list[PurePosixPath], list[PurePosixPath]]:
    changed: list[PurePosixPath] = []
    removed: list[PurePosixPath] = []
    for tool in _TOOLS:
        wanted = desired.tools[tool]
        prior = manifest.managed[tool] if manifest else _ToolManifest()
        for path, rendered in wanted.files.items():
            expected_observation = observations.files.get((tool, path))
            if _rendered_state(rendered) == expected_observation:
                continue
            target_relative = PurePosixPath(tool) / path
            _replace_file_at(
                config_fd,
                stage,
                staged_files[(tool, path)],
                target_relative,
                expected_observation,
            )
            changed.append(target_relative)
        authority_paths: set[PurePosixPath] = (
            set(desired.source_files) if tool == desired.source else set()
        )
        for path in prior.files:
            if path in wanted.files or path in authority_paths:
                continue
            target_relative = PurePosixPath(tool) / path
            if _unlink_file_at(
                config_fd,
                target_relative,
                observations.files.get((tool, path)),
            ):
                removed.append(target_relative)
        carrier_paths = {key[0] for key in wanted.fragments} | {
            key[0] for key in prior.fragments if tool != desired.source
        }
        for carrier in carrier_paths:
            initial = observations.carriers.get((tool, carrier))
            carrier_relative = PurePosixPath(tool) / carrier
            current = _carrier_state_at(config_fd, carrier_relative)
            if current != initial:
                confirmation = _carrier_state_at(config_fd, carrier_relative)
                if confirmation != current:
                    raise _RaceError("Settings carrier changed repeatedly during synchronization.")
                current = confirmation
                current_raw = current.content if current is not None else None
                if tool != desired.source and _stale_fragments_changed(
                    carrier, current_raw, wanted, prior
                ):
                    raise _RaceError("Owned stale settings changed during synchronization.")
                if _new_fragment_collision(carrier, current_raw, wanted, prior):
                    raise _RaceError(
                        "Unmanaged settings collision appeared during synchronization."
                    )
            current_raw = current.content if current is not None else None
            output = _merge_carrier(
                carrier,
                current_raw,
                wanted,
                prior,
                remove_stale=tool != desired.source,
            )
            if output == current_raw:
                continue
            staged = _stage_bytes(
                config_fd,
                stage,
                PurePosixPath("carriers") / tool / carrier,
                output,
                current.executable if current is not None else False,
            )
            _replace_carrier_at(config_fd, stage, staged, carrier_relative, current)
            changed.append(carrier_relative)
    return changed, removed


def _merge_carrier(
    carrier: PurePosixPath,
    raw: bytes | None,
    wanted: _DesiredTool,
    prior: _ToolManifest,
    *,
    remove_stale: bool,
) -> bytes:
    data, issue = _parse_carrier(carrier, raw)
    if issue is not None:
        raise ValueError(issue)
    for (path, keys), fragment in wanted.fragments.items():
        if path == carrier:
            _nested_set(data, keys, json.loads(fragment.value_json))
    if remove_stale:
        for path, keys in prior.fragments:
            if path == carrier and (path, keys) not in wanted.fragments:
                _nested_remove(data, keys)
    if carrier.suffix == ".json":
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    return tomli_w.dumps(data).encode()


def _new_fragment_collision(
    carrier: PurePosixPath,
    raw: bytes | None,
    wanted: _DesiredTool,
    prior: _ToolManifest,
) -> bool:
    data, issue = _parse_carrier(carrier, raw)
    if issue is not None:
        return True
    for key, fragment in wanted.fragments.items():
        if key[0] != carrier or key in prior.fragments:
            continue
        found, value, nested_issue = _nested_get(data, key[1])
        if nested_issue is not None:
            return True
        if found and _digest_json(value) != _digest(fragment.value_json):
            return True
    return False


def _stale_fragments_changed(
    carrier: PurePosixPath,
    raw: bytes | None,
    wanted: _DesiredTool,
    prior: _ToolManifest,
) -> bool:
    data, issue = _parse_carrier(carrier, raw)
    if issue is not None:
        return True
    for key, record in prior.fragments.items():
        if key[0] != carrier or key in wanted.fragments:
            continue
        found, value, nested_issue = _nested_get(data, key[1])
        if nested_issue is not None or not found or _digest_json(value) != record.value_hash:
            return True
    return False


def _stage_files(
    config_fd: int, stage: _Stage, desired: _DesiredState
) -> dict[tuple[ConfigSyncSource, PurePosixPath], _StagedFile]:
    staged_files: dict[tuple[ConfigSyncSource, PurePosixPath], _StagedFile] = {}
    for tool, wanted in desired.tools.items():
        for path, rendered in wanted.files.items():
            staged_files[(tool, path)] = _stage_bytes(
                config_fd,
                stage,
                PurePosixPath("files") / tool / path,
                rendered.content,
                rendered.executable,
            )
    return staged_files


def _files_unchanged(project_root: Path, observations: _Observations) -> bool:
    for (tool, path), expected in observations.files.items():
        current, issue = _file_state(project_root / "config" / tool, path)
        if issue is not None or current != expected:
            return False
    return True


def _adoptable_file(
    project_root: Path,
    tool: ConfigSyncSource,
    path: PurePosixPath,
    current: _FileState,
    manifest: _Manifest | None,
) -> bool:
    pristine = pristine_workflow_seed_digest(project_root, Path(tool) / Path(*path.parts))
    if pristine == current.content_hash:
        return True
    return bool(
        manifest is not None
        and manifest.active_source == tool
        and manifest.source_files.get(path) == current
    )


def _manifest_from_desired(desired: _DesiredState, prior_manifest: _Manifest | None) -> _Manifest:
    managed: dict[ConfigSyncSource, _ToolManifest] = {}
    for tool, wanted in desired.tools.items():
        files = {path: _rendered_state(item) for path, item in wanted.files.items()}
        native_only = (
            desired.native_only[tool]
            if tool == desired.source
            else (prior_manifest.managed[tool].native_only if prior_manifest is not None else {})
        )
        fragments = {
            key: _FragmentRecord(item.carrier_path, item.key_path, _digest(item.value_json))
            for key, item in wanted.fragments.items()
        }
        managed[tool] = _ToolManifest(files, native_only, fragments)
    return _Manifest(
        desired.source,
        desired.source_hash,
        desired.source_files,
        managed,
        dict(desired.semantic),
    )


def _serialize_manifest(manifest: _Manifest) -> bytes:
    data = {
        "schema_version": _SCHEMA_VERSION,
        "adapter_revision": ADAPTER_REVISION,
        "active_source": manifest.active_source,
        "source_hash": manifest.source_hash,
        "source_files": {
            path.as_posix(): _serialize_file_state(value)
            for path, value in sorted(manifest.source_files.items())
        },
        "managed": {
            tool: {
                "files": {
                    path.as_posix(): _serialize_file_state(value)
                    for path, value in sorted(manifest.managed[tool].files.items())
                },
                "native_only": {
                    path.as_posix(): _serialize_file_state(value)
                    for path, value in sorted(manifest.managed[tool].native_only.items())
                },
                "fragments": [
                    {
                        "carrier_path": record.carrier_path.as_posix(),
                        "key_path": list(record.key_path),
                        "value_hash": record.value_hash,
                    }
                    for record in sorted(
                        manifest.managed[tool].fragments.values(),
                        key=lambda item: (item.carrier_path, item.key_path),
                    )
                ],
            }
            for tool in _TOOLS
        },
        "semantic": [
            {
                "fingerprint": record.fingerprint,
                "adapter_revision": record.adapter_revision,
                "source_tool": record.source_tool,
                "target_tool": record.target_tool,
                "artifact_id": record.artifact_id,
                "source_path": record.source_path.as_posix(),
                "files": [
                    {
                        "path": path.as_posix(),
                        **_serialize_file_state(state),
                    }
                    for path, state in sorted(record.files.items())
                ],
                "fragments": [
                    {
                        "carrier_path": fragment.carrier_path.as_posix(),
                        "key_path": list(fragment.key_path),
                        "value_hash": fragment.value_hash,
                    }
                    for fragment in sorted(
                        record.fragments.values(),
                        key=lambda value: (value.carrier_path, value.key_path),
                    )
                ],
            }
            for record in sorted(
                manifest.semantic.values(),
                key=lambda value: (
                    value.source_tool,
                    value.target_tool,
                    value.artifact_id,
                    value.source_path,
                ),
            )
        ],
    }
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


def _load_manifest(path: Path) -> tuple[_Manifest | None, str | None]:
    if not path.exists() and not path.is_symlink():
        return None, None
    if path.is_symlink() or not path.is_file():
        return None, "Synchronization manifest must be a regular file."
    try:
        raw: object = json.loads(path.read_bytes(), object_pairs_hook=_unique_manifest_object)
        if not isinstance(raw, dict):
            raise ValueError
        data = cast(dict[str, object], raw)
        if set(data) != {
            "schema_version",
            "adapter_revision",
            "active_source",
            "source_hash",
            "source_files",
            "managed",
            "semantic",
        }:
            raise ValueError
        if (
            type(data.get("schema_version")) is not int
            or data.get("schema_version") != _SCHEMA_VERSION
        ):
            return None, "Unsupported synchronization manifest schema."
        if (
            type(data.get("adapter_revision")) is not int
            or data.get("adapter_revision") != ADAPTER_REVISION
        ):
            return None, "Unsupported adapter revision in synchronization manifest."
        source_raw = data.get("active_source")
        if source_raw not in _TOOLS:
            raise ValueError
        source = source_raw
        source_hash = _require_hash(data.get("source_hash"))
        source_files = _parse_file_hashes(data.get("source_files"), source)
        managed_raw = data.get("managed")
        if not isinstance(managed_raw, dict):
            raise ValueError
        managed_data = cast(dict[str, object], managed_raw)
        if set(managed_data) != set(_TOOLS):
            raise ValueError
        managed: dict[ConfigSyncSource, _ToolManifest] = {}
        for tool in _TOOLS:
            item = managed_data[tool]
            if not isinstance(item, dict):
                raise ValueError
            item_data = cast(dict[str, object], item)
            if set(item_data) != {"files", "native_only", "fragments"}:
                raise ValueError
            files = _parse_file_hashes(item_data.get("files"), tool)
            native_only = _parse_native_only_file_hashes(item_data.get("native_only"), tool)
            if set(files) & set(native_only):
                raise ValueError
            fragments = _parse_fragments(item_data.get("fragments"), tool)
            managed[tool] = _ToolManifest(files, native_only, fragments)
        semantic = _parse_semantic_records(data.get("semantic"), source, managed)
        return _Manifest(source, source_hash, source_files, managed, semantic), None
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
    ):
        return None, "Synchronization manifest is malformed or contains unsafe ownership."


def _parse_file_hashes(
    raw: object,
    tool: ConfigSyncSource,
) -> dict[PurePosixPath, _FileState]:
    if not isinstance(raw, dict):
        raise ValueError
    values: dict[PurePosixPath, _FileState] = {}
    for path_raw, state_raw in cast(dict[str, object], raw).items():
        path = PurePosixPath(path_raw)
        if (
            path.as_posix() != path_raw
            or not is_safe_relative_path(path)
            or not path_is_owned(tool, path)
            or path in values
        ):
            raise ValueError
        if not isinstance(state_raw, dict):
            raise ValueError
        state = cast(dict[str, object], state_raw)
        if set(state) != {"hash", "executable"}:
            raise ValueError
        executable = state.get("executable")
        if type(executable) is not bool:
            raise ValueError
        values[path] = _FileState(_require_hash(state.get("hash")), executable)
    return values


def _serialize_file_state(state: _FileState) -> dict[str, object]:
    return {"hash": state.content_hash, "executable": state.executable}


def _parse_native_only_file_hashes(
    raw: object, tool: ConfigSyncSource
) -> dict[PurePosixPath, _FileState]:
    values = _parse_file_hashes(raw, tool)
    if any(not native_only_path_is_owned(tool, path) for path in values):
        raise ValueError
    return values


def _parse_fragments(
    raw: object, tool: ConfigSyncSource
) -> dict[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord]:
    if not isinstance(raw, list):
        raise ValueError
    values: dict[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord] = {}
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            raise ValueError
        data = cast(dict[str, object], item)
        if set(data) != {"carrier_path", "key_path", "value_hash"}:
            raise ValueError
        carrier_raw = data.get("carrier_path")
        keys_raw = data.get("key_path")
        if not isinstance(carrier_raw, str) or not isinstance(keys_raw, list):
            raise ValueError
        key_values = cast(list[object], keys_raw)
        if not all(isinstance(key, str) and key for key in key_values):
            raise ValueError
        carrier = PurePosixPath(carrier_raw)
        keys = tuple(cast(str, key) for key in key_values)
        if (
            carrier.as_posix() != carrier_raw
            or not is_safe_relative_path(carrier)
            or not fragment_is_owned(tool, carrier, keys)
        ):
            raise ValueError
        record = _FragmentRecord(carrier, keys, _require_hash(data.get("value_hash")))
        identity = (carrier, keys)
        if identity in values:
            raise ValueError
        values[identity] = record
    return values


def _parse_semantic_records(
    raw: object,
    active_source: ConfigSyncSource,
    managed: dict[ConfigSyncSource, _ToolManifest],
) -> dict[_SemanticKey, _SemanticRecord]:
    if not isinstance(raw, list):
        raise ValueError
    records: dict[_SemanticKey, _SemanticRecord] = {}
    claimed_files: set[tuple[ConfigSyncSource, PurePosixPath]] = set()
    claimed_fragments: set[tuple[ConfigSyncSource, PurePosixPath, tuple[str, ...]]] = set()
    required = {
        "fingerprint",
        "adapter_revision",
        "source_tool",
        "target_tool",
        "artifact_id",
        "source_path",
        "files",
        "fragments",
    }
    for raw_record in cast(list[object], raw):
        if not isinstance(raw_record, dict):
            raise ValueError
        data = cast(dict[str, object], raw_record)
        if set(data) != required:
            raise ValueError
        source_raw = data.get("source_tool")
        target_raw = data.get("target_tool")
        artifact_id = data.get("artifact_id")
        source_path_raw = data.get("source_path")
        if (
            source_raw != active_source
            or source_raw not in _TOOLS
            or target_raw not in _TOOLS
            or target_raw == source_raw
            or not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(source_path_raw, str)
            or type(data.get("adapter_revision")) is not int
            or data.get("adapter_revision") != ADAPTER_REVISION
        ):
            raise ValueError
        source_tool = source_raw
        target_tool = target_raw
        source_path = PurePosixPath(source_path_raw)
        if source_path.as_posix() != source_path_raw or not is_safe_relative_path(source_path):
            raise ValueError
        unresolved = UnresolvedItem(
            artifact_id,
            "",
            source_path,
            b"",
            target_tool=target_tool,
        )
        contract = allowed_outputs_for_unresolved(unresolved)
        allowed_files = set(contract.file_paths)
        allowed_fragments = {
            (value.carrier_path, value.key_path) for value in contract.settings_fragments
        }
        files = _parse_semantic_files(data.get("files"), target_tool, allowed_files, claimed_files)
        fragments = _parse_semantic_fragments(
            data.get("fragments"),
            target_tool,
            allowed_fragments,
            claimed_fragments,
        )
        if not files and not fragments:
            raise ValueError
        fingerprint = _require_hash(data.get("fingerprint"))
        for path, state in files.items():
            if managed[target_tool].files.get(path) != state:
                raise ValueError
        for key, fragment in fragments.items():
            managed_fragment = managed[target_tool].fragments.get(key)
            if managed_fragment is None or managed_fragment.value_hash != fragment.value_hash:
                raise ValueError
        key = (source_tool, target_tool, artifact_id, source_path)
        if key in records:
            raise ValueError
        records[key] = _SemanticRecord(
            fingerprint,
            ADAPTER_REVISION,
            source_tool,
            target_tool,
            artifact_id,
            source_path,
            files,
            fragments,
        )
    return records


def _parse_semantic_files(
    raw: object,
    target: ConfigSyncSource,
    allowed: set[PurePosixPath],
    claimed: set[tuple[ConfigSyncSource, PurePosixPath]],
) -> dict[PurePosixPath, _FileState]:
    if not isinstance(raw, list):
        raise ValueError
    values: dict[PurePosixPath, _FileState] = {}
    for raw_file in cast(list[object], raw):
        if not isinstance(raw_file, dict):
            raise ValueError
        data = cast(dict[str, object], raw_file)
        if set(data) != {"path", "hash", "executable"}:
            raise ValueError
        path_raw = data.get("path")
        executable = data.get("executable")
        if not isinstance(path_raw, str) or type(executable) is not bool:
            raise ValueError
        path = PurePosixPath(path_raw)
        identity = (target, path)
        if (
            path.as_posix() != path_raw
            or not is_safe_relative_path(path)
            or not path_is_owned(target, path)
            or path not in allowed
            or path in values
            or identity in claimed
        ):
            raise ValueError
        values[path] = _FileState(_require_hash(data.get("hash")), executable)
        claimed.add(identity)
    return values


def _parse_semantic_fragments(
    raw: object,
    target: ConfigSyncSource,
    allowed: set[tuple[PurePosixPath, tuple[str, ...]]],
    claimed: set[tuple[ConfigSyncSource, PurePosixPath, tuple[str, ...]]],
) -> dict[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord]:
    if not isinstance(raw, list):
        raise ValueError
    values: dict[tuple[PurePosixPath, tuple[str, ...]], _FragmentRecord] = {}
    for raw_fragment in cast(list[object], raw):
        if not isinstance(raw_fragment, dict):
            raise ValueError
        data = cast(dict[str, object], raw_fragment)
        if set(data) != {"carrier_path", "key_path", "value_hash"}:
            raise ValueError
        carrier_raw = data.get("carrier_path")
        keys_raw = data.get("key_path")
        if not isinstance(carrier_raw, str) or not isinstance(keys_raw, list):
            raise ValueError
        key_values = cast(list[object], keys_raw)
        if not key_values or not all(isinstance(value, str) and value for value in key_values):
            raise ValueError
        carrier = PurePosixPath(carrier_raw)
        keys = tuple(cast(str, value) for value in key_values)
        key = (carrier, keys)
        identity = (target, carrier, keys)
        if (
            carrier.as_posix() != carrier_raw
            or not is_safe_relative_path(carrier)
            or not fragment_is_owned(target, carrier, keys)
            or key not in allowed
            or key in values
            or identity in claimed
        ):
            raise ValueError
        record = _FragmentRecord(carrier, keys, _require_hash(data.get("value_hash")))
        values[key] = record
        claimed.add(identity)
    return values


def _require_hash(raw: object) -> str:
    if not isinstance(raw, str) or len(raw) != _HASH_LENGTH:
        raise ValueError
    int(raw, 16)
    return raw


def _unique_manifest_object(
    values: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _source_entries(root: Path, tool: ConfigSyncSource) -> tuple[Path, ...]:
    ownership = OWNERSHIP_MATRIX[tool]
    fixed = {ownership.instruction_path}
    if tool == "codex":
        fixed.add(PurePosixPath("config.toml"))
    for hook in ownership.hooks:
        fixed.add(hook.script_path)
        if hook.carrier_path is not None:
            fixed.add(hook.carrier_path)
    prefixes = [
        PurePosixPath("agents"),
        PurePosixPath("skills"),
        PurePosixPath("context"),
        PurePosixPath("scripts"),
    ]
    if tool != "codex":
        prefixes.append(PurePosixPath("commands"))
    entries: set[Path] = set()
    for relative in fixed:
        candidate = root.joinpath(*relative.parts)
        if candidate.exists() or candidate.is_symlink():
            entries.add(candidate)
    for prefix in prefixes:
        directory = root.joinpath(*prefix.parts)
        if directory.exists() or directory.is_symlink():
            entries.add(directory)
        if directory.is_dir() and not directory.is_symlink():
            entries.update(directory.rglob("*"))
    return tuple(sorted(entries, key=lambda item: item.relative_to(root).as_posix()))


def _source_link_problem(
    root: Path, tool: ConfigSyncSource
) -> tuple[str, PurePosixPath | None] | None:
    if root.is_symlink():
        return "Source workflow root must not be a symlink.", None
    root_resolved = root.resolve()
    for item in _source_entries(root, tool):
        if not item.is_symlink():
            continue
        relative = PurePosixPath(item.relative_to(root).as_posix())
        try:
            resolved = item.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            return "Source symlink is dangling or cannot be resolved safely.", relative
        if not resolved.is_relative_to(root_resolved):
            return "Source symlink resolves outside its workflow root.", relative
        if not resolved.is_file():
            return "Source symlink must resolve to a regular file.", relative
    return None


def _source_fingerprint(root: Path, tool: ConfigSyncSource) -> str:
    return _source_fingerprints(root, tool)[0]


def _source_matches_snapshot(
    config_path: Path | None,
    root: Path,
    source: ConfigSyncSource,
    expected_fingerprint: str,
    expected_material: str,
) -> bool:
    try:
        if (
            load_config(config_path).config_sync.source != source
            or _source_link_problem(root, source) is not None
        ):
            return False
        fingerprint, material = _source_fingerprints(root, source)
        return fingerprint == expected_fingerprint and material == expected_material
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def _source_fingerprints(root: Path, tool: ConfigSyncSource) -> tuple[str, str]:
    identity_digest = hashlib.sha256()
    material_digest = hashlib.sha256()
    root_resolved = root.resolve()
    for item in _source_entries(root, tool):
        relative = item.relative_to(root).as_posix().encode()
        for digest in (identity_digest, material_digest):
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
        mode = item.lstat().st_mode
        if stat.S_ISLNK(mode):
            resolved = _safe_source_link_target(root_resolved, item)
            resolved_mode = resolved.stat().st_mode
            payload = resolved.read_bytes()
            identity_kind = b"L" if resolved_mode & stat.S_IXUSR else b"l"
            material_kind = b"x" if resolved_mode & stat.S_IXUSR else b"f"
            link = os.fsencode(os.readlink(item))
            identity_digest.update(len(link).to_bytes(8, "big"))
            identity_digest.update(link)
        elif stat.S_ISREG(mode):
            payload = item.read_bytes()
            identity_kind = material_kind = b"x" if mode & stat.S_IXUSR else b"f"
        elif stat.S_ISDIR(mode):
            payload = b""
            identity_kind = material_kind = b"d"
        else:
            payload = b""
            identity_kind = material_kind = b"o"
        for digest, kind in (
            (identity_digest, identity_kind),
            (material_digest, material_kind),
        ):
            digest.update(kind)
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return identity_digest.hexdigest(), material_digest.hexdigest()


def _create_stage(config_fd: int) -> _Stage:
    for _attempt in range(32):
        name = f".djinn-config-sync-stage-{secrets.token_hex(12)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=config_fd)
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
                dir_fd=config_fd,
            )
            opened = os.fstat(descriptor)
            identity = (opened.st_dev, opened.st_ino)
            current = os.stat(name, dir_fd=config_fd, follow_symlinks=False)
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
                _remove_empty_stage_directory(config_fd, name, identity)
            if descriptor is None:
                raise _StageCreateError from error
            raise _RaceError from error
    raise _StageCreateError


def _stage_attached(config_fd: int, stage: _Stage) -> None:
    try:
        current = os.stat(stage.name, dir_fd=config_fd, follow_symlinks=False)
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
                    raise
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
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or stat.S_ISLNK(current.st_mode)
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                    or stage.entries.get(relative) != entry
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


def _ensure_stage_directory(stage: _Stage, path: PurePosixPath) -> None:
    with _stage_parent_fd(stage, path / ".directory", create=True):
        pass


def _stage_bytes(
    config_fd: int,
    stage: _Stage,
    relative: PurePosixPath,
    content: bytes,
    executable: bool,
) -> _StagedFile:
    _stage_attached(config_fd, stage)
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
                _FileState(_digest(content), executable),
            )
        except OSError as error:
            raise _RaceError from error
        finally:
            os.close(descriptor)


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
        actual = _FileState(_digest(b"".join(chunks)), bool(file_stat.st_mode & stat.S_IXUSR))
        path_stat = os.stat(
            staged.relative_path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            actual != staged.state
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


def _cleanup_stage(config_fd: int, stage: _Stage) -> None:
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
            except (FileNotFoundError, OSError, _RaceError):
                continue
    finally:
        os.close(stage.descriptor)
    _remove_empty_stage_directory(
        config_fd,
        stage.name,
        (stage.device, stage.inode),
    )


def _remove_empty_stage_directory(
    config_fd: int,
    name: str,
    identity: tuple[int, int],
) -> None:
    try:
        current = os.stat(name, dir_fd=config_fd, follow_symlinks=False)
        if (
            stat.S_ISDIR(current.st_mode)
            and not stat.S_ISLNK(current.st_mode)
            and (current.st_dev, current.st_ino) == identity
        ):
            os.rmdir(name, dir_fd=config_fd)
    except OSError:
        pass


def _snapshot_source_at(
    config_fd: int,
    stage: _Stage,
    source: Path,
    tool: ConfigSyncSource,
) -> None:
    prefix = PurePosixPath("source")
    _stage_attached(config_fd, stage)
    _ensure_stage_directory(stage, prefix)
    source_resolved = source.resolve()
    for item in _source_entries(source, tool):
        relative = PurePosixPath(item.relative_to(source).as_posix())
        destination = prefix / relative
        mode = item.lstat().st_mode
        if stat.S_ISDIR(mode):
            _ensure_stage_directory(stage, destination)
        elif stat.S_ISLNK(mode):
            resolved = _safe_source_link_target(source_resolved, item)
            resolved_mode = resolved.stat().st_mode
            _stage_bytes(
                config_fd,
                stage,
                destination,
                resolved.read_bytes(),
                bool(resolved_mode & stat.S_IXUSR),
            )
        elif stat.S_ISREG(mode):
            _stage_bytes(
                config_fd,
                stage,
                destination,
                item.read_bytes(),
                bool(mode & stat.S_IXUSR),
            )


def _snapshot_source(source: Path, target: Path, tool: ConfigSyncSource) -> None:
    target.mkdir(parents=True)
    source_resolved = source.resolve()
    for item in _source_entries(source, tool):
        relative = item.relative_to(source)
        destination = target / relative
        mode = item.lstat().st_mode
        if stat.S_ISDIR(mode):
            destination.mkdir(parents=True, exist_ok=True)
        elif stat.S_ISLNK(mode):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_safe_source_link_target(source_resolved, item), destination)
        elif stat.S_ISREG(mode):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination, follow_symlinks=False)


def _source_file_hashes(
    root: Path, read_result: AdapterReadResult
) -> dict[PurePosixPath, _FileState]:
    values: dict[PurePosixPath, _FileState] = {}
    for artifact in read_result.artifacts:
        path = root.joinpath(*artifact.source_path.parts)
        try:
            resolved = path.resolve(strict=True)
            if resolved.is_relative_to(root.resolve()) and resolved.is_file():
                values[artifact.source_path] = _path_file_state(resolved)
        except (FileNotFoundError, OSError, RuntimeError):
            continue
    return values


def _safe_source_link_target(root_resolved: Path, item: Path) -> Path:
    resolved = item.resolve(strict=True)
    if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
        raise ValueError("Source symlink is not a safe in-root regular file.")
    return resolved


def _file_state(root: Path, relative: PurePosixPath) -> tuple[_FileState | None, str | None]:
    try:
        target = _target_path(root, relative)
    except ValueError as error:
        return None, str(error)
    if not target.exists() and not target.is_symlink():
        return None, None
    try:
        resolved = target.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return None, "Managed path cannot be resolved safely."
    if not resolved.is_relative_to(root.resolve()):
        return None, "Managed path resolves outside its workflow root."
    if target.is_symlink():
        return None, "Managed target path must not be a symlink."
    if not resolved.is_file():
        return None, "Managed path is not a regular file."
    try:
        return _path_file_state(resolved), None
    except OSError:
        return None, "Managed file cannot be read."


def _path_file_state(path: Path) -> _FileState:
    mode = path.stat().st_mode
    return _FileState(_digest(path.read_bytes()), bool(mode & stat.S_IXUSR))


def _rendered_state(item: RenderedFile) -> _FileState:
    return _FileState(_digest(item.content), item.executable)


def _read_carrier(root: Path, relative: PurePosixPath) -> tuple[_CarrierState | None, str | None]:
    try:
        target = _target_path(root, relative)
    except ValueError as error:
        return None, str(error)
    if not target.exists() and not target.is_symlink():
        return None, None
    try:
        if target.is_symlink():
            return None, "Settings carrier path must not be a symlink."
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            return None, "Settings carrier is not a safe regular file."
        mode = resolved.stat().st_mode
        return _CarrierState(resolved.read_bytes(), bool(mode & stat.S_IXUSR)), None
    except (FileNotFoundError, OSError, RuntimeError):
        return None, "Settings carrier cannot be read safely."


@contextmanager
def _parent_fd(
    config_fd: int, relative: PurePosixPath, *, create: bool
) -> Iterator[tuple[int, str]]:
    if not is_safe_relative_path(relative) or not relative.parts:
        raise ValueError("Managed path is absolute or contains traversal.")
    descriptor = os.dup(config_fd)
    try:
        for part in relative.parts[:-1]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=descriptor)
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor, relative.parts[-1]
    finally:
        os.close(descriptor)


def _require_config_root_attached(path: Path, config_fd: int) -> None:
    if not directory_is_attached(path, config_fd):
        raise _RaceError("Configuration root detached during synchronization.")


def _parent_is_attached(config_fd: int, target: PurePosixPath, parent_fd: int) -> bool:
    try:
        with _parent_fd(config_fd, target, create=False) as (current_fd, _name):
            expected = os.fstat(parent_fd)
            current = os.fstat(current_fd)
            return (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
    except (FileNotFoundError, OSError, ValueError):
        return False


def _read_regular_at(parent_fd: int, name: str) -> tuple[bytes, bool] | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("Managed path cannot be opened without following links.") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Managed path is not a regular file.")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read()
        return content, bool(info.st_mode & stat.S_IXUSR)
    finally:
        os.close(descriptor)


def _carrier_state_at(config_fd: int, relative: PurePosixPath) -> _CarrierState | None:
    try:
        with _parent_fd(config_fd, relative, create=False) as (parent_fd, name):
            value = _read_regular_at(parent_fd, name)
    except FileNotFoundError:
        return None
    except ValueError as error:
        raise _RaceError("Settings carrier became unsafe during synchronization.") from error
    if value is None:
        return None
    content, executable = value
    return _CarrierState(content, executable)


def _replace_file_at(
    config_fd: int,
    stage: _Stage,
    staged: _StagedFile,
    target: PurePosixPath,
    expected: _FileState | None,
) -> None:
    _replace_at(
        config_fd,
        stage,
        staged,
        target,
        expected,
        lambda value: _FileState(_digest(value[0]), value[1]),
        "Managed file",
    )


def _replace_carrier_at(
    config_fd: int,
    stage: _Stage,
    staged: _StagedFile,
    target: PurePosixPath,
    expected: _CarrierState | None,
) -> None:
    _replace_at(
        config_fd,
        stage,
        staged,
        target,
        expected,
        lambda value: _CarrierState(*value),
        "Settings carrier",
    )


def _replace_at(
    config_fd: int,
    stage: _Stage,
    staged: _StagedFile,
    target: PurePosixPath,
    expected: object | None,
    state_from_value: Callable[[tuple[bytes, bool]], object],
    label: str,
) -> None:
    _stage_attached(config_fd, stage)
    with _parent_fd(config_fd, target, create=True) as (target_fd, target_name):
        try:
            current = _read_regular_at(target_fd, target_name)
        except ValueError as error:
            raise _RaceError(f"{label} became unsafe before replacement.") from error
        current_state = state_from_value(current) if current is not None else None
        if current_state != expected:
            raise _RaceError(f"{label} changed immediately before replacement.")
        with _stage_parent_fd(stage, staged.relative_path, create=False) as stage_fd:
            _verify_staged_file(stage_fd, staged)
            if not _parent_is_attached(config_fd, target, target_fd):
                raise _RaceError(f"{label} parent detached before replacement.")
            if expected is None:
                try:
                    rename_noreplace(
                        stage_fd,
                        staged.relative_path.name,
                        target_fd,
                        target_name,
                    )
                except OSError as error:
                    raise _RaceError(f"{label} changed during replacement.") from error
            else:
                quarantine = _quarantine_target(stage, target_fd, target_name)
                try:
                    quarantined_value = _read_regular_at(stage.descriptor, quarantine.name)
                except ValueError:
                    _restore_quarantine(stage, quarantine, target_fd, target_name)
                    raise _RaceError(f"{label} became unsafe during replacement.") from None
                quarantined_state = (
                    state_from_value(quarantined_value) if quarantined_value is not None else None
                )
                if quarantined_state != expected:
                    _restore_quarantine(stage, quarantine, target_fd, target_name)
                    raise _RaceError(f"{label} changed during replacement.")
                if not _parent_is_attached(config_fd, target, target_fd):
                    _restore_quarantine(stage, quarantine, target_fd, target_name)
                    raise _RaceError(f"{label} parent detached during replacement.")
                try:
                    rename_noreplace(
                        stage_fd,
                        staged.relative_path.name,
                        target_fd,
                        target_name,
                    )
                except OSError as error:
                    _preserve_quarantine(stage, quarantine)
                    raise _QuarantinePreservedError(
                        f"{label} changed during publication."
                    ) from error
                if not _parent_is_attached(config_fd, target, target_fd):
                    _preserve_quarantine(stage, quarantine)
                    raise _QuarantinePreservedError(f"{label} parent detached after replacement.")
            if not _parent_is_attached(config_fd, target, target_fd):
                raise _RaceError(f"{label} parent detached after replacement.")


def _quarantine_target(
    stage: _Stage,
    target_fd: int,
    target_name: str,
) -> PurePosixPath:
    quarantine = PurePosixPath(f".quarantine-{secrets.token_hex(16)}")
    rename_noreplace(target_fd, target_name, stage.descriptor, quarantine.name)
    try:
        current = os.stat(quarantine.name, dir_fd=stage.descriptor, follow_symlinks=False)
        stage.entries[quarantine] = _StageEntry(
            current.st_dev,
            current.st_ino,
            stat.S_ISDIR(current.st_mode),
        )
    except (OSError, MemoryError) as error:
        _preserve_quarantine(stage, quarantine)
        raise _QuarantinePreservedError(
            "Quarantined file metadata could not be recorded safely."
        ) from error
    return quarantine


def _restore_quarantine(
    stage: _Stage,
    quarantine: PurePosixPath,
    target_fd: int,
    target_name: str,
) -> None:
    try:
        rename_noreplace(stage.descriptor, quarantine.name, target_fd, target_name)
    except OSError:
        _preserve_quarantine(stage, quarantine)
        raise _QuarantinePreservedError("Quarantined file could not be restored safely.") from None
    stage.entries.pop(quarantine, None)


def _preserve_quarantine(stage: _Stage, quarantine: PurePosixPath) -> None:
    stage.entries.pop(quarantine, None)


def _unlink_file_at(
    config_fd: int,
    target: PurePosixPath,
    expected: _FileState | None,
) -> bool:
    try:
        with _parent_fd(config_fd, target, create=False) as (parent_fd, name):
            try:
                current = _read_regular_at(parent_fd, name)
            except ValueError as error:
                raise _RaceError("Managed stale file became unsafe before removal.") from error
            current_state = (
                _FileState(_digest(current[0]), current[1]) if current is not None else None
            )
            if current_state != expected:
                raise _RaceError("Managed stale file changed immediately before removal.")
            if current is None:
                return False
            if not _parent_is_attached(config_fd, target, parent_fd):
                raise _RaceError("Managed stale file parent detached before removal.")
            quarantine_stage = _create_stage(config_fd)
            try:
                quarantine = _quarantine_target(quarantine_stage, parent_fd, name)
                try:
                    quarantined = _read_regular_at(
                        quarantine_stage.descriptor,
                        quarantine.name,
                    )
                except ValueError:
                    _restore_quarantine(quarantine_stage, quarantine, parent_fd, name)
                    raise _RaceError("Managed stale file became unsafe during removal.") from None
                quarantined_state = (
                    _FileState(_digest(quarantined[0]), quarantined[1])
                    if quarantined is not None
                    else None
                )
                if quarantined_state != expected:
                    _restore_quarantine(quarantine_stage, quarantine, parent_fd, name)
                    raise _RaceError("Managed stale file changed during removal.")
                if not _parent_is_attached(config_fd, target, parent_fd):
                    _restore_quarantine(quarantine_stage, quarantine, parent_fd, name)
                    raise _RaceError("Managed stale file parent detached during removal.")
            finally:
                _cleanup_stage(config_fd, quarantine_stage)
            return True
    except FileNotFoundError:
        if expected is not None:
            raise _RaceError("Managed stale path disappeared before removal.") from None
        return False


def _target_path(root: Path, relative: PurePosixPath) -> Path:
    if not is_safe_relative_path(relative):
        raise ValueError("Managed path is absolute or contains traversal.")
    root_resolved = root.resolve()
    target = root.joinpath(*relative.parts)
    if not target.resolve(strict=False).is_relative_to(root_resolved):
        raise ValueError("Managed path resolves outside its workflow root.")
    return target


def _parse_carrier(path: PurePosixPath, raw: bytes | None) -> tuple[dict[str, object], str | None]:
    if raw is None:
        return {}, None
    try:
        parsed: object
        if path.suffix == ".json":
            parsed = json.loads(raw)
        elif path.suffix == ".toml":
            parsed = tomllib.loads(raw.decode())
        else:
            return {}, "Unsupported settings carrier format."
        if not isinstance(parsed, dict):
            return {}, "Settings carrier must contain an object/table."
        return cast(dict[str, object], parsed), None
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}, "Settings carrier is malformed."


def _nested_get(data: dict[str, object], keys: tuple[str, ...]) -> tuple[bool, object, str | None]:
    current = data
    for key in keys[:-1]:
        value = current.get(key)
        if value is None:
            return False, None, None
        if not isinstance(value, dict):
            return False, None, "Settings fragment parent is not a table/object."
        current = cast(dict[str, object], value)
    if not keys or keys[-1] not in current:
        return False, None, None
    return True, current[keys[-1]], None


def _nested_set(data: dict[str, object], keys: tuple[str, ...], value: object) -> None:
    if not keys:
        raise ValueError("Settings key path is empty.")
    current = data
    for key in keys[:-1]:
        child = current.get(key)
        if child is None:
            child = {}
            current[key] = child
        if not isinstance(child, dict):
            raise ValueError("Settings fragment parent is not a table/object.")
        current = cast(dict[str, object], child)
    current[keys[-1]] = value


def _nested_remove(data: dict[str, object], keys: tuple[str, ...]) -> None:
    if not keys:
        return
    parents: list[tuple[dict[str, object], str]] = []
    current = data
    for key in keys[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            return
        parents.append((current, key))
        current = cast(dict[str, object], child)
    current.pop(keys[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)


def _manifest_failure(source: ConfigSyncSource, message: str) -> ConfigSyncAudit:
    return ConfigSyncAudit(
        source,
        None,
        (DriftItem(DriftClass.MANIFEST_INVALID, message),),
        (SyncProblem("manifest", message),),
    )


def _config_directory_failure(source: ConfigSyncSource) -> ConfigSyncAudit:
    message = "Project config path must be a real directory."
    return ConfigSyncAudit(
        source,
        None,
        (DriftItem(DriftClass.INVALID_VIEW, message),),
        (SyncProblem("config-directory", message),),
    )


def _source_changed_audit(
    source: ConfigSyncSource,
    manifest: _Manifest | None,
    *,
    message: str = "Configured source or authoritative bytes changed during synchronization.",
) -> ConfigSyncAudit:
    return ConfigSyncAudit(
        source,
        manifest.active_source if manifest else None,
        (DriftItem(DriftClass.SOURCE_CHANGED, message, source),),
    )


def _deduplicate_drifts(items: list[DriftItem]) -> tuple[DriftItem, ...]:
    values = {(item.kind, item.tool, item.relative_path, item.message): item for item in items}
    return tuple(
        sorted(
            values.values(),
            key=lambda item: (
                item.kind,
                item.tool or "",
                item.relative_path.as_posix() if item.relative_path else "",
                item.message,
            ),
        )
    )


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _digest_json(value: object) -> str:
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
