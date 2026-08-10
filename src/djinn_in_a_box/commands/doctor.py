"""Diagnostics — ``djinn doctor`` health checks + a fast preflight.

``doctor()`` is the full, report-only diagnostic (PASS/FAIL/WARN + remedy per
check). ``preflight()`` is the fast critical subset that auto-runs before
``build``/``start``: it refuses with a friendly message if Docker is unusable,
then provisions the host bind-mount sources (``ensure_host_env``) unless the
caller opts out with ``provision_host=False``. ``start`` opts out here because
it provisions later through compose workflow preparation instead.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.table import Table
from rich.text import Text

from djinn_in_a_box.config.defaults import KNOWN_CONFIG_ROOT_ENTRIES, SYNC_PATHS
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import (
    MIGRATING_ZONE_PREFIX,
    ZoneAssignment,
    ZoneAssignments,
    load_zone_assignments,
)
from djinn_in_a_box.core.config_sync import audit_config_sync as audit_workflow_config
from djinn_in_a_box.core.console import blank, console, error, rule, warning
from djinn_in_a_box.core.docker import (
    DJINN_NETWORK,
    ZoneRoots,
    ensure_host_env,
    ensure_network,
    get_config_root,
    get_dbus_mount_args,
    network_exists,
    resolve_zone_roots,
)
from djinn_in_a_box.core.exceptions import ConfigNotFoundError, ConfigValidationError
from djinn_in_a_box.core.paths import CONFIG_FILE, get_project_root
from djinn_in_a_box.core.seeding import SEED_MANIFEST, SeedingError, seed_config
from djinn_in_a_box.core.zone_migration import (
    find_unmigrated_assignments,
    find_zone_collisions,
)

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


CREDENTIAL_DIR_MODE = 0o700
"""Intended mode for credential directories under the config root."""

LARGE_NON_OVERLAYABLE_FILE_BYTES: Final = 10 * 1024 * 1024
"""Report individual config-zone files at least this large."""


def loose_credential_dirs(
    config: AppConfig | None,
    assignments: ZoneAssignments | None = None,
) -> list[Path]:
    """Managed credential and zone directories that are group- or other-accessible.

    ``ensure_host_env`` creates these with ``mode=0o700``, but ``Path.mkdir``
    applies a mode only at creation — a config root provisioned before that
    change keeps the umask default. This finds the drift so ``doctor --fix``
    can repair it.

    The audit includes zone roots and the agent/assignment directories Djinn
    creates beneath them. It still uses ``lstat`` so a symlinked name is skipped
    instead of followed — a redirect is someone's deliberate arrangement, not
    ours to chmod.
    """
    roots = resolve_zone_roots(config)
    candidates: list[Path] = [roots.config_root, roots.shared_root, roots.local_root]
    candidates.extend(roots.config_root / name for name in SYNC_PATHS.get("credentials", []))
    if assignments is not None:
        zone_roots = {"local": roots.local_root, "shared": roots.shared_root}
        for agent, by_zone in assignments.by_agent.items():
            for zone in ("local", "shared"):
                root = zone_roots[zone]
                for relative_path in by_zone[zone]:
                    current = root / agent
                    candidates.append(current)
                    for part in relative_path.parts:
                        current /= part
                        candidates.append(current)

    loose: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        if stat.S_IMODE(info.st_mode) & 0o077:
            loose.append(path)
    return loose


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
    """True if a legacy agent has content absent from every current zone.

    Covers the no-migration rename: the user's old credentials live under the
    legacy root and would be silently orphaned otherwise.
    """
    legacy = Path.home() / ".djinn" / "sync"
    try:
        if not legacy.is_dir():
            return False
    except OSError:
        # An unreadable (e.g. root-owned) legacy dir is itself the strongest signal
        # the migration hint is needed — degrade to "present", never raise. A
        # diagnostic must not crash on the condition it exists to diagnose.
        return True

    roots = resolve_zone_roots(config)
    current_roots = (roots.config_root, roots.shared_root, roots.local_root)
    for agent in SYNC_PATHS["credentials"]:
        legacy_agent = legacy / agent
        try:
            legacy_entries = tuple(legacy_agent.iterdir()) if legacy_agent.is_dir() else ()
        except OSError:
            return True
        for legacy_entry in legacy_entries:
            try:
                present = any(
                    _path_has_content(root / agent / legacy_entry.name) for root in current_roots
                )
            except OSError:
                # A new-root access problem is not a legacy-migration signal — don't emit
                # the misleading migration remedy for it.
                return False
            if not present:
                return True
    return False


def _path_has_content(path: Path) -> bool:
    if path.is_symlink() or path.is_file():
        return True
    return path.is_dir() and any(path.iterdir())


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


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PiB"


def _path_size_bytes(path: Path) -> int:
    """Return a path's size without following symlinks; unreadable entries count as zero."""
    try:
        info = path.lstat()
    except OSError:
        return 0
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    if not stat.S_ISDIR(info.st_mode):
        return 0
    total = 0
    try:
        children = tuple(path.iterdir())
    except OSError:
        return 0
    for child in children:
        total += _path_size_bytes(child)
    return total


def _zone_drift_entries(config: AppConfig, assignments: ZoneAssignments) -> tuple[Path, ...]:
    roots = resolve_zone_roots(config)
    drift: list[Path] = []
    for agent, by_zone in assignments.by_agent.items():
        agent_root = roots.config_root / agent
        if not agent_root.is_dir() or agent_root.is_symlink():
            continue
        accounted = set(KNOWN_CONFIG_ROOT_ENTRIES[agent])
        for zone in ("local", "shared"):
            accounted.update(path.parts[0] for path in by_zone[zone])
        try:
            children = tuple(agent_root.iterdir())
        except OSError:
            continue
        drift.extend(
            child
            for child in children
            if child.name not in accounted and not child.name.startswith(MIGRATING_ZONE_PREFIX)
        )
    return tuple(drift)


def _large_non_overlayable_files(config: AppConfig) -> tuple[Path, ...]:
    root = get_config_root(config)
    large_files: list[Path] = []
    for agent in KNOWN_CONFIG_ROOT_ENTRIES:
        agent_root = root / agent
        if not agent_root.is_dir() or agent_root.is_symlink():
            continue
        try:
            children = tuple(agent_root.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                info = child.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_size >= LARGE_NON_OVERLAYABLE_FILE_BYTES:
                large_files.append(child)
    return tuple(large_files)


def _unmigrated_detail(roots: ZoneRoots, assignment: ZoneAssignment) -> str:
    path = roots.config_root / assignment.agent / assignment.relative_path
    return f"{assignment.agent}/{assignment.relative_path} ({_format_size(_path_size_bytes(path))})"


def _skipped_default_detail(roots: ZoneRoots, assignment: ZoneAssignment) -> tuple[str, Path]:
    path = roots.config_root / assignment.agent
    for part in assignment.relative_path.parts:
        path /= part
        if path.is_file():
            break
    return f"{assignment.agent}/{assignment.relative_path} (blocked by {path})", path


def _zone_diagnostic_checks(config: AppConfig, assignments: ZoneAssignments) -> list[Check]:
    roots = resolve_zone_roots(config)
    unmigrated = find_unmigrated_assignments(assignments, roots)
    unmigrated_details = "; ".join(
        _unmigrated_detail(roots, assignment) for assignment in unmigrated
    )
    checks = [
        Check(
            "Unmigrated zone assignments",
            Status.WARN if unmigrated else Status.PASS,
            unmigrated_details if unmigrated else "none",
            "Run `djinn migrate-zones` to move the assigned paths." if unmigrated else "",
        )
    ]

    skipped_defaults = tuple(
        _skipped_default_detail(roots, assignment) for assignment in assignments.skipped_defaults
    )
    skipped_paths = ", ".join(str(path) for _, path in skipped_defaults)
    checks.append(
        Check(
            "Skipped shipped zone defaults",
            Status.WARN if skipped_defaults else Status.PASS,
            "; ".join(detail for detail, _ in skipped_defaults) if skipped_defaults else "none",
            f"Move or remove the conflicting regular file: {skipped_paths}."
            if skipped_defaults
            else "",
        )
    )

    collisions = find_zone_collisions(assignments, roots)
    collision_details = "; ".join(
        ", ".join(
            f"{path} ({_format_size(_path_size_bytes(path))})" for path in collision.populated_paths
        )
        for collision in collisions
    )
    checks.append(
        Check(
            "Unresolved zone collisions",
            Status.WARN if collisions else Status.PASS,
            collision_details if collisions else "none",
            "Keep the config-root copy, keep the zone copy, or merge the trees by hand."
            if collisions
            else "",
        )
    )

    drift = _zone_drift_entries(config, assignments)
    checks.append(
        Check(
            "Zone drift",
            Status.WARN if drift else Status.PASS,
            ", ".join(str(path) for path in drift) if drift else "none",
            (
                "Review these agent-root entries; add directory assignments in zones.toml "
                "when appropriate."
            )
            if drift
            else "",
        )
    )

    large_files = _large_non_overlayable_files(config)
    checks.append(
        Check(
            "Large non-overlayable files",
            Status.WARN if large_files else Status.PASS,
            ", ".join(f"{path} ({_format_size(_path_size_bytes(path))})" for path in large_files)
            if large_files
            else "none",
            "Review or remove these config-zone files; Djinn only overlays directories."
            if large_files
            else "",
        )
    )
    return checks


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

        try:
            assignments = load_zone_assignments(config)
            loose = loose_credential_dirs(config, assignments)
            checks.append(
                Check(
                    "Credential and zone dir modes",
                    Status.PASS if not loose else Status.WARN,
                    "0700" if not loose else ", ".join(str(path) for path in loose),
                    "" if not loose else "Run `djinn doctor --fix` to tighten them to 0700.",
                )
            )
            checks.extend(_zone_diagnostic_checks(config, assignments))
        except (ConfigValidationError, OSError) as exc:
            checks.append(
                Check(
                    "Zone configuration",
                    Status.WARN,
                    str(exc),
                    "Fix the zone roots or zones.toml, then re-run `djinn doctor`.",
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
                "~/.djinn/sync has agent content absent from the current zone roots",
                "Merge each agent entry into its matching current root; do not move the whole "
                "legacy root into an existing config root (DJINN_SYNC_ROOT was renamed to "
                "DJINN_CONFIG_ROOT).",
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

    # After ensure_host_env, so a directory it just created is already tight and
    # does not show up here. Reported per path: a silent chmod on a credential
    # store is exactly the kind of change that should be visible.
    try:
        assignments = load_zone_assignments(config)
    except ConfigValidationError as exc:
        failed = True
        console.print(f"Could not fix: zone directory modes ({exc})")
        assignments = None
    for path in loose_credential_dirs(config, assignments):
        try:
            path.chmod(CREDENTIAL_DIR_MODE)
        except OSError as e:
            failed = True
            console.print(f"Could not fix: {path} ({e})")
        else:
            console.print(f"Fixed: tightened {path} to 0700")

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
