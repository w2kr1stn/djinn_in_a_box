"""Control-phase regressions: literal diagnostics, lock roots, seed classes and read I/O."""

from __future__ import annotations

import errno
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import test_workflow_discard as discard_tests
from test_workflow_code_loop_fix1 import doctor_row, run_publisher
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import container, doctor
from djinn_in_a_box.config import loader
from djinn_in_a_box.core import config_sync as sync
from djinn_in_a_box.core import config_workflow as workflow
from djinn_in_a_box.core import session
from djinn_in_a_box.core import workflow_publisher as publisher
from djinn_in_a_box.core.console import escape_nonprintable

discard_case = discard_tests.discard_case


def start_refusal(case, monkeypatch):
    monkeypatch.setattr(container, "load_config", lambda: case.config)
    monkeypatch.setattr(container, "get_project_root", lambda: case.project)
    monkeypatch.setattr(container, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(container, "ensure_network", lambda: pytest.fail("start passed preflight"))
    result = CliRunner().invoke(app, ["start"])
    assert result.exit_code == 1, (result.output, result.exception)
    assert result.exception is not None and isinstance(result.exception, SystemExit)
    return result.output


def workflow_row(case, monkeypatch):
    monkeypatch.setattr(doctor, "get_project_root", lambda: case.project)
    monkeypatch.setattr(doctor, "audit_workflow_config", sync.audit_config_sync)
    row = doctor._config_workflow_check(case.config)
    assert row.status is doctor.Status.WARN
    return row


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_hostile_unreadable_root_is_literal_at_every_sink(discard_case, monkeypatch):
    case = discard_case
    name = "runtime[bold]\x1b]0;forged\x07\nFORGED-LINE"
    hostile = case.tmp / name
    case.config.config_root.rename(hostile)
    case.config = case.config.model_copy(update={"config_root": hostile})
    case.runtime = hostile / "codex"
    case.runtime.chmod(0)
    try:
        status = CliRunner().invoke(app, ["config", "status"])
        synced = CliRunner().invoke(app, ["config", "sync"])
        row = workflow_row(case, monkeypatch)
        monkeypatch.setattr(loader, "load_config", lambda: case.config)
        monkeypatch.setattr(doctor, "run_checks", lambda *a: [row])
        rendered = CliRunner().invoke(app, ["doctor"])
        refused = start_refusal(case, monkeypatch)
    finally:
        case.runtime.chmod(0o755)
    assert status.exit_code == 1, status.output
    assert synced.exit_code == 0, synced.output
    assert rendered.exit_code == 0, (rendered.output, rendered.exception)
    assert "Runtime:" in status.output and "Warning:" in synced.output
    for text in (status.output, synced.output, rendered.output, refused):
        assert escape_nonprintable(str(case.runtime)) in text
        assert "Permission denied" in text
        assert "[bold]" in text
        assert "\x1b" not in text and "\x07" not in text
        assert "\\nFORGED-LINE" in text
        assert not any(line.lstrip().startswith("FORGED-LINE") for line in text.splitlines())


@pytest.mark.parametrize("builder", ["inspect", "lock", "write", "canonical", "target"])
def test_operational_message_builders_escape_paths_and_causes(discard_case, builder):
    case = discard_case
    root = case.tmp / "[bold]\x1b]0;bad\x07\nFORGED-LINE"
    cause = OSError(errno.EIO, "bad\x1b[2J\x07\nFORGED-LINE")
    target = workflow.WorkflowDeliveryTarget("codex", root)
    if builder == "inspect":
        result = workflow.inspection_failure(workflow.WorkflowTargetInspection(
            target, publisher.PublishResult(publisher.DriftClass.INVALID_OR_SEMANTIC,
                                            write_error=cause)
        ), case.config)
        problem = result.problems[0]
    elif builder == "canonical":
        error = publisher.PublishError(publisher.DriftClass.INVALID_OR_SEMANTIC, lock_error=cause)
        error.__cause__ = cause
        problem = workflow._canonical_lock_failure(root, error).problems[0]
    elif builder == "target":
        root.symlink_to(case.runtime, target_is_directory=True)
        problem = workflow._prepare_target(target)
        assert problem is not None
    else:
        factory = (workflow._publish_lock_failure if builder == "lock"
                   else workflow._publish_write_failure)
        problem = factory(root, cause).problems[0]
    assert escape_nonprintable(str(root)) in problem.message
    assert "[bold]" in problem.message
    assert all(char.isprintable() for char in problem.message)


def test_discard_backup_exception_is_literal(discard_case, monkeypatch):
    case = discard_case
    discard_tests.drift(case)
    before = discard_tests.roots(case)

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "backup[bold]\x1b]0;bad\x07\nFORGED-LINE")

    monkeypatch.setattr(workflow, "backup_target_drift", fail)
    result = case.invoke()
    assert result.exit_code == 1, result.output
    assert "backup[bold]\\x1b]0;bad\\x07\\nFORGED-LINE" in result.output
    assert "\x1b" not in result.output and "\x07" not in result.output
    assert "\nFORGED-LINE" not in result.output
    assert discard_tests.roots(case) == before


@pytest.mark.parametrize("failed_root", ["runtime", "canonical"])
@pytest.mark.parametrize("operation", ["open", "acquire", "release"])
def test_discard_backup_lock_names_actual_root(discard_case, monkeypatch, failed_root, operation):
    case = discard_case
    discard_tests.drift(case)
    before = discard_tests.roots(case)
    canonical = case.project / "config"
    root = case.runtime if failed_root == "runtime" else canonical
    original_backup = workflow.backup_target_drift
    flock = publisher.fcntl.flock
    original_open = publisher.os.open
    runtime_backup = False
    faults = []

    def backup(*args, **kwargs):
        nonlocal runtime_backup
        runtime_backup = args[2] == case.runtime
        try:
            return original_backup(*args, **kwargs)
        finally:
            runtime_backup = False

    def fault(fd, op):
        if (
            runtime_backup and operation != "open"
            and os.path.samestat(os.fstat(fd), root.stat())
            and (op == publisher.fcntl.LOCK_UN) == (operation == "release")
        ):
            faults.append(op)
            raise OSError(errno.EIO, "backup flock I/O failure")
        return flock(fd, op)

    def fault_open(path, *args, **kwargs):
        if runtime_backup and operation == "open" and path == root:
            faults.append("open")
            raise OSError(errno.EIO, "backup flock I/O failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(workflow, "backup_target_drift", backup)
    monkeypatch.setattr(publisher.fcntl, "flock", fault)
    monkeypatch.setattr(publisher.os, "open", fault_open)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert faults
    assert str(root) in result.output and "backup flock I/O failure" in result.output
    if failed_root == "runtime":
        assert f"Failed to lock the workflow destination {root}" in result.output
        assert "destination is a lockable directory" in result.output
        assert str(canonical) not in result.output
        assert "Canonical workflow lock" not in result.output
    else:
        assert f"Canonical workflow lock failed at {canonical}" in result.output
        assert "canonical workflow root is a readable, lockable directory" in result.output
    assert "Backups were kept in" in result.output
    assert (discard_tests.backup_stamp(case) / "config/opencode/AGENTS.md").read_bytes() \
        == b"canonical edit\n"
    if failed_root == "runtime" and operation == "release":
        assert (discard_tests.backup_stamp(case) / "codex/AGENTS.md").read_bytes() \
            == b"runtime edit\n"
    assert discard_tests.roots(case) == before


@pytest.mark.parametrize("failure", ["seed-symlink", "seed-mismatch", "canonical-manifest"])
def test_real_publisher_class_and_origin_reach_both_consumers(discard_case, monkeypatch, failure):
    case = discard_case
    canonical = case.project / "config"
    seed = canonical / "opencode/AGENTS.md"
    target = case.tmp / "opencode-runtime"
    target.mkdir()
    assert run_publisher(case, target, canonical, check=False).returncode == 0
    if failure == "seed-symlink":
        seed.unlink()
        seed.symlink_to(canonical / "claude/AGENTS.md")
    elif failure == "seed-mismatch":
        seed.write_bytes(b"seed differs from manifest\n")
    else:
        (canonical / publisher.CANONICAL_MANIFEST_NAME).write_bytes(b"{}\n")
    checked = run_publisher(case, target, canonical)
    expected = "source-changed" if failure == "seed-mismatch" else "invalid-or-semantic"
    assert checked.returncode == (10 if failure == "seed-mismatch" else 13), checked.stderr
    assert f"workflow publisher: {expected}\n" in checked.stderr
    parsed = session.parse_publisher_drift_items(checked.stderr)
    assert parsed is not None and parsed.items
    assert parsed.items[0].origin == ("canonical-manifest" if failure == "canonical-manifest"
                                      else "seed")
    refreshed = session._publisher_refresh_error(checked.stderr)
    row = doctor_row(case.config, monkeypatch, checked)
    assert row.status is doctor.Status.WARN
    assert expected in row.detail and f"failed: {expected}" in refreshed
    item = publisher.CANONICAL_MANIFEST_NAME if failure == "canonical-manifest" else "AGENTS.md"
    assert item in row.detail and item in refreshed
    if expected == "source-changed":
        assert sync.WORKFLOW_REMEDIES[expected] in refreshed
        assert sync.WORKFLOW_REMEDIES[expected] in row.remedy
        assert "recreate the container" in row.remedy
    else:
        assert sync.WORKFLOW_REMEDIES["entrypoint-generic"] in refreshed
        assert row.remedy == sync.WORKFLOW_REMEDIES["entrypoint-generic"]
        assert "failed: source-changed" not in refreshed


@pytest.mark.parametrize("origin", ["seed", "canonical-manifest"])
@pytest.mark.parametrize("kind", [publisher.DriftClass.COLLISION, publisher.DriftClass.TARGET_DRIFT,
                                  publisher.DriftClass.INVALID_OR_SEMANTIC])
def test_non_target_remedy_precedes_target_and_semantic_advice(origin, kind):
    item = publisher.DriftEntry(kind, PurePosixPath("config.toml"), ("hook",),
                                origin=origin, reason="changed-during-publish")
    assert sync.workflow_remedy(kind, "claude", scope="opencode", items=(item,)) \
        == sync.WORKFLOW_REMEDIES["entrypoint-generic"]


@pytest.mark.parametrize("leaf", ["AGENTS.md", publisher.RUNTIME_MANIFEST_NAME])
@pytest.mark.parametrize("failure", ["permissions", "eio", "open-eio", "bare-oserror"])
def test_target_read_failure_is_operational_everywhere(
    discard_case, monkeypatch, capsys, leaf, failure
):
    if failure == "permissions" and os.geteuid() == 0:
        pytest.skip("permission bits do not bind root")
    case = discard_case
    canonical = case.project / "config"
    opencode = case.tmp / "opencode-runtime"
    opencode.mkdir()
    assert run_publisher(case, opencode, canonical, check=False).returncode == 0
    loaded = workflow.inspect_workflow_targets(case.project, case.config)[0]
    assert loaded.clean and loaded.view is not None
    before = discard_tests.roots(case), discard_tests.tree(opencode)
    paths = (case.runtime / leaf, opencode / leaf)
    modes = [path.stat().st_mode & 0o7777 for path in paths]
    original_read = publisher._read_pinned
    original_open = publisher.os.open

    def fail_read(pin, **kwargs):
        if Path(os.readlink(f"/proc/self/fd/{pin}")) in paths:
            if failure == "bare-oserror":
                raise OSError("read unavailable")
            raise OSError(errno.EIO, "Input/output error")
        return original_read(pin, **kwargs)

    def fail_open(name, flags, *args, **kwargs):
        parent = kwargs.get("dir_fd")
        if name == leaf and parent is not None and (
            Path(os.readlink(f"/proc/self/fd/{parent}")) in {case.runtime, opencode}
        ):
            raise OSError(errno.EIO, "Input/output error")
        return original_open(name, flags, *args, **kwargs)

    if failure == "permissions":
        for path in paths:
            path.chmod(0)
    elif failure in {"eio", "bare-oserror"}:
        monkeypatch.setattr(publisher, "_read_pinned", fail_read)
    else:
        monkeypatch.setattr(publisher.os, "open", fail_open)
    cause = ("Permission denied" if failure == "permissions" else "read unavailable"
             if failure == "bare-oserror" else "Input/output error")
    try:
        status = CliRunner().invoke(app, ["config", "status"])
        row = workflow_row(case, monkeypatch)
        refused = start_refusal(case, monkeypatch)
        published = publisher.publish_workflow_view(
            loaded.view, canonical, case.runtime, case.runtime / publisher.RUNTIME_MANIFEST_NAME
        )
        assert not published.success and published.write_error is not None
        assert cause in str(published.write_error) and leaf in str(published.write_error)
        write_problem = workflow._publish_write_failure(case.runtime, published.write_error)
        assert "readable" in write_problem.problems[0].remedy
        # A failing target read must never enter the backup writer or authorize an overwrite.
        def no_backup():
            pytest.fail("backup writer reached after target read failure")

        with pytest.raises(OSError, match=cause) as error:
            publisher.backup_target_drift(
                loaded.view, canonical, case.runtime,
                case.runtime / publisher.RUNTIME_MANIFEST_NAME, no_backup,
            )
        assert leaf in str(error.value)
        if failure == "permissions":
            checked = run_publisher(case, opencode, canonical)
        else:
            argv = ["--view", str(canonical / "opencode"), "--canonical-root", str(canonical),
                    "--target", str(opencode), "--manifest",
                    str(opencode / publisher.RUNTIME_MANIFEST_NAME), "--profile", "opencode",
                    "--ignore", "opencode.json", "--check"]
            capsys.readouterr()
            code = publisher.main(argv)
            checked = subprocess.CompletedProcess(argv, code, "", capsys.readouterr().err)
    finally:
        for path, mode in zip(paths, modes, strict=True):
            path.chmod(mode)
    assert status.exit_code == 1, (status.output, status.exception)
    for text in (status.output, row.detail, refused):
        assert cause in text and leaf in text and str(case.runtime) in text
        assert "invalid-or-semantic" not in text and "natively" not in text
    assert "readable" in status.output and "readable" in row.remedy and "readable" in refused
    assert checked.returncode == 13, checked.stderr
    diagnostic = next(line.removeprefix(publisher.PUBLISHER_WRITE_ERROR_PREFIX)
                      for line in checked.stderr.splitlines()
                      if line.startswith(publisher.PUBLISHER_WRITE_ERROR_PREFIX))
    details = json.loads(diagnostic)
    assert details["operation"] == "inspect"
    assert cause in details["error"] and leaf in details["error"]
    refreshed = session._publisher_refresh_error(checked.stderr)
    operational_row = doctor_row(case.config, monkeypatch, checked)
    for text in (refreshed, operational_row.detail):
        assert cause in text and leaf in text
        assert "invalid-or-semantic" not in text and "natively" not in text
    assert "readable" in refreshed and "readable" in operational_row.remedy
    assert (discard_tests.roots(case), discard_tests.tree(opencode)) == before


@pytest.mark.parametrize("leaf", ["AGENTS.md", publisher.RUNTIME_MANIFEST_NAME])
def test_discard_backup_read_error_keeps_item_cause_and_readability(
    discard_case, monkeypatch, leaf
):
    case = discard_case
    discard_tests.drift(case)
    before = discard_tests.roots(case)
    original_backup = workflow.backup_target_drift
    original_read = publisher._read_pinned
    runtime_backup = False

    def backup(*args, **kwargs):
        nonlocal runtime_backup
        runtime_backup = args[2] == case.runtime
        try:
            return original_backup(*args, **kwargs)
        finally:
            runtime_backup = False

    def fail_read(pin, **kwargs):
        if runtime_backup and Path(os.readlink(f"/proc/self/fd/{pin}")) == case.runtime / leaf:
            raise OSError(errno.EIO, "Input/output error")
        return original_read(pin, **kwargs)

    monkeypatch.setattr(workflow, "backup_target_drift", backup)
    monkeypatch.setattr(publisher, "_read_pinned", fail_read)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert "Workflow drift backup failed" in result.output
    assert leaf in result.output and "Input/output error" in result.output
    assert "readable" in result.output and "natively" not in result.output
    assert "invalid-or-semantic" not in result.output
    assert "Backups were kept in" in result.output
    assert (discard_tests.backup_stamp(case) / "config/opencode/AGENTS.md").read_bytes() \
        == b"canonical edit\n"
    assert discard_tests.roots(case) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
@pytest.mark.parametrize("origin", ["seed", "canonical-manifest"])
def test_canonical_view_read_refusals_keep_existing_class(discard_case, origin):
    case = discard_case
    canonical = case.project / "config"
    leaf = (canonical / "opencode/AGENTS.md" if origin == "seed"
            else canonical / publisher.CANONICAL_MANIFEST_NAME)
    mode = leaf.stat().st_mode & 0o7777
    leaf.chmod(0)
    try:
        checked = run_publisher(case, case.tmp / "absent-runtime", canonical)
    finally:
        leaf.chmod(mode)
    assert checked.returncode == 13, checked.stderr
    parsed = session.parse_publisher_drift_items(checked.stderr)
    assert parsed is not None and parsed.items[0].origin == origin
    assert publisher.PUBLISHER_WRITE_ERROR_PREFIX not in checked.stderr
    assert "Traceback" not in checked.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
@pytest.mark.parametrize("leaf", ["AGENTS.md", publisher.RUNTIME_MANIFEST_NAME])
def test_publish_cli_read_error_offers_readability(discard_case, leaf):
    case = discard_case
    canonical = case.project / "config"
    target = case.tmp / "opencode-runtime"
    target.mkdir()
    assert run_publisher(case, target, canonical, check=False).returncode == 0
    before = discard_tests.tree(target)
    path = target / leaf
    mode = path.stat().st_mode & 0o7777
    path.chmod(0)
    try:
        checked = run_publisher(case, target, canonical, check=False)
    finally:
        path.chmod(mode)
    assert checked.returncode == 13, checked.stderr
    refreshed = session._publisher_refresh_error(checked.stderr)
    assert "Permission denied" in refreshed and leaf in refreshed
    assert "publish workflow" in refreshed and "readable" in refreshed
    assert "natively" not in refreshed and "invalid-or-semantic" not in refreshed
    assert discard_tests.tree(target) == before


def test_problem_remedy_with_a_path_is_literal():
    problem = workflow.WorkflowPreparationProblem(
        "host-workflow-trust-failed", "m", "Make /state\x1b]0;bad\x07\nFORGED-LINE writable."
    )

    assert problem.remedy == "Make /state\\x1b]0;bad\\x07\\nFORGED-LINE writable."
