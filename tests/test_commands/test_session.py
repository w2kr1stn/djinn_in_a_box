"""Tests for the session CLI command."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.core.session import SessionResult

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
    with patch("djinn_in_a_box.core.session.load_agents", return_value=agents):
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
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
            patch("djinn_in_a_box.commands.session.sys") as mock_sys,
        ):
            mock_sys.stdin.isatty.return_value = True
            mock_instance = mock_mgr.return_value
            mock_instance.run_interactive.return_value = mock_session_result
            mock_instance.preflight_check.return_value = None

            runner.invoke(app, ["session", "--project", "testproj"])
            mock_instance.run_interactive.assert_called_once()
            assert mock_instance.run_interactive.call_args.kwargs["model"] is None

    def test_headless_calls_run_headless(self, tmp_path: Path) -> None:
        workspace = tmp_path / ".djinn" / "sessions" / "testproj"
        workspace.mkdir(parents=True)

        mock_session_result = SessionResult(returncode=0, stdout="output")
        with (
            patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
            patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
        ):
            mock_instance = mock_mgr.return_value
            mock_instance.run_headless.return_value = mock_session_result
            mock_instance.preflight_check.return_value = None

            runner.invoke(app, ["session", "--project", "testproj", "--prompt", "hello"])
            mock_instance.run_headless.assert_called_once()
            assert mock_instance.run_headless.call_args.kwargs["model"] is None
