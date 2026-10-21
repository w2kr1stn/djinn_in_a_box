"""Tests for agent execution commands."""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands.agent import build_agent_command
from djinn_in_a_box.config.loader import load_agents
from djinn_in_a_box.config.models import AgentConfig, AppConfig
from djinn_in_a_box.core.agent_runner import UnknownAgentError
from djinn_in_a_box.core.config_workflow import (
    WorkflowDeliveryTarget,
    WorkflowPreparationProblem,
    WorkflowPreparationResult,
)
from djinn_in_a_box.core.docker import (
    ContainerMount,
    DockerMode,
    MountCollisionError,
    MountSpecificationError,
    RunResult,
    WorkflowImageCompatibility,
)
from djinn_in_a_box.core.exceptions import RuntimeMountSpecificationError


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
            patch("djinn_in_a_box.commands.agent.validate_managed_bind_sources"),
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

        unknown = UnknownAgentError("invalid", ("claude", "codex", "opencode"))
        with (
            patch("djinn_in_a_box.commands.agent.run_headless_agent", side_effect=unknown),
            patch("djinn_in_a_box.commands.agent.load_config", return_value=object()),
            patch("djinn_in_a_box.commands.agent.validate_managed_bind_sources"),
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
        assert "mounts" not in mock_run.call_args.kwargs
        assert "resolved_mounts" in mock_run.call_args.kwargs
        assert mock_run.call_args.kwargs["app_config"] is run_mocks["config"]
        assert callable(mock_run.call_args.kwargs["on_ready"])

    def test_run_without_mount_resolves_implicit_here_at_cli_boundary(
        self,
        run_mocks: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(app, ["run", "claude", "test prompt"])

        assert result.exit_code == 0, result.output
        assert run_mocks["run"].call_args.kwargs["resolved_mounts"] == (
            ContainerMount(tmp_path, Path("/home/dev/workspace")),
        )

    @pytest.mark.parametrize("mount", ["~nosuchuser/x", "\x00"])
    def test_run_cli_reports_unresolvable_mount_without_traceback(
        self, run_mocks: dict[str, Any], mount: str
    ) -> None:
        result = CliRunner().invoke(
            app,
            ["run", "claude", "test prompt", "--mount", mount],
        )

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Mount path cannot be resolved" in result.output
        run_mocks["run"].assert_not_called()

    def test_run_cli_reports_unresolvable_mount_target_without_traceback(
        self, run_mocks: dict[str, Any], tmp_path: Path
    ) -> None:
        result = CliRunner().invoke(
            app,
            ["run", "claude", "test prompt", "--mount", f"{tmp_path}:/work\x00bad"],
        )

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Mount target cannot contain a NUL byte" in result.output
        run_mocks["run"].assert_not_called()

    def test_run_cli_labels_runtime_mount_builder_failures(
        self, run_mocks: dict[str, Any]
    ) -> None:
        run_mocks["run"].side_effect = RuntimeMountSpecificationError("bad builder output")

        result = CliRunner().invoke(app, ["run", "claude", "test prompt"])

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Internal runtime mount construction failed" in result.output

    def test_run_help_preserves_example_line_continuation(self, run_mocks: dict[str, Any]) -> None:
        result = CliRunner().invoke(app, ["run", "--help"])
        plain_output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)

        assert result.exit_code == 0
        lines = plain_output.splitlines()
        example_index = next(
            index for index, line in enumerate(lines) if "Compare these projects" in line
        )
        assert lines[example_index].rstrip().endswith("--here \\")
        assert "--mount" in lines[example_index + 1]

    def test_run_here_alone_resolves_current_directory(
        self,
        run_mocks: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        monkeypatch.chdir(tmp_path)
        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test prompt", here=True)

        assert run_mocks["run"].call_args.kwargs["resolved_mounts"] == (
            ContainerMount(tmp_path, Path("/home/dev/workspace")),
        )

    def test_run_here_keeps_workspace_first_with_two_additional_mounts(
        self,
        run_mocks: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        monkeypatch.chdir(tmp_path)
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()

        with pytest.raises(typer.Exit):
            run(
                agent="claude",
                prompt="test prompt",
                here=True,
                mount=[str(first), str(second)],
            )

        assert run_mocks["run"].call_args.kwargs["resolved_mounts"] == (
            ContainerMount(tmp_path, Path("/home/dev/workspace")),
            ContainerMount(first, Path("/home/dev/mount/first")),
            ContainerMount(second, Path("/home/dev/mount/second")),
        )

    def test_run_passes_each_mount_specification_to_the_runner(
        self, run_mocks: dict[str, Any], tmp_path: Path
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()

        with pytest.raises(typer.Exit):
            run(
                agent="claude",
                prompt="test prompt",
                mount=[str(first), f"{second}:/container/two:ro"],
            )

        assert "mounts" not in run_mocks["run"].call_args.kwargs
        assert run_mocks["run"].call_args.kwargs["resolved_mounts"] == (
            ContainerMount(first, Path("/home/dev/mount/one")),
            ContainerMount(second, Path("/container/two"), read_only=True),
        )

    def test_run_cli_preserves_repeated_mount_options(
        self, run_mocks: dict[str, Any], tmp_path: Path
    ) -> None:
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()

        result = CliRunner().invoke(
            app,
            [
                "run",
                "claude",
                "test prompt",
                "--mount",
                str(first),
                "--mount",
                str(second),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "mounts" not in run_mocks["run"].call_args.kwargs
        assert run_mocks["run"].call_args.kwargs["resolved_mounts"] == (
            ContainerMount(first, Path("/home/dev/mount/first")),
            ContainerMount(second, Path("/home/dev/mount/second")),
        )

    def test_run_status_lists_mounts_and_labels_only_an_actual_workspace(
        self, run_mocks: dict[str, Any]
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        with pytest.raises(typer.Exit):
            run(agent="claude", prompt="test prompt")
        on_ready = run_mocks["run"].call_args.kwargs["on_ready"]
        assert callable(on_ready)

        extra_mounts = (
            ContainerMount(Path("/host/readonly"), Path("/home/dev/mount/readonly"), True),
            ContainerMount(Path("/host/reference"), Path("/home/dev/mount/reference")),
        )
        with patch("djinn_in_a_box.commands.agent.status_line") as status_line:
            on_ready(extra_mounts)

        mount_lines = [
            call.args for call in status_line.call_args_list if call.args[0] == "Mount"
        ]
        workspace_lines = [
            call.args for call in status_line.call_args_list if call.args[0] == "Workspace"
        ]
        assert mount_lines == [
            ("Mount", "/host/readonly -> /home/dev/mount/readonly (ro)"),
            ("Mount", "/host/reference -> /home/dev/mount/reference (rw)"),
        ]
        assert workspace_lines == []

        workspace_mount = ContainerMount(Path("/host/workspace"), Path("/home/dev/workspace"))
        with patch("djinn_in_a_box.commands.agent.status_line") as status_line:
            on_ready((workspace_mount, extra_mounts[1]))

        workspace_lines = [
            call.args for call in status_line.call_args_list if call.args[0] == "Workspace"
        ]
        assert workspace_lines == [("Workspace", "/host/workspace")]

    def test_run_reports_a_mount_validation_error(
        self, run_mocks: dict[str, Any]
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        with (
            patch(
                "djinn_in_a_box.commands.agent.resolve_container_mounts",
                side_effect=FileNotFoundError("Mount path does not exist: /missing"),
            ),
            patch("djinn_in_a_box.commands.agent.error") as error,
            pytest.raises(typer.Exit) as exc_info,
        ):
            run(agent="claude", prompt="test prompt", mount=["/missing"])

        assert exc_info.value.exit_code == 1
        error.assert_called_once_with("Mount path does not exist: /missing")

    def test_run_does_not_translate_runner_file_not_found(
        self, run_mocks: dict[str, Any]
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        run_mocks["run"].side_effect = FileNotFoundError("runner cwd disappeared")

        with pytest.raises(FileNotFoundError, match="runner cwd disappeared"):
            run(agent="claude", prompt="test prompt")

    def test_run_reports_a_mount_collision_error(
        self, run_mocks: dict[str, Any]
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        run_mocks["run"].side_effect = MountCollisionError("mount collision detail")
        with (
            patch("djinn_in_a_box.commands.agent.error") as error,
            pytest.raises(typer.Exit) as exc_info,
        ):
            run(agent="claude", prompt="test prompt")

        assert exc_info.value.exit_code == 1
        error.assert_called_once_with("mount collision detail")

    def test_run_cli_refuses_managed_mount_before_creation(
        self, mock_app_config: AppConfig, claude_config: AgentConfig, tmp_path: Path
    ) -> None:
        root = "/home/dev/workspaces"
        target = f"{root}/x"
        with (
            patch("djinn_in_a_box.commands.agent.load_config", return_value=mock_app_config),
            patch(
                "djinn_in_a_box.commands.agent.prepare_config_workflow",
                return_value=WorkflowPreparationResult(True),
            ),
            patch(
                "djinn_in_a_box.commands.agent.workflow_image_compatible",
                return_value=WorkflowImageCompatibility.COMPATIBLE,
            ),
            patch("djinn_in_a_box.core.agent_runner.ensure_network", return_value=True),
            patch(
                "djinn_in_a_box.core.agent_runner.load_agents",
                return_value={"claude": claude_config},
            ),
            patch("djinn_in_a_box.core.docker.get_shell_mount_args", return_value=[]),
            patch("djinn_in_a_box.core.docker.get_sops_age_key_mount_args", return_value=[]),
            patch(
                "djinn_in_a_box.core.docker._zone_overlay_mount_args_and_targets",
                return_value=([], ()),
            ),
            patch("djinn_in_a_box.core.docker.subprocess.run") as docker_run,
            patch("djinn_in_a_box.core.docker.subprocess.Popen") as docker_popen,
            patch("djinn_in_a_box.core.docker.tempfile.mkstemp") as override,
            patch("djinn_in_a_box.core.console.err_console", Console(width=1000, no_color=True)),
        ):
            result = CliRunner().invoke(
                app, ["run", "claude", "test prompt", "--mount", f"{tmp_path}:{target}"]
            )

        assert result.exit_code == 1, result.output
        assert (
            f"Mount {tmp_path} -> {target} conflicts with Djinn-managed path {root} "
            f"(conflict path: {root})"
        ) in result.output
        docker_run.assert_not_called()
        docker_popen.assert_not_called()
        override.assert_not_called()

    def test_run_reports_a_mount_specification_error(
        self, run_mocks: dict[str, Any]
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        run_mocks["run"].side_effect = MountSpecificationError("bad volume arguments")
        with (
            patch("djinn_in_a_box.commands.agent.error") as error,
            pytest.raises(typer.Exit) as exc_info,
        ):
            run(agent="claude", prompt="test prompt")

        assert exc_info.value.exit_code == 1
        error.assert_called_once_with("bad volume arguments")

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

    def test_blocked_opencode_workflow_never_starts_runner(
        self, run_mocks: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        run_mocks["workflow"].return_value = WorkflowPreparationResult(
            False,
            (
                WorkflowPreparationProblem(
                    "semantic-agent-required",
                    "Workflow blocked in a.toml[error].",
                    "Resolve drift in a.toml[error].",
                ),
            ),
            ("Synchronized source changes into the canonical workflow: 2 changed, 1 removed.",),
        )

        with pytest.raises(typer.Exit) as exc_info:
            run(agent="opencode", prompt="test prompt")

        assert exc_info.value.exit_code == 1
        captured = " ".join(capsys.readouterr().err.split())
        assert "Workflow blocked in a.toml[error]." in captured
        assert "Synchronized source changes into the canonical workflow: 2 changed, 1 removed." in (
            captured
        )
        assert "Resolve drift in a.toml[error]." in captured
        run_mocks["run"].assert_not_called()

    def test_blocked_codex_workflow_never_starts_runner_and_checks_image(
        self, run_mocks: dict[str, Any]
    ) -> None:
        from djinn_in_a_box.commands.agent import run

        run_mocks["workflow"].return_value = WorkflowPreparationResult(
            False,
            (WorkflowPreparationProblem("blocked", "Workflow is blocked.", "Resolve drift."),),
        )

        with pytest.raises(typer.Exit) as exc_info:
            run(agent="codex", prompt="test prompt")

        assert exc_info.value.exit_code == 1
        run_mocks["workflow"].assert_called_once_with(
            Path("/project"),
            (WorkflowDeliveryTarget("codex", Path("/runtime/codex")),),
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

        assert run_mocks["run"].call_args.kwargs["docker_mode"] is DockerMode.AGENT

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


class TestAgentsJson:
    @pytest.fixture
    def definitions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, AgentConfig]:
        monkeypatch.setattr("djinn_in_a_box.config.loader.AGENTS_FILE", tmp_path / "absent.toml")
        return load_agents()

    @pytest.mark.parametrize("force_color", [False, True])
    def test_complete_sorted_json_is_byte_stable(
        self, definitions: dict[str, AgentConfig], force_color: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected = json.dumps(
            {name: cfg.model_dump() for name, cfg in definitions.items()}, indent=2, sort_keys=True,
        ) + "\n"
        fields = {
            "binary", "description", "headless_flags", "read_only_flags", "write_flags",
            "json_flags", "model_flag", "default_model", "prompt_template",
        }
        if force_color:
            monkeypatch.setenv("FORCE_COLOR", "1")
        else:
            monkeypatch.delenv("FORCE_COLOR", raising=False)
        outputs = []
        queries = []
        for width in (20, 80, 400):
            monkeypatch.setenv("COLUMNS", str(width))
            # Override the autouse width fixture so a Rich regression really wraps/colors.
            monkeypatch.setattr(
                "djinn_in_a_box.commands.agent.console", Console(force_terminal=force_color),
            )
            for _ in range(2):
                result = CliRunner().invoke(app, ["agents", "--json"])
                assert result.exit_code == 0, result.output
                assert result.stderr == ""
                assert result.stdout_bytes == expected.encode("utf-8")
                obj = json.loads(result.stdout)
                assert list(obj) == sorted(obj)
                for cfg in obj.values():
                    assert set(cfg) == fields
                    assert list(cfg) == sorted(cfg)
                outputs.append(result.stdout_bytes)
                queries.append(
                    json.dumps(obj["claude-isolated"], sort_keys=True, separators=(",", ":")),
                )
        assert all(output == outputs[0] for output in outputs)
        assert all(query == queries[0] for query in queries)

    def test_one_changed_flag_changes_only_its_agent_object(
        self, definitions: dict[str, AgentConfig], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        before = CliRunner().invoke(app, ["agents", "--json"])
        assert before.exit_code == 0
        preset = definitions["claude-isolated"]
        changed = {
            **definitions,
            "claude-isolated": preset.model_copy(
                update={"read_only_flags": [*preset.read_only_flags[:-1], "--tools=Read"]},
            ),
        }
        monkeypatch.setattr("djinn_in_a_box.commands.agent.load_agents", lambda: changed)
        after = CliRunner().invoke(app, ["agents", "--json"])
        assert after.exit_code == 0
        assert after.stdout_bytes == (
            json.dumps(
                {name: cfg.model_dump() for name, cfg in changed.items()}, indent=2, sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        old, new = json.loads(before.stdout), json.loads(after.stdout)
        assert old.keys() == new.keys()
        assert {name for name in old if old[name] != new[name]} == {"claude-isolated"}
        old_flags = old["claude-isolated"].pop("read_only_flags")
        new_flags = new["claude-isolated"].pop("read_only_flags")
        assert old_flags[:-1] == new_flags[:-1]
        assert old_flags[-1] == "--tools="
        assert new_flags[-1] == "--tools=Read"
        assert old == new
