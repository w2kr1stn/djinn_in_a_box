"""Tests for djinn_in_a_box.core.paths module."""

from pathlib import Path

import pytest

from djinn_in_a_box.core.paths import resolve_mount_path


class TestResolveMountPath:
    """Tests for resolve_mount_path()."""

    def test_tilde_expansion(self) -> None:
        """resolve_mount_path('~') should expand to home directory."""
        result = resolve_mount_path("~")
        assert result == Path.home()
        assert result.is_absolute()

    def test_relative_path_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_mount_path('.') should resolve to current directory."""
        monkeypatch.chdir(tmp_path)
        result = resolve_mount_path(".")
        assert result == tmp_path
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
