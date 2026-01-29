"""MCP Gateway commands for AI Dev Base CLI.

Commands for managing the Model Context Protocol Gateway
that provides MCP servers to AI coding agents.

This module implements the functionality from mcp/mcp.sh:
- Gateway lifecycle: start, stop, restart, status, logs
- Server management: enable, disable, servers, catalog
- Diagnostics: test, clean
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, ParamSpec, TypeVar

import typer

from ai_dev_base.core.console import console, error, info, success, warning
from ai_dev_base.core.docker import ensure_network, is_container_running
from ai_dev_base.core.paths import get_project_root

P = ParamSpec("P")
R = TypeVar("R")

# =============================================================================
# Constants
# =============================================================================

GATEWAY_CONTAINER: str = "mcp-gateway"
"""Name of the MCP Gateway container."""

GATEWAY_ENDPOINT_CONTAINER: str = "http://mcp-gateway:8811"
"""MCP Gateway endpoint accessible from other containers."""

GATEWAY_ENDPOINT_HOST: str = "http://localhost:8811"
"""MCP Gateway endpoint accessible from the host."""

AI_DEV_NETWORK: str = "ai-dev-network"
"""Docker network name for AI Dev containers."""


# =============================================================================
# Exceptions
# =============================================================================


class MCPCliNotFoundError(Exception):
    """Raised when the docker mcp CLI plugin is not installed."""


# =============================================================================
# Helper Functions
# =============================================================================


def check_mcp_cli() -> None:
    """Verify that the docker mcp CLI plugin is installed.

    Mirrors the check_mcp_cli() function from the original mcp.sh script.

    Raises:
        MCPCliNotFoundError: If the docker mcp plugin is not installed.
    """
    result = subprocess.run(
        ["docker", "mcp", "--help"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        msg = (
            "'docker mcp' CLI plugin not installed.\n\n"
            "Install it with:\n"
            "  git clone https://github.com/docker/mcp-gateway.git\n"
            "  cd mcp-gateway\n"
            "  make docker-mcp\n\n"
            "Or download a binary from:\n"
            "  https://github.com/docker/mcp-gateway/releases"
        )
        raise MCPCliNotFoundError(msg)


def require_running() -> None:
    """Ensure the MCP Gateway container is running.

    Mirrors the require_running() function from the original mcp.sh script.

    Raises:
        typer.Exit: If the gateway is not running.
    """
    if not is_container_running(GATEWAY_CONTAINER):
        error("MCP Gateway is not running")
        console.print("Start it with: mcpgateway start")
        raise typer.Exit(1)


def get_mcp_dir() -> Path:
    """Get the path to the mcp/ directory in the project.

    Returns:
        Path to the mcp/ directory containing docker-compose.yml.
    """
    return get_project_root() / "mcp"


def require_mcp_cli(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator to check for MCP CLI before executing a command.

    Wraps the function with a check for the docker mcp CLI plugin.
    If the plugin is not installed, displays an error and exits.

    Example:
        >>> @require_mcp_cli
        ... def my_command():
        ...     # docker mcp commands here
        ...     pass
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            check_mcp_cli()
        except MCPCliNotFoundError as e:
            error(str(e))
            raise typer.Exit(1) from None
        return func(*args, **kwargs)

    return wrapper


# =============================================================================
# Gateway Lifecycle Commands
# =============================================================================


def start() -> None:
    """Start the MCP Gateway service.

    Equivalent to: ./mcp.sh start

    - Checks for docker mcp CLI plugin
    - Ensures ai-dev-network exists
    - Starts the gateway via docker compose
    - Displays endpoint information on success
    """
    try:
        check_mcp_cli()
    except MCPCliNotFoundError as e:
        error(str(e))
        raise typer.Exit(1) from None

    ensure_network(AI_DEV_NETWORK)
    info("Starting MCP Gateway...")

    result = subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=get_mcp_dir(),
        check=False,
    )

    if result.returncode != 0:
        error("Failed to start MCP Gateway")
        raise typer.Exit(result.returncode)

    # Wait for container to be ready
    time.sleep(3)

    if is_container_running(GATEWAY_CONTAINER):
        success("MCP Gateway is running")
        console.print()
        console.print(f"Endpoint: {GATEWAY_ENDPOINT_CONTAINER} (from containers)")
        console.print(f"          {GATEWAY_ENDPOINT_HOST} (from host)")
        console.print()
        console.print("Next steps:")
        console.print("  mcpgateway enable duckduckgo    # Enable web search")
        console.print("  mcpgateway enable memory        # Enable persistent memory")
        console.print("  mcpgateway servers              # List enabled servers")
    else:
        error("MCP Gateway failed to start")
        # Show logs for debugging
        subprocess.run(
            ["docker", "compose", "logs"],
            cwd=get_mcp_dir(),
            check=False,
        )
        raise typer.Exit(1)


def stop() -> None:
    """Stop the MCP Gateway service.

    Equivalent to: ./mcp.sh stop
    """
    warning("Stopping MCP Gateway...")
    result = subprocess.run(
        ["docker", "compose", "down"],
        cwd=get_mcp_dir(),
        check=False,
    )
    if result.returncode != 0:
        error("Failed to stop MCP Gateway")
        raise typer.Exit(result.returncode)
    success("MCP Gateway stopped")


def restart() -> None:
    """Restart the MCP Gateway service.

    Equivalent to: ./mcp.sh restart
    """
    warning("Restarting MCP Gateway...")
    result = subprocess.run(
        ["docker", "compose", "restart"],
        cwd=get_mcp_dir(),
        check=False,
    )
    if result.returncode != 0:
        error("Failed to restart MCP Gateway")
        raise typer.Exit(result.returncode)
    time.sleep(2)
    success("MCP Gateway restarted")


def status() -> None:
    """Show gateway status and enabled servers.

    Equivalent to: ./mcp.sh status

    Displays:
    - Gateway running status
    - Container details (ID, Image, Status, Ports)
    - Enabled MCP servers
    - Running MCP containers
    """
    info("MCP Gateway Status")
    console.print("=" * 40)

    if is_container_running(GATEWAY_CONTAINER):
        console.print("Gateway: [green]Running[/green]")
        console.print()

        # Show container details
        subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"name={GATEWAY_CONTAINER}",
                "--format",
                "ID: {{.ID}}\nImage: {{.Image}}\nStatus: {{.Status}}\nPorts: {{.Ports}}",
            ],
            check=False,
        )

        console.print()
        info("Enabled Servers:")
        result = subprocess.run(
            ["docker", "mcp", "server", "ls"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            console.print(result.stdout.strip())
        else:
            console.print("  (none)")

        console.print()
        info("Running MCP Containers:")
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "name=mcp-",
                "--format",
                "  {{.Names}} ({{.Status}})",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        # Filter out the gateway itself
        if result.returncode == 0 and result.stdout.strip():
            lines = [
                line for line in result.stdout.strip().split("\n") if "mcp-gateway" not in line
            ]
            if lines:
                for line in lines:
                    console.print(line)
            else:
                console.print("  (none)")
        else:
            console.print("  (none)")
    else:
        console.print("Gateway: [red]Stopped[/red]")
        console.print()
        console.print("Start with: mcpgateway start")


def logs(
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow log output")] = False,
    tail: Annotated[int, typer.Option("--tail", "-n", help="Number of lines to show")] = 100,
) -> None:
    """Show gateway logs.

    Equivalent to: ./mcp.sh logs [-f]

    Args:
        follow: Follow log output in real-time.
        tail: Number of lines to show (default: 100).
    """
    require_running()

    cmd = ["docker", "logs"]
    if follow:
        cmd.append("-f")
    cmd.extend(["--tail", str(tail), GATEWAY_CONTAINER])

    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


# =============================================================================
# Server Management Commands
# =============================================================================


@require_mcp_cli
def enable(
    server: Annotated[str, typer.Argument(help="MCP server name to enable")],
) -> None:
    """Enable an MCP server.

    Equivalent to: ./mcp.sh enable <server>

    Examples:
        mcpgateway enable duckduckgo
        mcpgateway enable memory

    Args:
        server: Name of the MCP server to enable.
    """
    require_running()

    info(f"Enabling MCP server: {server}")
    result = subprocess.run(
        ["docker", "mcp", "server", "enable", server],
        check=False,
    )

    if result.returncode == 0:
        success(f"Server '{server}' enabled")
    else:
        error(f"Failed to enable server '{server}'")
        raise typer.Exit(result.returncode)


@require_mcp_cli
def disable(
    server: Annotated[str, typer.Argument(help="MCP server name to disable")],
) -> None:
    """Disable an MCP server.

    Equivalent to: ./mcp.sh disable <server>

    Args:
        server: Name of the MCP server to disable.
    """
    require_running()

    warning(f"Disabling MCP server: {server}")
    result = subprocess.run(
        ["docker", "mcp", "server", "disable", server],
        check=False,
    )

    if result.returncode == 0:
        success(f"Server '{server}' disabled")
    else:
        error(f"Failed to disable server '{server}'")
        raise typer.Exit(result.returncode)


@require_mcp_cli
def servers() -> None:
    """List enabled MCP servers.

    Equivalent to: ./mcp.sh servers
    """
    info("Enabled MCP Servers")
    console.print("=" * 40)

    result = subprocess.run(
        ["docker", "mcp", "server", "ls"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode == 0 and result.stdout.strip():
        console.print(result.stdout.strip())
    else:
        console.print("No servers enabled or gateway not running")


@require_mcp_cli
def catalog() -> None:
    """Show available servers in the catalog.

    Equivalent to: ./mcp.sh catalog

    Displays the docker-mcp catalog or provides instructions
    for initializing it.
    """
    info("MCP Server Catalog")
    console.print("=" * 40)

    result = subprocess.run(
        ["docker", "mcp", "catalog", "show", "docker-mcp"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        console.print("Unable to fetch catalog.")
        console.print()
        console.print("Initialize catalog first: docker mcp catalog init")
        console.print("Or browse online: https://hub.docker.com/search?q=mcp%2F")
    else:
        # Run again without capture to display output
        subprocess.run(
            ["docker", "mcp", "catalog", "show", "docker-mcp"],
            check=False,
        )


# =============================================================================
# Diagnostic Commands
# =============================================================================


def test() -> None:
    """Test gateway connectivity.

    Equivalent to: ./mcp.sh test

    Checks:
    - Container status
    - Localhost endpoint accessibility
    - Docker socket access from within the container
    - docker mcp CLI plugin installation
    """
    info("Testing MCP Gateway...")
    console.print()

    all_passed = True

    # Container status
    console.print("Container status: ", end="")
    if is_container_running(GATEWAY_CONTAINER):
        console.print("[green]Running[/green]")
    else:
        console.print("[red]Not running[/red]")
        all_passed = False

    # Localhost endpoint
    console.print("Localhost endpoint (host access): ", end="")
    result = subprocess.run(
        [
            "curl",
            "-s",
            "--connect-timeout",
            "2",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            f"{GATEWAY_ENDPOINT_HOST}/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout in ("200", "404"):
        console.print("[green]OK[/green]")
    else:
        console.print("[yellow]Not responding[/yellow]")

    # Container endpoint (via docker network)
    console.print("Container endpoint (network access): ", end="")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            AI_DEV_NETWORK,
            "curlimages/curl:latest",
            "-s",
            "--connect-timeout",
            "2",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            f"{GATEWAY_ENDPOINT_CONTAINER}/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout in ("200", "404"):
        console.print("[green]OK[/green]")
    else:
        console.print("[yellow]Not responding (network may not exist yet)[/yellow]")

    # Docker socket access
    console.print("Docker socket access: ", end="")
    result = subprocess.run(
        ["docker", "exec", GATEWAY_CONTAINER, "ls", "/var/run/docker.sock"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        console.print("[green]OK[/green]")
    else:
        console.print("[red]Failed[/red]")
        all_passed = False

    # CLI plugin
    console.print("docker mcp CLI plugin: ", end="")
    try:
        check_mcp_cli()
        console.print("[green]Installed[/green]")
    except MCPCliNotFoundError:
        console.print("[yellow]Not installed[/yellow]")

    # Show endpoint URLs
    console.print()
    console.print("MCP Gateway URLs:")
    console.print("  Claude Code: http://mcp-gateway:8811/sse")
    console.print("  Codex CLI:   http://mcp-gateway:8811")

    if not all_passed:
        raise typer.Exit(1)


def clean() -> None:
    """Stop gateway and remove all configuration (full reset).

    Equivalent to: ./mcp.sh clean

    This will:
    - Stop the MCP Gateway container
    - Remove the ai-dev-network
    - Remove ~/.docker/mcp configuration directory

    Requires user confirmation before proceeding.
    """
    warning("This will stop the gateway and remove all configuration!")

    if not typer.confirm("Are you sure?"):
        raise typer.Abort()

    # Stop gateway
    subprocess.run(
        ["docker", "compose", "down"],
        cwd=get_mcp_dir(),
        capture_output=True,
        check=False,
    )

    # Remove network
    result = subprocess.run(
        ["docker", "network", "rm", AI_DEV_NETWORK],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        warning(f"Failed to remove network '{AI_DEV_NETWORK}' (may not exist or be in use)")

    # Remove MCP config directory (~/.docker/mcp)
    mcp_config = Path.home() / ".docker" / "mcp"
    if mcp_config.exists():
        shutil.rmtree(mcp_config)

    success("MCP Gateway cleaned")
