"""Tests for ai_dev_base.config.loader module."""

# ruff: noqa: SLF001 - Testing private functions is intentional

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_dev_base.config.loader import (
    ConfigNotFoundError,
    ConfigValidationError,
    _transform_config_to_toml,
    _transform_toml_to_config,
    load_agents,
    load_config,
    save_config,
)
from ai_dev_base.config.models import AppConfig

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def sample_config_toml(tmp_path: Path) -> Path:
    """Create a sample config.toml file."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[general]
code_dir = "/home/user/projects"
timezone = "America/New_York"

[shell]
skip_mounts = true

[resources]
cpu_limit = 8
memory_limit = "16G"
cpu_reservation = 4
memory_reservation = "8G"
"""
    )
    return config_file


@pytest.fixture
def sample_agents_toml(tmp_path: Path) -> Path:
    """Create a sample agents.toml file."""
    agents_file = tmp_path / "agents.toml"
    agents_file.write_text(
        """
[agents.test-agent]
binary = "test-cli"
description = "Test Agent"
headless_flags = ["-h"]
read_only_flags = ["--readonly"]
write_flags = ["--write"]
json_flags = ["--json"]
model_flag = "-m"
prompt_template = "$PROMPT"
"""
    )
    return agents_file


@pytest.fixture
def sample_code_dir(tmp_path: Path) -> Path:
    """Create a sample code directory that exists."""
    code_dir = tmp_path / "projects"
    code_dir.mkdir()
    return code_dir


@pytest.fixture
def valid_config_toml(tmp_path: Path, sample_code_dir: Path) -> Path:
    """Create a valid config.toml with existing code_dir."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[general]
code_dir = "{sample_code_dir}"
timezone = "UTC"

[shell]
skip_mounts = false

[resources]
cpu_limit = 4
memory_limit = "8G"
cpu_reservation = 2
memory_reservation = "4G"
"""
    )
    return config_file


# =============================================================================
# ConfigNotFoundError Tests
# =============================================================================


class TestConfigNotFoundError:
    """Tests for ConfigNotFoundError exception."""

    def test_error_message_contains_path(self) -> None:
        """Error message should include the missing path."""
        path = Path("/nonexistent/config.toml")
        error = ConfigNotFoundError(path)

        assert str(path) in str(error)
        assert "codeagent init" in str(error)

    def test_path_attribute(self) -> None:
        """Error should store the path as an attribute."""
        path = Path("/test/path.toml")
        error = ConfigNotFoundError(path)

        assert error.path == path


# =============================================================================
# ConfigValidationError Tests
# =============================================================================


class TestConfigValidationError:
    """Tests for ConfigValidationError exception."""

    def test_message_only(self) -> None:
        """Should work with just a message."""
        error = ConfigValidationError("Test error")

        assert str(error) == "Test error"


# =============================================================================
# TOML Transformation Tests
# =============================================================================


class TestTransformTomlToConfig:
    """Tests for _transform_toml_to_config function."""

    def test_extracts_general_section(self) -> None:
        """Should extract code_dir and timezone from [general]."""
        data = {
            "general": {"code_dir": "/path", "timezone": "UTC"},
        }
        result = _transform_toml_to_config(data)

        assert result["code_dir"] == "/path"
        assert result["timezone"] == "UTC"

    def test_passes_through_shell_section(self) -> None:
        """Should pass through [shell] section."""
        data = {
            "general": {"code_dir": "/path"},
            "shell": {"skip_mounts": True},
        }
        result = _transform_toml_to_config(data)

        assert result["shell"]["skip_mounts"] is True

    def test_passes_through_resources_section(self) -> None:
        """Should pass through [resources] section."""
        data = {
            "general": {"code_dir": "/path"},
            "resources": {"cpu_limit": 8},
        }
        result = _transform_toml_to_config(data)

        assert result["resources"]["cpu_limit"] == 8

    def test_handles_missing_sections(self) -> None:
        """Should handle missing optional sections."""
        data: dict[str, Any] = {"general": {}}
        result = _transform_toml_to_config(data)

        assert "shell" not in result
        assert "resources" not in result


class TestTransformConfigToToml:
    """Tests for _transform_config_to_toml function."""

    def test_creates_general_section(self, sample_code_dir: Path) -> None:
        """Should create [general] section with code_dir and timezone."""
        config = AppConfig(code_dir=sample_code_dir, timezone="UTC")
        result = _transform_config_to_toml(config)

        assert result["general"]["code_dir"] == str(sample_code_dir)
        assert result["general"]["timezone"] == "UTC"

    def test_creates_shell_section(self, sample_code_dir: Path) -> None:
        """Should create [shell] section."""
        config = AppConfig(code_dir=sample_code_dir)
        result = _transform_config_to_toml(config)

        assert "skip_mounts" in result["shell"]

    def test_creates_resources_section(self, sample_code_dir: Path) -> None:
        """Should create [resources] section with all fields."""
        config = AppConfig(code_dir=sample_code_dir)
        result = _transform_config_to_toml(config)

        assert "cpu_limit" in result["resources"]
        assert "memory_limit" in result["resources"]
        assert "cpu_reservation" in result["resources"]
        assert "memory_reservation" in result["resources"]

    def test_includes_omp_theme_path_when_set(self, sample_code_dir: Path) -> None:
        """Should include omp_theme_path only when set."""
        config = AppConfig(code_dir=sample_code_dir)

        # Without omp_theme_path
        result = _transform_config_to_toml(config)
        assert "omp_theme_path" not in result["shell"]


# =============================================================================
# load_config Tests
# =============================================================================


class TestLoadConfig:
    """Tests for load_config function."""

    def test_raises_config_not_found_for_missing_file(self, tmp_path: Path) -> None:
        """Should raise ConfigNotFoundError for missing file."""
        missing_path = tmp_path / "nonexistent.toml"

        with pytest.raises(ConfigNotFoundError) as exc_info:
            load_config(missing_path)

        assert exc_info.value.path == missing_path

    def test_raises_config_validation_error_for_invalid_toml(self, tmp_path: Path) -> None:
        """Should raise ConfigValidationError for invalid TOML syntax."""
        invalid_file = tmp_path / "invalid.toml"
        invalid_file.write_text("this is not valid TOML [[[")

        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(invalid_file)

        assert "Invalid TOML syntax" in str(exc_info.value)

    def test_loads_valid_config(self, valid_config_toml: Path) -> None:
        """Should load and return valid AppConfig."""
        config = load_config(valid_config_toml)

        assert isinstance(config, AppConfig)
        assert config.timezone == "UTC"
        assert config.resources.cpu_limit == 4

    def test_raises_validation_error_for_invalid_values(self, tmp_path: Path) -> None:
        """Should raise ConfigValidationError for invalid field values."""
        invalid_file = tmp_path / "invalid_values.toml"
        invalid_file.write_text(
            """
[general]
code_dir = "/nonexistent/path/that/does/not/exist"
timezone = "Invalid"
"""
        )

        with pytest.raises(ConfigValidationError):
            load_config(invalid_file)


# =============================================================================
# load_agents Tests
# =============================================================================


class TestLoadAgents:
    """Tests for load_agents function."""

    def test_raises_error_for_explicit_missing_path(self, tmp_path: Path) -> None:
        """Should raise ConfigNotFoundError for explicit missing path."""
        missing_path = tmp_path / "missing.toml"

        with pytest.raises(ConfigNotFoundError):
            load_agents(missing_path)

    def test_loads_agents_from_toml(self, sample_agents_toml: Path) -> None:
        """Should load agents from TOML file."""
        agents = load_agents(sample_agents_toml)

        assert "test-agent" in agents
        assert agents["test-agent"].binary == "test-cli"
        assert agents["test-agent"].description == "Test Agent"

    def test_falls_back_to_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should fall back to DEFAULT_AGENTS when no files exist."""
        monkeypatch.setattr(
            "ai_dev_base.config.loader.AGENTS_FILE",
            Path("/nonexistent/agents.toml"),
        )
        monkeypatch.setattr(
            "ai_dev_base.config.loader.get_project_root",
            lambda: Path("/nonexistent"),
        )

        agents = load_agents()

        assert "claude" in agents
        assert "gemini" in agents
        assert "codex" in agents
        assert "opencode" in agents

    def test_raises_validation_error_for_invalid_agents(self, tmp_path: Path) -> None:
        """Should raise ConfigValidationError for invalid agent data."""
        invalid_file = tmp_path / "invalid_agents.toml"
        invalid_file.write_text(
            """
[agents.broken]
# Missing required 'binary' field
description = "Broken agent"
"""
        )

        with pytest.raises(ConfigValidationError):
            load_agents(invalid_file)


# =============================================================================
# save_config Tests
# =============================================================================


class TestSaveConfig:
    """Tests for save_config function."""

    def test_saves_config_to_file(self, tmp_path: Path, sample_code_dir: Path) -> None:
        """Should save AppConfig to TOML file."""
        config = AppConfig(code_dir=sample_code_dir, timezone="UTC")
        output_path = tmp_path / "output.toml"

        save_config(config, output_path)

        assert output_path.exists()
        content = output_path.read_text()
        assert "[general]" in content
        assert "UTC" in content

    def test_creates_parent_directories(self, tmp_path: Path, sample_code_dir: Path) -> None:
        """Should create parent directories if they don't exist."""
        config = AppConfig(code_dir=sample_code_dir)
        output_path = tmp_path / "nested" / "dir" / "config.toml"

        save_config(config, output_path)

        assert output_path.exists()


# =============================================================================
# Utility Function Tests
# =============================================================================
