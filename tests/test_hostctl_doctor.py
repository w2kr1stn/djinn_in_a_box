from __future__ import annotations

import pytest

from djinn_in_a_box.commands.doctor import Status, hostctl_checks
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import hostctl


@pytest.fixture(autouse=True)
def _isolate_unrelated_doctor_probes(monkeypatch, mock_home):
    from djinn_in_a_box.commands import doctor
    from djinn_in_a_box.core.desktop import DesktopInspection

    monkeypatch.setattr(doctor, "_docker_installed", lambda: False)
    monkeypatch.setattr(
        doctor.docker_core,
        "inspect_running_desktop",
        lambda: DesktopInspection(channels=(), raw_sources=(), raw_verified=True),
    )


@pytest.mark.parametrize("config", [None, "empty", "valid"])
def test_doctor_missing_data_is_unknown_and_never_starts(tmp_path, monkeypatch, config):
    if config == "empty":
        config = AppConfig(code_dir=tmp_path)
    elif config == "valid":
        config = AppConfig(
            code_dir=tmp_path,
            hostctl={"hosts": {"host-a": {"address": "host-a.example.ts.net", "user": "operator"}}},
        )
    monkeypatch.setattr(
        hostctl, "open_window", lambda *a, **k: pytest.fail("doctor started helper")
    )
    monkeypatch.setattr(hostctl, "command", lambda *a, **k: pytest.fail("doctor invoked Docker"))
    rows = hostctl_checks(config, daemon=False)
    assert {r.name for r in rows} == {
        "Hostctl configuration",
        "Hostctl window",
        "Hostctl helper",
        "Hostctl node",
        "Hostctl journal",
        "Hostctl peer trust",
        "Hostctl relay",
        "Hostctl sealing",
        "Hostctl direct probe",
    }
    assert all(r.status is Status.WARN for r in rows[1:])


def test_doctor_reports_needs_login_without_auth_url_or_sealing_claim(tmp_path, monkeypatch):
    config = AppConfig(code_dir=tmp_path)
    monkeypatch.setattr(
        hostctl,
        "snapshot",
        lambda: {
            "state": "opening",
            "helper": "helper-a",
            "running": True,
            "window": {"deadline": "2026-01-01T00:00:00Z"},
            "node": {"BackendState": "NeedsLogin", "AuthURL": "https://example.invalid/login"},
        },
    )
    rows = hostctl_checks(config, daemon=True)
    details = " ".join(row.detail for row in rows)
    assert "NeedsLogin" in details and "helper-owned deadline" in details
    assert "unknown: assessment unavailable" in details
    assert next(r for r in rows if r.name == "Hostctl relay").status is Status.WARN
    assert "example.invalid" not in details
    assert next(r for r in rows if r.name == "Hostctl node").status is Status.WARN


def test_doctor_inspection_errors_never_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hostctl,
        "snapshot",
        lambda: (_ for _ in ()).throw(hostctl.HostctlError("daemon unavailable")),
    )
    rows = hostctl_checks(AppConfig(code_dir=tmp_path), daemon=True)
    assert all(row.status is Status.WARN and "unknown" in row.detail for row in rows[1:])


def test_doctor_per_item_rows(monkeypatch):
    from typer.testing import CliRunner

    from djinn_in_a_box.cli.djinn import app
    from djinn_in_a_box.commands import doctor
    from djinn_in_a_box.config import loader
    from djinn_in_a_box.core import host_sealing as sealing

    pair = ("/workspace", "/delivery")
    assessment = sealing.Assessment(
        "dev-a",
        (
            sealing.Finding("first individual cause", pair, "cause", "one"),
            sealing.Finding("second individual cause", pair, "cause", "two"),
        ),
        (
            sealing.Finding("first individual unknown", pair, "unknown", "one"),
            sealing.Finding("second individual unknown", pair, "unknown", "two"),
        ),
    )
    snapshot = {
        "sealing_causes": assessment.causes,
        "sealing_errors": assessment.errors,
        "sealing_cause_details": assessment.cause_details,
        "sealing_error_details": assessment.error_details,
    }
    checks = doctor.hostctl_boundary_checks(snapshot, None)
    assert [(row.name, row.status, row.detail) for row in checks[:4]] == [
        ("Hostctl sealing cause 1", Status.FAIL, "first individual cause"),
        ("Hostctl sealing cause 2", Status.FAIL, "second individual cause"),
        ("Hostctl sealing unknown 1", Status.WARN, "first individual unknown"),
        ("Hostctl sealing unknown 2", Status.WARN, "second individual unknown"),
    ]
    grouped_only = doctor.hostctl_boundary_checks(
        {"sealing_causes": assessment.causes, "sealing_errors": assessment.errors}, None
    )
    assert grouped_only[0].name == "Hostctl sealing" and grouped_only[0].status is Status.WARN
    monkeypatch.setattr(loader, "load_config", lambda: None)
    monkeypatch.setattr(doctor, "run_checks", lambda *args: checks)
    monkeypatch.setattr(doctor.console, "width", 180)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    for row in checks[:4]:
        line = next(line for line in result.output.splitlines() if row.name in line)
        assert row.status.value.upper() in line and row.detail in line
    assert all(line not in result.output for line in (*assessment.causes, *assessment.errors))


@pytest.mark.parametrize("state", ["none", "kept", "bad-directory"])
@pytest.mark.parametrize("config_present", [False, True])
def test_postmortem_doctor_row(tmp_path, monkeypatch, state, config_present):
    from djinn_in_a_box.commands import doctor
    from djinn_in_a_box.core import post_mortem

    root = post_mortem.records_root(create=False)
    newest = None
    if state != "none":
        root.mkdir(mode=0o700)
        if state == "bad-directory":
            root.chmod(0o755)
        else:
            for stamp in ("120000000000", "120001000000"):
                newest = root / f"20261018T{stamp}Z-clean-123456789abc.json"
                newest.write_text("contents must never be read [red]secret[/red]")
                newest.chmod(0o600)
    from djinn_in_a_box.core import host_runtime

    monkeypatch.setattr(host_runtime, "inspect_object", lambda *a, **kw: None)
    monkeypatch.setattr(doctor, "_docker_installed", lambda: False)
    config = AppConfig(code_dir=tmp_path) if config_present else None
    row = next(r for r in doctor.run_checks(config) if r.name == "Post-mortem records")
    assert row.status is (Status.WARN if state == "bad-directory" else Status.PASS)
    assert row.detail == {"none": "none", "kept": f"2 kept; latest {newest}",
                          "bad-directory": "directory"}[state]
    assert "secret" not in row.detail


def test_postmortem_doctor_path_is_literal(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from djinn_in_a_box.cli.djinn import app
    from djinn_in_a_box.commands import doctor
    from djinn_in_a_box.core import post_mortem

    path = tmp_path / "[red]literal[/red]\n\t.json"
    monkeypatch.setattr(doctor.console, "width", 240)
    monkeypatch.setattr(post_mortem, "latest", lambda: (1, path, None))
    from djinn_in_a_box.core import host_runtime

    monkeypatch.setattr(host_runtime, "inspect_object", lambda *a, **kw: None)
    monkeypatch.setattr(doctor, "_docker_installed", lambda: False)
    row = next(r for r in doctor.run_checks(None) if r.name == "Post-mortem records")
    monkeypatch.setattr(doctor, "run_checks", lambda *a: [row])
    result = CliRunner().invoke(app, ["doctor"], terminal_width=240)
    assert result.exit_code == 0
    assert row.detail == f"1 kept; latest {tmp_path}/[red]literal[/red]\\n\\t.json"
    assert "\n" not in row.detail and "\t" not in row.detail
    assert "[red]literal[/red]\\n\\t.json" in result.output
    assert "secret" not in result.output


@pytest.mark.parametrize("read_error", [False, True])
def test_doctor_journal_readability_with_current_observation(tmp_path, monkeypatch, read_error):
    from djinn_in_a_box.core import host_runtime

    journal = hostctl.state_root() / "journal.jsonl"
    journal.write_text("{}\n")
    original = type(journal).open

    def opened(path, *args, **kwargs):
        if read_error and path == journal and args == ("rb",):
            raise PermissionError("journal is unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(journal), "open", opened)
    monkeypatch.setattr(host_runtime, "process_token", lambda pid: "token")
    monkeypatch.setattr(hostctl, "snapshot", lambda: {
        "helper": "helper-a", "state": "open", "observation": {
            "time": host_runtime.time.time(), "observer_pid": 1, "observer_token": "token",
        },
    })
    row = next(r for r in hostctl_checks(None, daemon=True) if r.name == "Hostctl journal")
    assert row.status is (Status.WARN if read_error else Status.PASS)
    assert "journal is unreadable" in row.detail if read_error else "observer current" in row.detail


@pytest.mark.parametrize("failure_first", [False, True])
def test_doctor_tail_opening_anchors_current_generation(tmp_path, monkeypatch, failure_first):
    root = hostctl.state_root()
    hostctl.journal("on", generation="old-A")
    hostctl.journal("diagnostic", detail="x" * 80000)
    hostctl.journal("on", generation="current-B")
    if failure_first:
        hostctl.journal(
            "relay-close-failed", generation="current-B", attempt=3, first="start",
            time="current",
        )
    hostctl.journal("relay-close-failed", generation="old-A", attempt=9, time="old")
    hostctl.journal("relay-closed", generation="old-A")
    if not failure_first:
        hostctl.journal(
            "relay-close-failed", generation="current-B", attempt=3, first="start",
            time="current",
        )
    tail = (root / "journal.jsonl").read_bytes()[-65536:]
    assert b'"event": "on", "generation": "old-A"' not in tail
    row = next(r for r in hostctl_checks(None, daemon=False) if r.name == "Hostctl journal")
    assert row.status is Status.FAIL and "failed 3× since start, last noted current" in row.detail
    assert "unverifiable" in row.detail


def test_doctor_last_opening_selects_existing_generation_state(tmp_path):
    hostctl.journal("diagnostic", generation="current-B")
    hostctl.journal("on", generation="old-A")
    hostctl.journal(
        "relay-close-failed", generation="current-B", attempt=2, first="start",
        time="current",
    )
    hostctl.journal("on", generation="current-B")
    hostctl.journal("relay-close-failed", generation="old-A", attempt=9)
    hostctl.journal("off", generation="old-A")
    row = next(r for r in hostctl_checks(None, daemon=False) if r.name == "Hostctl journal")
    assert row.status is Status.FAIL and "failed 2× since start, last noted current" in row.detail


def test_doctor_without_opening_uses_first_seen_generation_order(tmp_path):
    hostctl.journal("relay-close-failed", generation="old-A", attempt=9)
    hostctl.journal(
        "relay-close-failed", generation="current-B", attempt=2, first="start",
        time="current",
    )
    hostctl.journal("relay-close-failed", generation="old-A", attempt=10)
    hostctl.journal("relay-closed", generation="old-A")
    row = next(r for r in hostctl_checks(None, daemon=False) if r.name == "Hostctl journal")
    assert row.status is Status.FAIL and "failed 2× since start, last noted current" in row.detail


@pytest.mark.parametrize("fields", [{}, {"attempt": "invalid"}])
def test_doctor_invalid_attempt_metadata_warns(tmp_path, fields):
    hostctl.journal("relay-close-failed", generation="generation-a", **fields)
    row = next(r for r in hostctl_checks(None, daemon=False) if r.name == "Hostctl journal")
    assert row.status is Status.WARN and row.detail.startswith("unknown:")
