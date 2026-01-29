"""Container lifecycle commands for AI Dev Base CLI.

Provides commands for building, starting, and managing the AI development container:

Commands:
    - build: Build/rebuild the Docker image
    - start: Start interactive development shell with optional Docker/firewall
    - auth: Start with host network for OAuth authentication

These commands mirror the behavior of the original dev.sh Bash script,
maintaining backwards compatibility with the existing workflow.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Annotated

import typer

from ai_dev_base.config import ConfigNotFoundError, load_config
from ai_dev_base.core.console import blank, err_console, error, info, status_line, success
from ai_dev_base.core.docker import (
    ContainerOptions,
    cleanup_docker_proxy,
    compose_build,
    compose_run,
    compose_up,
    ensure_network,
    get_compose_files,
    get_shell_mount_args,
)
from ai_dev_base.core.paths import get_project_root, resolve_mount_path

# =============================================================================
# Build Command
# =============================================================================


def build(
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Build without using cache"),
    ] = False,
) -> None:
    """Build/rebuild the Docker image.

    Runs `docker compose build` to build the ai-dev-base image.
    This must be done before the first use and after Dockerfile changes.

    Equivalent to: ./dev.sh build
    """
    info("Building ai-dev-base image...")

    result = compose_build(no_cache=no_cache)

    if result.success:
        success("Done! Run 'codeagent start' to begin.")
    else:
        error(f"Build failed with exit code {result.returncode}")
        if result.stderr:
            err_console.print(result.stderr)
        raise typer.Exit(result.returncode)


# =============================================================================
# Start Command
# =============================================================================


def start(
    docker: Annotated[
        bool,
        typer.Option("--docker", "-d", help="Enable Docker access via secure proxy"),
    ] = False,
    firewall: Annotated[
        bool,
        typer.Option("--firewall", "-f", help="Enable network firewall (restricts outbound)"),
    ] = False,
    here: Annotated[
        bool,
        typer.Option("--here", help="Mount current directory as ~/workspace"),
    ] = False,
    mount: Annotated[
        Path | None,
        typer.Option("--mount", "-m", help="Mount specified path as ~/workspace"),
    ] = None,
) -> None:
    """Start interactive development shell.

    Launches the AI development container with an interactive shell.
    The container has access to the configured projects directory
    and optionally Docker socket access and firewall restrictions.

    Equivalent to: ./dev.sh start [options]

    Examples:
        codeagent start                    # Basic interactive shell
        codeagent start --docker           # With Docker access
        codeagent start --here             # Mount cwd as workspace
        codeagent start -d -f --here       # Full options
    """
    # Load configuration
    try:
        config = load_config()
    except ConfigNotFoundError as e:
        error(str(e))
        raise typer.Exit(1) from None

    # Ensure Docker network exists
    ensure_network()

    # Resolve mount path
    mount_path: Path | None = None
    if here:
        mount_path = Path.cwd()
    elif mount:
        try:
            mount_path = resolve_mount_path(mount)
        except FileNotFoundError as e:
            error(str(e))
            raise typer.Exit(1) from None
        except NotADirectoryError as e:
            error(str(e))
            raise typer.Exit(1) from None

    # Print status output (to stderr, matching Bash format)
    blank()
    info("Starting AI Dev environment...")
    blank()

    status_line("Projects", str(config.code_dir))

    if docker:
        status_line("Docker", "Enabled (via secure proxy)", "green")
    else:
        status_line("Docker", "Disabled (use --docker to enable)", "yellow")

    if firewall:
        status_line("Firewall", "Enabled (outbound restricted)", "green")
    else:
        status_line("Firewall", "Disabled (use --firewall to enable)", "yellow")

    if mount_path:
        status_line("Workspace", str(mount_path), "green")

    # Shell mount status
    shell_args = get_shell_mount_args(config)
    if config.shell.skip_mounts:
        status_line("Shell", "Using container defaults (skip_mounts=true)", "yellow")
    elif shell_args:
        status_line("Shell", "Host config mounted", "green")
    else:
        status_line("Shell", "No host config found", "yellow")

    blank()

    # Run container
    options = ContainerOptions(
        docker_enabled=docker,
        firewall_enabled=firewall,
        mount_path=mount_path,
    )

    result = compose_run(config, options, interactive=True)

    # Cleanup docker proxy if it was started
    cleanup_docker_proxy(docker)

    raise typer.Exit(result.returncode)


# =============================================================================
# Auth Command
# =============================================================================


def auth(
    docker: Annotated[
        bool,
        typer.Option("--docker", "-d", help="Enable Docker access via proxy"),
    ] = False,
) -> None:
    """Start with host network for OAuth authentication.

    This mode uses network_mode: host so OAuth callbacks from CLI tools
    (Claude Code, Gemini CLI, Codex) can reach localhost.

    After authenticating all tools, exit and use 'codeagent start' for
    normal development with the isolated network.

    Equivalent to: ./dev.sh auth [--docker]

    Example:
        codeagent auth              # Authenticate CLI tools
        codeagent auth --docker     # With Docker access
    """
    # Load configuration
    try:
        config = load_config()
    except ConfigNotFoundError as e:
        error(str(e))
        raise typer.Exit(1) from None

    info("Starting AI Dev with host network for OAuth authentication...")
    blank()
    err_console.print("This mode uses network_mode: host so OAuth callbacks work.")
    err_console.print(
        "After authenticating Claude Code, Gemini CLI and Codex, "
        "exit and use 'codeagent start'"
    )
    blank()

    project_root = get_project_root()
    compose_files = get_compose_files(docker_enabled=docker)

    # Start docker proxy separately if docker is enabled
    # In host network mode, the proxy needs to be started as a separate service
    if docker:
        err_console.print(
            "[yellow]Docker proxy starting separately for host network mode...[/yellow]"
        )
        compose_up(services=["docker-proxy"], docker_enabled=True)
        time.sleep(2)

    # Build the auth command with --profile auth and dev-auth service
    cmd = [
        "docker",
        "compose",
        *compose_files,
        "--profile",
        "auth",
        "run",
        "--rm",
    ]

    # Add shell mounts
    shell_args = get_shell_mount_args(config)
    cmd.extend(shell_args)

    # Service name
    cmd.append("dev-auth")

    # Execute interactively
    result = subprocess.run(
        cmd,
        cwd=project_root,
        check=False,
    )

    # Cleanup docker proxy if it was started
    cleanup_docker_proxy(docker)

    raise typer.Exit(result.returncode)
