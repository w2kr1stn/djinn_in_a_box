"""Main CLI entry point for codeagent."""

import typer

from ai_dev_base.commands.agent import agents, run
from ai_dev_base.commands.container import auth, build, start

app = typer.Typer(
    name="codeagent",
    help="AI Dev Base CLI - Manage AI development containers",
    no_args_is_help=True,
)

# Register container lifecycle commands
app.command()(build)
app.command()(start)
app.command()(auth)

# Register agent execution commands
app.command()(run)
app.command()(agents)


@app.callback()
def main() -> None:
    """AI Dev Base CLI for managing AI development containers."""
