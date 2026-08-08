from __future__ import annotations

import errno
import stat
from pathlib import Path

import pytest

from djinn_in_a_box.config import zones as zones_module
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import load_zone_assignments
from djinn_in_a_box.core import zone_migration
from djinn_in_a_box.core.docker import resolve_zone_roots
from djinn_in_a_box.core.exceptions import ZoneConfigurationError
from djinn_in_a_box.core.zone_migration import (
    adopt_archive_collision,
    reconcile_zone_assignments,
)


@pytest.fixture
def zone_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(zones_module, "ZONES_FILE", tmp_path / "zones.toml")
    return AppConfig(code_dir=projects, config_root=tmp_path / "config")


def test_migration_renames_into_an_empty_target_and_is_idempotent(zone_config: AppConfig) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    source.mkdir(parents=True)
    (source / "state.json").write_text("archive me")
    target = roots.local_root / "claude" / "jobs"
    target.mkdir(parents=True)

    first = reconcile_zone_assignments(zone_config)
    second = reconcile_zone_assignments(zone_config)

    assert len(first.moves) == 1
    assert not first.moves[0].copied_across_filesystems
    assert not source.exists()
    assert (target / "state.json").read_text() == "archive me"
    assert second.moves == ()
    assert second.collisions == ()


def test_cross_filesystem_migration_stages_on_destination_filesystem(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    source.mkdir(parents=True)
    (source / "state.json").write_text("copy me")
    target = roots.local_root / "claude" / "jobs"
    original_replace = zone_migration.os.replace
    original_mkdtemp = zone_migration.tempfile.mkdtemp
    staging_dirs: list[Path] = []

    def replace_with_exdev(source_path: str | Path, destination_path: str | Path) -> None:
        if Path(source_path) == source:
            raise OSError(errno.EXDEV, "Cross-device link")
        original_replace(source_path, destination_path)

    def record_staging_directory(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | Path | None = None,
    ) -> str:
        assert dir is not None
        staging_dirs.append(Path(dir))
        return original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)

    monkeypatch.setattr(zone_migration.os, "replace", replace_with_exdev)
    monkeypatch.setattr(zone_migration.tempfile, "mkdtemp", record_staging_directory)

    result = reconcile_zone_assignments(zone_config)

    assert len(result.moves) == 1
    assert result.moves[0].copied_across_filesystems
    assert staging_dirs == [target.parent]
    assert not source.exists()
    assert (target / "state.json").read_text() == "copy me"


@pytest.mark.parametrize("cross_filesystem", (False, True))
def test_migration_hardens_every_published_directory_without_following_symlinks(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch, cross_filesystem: bool
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    nested = source / "nested"
    nested.mkdir(parents=True)
    source.chmod(0o755)
    nested.chmod(0o755)
    outside = source.parent / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    (source / "outside-link").symlink_to(outside, target_is_directory=True)
    target = roots.local_root / "claude" / "jobs"

    if cross_filesystem:
        original_replace = zone_migration.os.replace

        def replace_with_exdev(source_path: str | Path, destination_path: str | Path) -> None:
            if Path(source_path) == source:
                raise OSError(errno.EXDEV, "Cross-device link")
            original_replace(source_path, destination_path)

        monkeypatch.setattr(zone_migration.os, "replace", replace_with_exdev)

    reconcile_zone_assignments(zone_config)

    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE((target / "nested").stat().st_mode) == 0o700
    assert (target / "outside-link").is_symlink()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_cross_filesystem_copy_failure_retains_source_for_resume(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    source.mkdir(parents=True)
    (source / "state.json").write_text("still here")
    target = roots.local_root / "claude" / "jobs"
    original_replace = zone_migration.os.replace
    original_copytree = zone_migration.shutil.copytree

    def replace_with_exdev(source_path: str | Path, destination_path: str | Path) -> None:
        if Path(source_path) == source:
            raise OSError(errno.EXDEV, "Cross-device link")
        original_replace(source_path, destination_path)

    def fail_copy(*_args: object, **_kwargs: object) -> Path:
        raise OSError("copy interrupted")

    monkeypatch.setattr(zone_migration.os, "replace", replace_with_exdev)
    monkeypatch.setattr(zone_migration.shutil, "copytree", fail_copy)

    with pytest.raises(OSError, match="copy interrupted"):
        reconcile_zone_assignments(zone_config)

    assert (source / "state.json").read_text() == "still here"
    assert target.is_dir()
    assert not any(target.iterdir())

    monkeypatch.setattr(zone_migration.shutil, "copytree", original_copytree)
    resumed = reconcile_zone_assignments(zone_config)

    assert len(resumed.moves) == 1
    assert (target / "state.json").read_text() == "still here"
    assert not source.exists()


def test_cross_filesystem_publish_change_preserves_both_trees(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    source.mkdir(parents=True)
    (source / "state.json").write_text("copied before publish")
    target = roots.local_root / "claude" / "jobs"
    original_replace = zone_migration.os.replace
    before_publish_calls = 0

    def replace_with_exdev(source_path: str | Path, destination_path: str | Path) -> None:
        if Path(source_path) == source:
            raise OSError(errno.EXDEV, "Cross-device link")
        original_replace(source_path, destination_path)

    def write_after_verification() -> None:
        nonlocal before_publish_calls
        before_publish_calls += 1
        if before_publish_calls == 2:
            (source / "written-during-publish.json").write_text("must not be deleted")

    monkeypatch.setattr(zone_migration.os, "replace", replace_with_exdev)

    with pytest.raises(ZoneConfigurationError, match="changed during publish"):
        reconcile_zone_assignments(zone_config, before_publish=write_after_verification)

    assert (source / "state.json").read_text() == "copied before publish"
    assert (source / "written-during-publish.json").read_text() == "must not be deleted"
    assert (target / "state.json").read_text() == "copied before publish"
    assert not (target / "written-during-publish.json").exists()


def test_retry_rehardens_a_published_destination_after_hardening_failure(
    zone_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    nested = source / "nested"
    nested.mkdir(parents=True)
    source.chmod(0o755)
    nested.chmod(0o755)
    (nested / "state.json").write_text("repair on retry")
    target = roots.local_root / "claude" / "jobs"
    original_chmod = zone_migration.os.chmod
    target_chmod_calls = 0

    def fail_post_publish_hardening(
        path: Path,
        mode: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal target_chmod_calls
        if path == target:
            target_chmod_calls += 1
            if target_chmod_calls == 2:
                raise OSError("hardening interrupted")
        original_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(zone_migration.os, "chmod", fail_post_publish_hardening)

    with pytest.raises(OSError, match="hardening interrupted"):
        reconcile_zone_assignments(zone_config)

    assert not source.exists()
    assert (target / "nested").stat().st_mode & 0o777 == 0o755

    resumed = reconcile_zone_assignments(zone_config)

    assert resumed.moves == ()
    assert stat.S_IMODE((target / "nested").stat().st_mode) == 0o700


def test_collision_preserves_every_assigned_tree_without_moving_anything(
    zone_config: AppConfig,
) -> None:
    roots = resolve_zone_roots(zone_config)
    config_jobs = roots.config_root / "claude" / "jobs"
    local_jobs = roots.local_root / "claude" / "jobs"
    config_jobs.mkdir(parents=True)
    local_jobs.mkdir(parents=True)
    (config_jobs / "config.txt").write_text("config")
    (local_jobs / "local.txt").write_text("local")
    unrelated = roots.config_root / "gemini" / "tmp"
    unrelated.mkdir(parents=True)
    (unrelated / "would-have-moved.txt").write_text("keep")

    result = reconcile_zone_assignments(zone_config)

    assert result.moves == ()
    assert len(result.collisions) == 1
    assert (config_jobs / "config.txt").read_text() == "config"
    assert (local_jobs / "local.txt").read_text() == "local"
    assert (unrelated / "would-have-moved.txt").read_text() == "keep"


def test_migration_rejects_a_symlinked_destination_before_moving_data(
    zone_config: AppConfig,
) -> None:
    roots = resolve_zone_roots(zone_config)
    source = roots.config_root / "claude" / "jobs"
    source.mkdir(parents=True)
    (source / "state.json").write_text("keep local")
    outside = source.parent / "outside"
    outside.mkdir()
    roots.local_root.mkdir()
    (roots.local_root / "claude").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ZoneConfigurationError, match="symlinked"):
        reconcile_zone_assignments(zone_config)

    assert (source / "state.json").read_text() == "keep local"
    assert not (outside / "jobs").exists()


def test_reassignment_rejects_a_symlinked_previous_zone_source(
    zone_config: AppConfig,
) -> None:
    zones_module.ZONES_FILE.write_text('[zones.claude]\nshared = ["newdir/cache"]\n')
    roots = resolve_zone_roots(zone_config)
    outside = roots.local_root / "outside"
    previous_source = outside / "cache"
    previous_source.mkdir(parents=True)
    (previous_source / "state.json").write_text("must stay outside")
    (roots.local_root / "claude").mkdir(parents=True)
    (roots.local_root / "claude" / "newdir").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ZoneConfigurationError, match="symlinked"):
        reconcile_zone_assignments(zone_config)

    assert (previous_source / "state.json").read_text() == "must stay outside"
    assert not (roots.shared_root / "claude" / "newdir" / "cache").exists()


def test_reconciliation_moves_data_from_a_previous_zone_after_reassignment(
    zone_config: AppConfig,
) -> None:
    roots = resolve_zone_roots(zone_config)
    previous_local = roots.local_root / "claude" / "projects"
    previous_local.mkdir(parents=True)
    (previous_local / "session.jsonl").write_text("shared now")
    target = roots.shared_root / "claude" / "projects"

    result = reconcile_zone_assignments(zone_config)

    assert len(result.moves) == 1
    assert result.moves[0].source == previous_local
    assert not previous_local.exists()
    assert (target / "session.jsonl").read_text() == "shared now"


def test_empty_source_is_removed_while_populated_target_is_retained(zone_config: AppConfig) -> None:
    roots = resolve_zone_roots(zone_config)
    empty_source = roots.config_root / "claude" / "jobs"
    populated_target = roots.local_root / "claude" / "jobs"
    empty_source.mkdir(parents=True)
    populated_target.mkdir(parents=True)
    (populated_target / "state.json").write_text("already migrated")

    result = reconcile_zone_assignments(zone_config)

    assert result.moves == ()
    assert not empty_source.exists()
    assert (populated_target / "state.json").read_text() == "already migrated"


def test_archive_adoption_moves_the_displaced_zone_tree_aside(zone_config: AppConfig) -> None:
    roots = resolve_zone_roots(zone_config)
    archive_copy = roots.config_root / "claude" / "projects"
    zone_copy = roots.shared_root / "claude" / "projects"
    archive_copy.mkdir(parents=True)
    zone_copy.mkdir(parents=True)
    (archive_copy / "archive.jsonl").write_text("archive")
    (zone_copy / "live.jsonl").write_text("live")
    assignments = load_zone_assignments(zone_config)
    collision = reconcile_zone_assignments(zone_config, assignments=assignments).collisions[0]

    displaced = adopt_archive_collision(collision, zone_config)

    assert displaced.parent == zone_copy.parent
    assert displaced.name.startswith("projects.pre-restore-")
    assert (displaced / "live.jsonl").read_text() == "live"
    assert (zone_copy / "archive.jsonl").read_text() == "archive"
    assert not archive_copy.exists()
