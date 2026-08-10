"""Tests for volume backup and restore commands."""

from __future__ import annotations

import subprocess
import tarfile
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import backup as backup_module
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.docker import RunResult

runner = CliRunner()


@pytest.fixture(autouse=True)
def mock_backup_config(mock_app_config: AppConfig) -> Generator[None]:
    """Existing command tests do not exercise config loading."""
    with patch("djinn_in_a_box.commands.backup.load_config", return_value=mock_app_config):
        yield


@pytest.fixture(autouse=True)
def mock_age_for_backup_commands() -> Generator[None]:
    """Keep command tests non-interactive while preserving age orchestration."""

    def encrypt_for_test(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        temp_path = Path(argv[argv.index("-o") + 1])
        archive_source = Path(argv[-1])
        temp_path.write_bytes(b"age-encryption.org/v1\n" + archive_source.read_bytes())
        return subprocess.CompletedProcess(argv, 0)

    with (
        patch("djinn_in_a_box.commands.backup.shutil.which", return_value="/usr/bin/age"),
        patch("djinn_in_a_box.commands.backup._has_controlling_terminal", return_value=True),
        patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
    ):
        mock_subprocess.run.side_effect = encrypt_for_test
        yield


class TestGuardNoContainersRunning:
    """Tests for _guard_no_containers_running."""

    def test_passes_when_no_containers(self) -> None:
        with patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]):
            backup_module._guard_no_containers_running()

    def test_refuses_an_unknown_container_state_from_the_docker_probe(self) -> None:
        with (
            patch(
                "djinn_in_a_box.core.docker.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["docker", "ps"], 1, stdout="", stderr="daemon unavailable"
                ),
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module._guard_no_containers_running()

        assert exc_info.value.exit_code == 1

    def test_exits_when_containers_running(self) -> None:
        with patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=["djinn"]):
            with pytest.raises(typer.Exit) as exc_info:
                backup_module._guard_no_containers_running()
            assert exc_info.value.exit_code == 1


class TestCollectItems:
    """Tests for _collect_items."""

    def test_collects_volumes_only(self) -> None:
        with (
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-uv-cache"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
        ):
            volumes, sync_paths = backup_module._collect_items(["cache"])
            assert volumes == ["djinn-uv-cache"]
            assert sync_paths == []

    def test_collects_sync_paths_only(self, tmp_path: Path) -> None:
        fake_path = tmp_path / "claude"
        with (
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[fake_path],
            ),
        ):
            volumes, sync_paths = backup_module._collect_items(["credentials"])
            assert volumes == []
            assert sync_paths == [fake_path]

    def test_exits_on_unknown_category(self) -> None:
        with pytest.raises(typer.Exit) as exc_info:
            backup_module._collect_items(["invalid"])
        assert exc_info.value.exit_code == 1

    def test_collects_from_multiple_categories(self, tmp_path: Path) -> None:
        fake_path = tmp_path / "claude"
        with (
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                side_effect=[["djinn-uv-cache"], []],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                side_effect=[[], [fake_path]],
            ),
        ):
            volumes, sync_paths = backup_module._collect_items(["cache", "credentials"])
            assert volumes == ["djinn-uv-cache"]
            assert sync_paths == [fake_path]

    def test_passes_config_to_sync_path_collection(self, tmp_path: Path) -> None:
        config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "configured-root")
        with (
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ) as mock_sync,
        ):
            backup_module._collect_items(["credentials"], config)

        mock_sync.assert_called_once_with("credentials", config)


class TestBackupCommand:
    """Tests for the backup command."""

    def test_backup_refuses_unknown_container_state_from_the_docker_probe(self) -> None:
        with (
            patch(
                "djinn_in_a_box.core.docker.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["docker", "ps"], 1, stdout="", stderr="daemon unavailable"
                ),
            ),
            patch(
                "djinn_in_a_box.commands.backup._collect_items",
                side_effect=AssertionError("backup proceeded after an unknown Docker state"),
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module.backup()

        assert exc_info.value.exit_code == 1

    def test_aborts_when_containers_running(self) -> None:
        with patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=["djinn"]):
            with pytest.raises(typer.Exit) as exc_info:
                backup_module.backup()
            assert exc_info.value.exit_code == 1

    def test_exits_when_no_volumes_found(self) -> None:
        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
        ):
            with pytest.raises(typer.Exit) as exc_info:
                backup_module.backup()
            assert exc_info.value.exit_code == 0

    def test_backup_creates_archive(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        staged_outer = staging_dir / f"djinn-backup-{datetime.now(tz=UTC):%Y-%m-%d}.tar.gz"
        real_iterdir = Path.iterdir
        real_add = cast(Callable[..., Any], tarfile.TarFile.add)
        added_paths: list[str] = []

        def include_staged_outer(path: Path) -> Iterator[Path]:
            children = list(real_iterdir(path))
            if path == staging_dir:
                children.append(staged_outer)
            return iter(children)

        def record_add(
            archive: tarfile.TarFile, name: str, *args: object, **kwargs: object
        ) -> None:
            added_paths.append(name)
            real_add(archive, name, *args, **kwargs)

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.get_existing_volumes_by_category") as mock_get,
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch("djinn_in_a_box.commands.backup.backup_volume") as mock_backup,
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch.object(Path, "iterdir", new=include_staged_outer),
            patch.object(tarfile.TarFile, "add", new=record_add),
        ):
            mock_get.side_effect = [["djinn-claude-config"], [], []]
            mock_backup.return_value = RunResult(returncode=0, stdout="", stderr="")

            fake_archive = staging_dir / "djinn-claude-config.tar.gz"
            with tarfile.open(fake_archive, "w:gz") as _tar:
                pass  # empty archive is fine for test

            backup_module.backup()

            mock_backup.assert_called_once_with("djinn-claude-config", staging_dir)
            archives = list(backups_dir.glob("djinn-backup-*.tar.gz.age"))
            assert len(archives) == 1
            assert archives[0].read_bytes().startswith(b"age-encryption.org/v1\n")
            assert str(staged_outer) not in added_paths

    def test_backup_replaces_previous(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        old_backup = backups_dir / "djinn-backup-2026-01-01.tar.gz"
        old_backup.touch()

        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.get_existing_volumes_by_category") as mock_get,
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch("djinn_in_a_box.commands.backup.backup_volume") as mock_backup,
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
        ):
            mock_get.return_value = ["djinn-claude-config"]
            mock_backup.return_value = RunResult(returncode=0, stdout="", stderr="")

            fake_archive = staging_dir / "djinn-claude-config.tar.gz"
            with tarfile.open(fake_archive, "w:gz") as _tar:
                pass

            backup_module.backup()

            assert not old_backup.exists()
            archives = list(backups_dir.glob("djinn-backup-*.tar.gz.age"))
            assert len(archives) == 1

        message = capsys.readouterr().err
        assert "Removed unencrypted backup" in message
        assert old_backup.name in message

    def test_no_encrypt_replaces_previous_encrypted_archive(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        old_backup = backups_dir / "djinn-backup-2026-01-01.tar.gz.age"
        old_backup.write_bytes(b"age-encryption.org/v1\nold")
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
        ):
            backup_module.backup(no_encrypt=True)

        assert not old_backup.exists()
        assert len(list(backups_dir.glob("djinn-backup-*.tar.gz"))) == 1
        assert not list(backups_dir.glob("djinn-backup-*.tar.gz.age"))

    def test_backup_removes_stale_temp_archives_during_backup(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        stale = backups_dir / ".djinn-backup-orphan"
        stale.write_bytes(b"stale")
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
        ):
            backup_module.backup()

        assert not stale.exists()

    def test_backup_aborts_on_volume_failure(self, tmp_path: Path) -> None:
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.get_existing_volumes_by_category") as mock_get,
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch("djinn_in_a_box.commands.backup.backup_volume") as mock_backup,
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
        ):
            mock_get.return_value = ["djinn-claude-config"]
            mock_backup.return_value = RunResult(returncode=1, stdout="", stderr="backup failed")
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            with pytest.raises(typer.Exit) as exc_info:
                backup_module.backup()
            assert exc_info.value.exit_code == 1

    def test_backup_with_categories_flag(self, tmp_path: Path) -> None:
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        backups_dir = tmp_path / "backups"

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.get_existing_volumes_by_category") as mock_get,
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch("djinn_in_a_box.commands.backup.backup_volume") as mock_backup,
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
        ):
            mock_get.return_value = ["djinn-uv-cache"]
            mock_backup.return_value = RunResult(returncode=0, stdout="", stderr="")

            fake_archive = staging_dir / "djinn-uv-cache.tar.gz"
            with tarfile.open(fake_archive, "w:gz") as _tar:
                pass

            backup_module.backup(categories=["cache"])

            mock_get.assert_called_once_with("cache")

    def test_backup_collects_sync_paths_from_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        code_dir = tmp_path / "code"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        (configured_root / "claude").mkdir(parents=True)
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)

        backups_dir = tmp_path / "backups"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        with (
            patch(
                "djinn_in_a_box.commands.backup.load_config",
                side_effect=lambda: load_config(config_file),
            ),
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=[],
            ),
            patch("djinn_in_a_box.commands.backup.backup_sync_path") as mock_backup_sync,
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
        ):
            mock_backup_sync.return_value = RunResult(returncode=0, stdout="", stderr="")

            backup_module.backup(categories=["credentials"])

        mock_backup_sync.assert_called_once_with(configured_root / "claude", staging_dir)

    def test_backup_tightens_existing_directory_and_archive_modes(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir(mode=0o755)
        backups_dir.chmod(0o755)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass
        real_mkstemp = backup_module.tempfile.mkstemp

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch(
                "djinn_in_a_box.commands.backup.tempfile.mkstemp", wraps=real_mkstemp
            ) as mock_mkstemp,
        ):
            backup_module.backup()

        archive = next(backups_dir.glob("djinn-backup-*.tar.gz.age"))
        assert backups_dir.stat().st_mode & 0o777 == 0o700
        assert archive.stat().st_mode & 0o777 == 0o600
        assert mock_mkstemp.call_args.kwargs["dir"] == backups_dir

    def test_backup_preserves_previous_archive_when_publication_fails(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        previous = backups_dir / "djinn-backup-2026-01-01.tar.gz"
        previous.write_bytes(b"known-good")
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.os.replace", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            backup_module.backup()

        assert previous.read_bytes() == b"known-good"
        assert not list(backups_dir.glob(".djinn-backup-*"))

    def test_backup_reports_rotation_failure_after_publication(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        previous = backups_dir / "djinn-backup-2026-01-01.tar.gz"
        previous.touch()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass
        original_unlink = Path.unlink
        events: list[tuple[str, str]] = []
        warnings: list[str] = []

        def fail_for_previous(path: Path, missing_ok: bool = False) -> None:
            if path == previous:
                raise OSError("permission denied")
            original_unlink(path, missing_ok=missing_ok)

        def record_success(message: str) -> None:
            events.append(("success", message))

        def record_warning(message: str) -> None:
            events.append(("warning", message))
            warnings.append(message)

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch.object(Path, "unlink", new=fail_for_previous),
            patch(
                "djinn_in_a_box.commands.backup.success",
                side_effect=record_success,
            ),
            patch(
                "djinn_in_a_box.commands.backup.warning",
                side_effect=record_warning,
            ),
        ):
            backup_module.backup()

        assert previous.exists()
        assert (
            len(list(backups_dir.glob("djinn-backup-*.tar.gz")))
            + len(list(backups_dir.glob("djinn-backup-*.tar.gz.age")))
            == 2
        )
        saved_at = next(
            index
            for index, (_, message) in enumerate(events)
            if message.startswith("Backup saved:")
        )
        warned_at = next(
            index
            for index, (kind, message) in enumerate(events)
            if kind == "warning" and message.startswith("Failed to remove old backup")
        )
        assert saved_at < warned_at
        assert "djinn-backup-2026-01-01.tar.gz" in warnings[-1]

    def test_backup_no_encrypt_publishes_cleartext_without_calling_age(
        self, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
            patch("djinn_in_a_box.commands.backup.warning") as mock_warning,
        ):
            backup_module.backup(no_encrypt=True)

        assert len(list(backups_dir.glob("djinn-backup-*.tar.gz"))) == 1
        assert not list(backups_dir.glob("djinn-backup-*.tar.gz.age"))
        mock_subprocess.run.assert_not_called()
        assert "can contain credentials" in mock_warning.call_args.args[0]

    def test_cli_no_encrypt_without_age_uses_atomic_private_publish(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir(mode=0o775)
        backups_dir.chmod(0o775)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass
        real_mkstemp = backup_module.tempfile.mkstemp
        real_replace = backup_module.os.replace

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.shutil.which", return_value=None),
            patch(
                "djinn_in_a_box.commands.backup.tempfile.mkstemp", wraps=real_mkstemp
            ) as mock_mkstemp,
            patch("djinn_in_a_box.commands.backup.os.replace", wraps=real_replace) as mock_replace,
        ):
            result = runner.invoke(app, ["backup", "--no-encrypt"])

        assert result.exit_code == 0, result.output
        archives = list(backups_dir.glob("djinn-backup-*.tar.gz"))
        assert len(archives) == 1
        archive = archives[0]
        assert backups_dir.stat().st_mode & 0o777 == 0o700
        assert archive.stat().st_mode & 0o777 == 0o600
        mock_mkstemp.assert_called_once_with(prefix=".djinn-backup-", dir=backups_dir)
        mock_replace.assert_called_once()
        published_temp, published_archive = mock_replace.call_args.args
        assert Path(published_temp).parent == backups_dir
        assert Path(published_temp).name.startswith(".djinn-backup-")
        assert Path(published_archive) == archive
        assert not list(backups_dir.glob(".djinn-backup-*"))

    def test_backup_invokes_age_passphrase_without_a_secret(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass
        events: list[tuple[str, str | None]] = []

        def record_warning_for_invocation(message: str) -> None:
            events.append(("warning", message))

        def run_age(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            events.append(("age", None))
            temp_path = Path(argv[argv.index("-o") + 1])
            temp_path.write_bytes(b"age-encryption.org/v1\n")
            return subprocess.CompletedProcess(argv, 0)

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
            patch(
                "djinn_in_a_box.commands.backup.warning",
                side_effect=record_warning_for_invocation,
            ),
        ):
            mock_subprocess.run.side_effect = run_age
            backup_module.backup()

        argv = mock_subprocess.run.call_args.args[0]
        assert argv[:2] == ["age", "--passphrase"]
        assert argv[2] == "-o"
        assert len(argv) == 5
        assert Path(argv[3]).parent == backups_dir
        assert Path(argv[4]).parent == staging_dir
        assert Path(argv[4]).name.startswith("djinn-backup-")
        assert Path(argv[4]).name.endswith(".tar.gz")
        assert mock_subprocess.run.call_args.kwargs == {"check": False}
        age_index = next(index for index, (kind, _) in enumerate(events) if kind == "age")
        pre_age_warnings = [
            message
            for kind, message in events[:age_index]
            if kind == "warning" and message is not None
        ]
        assert len(pre_age_warnings) == 1
        warning = pre_age_warnings[0]
        assert "empty input generates a passphrase shown once" in warning
        assert "older unencrypted backup is removed" in warning

    @pytest.mark.parametrize(
        "age_output",
        [
            pytest.param(b"", id="zero-byte"),
            pytest.param(b"not age", id="nonempty-without-header"),
        ],
    )
    def test_zero_byte_age_success_preserves_same_day_previous_archive(
        self, tmp_path: Path, age_output: bytes
    ) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        previous = backups_dir / f"djinn-backup-{date_str}.tar.gz.age"
        previous_bytes = b"age-encryption.org/v1\nknown-good"
        previous.write_bytes(previous_bytes)
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass

        def run_age(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            temp_path = Path(argv[argv.index("-o") + 1])
            temp_path.write_bytes(age_output)
            return subprocess.CompletedProcess(argv, 0)

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
            pytest.raises(typer.Exit) as exc_info,
        ):
            mock_subprocess.run.side_effect = run_age
            backup_module.backup()

        assert exc_info.value.exit_code == 1
        assert previous.read_bytes() == previous_bytes
        assert list(backups_dir.glob("djinn-backup-*.tar.gz.age")) == [previous]
        assert not list(backups_dir.glob(".djinn-backup-*"))

    def test_backup_missing_age_explains_the_opt_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backups_dir = tmp_path / "backups"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.shutil.which", return_value=None),
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module.backup()

        assert exc_info.value.exit_code == 1
        assert "Install age or use --no-encrypt" in capsys.readouterr().err
        assert not list(backups_dir.glob("djinn-backup-*"))

    def test_backup_without_a_controlling_terminal_exits_before_publication(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backups_dir = tmp_path / "backups"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup._has_controlling_terminal", return_value=False),
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module.backup()

        assert exc_info.value.exit_code == 1
        message = capsys.readouterr().err
        assert "controlling terminal" in message
        assert "interactive terminal" in message
        assert "--no-encrypt" in message
        assert not list(backups_dir.glob("djinn-backup-*"))

    def test_encryption_failure_preserves_previous_backup_and_cleans_temp(
        self, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        previous = backups_dir / "djinn-backup-2026-01-01.tar.gz"
        previous.write_bytes(b"known-good")
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
            pytest.raises(typer.Exit) as exc_info,
        ):
            mock_subprocess.run.return_value = subprocess.CompletedProcess([], 1)
            backup_module.backup()

        assert exc_info.value.exit_code == 1
        assert previous.read_bytes() == b"known-good"
        assert not list(backups_dir.glob(".djinn-backup-*"))
        assert not list(backups_dir.glob("djinn-backup-*.tar.gz.age"))

    def test_backup_removes_publish_temp_when_age_is_interrupted(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
            pytest.raises(KeyboardInterrupt),
        ):
            mock_subprocess.run.side_effect = KeyboardInterrupt
            backup_module.backup()

        assert not list(backups_dir.glob(".djinn-backup-*"))

    def test_backup_removes_staging_when_age_is_interrupted(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_archive = staging_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch(
                "djinn_in_a_box.commands.backup.backup_volume", return_value=RunResult(0, "", "")
            ),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
            pytest.raises(KeyboardInterrupt),
        ):
            mock_subprocess.run.side_effect = KeyboardInterrupt
            backup_module.backup()

        assert not staging_dir.exists()


class TestRestoreCommand:
    """Tests for the restore command."""

    def test_restore_refuses_unknown_container_state_from_the_docker_probe(self) -> None:
        with (
            patch(
                "djinn_in_a_box.core.docker.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["docker", "ps"], 1, stdout="", stderr="daemon unavailable"
                ),
            ),
            patch(
                "djinn_in_a_box.commands.backup._list_backups",
                side_effect=AssertionError("restore proceeded after an unknown Docker state"),
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module.restore()

        assert exc_info.value.exit_code == 1

    def test_aborts_when_containers_running(self) -> None:
        with patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=["djinn"]):
            with pytest.raises(typer.Exit) as exc_info:
                backup_module.restore()
            assert exc_info.value.exit_code == 1

    def test_restore_missing_age_explains_installation_without_backup_opt_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (backups_dir / "djinn-backup-2026-03-13.tar.gz.age").write_bytes(
            b"age-encryption.org/v1\nplaceholder"
        )

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            patch("djinn_in_a_box.commands.backup.shutil.which", return_value=None),
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module.restore()

        assert exc_info.value.exit_code == 1
        message = capsys.readouterr().err
        assert "age is required to restore" in message
        assert "Install age" in message
        assert "--no-encrypt" not in message

    def test_restore_legacy_archive_works_without_age(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_staging = tmp_path / "inner"
        inner_staging.mkdir()
        inner_archive = inner_staging / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass
        backup_file = backups_dir / "djinn-backup-2026-03-13.tar.gz"
        with tarfile.open(backup_file, "w:gz") as outer:
            outer.add(inner_archive, arcname=inner_archive.name)

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.restore_volume") as mock_restore,
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            patch("djinn_in_a_box.commands.backup.shutil.which", return_value=None),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
        ):
            mock_restore.return_value = RunResult(0, "", "")
            backup_module.restore()

        mock_restore.assert_called_once_with("djinn-claude-config", staging_dir)
        mock_subprocess.run.assert_not_called()
        assert not staging_dir.exists()

    def test_exits_when_no_backup_found(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
        ):
            with pytest.raises(typer.Exit) as exc_info:
                backup_module.restore()
            assert exc_info.value.exit_code == 1

    def test_restore_restores_volumes(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        backups_dir.chmod(0o775)
        assert backups_dir.stat().st_mode & 0o777 == 0o775
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        # Create a backup archive with one inner archive
        inner_staging = tmp_path / "inner"
        inner_staging.mkdir()
        inner_archive = inner_staging / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz") as _tar:
            pass

        backup_file = backups_dir / "djinn-backup-2026-03-13.tar.gz"
        with tarfile.open(backup_file, "w:gz") as outer:
            outer.add(str(inner_archive), arcname="djinn-claude-config.tar.gz")

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.restore_volume") as mock_restore,
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
        ):
            mock_restore.return_value = RunResult(returncode=0, stdout="", stderr="")
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            backup_module.restore()

            mock_restore.assert_called_once_with("djinn-claude-config", staging_dir)
            mock_subprocess.run.assert_not_called()

        assert backups_dir.stat().st_mode & 0o777 == 0o700

    def test_restore_handles_failure(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        inner_staging = tmp_path / "inner"
        inner_staging.mkdir()
        inner_archive = inner_staging / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz") as _tar:
            pass

        backup_file = backups_dir / "djinn-backup-2026-03-13.tar.gz"
        with tarfile.open(backup_file, "w:gz") as outer:
            outer.add(str(inner_archive), arcname="djinn-claude-config.tar.gz")

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.restore_volume") as mock_restore,
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
        ):
            mock_restore.return_value = RunResult(returncode=1, stdout="", stderr="restore failed")
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            with pytest.raises(typer.Exit) as exc_info:
                backup_module.restore()
            assert exc_info.value.exit_code == 1

    def test_restore_aborts_on_user_cancel(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        backup_file = backups_dir / "djinn-backup-2026-03-13.tar.gz"
        with tarfile.open(backup_file, "w:gz") as _tar:
            pass

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.typer.confirm", side_effect=typer.Abort()),
            pytest.raises(typer.Abort),
        ):
            backup_module.restore()

    def test_restore_exits_when_archive_has_no_inner_volumes(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        inner_staging = tmp_path / "inner"
        inner_staging.mkdir()
        (inner_staging / "some-file.txt").write_text("not a tar")

        backup_file = backups_dir / "djinn-backup-2026-03-13.tar.gz"
        with tarfile.open(backup_file, "w:gz") as outer:
            outer.add(str(inner_staging / "some-file.txt"), arcname="some-file.txt")

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
        ):
            mock_tempfile.mkdtemp.return_value = str(staging_dir)
            with pytest.raises(typer.Exit) as exc_info:
                backup_module.restore()
            assert exc_info.value.exit_code == 1

    def test_restore_exits_when_backups_dir_missing(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does-not-exist"
        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", nonexistent),
        ):
            with pytest.raises(typer.Exit) as exc_info:
                backup_module.restore()
            assert exc_info.value.exit_code == 1

    def test_restore_with_multiple_volumes(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        inner_staging = tmp_path / "inner"
        inner_staging.mkdir()

        backup_file = backups_dir / "djinn-backup-2026-03-13.tar.gz"
        with tarfile.open(backup_file, "w:gz") as outer:
            for name in ["djinn-claude-config", "djinn-azure-config"]:
                archive = inner_staging / f"{name}.tar.gz"
                with tarfile.open(archive, "w:gz") as _tar:
                    pass
                outer.add(str(archive), arcname=archive.name)

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.restore_volume") as mock_restore,
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
        ):
            mock_restore.return_value = RunResult(returncode=0, stdout="", stderr="")
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            backup_module.restore()

            assert mock_restore.call_count == 2

    def test_restore_sync_path_uses_config_file_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        code_dir = tmp_path / "code"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)

        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        inner_source = tmp_path / "inner-source"
        inner_source.mkdir()
        (inner_source / "token.txt").write_text("secret\n")
        inner_archive = tmp_path / "djinn-sync-claude.tar.gz"
        with tarfile.open(inner_archive, "w:gz") as inner:
            inner.add(str(inner_source / "token.txt"), arcname="token.txt")

        backup_file = backups_dir / "djinn-backup-2026-03-13.tar.gz"
        with tarfile.open(backup_file, "w:gz") as outer:
            outer.add(str(inner_archive), arcname=inner_archive.name)

        with (
            patch(
                "djinn_in_a_box.commands.backup.load_config",
                side_effect=lambda: load_config(config_file),
            ),
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
        ):
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            backup_module.restore()

        assert (configured_root / "claude" / "token.txt").read_text() == "secret\n"

    def test_restore_rehardens_sync_target_after_extracting_legacy_archive(
        self, tmp_path: Path
    ) -> None:
        code_dir = tmp_path / "code"
        code_dir.mkdir()
        config = AppConfig(code_dir=code_dir, config_root=tmp_path / "config")
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o755)
        outside.chmod(0o755)
        inner_archives: list[Path] = []
        for name in ("claude", "codex"):
            legacy_target = tmp_path / f"legacy-{name}"
            legacy_target.mkdir(mode=0o755)
            legacy_target.chmod(0o755)
            (legacy_target / "credentials.json").write_text("secret\n")
            nested = legacy_target / "nested"
            nested.mkdir(mode=0o755)
            nested.chmod(0o755)
            (nested / "session.jsonl").write_text("history\n")
            if name == "claude":
                (legacy_target / "outside-link").symlink_to(outside, target_is_directory=True)
            inner_archive = tmp_path / f"djinn-sync-{name}.tar.gz"
            with tarfile.open(inner_archive, "w:gz") as inner:
                inner.add(legacy_target, arcname=".")
            inner_archives.append(inner_archive)
        backup_file = backups_dir / "djinn-backup-2026-03-13.tar.gz"
        with tarfile.open(backup_file, "w:gz") as outer:
            for inner_archive in inner_archives:
                outer.add(inner_archive, arcname=inner_archive.name)

        with (
            patch("djinn_in_a_box.commands.backup.load_config", return_value=config),
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.typer.confirm", return_value=True),
        ):
            backup_module.restore()

        for name in ("claude", "codex"):
            assert (config.config_root / name).stat().st_mode & 0o777 == 0o700
            assert (config.config_root / name / "nested").stat().st_mode & 0o777 == 0o700
        assert outside.stat().st_mode & 0o777 == 0o755

    def test_restore_rehardens_a_partially_extracted_sync_target(self, tmp_path: Path) -> None:
        code_dir = tmp_path / "code"
        code_dir.mkdir()
        config = AppConfig(code_dir=code_dir, config_root=tmp_path / "config")
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        legacy_claude = tmp_path / "legacy-claude"
        nested = legacy_claude / "nested"
        nested.mkdir(parents=True, mode=0o755)
        legacy_claude.chmod(0o755)
        nested.chmod(0o755)
        (nested / "session.jsonl").write_text("history\n")
        inner_archive = tmp_path / "djinn-sync-claude.tar.gz"
        with tarfile.open(inner_archive, "w:gz") as inner:
            inner.add(legacy_claude, arcname=".")
        inner_archive.write_bytes(inner_archive.read_bytes()[:-8])
        backup_file = backups_dir / "djinn-backup-2026-03-13.tar.gz"
        with tarfile.open(backup_file, "w:gz") as outer:
            outer.add(inner_archive, arcname=inner_archive.name)

        with (
            patch("djinn_in_a_box.commands.backup.load_config", return_value=config),
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.typer.confirm", return_value=True),
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module.restore()

        assert exc_info.value.exit_code == 1
        assert (config.config_root / "claude").stat().st_mode & 0o777 == 0o700
        assert (config.config_root / "claude" / "nested").stat().st_mode & 0o777 == 0o700

    def test_restore_decrypts_age_archive_before_restoring(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_dir = tmp_path / "inner"
        inner_dir.mkdir()
        inner_archive = inner_dir / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass
        outer_archive = tmp_path / "outer.tar.gz"
        with tarfile.open(outer_archive, "w:gz") as outer:
            outer.add(inner_archive, arcname=inner_archive.name)
        (backups_dir / "djinn-backup-2026-03-13.tar.gz").write_bytes(b"legacy")
        encrypted_backup = backups_dir / "djinn-backup-2026-03-13.tar.gz.age"
        encrypted_backup.write_bytes(b"age-encryption.org/v1\nplaceholder")

        def decrypt_for_test(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            Path(argv[argv.index("-o") + 1]).write_bytes(outer_archive.read_bytes())
            return subprocess.CompletedProcess(argv, 0)

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.restore_volume") as mock_restore,
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
        ):
            mock_restore.return_value = RunResult(0, "", "")
            mock_subprocess.run.side_effect = decrypt_for_test
            backup_module.restore()

        argv = mock_subprocess.run.call_args.args[0]
        assert argv[:2] == ["age", "--decrypt"]
        assert mock_restore.call_count == 1
        assert mock_restore.call_args.args[0] == "djinn-claude-config"
        assert not staging_dir.exists()

    def test_restore_decryption_failure_cleans_partial_cleartext(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        encrypted_backup = backups_dir / "djinn-backup-2026-03-13.tar.gz.age"
        encrypted_backup.write_bytes(b"age-encryption.org/v1\nplaceholder")

        def partial_decrypt(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            Path(argv[argv.index("-o") + 1]).write_bytes(b"partial cleartext")
            return subprocess.CompletedProcess(argv, 1)

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
            patch("djinn_in_a_box.commands.backup.restore_volume") as mock_restore,
            pytest.raises(typer.Exit) as exc_info,
        ):
            mock_subprocess.run.side_effect = partial_decrypt
            backup_module.restore()

        assert exc_info.value.exit_code == 1
        mock_restore.assert_not_called()
        assert not staging_dir.exists()

    def test_restore_decryption_exit_failure_precedes_valid_partial_cleartext(
        self, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        inner_staging = tmp_path / "inner"
        inner_staging.mkdir()
        inner_archive = inner_staging / "djinn-claude-config.tar.gz"
        with tarfile.open(inner_archive, "w:gz"):
            pass
        valid_outer = tmp_path / "valid-outer.tar.gz"
        with tarfile.open(valid_outer, "w:gz") as outer:
            outer.add(inner_archive, arcname=inner_archive.name)
        encrypted_backup = backups_dir / "djinn-backup-2026-03-13.tar.gz.age"
        encrypted_backup.write_bytes(b"age-encryption.org/v1\nplaceholder")

        def failed_decrypt(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            Path(argv[argv.index("-o") + 1]).write_bytes(valid_outer.read_bytes())
            return subprocess.CompletedProcess(argv, 1)

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
            patch("djinn_in_a_box.commands.backup.restore_volume") as mock_restore,
            pytest.raises(typer.Exit) as exc_info,
        ):
            mock_subprocess.run.side_effect = failed_decrypt
            mock_restore.return_value = RunResult(0, "", "")
            backup_module.restore()

        assert exc_info.value.exit_code == 1
        mock_restore.assert_not_called()
        assert not staging_dir.exists()

    def test_restore_removes_staging_when_age_is_interrupted(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        encrypted_backup = backups_dir / "djinn-backup-2026-03-13.tar.gz.age"
        encrypted_backup.write_bytes(b"age-encryption.org/v1\nplaceholder")

        def interrupted_decrypt(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            Path(argv[argv.index("-o") + 1]).write_bytes(b"partial cleartext")
            raise KeyboardInterrupt

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            patch("djinn_in_a_box.commands.backup.subprocess") as mock_subprocess,
            patch("djinn_in_a_box.commands.backup.restore_volume") as mock_restore_volume,
            patch("djinn_in_a_box.commands.backup.restore_sync_path") as mock_restore_sync_path,
            pytest.raises(KeyboardInterrupt),
        ):
            mock_subprocess.run.side_effect = interrupted_decrypt
            backup_module.restore()

        mock_restore_volume.assert_not_called()
        mock_restore_sync_path.assert_not_called()
        assert not staging_dir.exists()

    def test_restore_without_controlling_terminal_exits_with_recovery_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (backups_dir / "djinn-backup-2026-03-13.tar.gz.age").write_bytes(
            b"age-encryption.org/v1\nplaceholder"
        )

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile.mkdtemp", return_value=str(staging_dir)),
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            patch("djinn_in_a_box.commands.backup._has_controlling_terminal", return_value=False),
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module.restore()

        assert exc_info.value.exit_code == 1
        message = capsys.readouterr().err
        assert "controlling terminal" in message
        assert "interactive terminal" in message

    def test_restore_rejects_age_name_without_header(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        (backups_dir / "djinn-backup-2026-03-13.tar.gz.age").write_bytes(b"not age")

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.typer.confirm") as mock_confirm,
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module.restore()

        assert exc_info.value.exit_code == 1
        mock_confirm.assert_not_called()
        assert "intact age-encrypted archive" in capsys.readouterr().err

    def test_restore_invalid_legacy_archive_has_format_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        (backups_dir / "djinn-backup-2026-03-13.tar.gz").write_bytes(b"bad")

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module.restore()

        assert exc_info.value.exit_code == 1
        message = capsys.readouterr().err
        assert "neither a valid age archive nor a readable gzip tar" in message
        assert "intact backup archive" in message

    def test_restore_three_byte_gzip_has_format_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        (backups_dir / "djinn-backup-2026-03-13.tar.gz").write_bytes(b"\x1f\x8b\x08")

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            pytest.raises(typer.Exit) as exc_info,
        ):
            backup_module.restore()

        assert exc_info.value.exit_code == 1
        message = capsys.readouterr().err
        assert "neither a valid age archive nor a readable gzip tar" in message

    def test_age_recipient_round_trip_uses_production_file_orchestration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identity = tmp_path / "test-identity.txt"
        subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
        recipient = subprocess.run(
            ["age-keygen", "-y", str(identity)], check=True, capture_output=True, text=True
        ).stdout.strip()
        backups_dir = tmp_path / "backups"

        def write_inner_archive(_: str, staging_dir: Path) -> RunResult:
            with tarfile.open(staging_dir / "djinn-claude-config.tar.gz", "w:gz"):
                pass
            return RunResult(0, "", "")

        def encrypt_for_round_trip(source: Path, output: Path) -> list[str]:
            return ["age", "-r", recipient, "-o", str(output), str(source)]

        def decrypt_for_round_trip(source: Path, output: Path) -> list[str]:
            return [
                "age",
                "--decrypt",
                "-i",
                str(identity),
                "-o",
                str(output),
                str(source),
            ]

        monkeypatch.setattr(
            backup_module,
            "_age_encrypt_argv",
            encrypt_for_round_trip,
        )
        monkeypatch.setattr(
            backup_module,
            "_age_decrypt_argv",
            decrypt_for_round_trip,
        )

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_volumes_by_category",
                return_value=["djinn-claude-config"],
            ),
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch("djinn_in_a_box.commands.backup.backup_volume", side_effect=write_inner_archive),
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.subprocess", subprocess),
            patch("djinn_in_a_box.commands.backup.typer.confirm"),
            patch("djinn_in_a_box.commands.backup.restore_volume") as mock_restore,
        ):
            mock_restore.return_value = RunResult(0, "", "")
            backup_module.backup()
            archive = next(backups_dir.glob("djinn-backup-*.tar.gz.age"))
            assert archive.read_bytes().startswith(b"age-encryption.org/v1")
            backup_module.restore()

        mock_restore.assert_called_once()


class TestBackupCleanup:
    """Tests for staging directory cleanup."""

    def test_backup_search_uses_only_the_two_archive_patterns(self, tmp_path: Path) -> None:
        """A trailing-wildcard pattern would sweep up partial writes and let
        ``sorted()[-1]`` pick one as the newest archive. The unsuffixed name
        below is the case a hidden temp prefix does not cover.
        """
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        plain = backups_dir / "djinn-backup-2026-01-01.tar.gz"
        encrypted = backups_dir / "djinn-backup-2026-01-02.tar.gz.age"
        hidden_temp = backups_dir / ".djinn-backup-orphan"
        suffixed = backups_dir / "djinn-backup-2026-01-03.tar.gz.age.part"
        for archive in (plain, encrypted, hidden_temp, suffixed):
            archive.touch()

        with patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir):
            assert backup_module._list_backups() == [plain, encrypted]


class TestControllingTerminalCheck:
    """The terminal probe itself -- the module-wide fixture stubs it out."""

    @pytest.fixture(autouse=True)
    def mock_age_for_backup_commands(self) -> Generator[None]:
        """Override the module fixture so the real probe runs."""
        yield

    def test_terminal_check_asks_for_the_controlling_terminal(self) -> None:
        """`age -p` reads from /dev/tty, not stdin -- a `sys.stdin.isatty()`
        gate would refuse `djinn backup < /dev/null` from an interactive shell.
        This pins that the probe opens /dev/tty.
        """
        opened: list[str] = []

        def record_open(path: str, *_args: object, **_kwargs: object) -> Any:
            opened.append(path)
            msg = "no controlling terminal"
            raise OSError(msg)

        with patch("builtins.open", side_effect=record_open):
            assert backup_module._has_controlling_terminal() is False

        assert opened == ["/dev/tty"]

    def test_backup_removes_stale_temp_archives(self, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        temp = backups_dir / ".djinn-backup-orphan"
        temp.touch()

        with patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir):
            backup_module._remove_stale_temp_archives()

        assert not temp.exists()

    def test_backup_cleans_staging_on_failure(self, tmp_path: Path) -> None:
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.get_existing_volumes_by_category") as mock_get,
            patch("djinn_in_a_box.commands.backup.backup_volume") as mock_backup,
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
            patch("djinn_in_a_box.commands.backup.shutil") as mock_shutil,
        ):
            mock_get.return_value = ["djinn-claude-config"]
            mock_backup.return_value = RunResult(returncode=1, stdout="", stderr="fail")
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            with pytest.raises(typer.Exit):
                backup_module.backup()

            mock_shutil.rmtree.assert_called_once()

    def test_backup_attempts_all_volumes_before_aborting(self, tmp_path: Path) -> None:
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.get_existing_volumes_by_category") as mock_get,
            patch("djinn_in_a_box.commands.backup.backup_volume") as mock_backup,
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
        ):
            mock_get.side_effect = [["vol-a", "vol-b"], [], []]
            mock_backup.side_effect = [
                RunResult(returncode=0, stdout="", stderr=""),
                RunResult(returncode=1, stdout="", stderr="disk full"),
            ]
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            with pytest.raises(typer.Exit) as exc_info:
                backup_module.backup()
            assert exc_info.value.exit_code == 1
            assert mock_backup.call_count == 2
