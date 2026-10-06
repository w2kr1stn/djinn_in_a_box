"""Host terminal controls; B1 exposes no relay or sealing assessment."""

from __future__ import annotations

import subprocess
from typing import Annotated, Any, NoReturn, cast

import typer
from rich.text import Text

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.core import hostctl
from djinn_in_a_box.core.console import console, error
from djinn_in_a_box.core.decorators import handle_config_errors

app = typer.Typer(help="Control the time-limited host Tailscale helper.", no_args_is_help=True)


def _failure(exc: Exception) -> NoReturn:
    error(str(exc))
    raise typer.Exit(1)


@app.command("on")
@handle_config_errors
def on(
    duration: Annotated[
        str | None, typer.Option("--for", help="Positive <n>m or <n>h; maximum 24h")
    ] = None,
    allow_unsealed: Annotated[bool, typer.Option("--allow-unsealed")] = False,
) -> None:
    config = load_config()
    try:
        hostctl.open_window(config, duration, allow_unsealed=allow_unsealed)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        _failure(exc)
    console.print(
        "Window opening. Login time counts; use djinn hostctl status for login/readiness."
    )
    console.print("B1: sealing unchecked; relay absent, so dev has no helper tailnet route.")


@app.command("off")
def off() -> None:
    try:
        hostctl.close_window()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        _failure(exc)
    console.print("Window closed. Node state retained.")


@app.command("limit")
def limit(minutes: Annotated[int, typer.Argument(min=1, max=1440)]) -> None:
    try:
        value = hostctl.limit_window(minutes)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        _failure(exc)
    console.print(Text(f"Effective deadline: {value['deadline']}"))


@app.command("status")
def status() -> None:
    try:
        value = hostctl.snapshot()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        _failure(exc)
        return
    console.print(Text(f"Window: {value['state']}; helper: {value['helper']}"))
    if "window" in value:
        console.print(Text(f"Deadline (helper-owned): {value['window']['deadline']}"))
    node = value.get("node")
    if isinstance(node, dict):
        node = cast(dict[str, Any], node)
        console.print(Text(f"Node: {node['BackendState']}"))
        url = node.get("AuthURL")
        if url:
            console.print(Text(f"Login on the host: {url}"))
    else:
        console.print(Text(f"Node: {node or 'unknown'}"))
    for key in ("window_error", "node_error"):
        if key in value:
            console.print(Text(f"Unknown: {value[key]}"))
    observation = value.get("observation")
    if not observation or value.get("observation_gap"):
        console.print("Observation: unavailable; journal/log gaps are unknown.")
    elif observation.get("error"):
        console.print(Text(f"Observation gap: {observation['error']}"))
    console.print(Text(f"Sealing: {value['sealing']}; relay: {value['relay']}"))
