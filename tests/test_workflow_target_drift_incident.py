"""Literal D7 replay. Docker-facing calls are faked; publisher operations are real."""

from __future__ import annotations

import errno
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from test_workflow_discard import (
    backup_stamp,
    discard_case,  # noqa: F401
    replace_file,
    roots,
)
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import container
from djinn_in_a_box.core import config_sync as sync
from djinn_in_a_box.core import config_workflow as workflow
from djinn_in_a_box.core import workflow_publisher
from djinn_in_a_box.core.docker import RunResult
from djinn_in_a_box.core.session import parse_publisher_drift_items
from djinn_in_a_box.core.workflow_publisher import RUNTIME_MANIFEST_NAME, DriftClass

ROOT = Path(__file__).resolve().parents[1]


def publisher(case, target, *, check=False):
    argv = [
        sys.executable, str(Path(workflow_publisher.__file__)),
        "--view", str(case.project / "config/opencode"),
        "--canonical-root", str(case.project / "config"),
        "--target", str(target), "--manifest", str(target / RUNTIME_MANIFEST_NAME),
        "--ignore", "opencode.json", "--profile", "opencode",
    ]
    if check:
        argv.append("--check")
    return subprocess.run(argv, capture_output=True, text=True, check=False,
                          stdin=subprocess.DEVNULL, timeout=5)


def commands(text):
    return re.findall(r"`(djinn [^`]+)`", text)


@pytest.mark.parametrize("host_side_canonical_edit", [False, True])
def test_four_copy_incident_follow_printed_remedy(request, monkeypatch,
                                                host_side_canonical_edit):
    case = request.getfixturevalue("discard_case")
    # Step 1: fixture ran config sync and start's Codex delivery. Run the actual
    # container entrypoint-equivalent publisher CLI for the fourth visible copy.
    opencode = case.tmp / "container-opencode"
    opencode.mkdir()
    initial = publisher(case, opencode)
    assert initial.returncode == 0, initial.stderr
    assert (opencode / "AGENTS.md").read_bytes() == b"original\n"

    # Step 2: agent attempts the four writes in incident order.
    (case.project / "config/claude/AGENTS.md").write_bytes(b"wanted source edit\n")
    codex = case.runtime / "AGENTS.md"
    assert codex.stat().st_mode & 0o777 == 0o444
    if os.geteuid() != 0:
        with pytest.raises(PermissionError) as blocked:
            codex.write_bytes(b"in-place edit must fail\n")
        assert blocked.value.errno == errno.EACCES
    replace_file(codex, b"agent Codex edit\n")
    codex.chmod(0o444)  # Claude-style rename can replace even a sealed inode.

    # The seed write is blocked by the parsed Compose :ro contract and is NOT
    # applied here. Dynamic EROFS proof belongs to the isolated live check.
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    mounts = compose["x-common-volumes"]
    seed_binds = [entry.split(":") for entry in mounts if isinstance(entry, str)
                  and entry.split(":")[1] == "/home/dev/.opencode/seed"]
    assert seed_binds == [["./config/opencode", "/home/dev/.opencode/seed", "ro"]]
    seed = case.project / "config/opencode/AGENTS.md"
    assert seed.read_bytes() == b"original\n"
    replace_file(opencode / "AGENTS.md", b"agent OpenCode edit\n")
    (opencode / "AGENTS.md").chmod(0o444)

    checked = publisher(case, opencode, check=True)
    assert checked.returncode == 11, checked.stderr
    diagnostic = parse_publisher_drift_items(checked.stderr)
    assert diagnostic is not None
    assert "opencode-runtime:AGENTS.md" in diagnostic.labels

    # Step 5 repeats steps 2–4 with the pre-fix mix: this additional canonical
    # edit is HOST-SIDE, never a write through the read-only container bind.
    if host_side_canonical_edit:
        seed.write_bytes(b"host canonical edit\n")

    # Step 3: status and start preflight name the drift and leave every canonical
    # and runtime byte/manifest unchanged. Docker discovery/image checks are faked
    # in discard_case, not assertions about a real daemon or image.
    status = CliRunner().invoke(app, ["config", "status"])
    assert status.exit_code == 1, status.output
    label = f"codex:AGENTS.md ({case.runtime})"
    assert label in status.output
    if host_side_canonical_edit:
        assert "config/opencode/AGENTS.md" in status.output
    else:
        audit = sync.audit_config_sync(case.project)
        assert audit.drift_classes == (DriftClass.SOURCE_CHANGED,)
    before = roots(case)
    refused = workflow.prepare_config_workflow(
        case.project, workflow.start_workflow_targets(case.config), require_compose_host_env=True,
    )
    assert not refused.success
    refusal = "\n".join(f"{p.message} Remedy: {p.remedy}" for p in refused.problems)
    assert ("config/opencode/AGENTS.md" if host_side_canonical_edit else label) in refusal
    assert roots(case) == before
    if host_side_canonical_edit:
        # Immutable publisher checks seed consistency before inspecting runtime.
        # Runtime drift was proven above, before this additional host-side edit.
        checked = publisher(case, opencode, check=True)
        assert checked.returncode == 10, checked.stderr
        diagnostic = parse_publisher_drift_items(checked.stderr)
        assert diagnostic is not None
        assert "config/opencode/AGENTS.md (seed)" in diagnostic.labels

    # Step 4: extract commands; run exactly the printed argv, in printed order.
    # First show discard's stopped-container precondition and extract its clean.
    case.running = ["djinn-incident-test"]
    running_refusal = case.invoke()
    assert running_refusal.exit_code == 1
    assert commands(running_refusal.output) == ["djinn clean"]
    assert roots(case) == before
    printed = commands(refusal)
    assert printed == ["djinn clean", "djinn config sync --discard-target-drift"]
    assert printed[0] == commands(running_refusal.output)[0]
    events = []

    def compose_down():
        events.append("clean")
        case.running = []
        return RunResult(0, "", "")

    monkeypatch.setattr(container, "compose_down", compose_down)
    backup = workflow.backup_target_drift
    synchronize = workflow.sync_config
    deliver = workflow._deliver_target

    def backup_trace(*args, **kwargs):
        if args[2] == case.project / "config":
            assert "preflight_manifest" in kwargs
            events.append("backup-config")
        else:
            events.append("backup-codex")
        return backup(*args, **kwargs)

    def sync_trace(*args, **kwargs):
        events.append("sync-config")
        assert kwargs["accept_drift"] if host_side_canonical_edit else True
        return synchronize(*args, **kwargs)

    def delivery_trace(*args, **kwargs):
        events.append("deliver-codex")
        assert kwargs["accept_drift"]
        return deliver(*args, **kwargs)

    monkeypatch.setattr(workflow, "backup_target_drift", backup_trace)
    monkeypatch.setattr(workflow, "sync_config", sync_trace)
    monkeypatch.setattr(workflow, "_deliver_target", delivery_trace)
    for printed_command in printed:
        argv = shlex.split(printed_command)
        assert argv.pop(0) == "djinn"
        executed = CliRunner().invoke(app, argv)
        assert executed.exit_code == 0, (printed_command, executed.output, executed.exception)
    assert events == ["clean", "backup-config", "backup-codex", "sync-config", "deliver-codex"]
    stamp = backup_stamp(case)
    assert (stamp / "codex/AGENTS.md").read_bytes() == b"agent Codex edit\n"
    if host_side_canonical_edit:
        assert (stamp / "config/opencode/AGENTS.md").read_bytes() == b"host canonical edit\n"
    else:
        assert not (stamp / "config/opencode/AGENTS.md").exists()
    for path in [stamp, *stamp.rglob("*")]:
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    assert seed.read_bytes() == codex.read_bytes() == b"wanted source edit\n"
    assert codex.stat().st_mode & 0o777 == 0o444
    assert (case.runtime / RUNTIME_MANIFEST_NAME).stat().st_mode & 0o777 == 0o444

    # Restore wrappers before testing the ordinary start preflight.
    monkeypatch.setattr(workflow, "sync_config", synchronize)
    monkeypatch.setattr(workflow, "_deliver_target", deliver)
    assert workflow.prepare_config_workflow(
        case.project, workflow.start_workflow_targets(case.config), require_compose_host_env=True,
    ).success
    clean_status = CliRunner().invoke(app, ["config", "status"])
    assert clean_status.exit_code == 0, clean_status.output
    recreated = case.tmp / "recreated-container-opencode"
    assert not recreated.exists()
    recreated.mkdir()
    fresh = publisher(case, recreated)
    assert fresh.returncode == 0, fresh.stderr
    assert publisher(case, recreated, check=True).returncode == 0
    assert (recreated / "AGENTS.md").read_bytes() == b"wanted source edit\n"
