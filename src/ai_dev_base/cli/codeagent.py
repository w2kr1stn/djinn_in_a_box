"""Main CLI entry point for codeagent."""

import typer

from ai_dev_base.commands.agent import agents, run
from ai_dev_base.commands.container import (
    audit,
    auth,
    build,
    clean_app,
    enter,
    start,
    status,
    update,
)

app = typer.Typer(
    name="codeagent",
    help="AI Dev Base CLI - Manage AI development containers",
    no_args_is_help=True,
)

# Register container lifecycle commands
app.command()(build)
app.command()(start)
app.command()(auth)
app.command()(status)
app.command()(audit)
app.command()(update)
app.command()(enter)

# Register clean as a subcommand group
app.add_typer(clean_app, name="clean")

# Register agent execution commands
app.command()(run)
app.command()(agents)


@app.callback()
def main() -> None:
    """AI Dev Base CLI for managing AI development containers."""
