"""Agent execution commands — run agents in headless mode within Docker containers."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Annotated, ParamSpec, TypeIs, TypeVar, cast

import typer
from rich.table import Table

from djinn_in_a_box.commands.zone_gate import zone_command_gate
from djinn_in_a_box.config.loader import load_agents, load_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncSource
from djinn_in_a_box.core.agent_runner import (
    AgentNetworkError,
    UnknownAgentError,
    run_headless_agent,
)
from djinn_in_a_box.core.agent_runner import build_agent_command as build_agent_command
from djinn_in_a_box.core.config_workflow import (
    WorkflowDeliveryTarget,
    prepare_config_workflow,
)
from djinn_in_a_box.core.console import (
    console,
    err_console,
    error,
    info,
    rule,
    status_line,
    warning,
)
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.docker import (
    ContainerMount,
    DockerMode,
    MountCollisionError,
    get_config_root,
    resolve_container_mounts,
    resolve_docker_mode,
    workflow_image_compatible,
)
from djinn_in_a_box.core.exceptions import (
    MountSpecificationError,
    RuntimeMountSpecificationError,
)
from djinn_in_a_box.core.paths import get_project_root

P = ParamSpec("P")
R = TypeVar("R")
_NO_GATED_CONFIG = object()
_gated_config: ContextVar[object] = ContextVar("gated_config", default=_NO_GATED_CONFIG)


def _is_app_config(value: object) -> TypeIs[AppConfig]:
    return isinstance(value, AppConfig)


def _active_config() -> AppConfig:
    config = _gated_config.get()
    if config is _NO_GATED_CONFIG:
        return load_config()
    return cast(AppConfig, config)


def _zone_gated_run(func: Callable[P, R]) -> Callable[P, R]:
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        candidate: object = load_config()
        token = _gated_config.set(candidate)
        try:
            if not _is_app_config(candidate):
                return func(*args, **kwargs)
            with zone_command_gate(candidate, "run"):
                return func(*args, **kwargs)
        finally:
            _gated_config.reset(token)

    return wrapper


def _agent_table(title: str) -> Table:
    return Table(
        title=title,
        title_style="table.title",
        header_style="table.header",
        border_style="border",
    )


def _show_run_status(
    agent: str,
    mounts: tuple[ContainerMount, ...],
    *,
    write: bool,
    json_output: bool,
    model: str | None = None,
    docker_mode: DockerMode,
    firewall: bool,
    timeout: int | None,
) -> None:
    """Render CLI-only execution status after runner prerequisites pass."""
    err_console.print()
    info(f"Running {agent} (headless)...")
    err_console.print()

    status_line("Agent", agent)
    for mount in mounts:
        mode = "ro" if mount.read_only else "rw"
        status_line(
            "Mount",
            f"{mount.source} -> {mount.target} ({mode})",
            value_style="path",
        )

    workspace = next(
        (mount.source for mount in mounts if mount.target == Path("/home/dev/workspace")),
        None,
    )
    if workspace is not None:
        status_line("Workspace", str(workspace), value_style="path")

    if model:
        status_line("Model", model)

    if write:
        status_line("Mode", "Read/Write (--write)", "status.disabled")
    else:
        status_line("Mode", "Read-only (plan/analysis)", "status.enabled")

    if docker_mode is DockerMode.PROXY:
        status_line("Docker", "Enabled (proxy)")
    elif docker_mode is DockerMode.DIRECT:
        status_line("Docker", "Enabled (DIRECT)", "warning")
    if firewall:
        status_line("Firewall", "Enabled")
    if json_output:
        status_line("Output", "JSON")
    if timeout:
        status_line("Timeout", f"{timeout}s")

    err_console.print()


@handle_config_errors
@_zone_gated_run
def run(
    agent: Annotated[
        str,
        typer.Argument(help="Agent to run: claude, gemini, codex, opencode"),
    ],
    prompt: Annotated[
        str,
        typer.Argument(help="Prompt to send to the agent"),
    ],
    write: Annotated[
        bool,
        typer.Option("--write", "-w", help="Allow file modifications"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="JSON output format"),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Model override (e.g., sonnet, gemini-2.5-flash)"),
    ] = None,
    docker: Annotated[
        bool,
        typer.Option("--docker", "-d", help="Enable Docker socket access via proxy"),
    ] = False,
    docker_direct: Annotated[
        bool,
        typer.Option("--docker-direct", help="Enable direct Docker socket access (no proxy)"),
    ] = False,
    firewall: Annotated[
        bool,
        typer.Option("--firewall", "-f", help="Enable network firewall"),
    ] = False,
    here: Annotated[
        bool,
        typer.Option("--here", help="Mount current directory as ~/workspace"),
    ] = False,
    mount: Annotated[
        list[str] | None,
        typer.Option(
            "--mount",
            help="Host directory to mount; repeatable: SRC[:DST[:ro|rw]]",
        ),
    ] = None,
    timeout: Annotated[
        int | None,
        typer.Option("--timeout", "-t", help="Timeout in seconds"),
    ] = None,
) -> None:
    """Run an agent in headless mode (non-interactive).

    The agent runs in an ephemeral Docker container, processes the prompt,
    outputs the result to stdout, and exits. Status information is written
    to stderr to keep stdout clean for agent output.

    By default, the current working directory is mounted as ~/workspace
    in the container (implicit --here behavior). Use --here to request that
    workspace explicitly alongside repeatable --mount values.

    Examples:

        # Simple read-only query
        djinn run claude "Explain this code"

        # Allow file modifications
        djinn run claude "Fix the bug in main.py" --write

        # Use a specific model with JSON output
        djinn run gemini "Refactor this file" --write --model gemini-2.5-flash --json

        # With Docker access and timeout
        djinn run claude "Build the Docker image" --docker --timeout 300

        # Mount the current directory and two additional directories
        djinn run claude "Compare these projects" --here \\
            --mount ~/other-project --mount ~/reference:/home/dev/reference:ro
    """
    try:
        docker_mode = resolve_docker_mode(docker, docker_direct)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from None

    checked_config = None
    delivery_targets: tuple[WorkflowDeliveryTarget, ...] = ()
    config = _active_config()
    checked_config = config
    if agent in {"claude", "codex"}:
        selected_agent = cast(ConfigSyncSource, agent)
        delivery_targets = (
            WorkflowDeliveryTarget(selected_agent, get_config_root(config) / selected_agent),
        )
    workflow = prepare_config_workflow(
        get_project_root(),
        delivery_targets,
        config_snapshot=config,
        require_compose_host_env=True,
        container_image_compatibility=workflow_image_compatible(),
    )
    if not workflow.success:
        problem = workflow.problems[0]
        error(problem.message)
        warning(problem.remedy)
        raise typer.Exit(1)

    try:
        resolved_mounts = resolve_container_mounts(
            tuple(mount or ()), here=here or not mount
        )
    except (MountSpecificationError, FileNotFoundError, NotADirectoryError) as e:
        error(str(e))
        raise typer.Exit(1) from None

    try:
        result = run_headless_agent(
            agent,
            prompt,
            write=write,
            json_output=json_output,
            model=model,
            docker_mode=docker_mode,
            firewall=firewall,
            resolved_mounts=resolved_mounts,
            timeout=timeout,
            app_config=checked_config,
            on_ready=lambda mounts: _show_run_status(
                agent,
                mounts,
                write=write,
                json_output=json_output,
                model=model,
                docker_mode=docker_mode,
                firewall=firewall,
                timeout=timeout,
            ),
        )
    except UnknownAgentError as e:
        error(str(e))
        console.print(f"Available agents: {', '.join(e.available)}")
        raise typer.Exit(1) from None
    except AgentNetworkError as e:
        error(str(e))
        raise typer.Exit(1) from None
    except RuntimeMountSpecificationError as e:
        error(f"Internal runtime mount construction failed: {e}")
        raise typer.Exit(1) from None
    except (MountCollisionError, MountSpecificationError) as e:
        error(str(e))
        raise typer.Exit(1) from None

    if result.stdout:
        console.print(result.stdout, end="")

    if result.stderr:
        err_console.print(result.stderr, end="")

    raise typer.Exit(result.returncode)


@handle_config_errors
def agents(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed configuration"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """List available agents.

    Shows all configured agents with their descriptions. Use --verbose
    for detailed configuration information or --json for machine-readable output.

    Examples:

        # Simple list
        djinn agents

        # Detailed view
        djinn agents --verbose

        # JSON output for scripting
        djinn agents --json
    """
    agent_configs = load_agents()

    if json_output:
        data = {
            name: cfg.model_dump(exclude={"prompt_template"})
            for name, cfg in sorted(agent_configs.items())
        }
        console.print(json.dumps(data, indent=2))
        return

    if verbose:
        for name, cfg in sorted(agent_configs.items()):
            table = _agent_table(name)
            table.add_column("Setting", style="table.category")
            table.add_column("Value", style="table.value")
            table.add_row("Description", cfg.description or cfg.binary)
            table.add_row("Binary", cfg.binary)
            table.add_row("Model flag", cfg.model_flag)
            if cfg.default_model:
                table.add_row("Default model", cfg.default_model)
            if cfg.headless_flags:
                table.add_row("Headless", " ".join(cfg.headless_flags))
            if cfg.write_flags:
                table.add_row("Write mode", " ".join(cfg.write_flags))
            if cfg.read_only_flags:
                table.add_row("Read-only", " ".join(cfg.read_only_flags))
            if cfg.json_flags:
                table.add_row("JSON flags", " ".join(cfg.json_flags))
            console.print(table)
            console.print()
    else:
        rule("Available Agents")
        console.print()
        table = _agent_table("Available Agents")
        table.add_column("Agent", style="table.category")
        table.add_column("Description", style="table.value")
        for name, cfg in sorted(agent_configs.items()):
            desc = cfg.description or cfg.binary
            table.add_row(name, desc)
        console.print(table)
