from __future__ import annotations

import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from djinn_in_a_box.commands import backup as backup_command
from djinn_in_a_box.commands import migrate_zones as migration_command
from djinn_in_a_box.config import zones as zones_module
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.docker import RunResult, resolve_zone_roots


@pytest.fixture
def zone_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(zones_module, "ZONES_FILE", tmp_path / "zones.toml")
    return AppConfig(code_dir=projects, config_root=tmp_path / "config")


def test_migrate_zones_confirms_unconditionally_and_is_idempotent(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    source.mkdir(parents=True)
    (source / "state.json").write_text("move")
    target = roots.local_root / "claude" / "jobs"
    monkeypatch.setattr(migration_command, "load_config", lambda: zone_config)

    def no_containers() -> list[str]:
        return []

    monkeypatch.setattr(migration_command, "get_running_containers", no_containers)

    with patch("djinn_in_a_box.commands.migrate_zones.typer.confirm", return_value=True) as confirm:
        migration_command.migrate_zones()
        migration_command.migrate_zones()

    assert confirm.call_count == 2
    assert "mirroring" in confirm.call_args.args[0]
    assert not source.exists()
    assert (target / "state.json").read_text() == "move"


def test_migrate_zones_rechecks_containers_before_publish(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    source.mkdir(parents=True)
    (source / "state.json").write_text("do not move")
    calls = 0

    def containers() -> list[str]:
        nonlocal calls
        calls += 1
        return [] if calls == 1 else ["djinn"]

    monkeypatch.setattr(migration_command, "load_config", lambda: zone_config)
    monkeypatch.setattr(migration_command, "get_running_containers", containers)

    with (
        patch("djinn_in_a_box.commands.migrate_zones.typer.confirm", return_value=True),
        pytest.raises(typer.Exit) as exc_info,
    ):
        migration_command.migrate_zones()

    assert exc_info.value.exit_code == 1
    assert (source / "state.json").read_text() == "do not move"


@pytest.mark.parametrize("probe_states", ((None,), ([], None)))
def test_migrate_zones_refuses_an_unknown_container_state(
    zone_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    probe_states: tuple[list[str] | None, ...],
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    source.mkdir(parents=True)
    (source / "state.json").write_text("do not move")
    probes = iter(probe_states)

    def unknown_or_empty_containers() -> list[str] | None:
        return next(probes, probe_states[-1])

    monkeypatch.setattr(migration_command, "load_config", lambda: zone_config)
    monkeypatch.setattr(migration_command, "get_running_containers", unknown_or_empty_containers)

    with (
        patch("djinn_in_a_box.commands.migrate_zones.typer.confirm", return_value=True),
        pytest.raises(typer.Exit) as exc_info,
    ):
        migration_command.migrate_zones()

    assert exc_info.value.exit_code == 1
    assert (source / "state.json").read_text() == "do not move"


def test_migrate_zones_collision_moves_nothing(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    target = roots.local_root / "claude" / "jobs"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    (source / "config.json").write_text("config")
    (target / "zone.json").write_text("zone")
    monkeypatch.setattr(migration_command, "load_config", lambda: zone_config)

    def no_containers() -> list[str]:
        return []

    monkeypatch.setattr(migration_command, "get_running_containers", no_containers)

    with (
        patch("djinn_in_a_box.commands.migrate_zones.typer.confirm", return_value=True),
        pytest.raises(typer.Exit) as exc_info,
    ):
        migration_command.migrate_zones()

    assert exc_info.value.exit_code == 1
    assert (source / "config.json").read_text() == "config"
    assert (target / "zone.json").read_text() == "zone"


def test_restore_reconciles_under_its_shared_lock_without_reacquiring_it(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    inner = tmp_path / "djinn-sync-claude.tar.gz"
    inner.write_bytes(b"placeholder")
    archive = backups / "djinn-backup-2026-01-01.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(inner, arcname=inner.name)

    def restore_sync_path(_name: str, _staging: Path, config: AppConfig) -> RunResult:
        source = resolve_zone_roots(config).config_root / "claude" / "jobs"
        source.mkdir(parents=True)
        (source / "state.json").write_text("restored")
        return RunResult(0, "", "")

    monkeypatch.setattr(backup_command, "BACKUPS_DIR", backups)
    monkeypatch.setattr(backup_command, "load_config", lambda: zone_config)

    def no_containers() -> list[str]:
        return []

    monkeypatch.setattr(backup_command, "get_running_containers", no_containers)
    monkeypatch.setattr(backup_command, "restore_sync_path", restore_sync_path)

    with patch("djinn_in_a_box.commands.backup.typer.confirm", return_value=True):
        backup_command.restore()

    target = resolve_zone_roots(zone_config).local_root / "claude" / "jobs"
    assert (target / "state.json").read_text() == "restored"


def test_restore_archive_adoption_moves_existing_zone_data_aside(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    inner = tmp_path / "djinn-sync-claude.tar.gz"
    inner.write_bytes(b"placeholder")
    archive = backups / "djinn-backup-2026-01-01.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(inner, arcname=inner.name)

    roots = resolve_zone_roots(zone_config)
    live_zone = roots.shared_root / "claude" / "projects"
    live_zone.mkdir(parents=True)
    (live_zone / "live.jsonl").write_text("live")

    def restore_sync_path(_name: str, _staging: Path, config: AppConfig) -> RunResult:
        archive_copy = resolve_zone_roots(config).config_root / "claude" / "projects"
        archive_copy.mkdir(parents=True)
        (archive_copy / "archive.jsonl").write_text("archive")
        return RunResult(0, "", "")

    def no_containers() -> list[str]:
        return []

    monkeypatch.setattr(backup_command, "BACKUPS_DIR", backups)
    monkeypatch.setattr(backup_command, "load_config", lambda: zone_config)
    monkeypatch.setattr(backup_command, "get_running_containers", no_containers)
    monkeypatch.setattr(backup_command, "restore_sync_path", restore_sync_path)

    with patch("djinn_in_a_box.commands.backup.typer.confirm", side_effect=(True, True)):
        backup_command.restore()

    displaced = next(live_zone.parent.glob("projects.pre-restore-*"))
    assert (displaced / "live.jsonl").read_text() == "live"
    assert (live_zone / "archive.jsonl").read_text() == "archive"
