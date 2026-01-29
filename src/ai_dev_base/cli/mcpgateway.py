"""CLI entry point for mcpgateway.

This module provides the mcpgateway CLI for managing the Model Context
Protocol Gateway that provides MCP servers to AI coding agents.

Usage:
    mcpgateway start          Start the MCP Gateway service
    mcpgateway stop           Stop the MCP Gateway service
    mcpgateway restart        Restart the MCP Gateway service
    mcpgateway status         Show gateway status and enabled servers
    mcpgateway logs [-f]      Show gateway logs

    mcpgateway enable <srv>   Enable an MCP server
    mcpgateway disable <srv>  Disable an MCP server
    mcpgateway servers        List enabled servers
    mcpgateway catalog        Show available servers in catalog

    mcpgateway test           Test gateway connectivity
    mcpgateway clean          Stop gateway and remove config (full reset)
"""

import typer

from ai_dev_base.commands import mcp

app = typer.Typer(
    name="mcpgateway",
    help="MCP Gateway CLI - Manage Model Context Protocol servers",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """MCP Gateway CLI for managing Model Context Protocol servers."""


# =============================================================================
# Gateway Lifecycle Commands
# =============================================================================


@app.command()
def start() -> None:
    """Start the MCP Gateway service."""
    mcp.start()


@app.command()
def stop() -> None:
    """Stop the MCP Gateway service."""
    mcp.stop()


@app.command()
def restart() -> None:
    """Restart the MCP Gateway service."""
    mcp.restart()


@app.command()
def status() -> None:
    """Show gateway status and enabled servers."""
    mcp.status()


@app.command()
def logs(
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
    tail: int = typer.Option(100, "--tail", "-n", help="Number of lines to show"),
) -> None:
    """Show gateway logs."""
    mcp.logs(follow=follow, tail=tail)


# =============================================================================
# Server Management Commands
# =============================================================================


@app.command()
def enable(
    server: str = typer.Argument(..., help="MCP server name to enable"),
) -> None:
    """Enable an MCP server.

    Examples:
        mcpgateway enable duckduckgo
        mcpgateway enable memory
    """
    mcp.enable(server)


@app.command()
def disable(
    server: str = typer.Argument(..., help="MCP server name to disable"),
) -> None:
    """Disable an MCP server."""
    mcp.disable(server)


@app.command()
def servers() -> None:
    """List enabled MCP servers."""
    mcp.servers()


@app.command()
def catalog() -> None:
    """Show available servers in the catalog."""
    mcp.catalog()


# =============================================================================
# Diagnostic Commands
# =============================================================================


@app.command()
def test() -> None:
    """Test gateway connectivity."""
    mcp.test()


@app.command()
def clean() -> None:
    """Stop gateway and remove all configuration (full reset)."""
    mcp.clean()
