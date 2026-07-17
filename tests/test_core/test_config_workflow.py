from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import NoReturn

import pytest

import djinn_in_a_box.core.config_workflow as workflow_module
from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig, ConfigSyncSource
from djinn_in_a_box.core.config_sync import (
    CANONICAL_REMEDY,
    CanonicalDeliveryViewResult,
    DriftClass,
)
from djinn_in_a_box.core.config_workflow import WorkflowDeliveryTarget, prepare_config_workflow
from djinn_in_a_box.core.docker import WorkflowImageCompatibility
from djinn_in_a_box.core.workflow_publisher import (
    RUNTIME_MANIFEST_NAME,
    CanonicalLockLease,
    CarrierFragment,
    WorkflowView,
)


def _workspace(tmp_path: Path, source: ConfigSyncSource = "claude") -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
    instruction = {"claude": "CLAUDE.md", "codex": "AGENTS.md", "opencode": "AGENTS.md"}
    (project / "config" / source / instruction[source]).write_text("shared workflow\n")
    config_path = tmp_path / "djinn.toml"
    runtime = tmp_path / "runtime"
    save_config(
        AppConfig(
            code_dir=tmp_path,
            config_root=runtime,
            config_sync=ConfigSyncConfig(source=source),
        ),
        config_path,
    )
    return project, config_path, runtime


def _legacy_manifest() -> bytes:
    return json.dumps(
        {"schema_version": 1, "tool": "claude", "files": {}, "fragments": []}
    ).encode()


def _ensure_host_env(_config: AppConfig) -> None:
    return None


def _audit_must_not_run(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("audit")


def test_host_runtime_publisher_syncs_selected_view_and_state_manifest(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    host_codex = tmp_path / "host-codex"

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("codex", host_codex, provision=True),),
        config_path=config_path,
    )

    assert result.success
    assert (host_codex / "AGENTS.md").read_text() == "shared workflow\n"
    assert (host_codex / RUNTIME_MANIFEST_NAME).is_file()


def test_preflight_reports_one_class_and_remedy_without_workflow_body(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    assert prepare_config_workflow(project, (target,), config_path=config_path).success
    sentinel = "PRIVATE-WORKFLOW-BODY"
    (project / "config/codex/AGENTS.md").write_text(sentinel)

    result = prepare_config_workflow(project, (target,), config_path=config_path)

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == DriftClass.TARGET_DRIFT.value
    assert problem.remedy
    assert sentinel not in repr(result)


def test_compose_claude_retires_legacy_without_publisher_and_codex_uses_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, runtime = _workspace(tmp_path)
    claude_root = runtime / "claude"
    codex_root = runtime / "codex"
    claude_root.mkdir(parents=True)
    codex_root.mkdir(parents=True)
    (claude_root / ".djinn-workflow-delivery.json").write_bytes(_legacy_manifest())
    monkeypatch.setattr(
        workflow_module,
        "workflow_image_compatible",
        lambda: WorkflowImageCompatibility.COMPATIBLE,
    )
    monkeypatch.setattr(workflow_module, "ensure_host_env", _ensure_host_env)

    result = prepare_config_workflow(
        project,
        (
            WorkflowDeliveryTarget("claude", claude_root),
            WorkflowDeliveryTarget("codex", codex_root),
        ),
        config_path=config_path,
        require_compose_host_env=True,
    )

    assert result.success
    assert not (claude_root / ".djinn-workflow-delivery.json").exists()
    assert not (claude_root / RUNTIME_MANIFEST_NAME).exists()
    assert (codex_root / RUNTIME_MANIFEST_NAME).is_file()


def test_compose_image_gate_blocks_before_audit_or_runtime_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, runtime = _workspace(tmp_path)
    sentinel = "PRIVATE-WORKFLOW-BODY"
    (project / "config/claude/CLAUDE.md").write_text(sentinel)
    monkeypatch.setattr(
        workflow_module,
        "workflow_image_compatible",
        lambda: WorkflowImageCompatibility.INCOMPATIBLE,
    )
    monkeypatch.setattr(
        workflow_module,
        "audit_config_sync",
        _audit_must_not_run,
    )

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("codex", runtime / "codex", provision=True),),
        config_path=config_path,
        require_compose_host_env=True,
    )

    assert not result.success
    assert result.problems[0].remedy == "Rebuild/recreate required."
    assert sentinel not in repr(result)
    assert not (runtime / "codex").exists()


def test_runtime_publish_rechecks_source_after_delivery_view_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    assert prepare_config_workflow(project, (target,), config_path=config_path).success
    destination = target.destination_root
    before_agents = (destination / "AGENTS.md").read_bytes()
    before_manifest = (destination / RUNTIME_MANIFEST_NAME).read_bytes()
    original = workflow_module.load_canonical_delivery_view

    def edit_source_after_view_load(
        project_root: Path,
        tool: ConfigSyncSource,
        *,
        config_path: Path | None = None,
        canonical_lease: CanonicalLockLease | None = None,
    ) -> CanonicalDeliveryViewResult:
        loaded = original(
            project_root,
            tool,
            config_path=config_path,
            canonical_lease=canonical_lease,
        )
        (project / "config/claude/CLAUDE.md").write_text("operator edit\n")
        return loaded

    monkeypatch.setattr(
        workflow_module, "load_canonical_delivery_view", edit_source_after_view_load
    )
    result = prepare_config_workflow(project, (target,), config_path=config_path)

    assert not result.success
    assert result.problems[0].identifier == DriftClass.SOURCE_CHANGED.value
    assert (destination / "AGENTS.md").read_bytes() == before_agents
    assert (destination / RUNTIME_MANIFEST_NAME).read_bytes() == before_manifest


def test_host_claude_profile_rewrites_only_managed_hook_paths(tmp_path: Path) -> None:
    _project, config_path, _runtime = _workspace(tmp_path)
    config = workflow_module.load_config(config_path)
    seed_value = json.dumps(
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
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    view = WorkflowView(
        "claude",
        (),
        (
            CarrierFragment(PurePosixPath("settings.json"), ("hooks", "Stop"), seed_value),
            CarrierFragment(
                PurePosixPath("settings.json"),
                ("operator", "keep"),
                b'"~/.claude_seed/keep"',
            ),
        ),
    )

    bridged = workflow_module._host_claude_view(  # pyright: ignore[reportPrivateUsage]
        config,
        WorkflowDeliveryTarget("claude", tmp_path / "host-claude"),
        view,
    )

    assert bridged is not None
    assert b"~/.claude/ready_notify_hook.py" in bridged.fragments[0].value_json
    assert bridged.fragments[1] == view.fragments[1]
    assert CANONICAL_REMEDY == (
        "Author or edit the artifact natively in the target tool's view, "
        "or make the source form portable."
    )
