"""Docker and Docker Compose operations for Djinn in a Box."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Required, TypedDict, cast

if TYPE_CHECKING:
    from djinn_in_a_box.config.models import AppConfig

from djinn_in_a_box.config.declarations import (
    BindDeclaration,
    DeclarationDiagnostic,
    DeclarationSet,
    VolumeDeclaration,
    declaration_error,
    inspect_declarations,
)
from djinn_in_a_box.config.defaults import (
    DESKTOP_RUNTIME_VOLUMES,
    SYNC_PATHS,
    VOLUME_CATEGORIES,
    volume_categories,
)
from djinn_in_a_box.config.volumes import PROTECTED_INTERNAL_VOLUMES
from djinn_in_a_box.core import agent_docker, desktop, host_runtime
from djinn_in_a_box.core.console import warning
from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE
from djinn_in_a_box.core.exceptions import (
    DeclarationSpecificationError,
    MountSpecificationError,
    RuntimeMountSpecificationError,
    SopsAgeKeyFileError,
    ZoneConfigurationError,
    ZoneRootValidationError,
)
from djinn_in_a_box.core.paths import get_project_root, resolve_mount_path
from djinn_in_a_box.core.ssh_delivery import GIT_ENVIRONMENT, MANAGED_SSH_TARGETS

git_runtime = host_runtime.git_runtime

DJINN_NETWORK: str = "djinn-network"
"""Docker network name for Djinn containers."""

BUILD_NETWORK_VAR: Final = "DJINN_BUILD_NETWORK"
"""Compose variable that sets ``build.network``; the build grants ``network.host`` from it."""

_SERVICE_CONTAINER_NAMES: dict[str, str] = {
    "dev": "djinn",
}
COMPOSE_PROJECT = "djinn-in-a-box"
DECLARED_VOLUME_PREFIX = "djinn-"
_WORKFLOW_IMAGE = "djinn-in-a-box:latest"
_WORKFLOW_PUBLISHER_LABEL = "djinn.workflow.publisher"
_WORKFLOW_IMAGE_INSPECT_TIMEOUT = 10.0
_MOUNT_ROOT = Path("/home/dev/mount")
_WORKSPACE_PATH = Path("/home/dev/workspace")
SOPS_AGE_KEY_TARGET: Final = Path("/home/dev/.config/sops/age/keys.txt")
"""Container path of the SOPS age identity — SOPS's own default location."""
_FORBIDDEN_MOUNT_TARGET_ROOTS = (Path("/proc"), Path("/sys"), Path("/dev"))
_IMAGE_PATH_ALIASES = {
    Path("/var/run"): Path("/run"),
    Path("/home/dev/.config/claude"): Path("/home/dev/.claude"),
}
_DIRECT_DOCKER_SOCKET_TARGETS = (Path("/run/docker.sock"),)
MANAGED_VOLUME_REPAIR_TARGETS = (
    Path("/home/dev/.cache/uv"),
    Path("/home/dev/.cache/djinn-tools"),
    Path("/home/dev/.local/share/fnm"),
    Path("/home/dev/.vscode-server"),
    Path("/home/dev/workspaces"),
)
MANAGED_TARGET_ROOTS = (
    *MANAGED_VOLUME_REPAIR_TARGETS,
    *MANAGED_SSH_TARGETS,
    *desktop.MANAGED_TARGETS,
    Path(agent_docker.DEV_ENDPOINT),
)
"""Targets at or below are refused for declared and per-invocation mounts."""
_COMPOSE_DEV_MOUNT_TARGETS: dict[Path, Literal["directory", "file"]] = {
    Path("/home/dev/.claude"): "directory",
    Path("/home/dev/.codex"): "directory",
    Path("/home/dev/.opencode"): "directory",
    Path("/home/dev/.local/share/opencode"): "directory",
    Path("/home/dev/.config/gh"): "directory",
    Path("/home/dev/.config/age"): "directory",
    Path("/home/dev/.cache/uv"): "directory",
    Path("/home/dev/.cache/djinn-tools"): "directory",
    Path("/home/dev/.vscode-server"): "directory",
    Path("/home/dev/workspaces"): "directory",
    Path("/home/dev/.gitconfig"): "file",
    Path("/home/dev/.claude_seed"): "directory",
    Path("/home/dev/.claude/skills"): "directory",
    Path("/home/dev/.claude/commands"): "directory",
    Path("/home/dev/.claude/agents"): "directory",
    Path("/home/dev/.claude/context"): "directory",
    Path("/home/dev/.claude/scripts"): "directory",
    Path("/home/dev/.claude/AGENTS.md"): "file",
    Path("/home/dev/.claude/CLAUDE.md"): "file",
    Path("/home/dev/.opencode/seed"): "directory",
    Path("/home/dev/.djinn-canonical"): "directory",
    Path("/home/dev/.config/mcp-servers.json"): "file",
    Path("/home/dev/sessions"): "directory",
}


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
    AGENT = "agent"
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
        return DockerMode.AGENT
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
    """Raised when a user mount hides another mount or enters a Djinn-managed root."""


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
    """Docker access mode (none, agent, or direct)."""

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
    owner: host_runtime.GitRuntime | None = field(default=None, compare=False, repr=False)

    @property
    def success(self) -> bool:
        return self.returncode == 0


def _docker_inspect(resource: str, name: str) -> bool:
    try:
        result = subprocess.run(
            [DOCKER_EXECUTABLE, resource, "inspect", name],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        warning("Docker is not installed")
        return False
    return result.returncode == 0


def _run_captured(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> RunResult:
    try:
        # A prompt (e.g. Compose asking to recreate a volume) must not wait on the terminal.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            check=False,
            timeout=timeout,
        )
        return RunResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)
    except FileNotFoundError as e:
        return RunResult(returncode=127, stdout="", stderr=f"Command not found: {e}")
    except PermissionError as e:
        return RunResult(returncode=126, stdout="", stderr=f"Permission denied: {e}")
    except subprocess.TimeoutExpired:
        return RunResult(returncode=124, stderr="Docker operation timed out")


def _run_streamed(
    cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
) -> RunResult:
    """Run a subprocess with stdout/stderr *inherited*, so its output is live.

    The build path needs this: a captured build reveals nothing until the process
    exits, which is precisely when a stalled build tells you nothing at all. The
    child writes straight to this process's terminal instead.

    Consequences the caller must know:

    - ``RunResult.stdout``/``stderr`` are empty when the process actually ran —
      the output already went to the terminal, there is nothing left to hand back.
      The two spawn failures below are the exception: there `stderr` carries the
      only diagnosis that exists.
    - No ``timeout``. ``subprocess.run`` would kill only this direct child, not its
      process group and not a BuildKit solve already running in the daemon, so a
      timeout here would report a cancellation it cannot actually perform.
    - ``stdin`` is closed: an inherited terminal descriptor lets a subprocess block
      on a prompt, and on this path that prompt is indistinguishable from a hang.
    - No ``text=True``: nothing is decoded here, because nothing is captured.
    """
    try:
        result = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, cwd=cwd, env=env, check=False,
        )
        return RunResult(returncode=result.returncode)
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


def _docker_list(cmd: list[str]) -> list[str] | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        warning("Docker is not installed")
        return None
    if result.returncode != 0:
        stderr_msg = result.stderr.strip() if result.stderr else f"exit code {result.returncode}"
        warning(f"Docker command failed: {stderr_msg}")
        return None
    if not result.stdout.strip():
        return []
    return [line for line in result.stdout.strip().split("\n") if line]


def network_exists(name: str = DJINN_NETWORK) -> bool:
    return _docker_inspect("network", name)


def delete_network(name: str) -> bool:
    result = _run_captured([DOCKER_EXECUTABLE, "network", "rm", name])
    if not result.success:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        warning(f"Failed to delete network '{name}': {detail}")
    return result.success


def ensure_network(name: str = DJINN_NETWORK) -> bool:
    if _docker_inspect("network", name):
        return True
    result = _run_captured([DOCKER_EXECUTABLE, "network", "create", name], timeout=5)
    if not result.success:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        warning(f"Failed to create Docker network '{name}': {detail}")
    return result.success


def get_compose_files(docker_mode: DockerMode = DockerMode.NONE) -> list[str]:
    """Get project and compose file arguments based on Docker mode."""
    project_root = get_project_root()
    files = ["-p", COMPOSE_PROJECT, "-f", str(project_root / "docker-compose.yml")]

    files.extend(["-f", str(project_root / "docker-compose.desktop.yml")])

    if docker_mode is DockerMode.AGENT:
        files.extend(["-f", str(project_root / "docker-compose.agent-docker.yml")])
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
    DJINN_WORKSPACE_TARGET selects both the workspace mount target and its cwd.

    ``config=None`` → best-effort guarded vars and the default projects target,
    for teardown / by-name operations (down/stop/rm act by project/container name).
    """
    terminal_width = _host_terminal_width()
    if config is None:
        env = {
            "CODE_DIR": str(Path.home()),
            "DJINN_WORKSPACE_TARGET": "/home/dev/projects",
            "DJINN_CONFIG_ROOT": str(get_config_root()),
        }
    else:
        env = {
            "CODE_DIR": str(config.code_dir),
            "DJINN_WORKSPACE_TARGET": str(config.workspace_target),
            "DJINN_CONFIG_ROOT": str(get_config_root(config)),
            "TZ": config.timezone,
            "CPU_LIMIT": str(config.resources.cpu_limit),
            "MEMORY_LIMIT": config.resources.memory_limit,
            "CPU_RESERVATION": str(config.resources.cpu_reservation),
            "MEMORY_RESERVATION": config.resources.memory_reservation,
            BUILD_NETWORK_VAR: config.build.network,
        }
    if terminal_width is not None:
        env["DJINN_TERM_WIDTH"] = terminal_width
    return env


def _compose_host_env(config: AppConfig | None) -> dict[str, str]:
    """Full host environment for a compose subprocess: inherited env + rendered vars."""
    inherited = {key: value for key, value in os.environ.items() if key not in desktop.MANAGED_ENV}
    return {**inherited, **build_compose_env(config)}


def _run_compose(
    args: list[str],
    *,
    config: AppConfig | None,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> RunResult:
    """Single choke-point for non-interactive ``docker compose`` calls.

    Every such compose invocation routes through here so the host interpolation
    env is always injected. (The interactive/headless path in ``compose_run`` is
    the only other sanctioned compose site.)

    ``extra_env`` carries values a subcommand cannot pass as a flag — ``up`` has
    no per-invocation ``-e`` — and is layered on top of the host bridge, never
    replacing it.
    """
    env = _compose_host_env(config)
    if extra_env:
        env.update(extra_env)
    return _run_captured([DOCKER_EXECUTABLE, "compose", *args], cwd=cwd, env=env, timeout=timeout)


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


def sops_age_key_file_problem(path: Path) -> str | None:
    """Describe why ``path`` cannot serve as the mounted SOPS age identity.

    Returns ``None`` when it can. Shared by the start-time mount builder (which
    refuses) and ``djinn doctor`` (which reports), so both judge the same way.
    Symlinks are followed: Docker binds the resolved file.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return "does not exist"
    except OSError as exc:
        return f"cannot be inspected ({exc.strerror or exc})"
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    if not os.access(path, os.R_OK):
        return "is not readable by the current user"
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        return f"is accessible to group or others (mode {mode:04o}); run chmod 600 on it"
    return None


def get_sops_age_key_mount_args(config: AppConfig) -> list[str]:
    """Build the read-only SOPS age identity mount, or nothing when unset.

    Fails closed: a configured identity that cannot be mounted safely stops the
    start instead of silently leaving SOPS without a key — or, worse, letting
    Docker create an empty directory at a missing source path.
    """
    key_file = config.sops_age_key_file
    if key_file is None:
        return []
    problem = sops_age_key_file_problem(key_file)
    if problem is not None:
        msg = (
            f"sops_age_key_file {key_file} {problem}. Fix the file or unset it with "
            "`djinn config set general.sops_age_key_file none`."
        )
        raise SopsAgeKeyFileError(msg)
    return [
        "-v", f"{key_file}:{SOPS_AGE_KEY_TARGET}:ro",
        "-e", f"SOPS_AGE_KEY_FILE={SOPS_AGE_KEY_TARGET}",
    ]


def get_zone_overlay_mount_args(config: AppConfig) -> list[str]:
    """Build bind-mount arguments for existing zone directories."""
    args, _ = _zone_overlay_mount_args_and_targets(config)
    return args


def _zone_overlay_mount_args_and_targets(config: AppConfig) -> tuple[list[str], tuple[Path, ...]]:
    """Return overlay arguments and every configured overlay target.

    Assigned overlay targets are reserved independently of whether a source
    currently exists.
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
                # Empty and populated assigned directories mount equally.
                if source.is_symlink() or (source.exists() and not source.is_dir()):
                    msg = f"Zone overlay source is not a directory: {source}"
                    raise ZoneConfigurationError(msg)
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
    config: AppConfig | None,
    docker_mode: DockerMode,
    *,
    shell_args: list[str] | None = None,
    sops_args: list[str] | None = None,
    zone_overlay_targets: tuple[Path, ...] | None = None,
) -> list[Path]:
    """Return targets occupied by this particular ``dev`` container invocation."""
    if zone_overlay_targets is None and config is not None:
        _, zone_overlay_targets = _zone_overlay_mount_args_and_targets(config)
    targets = [
        *_COMPOSE_DEV_MOUNT_TARGETS,
        *MANAGED_SSH_TARGETS,
        *desktop.MANAGED_TARGETS,
        Path(agent_docker.DEV_ENDPOINT),
        *([config.workspace_target] if config else []),
        *(zone_overlay_targets or ()),
        _MOUNT_ROOT,
    ]
    if docker_mode is DockerMode.DIRECT:
        targets.extend(_DIRECT_DOCKER_SOCKET_TARGETS)
    if shell_args is None:
        shell_args = get_shell_mount_args(config) if config else []
    if sops_args is None:
        sops_args = get_sops_age_key_mount_args(config) if config else []
    runtime_targets = [
        *_mount_targets_from_args(shell_args),
        *_mount_targets_from_args(sops_args),
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
    sops_args: list[str] | None = None,
    zone_overlay_targets: tuple[Path, ...] | None = None,
) -> None:
    """Reject user targets that hide occupied targets or enter Djinn-managed roots."""
    normalized_mounts = tuple(
        ContainerMount(mount.source, _normalize_mount_target(mount.target), mount.read_only)
        for mount in mounts
    )

    reserved_targets = _reserved_mount_targets(
        config,
        docker_mode,
        shell_args=shell_args,
        sops_args=sops_args,
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
        for root in MANAGED_TARGET_ROOTS:
            if mount_target == root or mount_target.is_relative_to(root):
                msg = (
                    f"Mount {mount.source} -> {mount_target} conflicts with Djinn-managed path "
                    f"{root} (conflict path: {root})"
                )
                raise MountCollisionError(msg)
        for target, description, display_target in occupied:
            if mount_target == target or target.is_relative_to(mount_target):
                msg = (
                    f"Mount {mount.source} -> {mount_target} conflicts with {description} "
                    f"(conflict path: {display_target})"
                )
                raise MountCollisionError(msg)
        occupied.append((mount_target, f"mount {mount.source} -> {mount_target}", mount_target))


@dataclass(frozen=True, slots=True)
class ResolvedDeclaration:
    name: str
    kind: Literal["bind", "volume"]
    source: str
    target: Path
    read_only: bool = False


@dataclass(slots=True)
class ResolvedDeclarations:
    mounts: list[ResolvedDeclaration]
    environment: dict[str, str]
    diagnostics: list[DeclarationDiagnostic]

    def require_valid(self) -> None:
        for diagnostic in self.diagnostics:
            if diagnostic.error:
                raise DeclarationSpecificationError(diagnostic.error)

    def compose_fragment(self) -> ComposeFragment:
        volumes: list[str | dict[str, object]] = []
        volume_map: dict[str, dict[str, object]] = {}
        targets: list[str] = []
        for mount in self.mounts:
            entry: dict[str, object] = {
                "type": mount.kind,
                "source": mount.source.replace("$", "$$"),
                "target": str(mount.target).replace("$", "$$"),
            }
            if mount.kind == "bind":
                entry["bind"] = {"create_host_path": False}
                if mount.read_only:
                    entry["read_only"] = True
            else:
                volume_map[mount.source] = {"name": mount.source}
                targets.append(str(mount.target))
            volumes.append(entry)
        environment = {
            **self.environment,
            "DJINN_DECLARED_VOLUME_TARGETS": json.dumps(targets),
        }
        fragment: ComposeFragment = {
            "services": {
                "dev": {
                    "volumes": volumes,
                    "environment": {
                        key: value.replace("$", "$$") for key, value in environment.items()
                    },
                }
            },
        }
        if volume_map:
            fragment["volumes"] = volume_map
        return fragment


class ComposeService(TypedDict, total=False):
    volumes: list[str | dict[str, object]]
    environment: dict[str, str | None]
    labels: dict[str, str]
    working_dir: str
    image: str
    command: list[str]
    entrypoint: list[str]
    user: str
    network_mode: str
    profiles: list[str]
    container_name: str
    cap_add: list[str]
    depends_on: dict[str, object]


class ComposeFragment(TypedDict, total=False):
    services: Required[dict[str, ComposeService]]
    volumes: dict[str, dict[str, object]]


def _declared_bind_source(mount: BindDeclaration) -> str:
    try:
        # Non-strict pathlib resolution can suppress symlink loops on newer Python.
        Path(mount.source).resolve(strict=True)
        source = resolve_mount_path(mount.source)
    except FileNotFoundError as exc:
        raise ValueError(f"source '{mount.source}' does not exist") from exc
    except NotADirectoryError as exc:
        raise ValueError(f"source '{mount.source}' is not a directory") from exc
    except MountSpecificationError as exc:
        raise ValueError(f"source '{mount.source}' cannot be resolved: {exc}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"source '{mount.source}' cannot be resolved: {exc}") from exc
        raise ValueError(f"cannot inspect source '{mount.source}': {exc}") from exc
    if ":" in str(source):
        raise ValueError("invalid source: resolved source must not contain ':'")
    if mount.marker is not None:
        marker = source / mount.marker
        try:
            mode = marker.lstat().st_mode
        except FileNotFoundError as exc:
            raise ValueError(f"marker '{mount.marker}' is missing in source '{source}'") from exc
        except OSError as exc:
            raise ValueError(f"cannot inspect marker '{marker}': {exc}") from exc
        if not stat.S_ISREG(mode):
            raise ValueError(f"marker '{mount.marker}' in source '{source}' is not a regular file")
    return str(source)


def declaration_reservation_context(
    config: AppConfig | None,
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Collect available runtime reservations and report optional inspection failures."""
    warnings: list[tuple[str, str]] = []
    runtime_args: dict[str, list[str]] = {}
    builders = {
        "shell_args": lambda: get_shell_mount_args(config) if config else [],
        "sops_args": lambda: get_sops_age_key_mount_args(config) if config else [],
    }
    for name, builder in builders.items():
        try:
            runtime_args[name] = _canonicalize_runtime_mount_args(builder())
        except (ValueError, RuntimeError, OSError) as exc:
            warnings.append((name, str(exc)))
            runtime_args[name] = []
    zones: tuple[Path, ...] = ()
    if config:
        try:
            _, zones = _zone_overlay_mount_args_and_targets(config)
        except (ValueError, RuntimeError, OSError) as exc:
            warnings.append(("zones", str(exc)))
    try:
        targets = _reserved_mount_targets(
            config,
            DockerMode.DIRECT,
            **runtime_args,
            zone_overlay_targets=zones,
        )
    except RuntimeError as exc:
        warnings.append(("targets", str(exc)))
        targets = _reserved_mount_targets(
            config,
            DockerMode.DIRECT,
            shell_args=[],
            sops_args=[],
            zone_overlay_targets=zones,
        )
    return targets, warnings


def resolve_declared_entries(
    config: AppConfig | None,
    options: ContainerOptions,
    *,
    runtime_targets: list[Path],
    caller_env: dict[str, str] | None,
    declarations: DeclarationSet | None = None,
) -> ResolvedDeclarations:
    """Resolve every declaration, retaining one diagnostic per named entry."""
    entries = declarations or inspect_declarations(
        config.mounts if config else {}, config.environment if config else {}
    )
    for key in (*GIT_ENVIRONMENT, *agent_docker.SELECTORS, "DOCKER_ENABLED",
                "DOCKER_DIRECT", "DJINN_FIREWALL_GATE"):
        if caller_env and key in caller_env:
            raise DeclarationSpecificationError(f"caller environment.{key} is reserved by Djinn")
    errors = {d.identity: d.error for d in entries.diagnostics}
    targets: dict[str, Path] = {}
    resolved: list[ResolvedDeclaration] = []
    for name, mount in entries.mounts.items():
        identity = f"mounts.{name}"
        try:
            target = _normalize_mount_target(mount.target)
            targets[name] = target
            if isinstance(mount, VolumeDeclaration):
                source = f"{DECLARED_VOLUME_PREFIX}{name}"
                if source.startswith(agent_docker.ENDPOINT_PREFIX):
                    raise ValueError("volume name is reserved for agent Docker endpoints")
                if source in ({v for values in VOLUME_CATEGORIES.values() for v in values}
                              | PROTECTED_INTERNAL_VOLUMES):
                    raise ValueError(f"volume '{source}' conflicts with built-in volume '{source}'")
                kind = "volume"
            else:
                source = _declared_bind_source(mount)
                kind = "bind"
            resolved.append(ResolvedDeclaration(
                name, kind, source, target,
                mount.read_only if isinstance(mount, BindDeclaration) else False,
            ))
        except (ValueError, MountSpecificationError) as exc:
            errors[identity] = declaration_error("mounts", name, str(exc))

    occupied = [
        (target, "built-in mount" if target in _COMPOSE_DEV_MOUNT_TARGETS else "reserved mount")
        for target in runtime_targets
    ]
    occupied.extend(
        (_normalize_mount_target(m.target), f"--mount/--here source '{m.source}'")
        for m in options.mounts
    )
    for name, target in targets.items():
        if errors.get(f"mounts.{name}"):
            continue
        for repair in MANAGED_TARGET_ROOTS:
            if target == repair or target.is_relative_to(repair):
                errors[f"mounts.{name}"] = declaration_error(
                    "mounts",
                    name,
                    f"target '{target}' conflicts with built-in mount '{repair}' "
                    "(Djinn-managed volume root)",
                )
                break
        comparisons = [
            *occupied,
            *((t, f"declared mount '{n}'") for n, t in targets.items() if n != name),
        ]
        for other, owner in comparisons:
            if target == other or other.is_relative_to(target):
                errors.setdefault(f"mounts.{name}", None)
                if errors[f"mounts.{name}"] is None:
                    errors[f"mounts.{name}"] = declaration_error(
                        "mounts", name, f"target '{target}' conflicts with {owner} at '{other}'"
                    )
                break
    for key in entries.environment:
        if caller_env and key in caller_env:
            errors[f"environment.{key}"] = declaration_error(
                "environment", key, "key is reserved by caller environment"
            )
    diagnostics = [
        DeclarationDiagnostic(d.collection, d.name, errors[d.identity]) for d in entries.diagnostics
    ]
    return ResolvedDeclarations(resolved, dict(entries.environment), diagnostics)


@contextmanager
def _compose_override(
    fragment: ComposeFragment, *, prefix: str = "djinn-detach-"
) -> Iterator[Path]:
    """Keep one override alive only for its Compose invocation."""
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".yml")
    path = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(fragment, handle)
        yield path
    finally:
        path.unlink(missing_ok=True)


_BUILD_PROGRESS_MODES: Final[frozenset[str]] = frozenset(
    {"auto", "tty", "plain", "quiet", "rawjson"}
)
BUILD_PROGRESS_ENV: Final[str] = "DJINN_BUILD_PROGRESS"


def _build_progress() -> str:
    """Progress renderer for the build, ``plain`` unless overridden.

    ``plain`` is the default because it keeps every stage line on screen, which is
    what makes a stalled build readable. Someone who wants the compact redrawing
    view back can set the env var; an unusable value falls back rather than
    letting buildx reject the whole build over a typo.
    """
    requested = os.environ.get(BUILD_PROGRESS_ENV)
    if requested is None:
        return "plain"
    if requested not in _BUILD_PROGRESS_MODES:
        warning(
            f"Ignoring {BUILD_PROGRESS_ENV}={requested!r}: "
            f"expected one of {', '.join(sorted(_BUILD_PROGRESS_MODES))}."
        )
        return "plain"
    return requested


def compose_build(
    config: AppConfig | None = None,
    *,
    no_cache: bool = False,
    agent_args: Mapping[str, str] | None = None,
) -> RunResult:
    """Build the compose-defined image with ``docker buildx bake``, streaming its log.

    Bake reads ``docker-compose.yml`` itself, so the compose file stays the one
    definition of the build. ``docker compose build`` cannot be used for it: compose
    drives bake internally but forwards only its own ``fs.read`` and
    ``security.insecure`` grants, never ``network.host``, and since buildx 0.37.2
    bake rejects an ungranted entitlement instead of skipping the consent check. A
    ``host`` build network therefore failed before its first step. Calling bake
    directly is how that consent is given — only when the interpolated build
    network is ``host``, the very value the compose file requests, so the grant
    cannot drift from the request.

    ``--load`` puts the result into the local image store on any builder. Compose
    asked for that implicitly; without it a ``docker-container`` builder would
    build the image and keep it only in its cache.

    The working directory is load-bearing: bake resolves the compose file's
    ``context`` against it, not against the file's own directory. Run from anywhere
    else, it would build whatever ``Dockerfile`` lies there under djinn's image tag.

    ``--progress plain`` is the deliberate default, overridable via
    ``DJINN_BUILD_PROGRESS``: it keeps every stage line on screen instead of
    redrawing one in place, so the stage a stalled build last entered stays readable.

    Streaming means the returned ``RunResult`` carries no output. The build log was
    already on the terminal; there is nothing to print afterwards.
    """
    project_root = get_project_root()
    compose_files = get_compose_files()
    env = _compose_host_env(config)
    cmd = [
        DOCKER_EXECUTABLE,
        "buildx",
        "bake",
        # This direct Bake command needs the Compose file selectors only.
        *compose_files[2:],
        "--progress",
        _build_progress(),
        "--load",
    ]
    if no_cache:
        cmd.append("--no-cache")
    if env.get(BUILD_NETWORK_VAR) == "host":
        cmd.extend(["--allow", "network.host"])
    for arg, version in sorted((agent_args or {}).items()):
        cmd.extend(["--set", f"dev.args.{arg}={version}"])
    cmd.extend(["dev", "dbus-helper"])
    return _run_streamed(cmd, cwd=project_root, env=env)


BACKGROUND_START_ERROR = (
    "Refusing to start an interactive container from a background process group.\n"
    "\n"
    "`docker compose run` allocates a TTY, and every terminal-attribute call from the\n"
    "background raises SIGTTOU. Compose forwards those signals into the container by the\n"
    "tens per second. Two effects are measured: the host carries the load, and Docker's\n"
    "event ring buffer overflows continuously — which erases the very records you would\n"
    "need to diagnose anything else going wrong in that container.\n"
    "\n"
    "`djinn start ... &` is exactly this shape. Use one of:\n"
    "  djinn start --detach ...        (no TTY client at all; then `djinn enter`)\n"
    "  djinn start ...                 (foreground, e.g. in its own tmux window)\n"
)


def is_background_process_group() -> bool:
    """True when this process is not in the terminal's foreground process group.

    Load-bearing for interactive ``compose run``, and the reason is easy to miss:
    Compose allocates a TTY and calls ``tcsetattr()`` on it. From a *background*
    process group that raises SIGTTOU unconditionally — the ``tostop`` terminal
    flag gates background *writes*, not terminal-attribute changes — and Compose
    forwards the signals into the container by the tens per second. Container PID 1
    survives them — namespace init discards a signal it has no handler for. What the
    storm reliably costs is host load and Docker's event ring buffer, which overflows
    continuously and takes the diagnostic record with it. Whether it also ends the
    container is unproven: the plausible path is SIGTTOU stopping the *host-side*
    compose client, after which ``--rm`` reaps it, but that was never observed
    directly.

    ``djinn start ... &`` is precisely that shape: ``&`` puts the process group in
    the background while a standard stream stays attached to the terminal.

    Keyed on **stdout**, and neither stdin nor stderr, because that is exactly what
    Compose keys on: it derives ``noTty`` from ``!dockerCli.Out().IsTerminal()``
    and allocates a TTY only when stdout is a terminal. So
    ``djinn start < /dev/null &`` still storms (stdout is the terminal), while
    ``djinn start > log &`` cannot (no TTY is allocated, nothing calls
    ``tcsetattr``) — and refusing the latter would block a safe shape that the
    entrypoint's no-TTY branch handles perfectly well.

    Returns False when stdout is not a TTY (redirected, a pipe, pytest's capture)
    or when there is no controlling terminal (``setsid``): with no allocated TTY
    there is nothing to raise SIGTTOU.
    """
    try:
        fd = sys.stdout.fileno()
        if not os.isatty(fd):
            return False
        return os.tcgetpgrp(fd) != os.getpgrp()
    except (AttributeError, ValueError, OSError):
        # No usable stdout (closed, replaced, or no controlling terminal).
        return False


def _validate_desktop_env(env: dict[str, str] | None) -> None:
    if env and (keys := desktop.MANAGED_ENV.intersection(env)):
        raise RuntimeMountSpecificationError(
            "Desktop endpoint environment is managed by Djinn: " + ", ".join(sorted(keys))
        )


def _operation_timeout(deadline: float | None, maximum: float) -> float:
    remaining = maximum if deadline is None else min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        raise RuntimeError("companion overall readiness timeout")
    return remaining


def _service_inspect(service: str, deadline: float | None = None) -> dict[str, Any] | None:
    result = _run_captured(
        [
            DOCKER_EXECUTABLE,
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={COMPOSE_PROJECT}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
        timeout=_operation_timeout(deadline, 5),
    )
    if not result.success:
        raise RuntimeError(result.stderr or "Docker service inspection failed")
    ids = result.stdout.split()
    if not ids:
        return None
    if len(ids) != 1:
        raise RuntimeError(f"ambiguous {service} ownership")
    return host_runtime.inspect_object(ids[0], DOCKER_EXECUTABLE,
                                       timeout=_operation_timeout(deadline, 5))


def _helper_evidence(
    actual: dict[str, Any], channel: str, deadline: float | None = None
) -> dict[str, Any]:
    result = _run_captured(
        [
            DOCKER_EXECUTABLE,
            "exec",
            str(actual["Id"]),
            "python3",
            "-I",
            "/etc/djinn/health.py",
            channel,
        ],
        cwd=Path("/"),
        timeout=_operation_timeout(deadline, 12),
    )
    if not result.success:
        raise RuntimeError(result.stderr or "helper package/health query failed")
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError("helper evidence is not an object")
    return cast(dict[str, Any], data)


def _desktop_volumes(endpoint: desktop.DesktopEndpoint, generation: str) -> dict[str, Any]:
    # Every Compose call must declare the volume identically: a diverging definition
    # makes Compose ask whether to recreate it.
    return {
        f"desktop-{endpoint.channel}": {
            "name": endpoint.volume,
            "labels": {host_runtime.GENERATION_LABEL: generation},
        }
    }


def _downstream_probe(
    config: AppConfig,
    options: ContainerOptions,
    endpoint: desktop.DesktopEndpoint,
    owner: host_runtime.GitRuntime,
    deadline: float | None = None,
) -> None:
    image = host_runtime.inspect_object(_WORKFLOW_IMAGE, DOCKER_EXECUTABLE, "image",
                                        timeout=_operation_timeout(deadline, 5))
    if image is None:
        raise RuntimeError("dev image is missing")
    name = f"djinn-desktop-probe-{owner.generation}-{endpoint.channel}"
    # Base dev uses UID 1000, default IPC/user namespace and NET_ADMIN. The probe
    # uses those same credentials/namespaces, with only the read-only output.
    argv = (
        [
            "dbus-send",
            f"--bus={endpoint.environment['DBUS_SESSION_BUS_ADDRESS']}",
            "--type=method_call",
            "--print-reply",
            "--reply-timeout=1500",
            "--dest=org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus.GetId",
        ]
        if endpoint.channel == "dbus"
        else ["pactl", "info"]
    )
    fragment: ComposeFragment = {
        "services": {
            name: {
                "image": str(image["Id"]),
                "user": "1000:1000",
                "entrypoint": argv,
                "command": [],
                "environment": dict(endpoint.environment),
                "network_mode": "none",
                "profiles": ["desktop"],
                "labels": {host_runtime.GENERATION_LABEL: owner.generation},
                "volumes": [
                    {
                        "type": "volume",
                        "source": f"desktop-{endpoint.channel}",
                        "target": endpoint.target,
                        "read_only": True,
                    }
                ],
            }
        },
        "volumes": _desktop_volumes(endpoint, owner.generation),
    }
    try:
        with _compose_override(fragment, prefix="djinn-probe-") as path:
            result = _run_compose(
                [
                    *get_compose_files(options.docker_mode),
                    "-f",
                    str(path),
                    "run",
                    "--rm",
                    "-T",
                    "--no-deps",
                    "--pull",
                    "never",
                    "--name",
                    name,
                    name,
                ],
                config=config,
                cwd=get_project_root(),
                timeout=_operation_timeout(deadline, 5),
            )
        if not result.success:
            raise RuntimeError(result.stderr or "downstream credentials probe failed")
    finally:
        actual = host_runtime.inspect_object(name, DOCKER_EXECUTABLE)
        if actual is not None:
            labels: dict[str, Any] = actual.get("Config", {}).get("Labels") or {}
            if labels.get(host_runtime.GENERATION_LABEL) == owner.generation:
                result = _run_captured(
                    [DOCKER_EXECUTABLE, "rm", "-f", str(actual["Id"])], timeout=5
                )
                if not result.success:
                    raise RuntimeError("failed to remove invocation-owned downstream probe")


def _require_agent_observer(options: ContainerOptions, owner: host_runtime.GitRuntime) -> None:
    if options.docker_mode is DockerMode.AGENT and (
        owner.observer is None or owner.observer.poll() is not None
    ):
        raise RuntimeMountSpecificationError("Agent Docker requires a functioning host observer")


def _prepare_workspace(
    config: AppConfig,
    options: ContainerOptions,
    declarations: ResolvedDeclarations,
    mounts: tuple[ContainerMount, ...],
    fragment: ComposeFragment,
) -> None:
    sessions = Path.home() / ".djinn/sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    delivery = [
        agent_docker.WorkspaceMount(
            "bind", str(config.code_dir.resolve()), str(config.workspace_target)
        ),
        agent_docker.WorkspaceMount("bind", str(sessions), "/home/dev/sessions"),
        *(
            agent_docker.WorkspaceMount("bind", str(m.source), str(m.target), m.read_only)
            for m in mounts
        ),
        *(
            agent_docker.WorkspaceMount(m.kind, m.source, str(m.target), m.read_only)
            for m in declarations.mounts
        ),
    ]
    targets: set[str] = set()
    for mount in delivery:
        if mount.target in targets:
            raise MountSpecificationError(f"Duplicate workspace target: {mount.target}")
        targets.add(mount.target)
        if options.docker_mode is DockerMode.AGENT:
            mount.require_safe_target()
    workspace = [mount.compose() for mount in delivery]
    dev = fragment["services"]["dev"]
    dev["volumes"] = [
        *[
            v
            for v in dev.get("volumes", [])
            if (v.get("target") if isinstance(v, dict) else v.split(":")[1]) not in targets
        ],
        *workspace,
    ]
    if options.docker_mode is DockerMode.AGENT:
        fragment["services"]["agent-docker"] = {"volumes": list(workspace)}


def require_agent_profile(actual: dict[str, Any], manifest: dict[str, Any]) -> None:
    config, host = actual["Config"], actual["HostConfig"]
    expected_host = {
        **agent_docker.HOST_PROFILE,
        **manifest["limits"],
        "NetworkMode": manifest["network"],
        "MemorySwap": 2 * manifest["limits"]["Memory"],
    }
    expected_config: dict[str, Any] = {
        "User": manifest["user"],
        "Cmd": manifest["command"],
        "Entrypoint": manifest["entrypoint"],
        "Healthcheck": manifest["healthcheck"],
        "WorkingDir": "/",
        "ExposedPorts": {"2375/tcp": {}, "2376/tcp": {}},
    }
    if (
        actual["Image"] != manifest["image_id"]
        or config["User"] != "1000:1000"
        or config["Cmd"] != agent_docker.COMMAND
        or json.dumps({key: host[key] for key in expected_host}, sort_keys=True)
        != json.dumps(expected_host, sort_keys=True)
        or {key: config[key] for key in expected_config} != expected_config
        or sorted(config["Env"]) != sorted(manifest["environment"])
        or agent_docker.mount_delivery(actual["Mounts"])
        != sorted(tuple(m) for m in manifest["mounts"])
        or any(m["Type"] == "bind" and m["Propagation"] != "rprivate" for m in actual["Mounts"])
        or set(actual["NetworkSettings"]["Networks"]) != {manifest["network"]}
        or actual["NetworkSettings"]["Networks"][manifest["network"]]["NetworkID"]
        != manifest["network_id"]
    ):
        raise RuntimeError("Agent Docker profile differs from trusted generation delivery")


def inspect_agent_endpoint(
    dev: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    planned_generation: str | None = None,
) -> agent_docker.EndpointVerification:
    """Bounded host reads; the shared verifier itself has no runtime operations."""
    try:
        if dev is None:
            dev = host_runtime.inspect_object(
                name or _SERVICE_CONTAINER_NAMES["dev"], DOCKER_EXECUTABLE
            )
            if dev is not None and dev["State"]["Running"] is False:
                dev = None
        selected = agent_docker.endpoint_selection(dev)
        if not selected.in_use or selected.errors:
            return selected
        assert dev is not None
        state = host_runtime.read_state(host_runtime.runtime_root())
        if state is None:
            raise ValueError("host-owned agent Docker generation state unavailable")
        manifest = state["agent_docker"]
        companion = host_runtime.inspect_owned_resource(
            state["resources"]["agent-docker"], state["generation"], DOCKER_EXECUTABLE
        )
        objects = [
            host_runtime.inspect_object(manifest["image"], DOCKER_EXECUTABLE, "image"),
            host_runtime.inspect_object(manifest["endpoint_volume"], DOCKER_EXECUTABLE, "volume"),
            host_runtime.inspect_object(manifest["cache_volume"], DOCKER_EXECUTABLE, "volume"),
            host_runtime.inspect_object(manifest["network_id"], DOCKER_EXECUTABLE, "network"),
        ]
        if companion is None or any(obj is None for obj in objects):
            raise ValueError("recorded agent Docker resource absent")
        users = _run_captured(
            [
                DOCKER_EXECUTABLE,
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                "volume=" + manifest["endpoint_volume"],
            ],
            timeout=5,
        )
        if not users.success:
            raise ValueError("agent Docker endpoint consumers unavailable")
        image, endpoint, cache, network = objects
        assert (
            image is not None and endpoint is not None and cache is not None and network is not None
        )
        return agent_docker.verify_endpoint(
            dev,
            state,
            companion,
            image,
            endpoint,
            cache,
            network,
            users.stdout.split(),
            planned_generation=planned_generation,
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        return agent_docker.EndpointVerification("unknown", "unknown", True, errors=(str(exc),))


def _wait_agent_healthy(
    record: dict[str, Any], owner: host_runtime.GitRuntime, deadline: float
) -> None:
    while True:
        if owner.observer is None or owner.observer.poll() is not None:
            raise RuntimeError("Agent Docker requires a functioning host observer")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Agent Docker readiness timeout")
        actual = host_runtime.inspect_owned_resource(
            record, owner.generation, owner.docker_path, timeout=min(5, remaining)
        )
        if actual is None or not actual["State"]["Running"]:
            raise RuntimeError("Agent Docker exited before readiness")
        health = actual["State"].get("Health", {}).get("Status")
        if time.monotonic() >= deadline:
            raise RuntimeError("Agent Docker readiness timeout")
        if health == "healthy":
            return
        if health == "unhealthy" or time.monotonic() >= deadline:
            raise RuntimeError(f"Agent Docker health {health or 'unknown'} (readiness timeout)")
        time.sleep(0.1)


def _agent_firewall(
    owner: host_runtime.GitRuntime,
    manifest: dict[str, Any],
    *,
    clear: bool = False,
    deadline: float | None = None,
) -> None:
    service = "agent-docker-firewall"
    name = manifest["name"] + "-firewall"
    labels = {
        host_runtime.GENERATION_LABEL: owner.generation,
        "com.docker.compose.project": owner.project,
        "com.docker.compose.service": service,
    }
    if (
        host_runtime.inspect_object(
            name, owner.docker_path, timeout=_operation_timeout(deadline, 5)
        )
        is not None
    ):
        raise RuntimeError("Existing firewall initializer is not invocation-owned")
    command = (
        "rm -f /endpoint/firewall-ready"
        if clear
        else "init-firewall.sh && touch /endpoint/firewall-ready"
    )
    args = [
        owner.docker_path,
        "create",
        "--name",
        name,
        "--user",
        "0",
        "--network",
        "none" if clear else "container:" + owner.resources["agent-docker"]["id"],
        "--entrypoint",
        "/bin/bash",
        "--mount",
        f"type=volume,source={manifest['endpoint_volume']},target=/endpoint,volume-nocopy",
    ]
    if not clear:
        args.extend(["--cap-add", "NET_ADMIN"])
    for key, value in labels.items():
        args.extend(["--label", f"{key}={value}"])
    result = _run_captured(
        [*args, manifest["dev_image_id"], "-ec", command], timeout=_operation_timeout(deadline, 10)
    )
    actual = host_runtime.inspect_object(
        name, owner.docker_path, timeout=_operation_timeout(deadline, 5)
    )
    if actual is not None:
        owner.register(service, actual)
    if not result.success or actual is None:
        raise RuntimeError(result.stderr or "Firewall initializer creation failed")
    host = actual["HostConfig"]
    raw_caps: list[str] = host.get("CapAdd") or []
    caps = [cap.removeprefix("CAP_") for cap in raw_caps]
    mounts = actual["Mounts"]
    if (
        actual["Image"] != manifest["dev_image_id"]
        or actual["Config"]["User"] != "0"
        or actual["Config"]["Entrypoint"] != ["/bin/bash"]
        or actual["Config"]["Cmd"] != ["-ec", command]
        or host["NetworkMode"]
        != ("none" if clear else "container:" + owner.resources["agent-docker"]["id"])
        or caps != ([] if clear else ["NET_ADMIN"])
        or host["Privileged"]
        or host.get("Devices")
        or host.get("DeviceRequests")
        or host.get("PidMode")
        or host.get("UsernsMode")
        or host["RestartPolicy"]["Name"] != "no"
        or len(mounts) != 1
        or mounts[0]["Type"] != "volume"
        or mounts[0]["Name"] != manifest["endpoint_volume"]
        or mounts[0]["Destination"] != "/endpoint"
        or not mounts[0]["RW"]
    ):
        raise RuntimeError("Firewall initializer differs from trusted delivery")
    host_runtime.run_runtime_command(owner.docker_path, "start", actual["Id"])
    result = _run_captured(
        [owner.docker_path, "wait", actual["Id"]], timeout=_operation_timeout(deadline, 60)
    )
    if not result.success or result.stdout.strip() != "0":
        logs = _run_captured([owner.docker_path, "logs", actual["Id"]], timeout=5)
        raise RuntimeError(
            f"Agent Docker firewall failed (exit {result.stdout.strip() or result.returncode}): "
            + logs.stdout + logs.stderr + result.stderr
        )
    if host_runtime.inspect_owned_resource(
        owner.resources[service], owner.generation, owner.docker_path
    ):
        host_runtime.run_runtime_command(owner.docker_path, "rm", actual["Id"])
    owner.forget(service)


def _prepare_agent_docker(
    config: AppConfig,
    options: ContainerOptions,
    fragment: ComposeFragment,
    owner: host_runtime.GitRuntime,
) -> None:
    if owner.root is None or owner.observer is None or owner.observer.poll() is not None:
        raise RuntimeError("Agent Docker requires a functioning host observer")
    if _service_inspect("agent-docker") is not None:
        raise RuntimeError("Existing agent Docker has no invocation ownership; clean from the host")
    image = host_runtime.inspect_object(agent_docker.IMAGE, DOCKER_EXECUTABLE, "image")
    if image is None:
        pull = _run_captured([DOCKER_EXECUTABLE, "pull", agent_docker.IMAGE], timeout=120)
        if not pull.success:
            raise RuntimeError(pull.stderr or "Pinned agent Docker image pull failed")
        image = host_runtime.inspect_object(agent_docker.IMAGE, DOCKER_EXECUTABLE, "image")
    digest = agent_docker.IMAGE.split("@", 1)[1]
    if image is None or not any(r.endswith("@" + digest) for r in image.get("RepoDigests", [])):
        raise RuntimeError("Pinned agent Docker image digest is unverified")
    if image["Config"]["User"] != "rootless" or image["Config"]["Entrypoint"] != [
        "dockerd-entrypoint.sh"
    ]:
        raise RuntimeError("Pinned image rootless entrypoint is unverified")
    cache_definition = fragment.setdefault("volumes", {}).setdefault(
        "agent-docker-data", {"name": agent_docker.CACHE}
    )
    cache = str(cache_definition["name"])
    volume = host_runtime.inspect_object(cache, DOCKER_EXECUTABLE, "volume")
    if volume is not None and (volume["Driver"] != "local" or volume.get("Options")):
        raise RuntimeError("Agent Docker cache must use the plain local volume driver")
    endpoint = agent_docker.ENDPOINT_PREFIX + owner.generation
    if host_runtime.inspect_object(endpoint, DOCKER_EXECUTABLE, "volume") is not None:
        raise RuntimeError("Agent Docker endpoint already exists")
    labels = {
        host_runtime.GENERATION_LABEL: owner.generation,
        "com.docker.compose.project": owner.project,
    }
    args = [DOCKER_EXECUTABLE, "volume", "create", "--driver", "local"]
    for key, value in agent_docker.ENDPOINT_OPTIONS.items():
        args.extend(["--opt", f"{key}={value}"])
    for key, value in labels.items():
        args.extend(["--label", f"{key}={value}"])
    result = _run_captured([*args, endpoint], timeout=5)
    owner.register_volume(endpoint)
    if not result.success:
        raise RuntimeError(result.stderr or "Endpoint volume creation failed")
    volume = host_runtime.inspect_object(endpoint, DOCKER_EXECUTABLE, "volume")
    if (
        volume is None
        or volume["Driver"] != "local"
        or volume["Options"] != agent_docker.ENDPOINT_OPTIONS
    ):
        raise RuntimeError("Agent Docker endpoint options differ from trusted tmpfs profile")
    fragment.setdefault("volumes", {})["agent-docker-endpoint"] = {
        "external": True,
        "name": endpoint,
    }
    companion = fragment["services"]["agent-docker"]
    companion.update(
        image=str(image["Id"]),
        labels={host_runtime.GENERATION_LABEL: owner.generation},
        environment={"DJINN_FIREWALL_GATE": str(options.firewall_enabled).lower()},
    )
    companion.setdefault("volumes", []).extend(
        [
            {"type": "volume", "source": "agent-docker-data", "target": agent_docker.DATA_ROOT},
            {
                "type": "volume",
                "source": "agent-docker-endpoint",
                "target": agent_docker.ENDPOINT,
                "volume": {"nocopy": True},
            },
        ]
    )
    deadline = time.monotonic() + 60
    with _compose_override(fragment) as path:
        resolved = _run_compose(
            [*get_compose_files(DockerMode.AGENT), "-f", str(path), "config", "--format", "json"],
            config=config,
            cwd=get_project_root(),
            timeout=10,
        )
        if not resolved.success:
            raise RuntimeError(
                resolved.stderr or "Agent Docker Compose delivery cannot be resolved"
            )
        delivery = json.loads(resolved.stdout)
        spec = delivery["services"]["agent-docker"]
        dev_spec = delivery["services"]["dev"]
        dev_image = host_runtime.inspect_object(dev_spec["image"], DOCKER_EXECUTABLE, "image")
        if dev_image is None:
            raise RuntimeError("Dev image missing; run djinn build")
        expected = [
            (
                m["type"],
                (
                    delivery["volumes"][m["source"]]["name"]
                    if m["type"] == "volume"
                    else m["source"]
                ).replace("$$", "$"),
                m["target"].replace("$$", "$"),
                not m.get("read_only", False),
            )
            for m in spec["volumes"]
        ]
        network_name = delivery["networks"]["djinn-network"]["name"]
        network = host_runtime.inspect_object(network_name, DOCKER_EXECUTABLE, "network")
        if network is None:
            raise RuntimeError("Agent Docker network unavailable")
        environment = dict(item.split("=", 1) for item in image["Config"]["Env"])
        environment.update(
            {key: str(value).replace("$$", "$") for key, value in spec["environment"].items()}
        )
        healthcheck = spec["healthcheck"]
        # These intervals are fixed in the trusted Compose profile.
        if (healthcheck["interval"], healthcheck["timeout"], healthcheck["start_period"]) != (
            "2s",
            "5s",
            "1m0s",
        ):
            raise RuntimeError("Agent Docker healthcheck intervals differ")
        manifest: dict[str, Any] = {
            "image": agent_docker.IMAGE,
            "image_id": image["Id"],
            "environment": [f"{key}={value}" for key, value in environment.items()],
            "command": spec["command"],
            "user": spec["user"],
            "healthcheck": {
                "Test": [word.replace("$$", "$") for word in healthcheck["test"]],
                "Interval": 2_000_000_000,
                "Timeout": 5_000_000_000,
                "StartPeriod": 60_000_000_000,
                "Retries": healthcheck["retries"],
            },
            "cache_volume": cache,
            "network_id": network["Id"],
            "entrypoint": [word.replace("$$", "$") for word in spec["entrypoint"]],
            "name": spec["container_name"],
            "network": delivery["networks"]["djinn-network"]["name"],
            "endpoint_volume": endpoint,
            "dev_image_id": dev_image["Id"],
            "firewall": options.firewall_enabled,
            "mounts": expected,
            "limits": {},
        }
        state = host_runtime.read_state(owner.root)
        if state is None or state["generation"] != owner.generation:
            raise RuntimeError("Runtime generation changed")
        state["agent_docker"] = manifest
        host_runtime.save_state(owner.root, state)
        result = _run_compose(
            [
                *get_compose_files(DockerMode.AGENT),
                "-f",
                str(path),
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "agent-docker",
            ],
            config=config,
            cwd=get_project_root(),
            timeout=20,
        )
    actual = _service_inspect("agent-docker")
    if actual is not None:
        owner.register("agent-docker", actual)
    if not result.success or actual is None:
        raise RuntimeError(result.stderr or "Agent Docker creation failed")
    # Persist the expected resource fields from trusted Compose limits, never from daemon claims.
    limits = spec["deploy"]["resources"]
    memory = limits["limits"]["memory"]
    reservation = limits["reservations"]["memory"]
    manifest["limits"] = {
        "NanoCpus": int(float(limits["limits"]["cpus"]) * 1e9),
        "Memory": int(memory),
        "MemoryReservation": int(reservation),
    }
    state = host_runtime.read_state(owner.root)
    assert state is not None
    state["agent_docker"] = manifest
    host_runtime.save_state(owner.root, state)
    require_agent_profile(actual, manifest)
    if options.firewall_enabled:
        _agent_firewall(owner, manifest, deadline=deadline)
    _wait_agent_healthy(owner.resources["agent-docker"], owner, deadline)
    fragment["services"]["dev"].setdefault("volumes", []).append(
        {
            "type": "volume",
            "source": "agent-docker-endpoint",
            "target": agent_docker.DEV_ENDPOINT,
            "read_only": True,
            "volume": {"nocopy": True},
        }
    )


def resume_agent_docker(root: Path, state: dict[str, Any], docker_path: str) -> None:
    manifest = state["agent_docker"]
    record = state["resources"]["agent-docker"]
    actual = host_runtime.inspect_owned_resource(record, state["generation"], docker_path)
    if actual is None:
        raise RuntimeError("Owning agent Docker disappeared")
    require_agent_profile(actual, manifest)
    owner = host_runtime.GitRuntime(
        root,
        state["generation"],
        docker_path=docker_path,
        project=state.get("project", "djinn-in-a-box"),
        resources=state["resources"],
    )
    deadline = time.monotonic() + 60
    if manifest["firewall"]:
        _agent_firewall(owner, manifest, clear=True, deadline=deadline)
    host_runtime.run_runtime_command(docker_path, "start", record["id"])
    if manifest["firewall"]:
        _agent_firewall(owner, manifest, deadline=deadline)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Agent Docker restart readiness timeout")
        actual = host_runtime.inspect_owned_resource(
            record, owner.generation, docker_path, timeout=min(5, remaining)
        )
        if actual is None or not actual["State"]["Running"]:
            raise RuntimeError("Agent Docker restart failed")
        status = actual["State"].get("Health", {}).get("Status")
        if time.monotonic() >= deadline:
            raise RuntimeError("Agent Docker restart readiness timeout")
        if status == "healthy":
            return
        if status == "unhealthy" or time.monotonic() >= deadline:
            raise RuntimeError("Agent Docker restart readiness failed")
        time.sleep(0.1)


def _prepare_companions(
    config: AppConfig,
    options: ContainerOptions,
    fragment: ComposeFragment,
    owner: host_runtime.GitRuntime,
) -> None:
    # This runs only after declarations/caller mounts/env and ownership were checked.
    if options.docker_mode is DockerMode.AGENT:
        try:
            _prepare_agent_docker(config, options, fragment, owner)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError,
                subprocess.SubprocessError) as exc:
            raise RuntimeMountSpecificationError(f"Agent Docker preparation failed: {exc}") from exc
    endpoints = desktop.discover_desktop_endpoints()
    for endpoint in endpoints:
        if endpoint.available is False:
            continue
        deadline = time.monotonic() + desktop.HELPER_SECONDS
        actual = None
        try:
            if endpoint.error:
                raise RuntimeError(endpoint.error)
            if os.getuid() != host_runtime.CONTAINER_USER_UID:
                raise RuntimeError("desktop helpers require host UID 1000; unsupported credentials")
            if owner.observer is None or owner.observer.poll() is not None:
                raise RuntimeError("host observer unavailable")
            image = host_runtime.inspect_object(desktop.HELPER_IMAGE, DOCKER_EXECUTABLE, "image",
                timeout=_operation_timeout(deadline, 5))
            if image is None:
                raise RuntimeError("helper image missing; run djinn build")
            if _service_inspect(endpoint.service, deadline) is not None:
                raise RuntimeError(
                    "existing helper has no invocation ownership; clean from the host"
                )
            volume = host_runtime.inspect_object(endpoint.volume, DOCKER_EXECUTABLE, "volume",
                timeout=_operation_timeout(deadline, 5))
            if volume is not None:
                raise RuntimeError(
                    "existing desktop volume has no invocation ownership; clean from the host"
                )
            helper = desktop.helper_fragment(
                endpoint, str(image["Id"]), owner.generation, host_runtime.GENERATION_LABEL
            )
            helper["volumes"] = _desktop_volumes(endpoint, owner.generation)
            fragment["services"].update(helper["services"])
            fragment.setdefault("volumes", {}).update(helper["volumes"])
            with _compose_override(fragment) as path:
                result = _run_compose(
                    [
                        *get_compose_files(options.docker_mode),
                        "-f",
                        str(path),
                        "up",
                        "-d",
                        "--no-deps",
                        "--no-build",
                        "--pull",
                        "never",
                        "--force-recreate",
                        endpoint.service,
                    ],
                    config=config,
                    cwd=get_project_root(),
                    timeout=_operation_timeout(deadline, desktop.HELPER_SECONDS),
                )
            owner.register_volume(endpoint.volume, timeout=_operation_timeout(deadline, 5))
            actual = _service_inspect(endpoint.service, deadline)
            if actual is not None:
                owner.register(endpoint.service, actual, endpoint.volume)
            if not result.success or actual is None:
                raise RuntimeError(result.stderr or "helper creation failed")
            while True:
                actual = host_runtime.inspect_object(str(actual["Id"]), DOCKER_EXECUTABLE,
                                                     timeout=_operation_timeout(deadline, 5))
                if actual is None or not actual.get("State", {}).get("Running"):
                    raise RuntimeError("helper is not running")
                health = actual["State"].get("Health", {}).get("Status")
                if health == "healthy":
                    break
                if health == "unhealthy" or time.monotonic() >= deadline:
                    raise RuntimeError(f"helper health {health or 'unknown'} (readiness timeout)")
                time.sleep(0.1)
            evidence = _helper_evidence(actual, endpoint.channel, deadline)
            reasons = desktop.helper_verification_reasons(
                endpoint, actual, image, evidence, desktop.image_policy()
            )
            if reasons:
                raise RuntimeError("; ".join(reasons))
            _downstream_probe(config, options, endpoint, owner, deadline)
            desktop.add_delivery(fragment, endpoint)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            warning(f"Desktop {endpoint.channel} missing: {exc}; starting without this endpoint")
            # Only resources already proven invocation-owned may be removed here.
            record = owner.resources.get(endpoint.service)
            if record is not None:
                try:
                    if host_runtime.inspect_owned_resource(
                        record, owner.generation, DOCKER_EXECUTABLE
                    ):
                        result = _run_captured(
                            [DOCKER_EXECUTABLE, "rm", "-f", record["id"]], timeout=5
                        )
                        if result.success:
                            owner.forget(endpoint.service)
                        else:
                            warning(f"Failed to remove desktop helper: {result.stderr}")
                except (
                    OSError, ValueError, RuntimeError, subprocess.SubprocessError
                ) as cleanup_error:
                    warning(f"Desktop cleanup preserved resources: {cleanup_error}")
            try:
                owner.discard_volume(endpoint.volume)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as cleanup_error:
                warning(f"Desktop volume cleanup preserved resources: {cleanup_error}")
            fragment["services"].pop(endpoint.service, None)


def inspect_running_desktop(
    dev_id: str = "djinn", *, dev_inspect: dict[str, Any] | None = None
) -> desktop.DesktopInspection:
    endpoints = desktop.discover_desktop_endpoints()
    helpers: dict[str, dict[str, Any] | None] = {}
    versions: dict[str, dict[str, Any]] = {}
    try:
        dev = (
            dev_inspect
            if dev_inspect is not None
            else host_runtime.inspect_object(dev_id, DOCKER_EXECUTABLE)
        )
        image = host_runtime.inspect_object(desktop.HELPER_IMAGE, DOCKER_EXECUTABLE, "image")
        for endpoint in endpoints:
            actual = _service_inspect(endpoint.service)
            helpers[endpoint.service] = actual
            if actual is not None and actual.get("State", {}).get("Running"):
                try:
                    versions[endpoint.service] = _helper_evidence(actual, endpoint.channel)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    versions[endpoint.service] = {}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        dev, image = None, None
    return desktop.inspect_desktop_endpoints(
        dev, helpers, {desktop.HELPER_IMAGE: image}, endpoints, versions
    )


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
    # Fail before allocating a TTY: a background start would otherwise degenerate
    # into a SIGTTOU storm that kills the container. Headless runs pass -T and are
    # unaffected.
    if interactive and is_background_process_group():
        return RunResult(returncode=1, stderr=BACKGROUND_START_ERROR)

    _validate_desktop_env(env)
    project_root = get_project_root()

    # Build compose command
    compose_files = get_compose_files(options.docker_mode)
    # Map service to fixed container name (matches container_name in compose YAML)
    container_name = _SERVICE_CONTAINER_NAMES.get(service, f"djinn-{service}")

    cmd = [DOCKER_EXECUTABLE, "compose", *compose_files, "run", "--rm", "--name", container_name]

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
    sops_args = _canonicalize_runtime_mount_args(get_sops_age_key_mount_args(config))
    zone_overlay_args, zone_overlay_targets = _zone_overlay_mount_args_and_targets(config)
    declarations = resolve_declared_entries(
        config,
        options,
        runtime_targets=_reserved_mount_targets(
            config,
            DockerMode.DIRECT,
            shell_args=shell_args,
            sops_args=sops_args,
            zone_overlay_targets=zone_overlay_targets,
        ),
        caller_env=env,
    )
    declarations.require_valid()
    validate_container_mounts(
        mounts,
        config,
        options.docker_mode,
        shell_args=shell_args,
        sops_args=sops_args,
        zone_overlay_targets=zone_overlay_targets,
    )

    if options.docker_mode is not DockerMode.AGENT or service != "dev":
        for mount in mounts:
            target = _resolve_image_aliases(mount.target)
            spec = f"{mount.source}:{target}" + (":ro" if mount.read_only else "")
            cmd.extend(["-v", spec])

    if mounts:
        workdir = mounts[0].target
        cmd.extend(["--workdir", str(workdir)])

    # Shell mounts (skip_mounts check is inside get_shell_mount_args)
    cmd.extend(zone_overlay_args)
    cmd.extend(shell_args)
    cmd.extend(sops_args)

    # Service name
    cmd.append(service)

    # Command to execute
    if command is not None:
        cmd.extend(["-c", command])

    # Host interpolation env for the compose file's ${...} vars (CODE_DIR,
    # DJINN_WORKSPACE_TARGET, DJINN_CONFIG_ROOT, TZ, resources). This is DISTINCT from the container
    # `-e` vars built above: docker compose interpolates the file at parse time
    # from the host subprocess environment, so it must be set here.
    host_env = _compose_host_env(config)
    fragment: ComposeFragment = (
        declarations.compose_fragment() if service == "dev" else {"services": {service: {}}}
    )
    if service == "dev":
        fragment["services"]["dev"].setdefault("environment", {}).update(
            dict.fromkeys(desktop.MANAGED_ENV)
        )
    if service == "dev" and options.docker_mode is DockerMode.AGENT:
        _prepare_workspace(config, options, declarations, mounts, fragment)
    owner_context = git_runtime(config, container_name) if service == "dev" else nullcontext(None)
    with owner_context as git_delivery:
        if git_delivery is not None:
            git_delivery.add_to_fragment(fragment)
            _prepare_companions(config, options, fragment, git_delivery)
            _require_agent_observer(options, git_delivery)
            git_delivery.begin_creation()
        with _compose_override(fragment, prefix="djinn-run-") as override_path:
            cmd[2 + len(compose_files) : 2 + len(compose_files)] = ["-f", str(override_path)]
            if git_delivery is not None:
                _guard_dev_creation(config, compose_files, override_path, git_delivery,
                                    _volume_specs_from_mount_args(cmd), env_vars)
                _require_agent_observer(options, git_delivery)
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
                        owner=git_delivery,
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
                    owner=git_delivery,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
            except subprocess.TimeoutExpired as e:
                assert timeout is not None  # TimeoutExpired only raised when timeout is set
                stdout, stderr = _decode_timeout_output(e, timeout)
                return RunResult(returncode=124, stdout=stdout, stderr=stderr, owner=git_delivery)
            except FileNotFoundError as e:
                return RunResult(
                    returncode=127,
                    owner=git_delivery,
                    stdout="",
                    stderr=f"Docker command not found: {e}",
                )
            except PermissionError as e:
                return RunResult(
                    returncode=126,
                    owner=git_delivery,
                    stdout="",
                    stderr=f"Permission denied: {e}",
                )


def _volume_specs_from_mount_args(args: list[str]) -> list[str]:
    """Pull the ``src:dst[:mode]`` specs out of a ``["-v", spec, ...]`` list."""
    specs: list[str] = []
    expecting_spec = False
    for arg in args:
        if expecting_spec:
            specs.append(arg)
            expecting_spec = False
        elif arg == "-v":
            expecting_spec = True
    return specs


def _env_pairs_from_mount_args(args: list[str]) -> dict[str, str]:
    """Pull the ``KEY=VALUE`` pairs out of a ``["-e", pair, ...]`` list.

    The runtime mount builders emit a socket *and* the variable that points at
    it — the SOPS identity pairs its bind with ``SOPS_AGE_KEY_FILE``. Keeping only the
    ``-v`` half mounts the socket and leaves every client unable to find it, so
    the detached path has to carry these across too.
    """
    pairs: dict[str, str] = {}
    expecting_pair = False
    for arg in args:
        if expecting_pair:
            key, _, value = arg.partition("=")
            pairs[key] = value
            expecting_pair = False
        elif arg == "-e":
            expecting_pair = True
    return pairs


def compose_up_detached(
    config: AppConfig,
    options: ContainerOptions,
    *,
    env: dict[str, str] | None = None,
    service: str = "dev",
    shell_mount_args: list[str] | None = None,
) -> RunResult:
    """Start the container in the background via ``docker compose up -d``.

    This path exists because ``compose run`` keeps a host-side foreground client
    attached to a TTY for as long as the container lives. Backgrounding that
    client (``djinn start ... &``) turns every terminal-attribute call into a
    SIGTTOU storm that ends with the host-side client stopped and ``--rm`` reaping
    the container — see
    ``is_background_process_group``. ``up -d`` leaves no client behind at all.

    ``run`` accepts per-invocation ``-v``/``--workdir``/``-e`` flags; ``up`` does
    not, so the same dynamic mounts reach Compose as a generated override file.
    It is written as JSON — valid YAML, so this needs no YAML dependency — and
    carries only absolute paths, which keeps it independent of Compose's
    project-directory resolution.

    The container keeps the TTY declared by ``tty``/``stdin_open`` in the compose
    file, so the interactive zsh ending the entrypoint stays alive with nothing
    attached. Attach afterwards with ``djinn enter``.
    """
    _validate_desktop_env(env)
    project_root = get_project_root()
    compose_files = get_compose_files(options.docker_mode)

    mounts = tuple(
        ContainerMount(mount.source, _normalize_mount_target(mount.target), mount.read_only)
        for mount in options.mounts
    )
    shell_args = _canonicalize_runtime_mount_args(
        get_shell_mount_args(config) if shell_mount_args is None else shell_mount_args
    )
    sops_args = _canonicalize_runtime_mount_args(get_sops_age_key_mount_args(config))
    zone_overlay_args, zone_overlay_targets = _zone_overlay_mount_args_and_targets(config)
    declarations = resolve_declared_entries(
        config,
        options,
        runtime_targets=_reserved_mount_targets(
            config,
            DockerMode.DIRECT,
            shell_args=shell_args,
            sops_args=sops_args,
            zone_overlay_targets=zone_overlay_targets,
        ),
        caller_env=env,
    )
    declarations.require_valid()
    validate_container_mounts(
        mounts,
        config,
        options.docker_mode,
        shell_args=shell_args,
        sops_args=sops_args,
        zone_overlay_targets=zone_overlay_targets,
    )

    volume_specs: list[str] = []
    if options.docker_mode is not DockerMode.AGENT or service != "dev":
        for mount in mounts:
            target = _resolve_image_aliases(mount.target)
            volume_specs.append(f"{mount.source}:{target}" + (":ro" if mount.read_only else ""))
    # Detached startup mounts every assigned overlay so writes reach its host zone.
    volume_specs.extend(_volume_specs_from_mount_args(zone_overlay_args))
    runtime_args = [*shell_args, *sops_args]
    volume_specs.extend(_volume_specs_from_mount_args(runtime_args))
    # The `-e` half of those same pairs has to ride along, or the sockets (and
    # the SOPS identity) are mounted but unreachable. Explicit `env` wins over
    # the derived values.
    environment = {**_env_pairs_from_mount_args(runtime_args), **(env or {})}

    service_override: ComposeService = {}
    if volume_specs:
        service_override["volumes"] = list(volume_specs)
    if mounts:
        service_override["working_dir"] = str(mounts[0].target)
    if environment:
        service_override["environment"] = dict(environment)

    fragment: ComposeFragment = (
        declarations.compose_fragment() if service == "dev" else {"services": {service: {}}}
    )
    if service == "dev":
        fragment["services"]["dev"].setdefault("environment", {}).update(
            dict.fromkeys(desktop.MANAGED_ENV)
        )
    declared_service = fragment["services"][service]
    service_override["volumes"] = [*volume_specs, *declared_service.get("volumes", [])]
    service_override["environment"] = {**environment, **declared_service.get("environment", {})}
    fragment["services"][service] = service_override
    container_name = _SERVICE_CONTAINER_NAMES.get(service, f"djinn-{service}")
    if service == "dev" and options.docker_mode is DockerMode.AGENT:
        _prepare_workspace(config, options, declarations, mounts, fragment)
    owner_context = git_runtime(config, container_name) if service == "dev" else nullcontext(None)
    with owner_context as git_delivery:
        if git_delivery is not None:
            git_delivery.add_to_fragment(fragment)
            _prepare_companions(config, options, fragment, git_delivery)
            _require_agent_observer(options, git_delivery)
            git_delivery.begin_creation()
        with _compose_override(fragment) as override_path:
            if git_delivery is not None:
                _guard_dev_creation(config, compose_files, override_path, git_delivery)
                _require_agent_observer(options, git_delivery)
            args = [*compose_files, "-f", str(override_path)]
            args.extend(["up", "-d", service])
            # ENABLE_FIREWALL rides the compose file's ${ENABLE_FIREWALL:-false}
            # interpolation, because `up` has no per-invocation `-e` flag to carry it.
            result = _run_compose(
                args,
                config=config,
                cwd=project_root,
                extra_env={
                    "ENABLE_FIREWALL": str(options.firewall_enabled).lower(),
                    # Tells the entrypoint nobody will ever use PID 1's shell here:
                    # consumers attach with `djinn enter`, which brings its own TTY.
                    "DJINN_DETACHED": "true",
                },
            )
            if result.success and git_delivery is not None:
                _require_agent_observer(options, git_delivery)
                git_delivery.retain()
                try:
                    _require_agent_observer(options, git_delivery)
                except RuntimeMountSpecificationError:
                    git_delivery.detached = False
                    raise
            return result


SELF_TEARDOWN_ERROR = (
    "Refusing to tear down the container this process is running inside.\n"
    "\n"
    "`docker compose down` selects by project name, which docker-compose.yml pins to\n"
    "`djinn-in-a-box` — so it reaps the running dev container no matter which copy of\n"
    "the repo it runs from, including a throwaway one. With the docker socket mounted,\n"
    "anything in here can do that to itself: a test, an agent, a stray command.\n"
    "\n"
    "Run teardown from the host instead:\n"
    "  djinn clean\n"
)


def service_container_name(service: str) -> str:
    return _SERVICE_CONTAINER_NAMES[service]


def planned_dev_inspection(
    delivery: dict[str, Any],
    extra_volumes: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolved creator delivery in the same shape consumed by actual assessment."""
    service = delivery["services"]["dev"]
    mounts: list[dict[str, Any]] = []
    for row in service.get("volumes", []):
        source = row.get("source", "")
        mount = {
            "Type": row["type"],
            "Source": source.replace("$$", "$"),
            "Destination": row["target"].replace("$$", "$"),
            "RW": not row.get("read_only", False),
            "Propagation": row.get("bind", {}).get("propagation", "rprivate")
            if row["type"] == "bind"
            else "",
        }
        if row["type"] == "volume":
            name = delivery["volumes"][source]["name"]
            actual = host_runtime.inspect_object(name, DOCKER_EXECUTABLE, "volume")
            definition = delivery["volumes"][source]
            mount.update(
                Name=name,
                Source=actual["Mountpoint"] if actual else "",
                PlannedVolume=actual
                if actual
                else {
                    "Driver": definition.get("driver", "local"),
                    "Options": definition.get("driver_opts") or {},
                },
            )
        mounts.append(mount)
    for spec in extra_volumes or []:
        parts = spec.split(":")
        mounts = [m for m in mounts if m["Destination"] != parts[1]]
        mounts.append(
            {
                "Type": "bind",
                "Source": parts[0],
                "Destination": parts[1],
                "RW": len(parts) < 3 or parts[2] != "ro",
                "Propagation": "rprivate",
            }
        )
    environment = {**service.get("environment", {}), **(extra_env or {})}
    networks = {}
    for name in service.get("networks", {}):
        network_name = delivery["networks"][name]["name"]
        network = host_runtime.inspect_object(network_name, DOCKER_EXECUTABLE, "network")
        if network is None:
            raise ValueError("planned network unavailable")
        networks[network_name] = {"NetworkID": network["Id"]}
    return {
        "Id": "planned",
        "State": {"Running": True},
        "Mounts": mounts,
        "Config": {
            "Env": [f"{k}={v}" for k, v in environment.items() if v is not None],
            "Labels": service.get("labels", {}),
            "Image": service["image"],
        },
        "HostConfig": {"NetworkMode": service.get("network_mode", "bridge")},
        "NetworkSettings": {"Networks": networks},
    }


def _guard_dev_creation(
    config: AppConfig,
    compose_files: list[str],
    override: Path,
    owner: host_runtime.GitRuntime,
    extra_volumes: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> None:
    from djinn_in_a_box.core import hostctl

    with hostctl.control_guard():
        helper = hostctl.inspect_helper()
        if helper is None or not helper["State"]["Running"]:
            return
    planned: dict[str, Any] = {"Id": "planned"}
    try:
        result = _run_compose(
            [*compose_files, "-f", str(override), "config", "--format", "json"],
            config=config,
            cwd=get_project_root(),
            timeout=10,
        )
        if not result.success:
            raise RuntimeError("resolved Compose delivery unavailable")
        delivery = json.loads(result.stdout)
        planned = planned_dev_inspection(delivery, extra_volumes, extra_env)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError):
        # Incomplete delivery is an uncertain assessment; close before proceeding.
        pass
    hostctl.guard_dev_start(planned, owner.generation)


def is_own_container(name: str) -> bool:
    """True when ``name`` is the container this process is running inside.

    Cheap and deliberately conservative: ``/.dockerenv`` plus a hostname match,
    because the compose file sets ``container_name`` and ``hostname`` to the same
    value. A renamed container falls through and is not protected — best-effort is
    the right trade here, since the alternative costs a Docker round-trip on every
    teardown.
    """
    try:
        if not Path("/.dockerenv").exists():
            return False
        return socket.gethostname() == name
    except OSError:
        return False


def compose_down(config: AppConfig | None = None) -> RunResult:
    """Config-independent teardown under the same guard, dev before helpers."""
    if is_own_container(_SERVICE_CONTAINER_NAMES["dev"]):
        return RunResult(returncode=1, stderr=SELF_TEARDOWN_ERROR)
    from djinn_in_a_box.core import hostctl

    observer_identity = None
    try:
        with hostctl.control_guard(), host_runtime.creation_guard() as root:
            state = host_runtime.read_state(root)
            if state is not None:
                for record in state.get("resources", {}).values():
                    host_runtime.inspect_owned_resource(
                        record, state["generation"], DOCKER_EXECUTABLE
                    )
                for name in state.get("volumes", []):
                    volume = host_runtime.inspect_object(name, DOCKER_EXECUTABLE, "volume")
                    labels: dict[str, Any] = (volume.get("Labels") or {}) if volume else {}
                    if volume and labels.get(host_runtime.GENERATION_LABEL) != state["generation"]:
                        return RunResult(
                            1, stderr="runtime volume ownership changed; preserving resources"
                        )
                if "agent_docker" in state:
                    listed = _run_captured(
                        [
                            DOCKER_EXECUTABLE,
                            "ps",
                            "-aq",
                            "--no-trunc",
                            "--filter",
                            f"label=com.docker.compose.project={COMPOSE_PROJECT}",
                        ],
                        timeout=5,
                    )
                    owned = {r["id"] for r in state["resources"].values()}
                    if state.get("dev_id"):
                        owned.add(state["dev_id"])
                    if not listed.success or set(listed.stdout.split()) - owned:
                        return RunResult(
                            1, stderr="unknown project resources; preserving resources"
                        )
            hostctl.stop_helper_locked(remove=True)
            actual = host_runtime.inspect_object(_SERVICE_CONTAINER_NAMES["dev"], DOCKER_EXECUTABLE)
            if actual is not None:
                labels: dict[str, Any] = actual.get("Config", {}).get("Labels") or {}
                if labels.get("com.docker.compose.project") != COMPOSE_PROJECT:
                    return RunResult(1, stderr="dev ownership is unknown; preserving resources")
                result = _run_captured(
                    [DOCKER_EXECUTABLE, "rm", "-f", str(actual["Id"])], timeout=10
                )
                if not result.success:
                    return result
                if (
                    host_runtime.inspect_object(_SERVICE_CONTAINER_NAMES["dev"], DOCKER_EXECUTABLE)
                    is not None
                ):
                    return RunResult(1, stderr="dev termination could not be verified")
            result = (
                RunResult(0)
                if state and "agent_docker" in state
                else _run_compose(
                    [*get_compose_files(), "down", "--remove-orphans"],
                    config=None,
                    cwd=get_project_root(),
                    timeout=15,
                )
            )
            if not result.success:
                return result
            if state is not None:
                if not host_runtime.cleanup_owned(root, state["generation"], DOCKER_EXECUTABLE):
                    return RunResult(1, stderr="runtime ownership changed during clean")
                observer_identity = (state["observer_pid"], state["observer_token"])
                if state.get("agent_pid", -1) > 0:
                    host_runtime.stop_owned_process(state["agent_pid"], state["agent_token"])
                host_runtime.clear_state(root, state["generation"])
            for name in DESKTOP_RUNTIME_VOLUMES:
                host_runtime.remove_runtime_volume(name, DOCKER_EXECUTABLE)
        # The observer's final cleanup needs the guard; never join it while locked.
        if observer_identity is not None:
            host_runtime.stop_owned_process(*observer_identity)
        return result
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return RunResult(1, stderr=f"Cleanup preserved resources: {exc}")


def is_container_running(name: str) -> bool:
    names = _docker_list(
        [DOCKER_EXECUTABLE, "ps", "--format", "{{.Names}}", "--filter", f"name=^{name}$"]
    )
    return names is not None and name in names


def get_running_containers(prefix: str = "djinn") -> list[str] | None:
    return _docker_list(
        [DOCKER_EXECUTABLE, "ps", "--format", "{{.Names}}", "--filter", f"name={prefix}"]
    )


def volume_exists(name: str) -> bool:
    return _docker_inspect("volume", name)


def delete_volume(name: str) -> bool:
    if name in PROTECTED_INTERNAL_VOLUMES:
        warning("Hostctl identity is protected; only djinn clean all deletes it")
        return False
    if name in DESKTOP_RUNTIME_VOLUMES:
        try:
            with host_runtime.creation_guard():
                if host_runtime.inspect_dev("djinn", DOCKER_EXECUTABLE) is not None:
                    raise RuntimeError("dev still owns desktop runtime")
                host_runtime.remove_runtime_volume(name, DOCKER_EXECUTABLE)
            return True
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            warning(f"Preserving runtime volume '{name}': {exc}")
            return False
    result = _run_captured([DOCKER_EXECUTABLE, "volume", "rm", name])
    if not result.success:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        warning(f"Failed to delete volume '{name}': {detail}")
    return result.success


def delete_volumes(names: list[str]) -> dict[str, bool]:
    return {name: delete_volume(name) for name in names}


def get_existing_volumes_by_category(
    category: str, config: AppConfig | None = None
) -> list[str]:
    defined_volumes = volume_categories(config).get(category, [])
    return [vol for vol in defined_volumes if volume_exists(vol)]


def backup_volume(name: str, dest_dir: Path) -> RunResult:
    if name in PROTECTED_INTERNAL_VOLUMES:
        return RunResult(1, stderr="Hostctl identity must never be backed up")
    return _run_captured(
        [
            DOCKER_EXECUTABLE,
            "run",
            "--rm",
            "-v",
            f"{name}:/source:ro",
            "-v",
            f"{dest_dir}:/backup",
            "alpine",
            "tar",
            "czf",
            f"/backup/{name}.tar.gz",
            "-C",
            "/source",
            ".",
        ]
    )


def restore_volume(name: str, source_dir: Path) -> RunResult:
    if name in PROTECTED_INTERNAL_VOLUMES:
        return RunResult(1, stderr="Hostctl identity must never be restored")
    archive_path = source_dir / f"{name}.tar.gz"
    if not archive_path.exists():
        return RunResult(returncode=1, stdout="", stderr=f"Archive not found: {archive_path}")

    return _run_captured(
        [
            DOCKER_EXECUTABLE,
            "run",
            "--rm",
            "-v",
            f"{name}:/data",
            "-v",
            f"{source_dir}:/backup:ro",
            "alpine",
            # Clear volume (.[!.]* matches dotfiles except . and ..) then extract backup
            "sh",
            "-c",
            'rm -rf /data/* /data/.[!.]* && tar xzf "/backup/$1.tar.gz" -C /data',
            "--",
            name,
        ]
    )


# =============================================================================
# Config root + host-env provisioning
# =============================================================================


def get_config_root(config: AppConfig | None = None) -> Path:
    """Resolve the config/credential root directory.

    Precedence: env ``DJINN_CONFIG_ROOT`` → ``config.config_root`` → default
    ``~/.djinn/config``.
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
        _ensure_zone_root(root)
    return roots


def _ensure_zone_root(root: Path) -> None:
    try:
        info = root.lstat()
    except FileNotFoundError:
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = root.lstat()
        except OSError as error:
            msg = f"Cannot create zone root {root}: {error}"
            raise ZoneRootValidationError(msg) from error
    except OSError as error:
        msg = f"Cannot inspect zone root {root}: {error}"
        raise ZoneRootValidationError(msg) from error

    if stat.S_ISLNK(info.st_mode):
        msg = f"Zone root must not be a symlink: {root}"
        raise ZoneRootValidationError(msg)
    if not stat.S_ISDIR(info.st_mode):
        msg = f"Zone root is not a directory: {root}"
        raise ZoneRootValidationError(msg)
    try:
        root.chmod(0o700)
    except OSError as error:
        msg = f"Cannot secure zone root {root}: {error}"
        raise ZoneRootValidationError(msg) from error


def _ensure_bind_target_directory(config_root: Path, agent: str, relative_path: Path) -> None:
    directory = config_root / agent
    for component in relative_path.parts:
        directory /= component
        try:
            info = directory.lstat()
        except FileNotFoundError:
            try:
                directory.mkdir(mode=0o700, exist_ok=True)
                info = directory.lstat()
            except OSError as error:
                msg = f"Cannot create bind-mount target {directory}: {error}"
                raise ZoneRootValidationError(msg) from error
        except OSError as error:
            msg = f"Cannot inspect bind-mount target {directory}: {error}"
            raise ZoneRootValidationError(msg) from error

        if stat.S_ISLNK(info.st_mode):
            msg = f"Bind-mount target must not be a symlink: {directory}"
            raise ZoneRootValidationError(msg)
        if not stat.S_ISDIR(info.st_mode):
            msg = f"Bind-mount target is not a directory: {directory}"
            raise ZoneRootValidationError(msg)


def _ensure_bind_target_file(config_root: Path, agent: str, relative_path: Path) -> None:
    _ensure_bind_target_directory(config_root, agent, relative_path.parent)
    target = config_root / agent / relative_path
    try:
        info = target.lstat()
    except FileNotFoundError:
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            info = target.lstat()
        except OSError as error:
            msg = f"Cannot create bind-mount target {target}: {error}"
            raise ZoneRootValidationError(msg) from error
    except OSError as error:
        msg = f"Cannot inspect bind-mount target {target}: {error}"
        raise ZoneRootValidationError(msg) from error

    if stat.S_ISLNK(info.st_mode):
        msg = f"Bind-mount target must not be a symlink: {target}"
        raise ZoneRootValidationError(msg)
    if not stat.S_ISREG(info.st_mode):
        msg = f"Bind-mount target is not a regular file: {target}"
        raise ZoneRootValidationError(msg)


def workflow_image_compatible(
    image: str = _WORKFLOW_IMAGE,
) -> WorkflowImageCompatibility:
    try:
        result = subprocess.run(
            [
                DOCKER_EXECUTABLE,
                "image",
                "inspect",
                image,
                "--format",
                '{{ index .Config.Labels "djinn.workflow.publisher" }}',
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
            [DOCKER_EXECUTABLE, "info"],
            capture_output=True,
            text=True,
            timeout=_WORKFLOW_IMAGE_INSPECT_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def ensure_host_env(config: AppConfig | None = None) -> None:
    """Idempotently create host bind-mount sources and nested config-root targets.

    The compose file mounts these paths unconditionally; if a source is missing
    when ``docker compose`` runs, the root Docker daemon auto-creates it
    root-owned. Creating them here (user-owned, before any compose call) prevents
    that footgun. Single host-provisioning routine reached through two entry
    paths: ``init``, ``doctor --fix``, and the ``build`` preflight call it
    directly; ``start``, ``run``, and container-mode ``session`` reach it via
    ``prepare_config_workflow(require_compose_host_env=True)``. ``start`` opts
    out of the *preflight* provisioning only (``provision_host=False``) — it
    still provisions through that workflow path before Compose runs.

    Provisions every assigned zone overlay source and every nested bind-mount
    target inside a config-root agent mount, compose-mounted credential
    subdir (``SYNC_PATHS['credentials']``) and the fixed extras. ``repo-dotfiles``
    is intentionally NOT provisioned: it is a host-side input read by
    ``_sync_build_files`` (a no-op when absent), not a compose bind-mount, so it
    cannot trigger the root-owned-mount footgun.
    """
    # Zone resolution imports this module; load assignments at the runtime boundary.
    from djinn_in_a_box.config.zones import ZONE_CONTAINER_TARGETS, load_zone_assignments

    roots = ensure_zone_roots(config)
    assignments = load_zone_assignments(config)
    zone_roots = {"local": roots.local_root, "shared": roots.shared_root}
    for agent, by_zone in assignments.by_agent.items():
        for zone in ("local", "shared"):
            for relative_path in by_zone[zone]:
                directory = zone_roots[zone]
                for component in (agent, *relative_path.parts):
                    directory /= component
                    _ensure_zone_root(directory)

    root = roots.config_root
    for name in SYNC_PATHS.get("credentials", []):
        # 0700: credential stores hold secrets (OAuth tokens, age identities).
        path = root / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    for agent, agent_root in ZONE_CONTAINER_TARGETS.items():
        target_kinds = {
            target.relative_to(agent_root): _COMPOSE_DEV_MOUNT_TARGETS[target]
            for target in repo_owned_submount_targets(agent_root)
        }
        for relative_paths in assignments.by_agent[agent].values():
            target_kinds.update(dict.fromkeys(relative_paths, "directory"))
        for relative_path, kind in target_kinds.items():
            if kind == "file":
                _ensure_bind_target_file(root, agent, relative_path)
            else:
                _ensure_bind_target_directory(root, agent, relative_path)

    djinn_dir = Path.home() / ".djinn"
    for sub in ("sessions", "backups"):
        (djinn_dir / sub).mkdir(parents=True, exist_ok=True)

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
        _remove_sync_path_item(item)


def _remove_sync_path_item(path: Path) -> bool:
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            if not _remove_sync_path_item(child):
                return False
        try:
            path.rmdir()
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EBUSY, errno.EPERM, None}:
                raise
            if next(path.iterdir(), None) is None:
                return False
            raise
        return True
    path.unlink()
    return True


def is_sync_archive(archive_name: str) -> bool:
    """Return True if archive filename follows the sync-path naming convention."""
    return archive_name.startswith(_SYNC_ARCHIVE_PREFIX)


def extract_sync_path_name(archive_name: str) -> str:
    """Extract the sync path subdir name from a djinn-sync-<name>.tar.gz filename."""
    return archive_name.removeprefix(_SYNC_ARCHIVE_PREFIX).removesuffix(".tar.gz")
