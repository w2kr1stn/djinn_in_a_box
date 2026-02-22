"""Djinn CLI — container lifecycle management for Djinn in a Box."""

from __future__ import annotations

from typing import Annotated

import typer

from djinn_in_a_box import __version__
from djinn_in_a_box.commands.agent import agents, run
from djinn_in_a_box.commands.config import config_path, config_show, init_config
from djinn_in_a_box.commands.container import (
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
    name="djinn",
    help="Djinn in a Box CLI - Manage AI development containers",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        typer.echo(f"djinn {__version__}")
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
    """Djinn in a Box CLI - Manage AI development containers.

    The Djinn in a Box provides a containerized development environment
    with pre-configured CLI coding agents (Claude, Gemini, Codex, OpenCode).

    [info.bold]Quick start:[/info.bold]

        djinn init            # First-time setup

        djinn build           # Build the Docker image

        djinn start           # Start development shell

        djinn run claude "Hello world"  # Run an agent
    """


config_app = typer.Typer(
    help="Manage configuration files.",
    no_args_is_help=True,
)

app.command("init")(init_config)

config_app.command("show")(config_show)
config_app.command("path")(config_path)
app.add_typer(config_app, name="config")

app.command()(build)
app.command()(start)
app.command()(auth)
app.command()(status)
app.command()(audit)
app.command()(update)
app.command()(enter)

app.add_typer(clean_app, name="clean")

app.command()(run)
app.command()(agents)

if __name__ == "__main__":
    app()
