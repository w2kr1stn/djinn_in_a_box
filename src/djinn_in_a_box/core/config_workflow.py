from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncSource
from djinn_in_a_box.core.config_sync import (
    CANONICAL_REMEDY,
    ConfigSyncAudit,
    DriftClass,
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
            else:
                problem = WorkflowPreparationProblem(
                    "image-incompatible",
                    "Workflow image is incompatible.",
                    "Rebuild/recreate required.",
                )
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
            if not retired.success:
                return _publish_failure(retired)
            continue
        if not _prepare_target(target):
            return _failure("invalid-or-semantic")
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
        except OSError:
            return _failure("invalid-or-semantic")
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


def _prepare_target(target: WorkflowDeliveryTarget) -> bool:
    try:
        if target.destination_root.exists():
            return target.destination_root.is_dir() and not target.destination_root.is_symlink()
        if not target.provision:
            return False
        target.destination_root.mkdir(parents=True)
        return True
    except OSError:
        return False


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
    drift = next(
        (item.kind for item in audit.drifts if item.kind is not DriftClass.CLEAN),
        DriftClass.INVALID_OR_SEMANTIC,
    )
    return _failure(drift.value)


def _publish_failure(result: PublishResult) -> WorkflowPreparationResult:
    return _failure(result.drift_class.value)


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
