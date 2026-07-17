from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

import djinn_in_a_box.core.config_workflow as workflow_module
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig, ConfigSyncSource
from djinn_in_a_box.core.config_delivery import (
    DELIVERY_MANIFEST_NAME,
    DeliveryProblem,
    DeliveryResult,
    DeliveryView,
)
from djinn_in_a_box.core.config_sync import (
    MANIFEST_NAME,
    CanonicalDeliveryViewResult,
    ConfigSyncAudit,
    ConfigSyncResult,
    DriftClass,
    DriftItem,
    SyncProblem,
)
from djinn_in_a_box.core.config_sync_adapters import RenderedFile, SettingsFragment
from djinn_in_a_box.core.config_workflow import (
    WorkflowDeliveryTarget,
    prepare_config_workflow,
)


def _returns[T](value: T) -> Callable[..., T]:
    def _stub(*_args: object, **_kwargs: object) -> T:
        return value

    return _stub


def _calls[T](factory: Callable[[], T]) -> Callable[..., T]:
    def _stub(*_args: object, **_kwargs: object) -> T:
        return factory()

    return _stub


def _fails(message: str) -> Callable[..., object]:
    def _stub(*_args: object, **_kwargs: object) -> object:
        pytest.fail(message)

    return _stub


def _captures_view(delivered: list[DeliveryView]) -> Callable[..., DeliveryResult]:
    def _stub(delivery_view: DeliveryView, *_args: object, **_kwargs: object) -> DeliveryResult:
        delivered.append(delivery_view)
        return DeliveryResult(True)

    return _stub


def _workspace(tmp_path: Path, *, source: ConfigSyncSource = "claude") -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
    instruction = {"claude": "CLAUDE.md", "codex": "AGENTS.md", "opencode": "AGENTS.md"}
    (project / "config" / source / instruction[source]).write_text("Shared workflow.\n")
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    config_root = tmp_path / "runtime"
    config_path = tmp_path / "config.toml"
    save_config(
        AppConfig(
            code_dir=code_dir,
            config_root=config_root,
            config_sync=ConfigSyncConfig(source=source),
        ),
        config_path,
    )
    return project, config_path, config_root


@pytest.fixture(autouse=True)
def _no_repository_seed(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow_module, "seed_config", _returns(list[Path]()))


def _claude_hook_view() -> tuple[DeliveryView, tuple[bytes, bytes, bytes]]:
    security = (
        b'[{"hooks":[{"command":"uv run python3 '
        b'~/.claude_seed/security_reminder_hook.py","type":"command"}],'
        b'"matcher":"Edit|Write"}]'
    )
    ready = (
        b'[{"hooks":[{"command":"uv run python3 '
        b'~/.claude_seed/ready_notify_hook.py","type":"command"}],"matcher":""}]'
    )
    unknown = b'["~/.claude_seed/operator-owned-neighbor"]'
    return (
        DeliveryView(
            "claude",
            (),
            (
                SettingsFragment(
                    PurePosixPath("settings.json"),
                    ("hooks", "PreToolUse"),
                    security,
                    "security-reminder-hook",
                ),
                SettingsFragment(
                    PurePosixPath("settings.json"),
                    ("hooks", "Stop"),
                    ready,
                    "ready-notify-hook",
                ),
                SettingsFragment(
                    PurePosixPath("settings.json"),
                    ("operator", "neighbor"),
                    unknown,
                    "operator-neighbor",
                ),
            ),
        ),
        (security, ready, unknown),
    )


def test_source_only_state_auto_syncs_and_delivers_selected_runtime(
    tmp_path: Path,
) -> None:
    project, config_path, config_root = _workspace(tmp_path)

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", config_root / "claude"),),
        config_path=config_path,
    )

    assert result.success is True
    assert (project / "config" / MANIFEST_NAME).is_file()
    assert (project / "config/opencode/AGENTS.md").read_text() == "Shared workflow.\n"
    assert (config_root / "claude/CLAUDE.md").read_text() == "Shared workflow.\n"
    assert (config_root / "claude/AGENTS.md").read_text() == "Shared workflow.\n"
    assert (config_root / "claude" / DELIVERY_MANIFEST_NAME).is_file()


def test_no_delivery_target_still_canonicalizes_opencode_projection(tmp_path: Path) -> None:
    project, config_path, _config_root = _workspace(tmp_path)

    result = prepare_config_workflow(project, config_path=config_path)

    assert result.success is True
    assert (project / "config/opencode/AGENTS.md").read_text() == "Shared workflow.\n"
    assert (project / "config/opencode/CLAUDE.md").read_text() == "Shared workflow.\n"


def test_unselected_runtime_collision_does_not_block_selected_delivery(
    tmp_path: Path,
) -> None:
    project, config_path, config_root = _workspace(tmp_path)
    config_root.mkdir()
    (config_root / "opencode").write_text("unrelated collision")

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", config_root / "claude"),),
        config_path=config_path,
    )

    assert result.success is True
    assert (config_root / "opencode").read_text() == "unrelated collision"
    assert (config_root / "claude/CLAUDE.md").is_file()


def test_runtime_collision_reports_safe_code_tool_and_relative_path(tmp_path: Path) -> None:
    project, config_path, config_root = _workspace(tmp_path)
    destination = config_root / "claude"
    destination.mkdir(parents=True)
    (destination / "CLAUDE.md").write_text("unmanaged runtime content\n")

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", destination),),
        config_path=config_path,
    )

    assert result.success is False
    problem = result.problems[0]
    assert problem.identifier == "delivery-unmanaged-file-collision"
    assert problem.message == (
        "Canonical workflow delivery failed: unmanaged-file-collision (claude:CLAUDE.md)."
    )
    assert "claude:CLAUDE.md" in problem.remedy
    assert "unmanaged runtime content" not in repr(problem)


def test_runtime_delivery_redacts_unknown_code_and_unsafe_path() -> None:
    sentinel = "PRIVATE-DELIVERY-SENTINEL"
    delivered = DeliveryResult(
        False,
        problems=(
            DeliveryProblem(
                f"unknown-{sentinel}",
                "claude",
                PurePosixPath("..") / sentinel,
            ),
        ),
    )

    result = workflow_module._delivery_failure(  # pyright: ignore[reportPrivateUsage]
        WorkflowDeliveryTarget("claude", Path("/unused")),
        delivered,
    )

    assert result.problems[0].identifier == "delivery-delivery-failed"
    assert result.problems[0].message == (
        "Canonical workflow delivery failed: delivery-failed (claude)."
    )
    assert sentinel not in repr(result)


def test_host_claude_delivery_rewrites_only_owned_seed_hook_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _config_root = _workspace(tmp_path)
    destination = tmp_path / "host-claude"
    destination.mkdir()
    audit = ConfigSyncAudit("claude", "claude")
    view, (_security, _ready, unknown) = _claude_hook_view()
    delivered: list[DeliveryView] = []
    monkeypatch.setattr(workflow_module, "audit_config_sync", _returns(audit))
    monkeypatch.setattr(
        workflow_module,
        "load_canonical_delivery_view",
        _returns(CanonicalDeliveryViewResult(True, audit, view, "revision")),
    )
    monkeypatch.setattr(
        workflow_module,
        "deliver_config_view",
        _captures_view(delivered),
    )

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", destination),),
        config_path=config_path,
    )

    assert result.success is True
    values = tuple(fragment.value_json for fragment in delivered[0].settings_fragments)
    assert b"~/.claude/security_reminder_hook.py" in values[0]
    assert b"~/.claude/ready_notify_hook.py" in values[1]
    assert values[2] == unknown


def test_host_claude_delivery_accepts_exact_host_hook_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _config_root = _workspace(tmp_path)
    destination = tmp_path / "host-claude"
    destination.mkdir()
    audit = ConfigSyncAudit("claude", "claude")
    source_view, source_values = _claude_hook_view()
    host_values = (
        source_values[0].replace(b"~/.claude_seed/", b"~/.claude/"),
        source_values[1].replace(b"~/.claude_seed/", b"~/.claude/"),
        source_values[2],
    )
    view = DeliveryView(
        "claude",
        (),
        tuple(
            SettingsFragment(
                fragment.carrier_path,
                fragment.key_path,
                value,
                fragment.artifact_id,
            )
            for fragment, value in zip(source_view.settings_fragments, host_values, strict=True)
        ),
    )
    delivered: list[DeliveryView] = []
    monkeypatch.setattr(workflow_module, "audit_config_sync", _returns(audit))
    monkeypatch.setattr(
        workflow_module,
        "load_canonical_delivery_view",
        _returns(CanonicalDeliveryViewResult(True, audit, view, "revision")),
    )
    monkeypatch.setattr(
        workflow_module,
        "deliver_config_view",
        _captures_view(delivered),
    )

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", destination),),
        config_path=config_path,
    )

    assert result.success is True
    values = tuple(fragment.value_json for fragment in delivered[0].settings_fragments)
    assert values == host_values


def test_host_claude_delivery_blocks_noncanonical_owned_hook_before_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _config_root = _workspace(tmp_path)
    destination = tmp_path / "host-claude"
    destination.mkdir()
    sentinel = b'[{"PRIVATE-HOOK-SENTINEL":true}]'
    audit = ConfigSyncAudit("claude", "claude")
    view = DeliveryView(
        "claude",
        (),
        (
            SettingsFragment(
                PurePosixPath("settings.json"),
                ("hooks", "PreToolUse"),
                sentinel,
                "security-reminder-hook",
            ),
        ),
    )
    monkeypatch.setattr(workflow_module, "audit_config_sync", _returns(audit))
    monkeypatch.setattr(
        workflow_module,
        "load_canonical_delivery_view",
        _returns(CanonicalDeliveryViewResult(True, audit, view, "revision")),
    )
    monkeypatch.setattr(
        workflow_module,
        "deliver_config_view",
        _fails("delivery must not run"),
    )

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", destination),),
        config_path=config_path,
    )

    assert result.success is False
    assert result.problems[0].identifier == "claude-host-hook-unsupported"
    assert "djinn config sync" in result.problems[0].remedy
    assert "PRIVATE-HOOK-SENTINEL" not in repr(result)


def test_container_claude_delivery_keeps_seed_hook_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, config_root = _workspace(tmp_path)
    destination = config_root / "claude"
    destination.mkdir(parents=True)
    audit = ConfigSyncAudit("claude", "claude")
    view, source_values = _claude_hook_view()
    delivered: list[DeliveryView] = []
    monkeypatch.setattr(workflow_module, "audit_config_sync", _returns(audit))
    monkeypatch.setattr(
        workflow_module,
        "load_canonical_delivery_view",
        _returns(CanonicalDeliveryViewResult(True, audit, view, "revision")),
    )
    monkeypatch.setattr(
        workflow_module,
        "deliver_config_view",
        _captures_view(delivered),
    )

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", destination),),
        config_path=config_path,
    )

    assert result.success is True
    values = tuple(fragment.value_json for fragment in delivered[0].settings_fragments)
    assert values == source_values


def test_start_style_delivery_checks_complete_host_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, config_root = _workspace(tmp_path)
    sentinel = "PRIVATE-CREDENTIAL-CONTENT"

    def fail_host_environment(_config: AppConfig) -> None:
        raise OSError(sentinel)

    monkeypatch.setattr(workflow_module, "ensure_host_env", fail_host_environment)

    result = prepare_config_workflow(
        project,
        (
            WorkflowDeliveryTarget("claude", config_root / "claude"),
            WorkflowDeliveryTarget("codex", config_root / "codex"),
        ),
        config_path=config_path,
        require_compose_host_env=True,
    )

    assert result.success is False
    assert result.problems[0].identifier == "bootstrap-failed"
    assert sentinel not in repr(result)
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_managed_target_drift_blocks_without_implicit_repair(tmp_path: Path) -> None:
    project, config_path, config_root = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("claude", config_root / "claude")
    assert prepare_config_workflow(project, (target,), config_path=config_path).success
    drifted = project / "config/opencode/AGENTS.md"
    drifted.write_text("operator drift\n")

    result = prepare_config_workflow(project, (target,), config_path=config_path)

    assert result.success is False
    assert result.problems[0].identifier == DriftClass.TARGET_DRIFT.value
    assert drifted.read_text() == "operator drift\n"


def test_semantic_gap_blocks_without_agent_or_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, config_root = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("claude", config_root / "claude")
    assert prepare_config_workflow(project, (target,), config_path=config_path).success
    skill = project / "config/claude/skills/convergence-loop/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: convergence-loop\ndescription: Converge reviews\n---\n\nSemantic variant.\n"
    )
    delivery_called = False

    def forbidden_delivery(*_args: object, **_kwargs: object) -> object:
        nonlocal delivery_called
        delivery_called = True
        raise AssertionError("delivery must not run")

    monkeypatch.setattr(workflow_module, "deliver_config_view", forbidden_delivery)

    result = prepare_config_workflow(project, (target,), config_path=config_path)

    assert result.success is False
    assert result.problems[0].identifier == DriftClass.INVALID_OR_SEMANTIC.value
    assert delivery_called is False


def test_symlink_destination_is_rejected_without_mutating_target(tmp_path: Path) -> None:
    project, config_path, _config_root = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "host-claude"
    destination.symlink_to(outside, target_is_directory=True)

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", destination, provision=True),),
        config_path=config_path,
    )

    assert result.success is False
    assert result.problems[0].identifier == "destination-unsafe"
    assert list(outside.iterdir()) == []


def test_config_load_failure_returns_sanitized_result(tmp_path: Path) -> None:
    sentinel = "PRIVATE-CONFIG-PATH-SENTINEL"
    missing = tmp_path / sentinel / "config.toml"

    result = prepare_config_workflow(tmp_path / "project", config_path=missing)

    assert result.success is False
    assert result.problems[0].identifier == "config-load-failed"
    assert sentinel not in repr(result)


def test_quarantine_preserved_delivery_has_non_destructive_operator_remedy(tmp_path: Path) -> None:
    target = WorkflowDeliveryTarget("codex", tmp_path)
    delivered = DeliveryResult(
        False,
        problems=(DeliveryProblem("quarantine-preserved", "codex"),),
        retryable=True,
    )

    result = workflow_module._delivery_failure(target, delivered)  # pyright: ignore[reportPrivateUsage]

    assert result.problems[0].identifier == "delivery-quarantine-preserved"
    assert ".djinn-*-stage-*" in result.problems[0].remedy
    assert "before deleting anything or retrying" in result.problems[0].remedy


@pytest.mark.parametrize(
    ("identifier", "remedy_marker"),
    [
        ("quarantine-preserved", "before deleting anything or retrying"),
        ("stage-create-failed", "Repair config stage-directory access"),
    ],
)
def test_canonical_sync_problem_survives_implicit_bootstrap_without_private_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identifier: str,
    remedy_marker: str,
) -> None:
    project, config_path, _config_root = _workspace(tmp_path)
    sentinel = "PRIVATE-WORKFLOW-BODY-SENTINEL"
    pre_sync = ConfigSyncAudit(
        "claude",
        None,
        (DriftItem(DriftClass.SOURCE_CHANGED, "Synchronization manifest is missing."),),
    )
    failed = ConfigSyncAudit(
        "claude",
        None,
        problems=(
            SyncProblem(
                identifier,
                sentinel,
                "codex",
                PurePosixPath(f"private/{sentinel}"),
            ),
        ),
    )
    monkeypatch.setattr(
        workflow_module,
        "audit_config_sync",
        _returns(pre_sync),
    )
    monkeypatch.setattr(
        workflow_module,
        "sync_config",
        _returns(ConfigSyncResult(False, failed, retryable=True)),
    )

    result = prepare_config_workflow(project, config_path=config_path)

    assert not result.success
    assert result.problems[0].identifier == identifier
    assert remedy_marker in result.problems[0].remedy
    assert sentinel not in repr(result)


def test_canonical_revision_change_after_delivery_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _config_root = _workspace(tmp_path)
    destination = tmp_path / "host-claude"
    destination.mkdir()
    audit = ConfigSyncAudit("claude", "claude")
    view = DeliveryView(
        "claude",
        (
            RenderedFile(
                PurePosixPath("CLAUDE.md"),
                b"Shared workflow.\n",
                "instructions",
            ),
            RenderedFile(
                PurePosixPath("AGENTS.md"),
                b"Shared workflow.\n",
                "instructions",
            ),
        ),
        (),
    )
    revisions = iter(("revision-one", "revision-two"))
    monkeypatch.setattr(workflow_module, "audit_config_sync", _returns(audit))
    monkeypatch.setattr(
        workflow_module,
        "load_canonical_delivery_view",
        _calls(lambda: CanonicalDeliveryViewResult(True, audit, view, next(revisions))),
    )
    monkeypatch.setattr(
        workflow_module,
        "deliver_config_view",
        _returns(DeliveryResult(True)),
    )

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", destination),),
        config_path=config_path,
    )

    assert result.success is False
    assert result.problems[0].identifier == "canonical-changed"


def test_provisioning_parent_symlink_swap_cannot_escape_destination_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "provision-parent"
    parent.mkdir()
    parked_parent = tmp_path / "parked-parent"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("unchanged")
    destination = parent / "claude"
    original_open = workflow_module.os.open
    swapped = False

    def swap_before_parent_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == parent.name and dir_fd is not None and not swapped:
            parent.rename(parked_parent)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workflow_module.os, "open", swap_before_parent_open)

    problem = workflow_module._prepare_destination(  # pyright: ignore[reportPrivateUsage]
        WorkflowDeliveryTarget("claude", destination, provision=True)
    )

    assert swapped is True
    assert problem is not None
    assert problem.identifier == "destination-unsafe"
    assert marker.read_text() == "unchanged"
    assert not (outside / "claude").exists()
    assert list(parked_parent.iterdir()) == []


def test_existing_destination_below_symlink_parent_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "claude").mkdir(parents=True)
    marker = outside / "claude/keep.txt"
    marker.write_text("unchanged")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    problem = workflow_module._prepare_destination(  # pyright: ignore[reportPrivateUsage]
        WorkflowDeliveryTarget("claude", linked_parent / "claude", provision=True)
    )

    assert problem is not None
    assert problem.identifier == "destination-unsafe"
    assert marker.read_text() == "unchanged"
    assert not (outside / "claude/.djinn-workflow-delivery.json").exists()


def test_caller_config_snapshot_switch_blocks_before_bootstrap_or_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, old_root = _workspace(tmp_path)
    snapshot = load_config(config_path)
    new_root = tmp_path / "new-runtime"
    save_config(
        AppConfig(
            code_dir=snapshot.code_dir,
            config_root=new_root,
            config_sync=ConfigSyncConfig(source="codex"),
        ),
        config_path,
    )
    monkeypatch.setattr(
        workflow_module,
        "ensure_host_env",
        _fails("bootstrap must not run"),
    )
    monkeypatch.setattr(
        workflow_module,
        "deliver_config_view",
        _fails("delivery must not run"),
    )

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", old_root / "claude"),),
        config_path=config_path,
        config_snapshot=snapshot,
        require_compose_host_env=True,
    )

    assert result.success is False
    assert result.problems[0].identifier == "config-changed"
    assert not old_root.exists()
    assert not new_root.exists()
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_parent_swap_after_prepare_cannot_redirect_delivery_outside(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "delivery-parent"
    destination = parent / "claude"
    destination.mkdir(parents=True)
    target = WorkflowDeliveryTarget("claude", destination)
    problem, identity = workflow_module._prepare_destination_with_identity(  # pyright: ignore[reportPrivateUsage]
        target
    )
    assert problem is None
    assert identity is not None

    parked_parent = tmp_path / "parked-delivery-parent"
    parent.rename(parked_parent)
    outside = tmp_path / "outside-delivery"
    (outside / "claude").mkdir(parents=True)
    marker = outside / "claude/keep.txt"
    marker.write_text("unchanged")
    parent.symlink_to(outside, target_is_directory=True)
    view = DeliveryView(
        "claude",
        (
            RenderedFile(PurePosixPath("CLAUDE.md"), b"instructions\n", "instructions"),
            RenderedFile(PurePosixPath("AGENTS.md"), b"instructions\n", "instructions"),
        ),
        (),
    )

    result = workflow_module.deliver_config_view(view, destination, expected_root_identity=identity)

    assert result.success is False
    assert result.retryable is True
    assert result.problems[0].identifier == "destination-root-race"
    assert marker.read_text() == "unchanged"
    assert not (outside / "claude/CLAUDE.md").exists()
    assert not (outside / "claude" / DELIVERY_MANIFEST_NAME).exists()
    assert list((parked_parent / "claude").iterdir()) == []
