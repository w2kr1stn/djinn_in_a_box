"""Tests for AI agent session management."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import traceback
from collections import UserDict
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest

from djinn_in_a_box.config.declarations import RESERVED_ENVIRONMENT
from djinn_in_a_box.config.models import AgentConfig
from djinn_in_a_box.core.docker import WorkflowImageCompatibility
from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE
from djinn_in_a_box.core.session import SessionManager, SessionResult, SessionTarget
from djinn_in_a_box.core.workflow_publisher import (
    PUBLISHER_LOCK_ERROR_PREFIX,
    PUBLISHER_WRITE_ERROR_PREFIX,
)

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
            patch.object(
                session_mgr,
                "workflow_image_compatible",
                return_value=WorkflowImageCompatibility.COMPATIBLE,
            ),
            patch(_SUBPROCESS_RUN, return_value=completed) as run,
        ):
            result = session_mgr.refresh_opencode_workflow(target)

        assert result.success is True
        run.assert_called_once_with(
            [
                DOCKER_EXECUTABLE,
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
            patch.object(
                session_mgr,
                "workflow_image_compatible",
                return_value=WorkflowImageCompatibility.COMPATIBLE,
            ),
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
            patch.object(
                session_mgr,
                "workflow_image_compatible",
                return_value=WorkflowImageCompatibility.COMPATIBLE,
            ),
            patch(_SUBPROCESS_RUN, return_value=failed),
        ):
            result = session_mgr.refresh_opencode_workflow(
                SessionTarget(container_id="container-id")
            )

        assert code in result.stderr
        assert expected in result.stderr

    def test_publisher_write_error_reports_destination_io(
        self, session_mgr: SessionManager, tmp_path: Path
    ) -> None:
        canonical = tmp_path / "canonical"
        target = tmp_path / "target"
        view = tmp_path / "view"
        publisher = (
            Path(__file__).resolve().parents[2]
            / "src/djinn_in_a_box/core/workflow_publisher.py"
        )
        canonical.mkdir()
        target.mkdir()
        view.mkdir()
        (canonical / ".djinn-config-sync.json").write_text('{"source":"opencode","items":[]}')
        sentinel = "PRIVATE-WORKFLOW-BODY-SENTINEL"
        (view / "AGENTS.md").write_text(sentinel)
        original_mode = stat.S_IMODE(target.stat().st_mode)
        target.chmod(0o555)
        try:
            failed = subprocess.run(
                [
                    sys.executable,
                    str(publisher),
                    "--view",
                    str(view),
                    "--canonical-root",
                    str(canonical),
                    "--target",
                    str(target),
                    "--manifest",
                    str(target / ".djinn-workflow-state.json"),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        finally:
            target.chmod(original_mode)

        assert failed.returncode == 13
        assert str(target) in failed.stderr
        assert "Permission denied" in failed.stderr
        assert sentinel not in failed.stderr

        with (
            patch.object(
                session_mgr,
                "workflow_image_compatible",
                return_value=WorkflowImageCompatibility.COMPATIBLE,
            ),
            patch(_SUBPROCESS_RUN, return_value=failed),
        ):
            result = session_mgr.refresh_opencode_workflow(
                SessionTarget(container_id="container-id")
            )

        assert result.returncode == failed.returncode
        assert result.stderr == (
            f"OpenCode workflow refresh failed to publish workflow to {target}: Permission denied. "
            "Remedy: Check that the workflow destination and its parent directories are writable "
            "with available space, then retry."
        )
        assert sentinel not in result.stderr

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

    def test_unreachable_container_blocks_refresh_without_rebuild_remedy(
        self, session_mgr: SessionManager
    ) -> None:
        inspect_failure = MagicMock(returncode=1, stdout="", stderr="daemon unavailable")

        with patch(_SUBPROCESS_RUN, return_value=inspect_failure) as run:
            result = session_mgr.refresh_opencode_workflow(
                SessionTarget(container_id="container-id")
            )

        assert result.stderr == "Docker daemon/container not reachable — retry."
        assert "Rebuild/recreate required" not in result.stderr
        run.assert_called_once()

    def test_missing_image_blocks_refresh_with_build_remedy(
        self, session_mgr: SessionManager
    ) -> None:
        with (
            patch.object(
                session_mgr,
                "workflow_image_compatible",
                return_value=WorkflowImageCompatibility.MISSING,
            ),
            patch(_SUBPROCESS_RUN) as run,
        ):
            result = session_mgr.refresh_opencode_workflow(
                SessionTarget(container_id="container-id")
            )

        assert result.stderr == "Workflow image is not built — run `djinn build`, then retry."
        run.assert_not_called()

    def test_inspect_timeout_is_unknown(self, session_mgr: SessionManager) -> None:
        with patch(
            _SUBPROCESS_RUN, side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=10)
        ):
            compatibility = session_mgr.workflow_image_compatible(
                SessionTarget(container_id="container-id")
            )

        assert compatibility is WorkflowImageCompatibility.UNKNOWN

    def test_missing_container_image_is_distinguished_when_daemon_replies(
        self, session_mgr: SessionManager
    ) -> None:
        container = MagicMock(returncode=0, stdout="sha256:image\n", stderr="")
        image = MagicMock(returncode=1, stdout="", stderr="")
        daemon = MagicMock(returncode=0, stdout="", stderr="")

        with patch(_SUBPROCESS_RUN, side_effect=(container, image, daemon)):
            compatibility = session_mgr.workflow_image_compatible(
                SessionTarget(container_id="container-id")
            )

        assert compatibility is WorkflowImageCompatibility.MISSING

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("", WorkflowImageCompatibility.INCOMPATIBLE),
            ("1\n", WorkflowImageCompatibility.COMPATIBLE),
        ],
    )
    def test_container_image_label_distinguishes_compatibility(
        self,
        session_mgr: SessionManager,
        label: str,
        expected: WorkflowImageCompatibility,
    ) -> None:
        container = MagicMock(returncode=0, stdout="sha256:image\n", stderr="")
        image = MagicMock(returncode=0, stdout=label, stderr="")

        with patch(_SUBPROCESS_RUN, side_effect=(container, image)):
            compatibility = session_mgr.workflow_image_compatible(
                SessionTarget(container_id="container-id")
            )

        assert compatibility is expected

    def test_publisher_refresh_maps_last_publisher_line_after_noise(
        self, session_mgr: SessionManager
    ) -> None:
        failed = MagicMock(
            returncode=13,
            stdout="",
            stderr="daemon note\nworkflow publisher: target-drift\ntrailing diagnostic\n",
        )

        with (
            patch.object(
                session_mgr,
                "workflow_image_compatible",
                return_value=WorkflowImageCompatibility.COMPATIBLE,
            ),
            patch(_SUBPROCESS_RUN, return_value=failed),
        ):
            result = session_mgr.refresh_opencode_workflow(
                SessionTarget(container_id="container-id")
            )

        assert "target-drift" in result.stderr
        assert "modified managed workflow item" in result.stderr

    def test_publisher_refresh_prefers_the_later_lock_diagnostic_over_write_diagnostic(
        self, session_mgr: SessionManager
    ) -> None:
        failed = MagicMock(
            returncode=13,
            stdout="",
            stderr=(
                "workflow publisher: invalid-or-semantic\n"
                + PUBLISHER_WRITE_ERROR_PREFIX
                + json.dumps({"destination": "/destination", "error": "No space left on device"})
                + "\n"
                + PUBLISHER_LOCK_ERROR_PREFIX
                + json.dumps({"root": "/canonical", "error": "No locks available"})
                + "\n"
            ),
        )

        with (
            patch.object(
                session_mgr,
                "workflow_image_compatible",
                return_value=WorkflowImageCompatibility.COMPATIBLE,
            ),
            patch(_SUBPROCESS_RUN, return_value=failed),
        ):
            result = session_mgr.refresh_opencode_workflow(
                SessionTarget(container_id="container-id")
            )

        assert result.stderr == (
            "OpenCode workflow refresh failed: Canonical workflow lock failed at /canonical: "
            "No locks available. Remedy: Check that the canonical workflow root is a readable, "
            "lockable directory and that no other Djinn process is stuck on it, then retry."
        )

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
            assert cmd[0:3] == [DOCKER_EXECUTABLE, "exec", "-it"]
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
            assert cmd[0] == DOCKER_EXECUTABLE
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


@pytest.mark.parametrize("mode", ["container", "host"])
def test_declarations_inherit_without_creation(tmp_path, monkeypatch, session_mgr, mode):
    from djinn_in_a_box.core import docker

    forbidden = MagicMock(side_effect=AssertionError("attachment must not create or resolve"))
    monkeypatch.setattr(docker, "resolve_declared_entries", forbidden)
    monkeypatch.setattr(docker, "compose_run", forbidden)
    monkeypatch.setattr(docker, "compose_up_detached", forbidden)
    monkeypatch.setattr(session_mgr, "_git_init_workspace", lambda workspace: None)
    monkeypatch.setattr(
        session_mgr, "_resolve_container_workdir", lambda workspace: "/home/dev/projects"
    )
    calls = []

    def capture(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return MagicMock(returncode=0)

    monkeypatch.setattr("djinn_in_a_box.core.session.subprocess.run", capture)
    monkeypatch.setenv("CDP_HOST", "host-literal")
    result = session_mgr.run_interactive(
        workspace_dir=tmp_path,
        target=SessionTarget(container_id="running" if mode == "container" else None),
    )
    assert result.returncode == 0 and len(calls) == 1
    cmd, kwargs = calls[0]
    if mode == "container":
        assert cmd[:3] == [DOCKER_EXECUTABLE, "exec", "-it"]
        assert not any("CDP_HOST=" in arg or "DJINN_DECLARED_" in arg for arg in cmd)
    else:
        assert cmd[0] == "claude" and kwargs["env"]["CDP_HOST"] == "host-literal"
    forbidden.assert_not_called()


# ── Per-session Environment Tests ──


@pytest.mark.parametrize("method", ["run_interactive", "run_headless"])
@pytest.mark.parametrize("mode", ["container", "host"])
def test_session_environment_is_literal_and_isolated(
    session_mgr, tmp_path, monkeypatch, caplog, method, mode
):
    secret = "SYNTHETIC-SESSION-SECRET"
    additions = {
        "OPENAI_API_KEY": secret,
        "SESSION_EMPTY": "",
        "_SESSION_UNICODE": "Grüße 雪 😀 e\u0301",
        "SESSION_LITERAL": "first\n$HOME 'single' \"double\" = last\r\n",
    }
    original = additions.copy()
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-inherited-key")
    monkeypatch.setenv("SESSION_EMPTY", "synthetic-inherited-nonempty")
    monkeypatch.setenv("SESSION_INHERITED", "synthetic-inherited-value")
    monkeypatch.setenv("TERM", "inherited-term")
    monkeypatch.setenv("COLORTERM", "inherited-colorterm")
    monkeypatch.setenv("DOCKER_HOST", "unix:///synthetic-docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "synthetic-context")
    before = dict(os.environ)
    workspace = tmp_path / "sessions" / "testproject" / "task with space"
    target = SessionTarget(container_id="stable-container" if mode == "container" else None)
    completed = MagicMock(returncode=0, stdout=secret + "\n", stderr="child diagnostic\n")
    kwargs = {"prompt": "test prompt"} if method == "run_headless" else {
        "initial_prompt": "test prompt"
    }
    caplog.set_level(logging.DEBUG, logger=_SESSION_MODULE)

    with (
        patch(f"{_SESSION_MODULE}._HOST_SESSIONS_BASE", tmp_path / "sessions"),
        patch.object(session_mgr, "resolve_target") as resolve,
        patch.object(session_mgr, "_git_init_workspace") as git_init,
        patch(_SUBPROCESS_RUN, return_value=completed) as run,
    ):
        result = getattr(session_mgr, method)(
            workspace_dir=workspace, target=target, model="selected-model", env=additions, **kwargs
        )

    resolve.assert_not_called()
    run.assert_called_once()
    cmd = run.call_args.args[0]
    child_env = run.call_args.kwargs["env"]
    for key, value in additions.items():
        assert child_env[key] == value
    assert child_env["SESSION_INHERITED"] == "synthetic-inherited-value"
    assert child_env["DOCKER_HOST"] == "unix:///synthetic-docker.sock"
    assert child_env["DOCKER_CONTEXT"] == "synthetic-context"
    assert additions == original
    environment_unchanged = dict(os.environ) == before
    assert environment_unchanged
    assert secret not in repr(cmd)
    assert secret not in caplog.text
    assert result.workspace_dir == workspace
    assert result.returncode == 0
    if method == "run_headless":
        assert result.stdout == completed.stdout
        assert result.stderr == completed.stderr
    if mode == "container":
        git_init.assert_not_called()
        assert cmd[:2] == [DOCKER_EXECUTABLE, "exec"]
        assert ("-it" in cmd) == (method == "run_interactive")
        forwarded = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-e"]
        expected_forwarded = {*additions, "TERM=xterm-256color", "COLORTERM=truecolor"}
        if method == "run_headless":
            expected_forwarded.add("AGENT_PROMPT=test prompt")
        assert set(forwarded) == expected_forwarded
        assert cmd[cmd.index("-w") + 1] == "/home/dev/sessions/testproject/task with space"
        assert cmd[-4:-1] == [target.container_id, "bash", "-lc"]
        assert "git init -q" in cmd[-1] and "selected-model" in cmd[-1]
        if method == "run_headless":
            assert "AGENT_PROMPT=test prompt" in forwarded
            assert '"$AGENT_PROMPT"' in cmd[-1]
        else:
            assert "'test prompt'" in cmd[-1]
    else:
        git_init.assert_called_once_with(workspace)
        assert cmd[0] == "claude"
        assert cmd[cmd.index("--model") + 1] == "selected-model"
        assert ("-p" in cmd) == (method == "run_headless")
        assert cmd[-1] == "test prompt"
        assert run.call_args.kwargs["cwd"] == workspace
        assert child_env["TERM"] == "xterm-256color"
        assert child_env["COLORTERM"] == "truecolor"


@pytest.mark.parametrize("method", ["run_interactive", "run_headless"])
@pytest.mark.parametrize("mode", ["container", "host"])
def test_session_environment_defaults_and_no_carryover(session_mgr, tmp_path, method, mode):
    target = SessionTarget(container_id="stable-container" if mode == "container" else None)
    kwargs = {"prompt": "test"} if method == "run_headless" else {}
    completed = MagicMock(returncode=0, stdout="", stderr="")
    with (
        patch.object(session_mgr, "_git_init_workspace"),
        patch(_SUBPROCESS_RUN, return_value=completed) as run,
    ):
        launch = getattr(session_mgr, method)
        launch(workspace_dir=tmp_path, target=target, **kwargs)
        launch(workspace_dir=tmp_path, target=target, env={"SESSION_ONCE": "synthetic"}, **kwargs)
        for empty in (None, {}):
            launch(workspace_dir=tmp_path, target=target, env=empty, **kwargs)

    baseline = run.call_args_list[0]
    for call in run.call_args_list[2:]:
        assert call.args == baseline.args
        same_options = call.kwargs == baseline.kwargs
        assert same_options


@pytest.mark.parametrize("method", ["run_interactive", "run_headless"])
@pytest.mark.parametrize("mode", ["container", "host"])
def test_session_environment_snapshots_before_agent_resolution(
    session_mgr, tmp_path, monkeypatch, method, mode
):
    additions = {"SESSION_SNAPSHOT": "synthetic-original"}
    monkeypatch.delenv("SESSION_LATE", raising=False)
    config = session_mgr._agents["claude"]

    def mutate_input(agent):
        additions["SESSION_SNAPSHOT"] = "synthetic-changed"
        additions["SESSION_LATE"] = "synthetic-late"
        return config

    target = SessionTarget(container_id="stable-container" if mode == "container" else None)
    kwargs = {"prompt": "test"} if method == "run_headless" else {}
    with (
        patch.object(session_mgr, "_resolve_agent", side_effect=mutate_input),
        patch.object(session_mgr, "_git_init_workspace"),
        patch(_SUBPROCESS_RUN, return_value=MagicMock(returncode=0, stdout="", stderr="")) as run,
    ):
        getattr(session_mgr, method)(workspace_dir=tmp_path, target=target, env=additions, **kwargs)

    assert run.call_args.kwargs["env"]["SESSION_SNAPSHOT"] == "synthetic-original"
    assert "SESSION_LATE" not in run.call_args.kwargs["env"]
    assert additions["SESSION_SNAPSHOT"] == "synthetic-changed"


@pytest.mark.parametrize("method", ["run_interactive", "run_headless"])
@pytest.mark.parametrize(
    "invalid_env",
    [
        [], (), set(), "", b"", 0, False, UserDict(), MappingProxyType({}),
        [("SESSION_VALUE", "synthetic")], "not a dictionary",
        {"SESSION_VALUE": None}, {"SESSION_VALUE": 0}, {"SESSION_VALUE": False},
        {"SESSION_VALUE": b"bytes"}, {"SESSION_VALUE": []},
        {1: "SYNTHETIC-INVALID-SECRET"}, {None: "SYNTHETIC-INVALID-SECRET"},
        {"": "SYNTHETIC-INVALID-SECRET"}, {"1KEY": "SYNTHETIC-INVALID-SECRET"},
        {"BAD-KEY": "SYNTHETIC-INVALID-SECRET"}, {"KEY=VALUE": "SYNTHETIC-INVALID-SECRET"},
        {"KEY\n": "SYNTHETIC-INVALID-SECRET"}, {"KEY\x00": "SYNTHETIC-INVALID-SECRET"},
        {"ÄKEY": "SYNTHETIC-INVALID-SECRET"}, {"KEY\ud800": "SYNTHETIC-INVALID-SECRET"},
        {"SESSION_VALUE": "SYNTHETIC-INVALID-SECRET\x00"},
        {"SESSION_VALUE": "SYNTHETIC-INVALID-SECRET\ud800"},
        {"SESSION_VALUE": "SYNTHETIC-INVALID-SECRET\udcff"},
    ],
)
def test_invalid_session_environment_is_rejected_before_other_work(
    session_mgr, tmp_path, method, invalid_env
):
    kwargs = {"prompt": "test"} if method == "run_headless" else {}
    with (
        patch.object(session_mgr, "_resolve_agent") as agent,
        patch.object(session_mgr, "resolve_target") as resolve,
        patch.object(session_mgr, "_git_init_workspace") as git_init,
        patch(_SUBPROCESS_RUN) as run,
        pytest.raises(ValueError) as exc,
    ):
        getattr(session_mgr, method)(workspace_dir=tmp_path, env=invalid_env, **kwargs)

    agent.assert_not_called()
    resolve.assert_not_called()
    git_init.assert_not_called()
    run.assert_not_called()
    diagnostic = "".join(traceback.format_exception(exc.value))
    assert "SYNTHETIC-INVALID-SECRET" not in diagnostic
    assert "UnicodeEncodeError" not in diagnostic
    assert exc.value.__suppress_context__


@pytest.mark.parametrize("method", ["run_interactive", "run_headless"])
@pytest.mark.parametrize(
    "name",
    sorted(RESERVED_ENVIRONMENT)
    + [
        "DOCKER_CONFIG", "DOCKER_API_VERSION", "COMPOSE_FUTURE", "DJINN_FUTURE",
        "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_FUNC_FUTURE", "ENV", "SHELLOPTS",
        "BASHOPTS", "CDPATH", "GLOBIGNORE", "BASH_ENV", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "XDG_CONFIG_HOME", "GODEBUG", "GOTRACEBACK",
        "BASH", "BASHPID", "COMP_WORDBREAKS", "EPOCHREALTIME", "EPOCHSECONDS",
        "HISTCMD", "LINENO", "OLDPWD", "OPTERR", "OPTIND", "PPID", "PS1", "PS2",
        "PWD", "RANDOM", "SHLVL", "SRANDOM", "_",
        "http_proxy", "HTTP_PROXY", "HtTp_PrOxY",
        "https_proxy", "HTTPS_PROXY", "hTtPs_pRoXy",
        "all_proxy", "ALL_PROXY", "AlL_PrOxY", "no_proxy", "NO_PROXY", "No_PrOxY",
    ],
)
def test_protected_session_environment_is_rejected(session_mgr, tmp_path, method, name):
    secret = "SYNTHETIC-PROTECTED-SECRET"
    additions = {name: secret}
    kwargs = {"prompt": "test"} if method == "run_headless" else {}
    with (
        patch.object(session_mgr, "resolve_target") as resolve,
        patch(_SUBPROCESS_RUN) as run,
        pytest.raises(ValueError) as exc,
    ):
        getattr(session_mgr, method)(workspace_dir=tmp_path, env=additions, **kwargs)
    resolve.assert_not_called()
    run.assert_not_called()
    assert secret not in "".join(traceback.format_exception(exc.value))


@pytest.mark.parametrize("method", ["run_interactive", "run_headless"])
@pytest.mark.parametrize("encoding_failure", ["mismatch", "non_utf8", "unencodable"])
def test_session_environment_checks_actual_host_encoding(
    session_mgr, tmp_path, method, encoding_failure
):
    secret = "SYNTHETIC-ENCODING-SECRET-雪"
    kwargs = {"prompt": "test"} if method == "run_headless" else {}
    error = UnicodeEncodeError("ascii", secret, 0, len(secret), secret)
    with (
        patch(
            f"{_SESSION_MODULE}.os.fsencode",
            return_value=b"different" if encoding_failure == "mismatch" else b"\xff",
            side_effect=error if encoding_failure == "unencodable" else None,
        ) as encode,
        patch.object(session_mgr, "_resolve_agent") as agent,
        patch(_SUBPROCESS_RUN) as run,
        pytest.raises(ValueError) as exc,
    ):
        getattr(session_mgr, method)(
            workspace_dir=tmp_path, env={"SESSION_VALUE": secret}, **kwargs
        )
    encode.assert_called_once_with(secret)
    agent.assert_not_called()
    run.assert_not_called()
    diagnostic = "".join(traceback.format_exception(exc.value))
    assert secret not in diagnostic
    assert "UnicodeEncodeError" not in diagnostic and "UnicodeDecodeError" not in diagnostic
    assert exc.value.__suppress_context__


@pytest.mark.parametrize("method", ["run_interactive", "run_headless"])
def test_session_environment_is_only_applied_to_host_agent_start(session_mgr, tmp_path, method):
    secret = "SYNTHETIC-AGENT-ONLY-SECRET"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    completed = MagicMock(returncode=0, stdout="", stderr="")
    kwargs = {"prompt": "test"} if method == "run_headless" else {}
    with patch(_SUBPROCESS_RUN, return_value=completed) as run:
        result = getattr(session_mgr, method)(
            workspace_dir=workspace, env={"SESSION_VALUE": secret}, **kwargs
        )
    assert result.success
    assert run.call_count == 3
    discovery, git_init, agent = run.call_args_list
    assert discovery.args[0][:2] == [DOCKER_EXECUTABLE, "ps"]
    assert git_init.args[0] == ["git", "init", "-q"]
    assert discovery.kwargs.get("env") is None
    assert git_init.kwargs.get("env") is None
    assert agent.kwargs["env"]["SESSION_VALUE"] == secret
    assert not list(workspace.iterdir())


@pytest.mark.parametrize("method", ["run_interactive", "run_headless"])
@pytest.mark.parametrize("mode", ["container", "host"])
@pytest.mark.parametrize("error_type, code", [(FileNotFoundError, 127), (PermissionError, 126)])
def test_session_environment_is_not_in_spawn_diagnostics(
    session_mgr, tmp_path, caplog, method, mode, error_type, code
):
    secret = "SYNTHETIC-SPAWN-SECRET"
    target = SessionTarget(container_id="stable-container" if mode == "container" else None)
    kwargs = {"prompt": "test"} if method == "run_headless" else {}
    caplog.set_level(logging.DEBUG, logger=_SESSION_MODULE)
    with (
        patch.object(session_mgr, "_git_init_workspace"),
        patch(_SUBPROCESS_RUN, side_effect=error_type("synthetic spawn failure")) as run,
    ):
        result = getattr(session_mgr, method)(
            workspace_dir=tmp_path, target=target, env={"SESSION_VALUE": secret}, **kwargs
        )
    assert result.returncode == code
    assert secret not in repr(result)
    assert secret not in repr(run.call_args.args)
    assert secret not in caplog.text


@pytest.mark.parametrize("mode", ["container", "host"])
@pytest.mark.parametrize("output_type", ["none", "bytes", "text"])
def test_session_environment_timeout_preserves_child_output(
    session_mgr, tmp_path, caplog, mode, output_type
):
    secret = "SYNTHETIC-TIMEOUT-SECRET"
    stdout = stderr = None
    if output_type == "bytes":
        stdout, stderr = secret.encode() + b"\xff", b"partial stderr\xff"
    elif output_type == "text":
        stdout, stderr = secret + "\n", "partial stderr\n"
    timeout = subprocess.TimeoutExpired(cmd=[], timeout=9, output=stdout, stderr=stderr)
    target = SessionTarget(container_id="stable-container" if mode == "container" else None)
    caplog.set_level(logging.DEBUG, logger=_SESSION_MODULE)
    with (
        patch.object(session_mgr, "_git_init_workspace"),
        patch(_SUBPROCESS_RUN, side_effect=timeout) as run,
    ):
        result = session_mgr.run_headless(
            workspace_dir=tmp_path, prompt="test", timeout=9, target=target,
            env={"SESSION_VALUE": secret},
        )
    assert result.returncode == 124 and result.workspace_dir == tmp_path
    assert secret not in repr(run.call_args.args)
    assert secret not in caplog.text
    if output_type == "none":
        assert result.stdout == "" and result.stderr == "Timeout after 9s"
        assert secret not in repr(result)
    elif output_type == "bytes":
        assert result.stdout == secret + "�" and result.stderr == "partial stderr�"
    else:
        assert result.stdout == stdout and result.stderr == stderr


def test_headless_host_child_receives_literal_session_environment(
    session_mgr, tmp_path, monkeypatch
):
    additions = {
        "SESSION_CHILD_SECRET": "SYNTHETIC-CHILD-SECRET",
        "SESSION_CHILD_EMPTY": "",
        "SESSION_CHILD_LITERAL": "first\n$HOME 'single' \"double\" = last",
        "SESSION_CHILD_UNICODE": "Grüße 雪 😀 e\u0301",
    }
    monkeypatch.setenv("SESSION_CHILD_SECRET", "synthetic-inherited-secret")
    monkeypatch.setenv("SESSION_CHILD_INHERITED", "synthetic-inherited-value")
    before = dict(os.environ)
    keys = [*additions, "SESSION_CHILD_INHERITED", "TERM", "COLORTERM"]
    script = (
        "import json, os; "
        f"print(json.dumps({{key: os.environ[key] for key in {keys!r}}}))"
    )
    session_mgr._agents["python"] = AgentConfig(binary=sys.executable, headless_flags=["-c"])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()

    result = session_mgr.run_headless(
        workspace_dir=workspace, prompt=script, agent="python", target=SessionTarget(),
        env=additions,
    )

    assert result.returncode == 0 and result.stderr == ""
    observed = json.loads(result.stdout)
    assert observed == {
        **additions, "SESSION_CHILD_INHERITED": "synthetic-inherited-value",
        "TERM": "xterm-256color", "COLORTERM": "truecolor",
    }
    environment_unchanged = dict(os.environ) == before
    assert environment_unchanged
    assert list(workspace.iterdir()) == [workspace / ".git"]


@pytest.mark.parametrize("method", ["run_interactive", "run_headless"])
def test_container_session_shell_preserves_allowed_environment(session_mgr, tmp_path, method):
    """Exercise the generated Bash command with real children, without a Docker daemon."""
    additions = {
        "OPENAI_API_KEY": "SYNTHETIC-SHELL-SECRET",
        "SESSION_EMPTY": "",
        "_SESSION_LITERAL": "first\n$HOME 'single' \"double\" = Grüße 雪 😀",
    }
    script = (
        "import json, os; "
        f"print(json.dumps({{key: os.environ[key] for key in {list(additions)!r}}}))"
    )
    session_mgr._agents["python"] = AgentConfig(
        binary=sys.executable, headless_flags=["-I", "-c"], write_flags=["-I", "-c"]
    )
    kwargs = {"prompt": script} if method == "run_headless" else {"initial_prompt": script}
    with (
        patch.object(session_mgr, "_resolve_container_workdir", return_value=str(tmp_path)),
        patch(_SUBPROCESS_RUN, return_value=MagicMock(returncode=0, stdout="", stderr="")) as run,
    ):
        getattr(session_mgr, method)(
            workspace_dir=tmp_path, agent="python", target=SessionTarget(container_id="synthetic"),
            env=additions, **kwargs,
        )

    # Docker forwarding is tested separately; this isolates its real Bash command.
    shell_args = run.call_args.args[0][-2:]
    child = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", *shell_args],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "AGENT_PROMPT": script,
            **additions,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stderr == ""
    assert json.loads(child.stdout) == additions
