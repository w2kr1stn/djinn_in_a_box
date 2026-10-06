"""Installation logs."""

from __future__ import annotations

import subprocess
from typing import Annotated

import typer

from djinn_in_a_box.core.console import blank, err_console, error, rule
from djinn_in_a_box.core.docker import is_container_running
from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE

app = typer.Typer(help="Show installation logs.", no_args_is_help=True)


@app.command()
def proxy(
    tail: Annotated[int, typer.Option("--tail", "-n", help="Number of log lines to show")] = 50,
) -> None:
    """Show Docker proxy audit log."""
    if not is_container_running("djinn-docker-proxy"):
        error("Docker Proxy is not running.")
        err_console.print("Start with: djinn start --docker")
        raise typer.Exit(1)
    rule(f"Docker Proxy Audit Log (last {tail} lines):")
    blank()
    result = subprocess.run(
        [DOCKER_EXECUTABLE, "logs", "--tail", str(tail), "djinn-docker-proxy"],
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise typer.Exit(result.returncode)
