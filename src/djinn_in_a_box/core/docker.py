"""Docker and Docker Compose operations for Djinn in a Box."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from djinn_in_a_box.config.models import AppConfig

from djinn_in_a_box.config.defaults import SYNC_PATHS, VOLUME_CATEGORIES
from djinn_in_a_box.core.console import warning
from djinn_in_a_box.core.exceptions import (
    MountSpecificationError,
    RuntimeMountSpecificationError,
    ZoneRootValidationError,
)
from djinn_in_a_box.core.paths import get_project_root, resolve_mount_path
from djinn_in_a_box.core.seeding import workflow_root_is_uninitialized

DJINN_NETWORK: str = "djinn-network"
"""Docker network name for Djinn containers."""

_CONTAINER_USER_UID: int = 1000
"""Must match USER_UID build ARG in Dockerfile."""

_SERVICE_CONTAINER_NAMES: dict[str, str] = {
    "dev": "djinn",
}
_WORKFLOW_IMAGE = "djinn-in-a-box:latest"
_WORKFLOW_PUBLISHER_LABEL = "djinn.workflow.publisher"
_WORKFLOW_IMAGE_INSPECT_TIMEOUT = 10.0
_MOUNT_ROOT = Path("/home/dev/mount")
_WORKSPACE_PATH = Path("/home/dev/workspace")
_FORBIDDEN_MOUNT_TARGET_ROOTS = (Path("/proc"), Path("/sys"), Path("/dev"))
_IMAGE_PATH_ALIASES = {
    Path("/var/run"): Path("/run"),
    Path("/home/dev/.config/claude"): Path("/home/dev/.claude"),
}
_DIRECT_DOCKER_SOCKET_TARGETS = (Path("/run/docker.sock"),)
_COMPOSE_DEV_MOUNT_TARGETS = (
    Path("/home/dev/.claude"),
    Path("/home/dev/.gemini"),
    Path("/home/dev/.codex"),
    Path("/home/dev/.opencode"),
    Path("/home/dev/.local/share/opencode"),
    Path("/home/dev/.config/gh"),
    Path("/home/dev/.config/age"),
    Path("/home/dev/.cache/uv"),
    Path("/home/dev/.cache/djinn-tools"),
    Path("/home/dev/.vscode-server"),
    Path("/home/dev/workspaces"),
    Path("/home/dev/.ssh"),
    Path("/home/dev/.gitconfig"),
    Path("/home/dev/.claude_seed"),
    Path("/home/dev/.claude/skills"),
    Path("/home/dev/.claude/commands"),
    Path("/home/dev/.claude/agents"),
    Path("/home/dev/.claude/context"),
    Path("/home/dev/.claude/scripts"),
    Path("/home/dev/.claude/CLAUDE.md"),
    Path("/home/dev/.claude/AGENTS.md"),
    Path("/home/dev/.gemini_seed"),
    Path("/home/dev/.opencode/seed"),
    Path("/home/dev/.djinn-canonical"),
    Path("/home/dev/.config/mcp-servers.json"),
    Path("/home/dev/projects"),
    Path("/home/dev/sessions"),
)


def repo_owned_submount_targets(agent_root: Path) -> tuple[Path, ...]:
    return tuple(
        target
        for target in _COMPOSE_DEV_MOUNT_TARGETS
        if target != agent_root and target.is_relative_to(agent_root)
    )


def _resolve_image_aliases(target: Path) -> Path:
    for link, real in _IMAGE_PATH_ALIASES.items():
        if target == link or target.is_relative_to(link):
            return real / target.relative_to(link)
    return target


def _image_alias_variants(target: Path) -> tuple[Path, ...]:
    canonical = _resolve_image_aliases(target)
    variants = [canonical]
    for link, real in _IMAGE_PATH_ALIASES.items():
        if canonical == real or canonical.is_relative_to(real):
            alias = link / canonical.relative_to(real)
            if alias not in variants:
                variants.append(alias)
    return tuple(variants)


def _validate_mount_target(target: Path) -> None:
    if any(
        target == root or target.is_relative_to(root)
        for root in _FORBIDDEN_MOUNT_TARGET_ROOTS
    ):
        msg = f"Mount target {target} is not allowed under /proc, /sys, or /dev"
        raise MountSpecificationError(msg)


def _normalize_mount_target(target: str | Path) -> Path:
    target_text = str(target)
    if "\x00" in target_text:
        raise MountSpecificationError("Mount target cannot contain a NUL byte")

    normalized = Path(re.sub(r"^/+", "/", os.path.normpath(target_text)))
    if not normalized.is_absolute():
        msg = f"Mount target must be an absolute container path: {target_text!r}"
        raise MountSpecificationError(msg)

    canonical = _resolve_image_aliases(normalized)
    _validate_mount_target(canonical)
    return canonical


def _normalize_runtime_mount_target(target: str) -> Path:
    try:
        return _normalize_mount_target(target)
    except MountSpecificationError as e:
        raise RuntimeMountSpecificationError(
            f"Invalid internal runtime mount target {target!r}: {e}"
        ) from e


class DockerMode(Enum):
    """Docker access mode for the development container."""

    NONE = "none"
    PROXY = "proxy"
    DIRECT = "direct"


class WorkflowImageCompatibility(Enum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    MISSING = "missing"
    UNKNOWN = "unknown"


def resolve_docker_mode(docker: bool, docker_direct: bool) -> DockerMode:
    if docker and docker_direct:
        msg = "--docker and --docker-direct are mutually exclusive"
        raise ValueError(msg)
    if docker:
        return DockerMode.PROXY
    if docker_direct:
        return DockerMode.DIRECT
    return DockerMode.NONE


@dataclass(frozen=True, slots=True)
class ContainerMount:
    """A host directory mounted at a container destination."""

    source: Path
    target: Path
    read_only: bool = False


class MountCollisionError(ValueError):
    """Raised when a user mount would hide another container mount."""


def parse_mount_spec(specification: str) -> tuple[str, Path | None, bool]:
    """Parse ``SRC[:DST[:ro|rw]]`` into source, optional target, and mode."""
    fields = specification.split(":")
    if not 1 <= len(fields) <= 3:
        msg = f"Invalid mount specification {specification!r}: expected SRC[:DST[:ro|rw]]"
        raise MountSpecificationError(msg)

    source = fields[0]
    if not source:
        msg = "Invalid mount specification: source path must not be empty"
        raise MountSpecificationError(msg)

    target: Path | None = None
    read_only = False
    if len(fields) >= 2:
        target_or_mode = fields[1]
        if target_or_mode in {"ro", "rw"}:
            read_only = target_or_mode == "ro"
        else:
            target = _normalize_mount_target(target_or_mode)

    if len(fields) == 3:
        if target is None:
            msg = "Mount target must be an absolute container path when a mode is provided"
            raise MountSpecificationError(msg)
        mode = fields[2]
        if mode not in {"ro", "rw"}:
            msg = f"Invalid mount mode {mode!r}: expected 'ro' or 'rw'"
            raise MountSpecificationError(msg)
        read_only = mode == "ro"

    return source, target, read_only


def _derived_mount_target(source: Path, assigned_targets: set[Path]) -> Path:
    """Choose a stable child of ``/home/dev/mount`` for a target-free mount."""
    basename = source.name
    candidate_name = basename
    candidate = _MOUNT_ROOT / candidate_name

    if not basename or candidate in assigned_targets:
        parent_name = source.parent.name
        if parent_name:
            candidate_name = f"{parent_name}-{basename}" if basename else parent_name
        elif basename:
            candidate_name = basename
        else:
            candidate_name = "root"
        candidate = _MOUNT_ROOT / candidate_name

    suffix = 2
    while candidate in assigned_targets:
        candidate = _MOUNT_ROOT / f"{candidate_name}-{suffix}"
        suffix += 1

    return candidate


def resolve_container_mounts(
    specifications: tuple[str, ...], *, here: bool = False
) -> tuple[ContainerMount, ...]:
    """Resolve sources and assign targets for one start or headless-run invocation."""
    parsed = [parse_mount_spec(specification) for specification in specifications]
    if any(target == _WORKSPACE_PATH for _, target, _ in parsed):
        msg = f"Mount target {_WORKSPACE_PATH} is reserved for --here"
        raise MountSpecificationError(msg)
    source_mounts = [
        (resolve_mount_path(source), target, read_only)
        for source, target, read_only in parsed
    ]

    mounts: list[ContainerMount] = []
    if here:
        mounts.append(
            ContainerMount(
                source=resolve_mount_path(Path.cwd()),
                target=_WORKSPACE_PATH,
            )
        )

    assigned_targets = {target for _, target, _ in source_mounts if target is not None}
    if here:
        assigned_targets.add(_WORKSPACE_PATH)

    for source, target, read_only in source_mounts:
        resolved_target = target
        if resolved_target is None:
            resolved_target = _derived_mount_target(source, assigned_targets)
        assigned_targets.add(resolved_target)
        mounts.append(
            ContainerMount(source=source, target=resolved_target, read_only=read_only)
        )

    return tuple(mounts)


@dataclass(frozen=True, slots=True)
class ContainerOptions:
    """Options for container execution (Docker access, firewall, mounts)."""

    docker_mode: DockerMode = DockerMode.NONE
    """Docker access mode (none, proxy, or direct)."""

    firewall_enabled: bool = False
    """Enable network firewall (restricts outbound traffic)."""

    mounts: tuple[ContainerMount, ...] = ()
    """Additional host-directory mounts for this container execution."""


@dataclass(frozen=True, slots=True)
class RunResult:
    """Result of a container execution."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def success(self) -> bool:
        return self.returncode == 0


def _docker_inspect(resource: str, name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", resource, "inspect", name], capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        warning("Docker is not installed")
        return False
    return result.returncode == 0


def _run_captured(
    cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
) -> RunResult:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, env=env, check=False,
        )
        return RunResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
    except FileNotFoundError as e:
        return RunResult(returncode=127, stdout="", stderr=f"Command not found: {e}")
    except PermissionError as e:
        return RunResult(returncode=126, stdout="", stderr=f"Permission denied: {e}")


def _decode_timeout_output(
    exc: subprocess.TimeoutExpired,
    timeout: int,
) -> tuple[str, str]:
    """Decode stdout/stderr from a TimeoutExpired exception."""
    stdout = (
        exc.stdout.decode(errors="replace")
        if isinstance(exc.stdout, bytes)
        else (exc.stdout or "")
    )
    stderr = (
        exc.stderr.decode(errors="replace")
        if isinstance(exc.stderr, bytes)
        else (exc.stderr or f"Timeout after {timeout}s")
    )
    return stdout, stderr


def _docker_list(cmd: list[str]) -> list[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        warning("Docker is not installed")
        return []
    if result.returncode != 0:
        stderr_msg = result.stderr.strip() if result.stderr else f"exit code {result.returncode}"
        warning(f"Docker command failed: {stderr_msg}")
        return []
    if not result.stdout.strip():
        return []
    return [line for line in result.stdout.strip().split("\n") if line]


def network_exists(name: str = DJINN_NETWORK) -> bool:
    return _docker_inspect("network", name)


def delete_network(name: str) -> bool:
    result = _run_captured(["docker", "network", "rm", name])
    if not result.success:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        warning(f"Failed to delete network '{name}': {detail}")
    return result.success


def ensure_network(name: str = DJINN_NETWORK) -> bool:
    if _docker_inspect("network", name):
        return True
    result = _run_captured(["docker", "network", "create", name])
    if not result.success:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        warning(f"Failed to create Docker network '{name}': {detail}")
    return result.success


def get_compose_files(docker_mode: DockerMode = DockerMode.NONE) -> list[str]:
    """Get compose file arguments ["-f", "file.yml", ...] based on Docker mode."""
    project_root = get_project_root()
    files = ["-f", str(project_root / "docker-compose.yml")]

    if docker_mode is DockerMode.PROXY:
        files.extend(["-f", str(project_root / "docker-compose.docker.yml")])
    elif docker_mode is DockerMode.DIRECT:
        files.extend(["-f", str(project_root / "docker-compose.docker-direct.yml")])

    return files


def _host_terminal_width() -> str | None:
    if not (sys.stdout.isatty() or sys.stderr.isatty()):
        return None

    columns = shutil.get_terminal_size().columns
    if columns <= 0:
        return None
    return str(columns)


def build_compose_env(config: AppConfig | None) -> dict[str, str]:
    """Render docker-compose interpolation variables from the loaded config.

    These are the host-side ``${VAR}`` values the compose file interpolates at
    parse time (NOT container ``-e`` env). The two ``${...:?}``-guarded vars
    (CODE_DIR, DJINN_CONFIG_ROOT) are always rendered so compose never hard-fails.

    ``config=None`` → best-effort placeholders for the two guarded vars only,
    for teardown / by-name operations (down/stop/rm act by project/container name).
    """
    terminal_width = _host_terminal_width()
    if config is None:
        env = {
            "CODE_DIR": str(Path.home()),
            "DJINN_CONFIG_ROOT": str(get_config_root()),
        }
    else:
        env = {
            "CODE_DIR": str(config.code_dir),
            "DJINN_CONFIG_ROOT": str(get_config_root(config)),
            "TZ": config.timezone,
            "CPU_LIMIT": str(config.resources.cpu_limit),
            "MEMORY_LIMIT": config.resources.memory_limit,
            "CPU_RESERVATION": str(config.resources.cpu_reservation),
            "MEMORY_RESERVATION": config.resources.memory_reservation,
        }
    if terminal_width is not None:
        env["DJINN_TERM_WIDTH"] = terminal_width
    return env


def _compose_host_env(config: AppConfig | None) -> dict[str, str]:
    """Full host environment for a compose subprocess: inherited env + rendered vars."""
    return {**os.environ, **build_compose_env(config)}


def _run_compose(args: list[str], *, config: AppConfig | None, cwd: Path) -> RunResult:
    """Single choke-point for *captured* ``docker compose`` calls.

    Every captured compose invocation routes through here so the host
    interpolation env is always injected. (The interactive/headless path in
    ``compose_run`` is the only other sanctioned compose site.)
    """
    return _run_captured(["docker", "compose", *args], cwd=cwd, env=_compose_host_env(config))


def get_shell_mount_args(config: AppConfig) -> list[str]:
    """Build shell config mount arguments (zshrc, configured OMP theme, oh-my-zsh custom).

    Returns empty list if config.shell.skip_mounts is True or no host files exist.
    """
    if config.shell.skip_mounts:
        return []

    args: list[str] = []
    home = Path.home()

    # ZSH config (mounted as .zshrc.local for sourcing)
    zshrc = home / ".zshrc"
    if zshrc.exists():
        args.extend(["-v", f"{zshrc}:/home/dev/.zshrc.local:ro"])

    # Oh My Posh theme (explicit config only — no auto-detection)
    omp_theme = config.shell.omp_theme_path
    if omp_theme is not None:
        if omp_theme.exists():
            args.extend(["-v", f"{omp_theme}:/home/dev/.zsh-theme.omp.json:ro"])
        else:
            # An explicitly configured theme must not vanish silently.
            warning(f"Configured OMP theme not found, skipping mount: {omp_theme}")

    # Oh My ZSH custom directory (plugins, themes, etc.)
    omz_custom = home / ".oh-my-zsh/custom"
    if omz_custom.is_dir():
        args.extend(["-v", f"{omz_custom}:/home/dev/.oh-my-zsh/custom:ro"])

    return args


def get_audio_mount_args() -> list[str]:
    """Build PulseAudio socket mount arguments for audio passthrough.

    Auto-detects the PulseAudio/PipeWire socket on the host.
    Returns empty list if no audio socket is found.
    """
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    pulse_socket = Path(xdg_runtime) / "pulse" / "native"

    if not pulse_socket.exists():
        return []

    container_socket = f"/run/user/{_CONTAINER_USER_UID}/pulse/native"
    return [
        "-v", f"{pulse_socket}:{container_socket}",
        "-e", f"PULSE_SERVER=unix:{container_socket}",
    ]


def get_dbus_mount_args() -> list[str]:
    """Build D-Bus session socket mount arguments when the host socket exists."""
    host_bus = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "bus"

    # A stale file/dir at the bus path must not inject a broken mount — only a
    # real Unix socket counts as a session bus.
    if not host_bus.is_socket():
        return []

    container_bus = f"/run/user/{_CONTAINER_USER_UID}/bus"
    return [
        "-v", f"{host_bus}:{container_bus}:ro",
        "-e", f"DBUS_SESSION_BUS_ADDRESS=unix:path={container_bus}",
    ]


def get_zone_overlay_mount_args(config: AppConfig) -> list[str]:
    """Build bind-mount arguments for existing zone directories."""
    args, _ = _zone_overlay_mount_args_and_targets(config)
    return args


def _zone_overlay_mount_args_and_targets(config: AppConfig) -> tuple[list[str], tuple[Path, ...]]:
    """Return overlay arguments and every configured overlay target.

    The targets are returned independently of source existence: a user mount at
    an assigned target would otherwise be reported as applied and then silently
    hidden when a later migration creates the overlay source.
    """
    # ``config.zones`` imports root resolution from this module, so retain this
    # import at the runtime boundary rather than creating an import cycle.
    from djinn_in_a_box.config.zones import ZONE_CONTAINER_TARGETS, load_zone_assignments

    roots = resolve_zone_roots(config)
    assignments = load_zone_assignments(config)
    args: list[str] = []
    targets: list[Path] = []
    zone_roots = {"local": roots.local_root, "shared": roots.shared_root}
    for agent, target_root in ZONE_CONTAINER_TARGETS.items():
        for zone in ("local", "shared"):
            for relative_path in assignments.by_agent[agent][zone]:
                target = target_root / relative_path
                targets.append(target)
                source = zone_roots[zone] / agent / relative_path
                # An empty directory is the completed-migration marker. It must
                # overlay just like populated data; only a missing source skips.
                if source.is_dir():
                    args.extend(["-v", f"{source}:{target}"])
    return args, tuple(targets)


def _mount_targets_from_args(args: list[str]) -> list[Path]:
    """Extract container targets from volume arguments built in this module."""
    targets: list[Path] = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in {"-v", "--volume"}:
            if index + 1 == len(args):
                raise RuntimeMountSpecificationError(
                    f"Volume flag {argument!r} requires a specification"
                )
            specification = args[index + 1]
            index += 2
        elif argument.startswith("--volume="):
            specification = argument.split("=", 1)[1]
            index += 1
        elif argument.startswith("--volume") or argument.startswith("--mount"):
            raise RuntimeMountSpecificationError(f"Unknown volume flag {argument!r}")
        else:
            index += 1
            continue

        _, target, _ = _normalize_runtime_mount_specification(specification)
        targets.append(target)
    return targets


def _normalize_runtime_mount_specification(
    specification: str,
) -> tuple[str, Path, str | None]:
    parts = specification.rsplit(":", 2)
    if len(parts) not in {2, 3} or not parts[0]:
        raise RuntimeMountSpecificationError(
            f"Invalid internal runtime mount specification {specification!r}"
        )
    if len(parts) == 3 and parts[2] not in {"ro", "rw"}:
        raise RuntimeMountSpecificationError(
            f"Invalid internal runtime mount mode in {specification!r}"
        )
    mode = parts[2] if len(parts) == 3 else None
    return parts[0], _normalize_runtime_mount_target(parts[1]), mode


def _canonicalize_runtime_mount_args(args: list[str]) -> list[str]:
    canonical_args = list(args)
    index = 0
    while index < len(canonical_args):
        argument = canonical_args[index]
        if argument in {"-v", "--volume"}:
            if index + 1 == len(canonical_args):
                raise RuntimeMountSpecificationError(
                    f"Volume flag {argument!r} requires a specification"
                )
            source, target, mode = _normalize_runtime_mount_specification(
                canonical_args[index + 1]
            )
            canonical_args[index + 1] = (
                f"{source}:{target}" + (f":{mode}" if mode is not None else "")
            )
            index += 2
        elif argument.startswith("--volume="):
            source, target, mode = _normalize_runtime_mount_specification(
                argument.split("=", 1)[1]
            )
            canonical_args[index] = (
                f"--volume={source}:{target}"
                + (f":{mode}" if mode is not None else "")
            )
            index += 1
        elif argument.startswith("--volume") or argument.startswith("--mount"):
            raise RuntimeMountSpecificationError(f"Unknown volume flag {argument!r}")
        else:
            index += 1
    return canonical_args


def _reserved_mount_targets(
    config: AppConfig,
    docker_mode: DockerMode,
    *,
    shell_args: list[str] | None = None,
    audio_args: list[str] | None = None,
    dbus_args: list[str] | None = None,
    zone_overlay_targets: tuple[Path, ...] | None = None,
) -> list[Path]:
    """Return targets occupied by this particular ``dev`` container invocation."""
    if zone_overlay_targets is None:
        _, zone_overlay_targets = _zone_overlay_mount_args_and_targets(config)
    targets = [*_COMPOSE_DEV_MOUNT_TARGETS, *zone_overlay_targets, _MOUNT_ROOT]
    if docker_mode is DockerMode.DIRECT:
        targets.extend(_DIRECT_DOCKER_SOCKET_TARGETS)
    if shell_args is None:
        shell_args = get_shell_mount_args(config)
    if audio_args is None:
        audio_args = get_audio_mount_args()
    if dbus_args is None:
        dbus_args = get_dbus_mount_args()
    runtime_targets = [
        *_mount_targets_from_args(shell_args),
        *_mount_targets_from_args(audio_args),
        *_mount_targets_from_args(dbus_args),
    ]
    accepted_runtime_targets: list[Path] = []
    for runtime_target in runtime_targets:
        if any(
            variant == runtime_target or variant.is_relative_to(runtime_target)
            for occupied_target in [*targets, *accepted_runtime_targets]
            for variant in _image_alias_variants(occupied_target)
        ):
            raise RuntimeMountSpecificationError(
                f"Internal runtime mount target {runtime_target} conflicts with "
                "another container mount"
            )
        accepted_runtime_targets.append(runtime_target)
    return [*targets, *accepted_runtime_targets]


def validate_container_mounts(
    mounts: tuple[ContainerMount, ...],
    config: AppConfig,
    docker_mode: DockerMode,
    *,
    shell_args: list[str] | None = None,
    audio_args: list[str] | None = None,
    dbus_args: list[str] | None = None,
    zone_overlay_targets: tuple[Path, ...] | None = None,
) -> None:
    """Reject user targets that equal or are ancestors of an occupied target."""
    normalized_mounts = tuple(
        ContainerMount(mount.source, _normalize_mount_target(mount.target), mount.read_only)
        for mount in mounts
    )

    reserved_targets = _reserved_mount_targets(
        config,
        docker_mode,
        shell_args=shell_args,
        audio_args=audio_args,
        dbus_args=dbus_args,
        zone_overlay_targets=zone_overlay_targets,
    )
    occupied: list[tuple[Path, str, Path]] = []
    for target in reserved_targets:
        occupied.extend(
            (variant, f"reserved mount at {target}", target)
            for variant in _image_alias_variants(target)
        )

    for mount in normalized_mounts:
        mount_target = mount.target
        for target, description, display_target in occupied:
            if mount_target == target or target.is_relative_to(mount_target):
                msg = (
                    f"Mount {mount.source} -> {mount_target} conflicts with {description} "
                    f"(conflict path: {display_target})"
                )
                raise MountCollisionError(msg)
        occupied.append(
            (mount_target, f"mount {mount.source} -> {mount_target}", mount_target)
        )


def compose_build(config: AppConfig | None = None, *, no_cache: bool = False) -> RunResult:
    project_root = get_project_root()
    args = ["build"]
    if no_cache:
        args.append("--no-cache")
    return _run_compose(args, config=config, cwd=project_root)


def compose_run(
    config: AppConfig,
    options: ContainerOptions,
    *,
    command: str | None = None,
    interactive: bool = True,
    env: dict[str, str] | None = None,
    service: str = "dev",
    timeout: int | None = None,
    shell_mount_args: list[str] | None = None,
    audio_mount_args: list[str] | None = None,
    dbus_mount_args: list[str] | None = None,
) -> RunResult:
    """Run a container via docker compose.

    Args:
        config: Application configuration.
        options: Container options (docker, firewall, mounts).
        command: Shell command to execute. If None, starts an interactive shell.
        interactive: Enable TTY and stdin (default: True).
        env: Additional environment variables to pass to the container.
        service: Compose service name (default: dev).
        timeout: Timeout in seconds (headless only). Returns exit code 124 on timeout.
    """
    project_root = get_project_root()

    # Build compose command
    compose_files = get_compose_files(options.docker_mode)
    # Map service to fixed container name (matches container_name in compose YAML)
    container_name = _SERVICE_CONTAINER_NAMES.get(service, f"djinn-{service}")

    cmd = ["docker", "compose", *compose_files, "run", "--rm",
           "--name", container_name]

    # TTY handling
    if not interactive:
        cmd.append("-T")

    # Environment variables
    env_vars: dict[str, str] = {
        "ENABLE_FIREWALL": str(options.firewall_enabled).lower(),
    }
    if env:
        env_vars.update(env)

    for key, value in env_vars.items():
        cmd.extend(["-e", f"{key}={value}"])

    mounts = tuple(
        ContainerMount(mount.source, _normalize_mount_target(mount.target), mount.read_only)
        for mount in options.mounts
    )
    shell_args = _canonicalize_runtime_mount_args(
        get_shell_mount_args(config) if shell_mount_args is None else shell_mount_args
    )
    audio_args = _canonicalize_runtime_mount_args(
        get_audio_mount_args() if audio_mount_args is None else audio_mount_args
    )
    dbus_args = _canonicalize_runtime_mount_args(
        get_dbus_mount_args() if dbus_mount_args is None else dbus_mount_args
    )
    zone_overlay_args, zone_overlay_targets = _zone_overlay_mount_args_and_targets(config)
    validate_container_mounts(
        mounts,
        config,
        options.docker_mode,
        shell_args=shell_args,
        audio_args=audio_args,
        dbus_args=dbus_args,
        zone_overlay_targets=zone_overlay_targets,
    )

    for mount in mounts:
        mount_target = _resolve_image_aliases(mount.target)
        mount_str = f"{mount.source}:{mount_target}"
        if mount.read_only:
            mount_str += ":ro"
        cmd.extend(["-v", mount_str])

    if mounts:
        workdir = mounts[0].target
        cmd.extend(["--workdir", str(workdir)])

    # Shell mounts (skip_mounts check is inside get_shell_mount_args)
    cmd.extend(zone_overlay_args)
    cmd.extend(shell_args)
    cmd.extend(audio_args)
    cmd.extend(dbus_args)

    # Service name
    cmd.append(service)

    # Command to execute
    if command is not None:
        cmd.extend(["-c", command])

    # Host interpolation env for the compose file's ${...} vars (CODE_DIR,
    # DJINN_CONFIG_ROOT, TZ, resources). This is DISTINCT from the container
    # `-e` vars built above: docker compose interpolates the file at parse time
    # from the host subprocess environment, so it must be set here.
    host_env = _compose_host_env(config)
    try:
        if interactive:
            # Interactive mode: inherit stdin/stdout/stderr
            result = subprocess.run(
                cmd,
                cwd=project_root,
                env=host_env,
                check=False,
            )
            return RunResult(
                returncode=result.returncode,
            )

        # Headless mode: capture output with optional timeout. stdin must be
        # closed explicitly: agent CLIs such as `codex exec` block waiting for
        # stdin when they inherit an open terminal descriptor.
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=project_root,
            env=host_env,
            timeout=timeout,
            check=False,
        )
        return RunResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired as e:
        assert timeout is not None  # TimeoutExpired only raised when timeout is set
        stdout, stderr = _decode_timeout_output(e, timeout)
        return RunResult(returncode=124, stdout=stdout, stderr=stderr)
    except FileNotFoundError as e:
        return RunResult(
            returncode=127,
            stdout="",
            stderr=f"Docker command not found: {e}",
        )
    except PermissionError as e:
        return RunResult(
            returncode=126,
            stdout="",
            stderr=f"Permission denied: {e}",
        )


def compose_down(config: AppConfig | None = None) -> RunResult:
    """Stop and remove the project's containers.

    ``--remove-orphans`` is required for correctness, not tidiness, and the
    load-bearing reason is easy to miss: Compose classifies containers created
    by ``compose run`` as one-off and skips them on a plain ``down`` — and that
    is exactly how ``start`` and ``run`` create the dev container. Without the
    flag, ``djinn clean`` reports success while the live session survives, and
    ``djinn backup``'s stop-all-containers guard then keeps refusing the very
    thing the user was just told to do.

    It additionally reaps containers the project owns but this file does not
    declare — a proxy left by ``--docker``, or a service dropped in an upgrade.
    Those two are transient; the one-off case is permanent. Do not drop the flag
    on the reasoning that ``cleanup_docker_proxy`` already covers the proxy.
    """
    project_root = get_project_root()
    compose_files = get_compose_files()
    return _run_compose(
        [*compose_files, "down", "--remove-orphans"], config=config, cwd=project_root
    )


def cleanup_docker_proxy(docker_mode: DockerMode, config: AppConfig | None = None) -> None:
    if docker_mode is not DockerMode.PROXY:
        return

    project_root = get_project_root()
    compose_files = get_compose_files(DockerMode.PROXY)

    stop_result = _run_compose(
        [*compose_files, "stop", "docker-proxy"], config=config, cwd=project_root,
    )
    if not stop_result.success:
        detail = stop_result.stderr.strip() or f"exit code {stop_result.returncode}"
        warning(f"Failed to stop docker-proxy: {detail}")

    rm_result = _run_compose(
        [*compose_files, "rm", "-f", "docker-proxy"], config=config, cwd=project_root,
    )
    if not rm_result.success:
        detail = rm_result.stderr.strip() or f"exit code {rm_result.returncode}"
        warning(f"Failed to remove docker-proxy: {detail}")


def is_container_running(name: str) -> bool:
    names = _docker_list(["docker", "ps", "--format", "{{.Names}}", "--filter", f"name=^{name}$"])
    return name in names


def get_running_containers(prefix: str = "djinn") -> list[str]:
    return _docker_list(["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={prefix}"])


def volume_exists(name: str) -> bool:
    return _docker_inspect("volume", name)


def delete_volume(name: str) -> bool:
    result = _run_captured(["docker", "volume", "rm", name])
    if not result.success:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        warning(f"Failed to delete volume '{name}': {detail}")
    return result.success


def delete_volumes(names: list[str]) -> dict[str, bool]:
    return {name: delete_volume(name) for name in names}


def get_existing_volumes_by_category(category: str) -> list[str]:
    defined_volumes = VOLUME_CATEGORIES.get(category, [])
    return [vol for vol in defined_volumes if volume_exists(vol)]


def backup_volume(name: str, dest_dir: Path) -> RunResult:
    return _run_captured([
        "docker", "run", "--rm",
        "-v", f"{name}:/source:ro",
        "-v", f"{dest_dir}:/backup",
        "alpine",
        "tar", "czf", f"/backup/{name}.tar.gz", "-C", "/source", ".",
    ])


def restore_volume(name: str, source_dir: Path) -> RunResult:
    archive_path = source_dir / f"{name}.tar.gz"
    if not archive_path.exists():
        return RunResult(returncode=1, stdout="", stderr=f"Archive not found: {archive_path}")

    return _run_captured([
        "docker", "run", "--rm",
        "-v", f"{name}:/data",
        "-v", f"{source_dir}:/backup:ro",
        "alpine",
        # Clear volume (.[!.]* matches dotfiles except . and ..) then extract backup
        "sh", "-c", 'rm -rf /data/* /data/.[!.]* && tar xzf "/backup/$1.tar.gz" -C /data',
        "--", name,
    ])


# =============================================================================
# Config root + host-env provisioning
# =============================================================================


def get_config_root(config: AppConfig | None = None) -> Path:
    """Resolve the config/credential root directory.

    Precedence: env ``DJINN_CONFIG_ROOT`` → ``config.config_root`` → default
    ``~/.djinn/config``. (Renamed from ``get_sync_root``; the "sync" vocabulary
    is kept only for the optional cross-host backup/sync-path layer below.)
    """
    env = os.environ.get("DJINN_CONFIG_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    if config is not None:
        return config.config_root
    return Path.home() / ".djinn" / "config"


@dataclass(frozen=True)
class ZoneRoots:
    config_root: Path
    shared_root: Path
    local_root: Path


def resolve_zone_roots(config: AppConfig | None = None) -> ZoneRoots:
    config_root = get_config_root(config)
    shared_root = (
        config.shared_root
        if config is not None and config.shared_root is not None
        else Path(f"{config_root}.shared")
    )
    local_root = (
        config.local_root
        if config is not None and config.local_root is not None
        else Path(f"{config_root}.local")
    )
    roots = ZoneRoots(config_root, shared_root, local_root)
    root_paths = (roots.config_root, roots.shared_root, roots.local_root)
    for index, root in enumerate(root_paths):
        for other in root_paths[index + 1 :]:
            if root == other or root.is_relative_to(other) or other.is_relative_to(root):
                msg = f"Zone roots must be distinct and not nested: {root} and {other}"
                raise ZoneRootValidationError(msg)
    return roots


def ensure_zone_roots(config: AppConfig | None = None) -> ZoneRoots:
    roots = resolve_zone_roots(config)
    for root in (roots.config_root, roots.shared_root, roots.local_root):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
    return roots


def workflow_image_compatible(
    image: str = _WORKFLOW_IMAGE,
) -> WorkflowImageCompatibility:
    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                image,
                "--format",
                "{{ index .Config.Labels \"djinn.workflow.publisher\" }}",
            ],
            capture_output=True,
            text=True,
            timeout=_WORKFLOW_IMAGE_INSPECT_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
        return WorkflowImageCompatibility.UNKNOWN
    if result.returncode != 0:
        return (
            WorkflowImageCompatibility.MISSING
            if _docker_daemon_reachable()
            else WorkflowImageCompatibility.UNKNOWN
        )
    return (
        WorkflowImageCompatibility.COMPATIBLE
        if result.stdout.strip() == "1"
        else WorkflowImageCompatibility.INCOMPATIBLE
    )


def _docker_daemon_reachable() -> bool:
    """Return whether Docker responds after an image-inspect failure."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=_WORKFLOW_IMAGE_INSPECT_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def ensure_host_env(config: AppConfig | None = None) -> None:
    """Idempotently create the unconditional host bind-mount sources.

    The compose file mounts these paths unconditionally; if a source is missing
    when ``docker compose`` runs, the root Docker daemon auto-creates it
    root-owned. Creating them here (user-owned, before any compose call) prevents
    that footgun. Single host-provisioning routine reached through two entry
    paths: ``init``, ``doctor --fix``, and the ``build`` preflight call it
    directly; ``start``, ``run``, and container-mode ``session`` reach it via
    ``prepare_config_workflow(require_compose_host_env=True)``. ``start`` opts
    out of the *preflight* provisioning only (``provision_host=False``) — it
    still provisions through that workflow path before Compose runs.

    Provisions the compose-mounted credential subdirs (``SYNC_PATHS['credentials']``)
    plus the fixed extras. ``repo-dotfiles`` is intentionally NOT provisioned: it
    is a host-side input read by ``_sync_build_files`` (a no-op when absent), not a
    compose bind-mount, so it cannot trigger the root-owned-mount footgun.
    """
    roots = ensure_zone_roots(config)
    root = roots.config_root
    for name in SYNC_PATHS.get("credentials", []):
        # 0700: credential stores hold secrets (OAuth tokens, age identities).
        # Applies on creation only, matching the ~/.ssh precedent below.
        path = root / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    claude_root = get_project_root() / "config" / "claude"
    companion = claude_root / "AGENTS.md"
    if not workflow_root_is_uninitialized(claude_root) and not (
        companion.exists() or companion.is_symlink()
    ):
        companion.touch(exist_ok=False)

    djinn_dir = Path.home() / ".djinn"
    for sub in ("sessions", "backups"):
        (djinn_dir / sub).mkdir(parents=True, exist_ok=True)

    (Path.home() / ".ssh").mkdir(parents=True, exist_ok=True, mode=0o700)
    gitconfig = Path.home() / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.touch()


# =============================================================================
# Sync paths (optional cross-host backup/restore layer; bind-mounts under the
# config root)
# =============================================================================

_SYNC_ARCHIVE_PREFIX: str = "djinn-sync-"


def get_existing_sync_paths_by_category(
    category: str, config: AppConfig | None = None
) -> list[Path]:
    """Return absolute paths for sync subdirs in a category that exist on disk."""
    root = get_config_root(config)
    return [root / name for name in SYNC_PATHS.get(category, []) if (root / name).is_dir()]


def backup_sync_path(path: Path, dest_dir: Path) -> RunResult:
    """Tar the contents of a sync path to dest_dir/djinn-sync-<name>.tar.gz."""
    archive = dest_dir / f"{_SYNC_ARCHIVE_PREFIX}{path.name}.tar.gz"
    return _run_captured(
        ["tar", "czf", str(archive), "-C", str(path), "."],
    )


def restore_sync_path(
    path_name: str, source_dir: Path, config: AppConfig | None = None
) -> RunResult:
    """Extract djinn-sync-<name>.tar.gz into ${DJINN_CONFIG_ROOT}/<name>/."""
    archive = source_dir / f"{_SYNC_ARCHIVE_PREFIX}{path_name}.tar.gz"
    if not archive.exists():
        return RunResult(returncode=1, stdout="", stderr=f"Archive not found: {archive}")

    target = get_config_root(config) / path_name
    target.mkdir(parents=True, exist_ok=True)
    _clear_directory_contents(target)

    return _run_captured(["tar", "xzf", str(archive), "-C", str(target)])


def clear_sync_path(path: Path) -> bool:
    """Remove the contents of a sync path (directory itself is preserved)."""
    if not path.is_dir():
        return False
    try:
        _clear_directory_contents(path)
    except OSError as e:
        warning(f"Failed to clear sync path '{path}': {e}")
        return False
    return True


def _clear_directory_contents(path: Path) -> None:
    for item in path.iterdir():
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()


def is_sync_archive(archive_name: str) -> bool:
    """Return True if archive filename follows the sync-path naming convention."""
    return archive_name.startswith(_SYNC_ARCHIVE_PREFIX)


def extract_sync_path_name(archive_name: str) -> str:
    """Extract the sync path subdir name from a djinn-sync-<name>.tar.gz filename."""
    return archive_name.removeprefix(_SYNC_ARCHIVE_PREFIX).removesuffix(".tar.gz")
