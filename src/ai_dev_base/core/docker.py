"""Docker and Docker Compose operations for AI Dev Base."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_dev_base.config.models import AppConfig

from ai_dev_base.core.paths import get_project_root

# =============================================================================
# Data Classes for Container Options and Results
# =============================================================================


@dataclass
class ContainerOptions:
    """Options for container execution (Docker access, firewall, mounts)."""

    docker_enabled: bool = False
    """Enable Docker socket access via proxy."""

    docker_direct: bool = False
    """Enable direct Docker socket access (no proxy)."""

    firewall_enabled: bool = False
    """Enable network firewall (restricts outbound traffic)."""

    mount_path: Path | None = None
    """Additional workspace mount path (maps to ~/workspace in container)."""

    shell_mounts: bool = True
    """Include shell configuration mounts (zshrc, oh-my-zsh, oh-my-posh)."""


@dataclass
class RunResult:
    """Result of a container execution."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def success(self) -> bool:
        """Check if the command succeeded (returncode == 0)."""
        return self.returncode == 0


# =============================================================================
# Docker Query Helpers
# =============================================================================


def _docker_inspect(resource: str, name: str) -> bool:
    """Check if a Docker resource exists via inspect. Returns True if found."""
    result = subprocess.run(["docker", resource, "inspect", name], capture_output=True, check=False)
    return result.returncode == 0


def _docker_list(cmd: list[str]) -> list[str]:
    """Run a Docker command and return stdout lines, or empty list on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return [line for line in result.stdout.strip().split("\n") if line]


# =============================================================================
# Network Management
# =============================================================================


def network_exists(name: str = "ai-dev-network") -> bool:
    """Check if a Docker network exists."""
    return _docker_inspect("network", name)


def delete_network(name: str) -> bool:
    """Delete a Docker network by name. Returns True on success."""
    result = subprocess.run(
        ["docker", "network", "rm", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        from ai_dev_base.core.console import warning

        stderr_msg = result.stderr.strip() if result.stderr else ""
        error_detail = stderr_msg or f"exit code {result.returncode}"
        warning(f"Failed to delete network '{name}': {error_detail}")
    return result.returncode == 0


def ensure_network(name: str = "ai-dev-network") -> bool:
    """Ensure Docker network exists, creating it if needed. Returns True on success."""
    if _docker_inspect("network", name):
        return True

    result = subprocess.run(
        ["docker", "network", "create", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        from ai_dev_base.core.console import warning

        stderr_msg = result.stderr.strip() if result.stderr else ""
        error_detail = stderr_msg or f"exit code {result.returncode}"
        warning(f"Failed to create Docker network '{name}': {error_detail}")
    return result.returncode == 0


# =============================================================================
# Compose File Management
# =============================================================================


def get_compose_files(
    docker_enabled: bool = False,
    docker_direct: bool = False,
) -> list[str]:
    """Get compose file arguments ["-f", "file.yml", ...] based on Docker options."""
    project_root = get_project_root()
    files = ["-f", str(project_root / "docker-compose.yml")]

    if docker_enabled:
        files.extend(["-f", str(project_root / "docker-compose.docker.yml")])
    elif docker_direct:
        files.extend(["-f", str(project_root / "docker-compose.docker-direct.yml")])

    return files


# =============================================================================
# Shell Mount Configuration
# =============================================================================


def get_shell_mount_args(config: AppConfig) -> list[str]:
    """Build shell config mount arguments (zshrc, OMP theme, oh-my-zsh custom).

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

    # Oh My Posh theme
    omp_theme = config.shell.omp_theme_path
    if omp_theme is None:
        # Default location from dev.sh line 80
        omp_theme = home / ".oh-my-zsh/custom/themes/.zsh-theme-remote.omp.json"

    if omp_theme.exists():
        args.extend(["-v", f"{omp_theme}:/home/dev/.zsh-theme.omp.json:ro"])

    # Oh My ZSH custom directory (plugins, themes, etc.)
    omz_custom = home / ".oh-my-zsh/custom"
    if omz_custom.is_dir():
        args.extend(["-v", f"{omz_custom}:/home/dev/.oh-my-zsh/custom:ro"])

    return args


# =============================================================================
# Compose Operations
# =============================================================================


def compose_build(*, no_cache: bool = False) -> RunResult:
    """Build the Docker image via `docker compose build`."""
    project_root = get_project_root()
    cmd = ["docker", "compose", "build"]

    if no_cache:
        cmd.append("--no-cache")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=project_root,
        check=False,
    )

    return RunResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def compose_run(
    config: AppConfig,
    options: ContainerOptions,
    *,
    command: str | None = None,
    interactive: bool = True,
    env: dict[str, str] | None = None,
    service: str = "dev",
    profile: str | None = None,
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
        profile: Compose profile to activate (e.g., "auth").
        timeout: Timeout in seconds (headless only). Returns exit code 124 on timeout.
    """
    project_root = get_project_root()

    # Build compose command
    compose_files = get_compose_files(
        docker_enabled=options.docker_enabled,
        docker_direct=options.docker_direct,
    )
    if profile:
        cmd = ["docker", "compose", *compose_files, "--profile", profile, "run", "--rm"]
    else:
        cmd = ["docker", "compose", *compose_files, "run", "--rm"]

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

    # Shell mounts
    if options.shell_mounts:
        shell_args = get_shell_mount_args(config)
        cmd.extend(shell_args)

    # Service name
    cmd.append(service)

    # Command to execute
    if command is not None:
        cmd.extend(["-c", command])

    # Execute (env vars are passed to the container via -e flags above,
    # no need to set them in the host subprocess environment)
    try:
        if interactive:
            # Interactive mode: inherit stdin/stdout/stderr
            result = subprocess.run(
                cmd,
                cwd=project_root,
                check=False,
            )
            return RunResult(
                returncode=result.returncode,
            )
        else:
            # Headless mode: capture output with optional timeout
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=project_root,
                timeout=timeout,
                check=False,
            )
            return RunResult(
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
    except subprocess.TimeoutExpired as e:
        # text=True means stdout/stderr are already str (not bytes)
        stdout = str(e.stdout) if e.stdout is not None else ""
        stderr = str(e.stderr) if e.stderr is not None else f"Timeout after {timeout}s"
        # Return code 124 is conventional for timeout (like GNU timeout command)
        return RunResult(
            returncode=124,
            stdout=stdout,
            stderr=stderr,
        )
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


def compose_up(
    services: list[str] | None = None,
    *,
    detach: bool = True,
    docker_enabled: bool = False,
    docker_direct: bool = False,
) -> RunResult:
    """Start compose services (detached by default)."""
    project_root = get_project_root()
    compose_files = get_compose_files(docker_enabled=docker_enabled, docker_direct=docker_direct)

    cmd = ["docker", "compose", *compose_files, "up"]

    if detach:
        cmd.append("-d")

    if services:
        cmd.extend(services)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=project_root,
        check=False,
    )

    return RunResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def compose_down(*, remove_volumes: bool = False) -> RunResult:
    """Stop and remove compose services."""
    project_root = get_project_root()
    compose_files = get_compose_files()

    cmd = ["docker", "compose", *compose_files, "down"]

    if remove_volumes:
        cmd.append("-v")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=project_root,
        check=False,
    )

    return RunResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def cleanup_docker_proxy(docker_enabled: bool) -> bool:
    """Stop and remove docker-proxy if it was started. No-op if docker_enabled is False."""
    if not docker_enabled:
        return True

    from ai_dev_base.core.console import warning

    project_root = get_project_root()
    compose_files = get_compose_files(docker_enabled=True)
    success = True

    # Stop the proxy
    stop_result = subprocess.run(
        ["docker", "compose", *compose_files, "stop", "docker-proxy"],
        capture_output=True,
        text=True,
        cwd=project_root,
        check=False,
    )
    if stop_result.returncode != 0:
        stderr_msg = stop_result.stderr.strip() if stop_result.stderr else ""
        if stderr_msg:
            warning(f"Failed to stop docker-proxy: {stderr_msg}")
        success = False

    # Remove the proxy container
    rm_result = subprocess.run(
        ["docker", "compose", *compose_files, "rm", "-f", "docker-proxy"],
        capture_output=True,
        text=True,
        cwd=project_root,
        check=False,
    )
    if rm_result.returncode != 0:
        stderr_msg = rm_result.stderr.strip() if rm_result.stderr else ""
        if stderr_msg:
            warning(f"Failed to remove docker-proxy: {stderr_msg}")
        success = False

    return success


# =============================================================================
# Container Status
# =============================================================================


def is_container_running(name: str) -> bool:
    """Check if a container is running by name (exact match)."""
    names = _docker_list(["docker", "ps", "--format", "{{.Names}}", "--filter", f"name=^{name}$"])
    return name in names


def get_running_containers(prefix: str = "ai-dev") -> list[str]:
    """Get list of running containers matching a name prefix."""
    return _docker_list(["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={prefix}"])


# =============================================================================
# Volume Management
# =============================================================================


def list_volumes(prefix: str = "ai-dev") -> list[str]:
    """List Docker volumes matching a name prefix."""
    return _docker_list(["docker", "volume", "ls", "-q", "--filter", f"name={prefix}"])


def volume_exists(name: str) -> bool:
    """Check if a Docker volume exists."""
    return _docker_inspect("volume", name)


def delete_volume(name: str) -> bool:
    """Delete a Docker volume by name. Returns True on success."""
    result = subprocess.run(
        ["docker", "volume", "rm", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        from ai_dev_base.core.console import warning

        stderr_msg = result.stderr.strip() if result.stderr else ""
        error_detail = stderr_msg or f"exit code {result.returncode}"
        warning(f"Failed to delete volume '{name}': {error_detail}")
    return result.returncode == 0


def delete_volumes(names: list[str]) -> dict[str, bool]:
    """Delete multiple volumes. Returns dict mapping name to success status."""
    return {name: delete_volume(name) for name in names}


def get_existing_volumes_by_category(category: str) -> list[str]:
    """Get existing volume names for a category (credentials/tools/cache/data)."""
    from ai_dev_base.config.defaults import VOLUME_CATEGORIES

    defined_volumes = VOLUME_CATEGORIES.get(category, [])
    return [vol for vol in defined_volumes if volume_exists(vol)]
