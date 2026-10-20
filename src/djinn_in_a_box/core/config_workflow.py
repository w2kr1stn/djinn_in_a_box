from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncSource
from djinn_in_a_box.core.config_lock import config_directory_lock
from djinn_in_a_box.core.config_sync import (
    CANONICAL_REMEDY,
    ConfigSyncAudit,
    ConfigSyncResult,
    DriftClass,
    SyncProblem,
    _BuildError,  # pyright: ignore[reportPrivateUsage]
    _release_manifest_records,  # pyright: ignore[reportPrivateUsage]
    _snapshot_build,  # pyright: ignore[reportPrivateUsage]
    audit_config_sync,
    load_canonical_delivery_view,
    sync_config,
    workflow_item_label,
    workflow_item_summary,
    workflow_label_component,
    workflow_remedy,
)
from djinn_in_a_box.core.docker import (
    WorkflowImageCompatibility,
    ensure_host_env,
    get_config_root,
    workflow_image_compatible,
)
from djinn_in_a_box.core.paths import CONFIG_DIR, get_project_root
from djinn_in_a_box.core.workflow_drift_backup import BackupAdmission, open_backup_root
from djinn_in_a_box.core.workflow_publisher import (
    CANONICAL_MANIFEST_NAME,
    CHANGED_DURING_PUBLISH,
    RUNTIME_MANIFEST_NAME,
    BackedUpEntry,
    BackedUpState,
    CanonicalLockLease,
    CarrierFragment,
    DriftEntry,
    ManifestItem,
    PublishedFile,
    PublishError,
    PublishResult,
    WorkflowView,
    backup_target_drift,
    canonical_lock,
    inspect_workflow_view,
    load_strict_json,
    publish_workflow_view,
    runtime_residue_prefixes,
)

HOST_WORKFLOW_TRUST_FILE = CONFIG_DIR / "host-workflow-trust.json"


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


_CLAUDE_HOOK_REWRITES: dict[tuple[str, ...], tuple[bytes, bytes]] = {
    ("hooks", "PreToolUse"): (
        _canonical_json(
            [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 ~/.claude_seed/security_reminder_hook.py",
                        }
                    ],
                }
            ]
        ),
        _canonical_json(
            [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 ~/.claude/security_reminder_hook.py",
                        }
                    ],
                }
            ]
        ),
    ),
    ("hooks", "Stop"): (
        _canonical_json(
            [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 ~/.claude_seed/ready_notify_hook.py",
                        }
                    ],
                }
            ]
        ),
        _canonical_json(
            [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 ~/.claude/ready_notify_hook.py",
                        }
                    ],
                }
            ]
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class WorkflowDeliveryTarget:
    tool: ConfigSyncSource
    destination_root: Path
    provision: bool = False


@dataclass(frozen=True, slots=True)
class HostWorkflowChange:
    status: Literal["new", "changed", "removed"]
    label: str


@dataclass(frozen=True, slots=True)
class HostWorkflowReview:
    tool: ConfigSyncSource
    sources: tuple[Path, ...]
    destination_root: Path
    items: frozenset[ManifestItem]
    changes: tuple[HostWorkflowChange, ...]


@dataclass(frozen=True, slots=True)
class _Delivery:
    view: WorkflowView
    items: frozenset[ManifestItem]
    source: ConfigSyncSource
    source_inputs: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class WorkflowPreparationProblem:
    identifier: str
    message: str
    remedy: str


@dataclass(frozen=True, slots=True)
class WorkflowPreparationResult:
    success: bool
    problems: tuple[WorkflowPreparationProblem, ...] = ()
    notices: tuple[str, ...] = ()



def start_workflow_targets(config: AppConfig) -> tuple[WorkflowDeliveryTarget, ...]:
    root = get_config_root(config)
    return tuple(WorkflowDeliveryTarget(tool, root / tool) for tool in ("claude", "codex"))


def print_workflow_notices(result: WorkflowPreparationResult) -> None:
    from djinn_in_a_box.core.console import info

    for notice in result.notices:
        info(notice)


@dataclass(frozen=True, slots=True)
class WorkflowTargetInspection:
    target: WorkflowDeliveryTarget
    result: PublishResult | None = None
    problem: WorkflowPreparationProblem | None = None
    view: WorkflowView | None = None

    @property
    def clean(self) -> bool:
        return self.problem is None and self.result is not None and self.result.success


def inspect_workflow_targets(
    project_root: Path,
    config: AppConfig,
    targets: tuple[WorkflowDeliveryTarget, ...] | None = None,
    *,
    compose: bool = True,
) -> tuple[WorkflowTargetInspection, ...]:
    selected = start_workflow_targets(config) if targets is None else targets
    selected = tuple(t for t in selected if not _compose_claude_target(config, t, compose))
    if not selected:
        return ()
    try:
        with canonical_lock(project_root / "config", exclusive=False) as lease:
            build = _snapshot_build(project_root, config.config_sync.source)
            if build.problems:
                problem = _audit_failure(
                    ConfigSyncAudit(build.source, None, problems=build.problems)
                ).problems[0]
                problem = replace(
                    problem, identifier="runtime-not-checked",
                    message=f"not checked ({workflow_label_component(problem.identifier)}: "
                    f"{problem.message})",
                )
                return tuple(WorkflowTargetInspection(t, problem=problem) for t in selected)
            inspections: list[WorkflowTargetInspection] = []
            for target in selected:
                # Missing roots are not provisioned by an inspection.
                problem = _prepare_target(replace(target, provision=True), provision=False)
                if problem is not None:
                    inspections.append(WorkflowTargetInspection(target, problem=problem))
                    continue
                if not target.destination_root.exists():
                    inspections.append(
                        WorkflowTargetInspection(target, PublishResult(DriftClass.CLEAN))
                    )
                    continue
                view = _delivery_view(config, target, build.views[target.tool])
                if view is None:
                    problem = _failure(DriftClass.INVALID_OR_SEMANTIC.value).problems[0]
                    inspections.append(WorkflowTargetInspection(target, problem=problem))
                    continue
                result = inspect_workflow_view(
                    view,
                    project_root / "config",
                    target.destination_root,
                    target.destination_root / RUNTIME_MANIFEST_NAME,
                    canonical_lease=lease,
                )
                inspections.append(WorkflowTargetInspection(target, result, view=view))
            return tuple(inspections)
    except PublishError as error:
        if error.lock_error is not None:
            problem = _canonical_lock_failure(project_root / "config", error).problems[0]
        else:
            problem = _failure(error.drift_class.value).problems[0]
        return tuple(WorkflowTargetInspection(t, problem=problem) for t in selected)
    except (_BuildError, OSError, ValueError) as error:
        reason = error.drift.value if isinstance(error, _BuildError) else type(error).__name__
        problem = WorkflowPreparationProblem(
            "runtime-not-checked",
            f"not checked ({workflow_label_component(reason)})",
            CANONICAL_REMEDY,
        )
        return tuple(WorkflowTargetInspection(t, problem=problem) for t in selected)


def inspection_failure(
    inspection: WorkflowTargetInspection, config: AppConfig
) -> WorkflowPreparationResult:
    if inspection.problem is not None:
        return WorkflowPreparationResult(False, (inspection.problem,))
    assert inspection.result is not None
    if inspection.result.lock_error is not None:
        return _publish_lock_failure(
            inspection.target.destination_root, inspection.result.lock_error
        )
    if inspection.result.write_error is not None:
        return _publish_write_failure(
            inspection.target.destination_root, inspection.result.write_error
        )
    return _publish_failure(inspection.result, config, inspection.target, inspection.view)


@dataclass(frozen=True)
class WorkflowDiscardResult:
    sync: ConfigSyncResult | None = None
    failure: WorkflowPreparationResult | None = None
    backups: tuple[tuple[str, BackedUpEntry], ...] = ()
    backup_directory: Path | None = None
    failed_phase: str | None = None
    completed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


def discard_workflow_target_drift(
    project_root: Path,
    config: AppConfig,
    admission: BackupAdmission,
    *,
    config_path: Path | None = None,
) -> WorkflowDiscardResult:
    """Run the read-only check, durable backup barrier and authorized publications.

    The command holds lifecycle exclusion throughout this call. Publisher leases are
    acquired independently in each phase; none crosses the backup barrier.
    """
    audit = audit_config_sync(project_root, config_path=config_path)
    inspections = inspect_workflow_targets(project_root, config)
    allowed = {DriftClass.SOURCE_CHANGED, DriftClass.TARGET_DRIFT}
    if audit.problems or any(item.kind not in allowed for item in audit.drifts):
        return WorkflowDiscardResult(failure=_audit_failure(audit))
    for inspection in inspections:
        if not inspection.clean and (
            inspection.result is None
            or inspection.result.drift_class is not DriftClass.TARGET_DRIFT
        ):
            return WorkflowDiscardResult(failure=inspection_failure(inspection, config))
    drifted = any(item.kind is DriftClass.TARGET_DRIFT for item in audit.drifts) or any(
        item.result is not None and item.result.drift_class is DriftClass.TARGET_DRIFT
        for item in inspections
    )
    if not drifted:
        return WorkflowDiscardResult(sync=sync_config(project_root, config_path=config_path))

    canonical_root = project_root / "config"
    tools = tuple(inspection.target.tool for inspection in inspections)
    backups: dict[str, tuple[BackedUpEntry, ...]] = {}
    backup_directory = admission.path
    backup_target: WorkflowDeliveryTarget | None = None
    try:
        with open_backup_root(admission) as directories:
            # Phase 1: release source-owned records exactly as canonical sync does.
            with canonical_lock(canonical_root, exclusive=True) as lease:
                build = _snapshot_build(project_root, config.config_sync.source)
                if build.problems:
                    return WorkflowDiscardResult(
                        failure=_audit_failure(ConfigSyncAudit(build.source, None,
                                                               problems=build.problems)),
                        backup_directory=backup_directory,
                        failed_phase="backup",
                        skipped=("canonical", *tools),
                    )
                preflight_manifest = _release_manifest_records(
                    canonical_root, build.source, build.fingerprint
                )
                backups["config"] = backup_target_drift(
                    build.canonical, canonical_root, canonical_root,
                    canonical_root / CANONICAL_MANIFEST_NAME, directories.root("config"),
                    canonical_lease=lease, preflight_manifest=preflight_manifest,
                )
            for inspection in inspections:
                if inspection.view is None:
                    continue
                target = inspection.target
                backup_target = target
                backups[target.tool] = backup_target_drift(
                    inspection.view, canonical_root, target.destination_root,
                    target.destination_root / RUNTIME_MANIFEST_NAME, directories.root(target.tool),
                )
            directories.barrier()
    except PublishError as exc:
        if exc.lock_error is not None:
            failure = _canonical_lock_failure(canonical_root, exc)
        else:
            target = backup_target
            failure = _failure(
                exc.drift_class.value, source=config.config_sync.source,
                labels=tuple(workflow_item_label(
                    target.tool if target is not None else None, item.path, item.key_path,
                    root=target.destination_root if target is not None else None,
                    origin=item.origin,
                ) for item in exc.items),
            )
        return WorkflowDiscardResult(
            failure=failure, backup_directory=backup_directory,
            failed_phase="backup", skipped=("canonical", *tools),
        )
    except (_BuildError, OSError, ValueError) as exc:
        failure = WorkflowPreparationResult(False, (WorkflowPreparationProblem(
            "drift-backup-failed", f"Workflow drift backup failed ({exc}).",
            "Correct the backup problem and retry; partial backups have been kept.",
        ),))
        return WorkflowDiscardResult(
            failure=failure, backup_directory=backup_directory,
            failed_phase="backup", skipped=("canonical", *tools),
        )

    entries = tuple((label, entry) for label, items in backups.items() for entry in items)

    def authorization(
        label: str,
    ) -> dict[tuple[PurePosixPath, tuple[str, ...] | None], BackedUpState]:
        return {(e.entry.path, e.entry.key_path): e.state for e in backups.get(label, ())}

    # Phase 2: these calls acquire fresh leases. Preparation would reject runtime
    # drift before the authorized delivery, so use the delivery level directly.
    synced = sync_config(
        project_root, config_path=config_path, accept_drift=authorization("config")
    )
    if not synced.success or not synced.audit.clean:
        return WorkflowDiscardResult(
            synced, backups=entries, backup_directory=backup_directory,
            failed_phase="canonical", skipped=tools,
        )
    for index, inspection in enumerate(inspections):
        result = _deliver_target(
            config, project_root, inspection.target, config_path, False, None,
            accept_drift=authorization(inspection.target.tool),
        )
        if not result.success:
            return WorkflowDiscardResult(
                synced, result, entries, backup_directory,
                failed_phase=f"runtime {inspection.target.tool}",
                completed=("canonical", *tools[:index]), skipped=tools[index + 1:],
            )
    return WorkflowDiscardResult(synced, backups=entries, backup_directory=backup_directory)


def prepare_config_workflow(
    project_root: Path,
    targets: tuple[WorkflowDeliveryTarget, ...] = (),
    *,
    config_path: Path | None = None,
    config_snapshot: AppConfig | None = None,
    require_compose_host_env: bool = False,
    container_image_compatibility: WorkflowImageCompatibility | None = None,
    confirm_host_workflow: Callable[[HostWorkflowReview], bool] | None = None,
) -> WorkflowPreparationResult:
    try:
        config = config_snapshot if config_snapshot is not None else load_config(config_path)
    except (OSError, TypeError, ValueError):
        return _failure("invalid-or-semantic")
    if require_compose_host_env:
        image_compatibility = (
            container_image_compatibility
            if container_image_compatibility is not None
            else workflow_image_compatible()
        )
        if image_compatibility is not WorkflowImageCompatibility.COMPATIBLE:
            if image_compatibility is WorkflowImageCompatibility.UNKNOWN:
                problem = WorkflowPreparationProblem(
                    "image-unreachable",
                    "Docker daemon/container not reachable.",
                    "Retry.",
                )
            elif image_compatibility is WorkflowImageCompatibility.MISSING:
                problem = WorkflowPreparationProblem(
                    "image-not-built",
                    "Workflow image is not built.",
                    "Run `djinn build`, then retry.",
                )
            elif image_compatibility is WorkflowImageCompatibility.INCOMPATIBLE:
                problem = WorkflowPreparationProblem(
                    "image-incompatible",
                    "Workflow image is incompatible.",
                    "Rebuild/recreate required.",
                )
            else:
                msg = f"Unhandled workflow image compatibility: {image_compatibility!r}"
                raise AssertionError(msg)
            return WorkflowPreparationResult(
                False,
                (problem,),
            )
        try:
            ensure_host_env(config)
        except OSError as e:
            # A host-path permission problem is not workflow drift. Routing it
            # through _failure() would answer "your config root is unwritable"
            # with the canonical remedy about non-portable workflow artifacts,
            # which sends the user looking in entirely the wrong place.
            return WorkflowPreparationResult(
                False,
                (
                    WorkflowPreparationProblem(
                        "host-provisioning-failed",
                        f"Failed to provision host directories: {e}",
                        "Check that your home and config-root paths are writable, then retry.",
                    ),
                ),
            )

    audit = audit_config_sync(project_root, config_path=config_path)
    if not audit.clean and not _auto_repairable(audit):
        return _audit_failure(audit)
    for inspection in inspect_workflow_targets(
        project_root, config, targets, compose=require_compose_host_env
    ):
        if not inspection.clean:
            return inspection_failure(inspection, config)
    notices: tuple[str, ...] = ()
    if not audit.clean:
        synced = sync_config(project_root, config_path=config_path)
        if not synced.success or not synced.audit.clean:
            return _audit_failure(synced.audit)
        notices = (
            "Synchronized source changes into the canonical workflow: "
            f"{len(synced.changed_paths)} changed, {len(synced.removed_paths)} removed.",
        )

    for target in targets:
        if _compose_claude_target(config, target, require_compose_host_env):
            continue
        gated = target.destination_root != get_config_root(config) / target.tool
        target_problem = _prepare_target(target, provision=not gated)
        if target_problem is not None:
            return WorkflowPreparationResult(False, (target_problem,), notices)
        result = _deliver_target(
            config, project_root, target, config_path, gated, confirm_host_workflow
        )
        if not result.success:
            return replace(result, notices=notices + result.notices)
    return WorkflowPreparationResult(True, notices=notices)


def _load_delivery(
    config: AppConfig,
    project_root: Path,
    target: WorkflowDeliveryTarget,
    config_path: Path | None,
    lease: CanonicalLockLease,
) -> _Delivery | WorkflowPreparationResult:
    loaded = load_canonical_delivery_view(
        project_root, target.tool, config_path=config_path, canonical_lease=lease
    )
    if not loaded.success or loaded.view is None:
        return _audit_failure(loaded.audit)
    view = _delivery_view(config, target, loaded.view)
    if view is None:
        return _failure("invalid-or-semantic")
    items = frozenset(
        [
            ManifestItem(
                item.relative_path, hashlib.sha256(item.content).hexdigest(), item.executable
            )
            for item in view.files
        ]
        + [
            ManifestItem(
                item.carrier_path, hashlib.sha256(item.value_json).hexdigest(), False, item.key_path
            )
            for item in view.fragments
        ]
    )
    return _Delivery(view, items, loaded.audit.configured_source, loaded.source_inputs)


def _deliver_target(
    config: AppConfig,
    project_root: Path,
    target: WorkflowDeliveryTarget,
    config_path: Path | None,
    gated: bool,
    confirm: Callable[[HostWorkflowReview], bool] | None,
    *,
    accept_drift: (
        Mapping[tuple[PurePosixPath, tuple[str, ...] | None], BackedUpState] | None
    ) = None,
) -> WorkflowPreparationResult:
    canonical_root = project_root / "config"
    record_path = HOST_WORKFLOW_TRUST_FILE
    root_key = str(target.destination_root.absolute())
    confirmed = _load_host_trust(record_path).get(root_key) if gated else None
    try:
        with canonical_lock(canonical_root, exclusive=False) as lease:
            delivery = _load_delivery(config, project_root, target, config_path, lease)
            if isinstance(delivery, WorkflowPreparationResult):
                return delivery
            if not gated or delivery.items == confirmed:
                return _publish_delivery(
                    delivery, canonical_root, target, lease,
                    config=config, accept_drift=accept_drift
                )
    except PublishError as error:
        return _canonical_lock_failure(canonical_root, error)
    except OSError:
        return _failure(DriftClass.INVALID_OR_SEMANTIC.value)

    sources = (canonical_root / delivery.source,)
    if target.tool != delivery.source:
        sources += (canonical_root / target.tool,)
    if target.tool == "claude":
        sources += (_claude_bridge_path(),)
    review = HostWorkflowReview(
        target.tool,
        sources,
        target.destination_root,
        delivery.items,
        _host_workflow_changes(delivery.items, confirmed or frozenset()),
    )
    if confirm is None or not confirm(review):
        return WorkflowPreparationResult(
            False,
            (
                WorkflowPreparationProblem(
                    "host-workflow-unconfirmed",
                    f"Host workflow for {target.tool} was not confirmed; nothing was published.",
                    f"Run `djinn session --agent {target.tool}` in a terminal, "
                    "review the listed changes and confirm.",
                ),
            ),
        )
    try:
        with canonical_lock(canonical_root, exclusive=False) as lease:
            delivery = _load_delivery(config, project_root, target, config_path, lease)
            if isinstance(delivery, WorkflowPreparationResult):
                if delivery.problems[0].identifier == "canonical-lock-failed":
                    return delivery
                return _host_workflow_changed()
            if delivery.items != review.items:
                return _host_workflow_changed()
            result = _publish_delivery(
                delivery, canonical_root, target, lease, config=config, confirming=True
            )
    except PublishError as error:
        return _canonical_lock_failure(canonical_root, error)
    except OSError:
        return _host_workflow_changed()
    if not result.success:
        return result
    try:
        _save_host_trust(record_path, root_key, review.items)
    except OSError as error:
        return WorkflowPreparationResult(
            False,
            (
                WorkflowPreparationProblem(
                    "host-workflow-trust-failed",
                    f"Failed to record host workflow trust at {record_path}: {error}",
                    f"Make {record_path.parent} writable, then retry.",
                ),
            ),
        )
    return result


def _publish_delivery(
    delivery: _Delivery,
    canonical_root: Path,
    target: WorkflowDeliveryTarget,
    lease: CanonicalLockLease,
    *,
    config: AppConfig,
    confirming: bool = False,
    accept_drift: (
        Mapping[tuple[PurePosixPath, tuple[str, ...] | None], BackedUpState] | None
    ) = None,
) -> WorkflowPreparationResult:
    # A failed publication after confirmation can leave the (empty) destination created
    # here; it is deliberately not removed, because a path-based cleanup cannot prove that
    # this invocation created what it would delete.
    target_problem = _prepare_target(target)
    if target_problem is not None:
        return WorkflowPreparationResult(False, (target_problem,))
    published = publish_workflow_view(
        delivery.view,
        canonical_root,
        target.destination_root,
        target.destination_root / RUNTIME_MANIFEST_NAME,
        canonical_lease=lease,
        accept_drift=accept_drift,
        source_root=canonical_root / delivery.source,
        source_inputs=delivery.source_inputs,
        source_residue_prefixes=runtime_residue_prefixes(delivery.source),
    )
    if published.lock_error is not None:
        return _publish_lock_failure(target.destination_root, published.lock_error)
    if published.write_error is not None:
        return _publish_write_failure(target.destination_root, published.write_error)
    if not published.success:
        if confirming and published.drift_class is DriftClass.SOURCE_CHANGED:
            return _host_workflow_changed()
        return _publish_failure(published, config, target, delivery.view)
    return WorkflowPreparationResult(True)


def _host_workflow_changed() -> WorkflowPreparationResult:
    return WorkflowPreparationResult(
        False,
        (
            WorkflowPreparationProblem(
                "host-workflow-changed",
                "Workflow source changed during confirmation; nothing was published.",
                "Retry.",
            ),
        ),
    )


def _host_workflow_changes(
    items: frozenset[ManifestItem], confirmed: frozenset[ManifestItem]
) -> tuple[HostWorkflowChange, ...]:
    old = {(item.path, item.key_path): item for item in confirmed}
    new = {(item.path, item.key_path): item for item in items}
    changes: list[HostWorkflowChange] = []
    for key in old.keys() | new.keys():
        if key not in old:
            status, item = "new", new[key]
        elif key not in new:
            status, item = "removed", old[key]
        elif old[key] != new[key]:
            status, item = "changed", new[key]
        else:
            continue
        label = item.path.as_posix()
        if item.key_path is not None:
            label += ": " + ".".join(item.key_path)
        elif item.executable:
            label += " (executable)"
        changes.append(HostWorkflowChange(status, label))
    return tuple(sorted(changes, key=lambda change: change.label))


def _load_host_trust(record_path: Path) -> dict[str, frozenset[ManifestItem]]:
    try:
        raw = load_strict_json(record_path.read_bytes())
        if not isinstance(raw, dict):
            raise ValueError
        result: dict[str, frozenset[ManifestItem]] = {}
        for root, entries in cast(dict[str, object], raw).items():
            if not root or not PurePosixPath(root).is_absolute() or not isinstance(entries, list):
                raise ValueError
            items: list[ManifestItem] = []
            identities: set[tuple[PurePosixPath, tuple[str, ...] | None]] = set()
            for entry in cast(list[object], entries):
                if not isinstance(entry, dict):
                    raise ValueError
                item = cast(dict[str, object], entry)
                if set(item) != {"path", "key_path", "content_hash", "executable"}:
                    raise ValueError
                path, key_path = item["path"], item["key_path"]
                content_hash, executable = item["content_hash"], item["executable"]
                if (
                    not isinstance(path, str)
                    or not path
                    or PurePosixPath(path).is_absolute()
                    or ".." in PurePosixPath(path).parts
                ):
                    raise ValueError
                if key_path is not None and (
                    not isinstance(key_path, list)
                    or not key_path
                    or any(
                        not isinstance(key, str) or not key for key in cast(list[object], key_path)
                    )
                ):
                    raise ValueError
                if (
                    not isinstance(content_hash, str)
                    or len(content_hash) != 64
                    or any(char not in "0123456789abcdef" for char in content_hash)
                    or not isinstance(executable, bool)
                    or (key_path is not None and executable)
                ):
                    raise ValueError
                keys = tuple(cast(list[str], key_path)) if key_path is not None else None
                identity = (PurePosixPath(path), keys)
                if identity in identities:
                    raise ValueError
                identities.add(identity)
                items.append(ManifestItem(identity[0], content_hash, executable, keys))
            result[root] = frozenset(items)
        return result
    except (OSError, ValueError, RecursionError):
        return {}


def _save_host_trust(record_path: Path, root: str, items: frozenset[ManifestItem]) -> None:
    record_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with config_directory_lock(record_path.parent, exclusive=True):
        records = _load_host_trust(record_path)
        records[root] = items
        payload = {
            key: [
                {
                    "path": item.path.as_posix(),
                    "key_path": item.key_path,
                    "content_hash": item.content_hash,
                    "executable": item.executable,
                }
                for item in sorted(
                    value, key=lambda item: (item.path.as_posix(), item.key_path or ())
                )
            ]
            for key, value in records.items()
        }
        descriptor, temporary = tempfile.mkstemp(dir=record_path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(_canonical_json(payload))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, record_path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def _compose_claude_target(
    config: AppConfig, target: WorkflowDeliveryTarget, compose: bool
) -> bool:
    return (
        compose
        and target.tool == "claude"
        and target.destination_root == get_config_root(config) / "claude"
    )


def _prepare_target(
    target: WorkflowDeliveryTarget, *, provision: bool = True
) -> WorkflowPreparationProblem | None:
    try:
        entry = target.destination_root.lstat()
    except FileNotFoundError:
        if not provision and target.provision:
            return None
        return _prepare_missing_target(target)
    except OSError as error:
        return WorkflowPreparationProblem(
            "target-provisioning-failed",
            f"Failed to prepare workflow destination {target.destination_root}: {error}",
            "Check that the destination and its parent directories are writable, then retry.",
        )
    if stat.S_ISLNK(entry.st_mode):
        return WorkflowPreparationProblem(
            "target-symlink",
            f"Workflow destination is a symlink: {target.destination_root}",
            "Replace the symlink with a directory managed by Djinn, then retry.",
        )
    if stat.S_ISDIR(entry.st_mode):
        return None
    return WorkflowPreparationProblem(
        "target-not-directory",
        f"Workflow destination is not a directory: {target.destination_root}",
        "Replace the destination with a directory managed by Djinn, then retry.",
    )


def _prepare_missing_target(target: WorkflowDeliveryTarget) -> WorkflowPreparationProblem | None:
    if not target.provision:
        return WorkflowPreparationProblem(
            "target-missing",
            f"Workflow destination does not exist: {target.destination_root}",
            "Create the destination directory or enable its provisioning, then retry.",
        )
    try:
        target.destination_root.mkdir(parents=True)
    except OSError as error:
        return WorkflowPreparationProblem(
            "target-provisioning-failed",
            f"Failed to prepare workflow destination {target.destination_root}: {error}",
            "Check that the destination and its parent directories are writable, then retry.",
        )
    return None


def _delivery_view(
    config: AppConfig, target: WorkflowDeliveryTarget, build_view: WorkflowView
) -> WorkflowView | None:
    if target.tool != "claude" or target.destination_root == get_config_root(config) / "claude":
        return build_view
    fragments: list[CarrierFragment] = []
    for fragment in build_view.fragments:
        replacement = _CLAUDE_HOOK_REWRITES.get(fragment.key_path)
        if fragment.carrier_path.as_posix() == "settings.json" and replacement is not None:
            if fragment.value_json == replacement[0]:
                fragment = CarrierFragment(fragment.carrier_path, fragment.key_path, replacement[1])
            elif fragment.value_json != replacement[1]:
                return None
        fragments.append(fragment)
    bridge = PublishedFile(
        PurePosixPath("CLAUDE.md"),
        _claude_bridge_path().read_bytes(),
    )
    return WorkflowView(
        build_view.source,
        (*build_view.files, bridge),
        tuple(fragments),
        build_view.source_fingerprint,
        build_view.target_tool,
        build_view.native_only_paths,
    )


def _claude_bridge_path() -> Path:
    return get_project_root() / "templates/claude/CLAUDE.md"


def _auto_repairable(audit: ConfigSyncAudit) -> bool:
    return (
        bool(audit.drifts)
        and not audit.problems
        and all(item.kind is DriftClass.SOURCE_CHANGED for item in audit.drifts)
    )


def _audit_failure(audit: ConfigSyncAudit) -> WorkflowPreparationResult:
    if audit.problems:
        labels = tuple(workflow_item_label(p.tool, p.relative_path) for p in audit.problems)
        return _sync_problem_failure(audit.problems[0], labels=labels)
    drift = next(
        (item.kind for item in audit.drifts if item.kind is not DriftClass.CLEAN),
        DriftClass.INVALID_OR_SEMANTIC,
    )
    labels = tuple(
        workflow_item_label(item.tool, item.relative_path, item.key_path)
        for item in audit.drifts if item.relative_path is not None
    )
    return _failure(
        drift.value,
        labels=labels,
        source=audit.configured_source,
        items=tuple(
            DriftEntry(
                item.kind,
                item.relative_path,
                item.key_path,
                reason=item.reason,
            )
            for item in audit.drifts if item.relative_path is not None
        ),
    )


def _sync_problem_failure(
    problem: SyncProblem, *, labels: tuple[str, ...] = ()
) -> WorkflowPreparationResult:
    locations = labels or (workflow_item_label(problem.tool, problem.relative_path),)
    return WorkflowPreparationResult(
        False,
        (
            WorkflowPreparationProblem(
                problem.identifier,
                f"{problem.message} ({workflow_item_summary(locations)})",
                problem.remedy or CANONICAL_REMEDY,
            ),
        ),
    )


def _publish_failure(
    result: PublishResult,
    config: AppConfig,
    target: WorkflowDeliveryTarget,
    view: WorkflowView | None = None,
) -> WorkflowPreparationResult:
    host = target.destination_root != get_config_root(config) / target.tool
    labels = tuple(
        workflow_item_label(
            target.tool, item.path, item.key_path, root=target.destination_root, origin=item.origin
        )
        for item in result.drift_items
    )
    failure = _failure(
        result.drift_class.value,
        labels=labels,
        source=config.config_sync.source,
        scope="host" if host else "start",
        items=result.drift_items,
    )
    if host and result.drift_class is DriftClass.TARGET_DRIFT and view is not None:
        # Only real managed file or carrier-key drift has a restore source. Manifest
        # entries are ownership metadata (never to be deleted or replaced by hand) and
        # a changed-during-publish refusal asks for a retry, not for an edit.
        actions = tuple(
            _host_restore_action(config, target, view, item)
            for item in [i for i in result.drift_items if _has_host_restore_action(i)][:5]
        )
        if actions:
            problem = failure.problems[0]
            failure = replace(
                failure,
                problems=(replace(problem, message=problem.message + " " + "; ".join(actions)),),
            )
    return failure


def _has_host_restore_action(item: DriftEntry) -> bool:
    return item.origin == "target" and item.reason != CHANGED_DURING_PUBLISH


def _host_restore_action(
    config: AppConfig, target: WorkflowDeliveryTarget, view: WorkflowView, item: DriftEntry
) -> str:
    label = workflow_item_label(target.tool, item.path, item.key_path, root=target.destination_root)
    files = {file.relative_path: file for file in view.files}
    fragments = {(fragment.carrier_path, fragment.key_path) for fragment in view.fragments}
    path = workflow_label_component(item.path)
    root = workflow_label_component(target.destination_root)
    if item.key_path is not None:
        key = ".".join(workflow_label_component(part) for part in item.key_path)
        if (item.path, item.key_path) not in fragments:
            return f"{label}: remove {key} from {root}/{path}"
        action = f"{label}: restore the value of {key} in config/{target.tool}/{path}"
        if target.tool == "claude" and item.key_path in _CLAUDE_HOOK_REWRITES:
            action += " with ~/.claude/ in place of ~/.claude_seed/"
        return action
    if item.path not in files:
        return f"{label}: remove {root}/{path}"
    if target.tool == "claude" and path == "CLAUDE.md":
        source = "templates/claude/CLAUDE.md of the Djinn installation"
    else:
        tool = target.tool
        source = f"config/{tool}/{path}"
    action = f"{label}: restore from {source}"
    if files[item.path].executable:
        action += "; keep the executable bit"
    return action


def _canonical_lock_failure(canonical_root: Path, error: PublishError) -> WorkflowPreparationResult:
    cause = error.__cause__ if isinstance(error.__cause__, OSError) else error
    return WorkflowPreparationResult(
        False,
        (
            WorkflowPreparationProblem(
                "canonical-lock-failed",
                # Deliberately does not say "acquire": the same channel carries
                # release failures (an interrupted unlock or close), where the
                # lease was held successfully and telling the user to restore
                # directory readability would be false advice.
                f"Canonical workflow lock failed at {canonical_root}: {cause}",
                "Check that the canonical workflow root is a readable, lockable "
                "directory and that no other Djinn process is stuck on it, then retry.",
            ),
        ),
    )


def _publish_lock_failure(destination: Path, error: OSError) -> WorkflowPreparationResult:
    """A lease failure, not a write failure.

    Kept apart from _publish_write_failure because nothing was published: naming
    the operation "publish" and advising writable directories with free space
    would describe neither what failed nor what fixes it.
    """
    return WorkflowPreparationResult(
        False,
        (
            WorkflowPreparationProblem(
                "workflow-lock-failed",
                f"Failed to lock the workflow destination {destination}: {error}",
                "Check that the destination is a lockable directory and that no other Djinn "
                "process is stuck on it, then retry.",
            ),
        ),
    )


def _publish_write_failure(destination: Path, error: OSError) -> WorkflowPreparationResult:
    return WorkflowPreparationResult(
        False,
        (
            WorkflowPreparationProblem(
                "workflow-publish-failed",
                f"Failed to publish workflow to {destination}: {error}",
                "Check that the workflow destination and its parent directories are writable "
                "with available space, then retry.",
            ),
        ),
    )


def _failure(
    identifier: str,
    *,
    labels: tuple[str, ...] = (),
    source: str = "claude",
    scope: str = "start",
    items: tuple[DriftEntry, ...] = (),
) -> WorkflowPreparationResult:
    try:
        drift = DriftClass(identifier)
    except ValueError:
        drift = DriftClass.INVALID_OR_SEMANTIC
    summary = workflow_item_summary(labels)
    location = f" in {summary}" if summary else ""
    return WorkflowPreparationResult(
        False,
        (
            WorkflowPreparationProblem(
                drift.value,
                f"Workflow preflight blocked: {drift.value}{location}.",
                workflow_remedy(drift, source, scope=scope, items=items),
            ),
        ),
    )
