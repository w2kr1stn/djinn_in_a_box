from __future__ import annotations

from pathlib import Path

import pytest

import djinn_in_a_box.core.docker as docker_mod
from djinn_in_a_box.commands import doctor as doctor_mod
from djinn_in_a_box.config import zones as zones_mod
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import load_zone_assignments
from djinn_in_a_box.core.docker import ensure_zone_roots, resolve_zone_roots
from djinn_in_a_box.core.zone_migration import reconcile_zone_assignments


@pytest.fixture
def zone_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(zones_mod, "ZONES_FILE", tmp_path / "zones.toml")
    return AppConfig(code_dir=projects, config_root=tmp_path / "config")


def _check_named(checks: list[doctor_mod.Check], name: str) -> doctor_mod.Check:
    return next(check for check in checks if check.name == name)


def test_unmigrated_doctor_sizes_only_unmigrated_paths(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = resolve_zone_roots(zone_config)
    unmigrated = roots.config_root / "claude" / "jobs"
    unmigrated.mkdir(parents=True)
    (unmigrated / "state.json").write_text("unmigrated")
    measured: list[Path] = []
    def record_size(path: Path) -> int:
        measured.append(path)
        return 0

    monkeypatch.setattr(doctor_mod, "_path_size_bytes", record_size)
    checks = doctor_mod.run_checks(zone_config)

    row = _check_named(checks, "Unmigrated zone assignments")
    assert row.status is doctor_mod.Status.WARN
    assert "claude/jobs" in row.detail
    assert "migrate-zones" in row.remedy
    assert measured
    assert all(path == unmigrated or path.is_relative_to(unmigrated) for path in measured)


def test_collision_doctor_names_both_copies_sizes_and_manual_options(
    zone_config: AppConfig,
) -> None:
    roots = ensure_zone_roots(zone_config)
    config_copy = roots.config_root / "claude" / "projects"
    zone_copy = roots.shared_root / "claude" / "projects"
    config_copy.mkdir(parents=True)
    zone_copy.mkdir(parents=True)
    (config_copy / "archive.jsonl").write_text("archive")
    (zone_copy / "live.jsonl").write_text("live")

    checks = doctor_mod.run_checks(zone_config)

    row = _check_named(checks, "Unresolved zone collisions")
    assert row.status is doctor_mod.Status.WARN
    assert str(config_copy) in row.detail
    assert str(zone_copy) in row.detail
    assert "B" in row.detail
    assert "keep the config-root copy" in row.remedy.lower()
    assert "keep the zone copy" in row.remedy.lower()
    assert "merge the trees by hand" in row.remedy.lower()


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


def test_legacy_sync_root_is_detected_after_host_provisions_the_new_root(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_mod.Path, "home", lambda: zone_config.config_root.parent)
    legacy_agent = zone_config.config_root.parent / ".djinn" / "sync" / "claude"
    legacy_agent.mkdir(parents=True)
    (legacy_agent / ".credentials.json").write_text("legacy credential")
    monkeypatch.setattr(docker_mod, "get_project_root", lambda: zone_config.config_root.parent)

    docker_mod.ensure_host_env(zone_config)

    assert doctor_mod._old_sync_root_present(zone_config) is True


def test_legacy_sync_root_is_not_reported_after_its_content_moves_to_a_zone(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_mod.Path, "home", lambda: zone_config.config_root.parent)
    legacy_projects = zone_config.config_root.parent / ".djinn" / "sync" / "claude" / "projects"
    legacy_projects.mkdir(parents=True)
    (legacy_projects / "legacy.jsonl").write_text("legacy transcript")
    current_projects = zone_config.config_root / "claude" / "projects"
    current_projects.mkdir(parents=True)
    (current_projects / "legacy.jsonl").write_text("legacy transcript")

    reconcile_zone_assignments(zone_config)

    assert doctor_mod._old_sync_root_present(zone_config) is False


def test_doctor_warns_when_an_assigned_path_cannot_be_inspected(
    zone_config: AppConfig,
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    source.mkdir(parents=True)
    (source / "state.json").write_text("unreadable")
    agent_root = source.parent
    agent_root.chmod(0o000)

    try:
        checks = doctor_mod.run_checks(zone_config)
    finally:
        agent_root.chmod(0o700)

    row = _check_named(checks, "Zone configuration")
    assert row.status is doctor_mod.Status.WARN
    assert "Cannot inspect zone path" in row.detail
