"""MCP Gateway CLI — MCP server management for Djinn in a Box."""

from typing import Annotated

import typer

from djinn_in_a_box import __version__
from djinn_in_a_box.commands import mcp

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


app.command("start")(mcp.start)
app.command("stop")(mcp.stop)
app.command("restart")(mcp.restart)
app.command("status")(mcp.status)
app.command("logs")(mcp.logs)

app.command("enable")(mcp.enable)
app.command("disable")(mcp.disable)
app.command("servers")(mcp.servers)
app.command("catalog")(mcp.catalog)

app.command("test")(mcp.test)
app.command("clean")(mcp.clean)

if __name__ == "__main__":
    app()
