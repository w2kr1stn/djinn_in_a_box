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
from djinn_in_a_box.core.docker import ContainerMount, DockerMode, RunResult


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
    ready: list[tuple[ContainerMount, ...]] = []

    result = run_headless_agent(
        "codex",
        "inspect this",
        json_output=True,
        docker_mode=DockerMode.PROXY,
        firewall=True,
        resolved_mounts=(
            ContainerMount(tmp_path, Path(f"/home/dev/mount/{tmp_path.name}")),
        ),
        timeout=120,
        on_ready=ready.append,
    )

    assert result is runner_mocks["result"]
    assert ready == [
        (ContainerMount(tmp_path, Path(f"/home/dev/mount/{tmp_path.name}")),)
    ]
    compose = runner_mocks["compose"]
    compose.assert_called_once()
    assert compose.call_args.args[0] is runner_mocks["app_config"]
    options = compose.call_args.args[1]
    assert options.docker_mode is DockerMode.PROXY
    assert options.firewall_enabled is True
    assert options.mounts == (
        ContainerMount(tmp_path, Path(f"/home/dev/mount/{tmp_path.name}")),
    )
    assert "--model configured-model" in compose.call_args.kwargs["command"]
    assert "--sandbox read-only" in compose.call_args.kwargs["command"]
    assert "--json" in compose.call_args.kwargs["command"]
    assert compose.call_args.kwargs["env"] == {"AGENT_PROMPT": "inspect this"}
    assert compose.call_args.kwargs["interactive"] is False
    assert compose.call_args.kwargs["timeout"] == 120
    runner_mocks["cleanup"].assert_called_once_with(DockerMode.PROXY, runner_mocks["app_config"])


def test_run_headless_agent_requires_resolved_mounts(
    runner_mocks: dict[str, Any],
) -> None:
    with pytest.raises(TypeError, match="resolved_mounts"):
        run_headless_agent("codex", "inspect")  # type: ignore[call-arg]


def test_run_headless_agent_uses_supplied_mount_collection_unchanged(
    tmp_path: Path,
    runner_mocks: dict[str, Any],
) -> None:
    # The CLI-level implicit --here contract is guarded by
    # test_run_without_mount_resolves_implicit_here_at_cli_boundary. This
    # lower-level test guards that the runner uses the resolved collection as-is.
    resolved = (ContainerMount(tmp_path, Path("/home/dev/workspace")),)

    run_headless_agent("codex", "inspect", resolved_mounts=resolved)

    options = runner_mocks["compose"].call_args.args[1]
    assert options.mounts is resolved


def test_run_headless_agent_on_ready_receives_all_resolved_mounts(
    tmp_path: Path,
    runner_mocks: dict[str, Any],
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    ready: list[tuple[ContainerMount, ...]] = []

    run_headless_agent(
        "codex",
        "inspect",
        resolved_mounts=(
            ContainerMount(first, Path("/home/dev/mount/first")),
            ContainerMount(second, Path("/home/dev/mount/second")),
        ),
        on_ready=ready.append,
    )

    expected = (
        ContainerMount(first, Path("/home/dev/mount/first")),
        ContainerMount(second, Path("/home/dev/mount/second")),
    )
    assert ready == [expected]


def test_checked_config_snapshot_is_used_without_reload(
    tmp_path: Path,
    runner_mocks: dict[str, Any],
) -> None:
    checked = runner_mocks["app_config"]
    changed = AppConfig(code_dir=tmp_path, config_root=tmp_path / "changed-root")
    runner_mocks["load_config"].reset_mock()
    runner_mocks["load_config"].return_value = changed

    run_headless_agent(
        "codex",
        "inspect",
        resolved_mounts=(),
        app_config=checked,
    )

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
        run_headless_agent("missing", "inspect", resolved_mounts=())

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
        run_headless_agent("codex", "inspect", resolved_mounts=())

    compose.assert_not_called()
    cleanup.assert_not_called()


def test_run_headless_agent_cleans_proxy_after_execution_error(
    runner_mocks: dict[str, Any],
) -> None:
    runner_mocks["compose"].side_effect = RuntimeError("compose failed")

    with pytest.raises(RuntimeError, match="compose failed"):
        run_headless_agent(
            "codex",
            "inspect",
            docker_mode=DockerMode.PROXY,
            resolved_mounts=(),
        )

    runner_mocks["cleanup"].assert_called_once_with(DockerMode.PROXY, runner_mocks["app_config"])
