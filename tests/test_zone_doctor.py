from __future__ import annotations

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
