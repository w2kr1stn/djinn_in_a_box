"""Ordered runtime bootstrap and delivery of canonical agent workflow views."""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncSource
from djinn_in_a_box.core.config_delivery import (
    DeliveryProblem,
    DeliveryResult,
    DeliveryView,
    deliver_config_view,
)
from djinn_in_a_box.core.config_sync import (
    ConfigSyncAudit,
    DriftClass,
    audit_config_sync,
    load_canonical_delivery_view,
    sync_config,
)
from djinn_in_a_box.core.config_sync_adapters import SettingsFragment, is_safe_relative_path
from djinn_in_a_box.core.docker import ensure_host_env, get_config_root
from djinn_in_a_box.core.seeding import SeedingError, seed_config

_DELIVERY_PROBLEM_CODES = frozenset(
    {
        "carrier-malformed",
        "carrier-serialization-failed",
        "concurrent-change",
        "destination-root-race",
        "destination-root-unsafe",
        "duplicate-file-path",
        "duplicate-fragment",
        "fragment-parent-collision",
        "invalid-delivery-data",
        "invalid-fragment-value",
        "invalid-rendered-workflow",
        "managed-file-drift",
        "managed-fragment-drift",
        "manifest-malformed",
        "manifest-tool-mismatch",
        "manifest-unsafe",
        "publication-failed",
        "quarantine-preserved",
        "stage-create-failed",
        "stale-file-drift",
        "stale-fragment-drift",
        "unmanaged-file-collision",
        "unmanaged-fragment-collision",
        "unowned-file-path",
        "unowned-fragment",
        "unsafe-carrier-path",
        "unsafe-carrier-target",
        "unsafe-file-path",
        "unsafe-file-target",
        "unsafe-fragment-key",
        "unsafe-parent",
        "unsupported-tool",
    }
)
_DELIVERY_COLLISION_CODES = frozenset(
    {
        "fragment-parent-collision",
        "unmanaged-file-collision",
        "unmanaged-fragment-collision",
    }
)
_DELIVERY_DRIFT_CODES = frozenset(
    {
        "managed-file-drift",
        "managed-fragment-drift",
        "stale-file-drift",
        "stale-fragment-drift",
    }
)
_DELIVERY_RETRY_CODES = frozenset(
    {
        "concurrent-change",
        "destination-root-race",
        "publication-failed",
        "quarantine-preserved",
        "stage-create-failed",
    }
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


_CLAUDE_HOST_FRAGMENT_REWRITES: dict[tuple[str, ...], tuple[bytes, bytes]] = {
    ("hooks", "PreToolUse"): (
        _canonical_json(
            [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": ("uv run python3 ~/.claude_seed/security_reminder_hook.py"),
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
    """One explicit native runtime destination."""

    tool: ConfigSyncSource
    destination_root: Path
    provision: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowPreparationProblem:
    """Content-free bootstrap failure and operator remedy."""

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
) -> WorkflowPreparationResult:
    """Prepare canonical state, then deliver only the explicitly requested views."""
    try:
        current_config = load_config(config_path)
    except (OSError, TypeError, ValueError) as error:
        return _failure(
            "config-load-failed",
            f"Workflow configuration failed to load: {type(error).__name__}.",
            "Fix the Djinn configuration, then retry.",
        )
    config = config_snapshot if config_snapshot is not None else current_config
    if current_config != config:
        return _config_changed_failure()
    source = config.config_sync.source
    try:
        runtime_problem = _prepare_runtime_roots(
            config,
            targets,
            require_compose_host_env=require_compose_host_env,
        )
        if runtime_problem is not None:
            return WorkflowPreparationResult(False, (runtime_problem,))
        seed_config(project_root, source=source)
    except (OSError, SeedingError, TypeError, ValueError) as error:
        return _failure(
            "bootstrap-failed",
            f"Workflow bootstrap failed: {type(error).__name__}.",
            "Check project and config-root ownership, then retry.",
        )

    audit = audit_config_sync(project_root, config_path=config_path)
    if not audit.clean:
        if _deterministic_auto_syncable(audit):
            if not _config_matches(config_path, config):
                return _config_changed_failure()
            synced = sync_config(project_root, config_path=config_path, allow_agent=False)
            if not synced.success or not synced.audit.clean:
                return _audit_failure(synced.audit)
            audit = synced.audit
        else:
            return _audit_failure(audit)

    view_tools: tuple[ConfigSyncSource, ...] = tuple(
        dict.fromkeys(target.tool for target in targets)
    )
    if not view_tools:
        view_tools = (source,)
    views: dict[ConfigSyncSource, DeliveryView] = {}
    revision: str | None = None
    for tool in view_tools:
        loaded = load_canonical_delivery_view(project_root, tool, config_path=config_path)
        if not loaded.success or loaded.view is None or loaded.revision is None:
            return _audit_failure(loaded.audit)
        if revision is not None and loaded.revision != revision:
            return _canonical_changed_failure()
        revision = loaded.revision
        views[tool] = loaded.view

    for target in targets:
        if not _config_matches(config_path, config):
            return _config_changed_failure()
        destination_problem, destination_identity = _prepare_destination_with_identity(target)
        if destination_problem is not None:
            return WorkflowPreparationResult(False, (destination_problem,))
        if destination_identity is None:
            return _canonical_changed_failure()
        if not _config_matches(config_path, config):
            return _config_changed_failure()
        delivery_view = views[target.tool]
        if _needs_claude_host_bridge(config, target):
            bridged = _claude_host_delivery_view(delivery_view)
            if isinstance(bridged, WorkflowPreparationProblem):
                return WorkflowPreparationResult(False, (bridged,))
            delivery_view = bridged
        delivered = deliver_config_view(
            delivery_view,
            target.destination_root,
            expected_root_identity=destination_identity,
        )
        if not delivered.success:
            return _delivery_failure(target, delivered)

    try:
        if load_config(config_path) != config:
            return _config_changed_failure()
    except (OSError, TypeError, ValueError):
        return _config_changed_failure()
    final = load_canonical_delivery_view(project_root, view_tools[0], config_path=config_path)
    if not final.success or final.revision != revision:
        return _canonical_changed_failure()
    if not _config_matches(config_path, config):
        return _config_changed_failure()
    return WorkflowPreparationResult(True)


def _prepare_runtime_roots(
    config: AppConfig,
    targets: tuple[WorkflowDeliveryTarget, ...],
    *,
    require_compose_host_env: bool,
) -> WorkflowPreparationProblem | None:
    # Compose mounts the complete shared host environment even when only one
    # workflow is delivered. Running-container and host sessions do not.
    if require_compose_host_env:
        ensure_host_env(config)
        return None
    config_root = get_config_root(config)
    container_targets = tuple(
        target for target in targets if target.destination_root.parent == config_root
    )
    if len(container_targets) == 1:
        selected = container_targets[0]
        return _prepare_destination(
            WorkflowDeliveryTarget(selected.tool, selected.destination_root, provision=True)
        )
    return None


def _needs_claude_host_bridge(
    config: AppConfig,
    target: WorkflowDeliveryTarget,
) -> bool:
    if target.tool != "claude":
        return False
    container_root = Path(os.path.abspath(get_config_root(config) / "claude"))
    destination = Path(os.path.abspath(target.destination_root))
    return destination != container_root


def _claude_host_delivery_view(
    view: DeliveryView,
) -> DeliveryView | WorkflowPreparationProblem:
    if view.tool != "claude":
        return view
    fragments: list[SettingsFragment] = []
    for fragment in view.settings_fragments:
        replacement = _CLAUDE_HOST_FRAGMENT_REWRITES.get(fragment.key_path)
        if fragment.carrier_path.as_posix() == "settings.json" and replacement is not None:
            if fragment.value_json == replacement[0]:
                fragment = SettingsFragment(
                    fragment.carrier_path,
                    fragment.key_path,
                    replacement[1],
                    fragment.artifact_id,
                )
            elif fragment.value_json != replacement[1]:
                return WorkflowPreparationProblem(
                    "claude-host-hook-unsupported",
                    "Canonical Claude host hook configuration is unsupported.",
                    "Restore the managed Claude hook registration to its canonical seed or "
                    "host form, run `djinn config sync`, then retry.",
                )
        fragments.append(fragment)
    return DeliveryView(view.tool, view.files, tuple(fragments))


def _deterministic_auto_syncable(audit: ConfigSyncAudit) -> bool:
    return (
        bool(audit.drifts)
        and not audit.problems
        and all(item.kind is DriftClass.SOURCE_ONLY for item in audit.drifts)
    )


def _audit_failure(audit: ConfigSyncAudit) -> WorkflowPreparationResult:
    problem_identifiers = {problem.identifier for problem in audit.problems}
    if "quarantine-preserved" in problem_identifiers:
        return _failure(
            "quarantine-preserved",
            "Canonical workflow synchronization preserved recovery data.",
            "Inspect and preserve the .djinn-*-stage-* quarantine data; reconcile it before "
            "deleting anything or retrying.",
        )
    if "stage-create-failed" in problem_identifiers:
        return _failure(
            "stage-create-failed",
            "Canonical workflow synchronization could not create its private stage.",
            "Repair config stage-directory access, then retry.",
        )
    drift = next(
        (
            item
            for item in audit.drift_classes
            if item not in {DriftClass.CLEAN, DriftClass.SOURCE_ONLY}
        ),
        DriftClass.CLEAN,
    )
    identifier = "canonical-not-clean" if drift is DriftClass.CLEAN else drift.value
    remedy = "Run `djinn config status`, resolve the reported drift, then retry."
    return _failure(
        identifier,
        "Canonical workflow configuration is not ready for runtime delivery.",
        remedy,
    )


def _canonical_changed_failure() -> WorkflowPreparationResult:
    return _failure(
        "canonical-changed",
        "Canonical workflow configuration changed during delivery.",
        "Retry after configuration changes have settled.",
    )


def _config_changed_failure() -> WorkflowPreparationResult:
    return _failure(
        "config-changed",
        "Djinn configuration changed during workflow preparation.",
        "Retry after configuration changes have settled.",
    )


def _config_matches(config_path: Path | None, expected: AppConfig) -> bool:
    try:
        return load_config(config_path) == expected
    except (OSError, TypeError, ValueError):
        return False


def _prepare_destination(
    target: WorkflowDeliveryTarget,
) -> WorkflowPreparationProblem | None:
    problem, _identity = _prepare_destination_with_identity(target)
    return problem


def _prepare_destination_with_identity(
    target: WorkflowDeliveryTarget,
) -> tuple[WorkflowPreparationProblem | None, tuple[int, int] | None]:
    destination = Path(os.path.abspath(target.destination_root))
    try:
        destination_stat = _reopen_real_directory(destination)
    except FileNotFoundError:
        if not target.provision:
            return _destination_problem("destination-missing"), None
        try:
            destination_stat = _provision_real_directory(destination)
        except OSError:
            return _destination_problem("destination-provision-failed"), None
    except OSError:
        return _destination_problem("destination-unsafe"), None
    if not stat.S_ISDIR(destination_stat.st_mode):
        return _destination_problem("destination-unsafe"), None
    return None, (destination_stat.st_dev, destination_stat.st_ino)


def _provision_real_directory(path: Path) -> os.stat_result:
    """Create an absolute directory chain without following a path-component link."""
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            try:
                child_stat = os.fstat(child)
                entry_stat = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(entry_stat.st_mode)
                    or stat.S_ISLNK(entry_stat.st_mode)
                    or (entry_stat.st_dev, entry_stat.st_ino)
                    != (child_stat.st_dev, child_stat.st_ino)
                ):
                    raise OSError("directory component changed")
            except OSError:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child

        opened = os.fstat(descriptor)
        current = _reopen_real_directory(absolute)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("directory path changed")
        return current
    finally:
        os.close(descriptor)


def _reopen_real_directory(path: Path) -> os.stat_result:
    """Revalidate every component and return the final pinned directory state."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        result = os.fstat(descriptor)
        if not stat.S_ISDIR(result.st_mode):
            raise OSError("destination is not a directory")
        return result
    finally:
        os.close(descriptor)


def _destination_problem(identifier: str) -> WorkflowPreparationProblem:
    return WorkflowPreparationProblem(
        identifier,
        "Workflow destination is missing or unsafe.",
        "Create a real writable directory or remove the conflicting path, then retry.",
    )


def _delivery_failure(
    target: WorkflowDeliveryTarget,
    delivered: DeliveryResult,
) -> WorkflowPreparationResult:
    problem = delivered.problems[0] if delivered.problems else None
    code = _safe_delivery_code(problem)
    tool = target.tool
    relative_path = (
        problem.relative_path
        if problem is not None
        and problem.relative_path is not None
        and is_safe_relative_path(problem.relative_path)
        else None
    )
    location = tool
    if relative_path is not None:
        location = f"{tool}:{relative_path.as_posix()}"

    if code == "quarantine-preserved":
        remedy = (
            "Inspect and preserve the .djinn-*-stage-* quarantine data; reconcile it before "
            "deleting anything or retrying."
        )
    elif code == "stage-create-failed":
        remedy = "Repair the workflow destination's stage-directory access, then retry."
    elif code in _DELIVERY_COLLISION_CODES:
        remedy = (
            f"Move or reconcile the conflicting unmanaged workflow item at {location}, then retry."
        )
    elif code in _DELIVERY_DRIFT_CODES:
        remedy = f"Restore or move the modified managed workflow item at {location}, then retry."
    elif code in _DELIVERY_RETRY_CODES:
        remedy = f"Retry delivery to {location} after concurrent changes settle."
    elif code.startswith("manifest-"):
        remedy = f"Repair or remove the invalid {tool} delivery manifest, then retry."
    else:
        remedy = f"Resolve the {tool} workflow destination problem at {location}, then retry."

    return _failure(
        f"delivery-{code}",
        f"Canonical workflow delivery failed: {code} ({location}).",
        remedy,
    )


def _safe_delivery_code(problem: DeliveryProblem | None) -> str:
    if problem is not None and problem.identifier in _DELIVERY_PROBLEM_CODES:
        return problem.identifier
    return "delivery-failed"


def _failure(
    identifier: str,
    message: str,
    remedy: str,
) -> WorkflowPreparationResult:
    return WorkflowPreparationResult(
        False,
        (WorkflowPreparationProblem(identifier, message, remedy),),
    )
