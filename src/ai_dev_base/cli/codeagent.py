"""Main CLI entry point for codeagent."""

import typer

app = typer.Typer(
    name="codeagent",
    help="AI Dev Base CLI - Manage AI development containers",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """AI Dev Base CLI for managing AI development containers."""
