"""CLI entry points for Djinn in a Box."""

from collections.abc import Callable

import typer

from djinn_in_a_box import __version__
from djinn_in_a_box.core.console import console


def version_callback(name: str) -> Callable[[bool], None]:
    """Create a --version callback for a Typer CLI app."""

    def callback(value: bool) -> None:
        if value:
            console.print(
                f"[primary.bold]{name}[/primary.bold] "
                f"[secondary]{__version__}[/secondary]"
            )
            raise typer.Exit()

    return callback
