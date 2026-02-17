"""Tests for config/models.py Pydantic configuration models."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dev_base.config.models import (
    AgentConfig,
    AgentsConfig,
    AppConfig,
    ResourceLimits,
    ShellConfig,
    validate_memory_format,
)

# =============================================================================
# Memory Format Validation Tests
# =============================================================================


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


# =============================================================================
# AgentConfig Tests
# =============================================================================


class TestAgentConfig:
    """Tests for AgentConfig model."""

    def test_minimal_config(self) -> None:
        """Test AgentConfig with only required field (binary)."""
        agent = AgentConfig(binary="claude")
        assert agent.binary == "claude"
        assert agent.description == ""
        assert agent.headless_flags == []
        assert agent.read_only_flags == []
        assert agent.write_flags == []
        assert agent.json_flags == []
        assert agent.model_flag == "--model"
        assert agent.prompt_template == '"$AGENT_PROMPT"'

    def test_full_config(self) -> None:
        """Test AgentConfig with all fields populated."""
        agent = AgentConfig(
            binary="claude",
            description="Anthropic Claude Code CLI",
            headless_flags=["-p"],
            read_only_flags=["--permission-mode", "plan"],
            write_flags=["--dangerously-skip-permissions"],
            json_flags=["--output-format", "json"],
            model_flag="--model",
            prompt_template='"$AGENT_PROMPT"',
        )
        assert agent.binary == "claude"
        assert agent.description == "Anthropic Claude Code CLI"
        assert agent.headless_flags == ["-p"]
        assert agent.read_only_flags == ["--permission-mode", "plan"]
        assert agent.write_flags == ["--dangerously-skip-permissions"]
        assert agent.json_flags == ["--output-format", "json"]

    def test_gemini_config(self) -> None:
        """Test AgentConfig for Gemini agent with different model_flag."""
        agent = AgentConfig(
            binary="gemini",
            description="Google Gemini CLI",
            headless_flags=["-p"],
            model_flag="-m",
        )
        assert agent.model_flag == "-m"

    def test_extra_fields_forbidden(self) -> None:
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            AgentConfig(binary="claude", unknown_field="value")  # type: ignore[call-arg]


# =============================================================================
# ResourceLimits Tests
# =============================================================================


class TestResourceLimits:
    """Tests for ResourceLimits model."""

    def test_default_values(self) -> None:
        """Test ResourceLimits with all default values."""
        limits = ResourceLimits()
        assert limits.cpu_limit == 6
        assert limits.memory_limit == "12G"
        assert limits.cpu_reservation == 2
        assert limits.memory_reservation == "4G"

    def test_custom_values(self) -> None:
        """Test ResourceLimits with custom values."""
        limits = ResourceLimits(
            cpu_limit=8,
            memory_limit="16G",
            cpu_reservation=4,
            memory_reservation="8G",
        )
        assert limits.cpu_limit == 8
        assert limits.memory_limit == "16G"
        assert limits.cpu_reservation == 4
        assert limits.memory_reservation == "8G"

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


# =============================================================================
# ShellConfig Tests
# =============================================================================


class TestShellConfig:
    """Tests for ShellConfig model."""

    def test_default_values(self) -> None:
        """Test ShellConfig with default values."""
        shell = ShellConfig()
        assert shell.skip_mounts is False
        assert shell.omp_theme_path is None

    def test_skip_mounts_true(self) -> None:
        """Test ShellConfig with skip_mounts enabled."""
        shell = ShellConfig(skip_mounts=True)
        assert shell.skip_mounts is True

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

    def test_omp_theme_path_as_path_object(self, tmp_path: Path) -> None:
        """Test that Path objects are accepted for omp_theme_path."""
        theme_path = tmp_path / "theme.omp.json"
        theme_path.touch()

        shell = ShellConfig(omp_theme_path=theme_path)
        assert shell.omp_theme_path == theme_path


# =============================================================================
# AppConfig Tests
# =============================================================================


class TestAppConfig:
    """Tests for AppConfig model."""

    def test_minimal_config(self, tmp_path: Path) -> None:
        """Test AppConfig with only required field (code_dir)."""
        config = AppConfig(code_dir=tmp_path)
        assert config.code_dir == tmp_path
        assert config.timezone == "Europe/Berlin"
        assert config.resources.cpu_limit == 6
        assert config.shell.skip_mounts is False

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

    def test_custom_timezone(self, tmp_path: Path) -> None:
        """Test AppConfig with custom timezone."""
        config = AppConfig(code_dir=tmp_path, timezone="America/New_York")
        assert config.timezone == "America/New_York"

    def test_timezone_validation_empty(self, tmp_path: Path) -> None:
        """Test that empty timezone raises validation error."""
        with pytest.raises(ValidationError, match="Timezone cannot be empty"):
            AppConfig(code_dir=tmp_path, timezone="")

    def test_timezone_validation_invalid_format(self, tmp_path: Path) -> None:
        """Test that invalid timezone format raises validation error."""
        with pytest.raises(ValidationError, match="Invalid timezone format"):
            AppConfig(code_dir=tmp_path, timezone="InvalidTimezone")

    def test_timezone_utc_accepted(self, tmp_path: Path) -> None:
        """Test that UTC and other simple timezones are accepted."""
        config = AppConfig(code_dir=tmp_path, timezone="UTC")
        assert config.timezone == "UTC"

    def test_full_config(self, tmp_path: Path) -> None:
        """Test AppConfig with all options specified."""
        config = AppConfig(
            code_dir=tmp_path,
            timezone="Europe/London",
            resources=ResourceLimits(cpu_limit=4, memory_limit="8G"),
            shell=ShellConfig(skip_mounts=True),
        )
        assert config.code_dir == tmp_path
        assert config.timezone == "Europe/London"
        assert config.resources.cpu_limit == 4
        assert config.resources.memory_limit == "8G"
        assert config.shell.skip_mounts is True

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


# =============================================================================
# AgentsConfig Tests
# =============================================================================


class TestAgentsConfig:
    """Tests for AgentsConfig container model."""

    def test_empty_agents(self) -> None:
        """Test AgentsConfig with no agents."""
        config = AgentsConfig()
        assert config.agents == {}

    def test_single_agent(self) -> None:
        """Test AgentsConfig with one agent."""
        config = AgentsConfig(agents={"claude": AgentConfig(binary="claude")})
        assert "claude" in config.agents
        assert config.agents["claude"].binary == "claude"

    def test_multiple_agents(self) -> None:
        """Test AgentsConfig with multiple agents."""
        config = AgentsConfig(
            agents={
                "claude": AgentConfig(binary="claude"),
                "gemini": AgentConfig(binary="gemini"),
                "codex": AgentConfig(binary="codex"),
            }
        )
        assert len(config.agents) == 3

    def test_from_dict(self) -> None:
        """Test creating AgentsConfig from dictionary (TOML-like)."""
        data = {
            "agents": {
                "claude": {
                    "binary": "claude",
                    "description": "Anthropic Claude Code CLI",
                    "headless_flags": ["-p"],
                    "write_flags": ["--dangerously-skip-permissions"],
                },
            }
        }
        config = AgentsConfig.model_validate(data)
        assert config.agents["claude"].binary == "claude"
        assert config.agents["claude"].headless_flags == ["-p"]
