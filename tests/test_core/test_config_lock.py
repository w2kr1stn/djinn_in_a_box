from __future__ import annotations

from pathlib import Path

import pytest

from djinn_in_a_box.core.config_lock import ConfigDirectoryLockError, config_directory_lock


def test_config_directory_lock_creates_no_artifact(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with config_directory_lock(config_dir, exclusive=False) as descriptor:
        assert descriptor >= 0
    assert list(config_dir.iterdir()) == []
    with config_directory_lock(config_dir, exclusive=True) as descriptor:
        assert descriptor >= 0
        assert list(config_dir.iterdir()) == []


def test_config_directory_lock_never_follows_directory_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with (
        pytest.raises(ConfigDirectoryLockError, match="cannot be locked safely"),
        config_directory_lock(alias, exclusive=True),
    ):
        pytest.fail("lock unexpectedly followed a directory symlink")
