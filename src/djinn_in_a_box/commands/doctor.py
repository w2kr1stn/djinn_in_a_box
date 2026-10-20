"""Diagnostics — ``djinn doctor`` health checks + a fast preflight.

``doctor()`` is the full, report-only diagnostic (PASS/FAIL/WARN + remedy per
check). ``preflight()`` is the fast critical subset that auto-runs before
``build``/``start``: it refuses with a friendly message if Docker is unusable,
then provisions the host bind-mount sources (``ensure_host_env``) unless the
caller opts out with ``provision_host=False``. ``start`` opts out here because
it provisions later through compose workflow preparation instead.
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final, cast

import typer
from rich.table import Table
from rich.text import Text

from djinn_in_a_box.config.declarations import DeclarationSet
from djinn_in_a_box.config.defaults import KNOWN_CONFIG_ROOT_ENTRIES, SYNC_PATHS
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import (
    ZONE_CONTAINER_TARGETS,
    ZoneAssignment,
    ZoneAssignments,
    load_zone_assignments,
)
from djinn_in_a_box.core import docker as docker_core
from djinn_in_a_box.core import post_mortem
from djinn_in_a_box.core import session as session_core
from djinn_in_a_box.core.config_sync import (
    audit_config_sync as audit_workflow_config,
)
from djinn_in_a_box.core.config_sync import (
    workflow_item_label,
    workflow_item_summary,
    workflow_remedy,
)
from djinn_in_a_box.core.config_workflow import (
    WorkflowPreparationProblem,
    inspect_workflow_targets,
    inspection_failure,
)
from djinn_in_a_box.core.console import (
    blank,
    console,
    error,
    escape_nonprintable,
    rule,
    warning,
)
from djinn_in_a_box.core.contained_paths import PathContainmentError, tighten_directory
from djinn_in_a_box.core.docker import (
    DJINN_NETWORK,
    SOPS_AGE_KEY_TARGET,
    ZoneRoots,
    ensure_host_env,
    ensure_network,
    get_config_root,
    network_exists,
    resolve_zone_roots,
    sops_age_key_file_problem,
)
from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE
from djinn_in_a_box.core.exceptions import (
    ConfigNotFoundError,
    ConfigValidationError,
    ZoneConfigurationError,
)
from djinn_in_a_box.core.paths import CONFIG_FILE, get_project_root
from djinn_in_a_box.core.seeding import SEED_MANIFEST, SeedingError, seed_config
from djinn_in_a_box.core.session import (
    SessionManager,
    SessionTarget,
    opencode_publisher_argv,
    parse_publisher_drift_items,
)
from djinn_in_a_box.core.workflow_publisher import DriftClass, DriftEntry

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

CODEX_DAEMON_SETTINGS_MAX_BYTES = 64 * 1024
CODEX_DAEMON_SCAN_MAX_ENTRIES = 200_000


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


def _sops_age_key_check(config: AppConfig, config_root: Path) -> Check | None:
    """Report the configured SOPS age identity, or nothing when it is unset.

    FAIL mirrors the start-time refusal. WARN flags an identity inside the config
    root: that root is meant to be mirrorable, and a private key belongs to one
    machine — mirroring it puts it into every replica and every backup.
    """
    key_file = config.sops_age_key_file
    if key_file is None:
        return None
    problem = sops_age_key_file_problem(key_file)
    if problem is not None:
        return Check(
            "SOPS age identity",
            Status.FAIL,
            f"{key_file} {problem}",
            "Fix the file or unset it: `djinn config set general.sops_age_key_file none`. "
            "`djinn start` refuses until then.",
        )
    if key_file.resolve().is_relative_to(config_root.resolve()):
        return Check(
            "SOPS age identity",
            Status.WARN,
            f"{key_file} lies inside the config root {config_root}",
            "Move the key to a machine-local path outside the config root.",
        )
    return Check(
        "SOPS age identity",
        Status.PASS,
        f"{key_file} (read-only at {SOPS_AGE_KEY_TARGET})",
    )


# -----------------------------------------------------------------------------
# Low-level probes (each degrades to a boolean; never raises)
# -----------------------------------------------------------------------------
def _docker_installed() -> bool:
    return Path(DOCKER_EXECUTABLE).is_file() and os.access(DOCKER_EXECUTABLE, os.X_OK)


def _command_ok(args: list[str]) -> bool:
    try:
        result = subprocess.run(
            args, capture_output=True, check=False, stdin=subprocess.DEVNULL, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def docker_daemon_ok() -> bool:
    """True if the Docker daemon is reachable."""
    return _command_ok([DOCKER_EXECUTABLE, "info"])


def _docker_socket_ok() -> bool:
    """True unless /var/run/docker.sock exists but is not accessible (permission)."""
    sock = Path("/var/run/docker.sock")
    if not sock.exists():
        return True  # absence is the daemon check's concern, not a permission issue
    return os.access(sock, os.R_OK | os.W_OK)


def compose_v2_ok() -> bool:
    """True if Docker Compose v2 is available (``docker compose``)."""
    return _command_ok([DOCKER_EXECUTABLE, "compose", "version"])


def buildx_ok() -> bool:
    """True if Docker Buildx is available (``docker buildx``), which builds the image."""
    return _command_ok([DOCKER_EXECUTABLE, "buildx", "version"])


def _image_built() -> bool:
    return _command_ok([DOCKER_EXECUTABLE, "image", "inspect", _IMAGE])


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
    inspections = inspect_workflow_targets(project_root, config)
    runtime_problems: list[WorkflowPreparationProblem] = []
    for inspection in inspections:
        if not inspection.clean:
            problem = inspection_failure(inspection, config).problems[0]
            runtime_problems.append(problem)
    if audit.clean:
        if runtime_problems:
            return Check(
                "Config workflow",
                Status.WARN,
                f"source={source}; " + " ".join(p.message for p in runtime_problems),
                " ".join(dict.fromkeys(p.remedy for p in runtime_problems)),
            )
        return Check("Config workflow", Status.PASS, f"source={source}; clean")
    drift = (
        ",".join(dict.fromkeys(item.kind.value for item in audit.drifts)) or "validation-problem"
    )
    labels = tuple(
        workflow_item_label(item.tool, item.relative_path, item.key_path) for item in audit.drifts
    )
    labels += tuple(workflow_item_label(item.tool, item.relative_path) for item in audit.problems)
    runtime_kinds: list[str] = []
    for inspection in inspections:
        if inspection.result is not None and not inspection.clean:
            runtime_kinds.append(inspection.result.drift_class.value)
            labels += tuple(
                workflow_item_label(
                    inspection.target.tool,
                    item.path,
                    item.key_path,
                    root=inspection.target.destination_root,
                    origin=item.origin,
                )
                for item in inspection.result.drift_items
            )
    drift = ",".join(dict.fromkeys([drift, *runtime_kinds]))
    kind = audit.drifts[0].kind if audit.drifts else DriftClass.INVALID_OR_SEMANTIC
    remedy = next(
        (problem.remedy for problem in audit.problems if problem.remedy is not None),
        workflow_remedy(
            kind,
            source,
            items=tuple(
                DriftEntry(
                    item.kind,
                    item.relative_path or PurePosixPath("."),
                    item.key_path,
                    reason=item.reason,
                )
                for item in audit.drifts
            ),
        ),
    )
    return Check(
        "Config workflow",
        Status.WARN,
        f"source={source}; drift={drift} ({workflow_item_summary(labels)})"
        + "".join(
            f"; {problem.message}"
            for problem in runtime_problems
            if problem.identifier == "runtime-not-checked"
        ),
        " ".join(dict.fromkeys([remedy, *(p.remedy for p in runtime_problems)])),
    )



def _opencode_runtime_check(config: AppConfig | None, *, daemon: bool) -> Check:
    name = "OpenCode runtime"
    unavailable = Check(
        name, Status.WARN, "check unavailable", "Run `djinn config status` on the host, then retry."
    )
    if not daemon or config is None:
        return unavailable
    container_name = session_core._DJINN_CONTAINER_NAME  # pyright: ignore[reportPrivateUsage]
    try:
        discovered = subprocess.run(
            [
                DOCKER_EXECUTABLE, "container", "inspect", container_name,
                "--format", "{{json .State.Running}}",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        absent = (
            discovered.returncode != 0
            and discovered.stderr.strip()
            == f"Error response from daemon: No such container: {container_name}"
        )
        if absent or (discovered.returncode == 0 and discovered.stdout.strip() == "false"):
            return Check(name, Status.PASS, "container-local; rebuilt at every djinn start")
        if discovered.returncode != 0 or discovered.stdout.strip() != "true":
            return unavailable
        target = SessionTarget(container_id=container_name)
        manager = SessionManager("workflow-check")
        compatibility = manager.workflow_image_compatible(target)
        if compatibility is not docker_core.WorkflowImageCompatibility.COMPATIBLE:
            detail = (
                "rebuild and recreate required"
                if compatibility is docker_core.WorkflowImageCompatibility.INCOMPATIBLE
                else "check unavailable"
            )
            return Check(
                name, Status.WARN, detail, "Run `djinn build`, `djinn clean`, `djinn start`."
            )
        checked = subprocess.run(
            opencode_publisher_argv(target, check=True),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return unavailable
    if checked.returncode == 0:
        return Check(name, Status.PASS, "clean; container-local; rebuilt at every djinn start")
    diagnostic = parse_publisher_drift_items(checked.stderr)
    if checked.returncode not in {10, 11, 12, 13} or diagnostic is None:
        return unavailable
    kind = {
        10: DriftClass.SOURCE_CHANGED,
        11: DriftClass.TARGET_DRIFT,
        12: DriftClass.COLLISION,
        13: DriftClass.INVALID_OR_SEMANTIC,
    }[checked.returncode]
    if any(item.origin == "seed" for item in diagnostic.items):
        kind = DriftClass.SOURCE_CHANGED
    detail = f"{kind.value} in {workflow_item_summary(diagnostic.labels, total=diagnostic.total)}"
    remedy = workflow_remedy(
        kind, config.config_sync.source, scope="opencode", items=diagnostic.items
    )
    if kind is DriftClass.SOURCE_CHANGED:
        remedy += " Then recreate the container (`djinn clean`, `djinn start`)."
    return Check(name, Status.WARN, detail, remedy)

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
        known_entries = KNOWN_CONFIG_ROOT_ENTRIES[agent]
        accounted = set(known_entries)
        for zone in ("local", "shared"):
            accounted.update(path.parts[0] for path in by_zone[zone])
        try:
            children = tuple(agent_root.iterdir())
        except OSError:
            continue
        drift.extend(
            child
            for child in children
            if child.name not in accounted
            and not any(fnmatch.fnmatchcase(child.name, entry) for entry in known_entries)
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


def _skipped_default_detail(roots: ZoneRoots, assignment: ZoneAssignment) -> tuple[str, Path]:
    path = roots.config_root / assignment.agent
    for part in assignment.relative_path.parts:
        path /= part
        if path.is_file():
            break
    return f"{assignment.agent}/{assignment.relative_path} (blocked by {path})", path


def _codex_daemon_tree_size(path: Path) -> tuple[int, int, Path | None, bool]:
    """Return logical bytes, entry count, unreadable path, and scan-cap flag."""
    pending = [path]
    total = entries = 0
    while pending:
        if entries >= CODEX_DAEMON_SCAN_MAX_ENTRIES:
            return total, entries, None, True
        current = pending.pop()
        try:
            info = os.lstat(current)
            entries += 1
            total += info.st_size
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(current) as children:
                    for child in children:
                        if entries + len(pending) >= CODEX_DAEMON_SCAN_MAX_ENTRIES:
                            return total, entries, None, True
                        pending.append(Path(child.path))
        except OSError:
            return total, entries, current, False
    return total, entries, None, False


def _codex_daemon_settings(path: Path) -> dict[str, Any]:
    """Read bounded, strict JSON without following a settings symlink."""

    def members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate member: {key}")
            result[key] = value
        return result

    def finite_number(raw: str) -> float:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value

    def reject_constant(raw: str) -> Any:
        raise ValueError(f"invalid constant: {raw}")

    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"not a regular file: {path}")
    if info.st_size > CODEX_DAEMON_SETTINGS_MAX_BYTES:
        raise ValueError(f"settings larger than 64 KiB: {path}")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"not a regular file: {path}")
        raw = source.read(CODEX_DAEMON_SETTINGS_MAX_BYTES + 1)
    if len(raw) > CODEX_DAEMON_SETTINGS_MAX_BYTES:
        raise ValueError(f"settings larger than 64 KiB: {path}")
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=members,
        parse_float=finite_number,
        parse_constant=reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("settings must be an object")
    if "updater" in value and not isinstance(value["updater"], dict):
        raise ValueError("updater must be an object")
    return cast("dict[str, Any]", value)


def _codex_daemon_checks(roots: ZoneRoots, assignments: ZoneAssignments) -> list[Check]:
    """Inspect only the assigned host-local daemon state; never invoke Codex."""
    remedy = "Start the dev container to apply the policy."

    def uninspected(detail: str, remedy: str) -> list[Check]:
        return [
            Check(
                "Codex daemon updater",
                Status.WARN,
                escape_nonprintable(f"{detail}; remote control: unset"),
                remedy,
            ),
            Check("Codex daemon releases", Status.WARN, escape_nonprintable(detail), remedy),
        ]

    local = assignments.by_agent["codex"]["local"]
    for entry in ("packages", "app-server-daemon"):
        if Path(entry) not in local:
            return uninspected(
                f"not inspected: {entry} overlay skipped",
                'See the "Skipped shipped zone defaults" row.',
            )

    home = roots.local_root / "codex"
    daemon = home / "app-server-daemon"
    packages = home / "packages"
    install = packages / "app-server-daemon"
    releases = install / "releases"
    admitted: set[Path] = set()
    # Provisioning refuses a symlinked zone root, so the local root is admitted like its children.
    for path in (roots.local_root, home, daemon, packages, install, releases):
        if path != roots.local_root and path.parent not in admitted:
            continue
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            return uninspected(f"unreadable: {path}", "Review local daemon directory permissions.")
        if not stat.S_ISDIR(info.st_mode):
            kind = "symlink" if stat.S_ISLNK(info.st_mode) else "non-directory"
            return uninspected(f"unexpected {kind} at {path}", "Review the local daemon layout.")
        admitted.add(path)

    current_name = "unset"
    current_exists = False
    current_raw: Path | None = None
    release_problem = ""
    if install in admitted:
        current = install / "current"
        try:
            os.lstat(current)
        except FileNotFoundError:
            pass
        except OSError:
            release_problem = f"unreadable: {current}"
        else:
            current_exists = True
            try:
                current_raw = Path(os.readlink(current))
                current_name = current_raw.name
            except OSError:
                release_problem = f"unreadable: {current}"

    release_names: list[str] = []
    staging: list[str] = []
    release_entries = 0
    size = 0
    if releases in admitted:
        size, release_entries, unreadable, capped = _codex_daemon_tree_size(releases)
        if unreadable is not None:
            release_problem = f"unreadable: {unreadable}"
        elif capped:
            release_problem = "scan incomplete"
        else:
            try:
                with os.scandir(releases) as children:
                    for index, child in enumerate(children):
                        if index >= CODEX_DAEMON_SCAN_MAX_ENTRIES:
                            release_problem = "scan incomplete"
                            break
                        info = os.lstat(child.path)
                        if child.name.startswith(".staging."):
                            staging.append(child.name)
                        elif stat.S_ISDIR(info.st_mode):
                            release_names.append(child.name)
            except OSError as exc:
                release_problem = f"unreadable: {exc.filename or releases}"

    if current_raw is not None and not release_problem:
        # Same rule as the start-time helper: a direct `releases/<name>` child with a manifest.
        selected = releases / current_name
        try:
            selected_ok = stat.S_ISDIR(os.lstat(selected).st_mode) and stat.S_ISREG(
                os.lstat(selected / "codex-package.json").st_mode
            )
        except OSError:
            selected_ok = False
        # Relative links are `releases/<name>`; absolute ones name the container's own releases.
        expected_parent = (
            ZONE_CONTAINER_TARGETS["codex"] / "packages/app-server-daemon/releases"
            if current_raw.is_absolute()
            else Path("releases")
        )
        if ".." in current_raw.parts or current_raw.parent != expected_parent or not selected_ok:
            release_problem = (
                f"invalid current selection {current_raw}; start-time pruning is skipped"
            )

    managed = current_exists or bool(release_entries > 1) or bool(release_problem)
    remote = "unset"
    updater_status = Status.PASS
    updater_detail = ""
    value: dict[str, Any] | None = None
    settings = daemon / "settings.json"
    try:
        value = _codex_daemon_settings(settings) if daemon in admitted else None
    except FileNotFoundError:
        value = None
    except (OSError, ValueError, RecursionError) as exc:
        updater_status = Status.WARN
        updater_detail = f"invalid settings: {exc}"
    if updater_status is Status.PASS:
        if value is None:
            updater_status = Status.WARN if managed else Status.PASS
            updater_detail = (
                "settings missing; the updater runs by default" if managed else "none installed"
            )
        else:
            remote_value = value.get("remoteControlEnabled")
            remote = "on" if remote_value is True else "off" if remote_value is False else "unset"
            enabled = value.get("updater", {}).get("autoUpdateEnabled")
            if enabled is False:
                updater_detail = "updater.autoUpdateEnabled: false"
            else:
                updater_status = Status.WARN
                updater_detail = (
                    "updater.autoUpdateEnabled: true"
                    if enabled is True
                    else "updater.autoUpdateEnabled unset or invalid; the updater runs by default"
                )
    updater = Check(
        "Codex daemon updater",
        updater_status,
        escape_nonprintable(f"{updater_detail}; remote control: {remote}"),
        remedy if updater_status is Status.WARN else "",
    )

    count = len(release_names)
    release_status = Status.WARN if release_problem or count > 1 else Status.PASS
    if release_problem:
        detail = release_problem
    elif count > 1:
        detail = f"{count} releases, {_format_size(size)}; pruning runs at dev-container start"
    elif count == 1:
        detail = f"{release_names[0]} ({_format_size(size)})"
    else:
        detail = "none installed"
    detail += f"; current: {current_name}"
    if staging:
        detail += f"; staging: {', '.join(sorted(staging))}"
    return [
        updater,
        Check(
            "Codex daemon releases",
            release_status,
            escape_nonprintable(detail),
            remedy if release_status is Status.WARN else "",
        ),
    ]


def _zone_diagnostic_checks(config: AppConfig, assignments: ZoneAssignments) -> list[Check]:
    roots = resolve_zone_roots(config)
    checks: list[Check] = []
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
    checks.extend(_codex_daemon_checks(roots, assignments))
    return checks


# -----------------------------------------------------------------------------
# Check assembly
def hostctl_checks(config: AppConfig | None, *, daemon: bool) -> list[Check]:
    from djinn_in_a_box.core import hostctl

    checks = [
        Check(
            "Hostctl configuration",
            Status.WARN if config is None or not config.hostctl.hosts else Status.PASS,
            "unknown/missing configuration"
            if config is None
            else f"{len(config.hostctl.hosts)} hosts; default {config.hostctl.default_duration}",
        )
    ]
    if not daemon:
        return checks + [
            Check(f"Hostctl {name}", Status.WARN, "unknown: Docker unavailable")
            for name in (
                "window",
                "helper",
                "node",
                "journal",
                "peer trust",
                "relay",
                "sealing",
                "direct probe",
            )
        ]
    try:
        value = hostctl.snapshot()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return checks + [
            Check(f"Hostctl {name}", Status.WARN, f"unknown: {exc}")
            for name in (
                "window",
                "helper",
                "node",
                "journal",
                "peer trust",
                "relay",
                "sealing",
                "direct probe",
            )
        ]
    window = value.get("window")
    checks.append(
        Check(
            "Hostctl window",
            Status.PASS if window else Status.WARN,
            f"{value['state']}; helper-owned deadline {window['deadline']}"
            if window
            else f"{value['state']}; deadline unknown",
        )
    )
    checks.append(
        Check(
            "Hostctl helper",
            Status.PASS if value.get("running") else Status.WARN,
            str(value["helper"]),
        )
    )
    node = value.get("node")
    if isinstance(node, dict):
        from typing import cast

        node = cast(dict[str, Any], node)
        self_node: dict[str, Any] = node.get("Self") or {}
        users: dict[str, Any] = node.get("User") or {}
        account: dict[str, Any] = users.get(str(self_node.get("UserID")), {})
        tags = self_node.get("Tags")
        detail = (
            f"{node['BackendState']}; name={self_node.get('HostName', 'unknown')}; "
            f"account={account.get('LoginName', 'unknown')}; "
            f"tags={tags if tags is not None else 'unknown'}; "
            f"key expiry={self_node.get('KeyExpiry', 'unknown')}"
        )
        checks.append(
            Check(
                "Hostctl node",
                Status.WARN,
                detail,
                "Enroll on the host and disable node-key expiry in the admin console.",
            )
        )
    else:
        checks.append(Check("Hostctl node", Status.WARN, str(node or "unknown")))
    root = hostctl.state_root(create=False)
    observation: dict[str, Any] = value.get("observation") or {}
    from djinn_in_a_box.core.host_runtime import process_token

    observer_live = (
        bool(observation)
        and time.time() - observation.get("time", 0) < 10
        and process_token(observation.get("observer_pid", -1)) == observation.get("observer_token")
    )
    checks.append(
        Check(
            "Hostctl journal",
            Status.PASS if (root / "journal.jsonl").exists() and observer_live else Status.WARN,
            "present; observer current"
            if observer_live
            else "observation/log gaps unknown; forced kills cannot record exact expiry",
        )
    )
    trust: dict[str, Any] = value.get("trust") or {}
    current_trust = bool(
        trust
        and trust.get("generation") == value.get("generation")
        and value.get("state") == "open"
    )
    checks.extend(
        [
            Check(
                "Hostctl peer trust",
                Status.PASS if current_trust else Status.WARN,
                f"{len(trust['peers'])} peers; frozen at opening"
                if current_trust
                else "unknown or cached from a closed window",
            ),
            Check(
                "Hostctl relay",
                Status.PASS if value.get("state") == "open" else Status.WARN,
                value.get("relay", "unknown")
                + (f"; {value['relay_error']}" if value.get("relay_error") else ""),
            ),
        ]
    )
    checks.extend(hostctl_boundary_checks(value, config))
    return checks


def hostctl_boundary_checks(value: dict[str, Any], config: AppConfig | None) -> list[Check]:
    from djinn_in_a_box.core import host_sealing, hostctl

    causes = value.get("sealing_cause_details", ())
    errors = value.get("sealing_error_details", ())
    checks = [
        Check(f"Hostctl sealing cause {i}", Status.FAIL, cause) for i, cause in enumerate(causes, 1)
    ]
    checks.extend(
        Check(f"Hostctl sealing unknown {i}", Status.WARN, error)
        for i, error in enumerate(errors, 1)
    )
    if not causes and not errors:
        state = value.get("sealing", "unknown: assessment unavailable")
        checks.append(
            Check("Hostctl sealing", Status.PASS if state == "sealed" else Status.WARN, state)
        )
    dev_id = value.get("dev_id")
    trust = value.get("trust")
    if not dev_id or not trust or config is None or not config.hostctl.hosts:
        checks.append(
            Check(
                "Hostctl direct probe",
                Status.WARN,
                "deferred: running dev and authenticated declared-peer snapshot required",
            )
        )
        return checks
    addresses: list[list[str]] = []
    try:
        for alias, host in config.hostctl.hosts.items():
            if trust["peers"][alias]["address"] != host.address:
                raise ValueError("cached peer declaration differs; open a new window")
        selected = {
            **trust,
            "peers": {alias: trust["peers"][alias] for alias in config.hostctl.hosts},
        }
        addresses = host_sealing.probe_addresses(selected)
        rows = host_sealing.run_probe(dev_id, selected)
        agent = value.get("agent")
        if agent:
            rows.extend(host_sealing.run_probe(agent["id"], selected))
        hostctl.verify_dev(dev_id)
        hostctl.verify_agent(dev_id, agent)
        for row in rows:
            state = row["state"]
            checks.append(
                Check(
                    f"Hostctl direct probe {row.get('namespace', 'dev')} "
                    f"{row['host']} {row['address']}",
                    Status.FAIL
                    if state == "reached"
                    else (Status.PASS if state == "blocked" else Status.WARN),
                    state + ("; " + host_sealing.PREREQUISITE if state == "reached" else ""),
                )
            )
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        for alias, address in addresses or [["", ""]]:
            checks.append(
                Check(
                    f"Hostctl direct probe {alias} {address}".rstrip(),
                    Status.WARN,
                    f"unknown: {exc}",
                )
            )
    return checks


# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
def agent_docker_checks(*, daemon: bool) -> list[Check]:
    from djinn_in_a_box.core import agent_docker, host_runtime

    names = (
        "Dev Docker endpoint",
        "Agent Docker identity",
        "Agent Docker health",
        "Agent Docker storage",
    )
    if not daemon:
        return [Check(name, Status.WARN, "unknown: Docker unavailable") for name in names]
    verified = docker_core.inspect_agent_endpoint()
    status = (
        Status.PASS
        if verified.kind in {"none", "agent"}
        else Status.FAIL
        if verified.in_use
        else Status.WARN
    )
    detail = verified.kind + "; " + verified.detail
    if not verified.in_use:
        detail += "; agent mode not in use"
    if verified.errors:
        detail += "; " + "; ".join(verified.errors)
    checks = [Check(names[0], status, detail)]
    if verified.kind == "agent":
        checks.extend(
            [
                Check(
                    names[1],
                    Status.PASS,
                    f"{verified.companion_id}; generation {verified.generation}; "
                    f"{agent_docker.IMAGE}; Engine {verified.version}",
                ),
                Check(names[2], Status.PASS, verified.health + " (host inspect healthcheck)"),
                Check(
                    names[3],
                    Status.PASS,
                    f"overlay2; {agent_docker.DATA_ROOT}; {verified.cache}; cache; "
                    "plain local storage / managed tmpfs endpoint",
                ),
            ]
        )
    else:
        checks.extend(
            Check(
                name,
                Status.FAIL if verified.in_use else Status.PASS,
                "unverified: " + "; ".join(verified.errors) if verified.in_use else "not in use",
            )
            for name in names[1:]
        )
    try:
        # Include stopped resources; retained cache alone is ordinary persistence.
        from djinn_in_a_box.core import hostctl

        resources = hostctl.command(
            "ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.service=agent-docker"
        ).split()
        endpoints = hostctl.command(
            "volume", "ls", "-q", "--filter", "name=^" + agent_docker.ENDPOINT_PREFIX
        ).split()
        stale = [item for item in resources if item != verified.companion_id]
        for name in endpoints:
            volume = host_runtime.inspect_object(name, hostctl.DOCKER_EXECUTABLE, "volume")
            if volume is None:
                raise ValueError("endpoint disappeared during inspection")
            if (
                verified.kind != "agent"
                or volume["Labels"].get(host_runtime.GENERATION_LABEL) != verified.generation
            ):
                stale.append(name)
        checks.append(
            Check(
                "Agent Docker stale/orphan resources",
                Status.WARN if stale else Status.PASS,
                "; ".join(stale) if stale else "none",
            )
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        checks.append(Check("Agent Docker stale/orphan resources", Status.WARN, f"unknown: {exc}"))
    return checks


def declaration_checks(
    config: AppConfig | None, declarations: DeclarationSet | None = None,
) -> list[Check]:
    """Inspect declarations even if an unrelated optional mount builder fails."""
    targets, warnings = docker_core.declaration_reservation_context(config)
    checks = [Check(f"Declaration context: {name}", Status.WARN, message)
              for name, message in warnings]
    resolved = docker_core.resolve_declared_entries(
        config, docker_core.ContainerOptions(), runtime_targets=targets, caller_env=None,
        declarations=declarations,
    )
    read_only_binds = {
        f"mounts.{mount.name}" for mount in resolved.mounts
        if mount.kind == "bind" and mount.read_only
    }
    checks.extend(Check(
        diagnostic.identity, Status.FAIL if diagnostic.error else Status.PASS,
        diagnostic.error or (
            "valid declaration (read-only)" if diagnostic.identity in read_only_binds
            else "valid declaration"
        ),
        "Fix config.toml or the declared host path." if diagnostic.error else "",
    ) for diagnostic in resolved.diagnostics)
    return checks


def run_checks(config: AppConfig | None, config_error: str | None = None) -> list[Check]:
    """Run every diagnostic and return the results (no side effects)."""
    kept, latest, problem = post_mortem.latest()
    checks = [Check(
        "Post-mortem records", Status.WARN if problem else Status.PASS,
        problem if problem else (
            f"{kept} kept; latest {escape_nonprintable(str(latest))}" if latest else "none"
        ),
    )]

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

    # Only `djinn build` needs buildx, so a missing plugin warns: an existing image
    # still starts and runs.
    buildx = installed and buildx_ok()
    checks.append(
        Check(
            "Buildx",
            Status.PASS if buildx else Status.WARN,
            "available" if buildx else "`docker buildx` not available",
            "" if buildx else "Install the Docker Buildx plugin; `djinn build` needs it.",
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
    checks.append(_opencode_runtime_check(config, daemon=daemon))

    if config is not None:
        code_ok = config.code_dir.is_dir()
        root_label = "AIOS root" if config.workspace == "aios" else "Projects dir"
        checks.append(
            Check(
                root_label,
                Status.PASS if code_ok else Status.FAIL,
                str(config.code_dir),
                "" if code_ok else f"Create the {root_label} or fix `general.code_dir`.",
            )
        )
        problems = docker_core.managed_bind_problems(config)
        checks.append(
            Check(
                "Managed bind sources",
                Status.FAIL if problems else Status.PASS,
                "; ".join(problems) if problems else "no overlap with host execution inputs",
            )
        )
        try:
            root = get_config_root(config)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            checks.append(Check("Config root", Status.FAIL, str(exc)))
        else:
            root_ok = root.is_dir()
            checks.append(
                Check(
                    "Config root",
                    Status.PASS if root_ok else Status.WARN,
                    str(root),
                    "" if root_ok else "Run `djinn init` (it provisions the config root).",
                )
            )
            sops_check = _sops_age_key_check(config, root)
            if sops_check is not None:
                checks.append(sops_check)

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
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                checks.append(
                    Check(
                        "Zone configuration",
                        Status.WARN,
                        str(exc),
                        "Fix the zone roots or zones.toml, then re-run `djinn doctor`.",
                    )
                )

    if config is not None:
        checks.extend(declaration_checks(config))
        from djinn_in_a_box.core.git_diagnostics import git_diagnostics

        checks.extend(
            Check(row.name, Status(row.status), row.detail, row.remedy)
            for row in git_diagnostics(config)
        )

    checks.extend(agent_docker_checks(daemon=daemon))
    checks.extend(hostctl_checks(config, daemon=daemon))

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

    inspection = docker_core.inspect_running_desktop()
    for row in inspection.channels:
        status = (
            Status.FAIL
            if row.state == "raw"
            else Status.PASS
            if row.state in {"off", "filtered", "locked"}
            else Status.WARN
        )
        detail = row.detail + (f"; Debian xdg-dbus-proxy {row.version}" if row.version else "")
        checks.append(
            Check(
                "D-Bus session" if row.channel == "dbus" else "Audio relay",
                status,
                detail,
                "Rebuild and recreate dev." if status is not Status.PASS else "",
            )
        )
    checks.append(
        Check(
            "Desktop raw sockets",
            Status.FAIL
            if inspection.raw_sources
            else Status.PASS
            if inspection.raw_verified
            else Status.WARN,
            "; ".join(inspection.raw_sources)
            if inspection.raw_sources
            else "no raw desktop sources in actual dev mounts"
            if inspection.raw_verified
            else "no running dev inspection; raw exposure unknown",
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
    except ZoneConfigurationError as e:
        failed = True
        console.print(f"Could not fix: host environment ({e})")
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
    try:
        roots = resolve_zone_roots(config)
    except ZoneConfigurationError as exc:
        failed = True
        console.print(f"Could not fix: zone directory modes ({exc})")
    else:
        for path in loose_credential_dirs(config, assignments):
            root = next(
                root
                for root in (roots.config_root, roots.shared_root, roots.local_root)
                if path.is_relative_to(root)
            )
            try:
                tighten_directory(
                    root, PurePosixPath(path.relative_to(root)), mode=CREDENTIAL_DIR_MODE
                )
            except (PathContainmentError, OSError) as e:
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

    Reports PASS/WARN/FAIL for Docker, Compose, configuration, the selected
    workspace root, the config root, the image, the network, and the optional MCP
    plugin — each with a remedy. Exits non-zero if any hard check fails.
    """
    from djinn_in_a_box.config.loader import load_config

    # Distinguish "missing" from "present but invalid" so the diagnostic is truthful:
    # a malformed config must report FAIL, never a misleading PASS.
    config: AppConfig | None = None
    config_error: str | None = None
    invalid_config: ConfigValidationError | None = None
    try:
        config = load_config()
    except ConfigNotFoundError:
        config = None  # the Configuration check reports the missing file
    except ConfigValidationError as e:
        config = None
        config_error = str(e)
        invalid_config = e

    rule("Djinn Doctor")

    checks = run_checks(config, config_error)
    if invalid_config is not None and invalid_config.declarations is not None:
        checks.extend(declaration_checks(
            invalid_config.reservation_config, invalid_config.declarations,
        ))
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
            if check.name in {"Projects dir", "AIOS root", "Config root"}
            else Text(check.detail)
        )
        table.add_row(
            Text(check.name),
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
