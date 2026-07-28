"""Diagnostics — ``djinn doctor`` health checks + a fast preflight.

``doctor()`` is the full, report-only diagnostic (PASS/FAIL/WARN + remedy per
check). ``preflight()`` is the fast critical subset that auto-runs before
``build``/``start``: it refuses with a friendly message if Docker is unusable,
then provisions the host bind-mount sources (``ensure_host_env``) unless the
caller opts out with ``provision_host=False``, as ``start`` does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table
from rich.text import Text

from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.config_sync import audit_config_sync as audit_workflow_config
from djinn_in_a_box.core.console import blank, console, error, rule, warning
from djinn_in_a_box.core.docker import (
    DJINN_NETWORK,
    ensure_host_env,
    ensure_network,
    get_config_root,
    get_dbus_mount_args,
    network_exists,
)
from djinn_in_a_box.core.exceptions import ConfigNotFoundError, ConfigValidationError
from djinn_in_a_box.core.paths import CONFIG_FILE, get_project_root
from djinn_in_a_box.core.seeding import SEED_MANIFEST, SeedingError, seed_config

_IMAGE: str = "djinn-in-a-box:latest"


class Status(Enum):
    """Outcome of a single diagnostic check."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


@dataclass(frozen=True, slots=True)
class Check:
    """A single diagnostic result."""

    name: str
    status: Status
    detail: str
    remedy: str = ""


# -----------------------------------------------------------------------------
# Low-level probes (each degrades to a boolean; never raises)
# -----------------------------------------------------------------------------
def _docker_installed() -> bool:
    return shutil.which("docker") is not None


def _command_ok(args: list[str]) -> bool:
    try:
        result = subprocess.run(args, capture_output=True, check=False)
    except (FileNotFoundError, PermissionError):
        return False
    return result.returncode == 0


def docker_daemon_ok() -> bool:
    """True if the Docker daemon is reachable."""
    return _command_ok(["docker", "info"])


def _docker_socket_ok() -> bool:
    """True unless /var/run/docker.sock exists but is not accessible (permission)."""
    sock = Path("/var/run/docker.sock")
    if not sock.exists():
        return True  # absence is the daemon check's concern, not a permission issue
    return os.access(sock, os.R_OK | os.W_OK)


def compose_v2_ok() -> bool:
    """True if Docker Compose v2 is available (``docker compose``)."""
    return _command_ok(["docker", "compose", "version"])


def _image_built() -> bool:
    return _command_ok(["docker", "image", "inspect", _IMAGE])


def _docker_mcp_ok() -> bool:
    return _command_ok(["docker", "mcp", "--help"])


def _old_sync_root_present(config: AppConfig | None) -> bool:
    """True if a populated legacy ~/.djinn/sync exists while the new root is empty.

    Covers the no-migration rename: the user's old credentials live under the
    legacy root and would be silently orphaned otherwise.
    """
    legacy = Path.home() / ".djinn" / "sync"
    try:
        legacy_populated = legacy.is_dir() and any(legacy.iterdir())
    except OSError:
        # An unreadable (e.g. root-owned) legacy dir is itself the strongest signal
        # the migration hint is needed — degrade to "present", never raise. A
        # diagnostic must not crash on the condition it exists to diagnose.
        return True
    if not legacy_populated:
        return False

    new_root = get_config_root(config)
    try:
        return not new_root.is_dir() or not any(new_root.iterdir())
    except OSError:
        # A new-root access problem is not a legacy-migration signal — don't emit
        # the misleading "mv ~/.djinn/sync" remedy for it.
        return False


def _seed_target_has_expected_type(path: Path, kind: str) -> bool:
    if kind == "file":
        return path.is_file()
    return path.is_dir()


def _missing_seed_targets(project_root: Path) -> list[str]:
    missing: list[str] = []
    for entry in SEED_MANIFEST:
        target = project_root / entry.target
        if not _seed_target_has_expected_type(target, entry.kind):
            missing.append(entry.target.as_posix())
    return missing


def _config_workflow_check(config: AppConfig | None) -> Check:
    if config is None:
        return Check(
            "Config workflow",
            Status.WARN,
            "not checked without valid configuration",
            "Fix the Configuration check, then run `djinn config status`.",
        )
    try:
        project_root = get_project_root()
        audit = audit_workflow_config(project_root)
    except (
        ConfigNotFoundError,
        ConfigValidationError,
        FileNotFoundError,
        OSError,
        ValueError,
    ) as exc:
        return Check(
            "Config workflow",
            Status.WARN,
            f"audit unavailable ({type(exc).__name__})",
            "Run from the Djinn repo, then run `djinn config status`.",
        )
    source = audit.configured_source
    if audit.clean:
        return Check("Config workflow", Status.PASS, f"source={source}; clean")
    drift = ",".join(item.kind.value for item in audit.drifts) or "validation-problem"
    return Check(
        "Config workflow",
        Status.WARN,
        f"source={source}; drift={drift}",
        "Run `djinn config status`, then `djinn config sync` when ready.",
    )


# -----------------------------------------------------------------------------
# Check assembly
# -----------------------------------------------------------------------------
def run_checks(config: AppConfig | None, config_error: str | None = None) -> list[Check]:
    """Run every diagnostic and return the results (no side effects)."""
    checks: list[Check] = []

    installed = _docker_installed()
    checks.append(
        Check(
            "Docker installed",
            Status.PASS if installed else Status.FAIL,
            "found on PATH" if installed else "not found",
            "" if installed else "Install Docker: https://docs.docker.com/engine/install/",
        )
    )

    daemon = installed and docker_daemon_ok()
    checks.append(
        Check(
            "Docker daemon",
            Status.PASS if daemon else Status.FAIL,
            "running" if daemon else "not reachable",
            "" if daemon else "Start the Docker daemon (e.g. `sudo systemctl start docker`).",
        )
    )

    socket_ok = (not installed) or _docker_socket_ok()
    checks.append(
        Check(
            "Docker socket",
            Status.PASS if socket_ok else Status.FAIL,
            "accessible" if socket_ok else "/var/run/docker.sock not accessible",
            ""
            if socket_ok
            else "Add your user to the 'docker' group (then re-login), or fix socket permissions.",
        )
    )

    compose = installed and compose_v2_ok()
    checks.append(
        Check(
            "Compose v2",
            Status.PASS if compose else Status.FAIL,
            "available" if compose else "`docker compose` not available",
            "" if compose else "Install the Docker Compose v2 plugin.",
        )
    )

    if config_error is not None:
        # config.toml exists but failed to parse/validate — surface the first concrete
        # field error (the last non-empty line), not the generic header line.
        detail_line = next(
            (ln.strip() for ln in reversed(config_error.splitlines()) if ln.strip()),
            config_error,
        )
        checks.append(
            Check(
                "Configuration",
                Status.FAIL,
                "present but invalid",
                f"Fix config.toml, then re-run `djinn doctor`. ({detail_line})",
            )
        )
    elif CONFIG_FILE.exists():
        checks.append(Check("Configuration", Status.PASS, str(CONFIG_FILE)))
    else:
        checks.append(
            Check(
                "Configuration",
                Status.FAIL,
                "missing",
                "Run `djinn init` to create the configuration.",
            )
        )

    checks.append(_config_workflow_check(config))

    if config is not None:
        code_ok = config.code_dir.is_dir()
        checks.append(
            Check(
                "Projects dir",
                Status.PASS if code_ok else Status.FAIL,
                str(config.code_dir),
                "" if code_ok else "Create the directory or fix `code_dir` in config.toml.",
            )
        )
        root = get_config_root(config)
        root_ok = root.is_dir()
        checks.append(
            Check(
                "Config root",
                Status.PASS if root_ok else Status.WARN,
                str(root),
                "" if root_ok else "Run `djinn init` (it provisions the config root).",
            )
        )

    image = daemon and _image_built()
    checks.append(
        Check(
            "Image built",
            Status.PASS if image else Status.WARN,
            f"{_IMAGE} present" if image else "not built",
            "" if image else "Run `djinn build`.",
        )
    )

    net = daemon and network_exists(DJINN_NETWORK)
    checks.append(
        Check(
            "Network",
            Status.PASS if net else Status.WARN,
            DJINN_NETWORK if net else "missing",
            "" if net else "Created automatically by `djinn start`.",
        )
    )

    mcp = daemon and _docker_mcp_ok()
    checks.append(
        Check(
            "docker mcp (optional)",
            Status.PASS if mcp else Status.WARN,
            "installed" if mcp else "not installed",
            "" if mcp else "Optional: install the Docker MCP plugin to use `mcpgateway`.",
        )
    )

    if _old_sync_root_present(config):
        checks.append(
            Check(
                "Legacy sync root",
                Status.WARN,
                "~/.djinn/sync is populated but the new config root is empty",
                "Move it once: `mv ~/.djinn/sync ~/.djinn/config` "
                "(DJINN_SYNC_ROOT was renamed to DJINN_CONFIG_ROOT).",
            )
        )

    dbus_available = bool(get_dbus_mount_args())
    checks.append(
        Check(
            "D-Bus session",
            Status.PASS,
            "desktop notifications available"
            if dbus_available
            else "not detected — desktop notifications off",
        )
    )

    try:
        project_root = get_project_root()
    except FileNotFoundError:
        checks.append(
            Check(
                "Seed config",
                Status.WARN,
                "Djinn repo could not be located; seed status unknown",
                "Run from a clone of the Djinn repo.",
            )
        )
    else:
        missing_seed_targets = _missing_seed_targets(project_root)
        checks.append(
            Check(
                "Seed config",
                Status.WARN if missing_seed_targets else Status.PASS,
                "missing: " + ", ".join(missing_seed_targets)
                if missing_seed_targets
                else "all seed targets present",
                "run `djinn init` (or `djinn doctor --fix`)." if missing_seed_targets else "",
            )
        )

    return checks


_STYLE: dict[Status, str] = {
    Status.PASS: "status.enabled",
    Status.WARN: "status.disabled",
    Status.FAIL: "status.error",
}
_GLYPH: dict[Status, str] = {Status.PASS: "✓", Status.WARN: "⚠", Status.FAIL: "✗"}
_LABEL: dict[Status, str] = {Status.PASS: "PASS", Status.WARN: "WARN", Status.FAIL: "FAIL"}


def _doctor_fix(config: AppConfig) -> bool:
    """Run idempotent doctor repairs. Returns True if any repair failed."""
    failed = False

    try:
        project_root = get_project_root()
    except FileNotFoundError as e:
        failed = True
        console.print(f"Could not fix: seed configuration (run from the Djinn repo: {e})")
    else:
        try:
            seed_config(project_root, source=config.config_sync.source)
            console.print("Fixed: seed configuration")
        except SeedingError as e:
            failed = True
            console.print(f"Could not fix: seed configuration ({e})")
        except PermissionError as e:
            failed = True
            console.print(f"Could not fix: seed configuration ({e})")
            console.print(
                f'Fix ownership with `sudo chown -R "$(id -u):$(id -g)" '
                f"{project_root / 'config'}`, then retry."
            )
        except OSError as e:
            failed = True
            console.print(
                f"Could not fix: seed configuration (check project config paths are writable: {e})"
            )

    try:
        ensure_host_env(config)
        console.print("Fixed: host environment")
    except OSError as e:
        failed = True
        console.print(f"Could not fix: host environment (check host paths are writable: {e})")

    try:
        network_ok = ensure_network()
    except OSError as e:
        network_ok = False
        network_error = str(e)
    else:
        network_error = ""

    if network_ok:
        console.print("Fixed: Docker network")
    else:
        failed = True
        remedy = "start Docker and retry"
        if network_error:
            remedy = f"{remedy}: {network_error}"
        console.print(f"Could not fix: Docker network ({remedy})")

    return failed


def doctor(
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Run idempotent repairs after reporting."),
    ] = False,
) -> None:
    """Diagnose the Djinn environment (report-only).

    Reports PASS/WARN/FAIL for Docker, Compose, configuration, the projects
    directory, the config root, the image, the network, and the optional MCP
    plugin — each with a remedy. Exits non-zero if any hard check fails.
    """
    from djinn_in_a_box.config.loader import load_config

    # Distinguish "missing" from "present but invalid" so the diagnostic is truthful:
    # a malformed config must report FAIL, never a misleading PASS.
    config: AppConfig | None = None
    config_error: str | None = None
    try:
        config = load_config()
    except ConfigNotFoundError:
        config = None  # the Configuration check reports the missing file
    except ConfigValidationError as e:
        config = None
        config_error = str(e)

    rule("Djinn Doctor")

    checks = run_checks(config, config_error)
    table = Table(
        title="Djinn Doctor",
        title_style="table.title",
        header_style="table.header",
        border_style="border",
    )
    table.add_column("Check", style="table.category")
    table.add_column("Status")
    table.add_column("Detail", style="table.value")
    table.add_column("Remedy", style="muted")
    for check in checks:
        glyph = _GLYPH[check.status]
        detail = (
            Text(check.detail, style="path")
            if check.name in {"Projects dir", "Config root"}
            else check.detail
        )
        table.add_row(
            check.name,
            Text(f"{glyph} {_LABEL[check.status]}", style=_STYLE[check.status]),
            detail,
            check.remedy,
        )
    console.print(table)

    blank()
    failed = [c for c in checks if c.status is Status.FAIL]
    if failed:
        error(f"{len(failed)} check(s) failed.")
        if not fix:
            raise typer.Exit(1)

    if not fix:
        return

    if config is None:
        if config_error is not None:
            error("Fix config.toml first (see the Configuration check above)")
        else:
            error("Run `djinn init` first")
        raise typer.Exit(1)

    fix_failed = _doctor_fix(config)
    if failed or fix_failed:
        raise typer.Exit(1)


def preflight(config: AppConfig, *, provision_host: bool = True) -> None:
    """Fast critical preflight before build/start.

    Verifies Docker is usable first (cheap read-only probes), then provisions the
    host bind-mount sources — so a Docker-down failure leaves no provisioning
    artifacts behind. Raises ``typer.Exit(1)`` with a friendly, actionable message
    on hard failure.
    """
    if not _docker_installed():
        error("Docker is not installed or not on PATH.")
        warning("Install Docker: https://docs.docker.com/engine/install/")
        raise typer.Exit(1)
    if not docker_daemon_ok():
        error("The Docker daemon is not reachable.")
        warning("Start it (e.g. `sudo systemctl start docker`), then retry. Run `djinn doctor`.")
        raise typer.Exit(1)

    if not provision_host:
        return

    # Docker is usable → now provision the (side-effecting) host bind-mount sources.
    try:
        ensure_host_env(config)
    except OSError as e:
        error(f"Failed to provision host directories: {e}")
        warning("Check that your home and config-root paths are writable, then retry.")
        raise typer.Exit(1) from e
