"""Shared exclusion and fail-closed discovery for offline host operations."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

import typer
from rich.markup import escape

from djinn_in_a_box.core import host_runtime, hostctl
from djinn_in_a_box.core.console import error, escape_nonprintable
from djinn_in_a_box.core.docker import get_running_containers
from djinn_in_a_box.core.ssh_delivery import GitSSHError


def guard_no_containers_running(
    operation: str = "backup/restore", *, prefix: str = "djinn"
) -> None:
    running = get_running_containers(prefix=prefix)
    if running is None:
        error("Could not determine whether Djinn containers are running.")
        error(f"Restore Docker access before {operation}, then retry.")
        raise typer.Exit(1)
    if running:
        error(f"Containers are running: {', '.join(running)}")
        error(f"Cannot proceed with {operation}.")
        error("Stop all containers first: `djinn clean`")
        raise typer.Exit(1)


@contextmanager
def lifecycle_guard() -> Iterator[None]:
    with ExitStack() as guards:
        try:
            guards.enter_context(hostctl.control_guard())
            guards.enter_context(host_runtime.creation_guard())
        except (hostctl.HostctlError, GitSSHError, OSError) as exc:
            error(escape(escape_nonprintable(str(exc))))
            raise typer.Exit(1) from exc
        yield
