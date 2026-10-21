"""Frozen preset contract and its existing command/session consumers."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from itertools import product
from pathlib import Path
from unittest.mock import patch

import pytest

from djinn_in_a_box.config.defaults import CLAUDE_ISOLATION_FLAGS, DEFAULT_AGENTS
from djinn_in_a_box.core.agent_runner import build_agent_command
from djinn_in_a_box.core.session import SessionManager, SessionTarget

ISOLATION_FLAGS = [
    "--safe-mode",
    "--setting-sources=",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--tools=",
]
ISOLATED_DEFINITION = {
    "binary": "claude",
    "description": (
        "Claude Code without user or project context: "
        "no tools, MCP, hooks, settings or memory"
    ),
    "headless_flags": ["-p", "--no-session-persistence"],
    "read_only_flags": [
        "--safe-mode", "--setting-sources=", "--strict-mcp-config",
        "--disable-slash-commands", "--tools=",
    ],
    "write_flags": [
        "--safe-mode", "--setting-sources=", "--strict-mcp-config",
        "--disable-slash-commands", "--tools=",
    ],
    "json_flags": ["--output-format", "json"],
    "model_flag": "--model",
    "default_model": None,
    "prompt_template": '-- "$AGENT_PROMPT"',
}
HEADLESS_COMMAND = (
    "claude -p --no-session-persistence --safe-mode --setting-sources= "
    '--strict-mcp-config --disable-slash-commands --tools= -- "$AGENT_PROMPT"'
)


def test_existing_builtins_are_unchanged() -> None:
    existing = {name: DEFAULT_AGENTS[name].model_dump() for name in ("claude", "codex", "opencode")}
    assert existing == {
        "claude": {
            "binary": "claude",
            "description": "Anthropic Claude Code CLI",
            "headless_flags": ["-p"],
            "read_only_flags": ["--permission-mode", "plan"],
            "write_flags": ["--dangerously-skip-permissions"],
            "json_flags": ["--output-format", "json"],
            "model_flag": "--model",
            "default_model": None,
            "prompt_template": '"$AGENT_PROMPT"',
        },
        "codex": {
            "binary": "codex",
            "description": "OpenAI Codex CLI",
            "headless_flags": ["exec"],
            "read_only_flags": ["--sandbox", "read-only"],
            "write_flags": ["--full-auto"],
            "json_flags": ["--json"],
            "model_flag": "--model",
            "default_model": None,
            "prompt_template": '"$AGENT_PROMPT"',
        },
        "opencode": {
            "binary": "opencode",
            "description": "OpenCode CLI",
            "headless_flags": ["run"],
            "read_only_flags": ["--agent", "plan"],
            "write_flags": [],
            "json_flags": ["--format", "json"],
            "model_flag": "-m",
            "default_model": None,
            "prompt_template": '"$AGENT_PROMPT"',
        },
    }


def test_isolated_definition_matches_spec() -> None:
    assert list(DEFAULT_AGENTS) == ["claude", "codex", "opencode", "claude-isolated"]
    assert DEFAULT_AGENTS["claude-isolated"].model_dump() == ISOLATED_DEFINITION


def test_isolation_flag_lists_are_independent() -> None:
    preset = DEFAULT_AGENTS["claude-isolated"]
    assert CLAUDE_ISOLATION_FLAGS == ISOLATION_FLAGS
    assert preset.read_only_flags is not preset.write_flags
    assert preset.read_only_flags is not CLAUDE_ISOLATION_FLAGS
    assert preset.write_flags is not CLAUDE_ISOLATION_FLAGS


def test_commented_example_matches_spec() -> None:
    example = Path(__file__).parents[1] / "templates/seed/config/agents.toml.example"
    section = example.read_text().split("# [agents.claude-isolated]\n", 1)[1]
    fields = tomllib.loads("\n".join(line.removeprefix("# ") for line in section.splitlines()))
    assert fields == {key: value for key, value in ISOLATED_DEFINITION.items() if value is not None}


@pytest.mark.parametrize(
    "write,json_output,model", product((False, True), (False, True), (None, "M")),
)
def test_headless_command_is_exact(write: bool, json_output: bool, model: str | None) -> None:
    expected = "claude -p --no-session-persistence "
    if model is not None:
        expected += "--model M "
    expected += (
        "--safe-mode --setting-sources= --strict-mcp-config --disable-slash-commands --tools= "
    )
    if json_output:
        expected += "--output-format json "
    expected += '-- "$AGENT_PROMPT"'
    command = build_agent_command(
        DEFAULT_AGENTS["claude-isolated"], write=write, json_output=json_output, model=model,
    )
    assert command == expected


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize(
    "prompt", ["- list item", "", 'quoted " $(echo x)\n$(touch "$PROMPT_MARKER")'],
)
def test_prompt_reaches_shell_as_one_literal_argument(
    shell: str, prompt: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\nprintf \'%s\\n\' "$#" "$@"\n')
    fake.chmod(0o755)
    marker = tmp_path / "embedded-command-ran"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENT_PROMPT", prompt)
    monkeypatch.setenv("PROMPT_MARKER", str(marker))
    command = build_agent_command(DEFAULT_AGENTS["claude-isolated"])
    result = subprocess.run(
        [executable, "-c", command], capture_output=True, text=True, check=False,
    )
    expected_argv = ["-p", "--no-session-persistence", *ISOLATION_FLAGS, "--", prompt]
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == "\n".join([str(len(expected_argv)), *expected_argv]) + "\n"
    assert not marker.exists()


@pytest.fixture
def session_manager(monkeypatch: pytest.MonkeyPatch) -> SessionManager:
    monkeypatch.setattr("djinn_in_a_box.core.session.load_agents", lambda: dict(DEFAULT_AGENTS))
    return SessionManager("isolated-test")


@pytest.mark.parametrize("model", [None, "M"])
@pytest.mark.parametrize("prompt", [None, "initial prompt"])
def test_interactive_session_keeps_isolation_without_headless_flags(
    session_manager: SessionManager, model: str | None, prompt: str | None,
) -> None:
    command = session_manager._build_interactive_command(
        DEFAULT_AGENTS["claude-isolated"], model, prompt,
    )
    expected = "claude " + ("--model M " if model is not None else "")
    expected += (
        "--safe-mode --setting-sources= --strict-mcp-config --disable-slash-commands --tools="
    )
    if prompt is not None:
        expected += " 'initial prompt'"
    assert command == expected
    assert "-p" not in command.split()
    assert "--no-session-persistence" not in command.split()


def test_container_headless_session_uses_shared_builder(
    session_manager: SessionManager, tmp_path: Path,
) -> None:
    with (
        patch(
            "djinn_in_a_box.commands.agent.build_agent_command", wraps=build_agent_command,
        ) as build,
        patch(
            "djinn_in_a_box.core.session.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "agent output", ""),
        ) as run,
    ):
        result = session_manager.run_headless(
            workspace_dir=tmp_path, agent="claude-isolated", prompt="prompt",
            target=SessionTarget("container"),
        )
    build.assert_called_once_with(DEFAULT_AGENTS["claude-isolated"], model=None)
    run.assert_called_once()
    argv = run.call_args.args[0]
    assert argv[-3:-1] == ["bash", "-c"]
    assert argv[-1].split("; ", 1)[1] == HEADLESS_COMMAND
    assert "AGENT_PROMPT=prompt" in argv
    assert result.returncode == 0
    assert result.stdout == "agent output"


@pytest.mark.parametrize("mode", ["interactive", "headless"])
def test_host_fallback_refuses_isolated_without_subprocesses(
    session_manager: SessionManager, tmp_path: Path, mode: str,
) -> None:
    with (
        patch.object(
            session_manager, "_guard_host_workspace", wraps=session_manager._guard_host_workspace,
        ) as guard,
        patch("djinn_in_a_box.core.session.subprocess.run") as run,
    ):
        if mode == "headless":
            result = session_manager.run_headless(
                workspace_dir=tmp_path, agent="claude-isolated", prompt="prompt",
                target=SessionTarget(),
            )
        else:
            result = session_manager.run_interactive(
                workspace_dir=tmp_path, agent="claude-isolated", target=SessionTarget(),
            )
    guard.assert_called_once_with(tmp_path, "claude-isolated")
    run.assert_not_called()
    assert result.returncode == 1
    assert "unsupported host agent 'claude-isolated'" in result.stderr
    assert result.stdout == ""
    assert result.workspace_dir == tmp_path
