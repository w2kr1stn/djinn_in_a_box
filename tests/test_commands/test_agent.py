"""Tests for agent execution commands."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import typer

from ai_dev_base.commands.agent import build_agent_command
from ai_dev_base.config.models import AgentConfig
from ai_dev_base.core.docker import RunResult

if TYPE_CHECKING:
    from ai_dev_base.config.models import AppConfig


@pytest.fixture
def claude_config() -> AgentConfig:
    """Claude agent configuration for testing."""
    return AgentConfig(
        binary="claude",
        description="Anthropic Claude Code CLI",
        headless_flags=["-p"],
        read_only_flags=["--permission-mode", "plan"],
        write_flags=["--dangerously-skip-permissions"],
        json_flags=["--output-format", "json"],
        model_flag="--model",
        prompt_template='"$AGENT_PROMPT"',
    )


@pytest.fixture
def gemini_config() -> AgentConfig:
    """Gemini agent configuration for testing."""
    return AgentConfig(
        binary="gemini",
        description="Google Gemini CLI",
        headless_flags=["-p"],
        read_only_flags=[],
        write_flags=[],
        json_flags=["--output-format", "json"],
        model_flag="-m",
        prompt_template='"$AGENT_PROMPT"',
    )


class TestBuildAgentCommand:
    """Tests for the build_agent_command function."""

    def test_basic_command_read_only(self, claude_config: AgentConfig) -> None:
        """Test basic command generation in read-only mode."""
        cmd = build_agent_command(claude_config)

        assert cmd.startswith("claude")
        assert "-p" in cmd
        assert "--permission-mode" in cmd
        assert "plan" in cmd
        assert '"$AGENT_PROMPT"' in cmd

    def test_basic_command_write_mode(self, claude_config: AgentConfig) -> None:
        """Test command generation with write mode enabled."""
        cmd = build_agent_command(claude_config, write=True)

        assert "--dangerously-skip-permissions" in cmd
        # Read-only flags should NOT be present
        assert "--permission-mode" not in cmd
        assert "plan" not in cmd

    def test_with_model_override(self, claude_config: AgentConfig) -> None:
        """Test command generation with model override."""
        cmd = build_agent_command(claude_config, model="sonnet")

        assert "--model" in cmd
        assert "sonnet" in cmd

    def test_with_json_output(self, claude_config: AgentConfig) -> None:
        """Test command generation with JSON output enabled."""
        cmd = build_agent_command(claude_config, json_output=True)

        assert "--output-format" in cmd
        assert "json" in cmd

    def test_all_options_combined(self, claude_config: AgentConfig) -> None:
        """Test command with all options enabled."""
        cmd = build_agent_command(
            claude_config,
            write=True,
            json_output=True,
            model="opus",
        )

        # Should have binary and headless flags
        assert cmd.startswith("claude -p")
        # Should have model
        assert "--model opus" in cmd
        # Should have write flags (not read-only)
        assert "--dangerously-skip-permissions" in cmd
        assert "--permission-mode" not in cmd
        # Should have json flags
        assert "--output-format json" in cmd
        # Should have prompt template
        assert '"$AGENT_PROMPT"' in cmd

    def test_model_none_not_added(self) -> None:
        """Test that None model is not added to command."""
        config = AgentConfig(
            binary="test-agent",
            model_flag="--model",
            prompt_template='"$AGENT_PROMPT"',
        )
        cmd = build_agent_command(config, model=None)

        assert "--model" not in cmd


class TestRunCommand:
    """Tests for the run command."""

    @pytest.fixture
    def mock_agent_configs(
        self, claude_config: AgentConfig, gemini_config: AgentConfig
    ) -> dict[str, AgentConfig]:
        """Provide mock agent configurations from top-level fixtures."""
        return {"claude": claude_config, "gemini": gemini_config}

    @pytest.fixture
    def run_mocks(
        self, mock_agent_configs: dict[str, AgentConfig], mock_app_config: AppConfig
    ) -> Generator[dict[str, Any]]:
        """Common mocks for run command tests."""
        with (
            patch("ai_dev_base.commands.agent.load_config", return_value=mock_app_config),
            patch("ai_dev_base.commands.agent.load_agents", return_value=mock_agent_configs),
            patch("ai_dev_base.commands.agent.ensure_network", return_value=True),
            patch("ai_dev_base.commands.agent.compose_run") as mock_run,
            patch("ai_dev_base.commands.agent.cleanup_docker_proxy") as mock_cleanup,
        ):
            mock_run.return_value = RunResult(returncode=0, stdout="output", stderr="")
            yield {"run": mock_run, "cleanup": mock_cleanup}

    def test_run_validates_agent_name(
        self,
        mock_agent_configs: dict[str, AgentConfig],
        mock_app_config: AppConfig,
    ) -> None:
        """Test run validates the agent name."""
        from ai_dev_base.commands.agent import run

        with (
            patch("ai_dev_base.commands.agent.load_config", return_value=mock_app_config),
            patch("ai_dev_base.commands.agent.load_agents", return_value=mock_agent_configs),
            pytest.raises(typer.Exit) as exc_info,
        ):
            run(agent="invalid", prompt="test prompt")

        assert exc_info.value.exit_code == 1

    def test_run_calls_compose_run(self, run_mocks: dict[str, Any]) -> None:
        """Test run calls compose_run with correct parameters."""
        from ai_dev_base.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test prompt")

        mock_run = run_mocks["run"]
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert "AGENT_PROMPT" in call_kwargs["env"]
        assert call_kwargs["env"]["AGENT_PROMPT"] == "test prompt"
        assert call_kwargs["interactive"] is False

    def test_run_with_write_flag(self, run_mocks: dict[str, Any]) -> None:
        """Test run --write uses write_flags."""
        from ai_dev_base.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test", write=True)

        call_kwargs = run_mocks["run"].call_args[1]
        assert "--dangerously-skip-permissions" in call_kwargs["command"]

    def test_run_with_timeout(self, run_mocks: dict[str, Any]) -> None:
        """Test run --timeout passes timeout value."""
        from ai_dev_base.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test", timeout=300)

        call_kwargs = run_mocks["run"].call_args[1]
        assert call_kwargs["timeout"] == 300

    def test_run_with_docker_flag(self, run_mocks: dict[str, Any]) -> None:
        """Test run --docker enables docker option."""
        from ai_dev_base.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test", docker=True)

        options = run_mocks["run"].call_args[0][1]
        assert options.docker_enabled is True
        run_mocks["cleanup"].assert_called_once_with(True)

    def test_run_with_docker_direct_flag(self, run_mocks: dict[str, Any]) -> None:
        """Test run --docker-direct sets docker_direct option and skips proxy cleanup."""
        from ai_dev_base.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test", docker_direct=True)

        options = run_mocks["run"].call_args[0][1]
        assert options.docker_direct is True
        assert options.docker_enabled is False
        run_mocks["cleanup"].assert_called_once_with(False)

    def test_run_docker_and_direct_mutually_exclusive(
        self,
        mock_agent_configs: dict[str, AgentConfig],
        mock_app_config: AppConfig,
    ) -> None:
        """Test run fails when both --docker and --docker-direct are used."""
        from ai_dev_base.commands.agent import run

        with (
            patch("ai_dev_base.commands.agent.load_config", return_value=mock_app_config),
            patch("ai_dev_base.commands.agent.load_agents", return_value=mock_agent_configs),
            pytest.raises(typer.Exit) as exc_info,
        ):
            run(agent="claude", prompt="test", docker=True, docker_direct=True)

        assert exc_info.value.exit_code == 1
