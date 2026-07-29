"""Docker and Docker Compose operations for Djinn in a Box."""

from __future__ import annotations

import os
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
from djinn_in_a_box.core.paths import get_project_root
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
class ContainerOptions:
    """Options for container execution (Docker access, firewall, mounts)."""

    docker_mode: DockerMode = DockerMode.NONE
    """Docker access mode (none, proxy, or direct)."""

    firewall_enabled: bool = False
    """Enable network firewall (restricts outbound traffic)."""

    mount_path: Path | None = None
    """Additional workspace mount path (maps to ~/workspace in container)."""


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

    # Workspace mount
    if options.mount_path is not None:
        mount_str = f"{options.mount_path}:/home/dev/workspace"
        cmd.extend(["-v", mount_str])
        cmd.extend(["--workdir", "/home/dev/workspace"])

    # Shell mounts (skip_mounts check is inside get_shell_mount_args)
    cmd.extend(get_shell_mount_args(config))
    cmd.extend(get_audio_mount_args())
    cmd.extend(get_dbus_mount_args())

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
    root = get_config_root(config)
    for name in SYNC_PATHS.get("credentials", []):
        # 0700: credential stores hold secrets (OAuth tokens, age identities).
        # Applies on creation only, matching the ~/.ssh precedent below.
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)

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
