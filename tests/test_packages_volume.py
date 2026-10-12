"""Registration and lifecycle coverage for the agent-installed package volume."""

from __future__ import annotations

import subprocess
import tarfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import backup as backup_module
from djinn_in_a_box.commands import container
from djinn_in_a_box.config.defaults import VOLUME_CATEGORIES
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import docker
from djinn_in_a_box.core.docker import ContainerMount, DockerMode, MountCollisionError, RunResult

runner = CliRunner()
PACKAGE_NAME = "djinn-packages"
PACKAGE_TARGET = Path("/var/lib/djinn-packages")


def _package_config(tmp_path: Path) -> AppConfig:
    return AppConfig(code_dir=tmp_path, config_root=tmp_path / "config")


def _declared_volume_config(tmp_path: Path, target: str, name: str = "worker") -> AppConfig:
    return AppConfig(code_dir=tmp_path).model_copy(
        update={
            "mounts": {
                name: {"volume": True, "target": target, "backup": "none"},
            },
        }
    )


def _runtime_reservations(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> list[Path]:
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda _config: [])
    monkeypatch.setattr(docker, "get_sops_age_key_mount_args", lambda _config: [])
    monkeypatch.setattr(
        docker, "_zone_overlay_mount_args_and_targets", lambda _config: ([], ())
    )
    targets, warnings = docker.declaration_reservation_context(config)
    assert warnings == []
    return targets


def _inspect_package_volume(
    command: list[str], **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    name = command[-1]
    if name == PACKAGE_NAME:
        return subprocess.CompletedProcess(command, 0, stdout=f"{name}\n", stderr="")
    return subprocess.CompletedProcess(
        command,
        1,
        stdout="",
        stderr=f"Error response from daemon: get {name}: no such volume",
    )


def test_packages_volume_registries() -> None:
    root = Path(__file__).resolve().parents[1]
    compose_text = (root / "docker-compose.yml").read_text()
    compose = yaml.safe_load(compose_text)
    expected_mount = f"packages:{PACKAGE_TARGET}"

    assert "# Agent-installed Debian package list, replayed at start" in compose_text
    assert expected_mount in compose["x-common-volumes"]
    assert expected_mount in compose["services"]["dev"]["volumes"]
    assert compose["volumes"]["packages"] == {"name": PACKAGE_NAME}
    assert [
        category for category, names in VOLUME_CATEGORIES.items() if PACKAGE_NAME in names
    ] == ["data"]
    assert docker._COMPOSE_DEV_MOUNT_TARGETS[PACKAGE_TARGET] == "directory"
    assert PACKAGE_TARGET not in docker.MANAGED_VOLUME_REPAIR_TARGETS
    assert PACKAGE_TARGET in docker.MANAGED_TARGET_ROOTS


def test_packages_volume_name_collision(tmp_path: Path) -> None:
    config = _declared_volume_config(tmp_path, "/home/dev/declared-packages", "packages")
    result = docker.resolve_declared_entries(
        config,
        docker.ContainerOptions(),
        runtime_targets=[],
        caller_env=None,
    )

    error = result.diagnostics[0].error
    assert error is not None
    assert PACKAGE_NAME in error and "built-in volume" in error


@pytest.mark.parametrize(
    ("target", "fails"),
    [
        (str(PACKAGE_TARGET), True),
        (str(PACKAGE_TARGET.parent), True),
        (str(PACKAGE_TARGET / "child"), True),
        (str(PACKAGE_TARGET.with_name("djinn-packages-sibling")), False),
    ],
    ids=["equal", "ancestor", "descendant", "sibling"],
)
def test_declared_package_targets_are_reserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    fails: bool,
) -> None:
    config = _declared_volume_config(tmp_path, target)
    runtime_targets = _runtime_reservations(config, monkeypatch)
    result = docker.resolve_declared_entries(
        config,
        docker.ContainerOptions(),
        runtime_targets=runtime_targets,
        caller_env=None,
    )

    error = result.diagnostics[0].error
    assert bool(error) is fails
    if fails:
        assert error is not None and "conflict" in error
    else:
        result.require_valid()


@pytest.mark.parametrize(
    ("target", "fails"),
    [
        (PACKAGE_TARGET, True),
        (PACKAGE_TARGET.parent, True),
        (PACKAGE_TARGET / "child", True),
        (PACKAGE_TARGET.with_name("djinn-packages-sibling"), False),
    ],
    ids=["equal", "ancestor", "descendant", "sibling"],
)
def test_invocation_package_targets_are_reserved(
    tmp_path: Path, target: Path, fails: bool
) -> None:
    mount = ContainerMount(tmp_path, target)
    if fails:
        with pytest.raises(MountCollisionError):
            docker.validate_container_mounts(
                (mount,),
                _package_config(tmp_path),
                DockerMode.NONE,
                shell_args=[],
                sops_args=[],
                zone_overlay_targets=(),
            )
    else:
        docker.validate_container_mounts(
            (mount,),
            _package_config(tmp_path),
            DockerMode.NONE,
            shell_args=[],
            sops_args=[],
            zone_overlay_targets=(),
        )


def test_packages_clean_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _package_config(tmp_path)

    with (
        patch.object(container, "compose_down", return_value=RunResult(0)) as compose_down,
        patch.object(container, "delete_volumes") as delete,
    ):
        result = runner.invoke(app, ["clean"])
    assert result.exit_code == 0, result.output
    compose_down.assert_called_once_with()
    delete.assert_not_called()

    with (
        patch.object(container, "load_config", return_value=config),
        patch.object(docker.subprocess, "run", side_effect=_inspect_package_volume),
        patch.object(container, "delete_volumes", return_value={PACKAGE_NAME: True}) as delete,
    ):
        result = runner.invoke(app, ["clean", "volumes", "--data", "--force"])
    assert result.exit_code == 0, result.output
    delete.assert_called_once_with([PACKAGE_NAME])

    with (
        patch.object(container, "load_config", return_value=config),
        patch.object(container.hostctl, "control_guard", return_value=nullcontext()),
        patch.object(container, "compose_down", return_value=RunResult(0)),
        patch.object(container.host_runtime, "creation_guard", return_value=nullcontext()),
        patch.object(container.host_runtime, "inspect_dev", return_value=None),
        patch.object(container.hostctl, "delete_state_locked"),
        patch.object(
            container,
            "delete_volumes",
            side_effect=lambda names: dict.fromkeys(names, True),
        ) as delete,
        patch.object(container, "get_existing_sync_paths_by_category", return_value=[]),
        patch.object(container, "network_exists", return_value=False),
    ):
        result = runner.invoke(app, ["clean", "all", "--force"])
    assert result.exit_code == 0, result.output
    assert PACKAGE_NAME in delete.call_args.args[0]


def test_default_backup_includes_packages_volume(
    tmp_path: Path,
) -> None:
    config = _package_config(tmp_path)
    backups_dir = tmp_path / "backups"

    def write_volume(name: str, destination: Path) -> RunResult:
        with tarfile.open(destination / f"{name}.tar.gz", "w:gz"):
            pass
        return RunResult(0)

    with (
        patch.object(backup_module, "load_config", return_value=config),
        patch.object(backup_module, "get_running_containers", return_value=[]),
        patch.object(docker.subprocess, "run", side_effect=_inspect_package_volume),
        patch.object(backup_module, "backup_volume", side_effect=write_volume) as backup_volume,
        patch.object(backup_module, "BACKUPS_DIR", backups_dir),
    ):
        result = runner.invoke(app, ["backup", "--no-encrypt"])

    assert result.exit_code == 0, result.output
    assert [call.args[0] for call in backup_volume.call_args_list] == [PACKAGE_NAME]
    archives = list(backups_dir.glob("djinn-backup-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        assert f"{PACKAGE_NAME}.tar.gz" in archive.getnames()
