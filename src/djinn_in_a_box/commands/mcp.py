"""MCP Gateway commands — lifecycle, server management, and diagnostics."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Annotated

import typer

from djinn_in_a_box.core.console import (
    console,
    err_console,
    error,
    info,
    rule,
    status_line,
    success,
    warning,
)
from djinn_in_a_box.core.docker import (
    DJINN_NETWORK,
    delete_network,
    ensure_network,
    is_container_running,
)
from djinn_in_a_box.core.paths import get_project_root

GATEWAY_CONTAINER: str = "mcp-gateway"
"""Name of the MCP Gateway container."""

GATEWAY_ENDPOINT_CONTAINER: str = "http://mcp-gateway:8811"
"""MCP Gateway endpoint accessible from other containers."""

GATEWAY_ENDPOINT_HOST: str = "http://localhost:8811"
"""MCP Gateway endpoint accessible from the host."""


def _get_mcp_dir() -> Path:
    """Return path to the mcp/ directory (lazy, avoids import-time crash)."""
    return get_project_root() / "mcp"


def _require_mcp_cli() -> None:
    try:
        result = subprocess.run(["docker", "mcp", "--help"], capture_output=True, check=False)
    except FileNotFoundError:
        error("Docker is not installed. Install Docker first, then the MCP CLI plugin.")
        raise typer.Exit(1) from None
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
    if not is_container_running(GATEWAY_CONTAINER):
        error("MCP Gateway is not running")
        err_console.print("Start it with: mcpgateway start")
        raise typer.Exit(1)


def _run_mcp_compose(args: list[str], error_msg: str) -> None:
    """Run a docker compose command in the MCP directory. Raises typer.Exit on failure."""
    try:
        result = subprocess.run(["docker", "compose", *args], cwd=_get_mcp_dir(), check=False)
    except FileNotFoundError:
        error("Docker is not installed or not on PATH.")
        raise typer.Exit(1) from None
    if result.returncode != 0:
        error(error_msg)
        raise typer.Exit(result.returncode)


def start() -> None:
    """Start the MCP Gateway service."""
    _require_mcp_cli()
    if not ensure_network(DJINN_NETWORK):
        error(f"Failed to create Docker network '{DJINN_NETWORK}'")
        raise typer.Exit(1)
    info("Starting MCP Gateway...")

    _run_mcp_compose(["up", "-d"], "Failed to start MCP Gateway")

    # Wait for container to be ready
    time.sleep(3)

    if is_container_running(GATEWAY_CONTAINER):
        success("MCP Gateway is running")
        err_console.print()
        status_line("Endpoint", f"{GATEWAY_ENDPOINT_CONTAINER} (from containers)")
        status_line("Host", f"{GATEWAY_ENDPOINT_HOST} (from host)")
        rule("Next steps")
        err_console.print("  mcpgateway enable duckduckgo    # Enable web search")
        err_console.print("  mcpgateway enable memory        # Enable persistent memory")
        err_console.print("  mcpgateway servers              # List enabled servers")
    else:
        error("MCP Gateway failed to start")
        # Show logs for debugging
        subprocess.run(
            ["docker", "compose", "logs"],
            cwd=_get_mcp_dir(),
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
    rule("MCP Gateway Status")

    if is_container_running(GATEWAY_CONTAINER):
        status_line("Gateway", "Running", "status.enabled")
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

        rule("Enabled Servers")
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

        rule("Running MCP Containers")
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
        status_line("Gateway", "Stopped", "status.error")
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
    rule("Enabled MCP Servers")

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
    rule("MCP Server Catalog")

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


def test() -> None:
    """Test gateway connectivity (container, endpoints, socket, CLI plugin)."""
    rule("Testing MCP Gateway")
    err_console.print()

    all_passed = True

    # Container status
    if is_container_running(GATEWAY_CONTAINER):
        status_line("Container", "Running", "status.enabled")
    else:
        status_line("Container", "Not running", "status.error")
        all_passed = False

    # Localhost endpoint
    try:
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
            status_line("Localhost", "OK", "status.enabled")
        else:
            status_line("Localhost", "Not responding", "status.disabled")
    except FileNotFoundError:
        status_line("Localhost", "curl not installed", "status.disabled")

    # Container endpoint (via docker network)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            DJINN_NETWORK,
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
        status_line("Network", "OK", "status.enabled")
    else:
        status_line("Network", "Not responding (network may not exist yet)", "status.disabled")

    # Docker socket access
    result = subprocess.run(
        ["docker", "exec", GATEWAY_CONTAINER, "ls", "/var/run/docker.sock"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        status_line("Docker sock", "OK", "status.enabled")
    else:
        status_line("Docker sock", "Failed", "status.error")
        all_passed = False

    # CLI plugin
    cli_check = subprocess.run(["docker", "mcp", "--help"], capture_output=True, check=False)
    if cli_check.returncode == 0:
        status_line("MCP plugin", "Installed", "status.enabled")
    else:
        status_line("MCP plugin", "Not installed", "status.disabled")

    # Show endpoint URLs
    rule("MCP Gateway URLs")
    err_console.print(f"  Streamable HTTP: {GATEWAY_ENDPOINT_CONTAINER}/mcp")

    if not all_passed:
        raise typer.Exit(1)


def clean() -> None:
    """Stop gateway and remove all configuration (full reset)."""
    warning("This will stop the gateway and remove all configuration!")

    if not typer.confirm("Are you sure?"):
        raise typer.Abort()

    # Stop gateway — warn on unexpected failures
    try:
        result = subprocess.run(
            ["docker", "compose", "down"],
            cwd=_get_mcp_dir(),
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            stderr_msg = result.stderr.strip() if result.stderr else ""
            if stderr_msg and "not running" not in stderr_msg.lower():
                warning(f"Failed to stop gateway: {stderr_msg}")
    except FileNotFoundError:
        warning("Docker is not installed, skipping container cleanup")

    # Remove network
    delete_network(DJINN_NETWORK)

    # Remove MCP config directory (~/.docker/mcp)
    mcp_config = Path.home() / ".docker" / "mcp"
    if mcp_config.exists():
        try:
            shutil.rmtree(mcp_config)
        except OSError as e:
            warning(f"Failed to remove {mcp_config}: {e}")

    success("MCP Gateway cleaned")
