"""Tests for config/models.py Pydantic configuration models."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dev_base.config.models import (
    AppConfig,
    ResourceLimits,
    ShellConfig,
    validate_memory_format,
)


class TestValidateMemoryFormat:
    """Tests for validate_memory_format helper function."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("12G", "12G"),
            ("4096M", "4096M"),
            ("512K", "512K"),
            ("8g", "8G"),  # lowercase normalized
            ("1024m", "1024M"),
            ("256k", "256K"),
        ],
    )
    def test_valid_formats(self, value: str, expected: str) -> None:
        """Test valid memory format strings are accepted and normalized."""
        result = validate_memory_format(value)
        assert result == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",  # empty
            "12",  # no suffix
            "G12",  # suffix before number
            "12 G",  # space
            "12GB",  # two-letter suffix
            "12.5G",  # decimal
            "-12G",  # negative
            "12T",  # invalid suffix
            "abc",  # non-numeric
        ],
    )
    def test_invalid_formats(self, value: str) -> None:
        """Test invalid memory format strings raise ValueError."""
        with pytest.raises(ValueError, match="Invalid memory format"):
            validate_memory_format(value)


class TestResourceLimits:
    """Tests for ResourceLimits model."""

    def test_memory_format_normalization(self) -> None:
        """Test that lowercase memory suffixes are normalized to uppercase."""
        limits = ResourceLimits(memory_limit="8g", memory_reservation="2g")
        assert limits.memory_limit == "8G"
        assert limits.memory_reservation == "2G"

    def test_invalid_memory_format(self) -> None:
        """Test that invalid memory format raises validation error."""
        with pytest.raises(ValidationError, match="Invalid memory format"):
            ResourceLimits(memory_limit="invalid")

    def test_cpu_limit_min_max(self) -> None:
        """Test CPU limit boundaries."""
        # Valid minimum (must set cpu_reservation <= cpu_limit)
        limits = ResourceLimits(cpu_limit=1, cpu_reservation=1)
        assert limits.cpu_limit == 1

        # Valid maximum
        limits = ResourceLimits(cpu_limit=128)
        assert limits.cpu_limit == 128

        # Below minimum
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            ResourceLimits(cpu_limit=0)

        # Above maximum
        with pytest.raises(ValidationError, match="less than or equal to 128"):
            ResourceLimits(cpu_limit=256)

    def test_cpu_reservation_cannot_exceed_limit(self) -> None:
        """Test that cpu_reservation cannot exceed cpu_limit."""
        # Valid: reservation <= limit
        limits = ResourceLimits(cpu_limit=4, cpu_reservation=2)
        assert limits.cpu_reservation == 2

        # Invalid: reservation > limit
        with pytest.raises(ValidationError, match="cannot exceed"):
            ResourceLimits(cpu_limit=2, cpu_reservation=4)


class TestShellConfig:
    """Tests for ShellConfig model."""

    def test_omp_theme_path_expansion(self, mock_home: Path) -> None:
        """Test that tilde in omp_theme_path is expanded."""
        theme_path = mock_home / ".config" / "theme.omp.json"
        theme_path.parent.mkdir(parents=True, exist_ok=True)
        theme_path.touch()

        shell = ShellConfig(omp_theme_path="~/.config/theme.omp.json")
        # Should be expanded to absolute path
        assert shell.omp_theme_path is not None
        assert shell.omp_theme_path.is_absolute()
        assert str(shell.omp_theme_path).endswith(".config/theme.omp.json")


class TestAppConfig:
    """Tests for AppConfig model."""

    def test_code_dir_tilde_expansion(self, mock_home: Path) -> None:
        """Test that tilde in code_dir is expanded."""
        projects_dir = mock_home / "projects"
        projects_dir.mkdir()

        config = AppConfig(code_dir="~/projects")
        assert config.code_dir == projects_dir

    def test_code_dir_validation_not_exists(self, tmp_path: Path) -> None:
        """Test that non-existent code_dir raises validation error."""
        nonexistent = tmp_path / "does_not_exist"
        with pytest.raises(ValidationError, match="code_dir does not exist"):
            AppConfig(code_dir=nonexistent)

    def test_code_dir_validation_not_directory(self, tmp_path: Path) -> None:
        """Test that file (not directory) code_dir raises validation error."""
        file_path = tmp_path / "file.txt"
        file_path.touch()
        with pytest.raises(ValidationError, match="code_dir is not a directory"):
            AppConfig(code_dir=file_path)

    def test_nested_model_from_dict(self, tmp_path: Path) -> None:
        """Test creating AppConfig from nested dictionary (TOML-like)."""
        data = {
            "code_dir": str(tmp_path),
            "timezone": "Europe/Berlin",
            "resources": {
                "cpu_limit": 4,
                "memory_limit": "8G",
            },
            "shell": {
                "skip_mounts": True,
            },
        }
        config = AppConfig.model_validate(data)
        assert config.resources.cpu_limit == 4
        assert config.shell.skip_mounts is True
