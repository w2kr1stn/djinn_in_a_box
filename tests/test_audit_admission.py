"""Audit admission shares lifecycle exclusion; its runtime only holds a session lease."""

from __future__ import annotations

import itertools
import json
import os
import select
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import container
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import assistant, hostctl
from djinn_in_a_box.core import opencode_credentials as oc
from djinn_in_a_box.core.docker import RunResult


@pytest.fixture
def audit_host(tmp_path, monkeypatch, mock_home):
    config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "config")
    carrier = config.config_root / "opencode"
    carrier.mkdir(parents=True, mode=0o700)
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    monkeypatch.setattr(container, "load_config", lambda: config)
    monkeypatch.setattr(container, "compose_down", lambda: RunResult(0))
    monkeypatch.setattr(container, "get_sync_paths_to_clear_by_category", lambda *args: [])
    monkeypatch.setattr(
        container, "get_existing_volumes_by_category", lambda *args: ["djinn-cache"]
    )
    monkeypatch.setattr(container, "delete_volumes", lambda names: dict.fromkeys(names, True))
    monkeypatch.setattr(container, "network_exists", lambda *args: False)
    monkeypatch.setattr(container.host_runtime, "inspect_dev", lambda *args: None)
    monkeypatch.setattr(hostctl, "delete_state_locked", lambda: None)
    monkeypatch.setattr(assistant, "container_exists", lambda _: False)
    monkeypatch.setattr(container, "recover_leftover_sessions", assistant.recover_leftover_sessions)
    monkeypatch.setattr(container, "export_opencode_data_credentials", lambda: b"[]")
    return config, carrier


def clean_args(command):
    return ["clean", command, "--force", *(["--credentials"] if command == "volumes" else [])]


def session_paths():
    root = Path(oc.default_audit_root())
    return set(root.iterdir()) if root.exists() else set()


def fast_control_clock(monkeypatch):
    ticks = itertools.count(step=8)
    monkeypatch.setattr(
        hostctl, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None)
    )


def recover(config, session=None):
    return oc.recover_audit_sessions(
        None,
        config.config_root / "opencode",
        lambda _: b"[]",
        lambda _: False,
        own_session=session,
    )


@pytest.mark.parametrize("command", ["volumes", "all"])
def test_admission_during_logout_export_is_refused_without_creating_session(
    audit_host, monkeypatch, command
):
    config, carrier = audit_host
    fast_control_clock(monkeypatch)
    attempts = []

    def export():
        assert hostctl._held.active
        before = session_paths()

        def admit():
            try:
                session = assistant.open_audit_session(config)
            except assistant.AssistantError as error:
                attempts.append(error)
            else:
                attempts.append(session)
                oc._write_io_file(session, "final.json", payload("session-only"))
                session.close()

        thread = threading.Thread(target=admit, daemon=True)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive(), "admission did not reach its bounded refusal"
        assert len(attempts) == 1
        assert isinstance(attempts[0], assistant.AssistantError)
        assert "host control is busy" in str(attempts[0])
        assert session_paths() == before
        return b"[]"

    monkeypatch.setattr(container, "export_opencode_data_credentials", export)
    result = CliRunner().invoke(app, clean_args(command))
    assert result.exit_code == 0, (result.output, result.exception)
    assert len(attempts) == 1
    assert recover(config).recovered == []
    assert not (carrier / "credentials.json").exists()
    assert not oc.sessions_in_use()


@pytest.mark.parametrize("command", ["volumes", "all"])
def test_audit_admitted_before_logout_refuses_by_lease(audit_host, monkeypatch, command):
    config, _ = audit_host
    calls = []
    monkeypatch.setattr(container, "export_opencode_data_credentials", lambda: calls.append(True))
    with assistant.open_audit_session(config) as session:
        assert not hostctl._held.active
        result = CliRunner().invoke(app, clean_args(command))
        assert result.exit_code == 1, result.output
        assert "Audit sessions are in use" in result.output
        assert session.id in result.output
        assert "host control is busy" not in result.output
        assert session.held
        assert not calls


def payload(identity):
    return json.dumps(
        [
            {
                "id": identity,
                "integrationID": "provider",
                "label": "fixture",
                "active": True,
                "value": {"type": "key", "key": os.urandom(16).hex()},
            }
        ]
    ).encode()


@contextmanager
def child_owner(config):
    """A real admitted session whose owner exits by stdin EOF, without finalization."""
    code = """
import json, os, sys
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import assistant, opencode_credentials as oc
s = assistant.open_audit_session(AppConfig(code_dir=sys.argv[1], config_root=sys.argv[2]))
oc._write_io_file(s, 'final.json', json.dumps([{
    'id': 'session-only', 'integrationID': 'provider', 'label': 'fixture', 'active': True,
    'value': {'type': 'key', 'key': os.urandom(16).hex()},
}]).encode())
print(s.id, flush=True)
sys.stdin.read()
os._exit(17)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(config.code_dir), str(config.config_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert select.select([process.stdout], [], [], 5)[0], "session owner did not start"
        identity = process.stdout.readline().strip()
        assert len(identity) == 32
        yield process, identity
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()


@pytest.mark.parametrize("command", ["volumes", "all"])
def test_owner_death_at_first_check_recovers_before_tombstones(audit_host, monkeypatch, command):
    config, carrier = audit_host
    check = container._refuse_running_audit_sessions
    recovery = container.recover_leftover_sessions
    checks = []
    recovered = []
    with child_owner(config) as (child, identity):
        assert [item[0] for item in oc.sessions_in_use()] == [identity]

        def owner_dies_at_check():
            # Reproduce death at the refusal sample: recovery-first would skip the lease,
            # then this boundary would release it too late to include its final export.
            checks.append(True)
            if command == "all" and len(checks) == 1:
                # Isolate the logout body's race from the redundant pre-compose check;
                # the running-audit refusal test covers that earlier check separately.
                return
            if child.poll() is None:
                child.stdin.close()
                assert child.wait(timeout=5) == 17
            check()

        def recovery_after_check(*args, **kwargs):
            recovery(*args, **kwargs)
            path = carrier / "credentials.json"
            entries = oc.parse_entries(path.read_bytes()) if path.exists() else []
            recovered.append(
                (
                    not Path(oc.default_audit_root(), identity).exists(),
                    "session-only" in {entry.id for entry in entries},
                )
            )

        monkeypatch.setattr(container, "_refuse_running_audit_sessions", owner_dies_at_check)
        monkeypatch.setattr(container, "recover_leftover_sessions", recovery_after_check)
        result = CliRunner().invoke(app, clean_args(command))
        assert result.exit_code == 0, (result.output, result.exception)
        # A recovery-first mutation lets the owner die after being skipped, then succeeds
        # at logout. This real later recovery must never republish its session-only login.
        assert recover(config).recovered == []
        assert len(checks) == (3 if command == "all" else 2)
        assert recovered == [(True, True)]
        assert "session-only" in json.loads((carrier / "credentials.removed.json").read_bytes())
        assert not (carrier / "credentials.json").exists()


@pytest.fixture
def audit_start(audit_host, tmp_path, monkeypatch):
    config, _ = audit_host
    (tmp_path / "assistant").mkdir()
    (tmp_path / "assistant/audit-briefing.md").write_text("Inspect the installation.")
    (tmp_path / "docker.sock").touch()
    monkeypatch.setattr(assistant, "audit_config", lambda: (config, ""))
    monkeypatch.setattr(assistant, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        assistant, "local_daemon", lambda: (tmp_path / "docker.sock", "linux/amd64")
    )
    monkeypatch.setattr(assistant, "session_mounts", lambda *args: [])
    monkeypatch.setattr(assistant, "ensure_image", lambda *args: "fixture-image")
    monkeypatch.setattr(assistant, "ensure_network", lambda *args: True)
    monkeypatch.setattr(assistant, "stop_container", lambda *args: None)
    monkeypatch.setattr(assistant, "cleanup", lambda *args: None)
    monkeypatch.setattr(assistant, "is_background_process_group", lambda: False)
    monkeypatch.setattr(
        assistant,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(isatty=lambda: True),
            stdout=SimpleNamespace(isatty=lambda: True),
        ),
    )
    return config


def test_audit_releases_admission_guard_before_container_run(audit_start, monkeypatch):
    reached = []

    def run(argv, **kwargs):
        assert not hostctl._held.active
        assert len(oc.sessions_in_use()) == 1
        reached.append(True)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    result = CliRunner().invoke(app, ["audit", "--agent", "opencode"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert reached == [True]


def test_running_audit_allows_cache_cleanup_and_refuses_global_logout_by_lease(
    audit_start, monkeypatch
):
    fast_control_clock(monkeypatch)
    outcomes = []

    def run(argv, **kwargs):
        assert len(oc.sessions_in_use()) == 1

        # A different thread represents an independent CLI; a reentrant check on this
        # thread would hide a control lock accidentally retained by the audit.
        def clean():
            started = time.monotonic()
            cache = CliRunner().invoke(app, ["clean", "volumes", "--cache", "--force"])
            logout = CliRunner().invoke(app, ["clean", "volumes", "--credentials", "--force"])
            outcomes.append((cache, logout, time.monotonic() - started))

        thread = threading.Thread(target=clean, daemon=True)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive(), "lifecycle commands waited on the audit runtime"
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    result = CliRunner().invoke(app, ["audit", "--agent", "opencode"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert len(outcomes) == 1
    cache, logout, elapsed = outcomes[0]
    assert elapsed < 2
    assert cache.exit_code == 0, cache.output
    assert "Deleted: djinn-cache" in cache.output
    assert logout.exit_code == 1, logout.output
    assert "Audit sessions are in use" in logout.output
    assert "host control is busy" not in cache.output + logout.output


def test_admission_creation_and_seed_hold_reentrant_control(audit_host, monkeypatch):
    config, _ = audit_host
    reached = []
    for name in ("create_audit_session", "materialize_seed", "write_seed"):
        original = getattr(oc, name)

        def guarded(*args, _name=name, _original=original, **kwargs):
            assert hostctl._held.active
            reached.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(oc, name, guarded)
    with hostctl.control_guard():
        descriptor = hostctl.held_control_descriptor()
        with assistant.open_audit_session(config):
            assert hostctl.held_control_descriptor() == descriptor
    assert reached == ["create_audit_session", "materialize_seed", "write_seed"]
    assert not hostctl._held.active


@pytest.mark.parametrize("error", [hostctl.HostctlError("state refused"), OSError(13, "denied")])
def test_admission_guard_errors_create_nothing(audit_host, monkeypatch, error):
    config, _ = audit_host

    @contextmanager
    def refused():
        raise error
        yield

    before = session_paths()
    monkeypatch.setattr(hostctl, "control_guard", refused)
    with pytest.raises(assistant.AssistantError, match="Could not start the OpenCode audit:"):
        assistant.open_audit_session(config)
    assert session_paths() == before


def test_audit_busy_deadline_reports_real_control_holder(audit_start, monkeypatch):
    from test_restore_lock_holders import flock_holder

    path = hostctl.state_root() / "control.lock"
    path.touch(mode=0o600)
    before = session_paths()
    with flock_holder(path) as child:
        fast_control_clock(monkeypatch)
        result = CliRunner().invoke(app, ["audit", "--agent", "opencode"])
        assert result.exit_code == 1, result.output
        assert "Could not start the OpenCode audit: host control is busy" in result.output
        assert f"held by PID {child.pid} (restore-writer) since " in result.output
        assert session_paths() == before
