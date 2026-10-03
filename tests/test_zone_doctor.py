from __future__ import annotations

from pathlib import Path

import pytest

from djinn_in_a_box.commands import doctor as doctor_mod
from djinn_in_a_box.config import zones as zones_mod
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import load_zone_assignments
from djinn_in_a_box.core.docker import ensure_zone_roots, resolve_zone_roots


@pytest.fixture
def zone_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(zones_mod, "ZONES_FILE", tmp_path / "zones.toml")
    return AppConfig(code_dir=projects, config_root=tmp_path / "config")


def _check_named(checks: list[doctor_mod.Check], name: str) -> doctor_mod.Check:
    return next(check for check in checks if check.name == name)


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


def test_drift_accounts_for_pattern_entries_but_reports_unmatched_names(
    zone_config: AppConfig,
) -> None:
    roots = resolve_zone_roots(zone_config)
    codex_root = roots.config_root / "codex"
    codex_root.mkdir(parents=True)
    (codex_root / "state_12.sqlite").touch()
    (codex_root / "state_12.sqlite-wal").touch()
    unexpected = codex_root / "unrecognized-runtime-file"
    unexpected.touch()

    checks = doctor_mod.run_checks(zone_config)

    row = _check_named(checks, "Zone drift")
    assert row.status is doctor_mod.Status.WARN
    assert str(unexpected) in row.detail
    assert str(codex_root / "state_12.sqlite") not in row.detail
    assert str(codex_root / "state_12.sqlite-wal") not in row.detail


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
