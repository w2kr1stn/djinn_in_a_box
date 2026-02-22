"""Container lifecycle commands — build, start, auth, status, clean, and more."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ai_dev_base.config import (
    VOLUME_CATEGORIES,
    ConfigNotFoundError,
    load_config,
)
from ai_dev_base.core.console import (
    blank,
    console,
    err_console,
    error,
    header,
    info,
    status_line,
    success,
    warning,
)
from ai_dev_base.core.decorators import handle_config_errors
from ai_dev_base.core.docker import (
    AI_DEV_NETWORK,
    ContainerOptions,
    check_docker_flags,
    cleanup_docker_proxy,
    compose_build,
    compose_down,
    compose_run,
    compose_up,
    delete_network,
    delete_volume,
    delete_volumes,
    ensure_network,
    get_running_containers,
    get_shell_mount_args,
    is_container_running,
    network_exists,
    volume_exists,
)
from ai_dev_base.core.paths import get_project_root, resolve_mount_path


def _get_existing_volumes_by_category(category: str) -> list[str]:
    """Get existing volume names for a category (credentials/tools/cache/data)."""
    defined_volumes = VOLUME_CATEGORIES.get(category, [])
    return [vol for vol in defined_volumes if volume_exists(vol)]


def build(
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Build without using cache"),
    ] = False,
) -> None:
    """Build/rebuild the Docker image.

    Must be done before first use and after Dockerfile changes.
    """
    info("Building ai-dev-base image...")

    result = compose_build(no_cache=no_cache)

    if result.success:
        success("Done! Run 'codeagent start' to begin.")
    else:
        error(f"Build failed with exit code {result.returncode}")
        if result.stderr:
            err_console.print(result.stderr)
        raise typer.Exit(result.returncode)


@handle_config_errors
def start(
    docker: Annotated[
        bool,
        typer.Option("--docker", "-d", help="Enable Docker access via secure proxy"),
    ] = False,
    docker_direct: Annotated[
        bool,
        typer.Option("--docker-direct", help="Enable direct Docker socket access (no proxy)"),
    ] = False,
    firewall: Annotated[
        bool,
        typer.Option("--firewall", "-f", help="Enable network firewall (restricts outbound)"),
    ] = False,
    here: Annotated[
        bool,
        typer.Option("--here", help="Mount current directory as ~/workspace"),
    ] = False,
    mount: Annotated[
        Path | None,
        typer.Option("--mount", "-m", help="Mount specified path as ~/workspace"),
    ] = None,
) -> None:
    """Start interactive development shell.

    Launches the AI development container with an interactive shell.
    The container has access to the configured projects directory
    and optionally Docker socket access and firewall restrictions.

    Examples:
        codeagent start                         # Basic interactive shell
        codeagent start --docker                # With Docker access (proxy)
        codeagent start --docker-direct         # With Docker access (direct)
        codeagent start --here                  # Mount cwd as workspace
        codeagent start -d -f --here            # Full options
    """
    check_docker_flags(docker, docker_direct)

    config = load_config()

    if not ensure_network():
        error(f"Failed to create Docker network '{AI_DEV_NETWORK}'")
        raise typer.Exit(1)

    # Resolve mount path
    mount_path: Path | None = None
    if here:
        mount_path = Path.cwd()
    elif mount:
        try:
            mount_path = resolve_mount_path(mount)
        except (FileNotFoundError, NotADirectoryError) as e:
            error(str(e))
            raise typer.Exit(1) from None

    # Print status output (to stderr, matching Bash format)
    blank()
    info("Starting AI Dev environment...")
    blank()

    status_line("Projects", str(config.code_dir))

    if docker:
        status_line("Docker", "Enabled (via secure proxy)", "status.enabled")
    elif docker_direct:
        status_line("Docker", "Enabled (DIRECT — no proxy)", "warning")
    else:
        status_line("Docker", "Disabled (use --docker to enable)", "status.disabled")

    if firewall:
        status_line("Firewall", "Enabled (outbound restricted)", "status.enabled")
    else:
        status_line("Firewall", "Disabled (use --firewall to enable)", "status.disabled")

    if mount_path:
        status_line("Workspace", str(mount_path), "status.enabled")

    # Shell mount status
    shell_args = get_shell_mount_args(config)
    if config.shell.skip_mounts:
        status_line("Shell", "Using container defaults (skip_mounts=true)", "status.disabled")
    elif shell_args:
        status_line("Shell", "Host config mounted", "status.enabled")
    else:
        status_line("Shell", "No host config found", "status.disabled")

    # Security warning for direct mode
    if docker_direct:
        blank()
        warning(
            "Direct Docker socket access grants full Docker control. "
            "This is equivalent to root access on the host. "
            "Use --docker (proxy) for safer operation."
        )

    blank()

    # Run container
    options = ContainerOptions(
        docker_enabled=docker,
        docker_direct=docker_direct,
        firewall_enabled=firewall,
        mount_path=mount_path,
    )

    result = compose_run(config, options, interactive=True)

    # Cleanup docker proxy if it was started (not needed for direct mode)
    cleanup_docker_proxy(docker)

    raise typer.Exit(result.returncode)


@handle_config_errors
def auth(
    docker: Annotated[
        bool,
        typer.Option("--docker", "-d", help="Enable Docker access via proxy"),
    ] = False,
    docker_direct: Annotated[
        bool,
        typer.Option("--docker-direct", help="Enable direct Docker socket access (no proxy)"),
    ] = False,
) -> None:
    """Start with host network for OAuth authentication.

    This mode uses network_mode: host so OAuth callbacks from CLI tools
    (Claude Code, Gemini CLI, Codex) can reach localhost.

    After authenticating all tools, exit and use 'codeagent start' for
    normal development with the isolated network.

    Example:
        codeagent auth                   # Authenticate CLI tools
        codeagent auth --docker          # With Docker access (proxy)
        codeagent auth --docker-direct   # With Docker access (direct)
    """
    check_docker_flags(docker, docker_direct)

    config = load_config()

    info("Starting AI Dev with host network for OAuth authentication...")
    blank()
    err_console.print("This mode uses network_mode: host so OAuth callbacks work.")
    err_console.print(
        "After authenticating Claude Code, Gemini CLI and Codex, exit and use 'codeagent start'"
    )
    blank()

    # Start docker proxy separately if docker proxy mode is enabled
    # In host network mode, the proxy needs to be started as a separate service
    if docker:
        err_console.print(
            "[warning]Docker proxy starting separately for host network mode...[/warning]"
        )
        proxy_result = compose_up(services=["docker-proxy"], docker_enabled=True)
        if not proxy_result.success:
            error("Failed to start Docker proxy for host network mode")
            raise typer.Exit(proxy_result.returncode)
        time.sleep(2)

    if docker_direct:
        warning(
            "Direct Docker socket access grants full Docker control. "
            "Use --docker (proxy) for safer operation."
        )

    # Run auth container via compose_run
    options = ContainerOptions(docker_enabled=docker, docker_direct=docker_direct)
    result = compose_run(config, options, service="dev-auth", profile="auth", interactive=True)

    # Cleanup docker proxy if it was started (not needed for direct mode)
    cleanup_docker_proxy(docker)

    raise typer.Exit(result.returncode)


def _print_volume_table(volumes: dict[str, list[str]]) -> None:
    """Print a formatted volume table to stdout."""
    table = Table(
        title="AI Dev Volumes",
        title_style="table.title",
        show_header=True,
        header_style="table.header",
    )

    table.add_column("Category", style="table.category", width=15)
    table.add_column("Volume", style="table.value")

    entries = [(cat, vols) for cat, vols in volumes.items() if vols]
    for i, (category, volume_list) in enumerate(entries):
        table.add_row(category.title(), volume_list[0])
        for vol in volume_list[1:]:
            table.add_row("", vol)
        if i < len(entries) - 1:
            table.add_row("", "")

    console.print(table)


def status() -> None:
    """Show container, volume, network, and service status."""
    # Check Docker availability
    docker_check = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        check=False,
    )
    if docker_check.returncode != 0:
        error("Docker is not available. Is the Docker daemon running?")
        raise typer.Exit(1)

    # Configuration
    header("Configuration")
    try:
        config = load_config()
        err_console.print(f"  CODE_DIR: {config.code_dir}")
    except ConfigNotFoundError:
        warning("Configuration not found. Run 'codeagent init' to create one.")

    blank()

    # Containers
    header("Containers")
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=ai-dev",
            "--filter",
            "name=mcp-",
            "--format",
            "table {{.Names}}\t{{.Status}}\t{{.Image}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout.strip():
        console.print(result.stdout.strip())
    else:
        err_console.print("  No containers found")

    blank()

    # Volumes
    header("Volumes")
    categorized = {
        cat: vols for cat in VOLUME_CATEGORIES if (vols := _get_existing_volumes_by_category(cat))
    }
    if categorized:
        _print_volume_table(categorized)
    else:
        err_console.print("  No volumes found")

    blank()

    # Networks
    header("Networks")
    result = subprocess.run(
        [
            "docker",
            "network",
            "ls",
            "--filter",
            "name=ai-dev",
            "--format",
            "table {{.Name}}\t{{.Driver}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout.strip():
        console.print(result.stdout.strip())
    else:
        err_console.print("  No networks found")

    blank()

    # Service Status
    header("Services")

    # Docker Proxy Status
    if is_container_running("ai-dev-docker-proxy"):
        success("  Docker Proxy: Running")
    else:
        err_console.print("  [status.disabled]Docker Proxy: Not running[/status.disabled]")

    # MCP Gateway Status
    if is_container_running("mcp-gateway"):
        success("  MCP Gateway: Running")
    else:
        err_console.print("  [status.disabled]MCP Gateway: Not running[/status.disabled]")


clean_app = typer.Typer(
    name="clean",
    help="Manage containers and volumes",
    no_args_is_help=False,
)


@clean_app.callback(invoke_without_command=True)
def clean_default(ctx: typer.Context) -> None:
    """Remove containers only (default action when no subcommand given).

    Runs `docker compose down` to stop and remove containers.
    Volumes and networks are preserved.

    """
    if ctx.invoked_subcommand is None:
        info("Stopping and removing containers...")
        result = compose_down()
        if result.success:
            success("Containers removed.")
        else:
            error(f"Failed to remove containers (exit code: {result.returncode})")
            if result.stderr:
                err_console.print(result.stderr)
            raise typer.Exit(result.returncode)


@clean_app.command("volumes")
def clean_volumes(
    credentials: Annotated[
        bool,
        typer.Option("--credentials", help="Delete credential volumes (claude, gemini, etc.)"),
    ] = False,
    tools: Annotated[
        bool,
        typer.Option("--tools", help="Delete tool config volumes (azure, pulumi, etc.)"),
    ] = False,
    cache: Annotated[
        bool,
        typer.Option("--cache", help="Delete cache volumes (uv-cache)"),
    ] = False,
    data: Annotated[
        bool,
        typer.Option("--data", help="Delete data volumes (opencode-data)"),
    ] = False,
    name: Annotated[
        str | None,
        typer.Argument(help="Specific volume name to delete"),
    ] = None,
) -> None:
    """Manage Docker volumes by category.

    Without options, lists all volumes grouped by category.
    With --credentials/--tools/--cache/--data, deletes volumes in that category.
    With a volume name argument, deletes that specific volume.

    Examples:
        codeagent clean volumes                    # List all volumes
        codeagent clean volumes --credentials      # Delete credential volumes
        codeagent clean volumes ai-dev-uv-cache    # Delete specific volume
    """
    # If a specific volume name is provided, delete it
    if name:
        if not name.startswith("ai-dev-"):
            error(f"Refusing to delete volume '{name}': only ai-dev-* volumes are managed")
            raise typer.Exit(1)
        if volume_exists(name):
            info(f"Deleting volume: {name}")
            if delete_volume(name):
                success(f"Volume '{name}' deleted.")
            else:
                error(f"Failed to delete volume '{name}' (may be in use)")
                raise typer.Exit(1)
        else:
            error(f"Volume '{name}' does not exist")
            raise typer.Exit(1)
        return

    # Check if any category flag is set
    categories_to_delete: list[str] = []
    if credentials:
        categories_to_delete.append("credentials")
    if tools:
        categories_to_delete.append("tools")
    if cache:
        categories_to_delete.append("cache")
    if data:
        categories_to_delete.append("data")

    # If no flags set, list volumes
    if not categories_to_delete:
        info("Volumes by category:")
        blank()
        categorized = {
            cat: vols
            for cat in VOLUME_CATEGORIES
            if (vols := _get_existing_volumes_by_category(cat))
        }
        if categorized:
            _print_volume_table(categorized)
        else:
            err_console.print("No volumes found.")
        blank()
        err_console.print("Use --credentials, --tools, --cache, or --data to delete.")
        return

    # Delete volumes in selected categories
    for category in categories_to_delete:
        volumes = _get_existing_volumes_by_category(category)
        if not volumes:
            warning(f"No existing volumes in category '{category}'")
            continue

        info(f"Deleting {category} volumes...")
        results = delete_volumes(volumes)

        for vol, deleted in results.items():
            if deleted:
                success(f"  Deleted: {vol}")
            else:
                error(f"  Failed: {vol} (may be in use)")


@clean_app.command("all")
def clean_all(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Remove EVERYTHING: containers, volumes, and network.

    This is a destructive operation that removes:
    - All ai-dev containers
    - All ai-dev volumes (credentials, tools, cache, data)
    - The ai-dev-network

    """
    if not force:
        confirm = typer.confirm(
            "This will delete ALL containers, volumes, and the network. Continue?",
            default=False,
        )
        if not confirm:
            info("Aborted.")
            raise typer.Exit(0)

    # Stop and remove containers
    info("Stopping and removing containers...")
    compose_down()

    # Delete all volumes
    info("Deleting all volumes...")
    all_volumes = [v for vols in VOLUME_CATEGORIES.values() for v in vols]
    results = delete_volumes(all_volumes)
    for vol, deleted in results.items():
        if deleted:
            success(f"  Deleted: {vol}")
        else:
            warning(f"  Failed: {vol}")

    # Remove network
    info("Removing network...")
    if network_exists(AI_DEV_NETWORK):
        if delete_network(AI_DEV_NETWORK):
            success(f"  Deleted: {AI_DEV_NETWORK}")
    else:
        err_console.print(f"  {AI_DEV_NETWORK} does not exist")

    blank()
    success("Cleanup complete.")


def audit(
    tail: Annotated[
        int,
        typer.Option("--tail", "-n", help="Number of log lines to show"),
    ] = 50,
) -> None:
    """Show Docker proxy audit log."""
    if not is_container_running("ai-dev-docker-proxy"):
        error("Docker Proxy is not running.")
        err_console.print("Start with: codeagent start --docker")
        raise typer.Exit(1)

    info(f"Docker Proxy Audit Log (last {tail} lines):")
    blank()

    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), "ai-dev-docker-proxy"],
        check=False,
    )
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


def update() -> None:
    """Update CLI agent versions in Dockerfile."""
    info("Updating CLI agent versions...")
    blank()

    project_root = get_project_root()
    script_path = project_root / "scripts" / "update-agents.sh"

    if not script_path.exists():
        error(f"Update script not found: {script_path}")
        raise typer.Exit(1)

    result = subprocess.run(
        [str(script_path)],
        cwd=project_root,
        check=False,
    )

    if result.returncode != 0:
        error(f"Update failed with exit code {result.returncode}")
        raise typer.Exit(result.returncode)

    success("Update completed successfully")


def enter() -> None:
    """Open a new shell in a running container."""
    if not sys.stdin.isatty():
        error("Cannot enter container: no TTY available (stdin is not a terminal)")
        raise typer.Exit(1)

    containers = get_running_containers("ai-dev-base-dev")
    if not containers:
        error("No running ai-dev-base container found.")
        err_console.print("Start one with: codeagent start")
        raise typer.Exit(1)

    container = containers[0]
    info(f"Opening new Zsh session in: {container}")
    blank()

    result = subprocess.run(
        ["docker", "exec", "-it", container, "zsh"],
        check=False,
    )
    raise typer.Exit(result.returncode)
