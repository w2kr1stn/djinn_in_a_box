"""Tests for volume backup and restore commands."""

from __future__ import annotations

import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from djinn_in_a_box.commands import backup as backup_module
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.docker import RunResult


@pytest.fixture(autouse=True)
def mock_backup_config(mock_app_config: AppConfig) -> None:
    """Existing command tests do not exercise config loading."""
    with patch("djinn_in_a_box.commands.backup.load_config", return_value=mock_app_config):
        yield


class TestGuardNoContainersRunning:
    """Tests for _guard_no_containers_running."""

    def test_passes_when_no_containers(self) -> None:
        with patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]):
            backup_module._guard_no_containers_running()

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

        with (
            patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=[]),
            patch("djinn_in_a_box.commands.backup.get_existing_volumes_by_category") as mock_get,
            patch(
                "djinn_in_a_box.commands.backup.get_existing_sync_paths_by_category",
                return_value=[],
            ),
            patch("djinn_in_a_box.commands.backup.backup_volume") as mock_backup,
            patch("djinn_in_a_box.commands.backup.BACKUPS_DIR", backups_dir),
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
        ):
            mock_get.side_effect = [["djinn-claude-config"], [], []]
            mock_backup.return_value = RunResult(returncode=0, stdout="", stderr="")
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            fake_archive = staging_dir / "djinn-claude-config.tar.gz"
            with tarfile.open(fake_archive, "w:gz") as _tar:
                pass  # empty archive is fine for test

            backup_module.backup()

            mock_backup.assert_called_once_with("djinn-claude-config", staging_dir)
            archives = list(backups_dir.glob("djinn-backup-*.tar.gz"))
            assert len(archives) == 1

    def test_backup_replaces_previous(self, tmp_path: Path) -> None:
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
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
        ):
            mock_get.return_value = ["djinn-claude-config"]
            mock_backup.return_value = RunResult(returncode=0, stdout="", stderr="")
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            fake_archive = staging_dir / "djinn-claude-config.tar.gz"
            with tarfile.open(fake_archive, "w:gz") as _tar:
                pass

            backup_module.backup()

            assert not old_backup.exists()
            archives = list(backups_dir.glob("djinn-backup-*.tar.gz"))
            assert len(archives) == 1

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
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
        ):
            mock_get.return_value = ["djinn-uv-cache"]
            mock_backup.return_value = RunResult(returncode=0, stdout="", stderr="")
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

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
            patch("djinn_in_a_box.commands.backup.tempfile") as mock_tempfile,
        ):
            mock_backup_sync.return_value = RunResult(returncode=0, stdout="", stderr="")
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            backup_module.backup(categories=["credentials"])

        mock_backup_sync.assert_called_once_with(configured_root / "claude", staging_dir)


class TestRestoreCommand:
    """Tests for the restore command."""

    def test_aborts_when_containers_running(self) -> None:
        with patch("djinn_in_a_box.commands.backup.get_running_containers", return_value=["djinn"]):
            with pytest.raises(typer.Exit) as exc_info:
                backup_module.restore()
            assert exc_info.value.exit_code == 1

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
        ):
            mock_restore.return_value = RunResult(returncode=0, stdout="", stderr="")
            mock_tempfile.mkdtemp.return_value = str(staging_dir)

            backup_module.restore()

            mock_restore.assert_called_once_with("djinn-claude-config", staging_dir)

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


class TestBackupCleanup:
    """Tests for staging directory cleanup."""

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
