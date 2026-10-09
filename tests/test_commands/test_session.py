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
            mock_instance.workflow_image_compatible.return_value = (
                WorkflowImageCompatibility.COMPATIBLE
            )
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
            instance.workflow_image_compatible.return_value = WorkflowImageCompatibility.COMPATIBLE
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
            instance.workflow_image_compatible.return_value = WorkflowImageCompatibility.COMPATIBLE
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
            instance.workflow_image_compatible.return_value = WorkflowImageCompatibility.COMPATIBLE
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

    def test_blocked_codex_workflow_stops_before_workspace_creation_and_agent(
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
                ["session", "--project", "new-project", "--agent", "codex", "--create"],
            )

        assert result.exit_code == 1
        workflow.assert_called_once()
        assert workflow.call_args.args == (
            Path("/project"),
            (WorkflowDeliveryTarget("codex", Path("/runtime/codex")),),
        )
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
            instance.workflow_image_compatible.return_value = WorkflowImageCompatibility.COMPATIBLE
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

    def test_missing_running_image_stops_with_build_remedy(
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
            instance.workflow_image_compatible.return_value = WorkflowImageCompatibility.MISSING
            result = runner.invoke(
                app,
                ["session", "--project", "new-project", "--agent", "opencode", "--create"],
            )

        assert result.exit_code == 1
        assert "Workflow image is not built." in result.output
        assert "Run `djinn build`, then retry." in result.output
        assert "Docker daemon/container not reachable" not in result.output
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


def test_host_workflow_confirmation_output_and_default(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from djinn_in_a_box.commands import session as command_module
    from djinn_in_a_box.core.config_workflow import HostWorkflowChange, HostWorkflowReview

    review = HostWorkflowReview(
        "claude",
        (tmp_path / "source[1]",),
        tmp_path / "host[1]",
        frozenset(),
        (
            HostWorkflowChange("new", "a[1]"),
            HostWorkflowChange("changed", "b (executable)"),
            HostWorkflowChange("removed", "settings.json: hooks.Stop"),
        ),
    )
    output = []
    monkeypatch.setattr(
        command_module.err_console, "print", lambda *args, **kwargs: output.append((args, kwargs))
    )
    monkeypatch.setattr(
        command_module, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )
    prompts = []

    def confirm(prompt, **kwargs):
        prompts.append((prompt, kwargs))
        return False

    monkeypatch.setattr(command_module.typer, "confirm", confirm)
    assert command_module._confirm_host_workflow(review) is False
    assert prompts == [
        (
            f"Publish this workflow to {review.destination_root} and run claude on the host?",
            {"default": False, "err": True},
        )
    ]
    assert [args[0] for args, _kwargs in output] == [
        "Host workflow for claude differs from your last confirmation.",
        "Sources:",
        f"  {review.sources[0]}",
        "+ a[1]",
        "~ b (executable)",
        "- settings.json: hooks.Stop",
    ]
    assert all(
        kwargs.get("markup") is False
        and kwargs.get("emoji") is False
        and kwargs.get("soft_wrap") is True
        for _args, kwargs in output
        if _args[0] != "Sources:"
    )
    monkeypatch.setattr(
        command_module, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: False))
    )
    prompts.clear()
    assert command_module._confirm_host_workflow(review) is False
    assert prompts == []


def test_host_workflow_review_escapes_nonprintable_text(tmp_path, monkeypatch):
    from io import StringIO
    from types import SimpleNamespace

    from rich.console import Console

    from djinn_in_a_box.commands import session as command_module
    from djinn_in_a_box.core.config_workflow import HostWorkflowChange, HostWorkflowReview

    characters = ("\n", "\r", "\x1b[2J\x1b[H", "\u202e", "\x85", "\u2028")
    escaped = (r"\n", r"\r", r"\x1b[2J\x1b[H", r"\u202e", r"\x85", r"\u2028")
    review = HostWorkflowReview(
        "claude",
        (*(tmp_path / f"source{ch}[1]é" for ch in characters), tmp_path / "source:smile:"),
        tmp_path / ("hosté" + "".join(characters)),
        frozenset(),
        (
            *(HostWorkflowChange("new", f"item{ch}[1]é") for ch in characters),
            # Rich turns :name: codes into emoji unless disabled; names must stay literal.
            HostWorkflowChange("new", "context/:england: notes.md"),
        ),
    )
    stream = StringIO()
    monkeypatch.setattr(
        command_module,
        "err_console",
        Console(file=stream, force_terminal=True, color_system=None, highlight=False, width=20),
    )
    monkeypatch.setattr(
        command_module, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )
    prompts = []
    monkeypatch.setattr(
        command_module.typer, "confirm", lambda prompt, **kwargs: prompts.append(prompt) or False
    )
    assert command_module._confirm_host_workflow(review) is False
    lines = stream.getvalue().splitlines()
    assert lines == [
        "Host workflow for claude differs from your last confirmation.",
        "Sources:",
        *(f"  {tmp_path}/source{value}[1]é" for value in escaped),
        f"  {tmp_path}/source:smile:",
        *(f"+ item{value}[1]é" for value in escaped),
        "+ context/:england: notes.md",
    ]
    assert all(ch.isprintable() for line in lines for ch in line)
    assert prompts == [
        f"Publish this workflow to {tmp_path}/hosté{''.join(escaped)} and run claude on the host?"
    ]
    assert all(ch.isprintable() for ch in prompts[0])


def test_host_workflow_eof_refuses_without_host_writes_or_launch(tmp_path, monkeypatch):
    from functools import partial
    from types import SimpleNamespace

    from djinn_in_a_box.commands import session as command_module
    from djinn_in_a_box.config.loader import save_config
    from djinn_in_a_box.config.models import AppConfig
    from djinn_in_a_box.core import config_workflow

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
    (project / "config/claude/AGENTS.md").write_text("workflow\n")
    bridge = tmp_path / "CLAUDE.md"
    bridge.write_text("@AGENTS.md\n")
    config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "runtime")
    config_path = tmp_path / "djinn.toml"
    save_config(config, config_path)
    monkeypatch.setattr(command_module, "load_config", lambda: config)
    monkeypatch.setattr(command_module, "get_project_root", lambda: project)
    monkeypatch.setattr(
        command_module,
        "prepare_config_workflow",
        partial(config_workflow.prepare_config_workflow, config_path=config_path),
    )
    monkeypatch.setattr(config_workflow, "_claude_bridge_path", lambda: bridge)
    monkeypatch.setattr(
        command_module, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )
    with (
        patch("djinn_in_a_box.commands.session.Path.home", return_value=home),
        patch("djinn_in_a_box.commands.session.SessionManager") as manager,
    ):
        instance = manager.return_value
        instance.resolve_target.return_value = SessionTarget()
        instance.preflight_check.return_value = None
        instance.run_headless.return_value = SessionResult(0)
        result = runner.invoke(
            app, ["session", "--project", "eof", "--create", "--prompt", "hello"], input=""
        )
    assert result.exit_code == 1, (result.output, result.exception)
    stderr = " ".join(result.stderr.split())
    assert "Host workflow for claude was not confirmed; nothing was published." in stderr
    assert (
        "Run `djinn session --agent claude` in a terminal, review the listed changes and confirm."
        in stderr
    )
    assert list(home.iterdir()) == []
    assert not config_workflow.HOST_WORKFLOW_TRUST_FILE.exists()
    instance.preflight_check.assert_not_called()
    instance.run_headless.assert_not_called()
    instance.run_interactive.assert_not_called()


def test_host_workflow_acceptance_modified_hook(tmp_path, monkeypatch):
    import builtins
    import io
    import json
    import os
    import subprocess
    from types import SimpleNamespace

    from djinn_in_a_box.commands import session as command_module
    from djinn_in_a_box.config import loader
    from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig
    from djinn_in_a_box.core import config_workflow, docker
    from djinn_in_a_box.core import session as core_session
    from djinn_in_a_box.core.workflow_publisher import RUNTIME_MANIFEST_NAME

    home = tmp_path / "home"
    project = tmp_path / "project"
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
    source = project / "config/claude"
    (source / "AGENTS.md").write_text("shared workflow\n")
    bridge = tmp_path / "bridge/CLAUDE.md"
    bridge.parent.mkdir()
    bridge.write_text("@AGENTS.md\n")
    hook = source / "security_reminder_hook.py"
    marker = tmp_path / "executions"

    def hook_version(version):
        hook.write_text(
            "from pathlib import Path\n"
            + f"with Path({str(marker)!r}).open('a') as stream:\n"
            + f"    stream.write({version!r} + '\\n')\n"
        )
        hook.chmod(0o700)

    hook_version("v1")
    (source / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Edit|Write",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 ~/.claude_seed/security_reminder_hook.py",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )
    config_path = tmp_path / "djinn.toml"
    loader.save_config(
        AppConfig(
            code_dir=tmp_path,
            config_root=tmp_path / "runtime",
            config_sync=ConfigSyncConfig(source="claude"),
        ),
        config_path,
    )
    (home / ".djinn/sessions/acceptance").mkdir(parents=True)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake_agent = binaries / "claude"
    fake_agent.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess\n"
        "from pathlib import Path\n"
        "settings = json.loads((Path(os.environ['HOME']) / '.claude/settings.json').read_text())\n"
        "command = settings['hooks']['PreToolUse'][0]['hooks'][0]['command']\n"
        "subprocess.run(['sh', '-c', command], stdin=subprocess.DEVNULL, check=True)\n"
    )
    fake_agent.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    original_config_file = loader.CONFIG_FILE
    original_config_dir = config_workflow.CONFIG_DIR
    monkeypatch.setattr(loader, "CONFIG_FILE", config_path)
    monkeypatch.setattr(command_module, "load_config", loader.load_config)
    monkeypatch.setattr(
        command_module, "prepare_config_workflow", config_workflow.prepare_config_workflow
    )
    monkeypatch.setattr(command_module, "get_project_root", lambda: project)
    monkeypatch.setattr(command_module, "get_config_root", docker.get_config_root)
    monkeypatch.setattr(config_workflow, "_claude_bridge_path", lambda: bridge)
    monkeypatch.setattr(core_session.SessionManager, "_find_container", lambda self: None)

    # Trap both built-in and pathlib opens of the original host configuration.
    def guard_open(original):
        def guarded(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)):
                selected = Path(path).absolute()
                assert selected != original_config_file
                assert not selected.is_relative_to(original_config_dir)
            return original(path, *args, **kwargs)

        return guarded

    monkeypatch.setattr(builtins, "open", guard_open(builtins.open))
    monkeypatch.setattr(io, "open", guard_open(io.open))
    original_run = subprocess.run
    launches = []

    def run(cmd, *args, **kwargs):
        if cmd[0] == "claude":
            launches.append(tuple(cmd))
        if kwargs.get("capture_output"):
            kwargs.setdefault("stdin", subprocess.DEVNULL)
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(core_session.subprocess, "run", run)
    arguments = ["session", "--project", "acceptance", "--agent", "claude", "--prompt", "hello"]

    def invoke(tty, answer=""):
        monkeypatch.setattr(
            command_module, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: tty))
        )
        return runner.invoke(app, arguments, input=answer)

    first = invoke(True, "y\n")
    assert first.exit_code == 0, (first.output, first.exception)
    assert marker.read_text() == "v1\n"
    assert len(launches) == 1
    record = config_workflow.HOST_WORKFLOW_TRUST_FILE
    assert record.exists()
    host_hook = home / ".claude/security_reminder_hook.py"
    manifest = home / ".claude" / RUNTIME_MANIFEST_NAME
    before = (host_hook.read_bytes(), manifest.read_bytes(), record.read_bytes())
    hook_version("v2")
    for tty, answer in [(False, ""), (True, "n\n"), (True, "\n")]:
        launches.clear()
        declined = invoke(tty, answer)
        assert declined.exit_code == 1, (declined.output, declined.exception)
        assert "~ security_reminder_hook.py (executable)" in declined.stderr
        assert (
            "Host workflow for claude was not confirmed; nothing was published." in declined.stderr
        )
        assert "Run `djinn session --agent claude` in a terminal" in declined.stderr
        assert (host_hook.read_bytes(), manifest.read_bytes(), record.read_bytes()) == before
        assert launches == []
        assert marker.read_text() == "v1\n"
    accepted = invoke(True, "y\n")
    assert accepted.exit_code == 0, (accepted.output, accepted.exception)
    assert marker.read_text() == "v1\nv2\n"
    assert len(launches) == 1
    unchanged = invoke(False)
    assert unchanged.exit_code == 0, (unchanged.output, unchanged.exception)
    assert "Publish this workflow" not in unchanged.output
    assert marker.read_text() == "v1\nv2\nv2\n"
    assert len(launches) == 2

    # Publication may succeed while recording fails; the agent still must not start.
    prior_record = record.read_bytes()
    hook_version("v3")
    original_replace = os.replace

    def refuse_record(src, dst, **kwargs):
        if Path(dst) == record:
            raise PermissionError("trust directory is read-only")
        return original_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", refuse_record)
    launches.clear()
    failed_record = invoke(True, "y\n")
    assert failed_record.exit_code == 1, (failed_record.output, failed_record.exception)
    assert "Failed to record host workflow trust" in failed_record.stderr
    assert "trust directory is read-only" in " ".join(failed_record.stderr.split())
    assert launches == []
    assert record.read_bytes() == prior_record
    assert marker.read_text() == "v1\nv2\nv2\n"


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("headless", [False, True], ids=["interactive", "prompt"])
def test_host_session_passes_exact_confirmation_callback(tmp_path, monkeypatch, agent, headless):
    from types import SimpleNamespace

    from djinn_in_a_box.commands import session as command_module

    (tmp_path / ".djinn/sessions/testproj").mkdir(parents=True)
    monkeypatch.setattr(
        command_module, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )
    with (
        patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
        patch("djinn_in_a_box.commands.session.SessionManager") as manager,
        patch("djinn_in_a_box.commands.session.prepare_config_workflow") as prepare,
    ):
        instance = manager.return_value
        instance.resolve_target.return_value = SessionTarget()
        instance.preflight_check.return_value = None
        instance.run_headless.return_value = SessionResult(0)
        instance.run_interactive.return_value = SessionResult(0)
        prepare.return_value = WorkflowPreparationResult(True)
        arguments = ["session", "--project", "testproj", "--agent", agent]
        if headless:
            arguments += ["--prompt", "hello"]
        result = runner.invoke(app, arguments)
    assert result.exit_code == 0, (result.output, result.exception)
    prepare.assert_called_once()
    assert (
        prepare.call_args.kwargs["confirm_host_workflow"] is command_module._confirm_host_workflow
    )


def test_container_session_passes_no_confirmation_callback(tmp_path):
    workspace = tmp_path / ".djinn/sessions/testproj"
    workspace.mkdir(parents=True)
    with (
        patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
        patch("djinn_in_a_box.commands.session.SessionManager") as manager,
        patch("djinn_in_a_box.commands.session.prepare_config_workflow") as prepare,
    ):
        manager.return_value.resolve_target.return_value = SessionTarget("container-123")
        manager.return_value.workflow_image_compatible.return_value = (
            WorkflowImageCompatibility.COMPATIBLE
        )
        manager.return_value.run_headless.return_value = SessionResult(0)
        prepare.return_value = WorkflowPreparationResult(True)
        result = runner.invoke(app, ["session", "--project", "testproj", "--prompt", "hello"])
    assert result.exit_code == 0, result.output
    assert prepare.call_args.kwargs["confirm_host_workflow"] is None
