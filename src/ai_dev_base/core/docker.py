"""Docker and Docker Compose operations for AI Dev Base.

Provides wrapper functions for:
- Network management (ensure_network, network_exists)
- Compose file selection (get_compose_files)
- Shell mount configuration (get_shell_mount_args)
- Container execution (compose_run, compose_build, compose_up, compose_down)
- Container status (is_container_running, get_running_containers)
- Volume management (list_volumes, delete_volume, delete_volumes)
- Cleanup (cleanup_docker_proxy)

All functions mirror the behavior of the original dev.sh Bash script.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
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
    """Options for container execution.

    Configures how the container should be started, including
    Docker socket access, firewall settings, and volume mounts.

    Example:
        >>> options = ContainerOptions(
        ...     docker_enabled=True,
        ...     firewall_enabled=True,
        ...     mount_path=Path("/path/to/workspace"),
        ... )
        >>> options.docker_enabled
        True
    """

    docker_enabled: bool = False
    """Enable Docker socket access via proxy."""

    firewall_enabled: bool = False
    """Enable network firewall (restricts outbound traffic)."""

    mount_path: Path | None = None
    """Additional workspace mount path (maps to ~/workspace in container)."""

    shell_mounts: bool = True
    """Include shell configuration mounts (zshrc, oh-my-zsh, oh-my-posh)."""


@dataclass
class RunResult:
    """Result of a container execution.

    Captures the exit code, output streams, and the command that was executed.

    Example:
        >>> result = RunResult(returncode=0, stdout="output", stderr="")
        >>> result.success
        True
    """

    returncode: int
    """Exit code from the container process."""

    stdout: str = ""
    """Standard output from the container (if captured)."""

    stderr: str = ""
    """Standard error from the container (if captured)."""

    command: list[str] = field(default_factory=lambda: [])
    """The command that was executed."""

    @property
    def success(self) -> bool:
        """Check if the command succeeded (returncode == 0)."""
        return self.returncode == 0


# =============================================================================
# Network Management
# =============================================================================


def network_exists(name: str = "ai-dev-network") -> bool:
    """Check if a Docker network exists.

    Uses `docker network inspect` to check for network existence.

    Args:
        name: Network name to check (default: ai-dev-network).

    Returns:
        True if the network exists, False otherwise.

    Example:
        >>> if not network_exists():
        ...     ensure_network()
    """
    result = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def ensure_network(name: str = "ai-dev-network") -> bool:
    """Create Docker network if it doesn't exist.

    Mirrors the ensure_network() function from dev.sh (lines 313-318):
    ```bash
    if ! docker network inspect ai-dev-network &>/dev/null; then
        echo -e "${BLUE}Creating ai-dev-network...${NC}"
        docker network create ai-dev-network
    fi
    ```

    Args:
        name: Network name to create (default: ai-dev-network).

    Returns:
        True if network was created, False if it already existed.

    Example:
        >>> created = ensure_network()
        >>> print("Network created" if created else "Network already exists")
    """
    if network_exists(name):
        return False

    result = subprocess.run(
        ["docker", "network", "create", name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


# =============================================================================
# Compose File Management
# =============================================================================


def get_compose_files(docker_enabled: bool = False) -> list[str]:
    """Get compose file arguments based on options.

    Mirrors the get_compose_files() function from dev.sh (lines 97-105):
    ```bash
    if [[ "$docker_enabled" == "true" ]]; then
        echo "-f docker-compose.yml -f docker-compose.docker.yml"
    else
        echo "-f docker-compose.yml"
    fi
    ```

    Args:
        docker_enabled: Include docker-compose.docker.yml for socket proxy.

    Returns:
        List of compose file arguments ["-f", "file1.yml", "-f", "file2.yml"].
        Always uses absolute paths based on project root.

    Example:
        >>> args = get_compose_files(docker_enabled=True)
        >>> len([a for a in args if a.endswith(".yml")])
        2
    """
    project_root = get_project_root()
    files = ["-f", str(project_root / "docker-compose.yml")]

    if docker_enabled:
        files.extend(["-f", str(project_root / "docker-compose.docker.yml")])

    return files


# =============================================================================
# Shell Mount Configuration
# =============================================================================


def get_shell_mount_args(config: AppConfig) -> list[str]:
    """Build shell configuration mount arguments.

    Mirrors the get_shell_mount_args() function from dev.sh (lines 65-92).

    Mounts (if files exist and skip_mounts is False):
    - ~/.zshrc -> /home/dev/.zshrc.local:ro
    - OMP theme -> /home/dev/.zsh-theme.omp.json:ro
    - ~/.oh-my-zsh/custom -> /home/dev/.oh-my-zsh/custom:ro

    Args:
        config: Application configuration with shell settings.

    Returns:
        List of volume mount arguments for docker compose run.
        Empty list if config.shell.skip_mounts is True.

    Example:
        >>> from ai_dev_base.config.models import AppConfig
        >>> config = AppConfig(code_dir=Path.home() / "projects")
        >>> args = get_shell_mount_args(config)
        >>> "-v" in args if args else True  # May be empty if no files exist
        True
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
    """Build the Docker image via compose.

    Runs `docker compose build` in the project root directory.

    Args:
        no_cache: If True, build without using cache.

    Returns:
        RunResult with returncode, stdout, stderr, and command.

    Example:
        >>> result = compose_build()
        >>> if result.success:
        ...     print("Build complete")
    """
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
        command=cmd,
    )


def compose_run(
    config: AppConfig,
    options: ContainerOptions,
    *,
    command: str | None = None,
    interactive: bool = True,
    env: dict[str, str] | None = None,
    service: str = "dev",
) -> RunResult:
    """Run a container via docker compose.

    Mirrors the container run logic from dev.sh cmd_start() and cmd_run().

    The container is started with:
    - Compose files based on docker_enabled flag
    - Shell mounts based on config.shell.skip_mounts
    - Additional workspace mount if options.mount_path is set
    - Environment variables for firewall and custom env

    For headless mode (interactive=False), the container runs without TTY
    and executes the command directly.

    Args:
        config: Application configuration.
        options: Container options (docker, firewall, mounts).
        command: Shell command to execute (passed as -c "command").
            If None, starts an interactive shell.
        interactive: Enable TTY and stdin (default: True).
            Set to False for headless agent execution.
        env: Additional environment variables to pass to the container.
        service: Compose service name (default: dev).

    Returns:
        RunResult with returncode, stdout, stderr, and command.
        For interactive mode, stdout/stderr will be empty as output
        goes directly to the terminal.

    Example:
        >>> # Interactive shell
        >>> result = compose_run(config, options)

        >>> # Headless agent execution
        >>> result = compose_run(
        ...     config, options,
        ...     command='echo "Hello"',
        ...     interactive=False,
        ... )
    """
    project_root = get_project_root()

    # Build compose command
    compose_files = get_compose_files(docker_enabled=options.docker_enabled)
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

    # Prepare environment for subprocess
    subprocess_env = os.environ.copy()
    subprocess_env.update(env_vars)

    # Execute
    if interactive:
        # Interactive mode: inherit stdin/stdout/stderr
        result = subprocess.run(
            cmd,
            cwd=project_root,
            env=subprocess_env,
            check=False,
        )
        return RunResult(
            returncode=result.returncode,
            command=cmd,
        )
    else:
        # Headless mode: capture output
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_root,
            env=subprocess_env,
            check=False,
        )
        return RunResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            command=cmd,
        )


def compose_up(
    services: list[str] | None = None,
    *,
    detach: bool = True,
    docker_enabled: bool = False,
) -> RunResult:
    """Start compose services.

    Args:
        services: List of service names to start. If None, starts all services.
        detach: Run in detached mode (default: True).
        docker_enabled: Include docker-compose.docker.yml.

    Returns:
        RunResult with returncode, stdout, stderr, and command.

    Example:
        >>> result = compose_up(services=["docker-proxy"], docker_enabled=True)
    """
    project_root = get_project_root()
    compose_files = get_compose_files(docker_enabled=docker_enabled)

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
        command=cmd,
    )


def compose_down(
    *,
    remove_volumes: bool = False,
    docker_enabled: bool = False,
) -> RunResult:
    """Stop and remove compose services.

    Args:
        remove_volumes: Also remove named volumes (default: False).
        docker_enabled: Include docker-compose.docker.yml.

    Returns:
        RunResult with returncode, stdout, stderr, and command.

    Example:
        >>> result = compose_down(remove_volumes=True)
    """
    project_root = get_project_root()
    compose_files = get_compose_files(docker_enabled=docker_enabled)

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
        command=cmd,
    )


def cleanup_docker_proxy(docker_enabled: bool) -> None:
    """Stop and remove docker-proxy if it was started.

    Mirrors the cleanup logic from dev.sh (lines 421-424 and 552-555):
    ```bash
    if [[ "$docker_enabled" == "true" ]]; then
        docker compose $compose_files stop docker-proxy 2>/dev/null || true
        docker compose $compose_files rm -f docker-proxy 2>/dev/null || true
    fi
    ```

    This cleanup is important to ensure the docker-proxy container
    doesn't keep running after the main dev container exits.

    Args:
        docker_enabled: Only cleanup if True (proxy was started).

    Example:
        >>> # After container run completes
        >>> cleanup_docker_proxy(docker_enabled=True)
    """
    if not docker_enabled:
        return

    project_root = get_project_root()
    compose_files = get_compose_files(docker_enabled=True)

    # Stop the proxy
    subprocess.run(
        ["docker", "compose", *compose_files, "stop", "docker-proxy"],
        capture_output=True,
        cwd=project_root,
        check=False,
    )

    # Remove the proxy container
    subprocess.run(
        ["docker", "compose", *compose_files, "rm", "-f", "docker-proxy"],
        capture_output=True,
        cwd=project_root,
        check=False,
    )


# =============================================================================
# Container Status
# =============================================================================


def is_container_running(name: str) -> bool:
    """Check if a container is running by name.

    Uses `docker ps` with name filter to check container status.

    Args:
        name: Container name to check (exact match).

    Returns:
        True if the container is running, False otherwise.

    Example:
        >>> if is_container_running("ai-dev-docker-proxy"):
        ...     print("Docker proxy is running")
    """
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name=^{name}$"],
        capture_output=True,
        text=True,
        check=False,
    )

    # Check for exact match in output
    running_containers = result.stdout.strip().split("\n")
    return name in running_containers


def get_running_containers(prefix: str = "ai-dev") -> list[str]:
    """Get list of running containers with prefix.

    Args:
        prefix: Container name prefix to filter by (default: ai-dev).

    Returns:
        List of running container names matching the prefix.

    Example:
        >>> containers = get_running_containers()
        >>> for name in containers:
        ...     print(f"Running: {name}")
    """
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={prefix}"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        return []

    return [c for c in result.stdout.strip().split("\n") if c]


# =============================================================================
# Volume Management
# =============================================================================


def list_volumes(prefix: str = "ai-dev") -> list[str]:
    """List Docker volumes with prefix.

    Args:
        prefix: Volume name prefix to filter by (default: ai-dev).

    Returns:
        List of volume names matching the prefix.

    Example:
        >>> volumes = list_volumes()
        >>> "ai-dev-claude-config" in volumes
        True
    """
    result = subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", f"name={prefix}"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0 or not result.stdout.strip():
        return []

    return [v for v in result.stdout.strip().split("\n") if v]


def volume_exists(name: str) -> bool:
    """Check if a Docker volume exists.

    Args:
        name: Volume name to check.

    Returns:
        True if the volume exists, False otherwise.

    Example:
        >>> if volume_exists("ai-dev-claude-config"):
        ...     print("Claude config volume exists")
    """
    result = subprocess.run(
        ["docker", "volume", "inspect", name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def delete_volume(name: str) -> bool:
    """Delete a Docker volume by name.

    Args:
        name: Volume name to delete.

    Returns:
        True if deleted successfully, False if failed (e.g., volume in use).

    Example:
        >>> if delete_volume("ai-dev-uv-cache"):
        ...     print("Cache volume deleted")
        ... else:
        ...     print("Failed to delete (in use?)")
    """
    result = subprocess.run(
        ["docker", "volume", "rm", name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def delete_volumes(names: list[str]) -> dict[str, bool]:
    """Delete multiple volumes.

    Attempts to delete each volume in the list, tracking success/failure
    for each one individually.

    Args:
        names: List of volume names to delete.

    Returns:
        Dict mapping volume name to success status (True=deleted, False=failed).

    Example:
        >>> results = delete_volumes(["ai-dev-uv-cache", "ai-dev-gh-config"])
        >>> for name, success in results.items():
        ...     status = "deleted" if success else "failed"
        ...     print(f"{name}: {status}")
    """
    return {name: delete_volume(name) for name in names}


def get_existing_volumes_by_category(category: str) -> list[str]:
    """Get existing volume names for a specific category.

    This function combines the volume category definitions from
    config/defaults.py with a check for actual existence on the system.

    Mirrors the get_volumes_by_category() function from dev.sh (lines 673-685)
    which only returns volumes that actually exist.

    Args:
        category: One of 'credentials', 'tools', 'cache', 'data'.

    Returns:
        List of existing volume names for the category.
        Empty list if category is unknown or no volumes exist.

    Example:
        >>> volumes = get_existing_volumes_by_category("credentials")
        >>> for vol in volumes:
        ...     print(f"  - {vol}")
    """
    from ai_dev_base.config.defaults import get_volumes_by_category

    defined_volumes = get_volumes_by_category(category)
    return [vol for vol in defined_volumes if volume_exists(vol)]
