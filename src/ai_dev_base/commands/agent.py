"""Agent execution commands for AI Dev Base CLI.

This module provides commands for executing AI coding agents in headless mode
within Docker containers. The agents (Claude, Gemini, Codex, OpenCode) run
in ephemeral containers, process a prompt, and return the result.

Commands:
    run: Execute an agent in headless mode with a prompt
    agents: List available agents and their configurations

The prompt is passed via the AGENT_PROMPT environment variable to avoid
shell escaping issues. By default, the current working directory is mounted
as the workspace (implicit --here behavior).
"""

from __future__ import annotations

import json
import shlex
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ai_dev_base.config import load_agents, load_config
from ai_dev_base.core.console import console, err_console, error, info, status_line
from ai_dev_base.core.decorators import handle_config_errors
from ai_dev_base.core.docker import (
    ContainerOptions,
    cleanup_docker_proxy,
    ensure_network,
    network_exists,
)

if TYPE_CHECKING:
    from ai_dev_base.config.models import AgentConfig

# =============================================================================
# Command Building
# =============================================================================


def build_agent_command(
    agent_config: AgentConfig,
    *,
    write: bool = False,
    json_output: bool = False,
    model: str | None = None,
) -> str:
    """Build shell command string for agent execution.

    Constructs the command that will be executed inside the container.
    The prompt is referenced via the $AGENT_PROMPT environment variable,
    which is expanded at runtime in the container shell.

    Mirrors the build_shell_command() function from the original agent_runner.py script.

    Args:
        agent_config: Agent configuration containing binary, flags, and template.
        write: Enable file modifications (uses write_flags instead of read_only_flags).
        json_output: Enable JSON output format (adds json_flags).
        model: Model override (e.g., 'sonnet', 'gemini-2.5-flash').

    Returns:
        Shell command string suitable for execution as 'zsh -c "<command>"'.
        The prompt is referenced via $AGENT_PROMPT, not embedded directly.

    Example:
        >>> from ai_dev_base.config import load_agents
        >>> agents = load_agents()
        >>> cmd = build_agent_command(agents["claude"], write=True, model="sonnet")
        >>> "claude" in cmd
        True
        >>> "--dangerously-skip-permissions" in cmd
        True
    """
    parts: list[str] = [shlex.quote(agent_config.binary)]
    parts.extend(shlex.quote(f) for f in agent_config.headless_flags)

    if model:
        parts.extend([shlex.quote(agent_config.model_flag), shlex.quote(model)])

    if write:
        parts.extend(shlex.quote(f) for f in agent_config.write_flags)
    else:
        parts.extend(shlex.quote(f) for f in agent_config.read_only_flags)

    if json_output:
        parts.extend(shlex.quote(f) for f in agent_config.json_flags)

    # Append prompt template (uses $AGENT_PROMPT env var expanded at runtime)
    parts.append(agent_config.prompt_template)

    return " ".join(parts)


# =============================================================================
# Run Command
# =============================================================================


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
        codeagent run claude "Explain this code"

        # Allow file modifications
        codeagent run claude "Fix the bug in main.py" --write

        # Use a specific model with JSON output
        codeagent run gemini "Refactor this file" --write --model gemini-2.5-flash --json

        # With Docker access and timeout
        codeagent run claude "Build the Docker image" --docker --timeout 300
    """
    # Validate mutual exclusivity
    if docker and docker_direct:
        error("--docker and --docker-direct are mutually exclusive")
        raise typer.Exit(1)

    # Load configuration (ConfigNotFoundError handled by decorator)
    app_config = load_config()
    agent_configs = load_agents()

    # Validate agent name
    if agent not in agent_configs:
        error(f"Unknown agent: {agent}")
        available = ", ".join(sorted(agent_configs.keys()))
        console.print(f"Available agents: {available}")
        raise typer.Exit(1)

    agent_config = agent_configs[agent]

    # Ensure Docker network exists
    if not network_exists() and not ensure_network():
        error("Failed to create Docker network 'ai-dev-network'")
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
        status_line("Mode", "Read/Write (--write)", "warning")
    else:
        status_line("Mode", "Read-only (plan/analysis)", "success")

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

    # Build agent command
    agent_cmd = build_agent_command(
        agent_config,
        write=write,
        json_output=json_output,
        model=model,
    )

    # Configure container options
    options = ContainerOptions(
        docker_enabled=docker,
        docker_direct=docker_direct,
        firewall_enabled=firewall,
        mount_path=workspace,
        shell_mounts=True,
    )

    # Execute in container
    from ai_dev_base.core.docker import compose_run

    result = compose_run(
        app_config,
        options,
        command=agent_cmd,
        interactive=False,
        env={"AGENT_PROMPT": prompt},
        timeout=timeout,
    )

    # Output to stdout (agent response)
    if result.stdout:
        console.print(result.stdout, end="")

    # Errors to stderr
    if result.stderr:
        err_console.print(result.stderr, end="")

    returncode = result.returncode

    # Cleanup docker proxy if it was started (not needed for direct mode)
    cleanup_docker_proxy(docker)

    raise typer.Exit(returncode)


# =============================================================================
# Agents List Command
# =============================================================================


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
        codeagent agents

        # Detailed view
        codeagent agents --verbose

        # JSON output for scripting
        codeagent agents --json
    """
    try:
        agent_configs = load_agents()
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as e:
        error(f"Failed to load agents: {e}")
        raise typer.Exit(1) from None

    if json_output:
        data = {
            name: {
                "binary": cfg.binary,
                "description": cfg.description,
                "headless_flags": cfg.headless_flags,
                "read_only_flags": cfg.read_only_flags,
                "write_flags": cfg.write_flags,
                "json_flags": cfg.json_flags,
                "model_flag": cfg.model_flag,
            }
            for name, cfg in sorted(agent_configs.items())
        }
        console.print(json.dumps(data, indent=2))
        return

    if verbose:
        for name, cfg in sorted(agent_configs.items()):
            console.print(f"[primary.bold]{name}[/primary.bold]: {cfg.description or cfg.binary}")
            console.print(f"  [muted]Binary:[/muted]      {cfg.binary}")
            console.print(f"  [muted]Model flag:[/muted]  {cfg.model_flag}")
            if cfg.headless_flags:
                console.print(f"  [muted]Headless:[/muted]    {' '.join(cfg.headless_flags)}")
            if cfg.write_flags:
                console.print(f"  [muted]Write mode:[/muted]  {' '.join(cfg.write_flags)}")
            if cfg.read_only_flags:
                console.print(f"  [muted]Read-only:[/muted]   {' '.join(cfg.read_only_flags)}")
            if cfg.json_flags:
                console.print(f"  [muted]JSON flags:[/muted]  {' '.join(cfg.json_flags)}")
            console.print()
    else:
        console.print("[header]Available Agents:[/header]")
        console.print()
        for name, cfg in sorted(agent_configs.items()):
            desc = cfg.description or cfg.binary
            console.print(f"  [primary]{name}[/primary]: {desc}")
