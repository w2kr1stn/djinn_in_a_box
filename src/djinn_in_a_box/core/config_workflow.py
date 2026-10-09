from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncSource
from djinn_in_a_box.core.config_lock import config_directory_lock
from djinn_in_a_box.core.config_sync import (
    CANONICAL_REMEDY,
    ConfigSyncAudit,
    DriftClass,
    SyncProblem,
    audit_config_sync,
    load_canonical_delivery_view,
    sync_config,
)
from djinn_in_a_box.core.docker import (
    WorkflowImageCompatibility,
    ensure_host_env,
    get_config_root,
    workflow_image_compatible,
)
from djinn_in_a_box.core.paths import CONFIG_DIR, get_project_root
from djinn_in_a_box.core.workflow_publisher import (
    RUNTIME_MANIFEST_NAME,
    CanonicalLockLease,
    CarrierFragment,
    ManifestItem,
    PublishedFile,
    PublishError,
    PublishResult,
    WorkflowView,
    canonical_lock,
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
    if not audit.clean:
        if not _auto_repairable(audit):
            return _audit_failure(audit)
        synced = sync_config(project_root, config_path=config_path)
        if not synced.success or not synced.audit.clean:
            return _audit_failure(synced.audit)

    for target in targets:
        if _compose_claude_target(config, target, require_compose_host_env):
            continue
        gated = target.destination_root != get_config_root(config) / target.tool
        target_problem = _prepare_target(target, provision=not gated)
        if target_problem is not None:
            return WorkflowPreparationResult(False, (target_problem,))
        result = _deliver_target(
            config, project_root, target, config_path, gated, confirm_host_workflow
        )
        if not result.success:
            return result
    return WorkflowPreparationResult(True)


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
    view = _host_claude_view(config, target, loaded.view)
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
                return _publish_delivery(delivery, canonical_root, target, lease)
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
            result = _publish_delivery(delivery, canonical_root, target, lease, confirming=True)
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
    confirming: bool = False,
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
        return _publish_failure(published)
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


def _host_claude_view(
    config: AppConfig, target: WorkflowDeliveryTarget, view: WorkflowView
) -> WorkflowView | None:
    if target.tool != "claude" or target.destination_root == get_config_root(config) / "claude":
        return view
    fragments: list[CarrierFragment] = []
    for fragment in view.fragments:
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
        view.source,
        (*view.files, bridge),
        tuple(fragments),
        view.source_fingerprint,
        view.target_tool,
        view.native_only_paths,
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
        return _sync_problem_failure(audit.problems[0])
    drift = next(
        (item.kind for item in audit.drifts if item.kind is not DriftClass.CLEAN),
        DriftClass.INVALID_OR_SEMANTIC,
    )
    return _failure(drift.value)


def _sync_problem_failure(problem: SyncProblem) -> WorkflowPreparationResult:
    return WorkflowPreparationResult(
        False,
        (
            WorkflowPreparationProblem(
                problem.identifier,
                problem.message,
                problem.remedy or CANONICAL_REMEDY,
            ),
        ),
    )


def _publish_failure(result: PublishResult) -> WorkflowPreparationResult:
    return _failure(result.drift_class.value)


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


def _failure(identifier: str) -> WorkflowPreparationResult:
    try:
        drift = DriftClass(identifier)
    except ValueError:
        drift = DriftClass.INVALID_OR_SEMANTIC
    remedies = {
        DriftClass.SOURCE_CHANGED: "Run `djinn config sync`, then retry.",
        DriftClass.TARGET_DRIFT: "Restore or move the modified managed workflow item, then retry.",
        DriftClass.COLLISION: "Move or remove the conflicting unmanaged workflow item, then retry.",
        DriftClass.INVALID_OR_SEMANTIC: CANONICAL_REMEDY,
        DriftClass.CLEAN: "",
    }
    return WorkflowPreparationResult(
        False,
        (
            WorkflowPreparationProblem(
                drift.value,
                f"Workflow preflight blocked: {drift.value}.",
                remedies[drift],
            ),
        ),
    )
