"""CLI entry points for Djinn in a Box."""

from collections.abc import Callable

import typer

from djinn_in_a_box import __version__


def version_callback(name: str) -> Callable[[bool], None]:
    """Create a --version callback for a Typer CLI app."""

    def callback(value: bool) -> None:
        if value:
            typer.echo(f"{name} {__version__}")
            raise typer.Exit()

    return callback
