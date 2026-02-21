"""Tests for ai_dev_base.core.paths module."""

from pathlib import Path

import pytest

from ai_dev_base.core.paths import (
    get_project_root,
    resolve_mount_path,
)


class TestGetProjectRoot:
    """Tests for get_project_root()."""

    def test_finds_project_root(self) -> None:
        """get_project_root() should find the directory with docker-compose.yml."""
        root = get_project_root()
        assert root.is_dir()
        assert root.is_absolute()
        assert (root / "docker-compose.yml").exists()
        assert (root / "pyproject.toml").exists()


class TestResolveMountPath:
    """Tests for resolve_mount_path()."""

    def test_tilde_expansion(self) -> None:
        """resolve_mount_path('~') should expand to home directory."""
        result = resolve_mount_path("~")
        assert result == Path.home()
        assert result.is_absolute()

    def test_relative_path_resolution(self, change_dir: Path) -> None:
        """resolve_mount_path('.') should resolve to current directory."""
        result = resolve_mount_path(".")
        assert result == change_dir
        assert result.is_absolute()

    def test_relative_subdir(self, change_dir: Path) -> None:
        """resolve_mount_path('./subdir') should resolve relative paths."""
        subdir = change_dir / "subdir"
        subdir.mkdir()

        result = resolve_mount_path("./subdir")
        assert result == subdir
        assert result.is_absolute()

    def test_absolute_path(self, tmp_path: Path) -> None:
        """resolve_mount_path('/absolute/path') should work."""
        result = resolve_mount_path(str(tmp_path))
        assert result == tmp_path
        assert result.is_absolute()

    def test_nonexistent_path_raises(self) -> None:
        """resolve_mount_path() should raise FileNotFoundError for missing paths."""
        with pytest.raises(FileNotFoundError, match="does not exist"):
            resolve_mount_path("/nonexistent/path/that/should/not/exist")

    def test_file_path_raises(self, tmp_path: Path) -> None:
        """resolve_mount_path() should raise NotADirectoryError for files."""
        test_file = tmp_path / "test_file.txt"
        test_file.write_text("test")

        with pytest.raises(NotADirectoryError, match="not a directory"):
            resolve_mount_path(test_file)

    def test_symlink_resolution(self, tmp_path: Path) -> None:
        """resolve_mount_path() should resolve symlinks."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)

        result = resolve_mount_path(link)
        assert result == real_dir
