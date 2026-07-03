from __future__ import annotations

from pathlib import Path

import pytest

import djinn_in_a_box.core.seeding as seeding_mod
from djinn_in_a_box.core.seeding import SEED_MANIFEST, SeedEntry, SeedingError, seed_config


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

    expected = [_target(tmp_path, entry) for entry in SEED_MANIFEST]
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


def test_partially_populated_target_only_fills_missing_entries(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    existing_entries = SEED_MANIFEST[:3]
    for entry in existing_entries:
        target = _target(tmp_path, entry)
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "file":
            target.write_text("already here\n", encoding="utf-8")
        else:
            target.mkdir(parents=True)

    created = seed_config(tmp_path)

    expected = [_target(tmp_path, entry) for entry in SEED_MANIFEST[3:]]
    assert created == expected
    for entry in existing_entries:
        target = _target(tmp_path, entry)
        if entry.kind == "file":
            assert target.read_text(encoding="utf-8") == "already here\n"
        else:
            assert target.is_dir()


def test_wrong_type_targets_are_repaired(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    seed_config(tmp_path)
    file_entry = next(entry for entry in SEED_MANIFEST if entry.kind == "file")
    directory_entry = next(entry for entry in SEED_MANIFEST if entry.kind == "directory")
    file_target = _target(tmp_path, file_entry)
    directory_target = _target(tmp_path, directory_entry)

    file_target.unlink()
    file_target.mkdir()
    directory_target.rmdir()
    directory_target.write_text("wrong type\n", encoding="utf-8")

    created = seed_config(tmp_path)

    assert created == [file_target, directory_target]
    assert file_target.is_file()
    assert file_target.read_text(encoding="utf-8") == f"seed for {file_entry.source}\n"
    assert directory_target.is_dir()


def test_non_removable_wrong_type_raises_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_fixture(tmp_path)
    target = tmp_path / "config" / "claude" / "CLAUDE.md"
    target.mkdir(parents=True)

    def raise_permission_error(_path: Path) -> None:
        raise PermissionError("root-owned")

    monkeypatch.setattr(seeding_mod, "_remove_path", raise_permission_error)

    with pytest.raises(SeedingError, match="sudo rm"):
        seed_config(tmp_path)


def test_missing_templates_seed_raises_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(SeedingError, match="templates/seed"):
        seed_config(tmp_path)
