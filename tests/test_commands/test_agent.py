"""Tests for agent execution commands.

Tests for:
- build_agent_command() - Shell command construction
- run() - Headless agent execution
- agents() - List available agents
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
import typer

from ai_dev_base.commands.agent import build_agent_command
from ai_dev_base.config.models import AgentConfig

if TYPE_CHECKING:
    from ai_dev_base.config.models import AppConfig

# =============================================================================
# Fixtures
# =============================================================================


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


@pytest.fixture
def codex_config() -> AgentConfig:
    """Codex agent configuration for testing."""
    return AgentConfig(
        binary="codex",
        description="OpenAI Codex CLI",
        headless_flags=["exec"],
        read_only_flags=[],
        write_flags=["--full-auto"],
        json_flags=["--json"],
        model_flag="--model",
        prompt_template='"$AGENT_PROMPT"',
    )


@pytest.fixture
def opencode_config() -> AgentConfig:
    """OpenCode agent configuration for testing."""
    return AgentConfig(
        binary="opencode",
        description="Anomaly OpenCode CLI",
        headless_flags=["run"],
        read_only_flags=["--agent", "plan"],
        write_flags=[],
        json_flags=["--format", "json"],
        model_flag="-m",
        prompt_template='"$AGENT_PROMPT"',
    )


# =============================================================================
# build_agent_command Tests
# =============================================================================


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

    def test_gemini_command_structure(self, gemini_config: AgentConfig) -> None:
        """Test Gemini agent command structure."""
        cmd = build_agent_command(gemini_config, model="gemini-2.5-flash")

        assert cmd.startswith("gemini")
        assert "-p" in cmd
        assert "-m gemini-2.5-flash" in cmd
        assert '"$AGENT_PROMPT"' in cmd

    def test_codex_command_structure(self, codex_config: AgentConfig) -> None:
        """Test Codex agent command structure with write mode."""
        cmd = build_agent_command(codex_config, write=True)

        assert cmd.startswith("codex exec")
        assert "--full-auto" in cmd
        assert '"$AGENT_PROMPT"' in cmd

    def test_opencode_command_structure(self, opencode_config: AgentConfig) -> None:
        """Test OpenCode agent command structure."""
        cmd = build_agent_command(opencode_config)

        assert cmd.startswith("opencode run")
        # Read-only mode
        assert "--agent plan" in cmd
        assert '"$AGENT_PROMPT"' in cmd

    def test_opencode_write_mode_no_extra_flags(self, opencode_config: AgentConfig) -> None:
        """Test OpenCode with write mode (has empty write_flags)."""
        cmd = build_agent_command(opencode_config, write=True)

        # Write mode, but no write_flags for opencode
        # So read_only_flags should NOT be present
        assert "--agent" not in cmd

    def test_command_order(self, claude_config: AgentConfig) -> None:
        """Test that command parts are in the correct order."""
        cmd = build_agent_command(
            claude_config,
            write=True,
            json_output=True,
            model="haiku",
        )
        parts = cmd.split()

        # Order: binary, headless_flags, model_flag, model, write_flags, json_flags, prompt
        binary_idx = parts.index("claude")
        headless_idx = parts.index("-p")
        model_flag_idx = parts.index("--model")
        model_idx = parts.index("haiku")
        write_idx = parts.index("--dangerously-skip-permissions")
        json_flag_idx = parts.index("--output-format")

        assert binary_idx < headless_idx < model_flag_idx < model_idx < write_idx < json_flag_idx


class TestBuildAgentCommandEdgeCases:
    """Edge case tests for build_agent_command."""

    def test_empty_headless_flags(self) -> None:
        """Test with empty headless flags."""
        config = AgentConfig(
            binary="test-agent",
            headless_flags=[],
            prompt_template='"$AGENT_PROMPT"',
        )
        cmd = build_agent_command(config)

        assert cmd == 'test-agent "$AGENT_PROMPT"'

    def test_empty_read_only_flags(self) -> None:
        """Test read-only mode with empty flags."""
        config = AgentConfig(
            binary="test-agent",
            headless_flags=["-p"],
            read_only_flags=[],
            prompt_template='"$AGENT_PROMPT"',
        )
        cmd = build_agent_command(config, write=False)

        assert cmd == 'test-agent -p "$AGENT_PROMPT"'

    def test_empty_write_flags(self) -> None:
        """Test write mode with empty flags."""
        config = AgentConfig(
            binary="test-agent",
            headless_flags=["-p"],
            write_flags=[],
            prompt_template='"$AGENT_PROMPT"',
        )
        cmd = build_agent_command(config, write=True)

        assert cmd == 'test-agent -p "$AGENT_PROMPT"'

    def test_empty_json_flags(self) -> None:
        """Test JSON output with empty flags."""
        config = AgentConfig(
            binary="test-agent",
            headless_flags=["-p"],
            json_flags=[],
            prompt_template='"$AGENT_PROMPT"',
        )
        cmd = build_agent_command(config, json_output=True)

        assert cmd == 'test-agent -p "$AGENT_PROMPT"'

    def test_model_none_not_added(self) -> None:
        """Test that None model is not added to command."""
        config = AgentConfig(
            binary="test-agent",
            model_flag="--model",
            prompt_template='"$AGENT_PROMPT"',
        )
        cmd = build_agent_command(config, model=None)

        assert "--model" not in cmd


# =============================================================================
# Integration Tests (Mocked)
# =============================================================================


class TestAgentCommandIntegration:
    """Integration tests with mocked subprocess calls."""

    @pytest.fixture
    def mock_agents(self) -> dict[str, AgentConfig]:
        """Provide mock agent configurations."""
        return {
            "claude": AgentConfig(
                binary="claude",
                description="Anthropic Claude Code CLI",
                headless_flags=["-p"],
                read_only_flags=["--permission-mode", "plan"],
                write_flags=["--dangerously-skip-permissions"],
                json_flags=["--output-format", "json"],
                model_flag="--model",
                prompt_template='"$AGENT_PROMPT"',
            ),
            "gemini": AgentConfig(
                binary="gemini",
                description="Google Gemini CLI",
                headless_flags=["-p"],
                model_flag="-m",
                prompt_template='"$AGENT_PROMPT"',
            ),
        }

    def test_agent_command_matches_bash_behavior(self, mock_agents: dict[str, AgentConfig]) -> None:
        """Verify command matches expected Bash script behavior."""
        # Test: codeagent run claude "test" should produce:
        # claude -p --permission-mode plan "$AGENT_PROMPT"
        cmd = build_agent_command(mock_agents["claude"])
        expected = 'claude -p --permission-mode plan "$AGENT_PROMPT"'
        assert cmd == expected

    def test_agent_command_with_write_matches_bash(
        self, mock_agents: dict[str, AgentConfig]
    ) -> None:
        """Verify write mode command matches expected Bash behavior."""
        # Test: codeagent run claude "test" --write should produce:
        # claude -p --dangerously-skip-permissions "$AGENT_PROMPT"
        cmd = build_agent_command(mock_agents["claude"], write=True)
        expected = 'claude -p --dangerously-skip-permissions "$AGENT_PROMPT"'
        assert cmd == expected

    def test_agent_command_with_model_matches_bash(
        self, mock_agents: dict[str, AgentConfig]
    ) -> None:
        """Verify model override command matches expected Bash behavior."""
        # Test: codeagent run claude "test" --write --model sonnet should produce:
        # claude -p --model sonnet --dangerously-skip-permissions "$AGENT_PROMPT"
        cmd = build_agent_command(mock_agents["claude"], write=True, model="sonnet")
        expected = 'claude -p --model sonnet --dangerously-skip-permissions "$AGENT_PROMPT"'
        assert cmd == expected

    def test_agent_command_with_json_matches_bash(
        self, mock_agents: dict[str, AgentConfig]
    ) -> None:
        """Verify JSON output command matches expected Bash behavior."""
        # Test: codeagent run claude "test" --json should produce:
        # claude -p --permission-mode plan --output-format json "$AGENT_PROMPT"
        cmd = build_agent_command(mock_agents["claude"], json_output=True)
        expected = 'claude -p --permission-mode plan --output-format json "$AGENT_PROMPT"'
        assert cmd == expected


# =============================================================================
# agents() Command Tests
# =============================================================================


class TestAgentsListCommand:
    """Tests for the agents list command."""

    def test_agents_function_exists(self) -> None:
        """Verify agents function can be imported."""
        from ai_dev_base.commands.agent import agents

        assert callable(agents)

    def test_build_agent_command_exists(self) -> None:
        """Verify build_agent_command function can be imported."""
        from ai_dev_base.commands.agent import build_agent_command

        assert callable(build_agent_command)

    def test_run_function_exists(self) -> None:
        """Verify run function can be imported."""
        from ai_dev_base.commands.agent import run

        assert callable(run)

    def test_agents_lists_available(self) -> None:
        """Test agents command lists available agents."""
        from ai_dev_base.commands.agent import agents

        with patch("ai_dev_base.commands.agent.load_agents") as mock_load:
            mock_load.return_value = {
                "claude": AgentConfig(
                    binary="claude",
                    description="Anthropic Claude",
                    prompt_template='"$AGENT_PROMPT"',
                ),
                "gemini": AgentConfig(
                    binary="gemini",
                    description="Google Gemini",
                    prompt_template='"$AGENT_PROMPT"',
                ),
            }

            # Should not raise
            agents()

            mock_load.assert_called_once()

    def test_agents_json_output(self) -> None:
        """Test agents --json outputs JSON format."""

        from ai_dev_base.commands.agent import agents

        with (
            patch("ai_dev_base.commands.agent.load_agents") as mock_load,
            patch("ai_dev_base.commands.agent.console") as mock_console,
        ):
            mock_load.return_value = {
                "claude": AgentConfig(
                    binary="claude",
                    description="Anthropic Claude",
                    prompt_template='"$AGENT_PROMPT"',
                ),
            }

            agents(json_output=True)

            # Should have printed JSON
            mock_console.print.assert_called_once()
            output = mock_console.print.call_args[0][0]
            import json

            data = json.loads(output)
            assert "claude" in data

    def test_agents_verbose_output(self) -> None:
        """Test agents --verbose shows detailed info."""
        from ai_dev_base.commands.agent import agents

        with (
            patch("ai_dev_base.commands.agent.load_agents") as mock_load,
            patch("ai_dev_base.commands.agent.console") as mock_console,
        ):
            mock_load.return_value = {
                "claude": AgentConfig(
                    binary="claude",
                    description="Anthropic Claude",
                    headless_flags=["-p"],
                    model_flag="--model",
                    prompt_template='"$AGENT_PROMPT"',
                ),
            }

            agents(verbose=True)

            # Should have printed multiple lines with details
            assert mock_console.print.call_count > 1


# =============================================================================
# run() Command Tests
# =============================================================================


class TestRunCommand:
    """Tests for the run command."""

    @pytest.fixture
    def mock_agent_configs(self) -> dict[str, AgentConfig]:
        """Provide mock agent configurations."""
        return {
            "claude": AgentConfig(
                binary="claude",
                description="Anthropic Claude Code CLI",
                headless_flags=["-p"],
                read_only_flags=["--permission-mode", "plan"],
                write_flags=["--dangerously-skip-permissions"],
                json_flags=["--output-format", "json"],
                model_flag="--model",
                prompt_template='"$AGENT_PROMPT"',
            ),
            "gemini": AgentConfig(
                binary="gemini",
                description="Google Gemini CLI",
                headless_flags=["-p"],
                model_flag="-m",
                prompt_template='"$AGENT_PROMPT"',
            ),
        }

    @pytest.fixture
    def mock_app_config(self, tmp_path: Path) -> AppConfig:
        """Provide mock app configuration."""
        from ai_dev_base.config.models import AppConfig, ResourceLimits, ShellConfig

        # Create a valid projects dir
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        return AppConfig(
            code_dir=projects_dir,
            resources=ResourceLimits(),
            shell=ShellConfig(),
        )

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

    def test_run_calls_compose_run(
        self,
        mock_agent_configs: dict[str, AgentConfig],
        mock_app_config: AppConfig,
    ) -> None:
        """Test run calls compose_run with correct parameters."""
        from ai_dev_base.commands.agent import run
        from ai_dev_base.core.docker import RunResult

        with (
            patch("ai_dev_base.commands.agent.load_config", return_value=mock_app_config),
            patch("ai_dev_base.commands.agent.load_agents", return_value=mock_agent_configs),
            patch("ai_dev_base.commands.agent.ensure_network"),
            patch("ai_dev_base.core.docker.compose_run") as mock_run,
            patch("ai_dev_base.commands.agent.cleanup_docker_proxy"),
            pytest.raises(typer.Exit),
        ):
            mock_run.return_value = RunResult(returncode=0, stdout="output", stderr="")

            run(agent="claude", prompt="test prompt")

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args[1]
            assert "AGENT_PROMPT" in call_kwargs["env"]
            assert call_kwargs["env"]["AGENT_PROMPT"] == "test prompt"
            assert call_kwargs["interactive"] is False

    def test_run_with_write_flag(
        self,
        mock_agent_configs: dict[str, AgentConfig],
        mock_app_config: AppConfig,
    ) -> None:
        """Test run --write uses write_flags."""
        from ai_dev_base.commands.agent import run
        from ai_dev_base.core.docker import RunResult

        with (
            patch("ai_dev_base.commands.agent.load_config", return_value=mock_app_config),
            patch("ai_dev_base.commands.agent.load_agents", return_value=mock_agent_configs),
            patch("ai_dev_base.commands.agent.ensure_network"),
            patch("ai_dev_base.core.docker.compose_run") as mock_run,
            patch("ai_dev_base.commands.agent.cleanup_docker_proxy"),
            pytest.raises(typer.Exit),
        ):
            mock_run.return_value = RunResult(returncode=0, stdout="output", stderr="")

            run(agent="claude", prompt="test", write=True)

            call_kwargs = mock_run.call_args[1]
            assert "--dangerously-skip-permissions" in call_kwargs["command"]

    def test_run_with_timeout(
        self,
        mock_agent_configs: dict[str, AgentConfig],
        mock_app_config: AppConfig,
    ) -> None:
        """Test run --timeout passes timeout value."""
        from ai_dev_base.commands.agent import run
        from ai_dev_base.core.docker import RunResult

        with (
            patch("ai_dev_base.commands.agent.load_config", return_value=mock_app_config),
            patch("ai_dev_base.commands.agent.load_agents", return_value=mock_agent_configs),
            patch("ai_dev_base.commands.agent.ensure_network"),
            patch("ai_dev_base.core.docker.compose_run") as mock_run,
            patch("ai_dev_base.commands.agent.cleanup_docker_proxy"),
            pytest.raises(typer.Exit),
        ):
            mock_run.return_value = RunResult(returncode=0, stdout="output", stderr="")

            run(agent="claude", prompt="test", timeout=300)

            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["timeout"] == 300

    def test_run_with_docker_flag(
        self,
        mock_agent_configs: dict[str, AgentConfig],
        mock_app_config: AppConfig,
    ) -> None:
        """Test run --docker enables docker option."""
        from ai_dev_base.commands.agent import run
        from ai_dev_base.core.docker import RunResult

        with (
            patch("ai_dev_base.commands.agent.load_config", return_value=mock_app_config),
            patch("ai_dev_base.commands.agent.load_agents", return_value=mock_agent_configs),
            patch("ai_dev_base.commands.agent.ensure_network"),
            patch("ai_dev_base.core.docker.compose_run") as mock_run,
            patch("ai_dev_base.commands.agent.cleanup_docker_proxy") as mock_cleanup,
            pytest.raises(typer.Exit),
        ):
            mock_run.return_value = RunResult(returncode=0, stdout="output", stderr="")

            run(agent="claude", prompt="test", docker=True)

            # Check options
            call_args = mock_run.call_args[0]
            options = call_args[1]  # Second positional arg
            assert options.docker_enabled is True

            # Cleanup should be called with True
            mock_cleanup.assert_called_once_with(True)


