from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from djinn_in_a_box.config.models import AgentConfig, AppConfig
from djinn_in_a_box.core.agent_runner import (
    AgentNetworkError,
    UnknownAgentError,
    run_headless_agent,
)
from djinn_in_a_box.core.docker import DockerMode, RunResult


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(code_dir=tmp_path)


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(
        binary="codex",
        headless_flags=["exec"],
        read_only_flags=["--sandbox", "read-only"],
        write_flags=["--full-auto"],
        json_flags=["--json"],
        default_model="configured-model",
    )


@pytest.fixture
def runner_mocks(
    app_config: AppConfig,
    agent_config: AgentConfig,
) -> Generator[dict[str, Any]]:
    with (
        patch(
            "djinn_in_a_box.core.agent_runner.load_config", return_value=app_config
        ) as load_config,
        patch(
            "djinn_in_a_box.core.agent_runner.load_agents",
            return_value={"codex": agent_config},
        ),
        patch("djinn_in_a_box.core.agent_runner.ensure_network", return_value=True) as network,
        patch("djinn_in_a_box.core.agent_runner.compose_run") as compose,
        patch("djinn_in_a_box.core.agent_runner.cleanup_docker_proxy") as cleanup,
    ):
        result = RunResult(returncode=0, stdout="agent output", stderr="")
        compose.return_value = result
        yield {
            "app_config": app_config,
            "cleanup": cleanup,
            "compose": compose,
            "network": network,
            "result": result,
            "load_config": load_config,
        }


def test_run_headless_agent_builds_and_executes_typed_request(
    tmp_path: Path,
    runner_mocks: dict[str, Any],
) -> None:
    ready: list[Path] = []

    result = run_headless_agent(
        "codex",
        "inspect this",
        json_output=True,
        docker_mode=DockerMode.PROXY,
        firewall=True,
        mount=tmp_path,
        timeout=120,
        on_ready=ready.append,
    )

    assert result is runner_mocks["result"]
    assert ready == [tmp_path]
    compose = runner_mocks["compose"]
    compose.assert_called_once()
    assert compose.call_args.args[0] is runner_mocks["app_config"]
    options = compose.call_args.args[1]
    assert options.docker_mode is DockerMode.PROXY
    assert options.firewall_enabled is True
    assert options.mount_path == tmp_path
    assert "--model configured-model" in compose.call_args.kwargs["command"]
    assert "--sandbox read-only" in compose.call_args.kwargs["command"]
    assert "--json" in compose.call_args.kwargs["command"]
    assert compose.call_args.kwargs["env"] == {"AGENT_PROMPT": "inspect this"}
    assert compose.call_args.kwargs["interactive"] is False
    assert compose.call_args.kwargs["timeout"] == 120
    runner_mocks["cleanup"].assert_called_once_with(DockerMode.PROXY, runner_mocks["app_config"])


def test_run_headless_agent_uses_current_directory_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_mocks: dict[str, Any],
) -> None:
    monkeypatch.chdir(tmp_path)

    run_headless_agent("codex", "inspect")

    options = runner_mocks["compose"].call_args.args[1]
    assert options.mount_path == tmp_path


def test_checked_config_snapshot_is_used_without_reload(
    tmp_path: Path,
    runner_mocks: dict[str, Any],
) -> None:
    checked = runner_mocks["app_config"]
    changed = AppConfig(code_dir=tmp_path, config_root=tmp_path / "changed-root")
    runner_mocks["load_config"].reset_mock()
    runner_mocks["load_config"].return_value = changed

    run_headless_agent("codex", "inspect", app_config=checked)

    runner_mocks["load_config"].assert_not_called()
    assert runner_mocks["compose"].call_args.args[0] is checked
    runner_mocks["cleanup"].assert_called_once_with(DockerMode.NONE, checked)


def test_run_headless_agent_rejects_unknown_agent_before_network(
    app_config: AppConfig,
) -> None:
    with (
        patch("djinn_in_a_box.core.agent_runner.load_config", return_value=app_config),
        patch(
            "djinn_in_a_box.core.agent_runner.load_agents",
            return_value={"codex": AgentConfig(binary="codex")},
        ),
        patch("djinn_in_a_box.core.agent_runner.ensure_network") as network,
        pytest.raises(UnknownAgentError) as exc_info,
    ):
        run_headless_agent("missing", "inspect")

    assert exc_info.value.available == ("codex",)
    network.assert_not_called()


def test_run_headless_agent_reports_network_failure_without_container(
    app_config: AppConfig,
    agent_config: AgentConfig,
) -> None:
    with (
        patch("djinn_in_a_box.core.agent_runner.load_config", return_value=app_config),
        patch(
            "djinn_in_a_box.core.agent_runner.load_agents",
            return_value={"codex": agent_config},
        ),
        patch("djinn_in_a_box.core.agent_runner.ensure_network", return_value=False),
        patch("djinn_in_a_box.core.agent_runner.compose_run") as compose,
        patch("djinn_in_a_box.core.agent_runner.cleanup_docker_proxy") as cleanup,
        pytest.raises(AgentNetworkError),
    ):
        run_headless_agent("codex", "inspect")

    compose.assert_not_called()
    cleanup.assert_not_called()


def test_run_headless_agent_cleans_proxy_after_execution_error(
    runner_mocks: dict[str, Any],
) -> None:
    runner_mocks["compose"].side_effect = RuntimeError("compose failed")

    with pytest.raises(RuntimeError, match="compose failed"):
        run_headless_agent("codex", "inspect", docker_mode=DockerMode.PROXY)

    runner_mocks["cleanup"].assert_called_once_with(DockerMode.PROXY, runner_mocks["app_config"])
