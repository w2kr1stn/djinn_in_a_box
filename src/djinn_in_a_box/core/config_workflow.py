from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncSource
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
from djinn_in_a_box.core.workflow_publisher import (
    RUNTIME_MANIFEST_NAME,
    CarrierFragment,
    PublishError,
    PublishResult,
    WorkflowView,
    canonical_lock,
    publish_workflow_view,
    retire_legacy_delivery_manifest,
)


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
                            "command": "uv run python3 ~/.claude_seed/security_reminder_hook.py",
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
                            "command": "uv run python3 ~/.claude/security_reminder_hook.py",
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
                            "command": "uv run python3 ~/.claude_seed/ready_notify_hook.py",
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
                            "command": "uv run python3 ~/.claude/ready_notify_hook.py",
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

    canonical_root = project_root / "config"
    for target in targets:
        if _compose_claude_target(config, target, require_compose_host_env):
            retired = retire_legacy_delivery_manifest(target.destination_root)
            if retired.write_error is not None:
                return _publish_write_failure(target.destination_root, retired.write_error)
            if not retired.success:
                return _publish_failure(retired)
            continue
        target_problem = _prepare_target(target)
        if target_problem is not None:
            return WorkflowPreparationResult(False, (target_problem,))
        try:
            with canonical_lock(canonical_root, exclusive=False) as lease:
                loaded = load_canonical_delivery_view(
                    project_root,
                    target.tool,
                    config_path=config_path,
                    canonical_lease=lease,
                )
                if not loaded.success or loaded.view is None:
                    return _audit_failure(loaded.audit)
                view = _host_claude_view(config, target, loaded.view)
                if view is None:
                    return _failure("invalid-or-semantic")
                published = publish_workflow_view(
                    view,
                    canonical_root,
                    target.destination_root,
                    target.destination_root / RUNTIME_MANIFEST_NAME,
                    canonical_lease=lease,
                    source_root=canonical_root / loaded.audit.configured_source,
                    source_inputs=loaded.source_inputs,
                )
        except PublishError as error:
            return _canonical_lock_failure(canonical_root, error)
        except OSError:
            return _failure(DriftClass.INVALID_OR_SEMANTIC.value)
        if published.write_error is not None:
            return _publish_write_failure(target.destination_root, published.write_error)
        if not published.success:
            return _publish_failure(published)
    return WorkflowPreparationResult(True)


def _compose_claude_target(
    config: AppConfig, target: WorkflowDeliveryTarget, compose: bool
) -> bool:
    return (
        compose
        and target.tool == "claude"
        and target.destination_root == get_config_root(config) / "claude"
    )


def _prepare_target(target: WorkflowDeliveryTarget) -> WorkflowPreparationProblem | None:
    try:
        entry = target.destination_root.lstat()
    except FileNotFoundError:
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
    return WorkflowView(
        view.source,
        view.files,
        tuple(fragments),
        view.source_fingerprint,
        view.target_tool,
        view.native_only_paths,
        view.provisioning_placeholder_paths,
    )


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
