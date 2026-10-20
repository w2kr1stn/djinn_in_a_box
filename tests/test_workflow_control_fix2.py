"""Control-phase regressions for canonical read failures and their consumers."""

from __future__ import annotations

import errno
import os
from pathlib import PurePosixPath

import pytest
import test_workflow_discard as discard_tests
from test_workflow_code_loop_fix1 import doctor_row, run_publisher
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import doctor
from djinn_in_a_box.core import config_sync as sync
from djinn_in_a_box.core import session
from djinn_in_a_box.core import workflow_drift_backup as storage
from djinn_in_a_box.core import workflow_publisher as publisher

discard_case = discard_tests.discard_case


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
@pytest.mark.parametrize("check", [True, False], ids=["check", "publish"])
def test_canonical_manifest_open_refusal_reaches_cli_and_consumers(
    discard_case, monkeypatch, check
):
    case = discard_case
    canonical = case.project / "config"
    target = case.tmp / "opencode-runtime"
    target.mkdir()
    assert run_publisher(case, target, canonical, check=False).returncode == 0
    before = discard_tests.roots(case), discard_tests.tree(target)
    mode = canonical.stat().st_mode & 0o7777
    canonical.chmod(0o400)
    try:
        checked = run_publisher(case, target, canonical, check=check)
    finally:
        canonical.chmod(mode)

    assert checked.returncode == 13, checked.stderr
    assert "workflow publisher: invalid-or-semantic\n" in checked.stderr
    assert "Traceback" not in checked.stderr
    assert publisher.PUBLISHER_WRITE_ERROR_PREFIX not in checked.stderr
    diagnostic = session.parse_publisher_drift_items(checked.stderr)
    assert diagnostic is not None and diagnostic.total == 1 and len(diagnostic.items) == 1
    item = diagnostic.items[0]
    assert item.kind is publisher.DriftClass.INVALID_OR_SEMANTIC
    assert item.path == PurePosixPath(publisher.CANONICAL_MANIFEST_NAME)
    assert item.origin == "canonical-manifest"

    refreshed = session._publisher_refresh_error(checked.stderr)
    row = doctor_row(case.config, monkeypatch, checked)
    assert row.status is doctor.Status.WARN
    assert "failed: invalid-or-semantic" in refreshed
    assert row.detail.startswith("invalid-or-semantic in ")
    for text in (refreshed, row.detail):
        assert publisher.CANONICAL_MANIFEST_NAME in text
    generic = sync.WORKFLOW_REMEDIES["entrypoint-generic"]
    assert generic in refreshed and row.remedy == generic
    assert (discard_tests.roots(case), discard_tests.tree(target)) == before


@pytest.mark.parametrize("origin", ["target", "target-manifest", "canonical-manifest", "seed"])
def test_direct_read_oserror_obeys_origin_policy(monkeypatch, origin):
    path = PurePosixPath("AGENTS.md")
    cause = OSError(errno.EIO, "Input/output error")

    def fail_read(*args):
        raise cause

    monkeypatch.setattr(publisher, "_read_relative", fail_read)
    if origin in {"target", "target-manifest"}:
        with pytest.raises(publisher.TargetReadError) as raised:
            publisher._read_item(-1, path, origin)
        assert raised.value.filename == str(path) and raised.value.errno == errno.EIO
    else:
        with pytest.raises(publisher.PublishError) as raised:
            publisher._read_item(-1, path, origin)
        assert raised.value.drift_class is publisher.DriftClass.INVALID_OR_SEMANTIC
        assert raised.value.items == (
            publisher.DriftEntry(publisher.DriftClass.INVALID_OR_SEMANTIC, path, origin=origin),
        )
    assert raised.value.__cause__ is cause


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_sync_unreadable_canonical_leaf_names_item_and_cause(discard_case):
    case = discard_case
    leaf = case.project / "config/opencode/AGENTS.md"
    before = discard_tests.roots(case)
    mode = leaf.stat().st_mode & 0o7777
    leaf.chmod(0)
    try:
        result = CliRunner().invoke(app, ["config", "sync"])
    finally:
        leaf.chmod(mode)

    assert result.exit_code == 1, (result.output, result.exception)
    assert isinstance(result.exception, SystemExit)
    problem = next(line for line in result.output.splitlines() if line.startswith("Problem:"))
    remedy = next(line for line in result.output.splitlines() if line.startswith("Remedy:"))
    assert "canonical-publish-failed" in problem
    assert "opencode/AGENTS.md" in problem and "Permission denied" in problem
    assert "readable" in remedy and "writable" in remedy
    assert "available space" in remedy
    assert "a partial update may already be in place" in remedy
    assert "rerun `djinn config sync` and check `djinn config status`" in remedy
    assert discard_tests.roots(case) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_discard_canonical_read_failure_after_backup_barrier_keeps_backups(
    discard_case, monkeypatch
):
    case = discard_case
    discard_tests.drift(case)
    leaf = case.project / "config/opencode/AGENTS.md"
    before = discard_tests.roots(case)
    mode = leaf.stat().st_mode & 0o7777
    real_barrier = storage.BackupDirectories.barrier
    barriers = []

    def barrier(self):
        real_barrier(self)
        barriers.append(True)
        leaf.chmod(0)

    monkeypatch.setattr(storage.BackupDirectories, "barrier", barrier)
    try:
        result = case.invoke()
    finally:
        leaf.chmod(mode)

    assert barriers == [True]
    assert result.exit_code == 1, (result.output, result.exception)
    assert isinstance(result.exception, SystemExit)
    problem = next(line for line in result.output.splitlines() if line.startswith("Problem:"))
    remedy = next(line for line in result.output.splitlines() if line.startswith("Remedy:"))
    assert "canonical-publish-failed" in problem
    assert "opencode/AGENTS.md" in problem and "Permission denied" in problem
    assert "readable" in remedy
    assert "Discard incomplete: failed during canonical." in result.output
    assert "Skipped: codex; runtime repair is incomplete." in result.output
    stamp = discard_tests.backup_stamp(case)
    assert f"Backed up to {stamp}" in result.output
    assert f"Backups were kept in {stamp}." in result.output
    assert (stamp / "config/opencode/AGENTS.md").read_bytes() == b"canonical edit\n"
    assert (stamp / "codex/AGENTS.md").read_bytes() == b"runtime edit\n"
    assert discard_tests.roots(case) == before


@pytest.mark.parametrize("named", [True, False], ids=["filename", "bare-oserror"])
def test_canonical_write_error_text_stays_unchanged(discard_case, named):
    root = discard_case.project / "config"
    error = OSError(errno.ENOSPC, "No space left on device", "unrelated-write") if named \
        else OSError("write unavailable")
    problem = sync._write_failure_audit("claude", root, error).problems[0]
    cause = "No space left on device" if named else "OSError"
    assert problem.message == f"Failed to publish the canonical workflow at {root}: {cause}"
