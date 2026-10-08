"""Public CLI/config contracts for assistant sessions."""

import json

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import assistant, config
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig, AssistantConfig
from djinn_in_a_box.core.assistant import AssistantError

runner = CliRunner()


def test_audit_cli_prompt_override_and_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(assistant, "run_audit", lambda *args: calls.append(args) or 17)
    occasion = 'first\n"$quotes" `literal`'
    result = runner.invoke(app, ["audit", occasion, "--agent", "codex"])
    assert result.exit_code == 17
    assert calls == [(occasion, "codex")]


def test_audit_cli_default_and_error(monkeypatch):
    def run(occasion, selected):
        assert occasion is None and selected is None
        raise AssistantError("fixture daemon failure")

    monkeypatch.setattr(assistant, "run_audit", run)
    result = runner.invoke(app, ["audit"])
    assert result.exit_code == 1 and "fixture daemon failure" in result.output


def test_old_audit_tail_and_invalid_agent_are_usage_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(assistant, "run_audit", lambda *args: calls.append(args) or 0)
    for args in (["audit", "--tail", "12"], ["audit", "--agent", "other"]):
        assert runner.invoke(app, args).exit_code == 2
    assert calls == []




@pytest.mark.parametrize("agent", ("claude", "codex", "opencode"))
def test_assistant_config_roundtrip_and_public_show(tmp_path, monkeypatch, agent):
    path = tmp_path / "config.toml"
    initial = AppConfig(code_dir=tmp_path, environment={"LITERAL": "$literal"})
    save_config(initial, path)
    monkeypatch.setattr(config, "load_config", lambda: load_config(path))
    monkeypatch.setattr(config, "save_config", lambda value: save_config(value, path))
    monkeypatch.setattr(
        config, "config_directory_lock", lambda **kw: __import__("contextlib").nullcontext()
    )
    result = runner.invoke(app, ["config", "set", "assistant.agent", agent])
    assert result.exit_code == 0, result.output
    loaded = load_config(path)
    assert loaded.assistant.agent == agent and loaded.environment == {"LITERAL": "$literal"}
    assert "[assistant]" in path.read_text()
    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)["assistant"] == {"agent": agent}
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0 and "Assistant" in result.output and agent in result.output
    preserved = config._set_config_value(loaded, "general.timezone", "Europe/Berlin")
    assert preserved.assistant == loaded.assistant
    expected = loaded.model_dump()
    expected["timezone"] = "Europe/Berlin"
    assert preserved.model_dump() == expected


def test_assistant_config_forbids_unknown_and_frozen(tmp_path):
    assert AppConfig(code_dir=tmp_path).assistant.agent == "claude"
    for data in ({"agent": "other"}, {"agent": "codex", "extra": True}):
        with pytest.raises(ValidationError):
            AssistantConfig.model_validate(data)
    model = AssistantConfig()
    with pytest.raises(ValidationError):
        model.agent = "codex"
