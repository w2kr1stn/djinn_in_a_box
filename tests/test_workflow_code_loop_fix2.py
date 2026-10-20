"""Iteration-2 regressions: diagnostic safety, operational causes and discard edges."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_workflow_discard as discard_tests
from test_workflow_code_loop_fix1 import doctor_row, run_publisher
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import _lifecycle, doctor
from djinn_in_a_box.commands import session as session_command
from djinn_in_a_box.core import config_sync as sync
from djinn_in_a_box.core import config_workflow as workflow
from djinn_in_a_box.core import session
from djinn_in_a_box.core import workflow_publisher as publisher
from djinn_in_a_box.core.console import escape_nonprintable
from djinn_in_a_box.core.docker import WorkflowImageCompatibility
from djinn_in_a_box.core.workflow_publisher import (
    PUBLISHER_LOCK_ERROR_PREFIX,
    PUBLISHER_WRITE_ERROR_PREFIX,
    RUNTIME_MANIFEST_NAME,
)

discard_case = discard_tests.discard_case


def refresh(case, monkeypatch, stderr):
    monkeypatch.setattr(session, "load_agents", lambda: {})
    monkeypatch.setattr(session, "load_config", lambda: case.config)
    manager = session.SessionManager("new")
    monkeypatch.setattr(manager, "workflow_image_compatible",
                        lambda target: WorkflowImageCompatibility.COMPATIBLE)
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 13, "", stderr
    ))
    return manager, manager.refresh_opencode_workflow(session.SessionTarget(container_id="fixture"))


@pytest.mark.parametrize("prefix,field", [
    (PUBLISHER_LOCK_ERROR_PREFIX, "root"),
    (PUBLISHER_LOCK_ERROR_PREFIX, "error"),
    (PUBLISHER_WRITE_ERROR_PREFIX, "destination"),
    (PUBLISHER_WRITE_ERROR_PREFIX, "error"),
])
def test_operational_fields_are_literal_through_refresh_and_cli(
    discard_case, monkeypatch, prefix, field
):
    case = discard_case
    payload = {"root": "/canonical", "destination": "/runtime", "error": "denied"}
    payload[field] += " [/error]\x1b]0;forged\x07\nError: forged"
    stderr = prefix + json.dumps(payload) + "\n"
    manager, refreshed = refresh(case, monkeypatch, stderr)
    operational = session.publisher_operational_failure(stderr)
    assert operational is not None
    for text in (refreshed.stderr, operational[0]):
        assert "\x1b" not in text and "\x07" not in text and "\nError: forged" not in text
    assert "[/error]\\x1b]0;forged\\x07\\nError: forged" in refreshed.stderr

    monkeypatch.setattr(session_command, "SessionManager", lambda *a, **k: manager)
    monkeypatch.setattr(manager, "resolve_target",
                        lambda: session.SessionTarget(container_id="fixture"))
    monkeypatch.setattr(session_command, "load_config", lambda: case.config)
    monkeypatch.setattr(session_command, "get_project_root", lambda: case.project)
    monkeypatch.setattr(session_command, "prepare_config_workflow",
                        lambda *a, **k: workflow.WorkflowPreparationResult(True))
    result = CliRunner().invoke(app, ["session", "--project", "new", "--agent", "opencode",
                                      "--create"])
    assert result.exit_code == 1, (result.output, result.exception)
    assert "\x1b" not in result.output and "\nError: forged" not in result.output
    assert "[/error]\\x1b]0;forged\\x07\\nError: forged" in result.output
    assert not (case.home / ".djinn/sessions/new").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
@pytest.mark.parametrize("check", [True, False])
@pytest.mark.parametrize("failed_root", ["target", "canonical"])
def test_cli_lock_diagnostic_names_failing_root(discard_case, monkeypatch, check, failed_root):
    case = discard_case
    target = case.tmp / "opencode-runtime"
    target.mkdir()
    canonical = case.project / "config"
    root = target if failed_root == "target" else canonical
    root.chmod(0)
    try:
        checked = run_publisher(case, target, canonical, check=check)
    finally:
        root.chmod(0o755)
    assert checked.returncode == 13, checked.stderr
    line = next(line for line in checked.stderr.splitlines()
                if line.startswith(PUBLISHER_LOCK_ERROR_PREFIX))
    assert json.loads(line.removeprefix(PUBLISHER_LOCK_ERROR_PREFIX))["root"] == str(root)
    row = doctor_row(case.config, monkeypatch, checked)
    assert str(root) in row.detail and "Permission denied" in row.detail
    _, refreshed = refresh(case, monkeypatch, checked.stderr)
    assert str(root) in refreshed.stderr
    if failed_root == "target":
        assert str(canonical) not in row.detail and "canonical" not in row.remedy
        assert "workflow destination" in row.detail and "lockable" in row.remedy
        assert "Canonical workflow lock" not in refreshed.stderr
    else:
        assert "Canonical workflow lock" in row.detail and "canonical" in row.remedy


@pytest.mark.parametrize("check", [True, False])
@pytest.mark.parametrize("failure", ["acquire", "release"])
def test_cli_target_flock_failure_keeps_target_root(
    discard_case, monkeypatch, capsys, check, failure
):
    case = discard_case
    target = case.tmp / "opencode-runtime"
    target.mkdir()
    flock = publisher.fcntl.flock

    def fail(fd, operation):
        is_target = os.path.samestat(os.fstat(fd), target.stat())
        is_release = operation == publisher.fcntl.LOCK_UN
        if is_target and is_release == (failure == "release"):
            raise OSError(5, "flock unavailable")
        return flock(fd, operation)

    monkeypatch.setattr(publisher.fcntl, "flock", fail)
    argv = ["--view", str(case.project / "config/opencode"), "--canonical-root",
            str(case.project / "config"), "--target", str(target), "--manifest",
            str(target / RUNTIME_MANIFEST_NAME), "--profile", "opencode"]
    if check:
        argv.append("--check")
    assert publisher.main(argv) == 13
    stderr = capsys.readouterr().err
    line = next(line for line in stderr.splitlines()
                if line.startswith(PUBLISHER_LOCK_ERROR_PREFIX))
    payload = json.loads(line.removeprefix(PUBLISHER_LOCK_ERROR_PREFIX))
    assert payload["root"] == str(target) and payload["scope"] == "target"


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_doctor_source_drift_keeps_unreadable_runtime_cause(discard_case, monkeypatch):
    case = discard_case
    (case.project / "config/claude/AGENTS.md").write_text("source update\n")
    monkeypatch.setattr(doctor, "get_project_root", lambda: case.project)
    monkeypatch.setattr(doctor, "audit_workflow_config", sync.audit_config_sync)
    case.runtime.chmod(0)
    try:
        row = doctor._config_workflow_check(case.config)
    finally:
        case.runtime.chmod(0o755)
    assert row.status is doctor.Status.WARN
    assert "source-changed" in row.detail and "invalid-or-semantic" not in row.detail
    assert str(case.runtime) in row.detail and "Permission denied" in row.detail
    assert "lockable" in row.remedy


def test_doctor_source_drift_keeps_unchecked_runtime_cause(discard_case, monkeypatch):
    case = discard_case
    (case.project / "config/claude/AGENTS.md").write_text("source update\n")
    monkeypatch.setattr(doctor, "get_project_root", lambda: case.project)
    monkeypatch.setattr(doctor, "audit_workflow_config", sync.audit_config_sync)
    target = workflow.WorkflowDeliveryTarget("codex", case.runtime)
    problem = workflow.WorkflowPreparationProblem(
        "destination-invalid", f"Cannot inspect {case.runtime}", "Fix runtime readability."
    )
    monkeypatch.setattr(doctor, "inspect_workflow_targets", lambda *a: (
        workflow.WorkflowTargetInspection(target, problem=problem),
    ))
    row = doctor._config_workflow_check(case.config)
    assert problem.message in row.detail and problem.remedy in row.remedy
    assert "invalid-or-semantic" not in row.detail


@pytest.mark.parametrize("hostile_path", [False, True])
def test_discard_skips_absent_runtime_and_syncs_canonical(discard_case, hostile_path):
    case = discard_case
    discard_tests.drift(case, runtime=False)
    shutil.rmtree(case.runtime)
    if hostile_path:
        case.config = case.config.model_copy(update={
            "config_root": case.tmp / "runtime [/error]\x1b[31m\nError: forged",
        })
        case.runtime = case.config.config_root / "codex"
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert sync.audit_config_sync(case.project).clean
    assert not case.runtime.exists()
    label = escape_nonprintable(str(case.runtime))
    assert f"Runtime codex ({label}) was not delivered because it does not exist yet" \
        in result.output
    assert "\x1b" not in result.output and "\nError: forged" not in result.output
    assert "djinn start will provision it" in result.output
    assert (discard_tests.backup_stamp(case) / "config/opencode/AGENTS.md").read_bytes() \
        == b"canonical edit\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
@pytest.mark.parametrize("source_drift", [False, True])
def test_inspection_io_error_has_readability_remedy(discard_case, monkeypatch, source_drift):
    case = discard_case
    source = case.project / "config/claude/skills/demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: guide\n---\nguide\n")
    assert sync.sync_config(case.project).success
    assert workflow.prepare_config_workflow(
        case.project, workflow.start_workflow_targets(case.config), require_compose_host_env=True
    ).success
    if source_drift:
        (source / "SKILL.md").write_text("---\nname: demo\ndescription: guide\n---\nupdate\n")
    monkeypatch.setattr(doctor, "get_project_root", lambda: case.project)
    monkeypatch.setattr(doctor, "audit_workflow_config", sync.audit_config_sync)
    child = case.runtime / "skills"
    child.chmod(0)
    try:
        status = CliRunner().invoke(app, ["config", "status"])
        row = doctor._config_workflow_check(case.config)
    finally:
        child.chmod(0o755)
    assert status.exit_code == 1, (status.output, status.exception)
    for text in (status.output, row.detail):
        assert f"Failed to inspect workflow destination {case.runtime}" in text
        assert "Permission denied" in text
        assert "Failed to publish" not in text
        assert "invalid-or-semantic" not in text
    assert "readable" in row.remedy and "writable" not in row.remedy
    assert "readable" in status.output and "writable" not in status.output


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
@pytest.mark.parametrize("check", [True, False])
def test_cli_inspection_and_publish_io_wording(discard_case, monkeypatch, check):
    case = discard_case
    source = case.project / "config/claude/skills/demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: demo\ndescription: guide\n---\nguide\n")
    assert sync.sync_config(case.project).success
    target = case.tmp / "opencode-runtime"
    target.mkdir()
    assert run_publisher(case, target, case.project / "config", check=False).returncode == 0
    child = target / "skills"
    child.chmod(0)
    try:
        checked = run_publisher(case, target, case.project / "config", check=check)
    finally:
        child.chmod(0o755)
    assert checked.returncode == 13, checked.stderr
    row = doctor_row(case.config, monkeypatch, checked)
    assert str(target) in row.detail and "Permission denied" in row.detail
    if check:
        assert "Failed to inspect workflow destination" in row.detail
        assert "readable" in row.remedy and "writable" not in row.remedy
    else:
        assert "Failed to publish workflow to" in row.detail
        assert "writable" in row.remedy


@pytest.mark.parametrize("tail", ["exec failed: denied\x1b[31m", "failure: " + "x" * 250])
def test_unparseable_refresh_keeps_bounded_sanitised_tail(discard_case, monkeypatch, tail):
    _, refreshed = refresh(discard_case, monkeypatch, "Traceback: earlier detail\n" + tail)
    expected = "OpenCode workflow refresh failed: " + escape_nonprintable(tail)[:200]
    assert refreshed.stderr == expected
    assert "\x1b" not in refreshed.stderr and "earlier detail" not in refreshed.stderr


@pytest.mark.parametrize("drifting_root", ["config", "codex"])
def test_backup_only_creates_directories_for_drifted_roots(discard_case, drifting_root):
    case = discard_case
    discard_tests.drift(case, canonical=drifting_root == "config", runtime=drifting_root == "codex")
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert {path.name for path in discard_tests.backup_stamp(case).iterdir()} == {drifting_root}


def test_lazy_backup_directory_is_private_before_copy(discard_case):
    case = discard_case
    discard_tests.drift(case, canonical=False, source=False)
    inspection = workflow.inspect_workflow_targets(case.project, case.config)[0]
    assert inspection.view is not None
    backup = case.tmp / "public-backup"
    backup.mkdir(mode=0o755)
    descriptor = os.open(backup, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(PermissionError, match="private directory"):
            publisher.backup_target_drift(
                inspection.view, case.project / "config", case.runtime,
                case.runtime / RUNTIME_MANIFEST_NAME, lambda: descriptor,
            )
    finally:
        os.close(descriptor)
    assert list(backup.iterdir()) == []
    assert (case.runtime / "AGENTS.md").read_bytes() == b"runtime edit\n"


def test_discard_reloads_targets_under_lifecycle_guard(discard_case, monkeypatch):
    case = discard_case
    original = case.config
    updated = original.model_copy(update={"config_root": case.tmp / "new-runtime"})
    target = workflow.WorkflowDeliveryTarget("codex", updated.config_root / "codex", provision=True)
    assert workflow.prepare_config_workflow(
        case.project, (target,), config_snapshot=updated
    ).success
    discard_tests.replace_file(target.destination_root / "AGENTS.md", b"new runtime edit\n")
    discard_tests.drift(case, canonical=False, source=False)
    guard = _lifecycle.lifecycle_guard

    @contextmanager
    def change_config():
        with guard():
            case.config = updated
            yield

    monkeypatch.setattr(_lifecycle, "lifecycle_guard", change_config)
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert (target.destination_root / "AGENTS.md").read_bytes() == b"original\n"
    assert (original.config_root / "codex/AGENTS.md").read_bytes() == b"runtime edit\n"
    assert (discard_tests.backup_stamp(case) / "codex/AGENTS.md").read_bytes() \
        == b"new runtime edit\n"


def test_discard_readmits_backup_after_guard_config_change(discard_case, monkeypatch):
    case = discard_case
    discard_tests.drift(case)
    before = discard_tests.roots(case)
    guard = _lifecycle.lifecycle_guard

    @contextmanager
    def change_config():
        with guard():
            case.config = case.config.model_copy(update={
                "code_dir": case.home / ".local/state/djinn/workflow-drift",
            })
            yield

    monkeypatch.setattr(_lifecycle, "lifecycle_guard", change_config)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert "overlaps code_dir" in result.output
    assert discard_tests.roots(case) == before
    assert not (case.home / ".local/state/djinn/workflow-drift").exists()


def test_lifecycle_exception_is_rendered_literally(discard_case, monkeypatch):
    @contextmanager
    def fail():
        raise OSError("guard [/error]\x1b[31m\nError: forged")
        yield

    monkeypatch.setattr(_lifecycle.hostctl, "control_guard", fail)
    result = discard_case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert "guard [/error]\\x1b[31m\\nError: forged" in result.output
    assert "\x1b" not in result.output and "\nError: forged" not in result.output


def test_implementation_documents_current_publisher_label():
    root = Path(__file__).resolve().parents[1]
    text = (root / "IMPLEMENTATION.md").read_text()
    assert 'djinn.workflow.publisher="2"' in text
    assert re.search(r'djinn\.workflow\.publisher="?(?!2\b)\d', text) is None
