"""MCP Gateway CLI entry point.

Provides commands for managing the Model Context Protocol Gateway
that enables MCP servers for AI coding agents.

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

from typing import Annotated

import typer

from ai_dev_base import __version__
from ai_dev_base.commands import mcp

app = typer.Typer(
    name="mcpgateway",
    help="MCP Gateway CLI - Manage Model Context Protocol servers",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        typer.echo(f"mcpgateway {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """MCP Gateway CLI - Manage Model Context Protocol servers.

    The MCP Gateway provides Model Context Protocol servers to AI coding
    agents running in the development container.

    [bold]Quick start:[/bold]

        mcpgateway start          # Start the gateway

        mcpgateway enable memory  # Enable a server

        mcpgateway status         # Check status
    """


# =============================================================================
# Gateway Lifecycle Commands
# =============================================================================

app.command("start")(mcp.start)
app.command("stop")(mcp.stop)
app.command("restart")(mcp.restart)
app.command("status")(mcp.status)
app.command("logs")(mcp.logs)

# =============================================================================
# Server Management Commands
# =============================================================================

app.command("enable")(mcp.enable)
app.command("disable")(mcp.disable)
app.command("servers")(mcp.servers)
app.command("catalog")(mcp.catalog)

# =============================================================================
# Diagnostic Commands
# =============================================================================

app.command("test")(mcp.test)
app.command("clean")(mcp.clean)


if __name__ == "__main__":
    app()
