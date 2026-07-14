"""Agent execution commands — run agents in headless mode within Docker containers."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from djinn_in_a_box.config.loader import load_agents, load_config
from djinn_in_a_box.core.console import console, err_console, error, info, rule, status_line
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.docker import (
    DJINN_NETWORK,
    ContainerOptions,
    cleanup_docker_proxy,
    compose_run,
    ensure_network,
    resolve_docker_mode,
)

if TYPE_CHECKING:
    from djinn_in_a_box.config.models import AgentConfig


def _agent_table(title: str) -> Table:
    return Table(
        title=title,
        title_style="table.title",
        header_style="table.header",
        border_style="border",
    )


def build_agent_command(
    agent_config: AgentConfig,
    *,
    write: bool = False,
    json_output: bool = False,
    model: str | None = None,
) -> str:
    """Build shell command string for agent execution.

    The prompt is referenced via $AGENT_PROMPT env var, expanded at container runtime.
    """
    parts: list[str] = [shlex.quote(agent_config.binary)]
    parts.extend(shlex.quote(f) for f in agent_config.headless_flags)

    effective_model = model if model is not None else agent_config.default_model
    if effective_model:
        parts.extend(
            [shlex.quote(agent_config.model_flag), shlex.quote(effective_model)]
        )

    if write:
        parts.extend(shlex.quote(f) for f in agent_config.write_flags)
    else:
        parts.extend(shlex.quote(f) for f in agent_config.read_only_flags)

    if json_output:
        parts.extend(shlex.quote(f) for f in agent_config.json_flags)

    # Append prompt template (uses $AGENT_PROMPT env var expanded at runtime)
    parts.append(agent_config.prompt_template)

    return " ".join(parts)


@handle_config_errors
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
    mount: Annotated[
        Path | None,
        typer.Option(
            "--mount",
            help="Workspace path to mount (default: current directory)",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
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
    in the container (implicit --here behavior). Use --mount to specify
    a different directory.

    Examples:

        # Simple read-only query
        djinn run claude "Explain this code"

        # Allow file modifications
        djinn run claude "Fix the bug in main.py" --write

        # Use a specific model with JSON output
        djinn run gemini "Refactor this file" --write --model gemini-2.5-flash --json

        # With Docker access and timeout
        djinn run claude "Build the Docker image" --docker --timeout 300
    """
    try:
        docker_mode = resolve_docker_mode(docker, docker_direct)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from None

    app_config = load_config()
    agent_configs = load_agents()

    if agent not in agent_configs:
        error(f"Unknown agent: {agent}")
        available = ", ".join(sorted(agent_configs.keys()))
        console.print(f"Available agents: {available}")
        raise typer.Exit(1)

    agent_config = agent_configs[agent]

    if not ensure_network():
        error(f"Failed to create Docker network '{DJINN_NETWORK}'")
        raise typer.Exit(1)

    # Determine workspace path (implicit --here: default to cwd)
    workspace = mount if mount else Path.cwd()

    # Print status to stderr (matching dev.sh format)
    err_console.print()
    info(f"Running {agent} (headless)...")
    err_console.print()

    status_line("Agent", agent)
    status_line("Workspace", str(workspace))

    if model:
        status_line("Model", model)

    if write:
        status_line("Mode", "Read/Write (--write)", "status.disabled")
    else:
        status_line("Mode", "Read-only (plan/analysis)", "status.enabled")

    if docker:
        status_line("Docker", "Enabled (proxy)")
    elif docker_direct:
        status_line("Docker", "Enabled (DIRECT)", "warning")
    if firewall:
        status_line("Firewall", "Enabled")
    if json_output:
        status_line("Output", "JSON")
    if timeout:
        status_line("Timeout", f"{timeout}s")

    err_console.print()

    agent_cmd = build_agent_command(
        agent_config,
        write=write,
        json_output=json_output,
        model=model,
    )

    options = ContainerOptions(
        docker_mode=docker_mode,
        firewall_enabled=firewall,
        mount_path=workspace,
    )

    try:
        result = compose_run(
            app_config,
            options,
            command=agent_cmd,
            interactive=False,
            env={"AGENT_PROMPT": prompt},
            timeout=timeout,
        )
    finally:
        cleanup_docker_proxy(docker_mode, app_config)

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
