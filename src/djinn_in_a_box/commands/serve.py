"""Start, inspect, stop and pair the container's managed agent servers."""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404
from typing import Annotated, Any

import typer
from rich.table import Table
from rich.text import Text

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.core import serve as core
from djinn_in_a_box.core.console import (
    console,
    error,
    escape_nonprintable,
    info,
    print_captured,
    success,
)
from djinn_in_a_box.core.decorators import handle_config_errors


def _literal(message: str) -> None:
    print_captured(escape_nonprintable(message), end="\n")


def _gate(container: core.ContainerInfo, *, detached: bool) -> bool:
    if container.state == core.ContainerState.UNKNOWN:
        error("Could not determine whether a Djinn container is running.")
        info("Restore Docker access, then retry.")
        raise typer.Exit(1)
    if container.state == core.ContainerState.ABSENT:
        return False
    if detached and not container.detached:
        error("The running Djinn container was not started with --detach.")
        info("Recreate it: djinn clean, then djinn start --detach")
        raise typer.Exit(1)
    return True


def _opencode_preflight(container: core.ContainerInfo, port: int) -> tuple[str | None, str, bool]:
    if not core.check_opencode_port(container, port):
        error(f"The running container does not publish the OpenCode port on 127.0.0.1:{port}.")
        info(
            "It was created before this feature or with another serve.opencode_host_port. "
            "Recreate it: djinn clean, then djinn start --detach"
        )
        raise typer.Exit(1)
    core.check_opencode_roots(container)
    core.check_opencode_networks(container)
    core.engine_supports_loopback_isolation()
    tailnet = core.tailnet_state(port)
    if tailnet[2]:
        error(
            "Tailscale Funnel is enabled for the OpenCode port; refusing "
            "(it would be reachable from the internet)."
        )
        info("Turn Funnel off for that entry, then retry.")
        raise typer.Exit(1)
    return tailnet


def _helper_failure(name: str, failure: core.ServeError) -> None:
    if failure.code == "busy":
        error("Another djinn serve start or stop is running; retry.")
    elif failure.code in ("exec-timeout", "timeout"):
        text = (
            "helper timed out; the server state is unknown"
            if failure.code == "exec-timeout"
            else "the helper ran out of time; the server state is unknown"
        )
        error(f"{name}: {text}")
        info("Check with: djinn serve --status")
    elif failure.code == "not-registered":
        error("opencode: another OpenCode service registered itself during start; retry")
    elif failure.code == "remote-config":
        error(
            "OpenCode remote access (OpenTunnel) is enabled in its service settings; refusing. "
            "Remove it in the container with: opencode service unset remote"
        )
    elif failure.code == "route-localnet":
        _literal(
            "Error: OpenCode requires route_localnet=0 on all container interfaces; refusing. "
            "Disable loopback routing in the container and endpoint sysctls, "
            "then recreate the container."
        )
    elif failure.code == "service-config":
        keys = re.findall(r"service.json contains '([^']+)'", str(failure))
        if keys:
            for key in keys:
                _literal(
                    f"Error: OpenCode service settings contain {key}, which djinn serve "
                    "cannot vouch for; refusing. Remove it in the container with: "
                    f"opencode service unset {key}"
                )
        else:
            error("OpenCode service settings contain unsupported keys; refusing.")
            _literal(str(failure))
    elif failure.code == "env-root":
        name_match = re.search(
            r"HOME|XDG_CONFIG_HOME|XDG_STATE_HOME|OPENCODE_CONFIG_DIR", str(failure)
        )
        selector = name_match.group() if name_match else "unknown root"
        _literal(
            f"Error: The container environment relocates OpenCode's settings "
            f"({selector}); refusing."
        )
    elif failure.code == "state-path":
        prefix = "OpenCode's settings path contains a symbolic link: "
        message = str(failure)
        if message.startswith(prefix):
            message = (
                "OpenCode's settings path contains a symbolic link "
                f"({message.removeprefix(prefix)}); refusing."
            )
        _literal(f"Error: {message}")
    else:
        # state-dir/password/service-json/spawn/usage/protocol all retain literal diagnostics.
        error(f"{name}: helper failed")
        diagnostic = failure.detail if failure.code == "protocol" else str(failure)
        if diagnostic:
            _literal(diagnostic)


def _status_table(snapshot: core.Snapshot) -> None:
    if snapshot.busy:
        info("busy (start/stop in progress)")
    table = Table()
    for column in ("Name", "State", "Since", "Endpoint / hint", "Last error"):
        table.add_column(column)
    for name in core.NAMES:
        row = snapshot.servers[name]
        state = row["state"]
        if state == "degraded":
            state = f"degraded ({row['reason']})"
        if name == core.OPENCODE:
            endpoint = (
                f"http://127.0.0.1:{snapshot.port}"
                if snapshot.published
                else "port not published (recreate once)"
            )
            endpoint += (
                f"\ntailnet: {snapshot.url}"
                if snapshot.url
                else f"\ntailnet: unknown ({snapshot.reason})"
            )
            endpoint += (
                f"\nrelay refused since start: {row['relay_refused']}"
                if row["relay_refused"] is not None
                else f"\nrelay refused: unknown ({row['relay_note']})"
            )
            if row["foreign_listener"]:
                endpoint += (
                    "\nnote: port 4096 is used by another process; possibly "
                    "a djinn serve from before the update — recreate the container"
                )
            if snapshot.funnel:
                endpoint += "\n" + core.FUNNEL_WARNING
        else:
            endpoint = "Remote Control: see the Claude apps"
        # Each cell is data; escaped controls cannot become terminal instructions or markup.
        table.add_row(
            *(
                Text(escape_nonprintable(str(value)))
                for value in (
                    name,
                    state,
                    row["since"] or "",
                    endpoint,
                    row["last_line"] or "" if row["state"] == "exited" else "",
                )
            )
        )
    console.print(table)


def _start_result(
    name: str, result: dict[str, Any], port: int, tailnet: tuple[str | None, str, bool]
) -> bool:
    outcome, pid = result["result"], result["pid"]
    if outcome == "exited":
        error(f"{name}: exited during startup")
        for line in result["log_tail"]:
            _literal(line)
        return False
    if outcome == "already-running":
        info(f"{name}: already running (pid {pid})")
    elif outcome == "restarted" and name == core.OPENCODE:
        success(f"opencode: restarted (pid {pid})")
    else:
        success(f"{name}: started (pid {pid})")
    if name == core.OPENCODE:
        info(f"Local URL: http://127.0.0.1:{port}")
        url, reason, _ = tailnet
        _literal(f"Tailnet URL: {url}" if url else f"No tailnet URL: {reason}")
        _literal(f"Password file: {core.password_path()}")
        if result["stopped_previous"]:
            info("The previous OpenCode service was stopped; attached clients must reconnect.")
    return True


@handle_config_errors
def serve(
    names: Annotated[list[str] | None, typer.Argument(help="Servers: claude, opencode")] = None,
    status: Annotated[bool, typer.Option("--status", help="Show server states.")] = False,
    stop: Annotated[bool, typer.Option("--stop", help="Stop named servers, or all.")] = False,
    pair: Annotated[
        str | None, typer.Option("--pair", help="Pair OpenCode via the tailnet.")
    ] = None,
) -> None:
    """Serve coding agents inside the running detached Djinn container."""
    selected = list(dict.fromkeys(names or []))
    if sum((status, stop, pair is not None)) > 1 or (selected and (status or pair is not None)):
        error("Choose one mode: start, --status, --stop or --pair.")
        raise typer.Exit(1)
    if pair is not None and pair != core.OPENCODE:
        error("Pairing is only available for opencode.")
        raise typer.Exit(1)
    for name in selected:
        if name not in core.NAMES:
            _literal(f"Error: Unknown server '{name}'. Choose from: claude, opencode")
            raise typer.Exit(1)
    if not selected and not (status or stop or pair is not None):
        error(
            "Nothing to do. Use: djinn serve <claude|opencode ...>, --status, "
            "--stop [NAME ...] or --pair opencode"
        )
        raise typer.Exit(1)
    config = load_config()
    port = config.serve.opencode_host_port
    try:
        if status:
            snapshot = core.collect_snapshot(config)
            if not _gate(snapshot.container, detached=False):
                info("No running Djinn container.")
            elif not snapshot.container.detached:
                info("The running Djinn container was not started with --detach.")
            else:
                _status_table(snapshot)
            return
        container = core.detect_container()
        if not _gate(container, detached=not stop):
            if stop:
                info("No running Djinn container; nothing to stop.")
                return
            error("No running Djinn container found.")
            info("Start one with: djinn start --detach")
            raise typer.Exit(1)
        if stop:
            failed = False
            for name in selected or core.NAMES:
                try:
                    data = core.exec_helper(container.id, ["stop", "--name", name])
                    row = data["results"][name]
                    if row["result"] == "survived":
                        error(f"{name}: could not be stopped (pid {row['pid']})")
                        failed = True
                    elif row["result"] == "stopped":
                        success(f"{name}: stopped")
                        if name == core.OPENCODE:
                            info(
                                "OpenCode returns to its loopback default service "
                                "when a client next needs it."
                            )
                    else:
                        info(f"{name}: not running")
                except core.ServeError as failure:
                    _helper_failure(name, failure)
                    failed = True
            if failed:
                raise typer.Exit(1)
            return
        if pair is None:
            core.check_process_state(container)
        tailnet: tuple[str | None, str, bool] = (None, "not queried", False)
        if core.OPENCODE in selected or pair is not None:
            tailnet = _opencode_preflight(container, port)
        if pair is not None:
            data = core.exec_helper(container.id, ["status"], timeout=15)
            if data["servers"][core.OPENCODE]["state"] != "running":
                error("opencode is not running; start it with: djinn serve opencode")
                raise typer.Exit(1)
            url, reason, _ = tailnet
            if url is None:
                _literal(f"Error: No tailnet URL: {reason}")
                info(
                    f"Set up a Tailscale Serve entry for http://127.0.0.1:{port} "
                    "(see README), then retry."
                )
                raise typer.Exit(1)
            try:
                # Inspected ID, fixed argv, validated URL; output remains on the terminal.
                result = subprocess.run(  # nosec B603
                    [
                        core.DOCKER_EXECUTABLE,
                        "exec",
                        container.id,
                        "opencode",
                        "pair",
                        "--url",
                        url,
                    ],
                    stdin=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                error("opencode: pairing failed")
                raise typer.Exit(1) from None
            raise typer.Exit(result.returncode)
        # All gates for all names complete before any start (including password creation).
        if core.OPENCODE in selected:
            core.exec_helper(container.id, ["opencode-preflight"], timeout=15)
        password = core.ensure_password(config, container) if core.OPENCODE in selected else None
        if core.OPENCODE in selected and tailnet[0] is None:
            _literal(f"Funnel state unknown ({tailnet[1]})")
        failed = False
        for name in selected:
            args = (
                ["claude-start", "--prefix", core.claude_prefix_slug()]
                if name == core.CLAUDE
                else ["opencode-start", "--port", str(core.OPENCODE_CONTAINER_PORT)]
            )
            if name == core.OPENCODE and tailnet[0]:
                args += ["--cors", tailnet[0]]
            stdin = json.dumps({"password": password}) + "\n" if name == core.OPENCODE else None
            try:
                data = core.exec_helper(container.id, args, stdin=stdin)
                if not _start_result(name, data, port, tailnet):
                    failed = True
                    break
            except core.ServeError as failure:
                _helper_failure(name, failure)
                failed = True
                break
        if failed:
            raise typer.Exit(1)
    except core.ServeError as failure:
        if failure.code == "gate":
            _literal(f"Error: {failure}")
        else:
            _helper_failure(core.OPENCODE if pair is not None else "serve", failure)
        raise typer.Exit(1) from None
