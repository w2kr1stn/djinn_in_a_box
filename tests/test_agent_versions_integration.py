"""Join real discovery, persistence and Git cleanliness to both image builders."""

import subprocess
from unittest.mock import MagicMock

import pytest
from test_core.test_assistant import Docker
from test_update_agents import PINS, ROOT, snapshot
from test_update_agents import script_env as script_env
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import container
from djinn_in_a_box.core import agent_versions, assistant, docker, paths


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode"])
def test_update_clean_checkout_and_versions_reach_both_builders(
    request, tmp_path, mock_app_config, monkeypatch, agent,
):
    project, env, calls = request.getfixturevalue("script_env")
    context = project / "assistant"
    context.mkdir()
    for name in ("Dockerfile", "entrypoint.sh"):
        (context / name).write_bytes((ROOT / "assistant" / name).read_bytes())
    with (project / "Dockerfile").open("a") as stream:
        stream.write("ARG DOCKER_VERSION=27.4.1\n")
    for name in ("docker-compose.yml", "docker-compose.desktop.yml"):
        (project / name).write_bytes((ROOT / name).read_bytes())
    for argv in (["git", "init", "-q"], ["git", "add", "."],
                 ["git", "-c", "user.name=Test Operator", "-c", "user.email=test@example.com",
                  "-c", "core.hooksPath=/dev/null", "commit", "-qm", "baseline"]):
        subprocess.run(argv, cwd=project, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=10, check=True)

    def git_status():
        return subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=project,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=True,
        ).stdout

    assert git_status() == ""
    before = snapshot(project)
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    for key in ("PATH", "npm_calls", "TERM", "NO_COLOR"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setattr(container, "get_project_root", lambda: project)
    monkeypatch.setattr(docker, "get_project_root", lambda: project)
    monkeypatch.setattr(assistant, "get_project_root", lambda: project)
    assert not paths.AGENT_VERSIONS_FILE.is_relative_to(project)
    runner = CliRunner()
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert agent_versions.load_versions() == PINS
    assert git_status() == ""
    assert snapshot(project) == before
    assert len(calls.read_text().splitlines()) == 3
    assert set(calls.read_text().splitlines()) == {
        "view @anthropic-ai/claude-code version", "view @openai/codex version",
        "view opencode-ai version",
    }
    monkeypatch.setattr(container, "load_config", lambda: mock_app_config)
    for name in ("preflight", "_sync_build_files", "_pull_agent_docker_image_if_missing"):
        monkeypatch.setattr(container, name, MagicMock())
    monkeypatch.setattr(container.hostctl, "build_supervisor",
                        lambda **kwargs: docker.RunResult(returncode=0))
    monkeypatch.setattr(container.hostctl, "install_supervisor", MagicMock())
    monkeypatch.delenv("DJINN_BUILD_PROGRESS", raising=False)
    fake = Docker()
    original_run = subprocess.run

    def run(argv, **kwargs):
        if argv[0] == docker.DOCKER_EXECUTABLE:
            if argv[1:3] == ["buildx", "bake"]:
                fake.calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0)
            return fake.run(argv, **kwargs)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 0, result.output
    bake, kwargs = fake.calls[0]
    assert kwargs["cwd"] == project
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert bake[bake.index("--set"):] == [
        "--set", "dev.args.CLAUDE_CODE_VERSION=2.1.300",
        "--set", "dev.args.CODEX_VERSION=0.161.0",
        "--set", "dev.args.OPENCODE_VERSION=1.18.40", "dev", "dbus-helper",
    ]
    image = assistant.ensure_image(project, agent, "linux/amd64", "default")
    build = next(argv for argv, _ in fake.calls if argv[1:3] == ["buildx", "build"])
    selected_arg = assistant.PIN_NAMES[agent]
    assert f"AGENT_VERSION={PINS[selected_arg]}" in build
    assert "DOCKER_VERSION=27.4.1" in build
    fingerprint = assistant.label(fake.image, assistant.CONTENT_LABEL)
    assert assistant.ensure_image(project, agent, "linux/amd64", "default") == image
    assert len([argv for argv, _ in fake.calls if argv[1:3] == ["buildx", "build"]]) == 1
    paths.AGENT_VERSIONS_FILE.unlink()
    assert assistant.ensure_image(project, agent, "linux/amd64", "default") != image
    assert assistant.label(fake.image, assistant.CONTENT_LABEL) != fingerprint
    assert snapshot(project) == before
    assert git_status() == ""
    assert list(caller.iterdir()) == []
