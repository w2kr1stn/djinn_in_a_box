"""Tests for the session CLI command."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.core.config_workflow import (
    WorkflowDeliveryTarget,
    WorkflowPreparationProblem,
    WorkflowPreparationResult,
)
from djinn_in_a_box.core.docker import WorkflowImageCompatibility
from djinn_in_a_box.core.session import SessionResult, SessionTarget

runner = CliRunner()


@pytest.fixture(autouse=True)
def _mock_agents() -> Generator[None]:
    """Mock load_agents for all tests to avoid file system access."""
    from djinn_in_a_box.config.models import AgentConfig as _AgentConfig

    agents: dict[str, _AgentConfig] = {
        "claude": _AgentConfig(
            binary="claude",
            headless_flags=["-p"],
            write_flags=["--dangerously-skip-permissions"],
        ),
    }
    with (
        patch("djinn_in_a_box.core.session.load_agents", return_value=agents),
        patch("djinn_in_a_box.commands.session.load_config", return_value=object()),
        patch(
            "djinn_in_a_box.commands.session.prepare_config_workflow",
            return_value=WorkflowPreparationResult(True),
        ),
        patch(
            "djinn_in_a_box.commands.session.get_project_root",
            return_value=Path("/project"),
        ),
        patch(
            "djinn_in_a_box.commands.session.get_config_root",
            return_value=Path("/runtime"),
        ),
    ):
        yield


class TestSessionCommand:
    def test_help_output(self) -> None:
        result = runner.invoke(app, ["session", "--help"])
        assert result.exit_code == 0
        assert "session" in result.output.lower() or "interactive" in result.output.lower()

    def test_nonexistent_workspace_exits_1(self, tmp_path: Path) -> None:
        with patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path):
            result = runner.invoke(app, ["session", "--project", "nonexistent"])
            assert result.exit_code == 1

    def test_interactive_calls_run_interactive(self, tmp_path: Path) -> None:
        workspace = tmp_path / ".djinn" / "sessions" / "testproj"
        workspace.mkdir(parents=True)

        mock_session_result = SessionResult(returncode=0)
        target = SessionTarget(container_id="container-123")
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
            patch("djinn_in_a_box.commands.session.sys") as mock_sys,
        ):
            mock_sys.stdin.isatty.return_value = True
            mock_instance = mock_mgr.return_value
            mock_instance.resolve_target.return_value = target
            mock_instance.run_interactive.return_value = mock_session_result
            mock_instance.preflight_check.return_value = None

            runner.invoke(
                app,
                ["session", "--project", "testproj", "--agent", "opencode"],
            )
            mock_instance.resolve_target.assert_called_once_with()
            mock_instance.refresh_opencode_workflow.assert_called_once_with(target)
            mock_instance.preflight_check.assert_called_once_with(
                agent="opencode",
                target=target,
            )
            assert mock_instance.preflight_check.call_args.kwargs["target"] is target
            mock_instance.run_interactive.assert_called_once()
            interactive_kwargs = mock_instance.run_interactive.call_args.kwargs
            assert interactive_kwargs["agent"] == "opencode"
            assert interactive_kwargs["model"] is None
            assert interactive_kwargs["target"] is target

    def test_container_opencode_prepares_then_refreshes_before_workspace_preflight(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / ".djinn" / "sessions" / "testproj"
        workspace.mkdir(parents=True)
        target = SessionTarget(container_id="container-123")
        events: list[str] = []
        config = object()

        def prepare(*_args: object, **_kwargs: object) -> WorkflowPreparationResult:
            events.append("prepare")
            return WorkflowPreparationResult(True)

        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
            patch("djinn_in_a_box.commands.session.load_config", return_value=config),
            patch(
                "djinn_in_a_box.commands.session.prepare_config_workflow", side_effect=prepare
            ) as workflow,
        ):

            def _refresh(_target: object) -> SessionResult:
                events.append("refresh")
                return SessionResult(0)

            def _preflight(**_kwargs: object) -> None:
                events.append("preflight")

            def _run(**_kwargs: object) -> SessionResult:
                events.append("run")
                return SessionResult(0)

            instance = mock_mgr.return_value
            instance.resolve_target.return_value = target
            instance.refresh_opencode_workflow.side_effect = _refresh
            instance.preflight_check.side_effect = _preflight
            instance.run_headless.side_effect = _run

            result = runner.invoke(
                app,
                [
                    "session",
                    "--project",
                    "testproj",
                    "--agent",
                    "opencode",
                    "--prompt",
                    "hello",
                ],
            )

        assert result.exit_code == 0, result.output
        workflow.assert_called_once()
        assert workflow.call_args.args == (Path("/project"), ())
        assert workflow.call_args.kwargs["config_snapshot"] is config
        assert workflow.call_args.kwargs["require_compose_host_env"] is True
        assert events == ["prepare", "refresh", "preflight", "run"]
        instance.resolve_target.assert_called_once_with()

    def test_container_claude_uses_compose_delivery_mode(self, tmp_path: Path) -> None:
        workspace = tmp_path / ".djinn" / "sessions" / "testproj"
        workspace.mkdir(parents=True)
        target = SessionTarget(container_id="container-123")
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
            patch("djinn_in_a_box.commands.session.prepare_config_workflow") as workflow,
        ):
            workflow.return_value = WorkflowPreparationResult(True)
            instance = mock_mgr.return_value
            instance.resolve_target.return_value = target
            instance.run_headless.return_value = SessionResult(0)

            result = runner.invoke(
                app,
                [
                    "session",
                    "--project",
                    "testproj",
                    "--agent",
                    "claude",
                    "--prompt",
                    "hello",
                ],
            )

        assert result.exit_code == 0, result.output
        assert workflow.call_args.args == (
            Path("/project"),
            (WorkflowDeliveryTarget("claude", Path("/runtime/claude")),),
        )
        assert workflow.call_args.kwargs["require_compose_host_env"] is True
        instance.refresh_opencode_workflow.assert_not_called()

    def test_blocked_workflow_stops_before_workspace_creation_and_agent(
        self, tmp_path: Path
    ) -> None:
        target = SessionTarget(container_id="container-123")
        blocked = WorkflowPreparationResult(
            False,
            (WorkflowPreparationProblem("blocked", "Workflow blocked.", "Resolve drift."),),
        )
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
            patch(
                "djinn_in_a_box.commands.session.prepare_config_workflow",
                return_value=blocked,
            ),
        ):
            instance = mock_mgr.return_value
            instance.resolve_target.return_value = target
            result = runner.invoke(
                app,
                [
                    "session",
                    "--project",
                    "new-project",
                    "--agent",
                    "opencode",
                    "--create",
                ],
            )

        assert result.exit_code == 1
        assert not (tmp_path / ".djinn/sessions/new-project").exists()
        instance.resolve_target.assert_called_once_with()
        instance.refresh_opencode_workflow.assert_not_called()
        instance.preflight_check.assert_not_called()
        instance.run_interactive.assert_not_called()

    def test_blocked_gemini_workflow_stops_before_workspace_creation_and_agent(
        self, tmp_path: Path
    ) -> None:
        target = SessionTarget(container_id="container-123")
        blocked = WorkflowPreparationResult(
            False,
            (WorkflowPreparationProblem("blocked", "Workflow blocked.", "Resolve drift."),),
        )
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
            patch(
                "djinn_in_a_box.commands.session.prepare_config_workflow",
                return_value=blocked,
            ) as workflow,
        ):
            instance = mock_mgr.return_value
            instance.resolve_target.return_value = target
            instance.workflow_image_compatible.return_value = WorkflowImageCompatibility.COMPATIBLE

            result = runner.invoke(
                app,
                ["session", "--project", "new-project", "--agent", "gemini", "--create"],
            )

        assert result.exit_code == 1
        workflow.assert_called_once()
        assert workflow.call_args.args == (Path("/project"), ())
        instance.workflow_image_compatible.assert_called_once_with(target)
        assert not (tmp_path / ".djinn/sessions/new-project").exists()
        instance.preflight_check.assert_not_called()
        instance.run_interactive.assert_not_called()

    def test_failed_container_opencode_refresh_stops_before_workspace_creation(
        self, tmp_path: Path
    ) -> None:
        target = SessionTarget(container_id="container-123")
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
        ):
            instance = mock_mgr.return_value
            instance.resolve_target.return_value = target
            instance.refresh_opencode_workflow.return_value = SessionResult(
                1, stderr="OpenCode workflow refresh failed"
            )
            result = runner.invoke(
                app,
                [
                    "session",
                    "--project",
                    "new-project",
                    "--agent",
                    "opencode",
                    "--create",
                ],
            )

        assert result.exit_code == 1
        assert not (tmp_path / ".djinn/sessions/new-project").exists()
        instance.resolve_target.assert_called_once_with()
        instance.refresh_opencode_workflow.assert_called_once_with(target)
        instance.preflight_check.assert_not_called()
        instance.run_interactive.assert_not_called()

    @pytest.mark.parametrize("agent", ("claude", "codex", "opencode"))
    def test_old_running_image_stops_before_workflow_or_workspace(
        self, tmp_path: Path, agent: str
    ) -> None:
        target = SessionTarget(container_id="container-123")
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
            patch("djinn_in_a_box.commands.session.prepare_config_workflow") as workflow,
        ):
            instance = mock_mgr.return_value
            instance.resolve_target.return_value = target
            instance.workflow_image_compatible.return_value = (
                WorkflowImageCompatibility.INCOMPATIBLE
            )
            result = runner.invoke(
                app,
                ["session", "--project", "new-project", "--agent", agent, "--create"],
            )

        assert result.exit_code == 1
        assert "Rebuild/recreate required." in result.output
        assert not (tmp_path / ".djinn/sessions/new-project").exists()
        workflow.assert_not_called()
        instance.workflow_image_compatible.assert_called_once_with(target)
        instance.refresh_opencode_workflow.assert_not_called()
        instance.preflight_check.assert_not_called()

    def test_unreachable_running_image_stops_before_workflow_or_workspace(
        self, tmp_path: Path
    ) -> None:
        target = SessionTarget(container_id="container-123")
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
            patch("djinn_in_a_box.commands.session.prepare_config_workflow") as workflow,
        ):
            instance = mock_mgr.return_value
            instance.resolve_target.return_value = target
            instance.workflow_image_compatible.return_value = WorkflowImageCompatibility.UNKNOWN
            result = runner.invoke(
                app,
                ["session", "--project", "new-project", "--agent", "opencode", "--create"],
            )

        assert result.exit_code == 1
        assert "Docker daemon/container not reachable" in result.output
        assert "Rebuild/recreate required." not in result.output
        assert not (tmp_path / ".djinn/sessions/new-project").exists()
        workflow.assert_not_called()
        instance.refresh_opencode_workflow.assert_not_called()
        instance.preflight_check.assert_not_called()

    def test_host_opencode_delivers_only_selected_root(self, tmp_path: Path) -> None:
        workspace = tmp_path / ".djinn" / "sessions" / "testproj"
        workspace.mkdir(parents=True)
        target = SessionTarget()
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
            patch("djinn_in_a_box.commands.session.prepare_config_workflow") as workflow,
        ):
            workflow.return_value = WorkflowPreparationResult(True)
            instance = mock_mgr.return_value
            instance.resolve_target.return_value = target
            instance.run_headless.return_value = SessionResult(0)
            result = runner.invoke(
                app,
                [
                    "session",
                    "--project",
                    "testproj",
                    "--agent",
                    "opencode",
                    "--prompt",
                    "hello",
                ],
            )

        assert result.exit_code == 0, result.output
        targets = workflow.call_args.args[1]
        assert len(targets) == 1
        assert targets[0].tool == "opencode"
        assert targets[0].destination_root == tmp_path / ".config/opencode"
        assert targets[0].provision is True
        instance.refresh_opencode_workflow.assert_not_called()

    def test_headless_calls_run_headless(self, tmp_path: Path) -> None:
        workspace = tmp_path / ".djinn" / "sessions" / "testproj"
        workspace.mkdir(parents=True)

        mock_session_result = SessionResult(returncode=0, stdout="output")
        target = SessionTarget()
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
        ):
            mock_instance = mock_mgr.return_value
            mock_instance.resolve_target.return_value = target
            mock_instance.run_headless.return_value = mock_session_result
            mock_instance.preflight_check.return_value = None

            runner.invoke(
                app,
                [
                    "session",
                    "--project",
                    "testproj",
                    "--agent",
                    "codex",
                    "--prompt",
                    "hello",
                ],
            )
            mock_instance.resolve_target.assert_called_once_with()
            mock_instance.preflight_check.assert_called_once_with(
                agent="codex",
                target=target,
            )
            assert mock_instance.preflight_check.call_args.kwargs["target"] is target
            mock_instance.run_headless.assert_called_once()
            headless_kwargs = mock_instance.run_headless.call_args.kwargs
            assert headless_kwargs["agent"] == "codex"
            assert headless_kwargs["model"] is None
            assert headless_kwargs["target"] is target
