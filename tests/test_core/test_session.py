"""Tests for AI agent session management."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from djinn_in_a_box.config.models import AgentConfig
from djinn_in_a_box.core.session import SessionManager, SessionResult, SessionTarget

_SESSION_MODULE = "djinn_in_a_box.core.session"
_SUBPROCESS_RUN = f"{_SESSION_MODULE}.subprocess.run"
_AGENT_LOAD = "djinn_in_a_box.commands.agent.load_agents"


# ── Fixtures ──


@pytest.fixture
def mock_agents() -> dict[str, AgentConfig]:
    """Mock load_agents to return test agent configs."""
    return {
        "claude": AgentConfig(
            binary="claude",
            headless_flags=["-p"],
            write_flags=["--dangerously-skip-permissions"],
            model_flag="--model",
            prompt_template='"$AGENT_PROMPT"',
        ),
    }


@pytest.fixture
def session_mgr(mock_agents: dict[str, AgentConfig]) -> SessionManager:
    """Create a SessionManager with mocked agents."""
    with patch(f"{_SESSION_MODULE}.load_agents", return_value=mock_agents):
        return SessionManager("testproject")


# ── SessionResult Tests ──


class TestSessionTarget:
    def test_host_mode_default(self) -> None:
        target = SessionTarget()
        assert target.container_id is None
        assert target.container_mode is False

    def test_container_mode(self) -> None:
        target = SessionTarget(container_id="abc123")
        assert target.container_mode is True

    def test_frozen_dataclass(self) -> None:
        target = SessionTarget()
        with pytest.raises(AttributeError):
            target.container_id = "abc123"  # type: ignore[misc]


class TestSessionResult:
    def test_success_on_zero_returncode(self) -> None:
        result = SessionResult(returncode=0)
        assert result.success is True

    def test_failure_on_nonzero_returncode(self) -> None:
        result = SessionResult(returncode=1)
        assert result.success is False

    def test_frozen_dataclass(self) -> None:
        result = SessionResult(returncode=0)
        with pytest.raises(AttributeError):
            result.returncode = 1  # type: ignore[misc]

    def test_defaults(self) -> None:
        result = SessionResult(returncode=0)
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.workspace_dir is None


# ── Container Discovery Tests ──


class TestFindContainer:
    def test_returns_id_when_running(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc123def456\n"
        with patch(_SUBPROCESS_RUN, return_value=mock_result):
            assert session_mgr._find_container() == "abc123def456"

    def test_returns_none_when_not_running(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "\n"
        with patch(_SUBPROCESS_RUN, return_value=mock_result):
            assert session_mgr._find_container() is None

    def test_returns_none_on_error(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch(_SUBPROCESS_RUN, return_value=mock_result):
            assert session_mgr._find_container() is None

    def test_returns_none_on_file_not_found(self, session_mgr: SessionManager) -> None:
        with patch(_SUBPROCESS_RUN, side_effect=FileNotFoundError):
            assert session_mgr._find_container() is None

    def test_returns_none_on_timeout(self, session_mgr: SessionManager) -> None:
        with patch(
            _SUBPROCESS_RUN,
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10),
        ):
            assert session_mgr._find_container() is None


class TestResolveTarget:
    def test_wraps_discovered_container(self, session_mgr: SessionManager) -> None:
        with patch.object(session_mgr, "_find_container", return_value="abc123"):
            target = session_mgr.resolve_target()

        assert target == SessionTarget(container_id="abc123")

    def test_wraps_host_fallback(self, session_mgr: SessionManager) -> None:
        with patch.object(session_mgr, "_find_container", return_value=None):
            target = session_mgr.resolve_target()

        assert target == SessionTarget()


class TestRefreshOpenCodeWorkflow:
    def test_uses_publisher_with_exact_container_arguments(
        self, session_mgr: SessionManager
    ) -> None:
        completed = MagicMock(returncode=0, stdout="ignored", stderr="ignored")
        target = SessionTarget(container_id="stable-container-id")

        with (
            patch.object(session_mgr, "workflow_image_compatible", return_value=True),
            patch(_SUBPROCESS_RUN, return_value=completed) as run,
        ):
            result = session_mgr.refresh_opencode_workflow(target)

        assert result.success is True
        run.assert_called_once_with(
            [
                "docker",
                "exec",
                "stable-container-id",
                "python3",
                "/home/dev/workflow-publisher.py",
                "--view",
                "/home/dev/.opencode/seed",
                "--canonical-root",
                "/home/dev/.djinn-canonical",
                "--target",
                "/home/dev/.config/opencode",
                "--manifest",
                "/home/dev/.config/opencode/.djinn-workflow-state.json",
                "--ignore",
                ".opencode.json",
                "--profile",
                "opencode",
            ],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )

    def test_failure_output_is_sanitized(self, session_mgr: SessionManager) -> None:
        sentinel = "PRIVATE-CONTAINER-OUTPUT"
        failed = MagicMock(returncode=9, stdout=sentinel, stderr=sentinel)

        with (
            patch.object(session_mgr, "workflow_image_compatible", return_value=True),
            patch(_SUBPROCESS_RUN, return_value=failed),
        ):
            result = session_mgr.refresh_opencode_workflow(
                SessionTarget(container_id="container-id")
            )

        assert result.returncode == 9
        assert result.stderr == "OpenCode workflow refresh failed"
        assert sentinel not in repr(result)

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("source-changed", "Run `djinn config sync`"),
            ("target-drift", "modified managed workflow item"),
            ("collision", "conflicting unmanaged workflow item"),
            ("invalid-or-semantic", "Author or edit the artifact natively"),
        ],
    )
    def test_publisher_classes_have_one_content_free_remedy(
        self, session_mgr: SessionManager, code: str, expected: str
    ) -> None:
        failed = MagicMock(
            returncode=13,
            stdout="",
            stderr=f"workflow publisher: {code}\n",
        )

        with (
            patch.object(session_mgr, "workflow_image_compatible", return_value=True),
            patch(_SUBPROCESS_RUN, return_value=failed),
        ):
            result = session_mgr.refresh_opencode_workflow(
                SessionTarget(container_id="container-id")
            )

        assert code in result.stderr
        assert expected in result.stderr

    def test_running_old_container_blocks_before_exec_even_if_tag_is_new(
        self, session_mgr: SessionManager
    ) -> None:
        container = MagicMock(returncode=0, stdout="sha256:old-image\n", stderr="")
        image = MagicMock(returncode=0, stdout="0\n", stderr="")

        with patch(_SUBPROCESS_RUN, side_effect=(container, image)) as run:
            result = session_mgr.refresh_opencode_workflow(
                SessionTarget(container_id="container-id")
            )

        assert result.stderr == "Rebuild/recreate required."
        assert run.call_count == 2
        assert run.call_args_list[1].args[0][3] == "sha256:old-image"

    def test_host_target_does_not_execute_refresh(self, session_mgr: SessionManager) -> None:
        with patch(_SUBPROCESS_RUN) as run:
            result = session_mgr.refresh_opencode_workflow(SessionTarget())

        assert result.success is False
        run.assert_not_called()


# ── Preflight Check Tests ──


class TestPreflightCheck:
    def test_passes_when_container_running(self, session_mgr: SessionManager) -> None:
        with patch.object(session_mgr, "_find_container", return_value="abc123"):
            session_mgr.preflight_check()  # should not raise

    def test_passes_when_host_cli_available(self, session_mgr: SessionManager) -> None:
        with (
            patch.object(session_mgr, "_find_container", return_value=None),
            patch(f"{_SESSION_MODULE}.shutil.which", return_value="/usr/bin/claude"),
        ):
            session_mgr.preflight_check()  # should not raise

    def test_raises_when_nothing_available(self, session_mgr: SessionManager) -> None:
        with (
            patch.object(session_mgr, "_find_container", return_value=None),
            patch(f"{_SESSION_MODULE}.shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="No running Djinn container"),
        ):
            session_mgr.preflight_check()

    def test_host_mode_checks_selected_agent_binary(self, session_mgr: SessionManager) -> None:
        session_mgr._agents["codex"] = AgentConfig(binary="custom-codex")
        target = SessionTarget()
        with patch(
            f"{_SESSION_MODULE}.shutil.which", return_value="/usr/bin/custom-codex"
        ) as which:
            resolved = session_mgr.preflight_check(agent="codex", target=target)

        assert resolved is target
        which.assert_called_once_with("custom-codex")

    def test_host_mode_error_names_selected_agent_binary(self, session_mgr: SessionManager) -> None:
        session_mgr._agents["codex"] = AgentConfig(binary="custom-codex")
        with (
            patch(f"{_SESSION_MODULE}.shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="custom-codex"),
        ):
            session_mgr.preflight_check(agent="codex", target=SessionTarget())

    def test_resolved_target_is_shared_by_preflight_and_launch(
        self, session_mgr: SessionManager
    ) -> None:
        mock_result = MagicMock(returncode=0)
        with (
            patch.object(session_mgr, "_find_container", return_value="abc123") as find,
            patch(_SUBPROCESS_RUN, return_value=mock_result) as run,
        ):
            target = session_mgr.resolve_target()
            assert session_mgr.preflight_check(target=target) is target
            result = session_mgr.run_interactive(workspace_dir=Path("/tmp/ws"), target=target)

        find.assert_called_once_with()
        assert "abc123" in run.call_args.args[0]
        assert result.returncode == 0


# ── Interactive Session Tests ──


class TestRunInteractiveContainer:
    def test_calls_docker_exec_with_correct_args(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch.object(session_mgr, "_find_container", return_value="abc123"),
            patch(_SUBPROCESS_RUN, return_value=mock_result) as mock_run,
        ):
            result = session_mgr.run_interactive(workspace_dir=Path("/tmp/ws"))

            cmd = mock_run.call_args[0][0]
            assert cmd[0:3] == ["docker", "exec", "-it"]
            assert "abc123" in cmd
            assert "bash" in cmd
            assert result.returncode == 0

    def test_passes_session_env_vars(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch.object(session_mgr, "_find_container", return_value="abc123"),
            patch(_SUBPROCESS_RUN, return_value=mock_result) as mock_run,
        ):
            session_mgr.run_interactive(workspace_dir=Path("/tmp/ws"))

            cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(cmd)
            assert "TERM=xterm-256color" in cmd_str
            assert "COLORTERM=truecolor" in cmd_str

    def test_command_includes_git_init(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch.object(session_mgr, "_find_container", return_value="abc123"),
            patch(_SUBPROCESS_RUN, return_value=mock_result) as mock_run,
        ):
            session_mgr.run_interactive(workspace_dir=Path("/tmp/ws"))

            cmd = mock_run.call_args[0][0]
            shell_cmd = cmd[-1]  # last arg to bash -lc
            assert "git init -q" in shell_cmd

    def test_returns_workspace_dir(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 42
        ws = Path("/tmp/ws")
        with (
            patch.object(session_mgr, "_find_container", return_value="abc123"),
            patch(_SUBPROCESS_RUN, return_value=mock_result),
        ):
            result = session_mgr.run_interactive(workspace_dir=ws)
            assert result.workspace_dir == ws
            assert result.returncode == 42


class TestRunInteractiveHost:
    def test_calls_subprocess_in_host_mode(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        ws = Path("/tmp/ws")
        with (
            patch.object(session_mgr, "_find_container", return_value=None),
            patch.object(session_mgr, "_git_init_workspace") as mock_git,
            patch(_SUBPROCESS_RUN, return_value=mock_result) as mock_run,
        ):
            result = session_mgr.run_interactive(workspace_dir=ws)

            mock_git.assert_called_once_with(ws)
            assert mock_run.call_args.kwargs.get("cwd") == ws
            assert result.returncode == 0


# ── Headless Session Tests ──


class TestRunHeadlessContainer:
    def test_calls_docker_exec_without_it(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "output"
        mock_result.stderr = ""
        with (
            patch.object(session_mgr, "_find_container", return_value="abc123"),
            patch(_AGENT_LOAD, return_value=session_mgr._agents),
            patch(_SUBPROCESS_RUN, return_value=mock_result) as mock_run,
        ):
            result = session_mgr.run_headless(workspace_dir=Path("/tmp/ws"), prompt="test")

            cmd = mock_run.call_args[0][0]
            assert "-it" not in cmd
            assert cmd[0] == "docker"
            assert cmd[1] == "exec"
            assert result.stdout == "output"

    def test_timeout_returns_124(self, session_mgr: SessionManager) -> None:
        with (
            patch.object(session_mgr, "_find_container", return_value="abc123"),
            patch(_AGENT_LOAD, return_value=session_mgr._agents),
            patch(
                _SUBPROCESS_RUN,
                side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10),
            ),
        ):
            result = session_mgr.run_headless(
                workspace_dir=Path("/tmp/ws"), prompt="test", timeout=10
            )
            assert result.returncode == 124

    def test_passes_agent_prompt_env(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        with (
            patch.object(session_mgr, "_find_container", return_value="abc123"),
            patch(_AGENT_LOAD, return_value=session_mgr._agents),
            patch(_SUBPROCESS_RUN, return_value=mock_result) as mock_run,
        ):
            session_mgr.run_headless(workspace_dir=Path("/tmp/ws"), prompt="hello world")

            cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(cmd)
            assert "AGENT_PROMPT=hello world" in cmd_str


class TestRunHeadlessHost:
    def test_captures_output_in_host_mode(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "host output"
        mock_result.stderr = ""
        with (
            patch.object(session_mgr, "_find_container", return_value=None),
            patch.object(session_mgr, "_git_init_workspace"),
            patch(_SUBPROCESS_RUN, return_value=mock_result),
        ):
            result = session_mgr.run_headless(workspace_dir=Path("/tmp/ws"), prompt="test")
            assert result.stdout == "host output"

    def test_timeout_returns_124_host(self, session_mgr: SessionManager) -> None:
        with (
            patch.object(session_mgr, "_find_container", return_value=None),
            patch.object(session_mgr, "_git_init_workspace"),
            patch(
                _SUBPROCESS_RUN,
                side_effect=subprocess.TimeoutExpired(cmd=[], timeout=10),
            ),
        ):
            result = session_mgr.run_headless(workspace_dir=Path("/tmp/ws"), prompt="test")
            assert result.returncode == 124

    def test_reuses_caller_resolved_host_target(self, session_mgr: SessionManager) -> None:
        mock_result = MagicMock(returncode=0, stdout="host", stderr="")
        with (
            patch.object(
                session_mgr,
                "_find_container",
                side_effect=AssertionError("target was resolved again"),
            ),
            patch.object(session_mgr, "_git_init_workspace"),
            patch(_SUBPROCESS_RUN, return_value=mock_result),
        ):
            result = session_mgr.run_headless(
                workspace_dir=Path("/tmp/ws"),
                prompt="test",
                target=SessionTarget(),
            )

        assert result.stdout == "host"


# ── Git Init Tests ──


class TestGitInitWorkspace:
    def test_skips_if_git_dir_exists(self, session_mgr: SessionManager, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        with patch(_SUBPROCESS_RUN) as mock_run:
            session_mgr._git_init_workspace(tmp_path)
            mock_run.assert_not_called()

    def test_runs_git_init_if_no_git_dir(self, session_mgr: SessionManager, tmp_path: Path) -> None:
        with patch(_SUBPROCESS_RUN) as mock_run:
            session_mgr._git_init_workspace(tmp_path)
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert cmd == ["git", "init", "-q"]


# ── Agent Resolution Tests ──


class TestResolveContainerWorkdir:
    def test_workspace_under_sessions_base(
        self, session_mgr: SessionManager, tmp_path: Path
    ) -> None:
        """Workspace under ~/.djinn/sessions/ maps to container path."""
        host_base = tmp_path / ".djinn" / "sessions"
        ws = host_base / "my-project" / "20260312T140000_task_abc"
        ws.mkdir(parents=True)
        with patch(f"{_SESSION_MODULE}._HOST_SESSIONS_BASE", host_base):
            result = session_mgr._resolve_container_workdir(ws)
        assert result == "/home/dev/sessions/my-project/20260312T140000_task_abc"

    def test_workspace_outside_sessions_base_falls_back(
        self, session_mgr: SessionManager, tmp_path: Path
    ) -> None:
        """Workspace outside sessions base falls back to project default."""
        ws = tmp_path / "some" / "other" / "path"
        ws.mkdir(parents=True)
        result = session_mgr._resolve_container_workdir(ws)
        assert result == "/home/dev/sessions/testproject"

    def test_workspace_at_project_root(self, session_mgr: SessionManager, tmp_path: Path) -> None:
        """Workspace at project root (no subdir) maps correctly."""
        host_base = tmp_path / ".djinn" / "sessions"
        ws = host_base / "myproject"
        ws.mkdir(parents=True)
        with patch(f"{_SESSION_MODULE}._HOST_SESSIONS_BASE", host_base):
            result = session_mgr._resolve_container_workdir(ws)
        assert result == "/home/dev/sessions/myproject"


class TestResolveAgent:
    def test_resolves_known_agent(self, session_mgr: SessionManager) -> None:
        config = session_mgr._resolve_agent("claude")
        assert config.binary == "claude"

    def test_raises_for_unknown_agent(self, session_mgr: SessionManager) -> None:
        with pytest.raises(ValueError, match="Unknown agent: unknown"):
            session_mgr._resolve_agent("unknown")


class TestSessionModelResolution:
    def test_interactive_command_uses_agent_default_model(
        self, session_mgr: SessionManager
    ) -> None:
        config = AgentConfig(binary="codex", default_model="configured-model")

        command = session_mgr._build_host_interactive_command(config, None, None)

        assert command == ["codex", "--model", "configured-model"]

    def test_interactive_command_prefers_explicit_model(self, session_mgr: SessionManager) -> None:
        config = AgentConfig(binary="codex", default_model="configured-model")

        command = session_mgr._build_host_interactive_command(config, "gpt-5.6", None)

        assert command == ["codex", "--model", "gpt-5.6"]
