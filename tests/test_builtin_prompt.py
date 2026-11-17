"""Built-in headless commands preserve prompts starting with a dash."""

import subprocess
from pathlib import Path

import pytest

from djinn_in_a_box.config.defaults import DEFAULT_AGENTS
from djinn_in_a_box.config.models import AgentConfig
from djinn_in_a_box.core.agent_runner import build_agent_command


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
def test_builtin_dash_prompt(
    agent: str, write: bool, json_output: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / agent
    fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("AGENT_PROMPT", "-x test prompt")
    command = build_agent_command(DEFAULT_AGENTS[agent], write=write, json_output=json_output)
    assert command.endswith('-- "$AGENT_PROMPT"')
    result = subprocess.run(
        ["/bin/bash", "-c", command], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines()[-2:] == ["--", "-x test prompt"]


def test_custom_agent_prompt_default() -> None:
    assert AgentConfig(binary="custom").prompt_template == '"$AGENT_PROMPT"'
