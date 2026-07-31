"""Non-UI headless agent execution for CLI and internal consumers."""

from __future__ import annotations

import shlex
from collections.abc import Callable

from djinn_in_a_box.config.loader import load_agents, load_config
from djinn_in_a_box.config.models import AgentConfig, AppConfig
from djinn_in_a_box.core.docker import (
    DJINN_NETWORK,
    ContainerMount,
    ContainerOptions,
    DockerMode,
    RunResult,
    cleanup_docker_proxy,
    compose_run,
    ensure_network,
    resolve_container_mounts,
)


class UnknownAgentError(ValueError):
    """Raised when a configured agent name cannot be resolved."""

    def __init__(self, agent: str, available: tuple[str, ...]) -> None:
        self.agent = agent
        self.available = available
        super().__init__(f"Unknown agent: {agent}")


class AgentNetworkError(RuntimeError):
    """Raised when the shared Docker network cannot be prepared."""

    def __init__(self, network: str = DJINN_NETWORK) -> None:
        self.network = network
        super().__init__(f"Failed to create Docker network '{network}'")


def build_agent_command(
    agent_config: AgentConfig,
    *,
    write: bool = False,
    json_output: bool = False,
    model: str | None = None,
) -> str:
    """Build a headless agent command that reads its prompt from the environment."""
    parts: list[str] = [shlex.quote(agent_config.binary)]
    parts.extend(shlex.quote(flag) for flag in agent_config.headless_flags)

    effective_model = model if model is not None else agent_config.default_model
    if effective_model:
        parts.extend([shlex.quote(agent_config.model_flag), shlex.quote(effective_model)])

    flags = agent_config.write_flags if write else agent_config.read_only_flags
    parts.extend(shlex.quote(flag) for flag in flags)
    if json_output:
        parts.extend(shlex.quote(flag) for flag in agent_config.json_flags)

    parts.append(agent_config.prompt_template)
    return " ".join(parts)


def run_headless_agent(
    agent: str,
    prompt: str,
    *,
    write: bool = False,
    json_output: bool = False,
    model: str | None = None,
    docker_mode: DockerMode = DockerMode.NONE,
    firewall: bool = False,
    mounts: tuple[str, ...] = (),
    resolved_mounts: tuple[ContainerMount, ...] | None = None,
    timeout: int | None = None,
    on_ready: Callable[[tuple[ContainerMount, ...]], None] | None = None,
    app_config: AppConfig | None = None,
) -> RunResult:
    """Run one configured agent without invoking workflow bootstrap or UI code."""
    checked_config = app_config if app_config is not None else load_config()
    agent_configs = load_agents()
    try:
        agent_config = agent_configs[agent]
    except KeyError:
        raise UnknownAgentError(agent, tuple(sorted(agent_configs))) from None

    container_mounts = (
        resolved_mounts
        if resolved_mounts is not None
        else resolve_container_mounts(mounts, here=not mounts)
    )
    if not ensure_network():
        raise AgentNetworkError

    if on_ready is not None:
        on_ready(container_mounts)

    agent_command = build_agent_command(
        agent_config,
        write=write,
        json_output=json_output,
        model=model,
    )
    options = ContainerOptions(
        docker_mode=docker_mode,
        firewall_enabled=firewall,
        mounts=container_mounts,
    )

    try:
        return compose_run(
            checked_config,
            options,
            command=agent_command,
            interactive=False,
            env={"AGENT_PROMPT": prompt},
            timeout=timeout,
        )
    finally:
        cleanup_docker_proxy(docker_mode, checked_config)
