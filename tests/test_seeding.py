from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import djinn_in_a_box.core.seeding as seeding_mod
from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core.seeding import (
    CLAUDE_BASELINE_SEEDS,
    SEED_MANIFEST,
    SHARED_SEEDS,
    WORKFLOW_ROOT_NAMES,
    SeedEntry,
    SeedingError,
    pristine_workflow_seed_digest,
    seed_config,
)


def _write_seed_fixture(project_root: Path) -> None:
    seed_root = project_root / "templates" / "seed"
    for entry in SEED_MANIFEST:
        source = seed_root / entry.source
        source.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "file":
            source.write_text(f"seed for {entry.source}\n", encoding="utf-8")
        else:
            source.mkdir(parents=True, exist_ok=True)
            (source / ".gitkeep").write_text("", encoding="utf-8")


def _target(project_root: Path, entry: SeedEntry) -> Path:
    return project_root / entry.target


def test_empty_target_creates_all_manifest_targets(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)

    created = seed_config(tmp_path)

    expected = [tmp_path / "config" / name for name in WORKFLOW_ROOT_NAMES]
    expected.extend(_target(tmp_path, entry) for entry in SEED_MANIFEST)
    assert created == expected
    for entry in SEED_MANIFEST:
        target = _target(tmp_path, entry)
        if entry.kind == "file":
            assert target.is_file()
            assert target.read_text(encoding="utf-8") == f"seed for {entry.source}\n"
        else:
            assert target.is_dir()
            assert not (target / ".gitkeep").exists()


def test_user_modified_file_remains_byte_identical(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    seed_config(tmp_path)
    target = tmp_path / "config" / "claude" / "settings.json"
    modified = b'{"local": true}\n'
    target.write_bytes(modified)

    assert seed_config(tmp_path) == []
    assert target.read_bytes() == modified


def test_run_twice_second_run_returns_empty(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)

    assert seed_config(tmp_path)
    assert seed_config(tmp_path) == []


def test_initialized_claude_root_is_preserved_without_filling_baseline(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    claude_root = tmp_path / "config" / "claude"
    claude_root.mkdir(parents=True)
    custom = claude_root / "custom.txt"
    custom.write_bytes(b"operator bytes\n")

    created = seed_config(tmp_path)

    expected = [tmp_path / "config" / "codex", tmp_path / "config" / "opencode"]
    expected.extend(_target(tmp_path, entry) for entry in SHARED_SEEDS)
    assert created == expected
    assert custom.read_bytes() == b"operator bytes\n"
    for entry in CLAUDE_BASELINE_SEEDS:
        assert not _target(tmp_path, entry).exists()


@pytest.mark.parametrize("source", ["codex", "opencode"])
def test_non_claude_first_use_creates_empty_workflow_roots(
    tmp_path: Path, source: ConfigSyncSource
) -> None:
    _write_seed_fixture(tmp_path)

    seed_config(tmp_path, source=source)

    for name in WORKFLOW_ROOT_NAMES:
        root = tmp_path / "config" / name
        assert root.is_dir()
        assert list(root.iterdir()) == []
    for entry in SHARED_SEEDS:
        assert _target(tmp_path, entry).exists()


def test_wrong_type_targets_are_repaired(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    seed_config(tmp_path)
    file_entry = next(entry for entry in SHARED_SEEDS if entry.kind == "file")
    directory_entry = next(entry for entry in SHARED_SEEDS if entry.kind == "directory")
    file_target = _target(tmp_path, file_entry)
    directory_target = _target(tmp_path, directory_entry)

    file_target.unlink()
    file_target.mkdir()
    directory_target.rmdir()
    directory_target.write_text("wrong type\n", encoding="utf-8")

    created = seed_config(tmp_path)

    assert created == [directory_target, file_target]
    assert file_target.is_file()
    assert file_target.read_text(encoding="utf-8") == f"seed for {file_entry.source}\n"
    assert directory_target.is_dir()


@pytest.mark.parametrize("collision_kind", ["file", "symlink"])
def test_workflow_root_collision_fails_closed_before_any_mutation(
    tmp_path: Path, collision_kind: str
) -> None:
    _write_seed_fixture(tmp_path)
    config_root = tmp_path / "config"
    config_root.mkdir()
    collision = config_root / "codex"
    if collision_kind == "file":
        collision.write_bytes(b"operator sentinel\n")
    else:
        target = tmp_path / "operator-codex"
        target.mkdir()
        (target / "sentinel").write_bytes(b"operator sentinel\n")
        collision.symlink_to(target, target_is_directory=True)

    with pytest.raises(SeedingError, match="must be a real directory"):
        seed_config(tmp_path)

    assert not (config_root / "claude").exists()
    assert not (config_root / "opencode").exists()
    assert collision.is_symlink() if collision_kind == "symlink" else collision.is_file()
    if collision_kind == "file":
        assert collision.read_bytes() == b"operator sentinel\n"
    else:
        assert (collision / "sentinel").read_bytes() == b"operator sentinel\n"


def test_non_removable_wrong_type_raises_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_fixture(tmp_path)
    target = tmp_path / "config" / "mcp-servers.json"
    target.mkdir(parents=True)

    def raise_permission_error(_path: Path) -> None:
        raise PermissionError("root-owned")

    monkeypatch.setattr(seeding_mod, "_remove_path", raise_permission_error)

    with pytest.raises(SeedingError, match="sudo rm"):
        seed_config(tmp_path)


def test_missing_templates_seed_raises_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(SeedingError, match="templates/seed"):
        seed_config(tmp_path)


def test_pristine_workflow_seed_digest_matches_shipped_bytes(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    relative = Path("claude/CLAUDE.md")
    template = tmp_path / "templates" / "seed" / "config" / relative

    digest = pristine_workflow_seed_digest(tmp_path, relative)

    assert digest == hashlib.sha256(template.read_bytes()).hexdigest()
    assert pristine_workflow_seed_digest(tmp_path, Path("claude/custom.md")) is None
    assert pristine_workflow_seed_digest(tmp_path, Path("../outside")) is None
