from __future__ import annotations

import subprocess
from pathlib import PurePosixPath
from unittest.mock import Mock

import pytest
import test_workflow_host_integration as host_tests
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import config as config_command
from djinn_in_a_box.commands import doctor
from djinn_in_a_box.core import config_sync, session
from djinn_in_a_box.core import config_workflow as workflow
from djinn_in_a_box.core.docker import WorkflowImageCompatibility
from djinn_in_a_box.core.workflow_publisher import (
    CANONICAL_MANIFEST_NAME,
    CarrierFragment,
    DriftClass,
    WorkflowView,
    canonical_lock,
)

case = host_tests.case
prepare = host_tests.prepare


@pytest.mark.parametrize("running", [False, True])
def test_doctor_reads_shared_identity_at_each_call(case, monkeypatch, running):
    gate = Mock(return_value=WorkflowImageCompatibility.COMPATIBLE)
    monkeypatch.setattr(session, "container_workflow_image_compatible", gate)
    for identity in ("first-runtime", "second-runtime"):
        monkeypatch.setattr(session, "_DJINN_CONTAINER_NAME", identity)
        discovery = subprocess.CompletedProcess(
            [],
            0 if running else 1,
            "true\n" if running else "",
            "" if running else f"Error response from daemon: No such container: {identity}\n",
        )
        run = Mock(side_effect=[discovery, subprocess.CompletedProcess([], 0, "", "")])
        monkeypatch.setattr(doctor.subprocess, "run", run)
        gate.reset_mock()
        row = doctor._opencode_runtime_check(case[2], daemon=True)
        assert row.status is doctor.Status.PASS
        assert run.call_args_list[0].args == (
            [
                session.DOCKER_EXECUTABLE,
                "container",
                "inspect",
                identity,
                "--format",
                "{{json .State.Running}}",
            ],
        )
        assert all(
            call.kwargs["stdin"] == subprocess.DEVNULL and call.kwargs["timeout"] == 10
            for call in run.call_args_list
        )
        if running:
            target = session.SessionTarget(container_id=identity)
            gate.assert_called_once_with(identity)
            assert run.call_args_list[1].args == (
                session.opencode_publisher_argv(target, check=True),
            )
        else:
            gate.assert_not_called()
            assert run.call_count == 1
            assert row.detail == "container-local; rebuilt at every djinn start"


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: No such object: djinn\n",
        "error: no such object: djinn\n",
        "Error response from daemon: No such container: another-runtime\n",
    ],
)
def test_doctor_rejects_other_absence_diagnostics(case, monkeypatch, stderr):
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", stderr))
    monkeypatch.setattr(doctor.subprocess, "run", run)
    row = doctor._opencode_runtime_check(case[2], daemon=True)
    assert (row.status, row.detail) == (doctor.Status.WARN, "check unavailable")
    assert run.call_args.args[0][:4] == [
        session.DOCKER_EXECUTABLE,
        "container",
        "inspect",
        "djinn",
    ]


@pytest.mark.parametrize(
    "kind",
    [
        DriftClass.SOURCE_CHANGED,
        DriftClass.TARGET_DRIFT,
        DriftClass.COLLISION,
        DriftClass.INVALID_OR_SEMANTIC,
    ],
)
def test_itemless_failure_has_no_location(kind):
    result = workflow._failure(kind.value)
    assert result.problems[0].message == f"Workflow preflight blocked: {kind.value}."


def test_explicit_failure_location_is_retained():
    result = workflow._failure(
        DriftClass.TARGET_DRIFT.value,
        labels=("config/codex/AGENTS.md",),
    )
    assert result.problems[0].message == (
        "Workflow preflight blocked: target-drift in config/codex/AGENTS.md."
    )


def test_app_config_load_failure_has_no_invented_item(case, monkeypatch):
    monkeypatch.setattr(workflow, "load_config", Mock(side_effect=ValueError("invalid config")))
    result = workflow.prepare_config_workflow(case[0], config_path=case[1])
    assert result.problems[0].message == "Workflow preflight blocked: invalid-or-semantic."


def test_host_hook_rewrite_mismatch_has_no_invented_item(case, monkeypatch):
    assert prepare(case).success
    audit = config_sync.ConfigSyncAudit("claude", "claude")
    view = WorkflowView(
        "claude",
        files=(),
        fragments=(
            CarrierFragment(PurePosixPath("settings.json"), ("hooks", "PreToolUse"), b"[]"),
        ),
        target_tool="claude",
    )
    monkeypatch.setattr(
        workflow,
        "load_canonical_delivery_view",
        lambda *args, **kwargs: config_sync.CanonicalDeliveryViewResult(True, audit, view),
    )
    target = workflow.WorkflowDeliveryTarget("claude", case[2].code_dir / "host-claude")
    with canonical_lock(case[0] / "config", exclusive=False) as lease:
        result = workflow._load_delivery(case[2], case[0], target, case[1], lease)
    assert isinstance(result, workflow.WorkflowPreparationResult)
    assert result.problems[0].message == "Workflow preflight blocked: invalid-or-semantic."


def test_delivery_oserror_has_no_invented_item(case, monkeypatch):
    monkeypatch.setattr(workflow, "_load_delivery", Mock(side_effect=OSError("read failed")))
    result = prepare(case)
    assert result.problems[0].message == "Workflow preflight blocked: invalid-or-semantic."


@pytest.mark.parametrize("kind", [DriftClass.INVALID_OR_SEMANTIC, DriftClass.SOURCE_CHANGED])
def test_generic_audit_reports_tree_and_preflight_has_no_item(case, monkeypatch, kind):
    monkeypatch.setattr(
        config_sync, "_snapshot_build", Mock(side_effect=config_sync._BuildError(kind))
    )
    audit = config_sync.audit_config_sync(case[0], config_path=case[1])
    assert audit.drift_classes == (kind,)
    assert audit.drifts[0].relative_path is None
    remedy = Mock(wraps=workflow.workflow_remedy)
    monkeypatch.setattr(workflow, "workflow_remedy", remedy)
    assert workflow._audit_failure(audit).problems[0].message == (
        f"Workflow preflight blocked: {kind.value}."
    )
    assert remedy.call_args.kwargs["items"] == ()
    monkeypatch.setattr(config_command, "inspect_workflow_targets", lambda root, config: ())
    status = CliRunner().invoke(app, ["config", "status"])
    assert status.exit_code == 1
    assert [line for line in status.output.splitlines() if line.startswith("Drift:")] == [
        f"Drift: {kind.value} (config)",
    ]
    monkeypatch.setattr(doctor, "get_project_root", lambda: case[0])
    monkeypatch.setattr(doctor, "audit_workflow_config", lambda root: audit)
    monkeypatch.setattr(doctor, "inspect_workflow_targets", lambda root, config: ())
    row = doctor._config_workflow_check(case[2])
    assert row.detail == f"source=claude; drift={kind.value} (config)"


@pytest.mark.parametrize("tool", [None, "claude"])
def test_pathless_label_is_the_canonical_tree(tool):
    assert config_sync.workflow_item_label(tool, None) == "config"


@pytest.mark.parametrize("problem", ["malformed", "symlink", "read-error"])
def test_confirmed_manifest_problem_keeps_manifest_location(case, monkeypatch, problem):
    assert prepare(case).success
    manifest = case[0] / "config" / CANONICAL_MANIFEST_NAME
    if problem == "malformed":
        manifest.write_text("{")
    elif problem == "symlink":
        saved = case[2].code_dir / "saved-manifest"
        manifest.rename(saved)
        manifest.symlink_to(saved)
    else:
        monkeypatch.setattr(config_sync, "_read_manifest", Mock(side_effect=OSError("read failed")))
    audit = config_sync.audit_config_sync(case[0], config_path=case[1])
    assert audit.drifts[0].relative_path == PurePosixPath(CANONICAL_MANIFEST_NAME)
    refusal = workflow._audit_failure(audit)
    assert refusal.problems[0].message == (
        "Workflow preflight blocked: invalid-or-semantic in config/.djinn-config-sync.json."
    )
    monkeypatch.setattr(config_command, "inspect_workflow_targets", lambda root, config: ())
    result = CliRunner().invoke(app, ["config", "status"])
    assert result.exit_code == 1
    assert [line for line in result.output.splitlines() if line.startswith("Drift:")] == [
        "Drift: invalid-or-semantic (config/.djinn-config-sync.json)",
    ]
