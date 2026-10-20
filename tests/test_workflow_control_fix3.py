"""Control-phase regressions for seed walker I/O and item-less refresh advice."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath

import pytest
import test_workflow_discard as discard_tests
from test_workflow_code_loop_fix1 import doctor_row, run_publisher
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import doctor
from djinn_in_a_box.core import config_sync as sync
from djinn_in_a_box.core import config_workflow as workflow
from djinn_in_a_box.core import session
from djinn_in_a_box.core import workflow_publisher as publisher

discard_case = discard_tests.discard_case


def snapshot(root):
    """Snapshot synthetic roots; credential-like entries contribute metadata only."""
    result = {}
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(parent) / name
            info = path.lstat()
            sensitive = any(
                part in name.lower() for part in ("auth", "cred", "token", "secret", "key")
            ) or name.lower().endswith(".db")
            value = None
            if not sensitive:
                if stat.S_ISLNK(info.st_mode):
                    value = os.readlink(path)
                elif stat.S_ISREG(info.st_mode):
                    value = path.read_bytes()
            result[str(path.relative_to(root))] = info.st_mode, value
    return result


def assert_seed_refusal(case, monkeypatch, checked, seed, target):
    assert checked.returncode == 13, checked.stderr
    assert "workflow publisher: invalid-or-semantic\n" in checked.stderr
    assert "Traceback" not in checked.stderr
    assert publisher.PUBLISHER_WRITE_ERROR_PREFIX not in checked.stderr
    assert publisher.PUBLISHER_LOCK_ERROR_PREFIX not in checked.stderr
    raw = next(line.removeprefix(publisher.PUBLISHER_DRIFT_ITEMS_PREFIX)
               for line in checked.stderr.splitlines()
               if line.startswith(publisher.PUBLISHER_DRIFT_ITEMS_PREFIX))
    payload = json.loads(raw)
    assert payload == {"items": [{
        "class": "invalid-or-semantic", "origin": "seed", "root": str(seed),
        "path": "AGENTS.md", "key": None, "reason": None,
    }], "total": 1, "truncated": False}
    parsed = session.parse_publisher_drift_items(checked.stderr)
    assert parsed is not None and parsed.labels == ("config/opencode/AGENTS.md (seed)",)
    refreshed = session._publisher_refresh_error(checked.stderr)
    row = doctor_row(case.config, monkeypatch, checked)
    assert row.status is doctor.Status.WARN
    assert row.detail == "invalid-or-semantic in config/opencode/AGENTS.md (seed)"
    assert refreshed.startswith("OpenCode workflow refresh failed: invalid-or-semantic in ")
    generic = sync.WORKFLOW_REMEDIES["entrypoint-generic"]
    assert row.remedy == generic and refreshed.endswith("Remedy: " + generic)
    assert "config/opencode/AGENTS.md (seed)" in refreshed
    assert str(target) not in row.detail
    for text in (row.remedy, refreshed):
        assert "natively" not in text and "portable" not in text
        assert "djinn config status" in text and "on the host" in text
        assert "recreate the container" in text


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
@pytest.mark.parametrize("check", [True, False], ids=["check", "publish"])
def test_unsearchable_seed_refusal_reaches_cli_and_consumers(discard_case, monkeypatch, check):
    case = discard_case
    canonical = case.project / "config"
    seed = canonical / "opencode"
    target = case.tmp / "opencode-runtime"
    target.mkdir()
    assert run_publisher(case, target, canonical, check=False).returncode == 0
    before = snapshot(canonical), snapshot(case.config.config_root), snapshot(target)
    mode = seed.stat().st_mode & 0o7777
    seed.chmod(0o400)
    try:
        checked = run_publisher(case, target, canonical, check=check)
    finally:
        seed.chmod(mode)
    assert_seed_refusal(case, monkeypatch, checked, seed, target)
    assert (snapshot(canonical), snapshot(case.config.config_root), snapshot(target)) == before


@pytest.mark.parametrize("check", [True, False], ids=["check", "publish"])
def test_seed_initial_open_eio_reaches_cli_and_consumers(discard_case, monkeypatch, capsys, check):
    case = discard_case
    canonical = case.project / "config"
    seed = canonical / "opencode"
    target = case.tmp / "opencode-runtime"
    target.mkdir()
    assert run_publisher(case, target, canonical, check=False).returncode == 0
    before = snapshot(canonical), snapshot(case.config.config_root), snapshot(target)
    real_open = publisher.os.open
    seed_info = seed.stat()
    hits = []

    def fail_open(name, flags, *args, **kwargs):
        parent = kwargs.get("dir_fd")
        if (name == "AGENTS.md" and flags & os.O_PATH and parent is not None
                and os.path.samestat(os.fstat(parent), seed_info)):
            hits.append(name)
            raise OSError(errno.EIO, "synthetic seed initial-open I/O failure")
        return real_open(name, flags, *args, **kwargs)

    argv = ["--view", str(seed), "--canonical-root", str(canonical),
            "--target", str(target), "--manifest", str(target / publisher.RUNTIME_MANIFEST_NAME),
            "--ignore", "opencode.json", "--profile", "opencode"]
    if check:
        argv.append("--check")
    with monkeypatch.context() as patch:
        patch.setattr(publisher.os, "open", fail_open)
        capsys.readouterr()
        code = publisher.main(argv)
        output = capsys.readouterr()
    assert hits == ["AGENTS.md"] and output.out == ""
    checked = subprocess.CompletedProcess(argv, code, output.out, output.err)
    assert_seed_refusal(case, monkeypatch, checked, seed, target)
    assert (snapshot(canonical), snapshot(case.config.config_root), snapshot(target)) == before


@pytest.mark.parametrize("stage", ["stat", "initial-open"])
def test_view_entry_io_keeps_relative_path_and_cause(tmp_path, monkeypatch, stage):
    child = tmp_path / "nested"
    child.mkdir()
    (child / "AGENTS.md").write_bytes(b"synthetic instructions\n")
    child_info = child.stat()
    operation = publisher.os.stat if stage == "stat" else publisher.os.open
    cause = OSError(errno.EIO, "synthetic view I/O failure")

    def fail(name, *args, **kwargs):
        parent = kwargs.get("dir_fd")
        if (name == "AGENTS.md" and parent is not None
                and os.path.samestat(os.fstat(parent), child_info)):
            raise cause
        return operation(name, *args, **kwargs)

    monkeypatch.setattr(publisher.os, "stat" if stage == "stat" else "open", fail)
    with pytest.raises(publisher.PublishError) as raised:
        publisher.snapshot_file_view(tmp_path, source="claude", item_origin="seed")
    error = raised.value
    assert error.drift_class is publisher.DriftClass.INVALID_OR_SEMANTIC
    assert error.items == (publisher.DriftEntry(
        publisher.DriftClass.INVALID_OR_SEMANTIC, PurePosixPath("nested/AGENTS.md"), origin="seed"
    ),)
    assert isinstance(error.__cause__, publisher._ViewEntryError)
    assert error.__cause__.relative == PurePosixPath("nested/AGENTS.md")
    assert error.__cause__.__cause__ is cause


@pytest.mark.parametrize("stage", ["stat", "initial-open"])
def test_vanished_view_entry_still_refuses(tmp_path, monkeypatch, stage):
    (tmp_path / "AGENTS.md").write_bytes(b"synthetic instructions\n")
    operation = publisher.os.stat if stage == "stat" else publisher.os.open

    def vanish(name, *args, **kwargs):
        if name == "AGENTS.md" and kwargs.get("dir_fd") is not None:
            raise FileNotFoundError(errno.ENOENT, "synthetic vanished entry")
        return operation(name, *args, **kwargs)

    monkeypatch.setattr(publisher.os, "stat" if stage == "stat" else "open", vanish)
    with pytest.raises(publisher.PublishError) as raised:
        publisher.snapshot_file_view(tmp_path, source="claude", item_origin="seed")
    assert raised.value.drift_class is publisher.DriftClass.INVALID_OR_SEMANTIC
    assert raised.value.items == (publisher.DriftEntry(
        publisher.DriftClass.INVALID_OR_SEMANTIC, PurePosixPath("AGENTS.md"), origin="seed"
    ),)


@pytest.mark.parametrize("envelope", ["empty", "missing", "malformed"])
@pytest.mark.parametrize(
    "kind", ["invalid-or-semantic", "collision", "target-drift", "source-changed"]
)
def test_itemless_refresh_uses_generic_except_source_changed(
    discard_case, monkeypatch, kind, envelope
):
    stderr = f"workflow publisher: {kind}\n"
    if envelope != "missing":
        payload = '{"items":[],"total":0,"truncated":false}' if envelope == "empty" else "{"
        stderr += publisher.PUBLISHER_DRIFT_ITEMS_PREFIX + payload
    refreshed = session._publisher_refresh_error(stderr)
    remedy = sync.WORKFLOW_REMEDIES[
        "source-changed" if kind == "source-changed" else "entrypoint-generic"
    ]
    assert refreshed == f"OpenCode workflow refresh failed: {kind}. Remedy: {remedy}"
    checked = subprocess.CompletedProcess([], publisher.EXIT_CODES[publisher.DriftClass(kind)],
                                          "", stderr)
    row = doctor_row(discard_case.config, monkeypatch, checked)
    assert row.status is doctor.Status.WARN and row.detail == "check unavailable"


@pytest.mark.parametrize("scope", ["start", "host"])
@pytest.mark.parametrize("kind", list(publisher.DriftClass))
def test_itemless_other_scopes_keep_existing_remedy(scope, kind):
    key = "host-target-drift" if scope == "host" and kind is publisher.DriftClass.TARGET_DRIFT \
        else kind.value
    assert sync.workflow_remedy(kind, "claude", scope=scope) \
        == sync.WORKFLOW_REMEDIES[key].format(source="claude")


def test_clean_opencode_still_has_no_remedy():
    assert sync.workflow_remedy(publisher.DriftClass.CLEAN, "claude", scope="opencode") == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_host_canonical_walk_failures_keep_classes_exits_and_advice(discard_case):
    case = discard_case
    canonical = case.project / "config"
    source = canonical / "claude"
    before = snapshot(canonical), snapshot(case.config.config_root)
    mode = source.stat().st_mode & 0o7777
    source.chmod(0o400)
    try:
        for read in (lambda: publisher.snapshot_file_view(source, source="claude"),
                     lambda: sync._fingerprint(source)):
            with pytest.raises(publisher.PublishError) as raised:
                read()
            assert raised.value.drift_class is publisher.DriftClass.INVALID_OR_SEMANTIC
            assert raised.value.items == ()
        with pytest.raises(sync._BuildError) as build_error:
            sync._snapshot_build(case.project, "claude")
        assert build_error.value.drift is publisher.DriftClass.INVALID_OR_SEMANTIC
        with pytest.raises(publisher.PublishError) as raised:
            publisher.fingerprint_source_inputs(source)
        assert raised.value.drift_class is publisher.DriftClass.SOURCE_CHANGED
        assert raised.value.items == ()
        audit = sync.audit_config_sync(case.project)
        delivery = sync.load_canonical_delivery_view(case.project, "codex")
        inspections = workflow.inspect_workflow_targets(case.project, case.config)
        status = CliRunner().invoke(app, ["config", "status"])
        synced = CliRunner().invoke(app, ["config", "sync"])
    finally:
        source.chmod(mode)
    assert audit.drifts[0].kind is publisher.DriftClass.INVALID_OR_SEMANTIC
    assert not delivery.success and delivery.audit.drifts[0].kind is audit.drifts[0].kind
    assert inspections[0].problem.identifier == "runtime-not-checked"
    assert inspections[0].problem.message == "not checked (invalid-or-semantic)"
    assert inspections[0].problem.remedy == sync.CANONICAL_REMEDY
    for result in (status, synced):
        assert result.exit_code == 1, (result.output, result.exception)
        assert isinstance(result.exception, SystemExit)
        assert "invalid-or-semantic" in result.output
        assert "natively" in result.output
    assert (snapshot(canonical), snapshot(case.config.config_root)) == before
