"""MCP Gateway commands — lifecycle, server management, and diagnostics."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Annotated

import typer

from ai_dev_base.core.console import console, err_console, error, info, success, warning
from ai_dev_base.core.docker import delete_network, ensure_network, is_container_running
from ai_dev_base.core.paths import get_project_root

GATEWAY_CONTAINER: str = "mcp-gateway"
"""Name of the MCP Gateway container."""

GATEWAY_ENDPOINT_CONTAINER: str = "http://mcp-gateway:8811"
"""MCP Gateway endpoint accessible from other containers."""

GATEWAY_ENDPOINT_HOST: str = "http://localhost:8811"
"""MCP Gateway endpoint accessible from the host."""

AI_DEV_NETWORK: str = "ai-dev-network"
"""Docker network name for AI Dev containers."""


MCP_DIR: Path = get_project_root() / "mcp"
"""Path to the mcp/ directory containing docker-compose.yml."""


def _require_mcp_cli() -> None:
    """Check for MCP CLI plugin and exit with error if not installed."""
    result = subprocess.run(["docker", "mcp", "--help"], capture_output=True, check=False)
    if result.returncode != 0:
        error(
            "'docker mcp' CLI plugin not installed.\n\n"
            "Install it with:\n"
            "  git clone https://github.com/docker/mcp-gateway.git\n"
            "  cd mcp-gateway && make docker-mcp\n\n"
            "Or download a binary from:\n"
            "  https://github.com/docker/mcp-gateway/releases"
        )
        raise typer.Exit(1)


def _require_running() -> None:
    """Exit with error if MCP Gateway container is not running."""
    if not is_container_running(GATEWAY_CONTAINER):
        error("MCP Gateway is not running")
        err_console.print("Start it with: mcpgateway start")
        raise typer.Exit(1)


def _run_mcp_compose(args: list[str], error_msg: str) -> None:
    """Run a docker compose command in the MCP directory. Raises typer.Exit on failure."""
    result = subprocess.run(["docker", "compose", *args], cwd=MCP_DIR, check=False)
    if result.returncode != 0:
        error(error_msg)
        raise typer.Exit(result.returncode)


# =============================================================================
# Gateway Lifecycle Commands
# =============================================================================


def start() -> None:
    """Start the MCP Gateway service."""
    _require_mcp_cli()
    ensure_network(AI_DEV_NETWORK)
    info("Starting MCP Gateway...")

    _run_mcp_compose(["up", "-d"], "Failed to start MCP Gateway")

    # Wait for container to be ready
    time.sleep(3)

    if is_container_running(GATEWAY_CONTAINER):
        success("MCP Gateway is running")
        err_console.print()
        err_console.print(f"Endpoint: {GATEWAY_ENDPOINT_CONTAINER} (from containers)")
        err_console.print(f"          {GATEWAY_ENDPOINT_HOST} (from host)")
        err_console.print()
        err_console.print("Next steps:")
        err_console.print("  mcpgateway enable duckduckgo    # Enable web search")
        err_console.print("  mcpgateway enable memory        # Enable persistent memory")
        err_console.print("  mcpgateway servers              # List enabled servers")
    else:
        error("MCP Gateway failed to start")
        # Show logs for debugging
        subprocess.run(
            ["docker", "compose", "logs"],
            cwd=MCP_DIR,
            check=False,
        )
        raise typer.Exit(1)


def stop() -> None:
    """Stop the MCP Gateway service."""
    warning("Stopping MCP Gateway...")
    _run_mcp_compose(["down"], "Failed to stop MCP Gateway")
    success("MCP Gateway stopped")


def restart() -> None:
    """Restart the MCP Gateway service."""
    warning("Restarting MCP Gateway...")
    _run_mcp_compose(["restart"], "Failed to restart MCP Gateway")
    time.sleep(2)
    success("MCP Gateway restarted")


def status() -> None:
    """Show gateway status and enabled servers."""
    info("MCP Gateway Status")
    err_console.print("=" * 40)

    if is_container_running(GATEWAY_CONTAINER):
        err_console.print("Gateway: [status.enabled]Running[/status.enabled]")
        err_console.print()

        # Show container details
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"name={GATEWAY_CONTAINER}",
                "--format",
                "ID: {{.ID}}\nImage: {{.Image}}\nStatus: {{.Status}}\nPorts: {{.Ports}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.stdout.strip():
            err_console.print(result.stdout.strip())

        err_console.print()
        info("Enabled Servers:")
        result = subprocess.run(
            ["docker", "mcp", "server", "ls"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            err_console.print(result.stdout.strip())
        else:
            err_console.print("  (none)")

        err_console.print()
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
                    err_console.print(line)
            else:
                err_console.print("  (none)")
        else:
            err_console.print("  (none)")
    else:
        err_console.print("Gateway: [status.error]Stopped[/status.error]")
        err_console.print()
        err_console.print("Start with: mcpgateway start")


def logs(
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow log output")] = False,
    tail: Annotated[int, typer.Option("--tail", "-n", help="Number of lines to show")] = 100,
) -> None:
    """Show gateway logs."""
    _require_running()

    cmd = ["docker", "logs"]
    if follow:
        cmd.append("-f")
    cmd.extend(["--tail", str(tail), GATEWAY_CONTAINER])

    result = subprocess.run(cmd, check=False)
    raise typer.Exit(result.returncode)


# =============================================================================
# Server Management Commands
# =============================================================================


def enable(
    server: Annotated[str, typer.Argument(help="MCP server name to enable")],
) -> None:
    """Enable an MCP server."""
    _require_mcp_cli()
    _require_running()

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


def disable(
    server: Annotated[str, typer.Argument(help="MCP server name to disable")],
) -> None:
    """Disable an MCP server."""
    _require_mcp_cli()
    _require_running()

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


def servers() -> None:
    """List enabled MCP servers."""
    _require_mcp_cli()
    info("Enabled MCP Servers")
    err_console.print("=" * 40)

    result = subprocess.run(
        ["docker", "mcp", "server", "ls"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode == 0 and result.stdout.strip():
        console.print(result.stdout.strip())
    else:
        err_console.print("No servers enabled or gateway not running")


def catalog() -> None:
    """Show available servers in the catalog."""
    _require_mcp_cli()
    info("MCP Server Catalog")
    err_console.print("=" * 40)

    result = subprocess.run(
        ["docker", "mcp", "catalog", "show", "docker-mcp"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        err_console.print("Unable to fetch catalog.")
        err_console.print()
        err_console.print("Initialize catalog first: docker mcp catalog init")
        err_console.print("Or browse online: https://hub.docker.com/search?q=mcp%2F")
    else:
        console.print(result.stdout.strip())


# =============================================================================
# Diagnostic Commands
# =============================================================================


def test() -> None:
    """Test gateway connectivity (container, endpoints, socket, CLI plugin)."""
    info("Testing MCP Gateway...")
    err_console.print()

    all_passed = True

    # Container status
    err_console.print("Container status: ", end="")
    if is_container_running(GATEWAY_CONTAINER):
        err_console.print("[status.enabled]Running[/status.enabled]")
    else:
        err_console.print("[status.error]Not running[/status.error]")
        all_passed = False

    # Localhost endpoint
    err_console.print("Localhost endpoint (host access): ", end="")
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
        err_console.print("[status.enabled]OK[/status.enabled]")
    else:
        err_console.print("[status.disabled]Not responding[/status.disabled]")

    # Container endpoint (via docker network)
    err_console.print("Container endpoint (network access): ", end="")
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
        err_console.print("[status.enabled]OK[/status.enabled]")
    else:
        err_console.print(
            "[status.disabled]Not responding (network may not exist yet)[/status.disabled]"
        )

    # Docker socket access
    err_console.print("Docker socket access: ", end="")
    result = subprocess.run(
        ["docker", "exec", GATEWAY_CONTAINER, "ls", "/var/run/docker.sock"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        err_console.print("[status.enabled]OK[/status.enabled]")
    else:
        err_console.print("[status.error]Failed[/status.error]")
        all_passed = False

    # CLI plugin
    err_console.print("docker mcp CLI plugin: ", end="")
    cli_check = subprocess.run(["docker", "mcp", "--help"], capture_output=True, check=False)
    if cli_check.returncode == 0:
        err_console.print("[status.enabled]Installed[/status.enabled]")
    else:
        err_console.print("[status.disabled]Not installed[/status.disabled]")

    # Show endpoint URLs
    err_console.print()
    err_console.print("MCP Gateway URLs:")
    err_console.print("  Claude Code: http://mcp-gateway:8811/sse")
    err_console.print("  Codex CLI:   http://mcp-gateway:8811")

    if not all_passed:
        raise typer.Exit(1)


def clean() -> None:
    """Stop gateway and remove all configuration (full reset)."""
    warning("This will stop the gateway and remove all configuration!")

    if not typer.confirm("Are you sure?"):
        raise typer.Abort()

    # Stop gateway (ignore errors — may not be running)
    subprocess.run(
        ["docker", "compose", "down"],
        cwd=MCP_DIR,
        capture_output=True,
        check=False,
    )

    # Remove network
    delete_network(AI_DEV_NETWORK)

    # Remove MCP config directory (~/.docker/mcp)
    mcp_config = Path.home() / ".docker" / "mcp"
    if mcp_config.exists():
        try:
            shutil.rmtree(mcp_config)
        except OSError as e:
            warning(f"Failed to remove {mcp_config}: {e}")

    success("MCP Gateway cleaned")
