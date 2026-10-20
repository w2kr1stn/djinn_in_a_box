"""Host terminal controls for the bounded helper relay."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import Annotated, Any, NoReturn, cast

import typer
from rich.markup import escape
from rich.text import Text

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.core import hostctl
from djinn_in_a_box.core.console import console, error, escape_nonprintable
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
    console.print(
        "Relay waits for enrollment, peer trust, sealing, direct-route probe and journal readiness."
    )
    console.print(
        "Once status reports open, "
        "djinn hostctl check shows the SSH approval link in this terminal."
    )
    if allow_unsealed:
        console.print(
            "Sealing override recorded; dev host authority makes the window an operating aid only."
        )


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


@app.command("check")
def check(
    host: Annotated[
        str | None,
        typer.Argument(metavar="[HOST]", help="Declared alias of the current opening; default all"),
    ] = None,
) -> None:
    """Connect once per declared host from this terminal so the SSH approval link shows here."""

    def report(line: str) -> None:
        console.print(Text(line))

    try:
        code = hostctl.check_hosts(host, report)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # The refusal may echo HOST, which must never be read as markup.
        error(escape(str(exc)))
        raise typer.Exit(1) from None
    raise typer.Exit(code)


SUMMARY_CAP = 3


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun} group" + ("" if count == 1 else "s")


def _time_left(window: dict[str, Any] | None, now: datetime) -> str:
    try:
        deadline = datetime.fromisoformat(str((window or {})["deadline"]))
    except (KeyError, ValueError):
        return "time left unknown"
    if deadline.tzinfo is None:
        return "time left unknown"
    seconds = (deadline.astimezone(UTC) - now.astimezone(UTC)).total_seconds()
    if seconds <= 0:
        return "deadline passed"
    if seconds < 60:
        return "less than 1 min left"
    return f"about {int(seconds // 60)} min left"


def _capped(label: str, groups: tuple[str, ...]) -> list[str]:
    lines = [f"  {label}: {escape_nonprintable(group)}" for group in groups[:SUMMARY_CAP]]
    if len(groups) > SUMMARY_CAP:
        lines.append(f"  {label}: +{len(groups) - SUMMARY_CAP} more")
    return lines


def _group_tuple(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (tuple, list)):
        return ()
    return tuple(str(item) for item in cast(list[Any], raw))


def summary_lines(value: dict[str, Any], *, now: datetime) -> tuple[str, ...]:
    """Pure snapshot-to-lines summary: no probe, no clock, no state; `now` is supplied."""
    absent = value.get("helper") == "absent"
    gen = value.get("generation")
    gen = gen if isinstance(gen, str) and gen else None
    running_raw = value.get("running")
    running = running_raw is True
    state = value.get("state")
    node_raw = value.get("node")
    node = cast(dict[str, Any], node_raw) if isinstance(node_raw, dict) else None
    # Hard reasons always mean "unknown"; soft ones only while the helper is still starting.
    hard: list[str] = []
    soft: list[str] = []
    present = not absent
    if present and (gen is None or not isinstance(running_raw, bool)):
        hard.append("helper identity")
    window_raw = value.get("window")
    window = cast(dict[str, Any], window_raw) if isinstance(window_raw, dict) else None
    if window is not None and (gen is None or window.get("generation") != gen):
        hard.append("window mismatch")
        window = None
    if "window_error" in value:
        hard.append("window_error")
    if "relay_error" in value:
        hard.append("relay_error")
    paused = window is not None and window.get("paused") is True
    closed_on_disk = window is not None and window.get("closed") is True
    relay_text = str(value.get("relay", ""))
    if present and running:
        observation_raw = value.get("observation")
        observation = (
            cast(dict[str, Any], observation_raw) if isinstance(observation_raw, dict) else {}
        )
        if not observation:
            soft.append("observation")
        elif value.get("observation_gap") or observation.get("error"):
            hard.append("observation")
        if "node_error" in value or node_raw is None:
            soft.append("node")
        elif node is None:
            hard.append("node")

    # Window state.
    reason: str | None = None
    if absent:
        word, reason = "closed", "helper absent"
        if state != "closed":
            hard.append("state")
    elif not running:
        word = "closed"
        text = (window or {}).get("reason")
        reason = escape_nonprintable(text) if isinstance(text, str) and text else "helper stopped"
    elif state == "open":
        contradicted = paused or closed_on_disk or not relay_text.startswith("open")
        word = "unknown" if contradicted else "open"
    elif state == "opening":
        word = "closed" if closed_on_disk else "paused" if paused else "opening"
        text = (window or {}).get("reason")
        if closed_on_disk and isinstance(text, str) and text:
            reason = escape_nonprintable(text)
    else:
        word = "unknown"
    if word == "unknown":
        hard.append("window state")
    if "helper identity" in hard:
        word, reason = "unknown", None

    trust_raw = value.get("trust")
    trust = cast(dict[str, Any], trust_raw) if isinstance(trust_raw, dict) else {}
    peers_raw = trust.get("peers")
    peers = cast(dict[str, Any], peers_raw) if isinstance(peers_raw, dict) else {}
    current_trust = bool(gen and trust.get("generation") == gen and peers)
    # Admission writes the frozen trust first, so an admitted window (open or paused) needs it.
    admitted = word == "open" or (window is not None and window.get("admission") is True)
    if admitted and not current_trust:
        hard.append("trust")

    authurl = bool(node and node.get("AuthURL"))
    needs_login = bool(running and node and node.get("BackendState") == "NeedsLogin")
    if needs_login and not authurl:
        hard.append("login link")
    incomplete = bool(hard) or (bool(soft) and state != "opening")

    # Relay.
    if "relay_error" in value or word == "unknown":
        relay = "unknown"
    elif word == "closed":
        relay = "closed"
    elif word == "paused":
        relay = "paused"
    elif word == "open":
        relay = "open"
    elif relay_text.startswith("closed pending readiness"):
        relay = "pending"
    elif relay_text.startswith("closed"):
        relay = "closed"
    else:
        relay = "unknown"
    if relay == "unknown" and not incomplete:
        incomplete = True

    # Window line.
    parts = [word + (f" ({reason})" if reason else "")]
    if running:
        parts.append("time left unknown" if "window_error" in value else _time_left(window, now))
    qualifiers: list[str] = []
    if "window_error" in value:
        qualifiers.append("window state unavailable")
    if running and ("node" in hard or "node" in soft):
        qualifiers.append("node state unknown")
    if relay == "unknown":
        qualifiers.append("admission unknown")
    if running and ("observation" in hard or "observation" in soft):
        qualifiers.append("observation gap")
    window_line = "Window: " + "; ".join([*parts, *qualifiers])

    # Sealing.
    sealing = value.get("sealing")
    sealing = sealing if sealing in ("sealed", "deferred", "unsealed", "unknown") else "unknown"
    errors = _group_tuple(value.get("sealing_errors"))
    causes = _group_tuple(value.get("sealing_causes"))
    if sealing == "sealed":
        sealing_line = "Sealing: sealed"
    elif sealing == "deferred":
        sealing_line = "Sealing: deferred (no dev to assess)"
    else:
        counts = [_plural(len(causes), "cause")] if causes else []
        if sealing == "unknown" and errors:
            counts.insert(0, _plural(len(errors), "unknown"))
        sealing_line = f"Sealing: {sealing}" + (f" ({', '.join(counts)})" if counts else "")

    # Peers: only for the current generation.
    if current_trust:
        names = ", ".join(escape_nonprintable(alias) for alias in peers)
        peer_text = f"{names} ({len(peers)}; frozen for this generation)"
    else:
        peer_text = "unknown"

    # Next: first matching rule.
    if incomplete:
        action = "djinn doctor"
    elif needs_login:
        action = "open the login link above in your own browser"
    elif word == "paused":
        action = "wait for the dev start"
    elif word == "opening":
        action = "wait and rerun djinn hostctl status"
    elif word == "open":
        action = "djinn hostctl check, then agents can use ssh <alias>"
    elif sealing == "unknown":
        action = "djinn doctor first"
    elif sealing == "unsealed":
        action = "djinn hostctl on --allow-unsealed (only if you accept the unsealed boundary)"
    else:
        action = "djinn hostctl on"
    return (
        window_line,
        sealing_line,
        *_capped("Unknown", errors),
        *_capped("Cause", causes),
        f"Relay: {relay}; peers: {peer_text}",
        f"Next: {action}",
    )


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
    for key in ("window_error", "node_error", "relay_error"):
        if key in value:
            console.print(Text(f"Unknown: {value[key]}"))
    observation = value.get("observation")
    if not observation or value.get("observation_gap"):
        console.print("Observation: unavailable; journal/log gaps are unknown.")
    elif observation.get("error"):
        console.print(Text(f"Observation gap: {observation['error']}"))
    console.print(Text(f"Sealing: {value['sealing']}; relay: {value['relay']}"))
    grouped = (*value.get("sealing_causes", ()), *value.get("sealing_errors", ()))
    details = (*value.get("sealing_cause_details", ()), *value.get("sealing_error_details", ()))
    for cause in grouped:
        console.print(Text(cause))
    if grouped != details:
        console.print("djinn doctor lists each item.")
    console.print()
    console.print("Summary")
    for line in summary_lines(value, now=datetime.now(UTC)):
        console.print(Text(line))
