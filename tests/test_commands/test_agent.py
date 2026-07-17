"""Tests for agent execution commands."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer

from djinn_in_a_box.commands.agent import build_agent_command
from djinn_in_a_box.config.models import AgentConfig
from djinn_in_a_box.core.agent_runner import UnknownAgentError
from djinn_in_a_box.core.config_workflow import (
    WorkflowPreparationProblem,
    WorkflowPreparationResult,
)
from djinn_in_a_box.core.docker import DockerMode, RunResult, WorkflowImageCompatibility


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

    def test_uses_configured_default_model(self) -> None:
        config = AgentConfig(binary="codex", default_model="configured-model")

        cmd = build_agent_command(config)

        assert "--model configured-model" in cmd

    def test_explicit_model_overrides_configured_default(self) -> None:
        config = AgentConfig(binary="codex", default_model="configured-model")

        cmd = build_agent_command(config, model="gpt-5.6")

        assert "--model gpt-5.6" in cmd
        assert "configured-model" not in cmd

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
    def run_mocks(
        self,
    ) -> Generator[dict[str, Any]]:
        """Common mocks for run command tests."""
        with (
            patch("djinn_in_a_box.commands.agent.run_headless_agent") as mock_run,
            patch("djinn_in_a_box.commands.agent.load_config") as mock_config,
            patch(
                "djinn_in_a_box.commands.agent.prepare_config_workflow",
                return_value=WorkflowPreparationResult(True),
            ) as mock_workflow,
            patch(
                "djinn_in_a_box.commands.agent.get_project_root",
                return_value=Path("/project"),
            ),
            patch(
                "djinn_in_a_box.commands.agent.get_config_root",
                return_value=Path("/runtime"),
            ),
            patch(
                "djinn_in_a_box.commands.agent.workflow_image_compatible",
                return_value=WorkflowImageCompatibility.COMPATIBLE,
            ) as mock_image_compatibility,
        ):
            config = object()
            mock_config.return_value = config
            mock_run.return_value = RunResult(returncode=0, stdout="output", stderr="")
            yield {
                "run": mock_run,
                "workflow": mock_workflow,
                "config": config,
                "load_config": mock_config,
                "image_compatibility": mock_image_compatibility,
            }

    def test_run_validates_agent_name(
        self,
    ) -> None:
        """Test run validates the agent name."""
        from djinn_in_a_box.commands.agent import run

        unknown = UnknownAgentError("invalid", ("claude", "gemini"))
        with (
            patch("djinn_in_a_box.commands.agent.run_headless_agent", side_effect=unknown),
            patch("djinn_in_a_box.commands.agent.load_config", return_value=object()),
            patch(
                "djinn_in_a_box.commands.agent.workflow_image_compatible",
                return_value=WorkflowImageCompatibility.COMPATIBLE,
            ),
            patch(
                "djinn_in_a_box.commands.agent.prepare_config_workflow",
                return_value=WorkflowPreparationResult(True),
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            run(agent="invalid", prompt="test prompt")

        assert exc_info.value.exit_code == 1

    def test_run_delegates_to_internal_runner(self, run_mocks: dict[str, Any]) -> None:
        """Test run delegates execution and prompt handling to the internal runner."""
        from djinn_in_a_box.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test prompt")

        mock_run = run_mocks["run"]
        mock_run.assert_called_once()
        assert mock_run.call_args.args == ("claude", "test prompt")
        assert mock_run.call_args.kwargs["app_config"] is run_mocks["config"]
        assert callable(mock_run.call_args.kwargs["on_ready"])

    def test_run_keeps_checked_config_when_loader_changes_after_bootstrap(
        self, run_mocks: dict[str, Any]
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        checked = run_mocks["config"]
        changed = object()

        def flip_config_after_bootstrap(
            *_args: object, **_kwargs: object
        ) -> WorkflowPreparationResult:
            run_mocks["load_config"].return_value = changed
            return WorkflowPreparationResult(True)

        run_mocks["workflow"].side_effect = flip_config_after_bootstrap

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test prompt")

        assert run_mocks["run"].call_args.kwargs["app_config"] is checked
        run_mocks["load_config"].assert_called_once_with()

    def test_opencode_prepares_canonical_workflow_without_host_delivery(
        self, run_mocks: dict[str, Any]
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="opencode", prompt="test prompt")

        run_mocks["workflow"].assert_called_once_with(
            Path("/project"),
            (),
            config_snapshot=run_mocks["config"],
            require_compose_host_env=True,
            container_image_compatibility=WorkflowImageCompatibility.COMPATIBLE,
        )
        run_mocks["run"].assert_called_once()

    def test_blocked_opencode_workflow_never_starts_runner(self, run_mocks: dict[str, Any]) -> None:
        from djinn_in_a_box.commands.agent import run

        run_mocks["workflow"].return_value = WorkflowPreparationResult(
            False,
            (
                WorkflowPreparationProblem(
                    "semantic-agent-required",
                    "Workflow is blocked.",
                    "Resolve drift.",
                ),
            ),
        )

        with pytest.raises(typer.Exit) as exc_info:
            run(agent="opencode", prompt="test prompt")

        assert exc_info.value.exit_code == 1
        run_mocks["run"].assert_not_called()

    def test_blocked_gemini_workflow_never_starts_runner_and_checks_image(
        self, run_mocks: dict[str, Any]
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        run_mocks["workflow"].return_value = WorkflowPreparationResult(
            False,
            (WorkflowPreparationProblem("blocked", "Workflow is blocked.", "Resolve drift."),),
        )

        with pytest.raises(typer.Exit) as exc_info:
            run(agent="gemini", prompt="test prompt")

        assert exc_info.value.exit_code == 1
        run_mocks["workflow"].assert_called_once_with(
            Path("/project"),
            (),
            config_snapshot=run_mocks["config"],
            require_compose_host_env=True,
            container_image_compatibility=WorkflowImageCompatibility.COMPATIBLE,
        )
        run_mocks["image_compatibility"].assert_called_once_with()
        run_mocks["run"].assert_not_called()

    def test_run_with_write_flag(self, run_mocks: dict[str, Any]) -> None:
        """Test run --write uses write_flags."""
        from djinn_in_a_box.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test", write=True)

        assert run_mocks["run"].call_args.kwargs["write"] is True

    def test_run_with_timeout(self, run_mocks: dict[str, Any]) -> None:
        """Test run --timeout passes timeout value."""
        from djinn_in_a_box.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test", timeout=300)

        assert run_mocks["run"].call_args.kwargs["timeout"] == 300

    def test_run_with_docker_flag(self, run_mocks: dict[str, Any]) -> None:
        """Test run --docker enables docker option."""
        from djinn_in_a_box.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test", docker=True)

        assert run_mocks["run"].call_args.kwargs["docker_mode"] is DockerMode.PROXY

    def test_run_with_docker_direct_flag(self, run_mocks: dict[str, Any]) -> None:
        """Test run --docker-direct delegates the direct Docker mode."""
        from djinn_in_a_box.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test", docker_direct=True)

        assert run_mocks["run"].call_args.kwargs["docker_mode"] is DockerMode.DIRECT

    def test_run_docker_and_direct_mutually_exclusive(
        self,
    ) -> None:
        """Test run fails when both --docker and --docker-direct are used."""
        from djinn_in_a_box.commands.agent import run

        with (
            patch("djinn_in_a_box.commands.agent.run_headless_agent") as mock_run,
            pytest.raises(typer.Exit) as exc_info,
        ):
            run(agent="claude", prompt="test", docker=True, docker_direct=True)

        assert exc_info.value.exit_code == 1
        mock_run.assert_not_called()
