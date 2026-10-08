from __future__ import annotations

import pytest

from djinn_in_a_box.commands.doctor import Status, hostctl_checks
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import hostctl


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
