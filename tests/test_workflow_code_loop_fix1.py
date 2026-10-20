"""Code-Loop fix round 1 (#166): host manifest advice, operational failures, discard phases."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
import test_workflow_host_integration as host_tests
from test_workflow_discard import (
    discard_case,  # noqa: F401
    drift,
    roots,
)
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import config as config_command
from djinn_in_a_box.commands import doctor
from djinn_in_a_box.config import loader
from djinn_in_a_box.core import config_workflow as workflow
from djinn_in_a_box.core import session, workflow_publisher
from djinn_in_a_box.core.config_sync import CANONICAL_REMEDY, WORKFLOW_REMEDIES
from djinn_in_a_box.core.docker import WorkflowImageCompatibility
from djinn_in_a_box.core.workflow_publisher import (
    CANONICAL_MANIFEST_NAME,
    RUNTIME_MANIFEST_NAME,
    DriftClass,
    DriftEntry,
    PublishResult,
)

case = host_tests.case
prepare = host_tests.prepare


# --- CC-2: a host recovery never tells the user to touch the ownership manifest ---


def test_host_manifest_changed_at_mutation_boundary_gives_retry_not_manifest_removal(
    case, monkeypatch
):
    assert prepare(case, host=True).success
    root = case[2].code_dir / "host"
    manifest = root / RUNTIME_MANIFEST_NAME
    (case[0] / "config/claude/AGENTS.md").write_text("source update\n")

    def edit_manifest(count: int) -> None:
        if count == 1:
            content = manifest.read_bytes()
            manifest.unlink()
            manifest.write_bytes(content + b"\n")

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", edit_manifest)
    result = prepare(case, host=True)
    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", lambda count: None)

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "target-drift"
    assert RUNTIME_MANIFEST_NAME in problem.message
    assert "remove" not in problem.message and "restore" not in problem.message
    assert problem.remedy == WORKFLOW_REMEDIES["changed-during-publish"]
    assert manifest.read_bytes().endswith(b"\n\n")


def test_host_actions_only_for_managed_file_drift(case):
    assert prepare(case, host=True).success
    project, _, config = case
    target = workflow.WorkflowDeliveryTarget("codex", config.code_dir / "host")
    loaded = workflow.inspect_workflow_targets(project, config, (target,), compose=False)[0]
    assert loaded.view is not None
    items = (
        DriftEntry(DriftClass.TARGET_DRIFT, PurePosixPath("AGENTS.md")),
        DriftEntry(
            DriftClass.TARGET_DRIFT,
            PurePosixPath(RUNTIME_MANIFEST_NAME),
            origin="target-manifest",
        ),
        DriftEntry(
            DriftClass.TARGET_DRIFT,
            PurePosixPath("config.toml"),
            reason="changed-during-publish",
        ),
        DriftEntry(
            DriftClass.TARGET_DRIFT,
            PurePosixPath(CANONICAL_MANIFEST_NAME),
            origin="canonical-manifest",
        ),
    )
    failure = workflow._publish_failure(  # pyright: ignore[reportPrivateUsage]
        PublishResult(DriftClass.TARGET_DRIFT, drift_items=items), config, target, loaded.view
    )
    message = failure.problems[0].message
    assert "restore from config/codex/AGENTS.md" in message
    assert message.count("restore") == 1 and "remove" not in message

    only_metadata = workflow._publish_failure(  # pyright: ignore[reportPrivateUsage]
        PublishResult(DriftClass.TARGET_DRIFT, drift_items=items[1:]), config, target, loaded.view
    )
    assert "restore" not in only_metadata.problems[0].message
    assert "remove" not in only_metadata.problems[0].message
    assert not only_metadata.problems[0].message.endswith(" ")


# --- CC-1 / CA-3: operational failures are not content drift ---


def run_publisher(case, target: Path, canonical: Path, *, check: bool = True):
    argv = [
        sys.executable, str(Path(workflow_publisher.__file__)),
        "--view", str(case.project / "config/opencode"),
        "--canonical-root", str(canonical),
        "--target", str(target), "--manifest", str(target / RUNTIME_MANIFEST_NAME),
        "--ignore", "opencode.json", "--profile", "opencode",
    ]
    if check:
        argv.append("--check")
    return subprocess.run(argv, capture_output=True, text=True, check=False,
                          stdin=subprocess.DEVNULL, timeout=10)


def doctor_row(config, monkeypatch, checked: subprocess.CompletedProcess[str]):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 0, "true\n", "")
        return subprocess.CompletedProcess(argv, checked.returncode, "", checked.stderr)

    monkeypatch.setattr(doctor.subprocess, "run", run)
    monkeypatch.setattr(session, "container_workflow_image_compatible",
                        lambda container_id: WorkflowImageCompatibility.COMPATIBLE)
    return doctor._opencode_runtime_check(config, daemon=True)


def assert_operational_row(row):
    assert row.status is doctor.Status.WARN
    assert row.detail.startswith("check unavailable (")
    assert "invalid-or-semantic" not in row.detail
    assert row.remedy != CANONICAL_REMEDY and "natively" not in row.remedy
    assert "retry" in row.remedy


def test_doctor_lock_error_from_missing_canonical_root(discard_case, monkeypatch):  # noqa: F811
    case = discard_case
    target = case.tmp / "opencode-runtime"
    target.mkdir()
    missing = case.tmp / "missing-canonical"
    checked = run_publisher(case, target, missing)
    assert checked.returncode == 13 and "lock-error" in checked.stderr
    row = doctor_row(case.config, monkeypatch, checked)
    assert_operational_row(row)
    assert "Canonical workflow lock failed at" in row.detail
    assert "missing-canonical" in row.detail
    assert "No such file" in row.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_doctor_unreadable_target_directory(discard_case, monkeypatch):  # noqa: F811
    case = discard_case
    target = case.tmp / "opencode-runtime"
    target.mkdir()
    assert run_publisher(case, target, case.project / "config", check=False).returncode == 0
    target.chmod(0)
    try:
        checked = run_publisher(case, target, case.project / "config")
    finally:
        target.chmod(0o700)
    assert checked.returncode == 13, checked.stderr
    row = doctor_row(case.config, monkeypatch, checked)
    assert_operational_row(row)
    assert "Permission denied" in row.detail


def test_doctor_itemless_nonzero_result_is_unavailable(case, monkeypatch):
    empty = '{"items":[],"total":0,"truncated":false}'
    prefix = workflow_publisher.PUBLISHER_DRIFT_ITEMS_PREFIX
    stderr = f"workflow publisher: invalid-or-semantic\n{prefix}{empty}\n"
    row = doctor_row(case[2], monkeypatch, subprocess.CompletedProcess([], 13, "", stderr))
    assert (row.status, row.detail) == (doctor.Status.WARN, "check unavailable")


def test_operational_text_is_sanitised():
    stderr = workflow_publisher.PUBLISHER_LOCK_ERROR_PREFIX + (
        '{"root": "/x[red]\\n\\u001b[2Jroot", "error": "bad [/error]"}'
    )
    cause, remedy = session.publisher_operational_failure(stderr) or ("", "")
    assert "[" not in cause and "]" not in cause and "\n" not in cause and "\x1b" not in cause
    assert cause.startswith("Canonical workflow lock failed at /x")
    assert remedy


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_status_and_preflight_report_unreadable_runtime_root(discard_case):  # noqa: F811
    case = discard_case
    case.runtime.chmod(0)
    try:
        status = CliRunner().invoke(app, ["config", "status"])
        prepared = workflow.prepare_config_workflow(
            case.project, workflow.start_workflow_targets(case.config),
            require_compose_host_env=True,
        )
    finally:
        case.runtime.chmod(0o755)
    assert status.exit_code == 1, status.output
    assert "Permission denied" in status.output
    assert "Drift: invalid-or-semantic" not in status.output
    assert "()" not in status.output and "natively" not in status.output
    assert not prepared.success
    problem = prepared.problems[0]
    assert problem.identifier in {"workflow-publish-failed", "workflow-lock-failed"}
    assert "Permission denied" in problem.message
    assert problem.remedy != CANONICAL_REMEDY


def test_failure_without_labels_has_no_location_clause():
    failure = workflow._failure("invalid-or-semantic")  # pyright: ignore[reportPrivateUsage]
    message = failure.problems[0].message
    assert " in " not in message


def test_status_printer_reports_write_error_without_drift_label(case, monkeypatch):
    project, _, config = case
    target = workflow.WorkflowDeliveryTarget("codex", config.config_root / "codex")
    result = PublishResult(
        DriftClass.INVALID_OR_SEMANTIC, write_error=PermissionError(13, "denied")
    )
    inspection = workflow.WorkflowTargetInspection(target, result)
    monkeypatch.setattr(
        config_command, "inspect_workflow_targets", lambda root, cfg: (inspection,)
    )
    lines: list[str] = []
    monkeypatch.setattr(
        config_command.console, "print", lambda value, *a, **k: lines.append(str(value))
    )
    printer = config_command._print_runtime_inspections  # pyright: ignore[reportPrivateUsage]
    assert printer(project) is False
    text = "\n".join(lines)
    assert "Runtime: Failed to publish workflow to" in text and "denied" in text
    assert "Drift:" not in text and "()" not in text

    lines.clear()
    itemless = workflow.WorkflowTargetInspection(
        target, PublishResult(DriftClass.INVALID_OR_SEMANTIC)
    )
    monkeypatch.setattr(
        config_command, "inspect_workflow_targets", lambda root, cfg: (itemless,)
    )
    assert printer(project) is False
    assert "Drift: invalid-or-semantic" in lines and "()" not in "\n".join(lines)


# --- CC-4 / CA-4: the doctor check does not load agents.toml ---


def test_doctor_running_container_with_malformed_agents_file(case, monkeypatch, tmp_path):
    broken = tmp_path / "agents.toml"
    broken.write_text("[agents\n")
    monkeypatch.setattr(loader, "AGENTS_FILE", broken)
    with pytest.raises(Exception, match="Invalid TOML"):
        loader.load_agents()
    row = doctor_row(case[2], monkeypatch, subprocess.CompletedProcess([], 0, "", ""))
    assert row.status is doctor.Status.PASS

    drifted = subprocess.CompletedProcess([], 11, "", "workflow publisher: target-drift\n")
    row = doctor_row(case[2], monkeypatch, drifted)
    assert row.status is doctor.Status.WARN and row.detail == "check unavailable"


def test_session_manager_delegates_to_module_level_image_check(monkeypatch):
    seen = []
    monkeypatch.setattr(
        session, "container_workflow_image_compatible",
        lambda container_id: seen.append(container_id) or WorkflowImageCompatibility.MISSING,
    )
    manager = session.SessionManager("workflow-check")
    assert (
        manager.workflow_image_compatible(session.SessionTarget("abc"))
        is WorkflowImageCompatibility.MISSING
    )
    assert seen == ["abc"]
    assert (
        manager.workflow_image_compatible(session.SessionTarget())
        is WorkflowImageCompatibility.UNKNOWN
    )


# --- CC-3: phase-2 failure of the discard orchestration ---


def test_canonical_write_failure_after_rename_is_reported(discard_case, monkeypatch):  # noqa: F811
    case = discard_case
    drift(case)
    canonical = case.project / "config"
    manifest = canonical / CANONICAL_MANIFEST_NAME
    initial = manifest.read_bytes()
    fsync = os.fsync

    def fault(fd):
        try:
            path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            path = None
        if path == canonical and manifest.read_bytes() != initial:
            raise OSError(5, "synthetic final canonical fsync failure")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", fault)
    before_runtime = (case.runtime / "AGENTS.md").read_bytes()
    result = case.invoke()
    monkeypatch.setattr(os, "fsync", fsync)

    assert result.exit_code == 1, (result.output, result.exception)
    out = result.output
    assert "synthetic final canonical fsync failure" in out
    assert "Discard incomplete: failed during canonical" in out
    assert "Skipped: codex; runtime repair is incomplete" in out
    assert "Backups were kept in" in out and "Backed up to" in out
    assert "State: clean" not in out
    stamps = list((case.home / ".local/state/djinn/workflow-drift").iterdir())
    assert len(stamps) == 1 and any(stamps[0].rglob("AGENTS.md"))
    assert (case.runtime / "AGENTS.md").read_bytes() == before_runtime == b"runtime edit\n"


def test_runtime_phase_failure_names_completed_and_skipped(discard_case, monkeypatch):  # noqa: F811
    case = discard_case
    drift(case)
    real = workflow._deliver_target  # pyright: ignore[reportPrivateUsage]

    def failing(config, project_root, target, *args, **kwargs):
        return workflow._publish_write_failure(  # pyright: ignore[reportPrivateUsage]
            target.destination_root, OSError(28, "No space left on device")
        )

    monkeypatch.setattr(workflow, "_deliver_target", failing)
    result = case.invoke()
    monkeypatch.setattr(workflow, "_deliver_target", real)
    assert result.exit_code == 1, (result.output, result.exception)
    assert "No space left on device" in result.output
    assert "Discard incomplete: failed during runtime codex" in result.output
    assert "Completed: canonical." in result.output
    assert "Backups were kept in" in result.output


def test_backup_failure_reports_nothing_overwritten(discard_case, monkeypatch):  # noqa: F811
    case = discard_case
    drift(case)
    before = roots(case)

    def fail(*args, **kwargs):
        raise OSError(5, "backup disk gone")

    monkeypatch.setattr(workflow, "backup_target_drift", fail)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert "backup disk gone" in result.output
    assert "Discard incomplete: failed during backup" in result.output
    assert "Skipped: canonical, codex; runtime repair is incomplete" in result.output
    assert roots(case) == before


def test_sync_config_surfaces_write_and_lock_errors(discard_case, monkeypatch):  # noqa: F811
    case = discard_case
    (case.project / "config/claude/AGENTS.md").write_bytes(b"source update\n")
    from djinn_in_a_box.core import config_sync

    real = config_sync.publish_workflow_view

    def write_failure(*args, **kwargs):
        return PublishResult(DriftClass.INVALID_OR_SEMANTIC, write_error=OSError(28, "No space"))

    monkeypatch.setattr(config_sync, "publish_workflow_view", write_failure)
    result = config_sync.sync_config(case.project)
    assert not result.success
    problem = result.audit.problems[0]
    assert problem.identifier == config_sync.WRITE_PROBLEM_IDENTIFIER
    assert "No space" in problem.message and str(case.project / "config") in problem.message
    assert problem.remedy and "retry" in problem.remedy

    def lock_failure(*args, **kwargs):
        return PublishResult(DriftClass.INVALID_OR_SEMANTIC, lock_error=OSError(13, "denied"))

    monkeypatch.setattr(config_sync, "publish_workflow_view", lock_failure)
    problem = config_sync.sync_config(case.project).audit.problems[0]
    assert problem.identifier == "canonical-lock-failed" and "denied" in problem.message
    monkeypatch.setattr(config_sync, "publish_workflow_view", real)


# --- CL-B-1: exception text and paths are printed literally ---


@pytest.mark.parametrize("suffix", ["[red]BAD[/red]\nFORGED-LINE", "[/error]"])
def test_discard_refusal_prints_declared_path_literally(discard_case, suffix):  # noqa: F811
    from djinn_in_a_box.config.declarations import BindDeclaration

    case = discard_case
    source = case.home / ".local/state/djinn/workflow-drift" / f"stamp{suffix}"
    case.config = case.config.model_copy(
        update={"mounts": {"data": BindDeclaration(source=str(source), target="/data")}}
    )
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert result.output.strip()
    assert "MarkupError" not in repr(result.exception)
    assert "overlaps" in result.output
    assert suffix.replace("\n", "\\n") in result.output
    assert "\nFORGED-LINE" not in result.output


def test_backup_failure_text_is_literal(discard_case, monkeypatch):  # noqa: F811
    case = discard_case
    drift(case)

    def fail(*args, **kwargs):
        raise OSError(5, "boom [/error]\nFORGED-LINE")

    monkeypatch.setattr(workflow, "backup_target_drift", fail)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert "boom [/error]\\nFORGED-LINE" in result.output
    assert "\nFORGED-LINE" not in result.output


# --- CA-2: no compatibility alias ---


def test_no_host_claude_view_alias():
    assert not hasattr(workflow, "_host_claude_view")
    assert callable(workflow._delivery_view)  # pyright: ignore[reportPrivateUsage]
