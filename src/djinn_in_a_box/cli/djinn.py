"""Djinn CLI — container lifecycle management for Djinn in a Box."""

from __future__ import annotations

from typing import Annotated

import typer

from djinn_in_a_box.cli import version_callback
from djinn_in_a_box.commands.agent import agents, run
from djinn_in_a_box.commands.backup import backup, restore
from djinn_in_a_box.commands.config import (
    config_edit,
    config_path,
    config_set,
    config_show,
    config_status,
    config_sync,
    init_config,
)
from djinn_in_a_box.commands.container import (
    audit,
    build,
    clean_app,
    enter,
    start,
    status,
    update,
)
from djinn_in_a_box.commands.doctor import doctor
from djinn_in_a_box.commands.migrate_zones import migrate_zones
from djinn_in_a_box.commands.session import session

app = typer.Typer(
    name="djinn",
    help="Djinn in a Box CLI - Manage AI development containers",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

_version_callback = version_callback("djinn")


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
config_app.command("set")(config_set)
config_app.command("edit")(config_edit)
config_app.command("status")(config_status)
config_app.command("sync")(config_sync)
app.add_typer(config_app, name="config")

app.command()(build)
app.command()(start)
app.command()(status)
app.command()(doctor)
app.command("migrate-zones")(migrate_zones)
app.command()(audit)
app.command()(update)
app.command()(enter)

app.add_typer(clean_app, name="clean")

app.command()(backup)
app.command()(restore)

app.command()(run)
app.command()(agents)
app.command()(session)

if __name__ == "__main__":
    app()
