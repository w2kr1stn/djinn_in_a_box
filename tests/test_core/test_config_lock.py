from __future__ import annotations

from pathlib import Path

from djinn_in_a_box.core.config_lock import config_directory_lock


def test_config_directory_lock_creates_no_artifact(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with config_directory_lock(config_dir, exclusive=False) as descriptor:
        assert descriptor >= 0
    assert list(config_dir.iterdir()) == []
    with config_directory_lock(config_dir, exclusive=True) as descriptor:
        assert descriptor >= 0
        assert list(config_dir.iterdir()) == []
