from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from djinn_in_a_box.commands import agent, backup, container, doctor
from djinn_in_a_box.commands.zone_gate import GatedCommand, zone_command_gate
from djinn_in_a_box.config import zones as zones_module
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import load_zone_assignments
from djinn_in_a_box.core.config_lock import (
    ConfigDirectoryLockBusyError,
    config_directory_lock,
)
from djinn_in_a_box.core.docker import ensure_zone_roots, resolve_zone_roots


@pytest.fixture
def zone_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(zones_module, "ZONES_FILE", tmp_path / "zones.toml")
    return AppConfig(code_dir=projects, config_root=tmp_path / "config")


def _write_unmigrated_assignment(config: AppConfig) -> None:
    roots = resolve_zone_roots(config)
    source = roots.config_root / "claude" / "jobs"
    source.mkdir(parents=True)
    (source / "state.json").write_text("unmigrated")


def _write_collision(config: AppConfig) -> None:
    roots = ensure_zone_roots(config)
    source = roots.config_root / "claude" / "jobs"
    target = roots.local_root / "claude" / "jobs"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    (source / "config.json").write_text("config")
    (target / "zone.json").write_text("zone")


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        ("start", True),
        ("run", True),
        ("backup", False),
        ("restore", False),
        ("clean", False),
    ],
)
def test_unmigrated_gate_matrix_per_command(
    zone_config: AppConfig, command: GatedCommand, refused: bool
) -> None:
    _write_unmigrated_assignment(zone_config)

    if refused:
        with pytest.raises(typer.Exit) as exc_info, zone_command_gate(zone_config, command):
            pass
        assert exc_info.value.exit_code == 1
    else:
        with zone_command_gate(zone_config, command):
            pass


@pytest.mark.parametrize("command", ("start", "run", "backup", "restore", "clean"))
def test_collision_gates_every_command(zone_config: AppConfig, command: GatedCommand) -> None:
    _write_collision(zone_config)

    with pytest.raises(typer.Exit) as exc_info, zone_command_gate(zone_config, command):
        pass

    assert exc_info.value.exit_code == 1


@pytest.mark.parametrize("command", ("start", "run", "backup", "restore", "clean"))
def test_exclusive_migration_lock_gates_every_command(
    zone_config: AppConfig, command: GatedCommand
) -> None:
    roots = ensure_zone_roots(zone_config)

    with (
        config_directory_lock(roots.local_root, exclusive=True),
        pytest.raises(typer.Exit) as exc_info,
        zone_command_gate(zone_config, command),
    ):
        pass

    assert exc_info.value.exit_code == 1


def test_each_gate_condition_prints_its_own_remedy(zone_config: AppConfig) -> None:
    _write_unmigrated_assignment(zone_config)
    with (
        patch("djinn_in_a_box.commands.zone_gate.error") as report,
        pytest.raises(typer.Exit),
        zone_command_gate(zone_config, "start"),
    ):
        pass
    assert "migrate-zones" in report.call_args.args[0]

    roots = ensure_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    target = roots.local_root / "claude" / "jobs"
    target.mkdir(parents=True)
    (target / "zone.json").write_text("zone")
    assert source.is_dir()
    with (
        patch("djinn_in_a_box.commands.zone_gate.error") as report,
        pytest.raises(typer.Exit),
        zone_command_gate(zone_config, "backup"),
    ):
        pass
    assert "doctor" in report.call_args.args[0]

    with (
        config_directory_lock(roots.local_root, exclusive=True),
        patch("djinn_in_a_box.commands.zone_gate.error") as report,
        pytest.raises(typer.Exit),
        zone_command_gate(zone_config, "clean"),
    ):
        pass
    assert "migration is in progress" in report.call_args.args[0]


def test_command_passing_the_gate_holds_a_shared_lock_against_migration(
    zone_config: AppConfig,
) -> None:
    roots = ensure_zone_roots(zone_config)

    with (
        zone_command_gate(zone_config, "backup"),
        pytest.raises(ConfigDirectoryLockBusyError),
        config_directory_lock(roots.local_root, exclusive=True, blocking=False),
    ):
        pass


def test_missing_local_root_is_treated_as_no_migration_in_progress(zone_config: AppConfig) -> None:
    roots = resolve_zone_roots(zone_config)
    assert not roots.local_root.exists()

    with zone_command_gate(zone_config, "backup"):
        pass

    assert roots.local_root.is_dir()


@pytest.mark.parametrize(
    "entrypoint",
    ("start", "run", "backup", "restore", "clean"),
)
def test_each_command_entrypoint_refuses_before_its_work(
    zone_config: AppConfig, entrypoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_collision(zone_config)

    if entrypoint == "start":
        monkeypatch.setattr(container, "load_config", lambda: zone_config)
        call = container.start
    elif entrypoint == "run":
        monkeypatch.setattr(agent, "load_config", lambda: zone_config)

        def call() -> None:
            agent.run("claude", "prompt")

    elif entrypoint == "backup":
        monkeypatch.setattr(backup, "load_config", lambda: zone_config)
        call = backup.backup
    elif entrypoint == "restore":
        monkeypatch.setattr(backup, "load_config", lambda: zone_config)
        call = backup.restore
    else:
        monkeypatch.setattr(container, "load_config", lambda: zone_config)

        def call() -> None:
            container.clean_all(force=True)

    with pytest.raises(typer.Exit) as exc_info:
        call()

    assert exc_info.value.exit_code == 1


def test_doctor_has_no_zone_gate() -> None:
    assert "zone_command_gate" not in inspect.getsource(doctor)


def test_assignment_loader_still_runs_under_a_shared_gate(zone_config: AppConfig) -> None:
    with zone_command_gate(zone_config, "backup"):
        assignments = load_zone_assignments(zone_config)

    assert assignments.by_agent["claude"]["local"]
