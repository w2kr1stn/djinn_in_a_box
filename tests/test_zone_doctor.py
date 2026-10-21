from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from djinn_in_a_box.commands import doctor as doctor_mod
from djinn_in_a_box.config import zones as zones_mod
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import load_zone_assignments
from djinn_in_a_box.core.docker import ensure_zone_roots, resolve_zone_roots

# Every test here runs the doctor; none may depend on a running `djinn` container (#194).
pytestmark = pytest.mark.usefixtures("quiet_doctor")
_REAL_OPENCODE_RUNTIME_CHECK = doctor_mod._opencode_runtime_check


@pytest.fixture(autouse=True)
def _isolate_credential_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_home: Path
) -> None:
    from djinn_in_a_box.core import opencode_credentials as oc

    monkeypatch.setattr(
        oc, "default_audit_root", lambda: str(tmp_path / "home/.local/state/djinn/opencode-audit")
    )
    monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: False)
    # run_checks inspects the desktop helpers unconditionally; keep these tests off the host daemon.
    from djinn_in_a_box.core.desktop import DesktopInspection

    monkeypatch.setattr(
        doctor_mod.docker_core,
        "inspect_running_desktop",
        lambda: DesktopInspection(channels=(), raw_sources=(), raw_verified=True),
    )


@pytest.fixture
def zone_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(zones_mod, "ZONES_FILE", tmp_path / "zones.toml")
    return AppConfig(code_dir=projects, config_root=tmp_path / "config")


def _check_named(checks: list[doctor_mod.Check], name: str) -> doctor_mod.Check:
    return next(check for check in checks if check.name == name)


def test_doctor_tests_cannot_reach_the_real_docker_cli(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_mod, "_opencode_runtime_check", _REAL_OPENCODE_RUNTIME_CHECK)
    # The first call of the real row is a read-only `docker container inspect`; it must be refused
    # too, not only the later `docker exec`.
    with pytest.raises(AssertionError, match="doctor test reached the real Docker CLI") as refused:
        doctor_mod.run_checks(zone_config)
    assert "'container', 'inspect'" in str(refused.value)
    assert "'exec'" not in str(refused.value)


def test_drift_accounts_for_intermediate_assignment_segments(zone_config: AppConfig) -> None:
    roots = resolve_zone_roots(zone_config)
    agent_root = roots.config_root / "claude"
    (agent_root / "plugins").mkdir(parents=True)
    unexpected = agent_root / "new-agent-directory"
    unexpected.mkdir()

    checks = doctor_mod.run_checks(zone_config)

    row = _check_named(checks, "Zone drift")
    assert row.status is doctor_mod.Status.WARN
    assert str(unexpected) in row.detail
    assert str(agent_root / "plugins") not in row.detail


@pytest.mark.parametrize(
    "name",
    ("state_12.sqlite", "state_12.sqlite-wal", "state_12.sqlite-shm", ".sqlite-maintenance.lock"),
)
def test_drift_reports_sqlite_leftovers_in_config_root(zone_config: AppConfig, name: str) -> None:
    roots = resolve_zone_roots(zone_config)
    codex_root = roots.config_root / "codex"
    codex_root.mkdir(parents=True)
    leftover = codex_root / name
    leftover.touch()
    (codex_root / "config.toml").touch()
    unexpected = codex_root / "unrecognized-runtime-file"
    unexpected.touch()

    checks = doctor_mod.run_checks(zone_config)

    row = _check_named(checks, "Zone drift")
    assert row.status is doctor_mod.Status.WARN
    assert str(unexpected) in row.detail
    assert str(leftover) in row.detail
    assert str(codex_root / "config.toml") not in row.detail


def test_doctor_reports_large_direct_files_and_loose_zone_permissions(
    zone_config: AppConfig,
) -> None:
    roots = ensure_zone_roots(zone_config)
    large_file = roots.config_root / "codex" / "logs.sqlite"
    large_file.parent.mkdir(parents=True)
    large_file.write_bytes(b"x" * doctor_mod.LARGE_NON_OVERLAYABLE_FILE_BYTES)
    roots.shared_root.chmod(0o755)

    assignments = load_zone_assignments(zone_config)
    checks = doctor_mod.run_checks(zone_config)
    loose = doctor_mod.loose_credential_dirs(zone_config, assignments)

    large = _check_named(checks, "Large non-overlayable files")
    assert large.status is doctor_mod.Status.WARN
    assert str(large_file) in large.detail
    assert roots.shared_root in loose


def test_doctor_reports_skipped_shipped_default_and_names_conflicting_file(
    zone_config: AppConfig,
) -> None:
    roots = resolve_zone_roots(zone_config)
    conflict = roots.config_root / "claude" / "plugins"
    conflict.parent.mkdir(parents=True)
    conflict.write_text("not a directory")

    checks = doctor_mod.run_checks(zone_config)

    row = _check_named(checks, "Skipped shipped zone defaults")
    assert row.status is doctor_mod.Status.WARN
    assert "claude/plugins/cache" in row.detail
    assert str(conflict) in row.detail
    assert str(conflict) in row.remedy
    assert "move or remove" in row.remedy.lower()


def test_opencode_personal_cli_and_themes_are_known_entries(zone_config: AppConfig) -> None:
    root = zone_config.config_root / "opencode"
    (root / "themes").mkdir(parents=True)
    (root / "themes/night.json").write_text("{}")
    (root / "cli.json").write_text("{}")
    unexpected = root / "unexpected.json"
    unexpected.write_text("{}")
    row = _check_named(doctor_mod.run_checks(zone_config), "Zone drift")
    assert row.status is doctor_mod.Status.WARN
    assert str(unexpected) in row.detail
    assert str(root / "themes") not in row.detail
    assert str(root / "cli.json") not in row.detail


def test_claude_mcp_needs_auth_cache_is_a_known_entry(zone_config: AppConfig) -> None:
    root = zone_config.config_root / "claude"
    root.mkdir(parents=True)
    (root / "settings.json").write_text("{}")
    (root / "mcp-needs-auth-cache.json").write_text("{}")
    row = _check_named(doctor_mod.run_checks(zone_config), "Zone drift")
    assert row.status is doctor_mod.Status.PASS
    assert row.detail == "none"


def test_opencode_credential_carriers_and_transient_files_are_known_entries(
    zone_config: AppConfig,
) -> None:
    from djinn_in_a_box.core import opencode_credentials as oc

    root = zone_config.config_root / "opencode"
    root.mkdir(parents=True)
    known = (
        "credentials.json",
        "credentials.removed.json",
        "credentials.sync-conflict-20261020-120000-ABCDEFG.json",
        "credentials.removed.sync-conflict-20261020-120000-ABCDEFG.json",
        oc._temp_name(oc.CREDENTIALS_NAME),
        oc._temp_name(oc.TOMBSTONES_NAME),
    )
    for name in known:
        (root / name).write_text("[]")
    unexpected = root / "unexpected.json"
    unexpected.write_text("[]")
    row = _check_named(doctor_mod.run_checks(zone_config), "Zone drift")
    assert row.status is doctor_mod.Status.WARN
    assert str(unexpected) in row.detail
    for name in known:
        assert str(root / name) not in row.detail


@pytest.mark.parametrize(
    "kind",
    [
        "invalid-file",
        "differing-value",
        "stale-temp",
        "audit-session",
        "audit-root",
    ],
)
def test_opencode_credential_doctor_reports_each_kind_without_values(
    zone_config, tmp_path, monkeypatch, kind
):
    from djinn_in_a_box.core import opencode_credentials as oc

    root = zone_config.config_root / "opencode"
    root.mkdir(parents=True, mode=0o700)
    sentinel = os.urandom(32).hex()

    def entry(value):
        return {
            "id": "account",
            "integrationID": "provider",
            "active": True,
            "label": sentinel,
            "value": {"type": "key", "key": value},
        }

    main = root / "credentials.json"
    main.write_text(json.dumps([entry(sentinel)]))
    main.chmod(0o600)
    subject = "credentials.json"
    detail = ""
    if kind == "invalid-file":
        main.write_text(json.dumps({"invalid": sentinel}))
        detail = "credentials are not an array"
    elif kind == "differing-value":
        copy = root / "credentials.sync-conflict-20261020-120000-ABCDEFG.json"
        copy.write_text(json.dumps([entry(os.urandom(32).hex())]))
        copy.chmod(0o600)
        subject, detail = "account", "provider"
    elif kind == "stale-temp":
        subject = ".credentials.json.0123456789abcdef.tmp"
        (root / subject).write_text(sentinel)
    elif kind == "audit-session":
        session = oc.create_audit_session(policy=oc.RootPolicy(home=str(tmp_path / "home")))
        subject, detail = session.id, "retained"
        session.close()
    else:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        (home / ".local").symlink_to(tmp_path / "missing")
        subject = ""
    # Positive control: the runtime sentinel exists in credential storage before scanning.
    assert sentinel.encode() in main.read_bytes()
    findings = oc.carrier_findings(root, policy=oc.RootPolicy(home=str(tmp_path / "home")))
    assert any(item.kind == kind for item in findings)
    # The check uses the same home policy as ordinary diagnostics.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    row = _check_named(doctor_mod.run_checks(zone_config), "OpenCode credentials")
    assert row.status is doctor_mod.Status.WARN
    assert kind in row.detail
    assert subject in row.detail and detail in row.detail
    assert sentinel not in row.detail + row.remedy
    assert ("conflict-copy" in row.detail) == (kind == "differing-value")
    if kind in {"audit-session", "audit-root"}:
        assert "next `djinn audit` or `djinn start`" in row.remedy
        assert "without a database" in row.remedy
        assert "removed by hand after inspection" in row.remedy
    if kind == "differing-value":
        assert "next dev start merges them" in row.remedy


def test_opencode_credential_doctor_clean_is_pass(zone_config):
    row = _check_named(doctor_mod.run_checks(zone_config), "OpenCode credentials")
    assert row.status is doctor_mod.Status.PASS
    assert row.detail == "clean"


@pytest.mark.parametrize(
    "name,document",
    [
        ("credentials.sync-conflict-20261020-120000-ABCDEFG.json", []),
        ("credentials.removed.sync-conflict-20261020-120000-ABCDEFG.json", ["removed"]),
    ],
)
def test_opencode_doctor_reports_pending_conflict_copy_without_zone_drift(
    zone_config, name, document
):
    root = zone_config.config_root / "opencode"
    root.mkdir(parents=True)
    copy = root / name
    copy.write_text(json.dumps(document))
    copy.chmod(0o600)
    checks = doctor_mod.run_checks(zone_config)
    row = _check_named(checks, "OpenCode credentials")
    assert row.status is doctor_mod.Status.WARN
    assert "conflict-copy" in row.detail
    assert copy.name in row.detail
    assert "next dev start's credential sync merges, backs up and removes" in row.remedy
    drift = _check_named(checks, "Zone drift")
    assert drift.status is doctor_mod.Status.PASS
    assert copy.name not in drift.detail
