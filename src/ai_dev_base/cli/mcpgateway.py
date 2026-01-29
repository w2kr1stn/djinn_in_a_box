"""CLI entry point for mcpgateway."""

import typer

app = typer.Typer(
    name="mcpgateway",
    help="MCP Gateway CLI - Manage Model Context Protocol servers",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """MCP Gateway CLI for managing Model Context Protocol servers."""
