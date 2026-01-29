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

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from ai_dev_base.config import ConfigNotFoundError, load_agents, load_config
from ai_dev_base.core.console import console, err_console, error, info, status_line
from ai_dev_base.core.docker import (
    ContainerOptions,
    cleanup_docker_proxy,
    ensure_network,
    get_compose_files,
    get_shell_mount_args,
)
from ai_dev_base.core.paths import get_project_root

if TYPE_CHECKING:
    from ai_dev_base.config.models import AgentConfig, AppConfig

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

    Mirrors the build_shell_command() function from agent_runner.py (lines 88-136).

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
    parts: list[str] = [agent_config.binary]
    parts.extend(agent_config.headless_flags)

    if model:
        parts.extend([agent_config.model_flag, model])

    if write:
        parts.extend(agent_config.write_flags)
    else:
        parts.extend(agent_config.read_only_flags)

    if json_output:
        parts.extend(agent_config.json_flags)

    # Append prompt template (uses $AGENT_PROMPT env var expanded at runtime)
    parts.append(agent_config.prompt_template)

    return " ".join(parts)


# =============================================================================
# Headless Execution with Timeout Support
# =============================================================================


def _compose_run_with_timeout(
    config: AppConfig,
    options: ContainerOptions,
    *,
    command: str,
    env: dict[str, str],
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """Run container via compose with optional timeout.

    This is a specialized version of compose_run that supports timeout
    and proper signal handling for headless execution.

    Args:
        config: Application configuration.
        options: Container options.
        command: Shell command to execute.
        env: Environment variables to pass.
        timeout: Timeout in seconds (None = no timeout).

    Returns:
        Tuple of (returncode, stdout, stderr).
    """
    project_root = get_project_root()

    # Build compose command
    compose_files = get_compose_files(docker_enabled=options.docker_enabled)
    cmd = ["docker", "compose", *compose_files, "run", "--rm", "-T"]

    # Environment variables
    env_vars: dict[str, str] = {
        "ENABLE_FIREWALL": str(options.firewall_enabled).lower(),
    }
    env_vars.update(env)

    for key, value in env_vars.items():
        cmd.extend(["-e", f"{key}={value}"])

    # Workspace mount
    if options.mount_path is not None:
        mount_str = f"{options.mount_path}:/home/dev/workspace"
        cmd.extend(["-v", mount_str])
        cmd.extend(["--workdir", "/home/dev/workspace"])

    # Shell mounts
    if options.shell_mounts:
        shell_args = get_shell_mount_args(config)
        cmd.extend(shell_args)

    # Service and command
    cmd.append("dev")
    cmd.extend(["-c", command])

    # Prepare environment for subprocess
    subprocess_env = os.environ.copy()
    subprocess_env.update(env_vars)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_root,
            env=subprocess_env,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr

    except subprocess.TimeoutExpired as e:
        # Handle timeout: decode output if available
        # e.stdout/e.stderr are bytes | None when using capture_output=True
        stdout = e.stdout.decode() if e.stdout is not None else ""
        stderr = e.stderr.decode() if e.stderr is not None else f"Timeout after {timeout}s"

        # Return code 124 is conventional for timeout (like GNU timeout command)
        return 124, stdout, stderr


# =============================================================================
# Run Command
# =============================================================================


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
    # Load configuration
    try:
        app_config = load_config()
        agent_configs = load_agents()
    except ConfigNotFoundError as e:
        error(str(e))
        raise typer.Exit(1) from None

    # Validate agent name
    if agent not in agent_configs:
        error(f"Unknown agent: {agent}")
        available = ", ".join(sorted(agent_configs.keys()))
        console.print(f"Available agents: {available}")
        raise typer.Exit(1)

    agent_config = agent_configs[agent]

    # Ensure Docker network exists
    ensure_network()

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
        status_line("Mode", "Read/Write (--write)", "yellow")
    else:
        status_line("Mode", "Read-only (plan/analysis)", "green")

    if docker:
        status_line("Docker", "Enabled")
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
        firewall_enabled=firewall,
        mount_path=workspace,
        shell_mounts=True,
    )

    # Execute in container
    returncode, stdout, stderr = _compose_run_with_timeout(
        app_config,
        options,
        command=agent_cmd,
        env={"AGENT_PROMPT": prompt},
        timeout=timeout,
    )

    # Output to stdout (agent response)
    if stdout:
        console.print(stdout, end="")

    # Errors to stderr
    if stderr:
        err_console.print(stderr, end="")

    # Cleanup docker proxy if it was started
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
    except Exception as e:
        error(f"Failed to load agents: {e}")
        raise typer.Exit(1) from None

    if json_output:
        import json

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
            console.print(f"[bold cyan]{name}[/bold cyan]: {cfg.description or cfg.binary}")
            console.print(f"  [dim]Binary:[/dim]      {cfg.binary}")
            console.print(f"  [dim]Model flag:[/dim]  {cfg.model_flag}")
            if cfg.headless_flags:
                console.print(f"  [dim]Headless:[/dim]    {' '.join(cfg.headless_flags)}")
            if cfg.write_flags:
                console.print(f"  [dim]Write mode:[/dim]  {' '.join(cfg.write_flags)}")
            if cfg.read_only_flags:
                console.print(f"  [dim]Read-only:[/dim]   {' '.join(cfg.read_only_flags)}")
            if cfg.json_flags:
                console.print(f"  [dim]JSON flags:[/dim]  {' '.join(cfg.json_flags)}")
            console.print()
    else:
        console.print("[bold]Available Agents:[/bold]")
        console.print()
        for name, cfg in sorted(agent_configs.items()):
            desc = cfg.description or cfg.binary
            console.print(f"  [cyan]{name}[/cyan]: {desc}")
