"""Container lifecycle commands — build, start, status, clean, and more."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from djinn_in_a_box.commands.doctor import preflight
from djinn_in_a_box.config.defaults import SYNC_PATHS, VOLUME_CATEGORIES
from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.banner import banner
from djinn_in_a_box.core.config_workflow import (
    WorkflowDeliveryTarget,
    prepare_config_workflow,
)
from djinn_in_a_box.core.console import (
    blank,
    console,
    err_console,
    error,
    info,
    rule,
    status_line,
    success,
    warning,
)
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.docker import (
    DJINN_NETWORK,
    ContainerOptions,
    MountCollisionError,
    cleanup_docker_proxy,
    clear_sync_path,
    compose_build,
    compose_down,
    compose_run,
    delete_network,
    delete_volume,
    delete_volumes,
    ensure_network,
    get_audio_mount_args,
    get_config_root,
    get_dbus_mount_args,
    get_existing_sync_paths_by_category,
    get_existing_volumes_by_category,
    get_running_containers,
    get_shell_mount_args,
    is_container_running,
    network_exists,
    resolve_container_mounts,
    resolve_docker_mode,
    volume_exists,
)
from djinn_in_a_box.core.exceptions import (
    ConfigNotFoundError,
    ConfigValidationError,
    MountSpecificationError,
    RuntimeMountSpecificationError,
)
from djinn_in_a_box.core.paths import get_project_root


def _sync_build_files(config: AppConfig | None = None) -> None:
    """Copy build-time files from sync dir into the repo (both are gitignored).

    Docker COPY cannot follow symlinks outside the build context, so we keep
    real files in the repo and refresh them from the sync dir before each build.
    """
    import shutil

    repo_dotfiles = get_config_root(config) / "repo-dotfiles"
    if not repo_dotfiles.is_dir():
        return

    project_root = get_project_root()
    mappings: list[tuple[str, Path]] = [
        ("packages.txt", project_root / "packages.txt"),
        ("tools.txt", project_root / "tools" / "tools.txt"),
    ]
    for name, target in mappings:
        source = repo_dotfiles / name
        if source.is_file():
            shutil.copy2(source, target)


@handle_config_errors
def build(
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Build without using cache"),
    ] = False,
) -> None:
    """Build/rebuild the Docker image.

    Must be done before first use and after Dockerfile changes.
    """
    config = load_config()
    preflight(config)
    _sync_build_files(config)
    info("Building djinn-in-a-box image...")

    result = compose_build(config, no_cache=no_cache)

    if result.success:
        blank()
        success("Done! Run 'djinn start' to begin.")
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
        list[str] | None,
        typer.Option(
            "--mount",
            "-m",
            help="Host directory to mount; repeatable: SRC[:DST[:ro|rw]]",
        ),
    ] = None,
) -> None:
    """Start interactive development shell.

    Launches the AI development container with an interactive shell.
    The container has access to the configured projects directory
    and optionally Docker socket access and firewall restrictions.

    Examples:
        djinn start                         # Basic interactive shell
        djinn start --docker                # With Docker access (proxy)
        djinn start --docker-direct         # With Docker access (direct)
        djinn start --here                  # Mount cwd as workspace
        djinn start -d -f --here            # Full options
    """
    try:
        docker_mode = resolve_docker_mode(docker, docker_direct)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from None

    config = load_config()
    preflight(config, provision_host=False)

    config_root = get_config_root(config)
    workflow = prepare_config_workflow(
        get_project_root(),
        (
            WorkflowDeliveryTarget("claude", config_root / "claude"),
            WorkflowDeliveryTarget("codex", config_root / "codex"),
        ),
        config_snapshot=config,
        require_compose_host_env=True,
    )
    if not workflow.success:
        problem = workflow.problems[0]
        error(problem.message)
        warning(problem.remedy)
        raise typer.Exit(1)

    if not ensure_network():
        error(f"Failed to create Docker network '{DJINN_NETWORK}'")
        raise typer.Exit(1)

    try:
        mounts = resolve_container_mounts(tuple(mount or ()), here=here)
    except (MountSpecificationError, FileNotFoundError, NotADirectoryError) as e:
        error(str(e))
        raise typer.Exit(1) from None

    # Print status output (to stderr, matching Bash format)
    banner()
    rule("Environment")

    status_line("Projects", str(config.code_dir), value_style="path")

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

    for resolved_mount in mounts:
        mode = "ro" if resolved_mount.read_only else "rw"
        status_line(
            "Mount",
            f"{resolved_mount.source} -> {resolved_mount.target} ({mode})",
            value_style="path",
        )

    # Shell mount status
    shell_args = get_shell_mount_args(config)
    if config.shell.skip_mounts:
        status_line("Shell", "Using container defaults (skip_mounts=true)", "status.disabled")
    elif shell_args:
        status_line("Shell", "Host config mounted", "status.enabled")
    else:
        status_line("Shell", "No host config found", "status.disabled")

    # Audio passthrough status
    audio_args = get_audio_mount_args()
    dbus_args = get_dbus_mount_args()
    if audio_args:
        status_line("Audio", "PulseAudio forwarding enabled", "status.enabled")
    else:
        status_line("Audio", "No audio device detected", "status.disabled")

    # Security warning for direct mode
    if docker_direct:
        blank()
        warning(
            "Direct Docker socket access grants full Docker control. "
            "This is equivalent to root access on the host. "
            "Use --docker (proxy) for safer operation."
        )

    rule("Container")

    # Run container
    options = ContainerOptions(
        docker_mode=docker_mode,
        firewall_enabled=firewall,
        mounts=mounts,
    )

    try:
        result = compose_run(
            config,
            options,
            interactive=True,
            shell_mount_args=shell_args,
            audio_mount_args=audio_args,
            dbus_mount_args=dbus_args,
        )
    except (MountCollisionError, MountSpecificationError) as e:
        error(str(e))
        raise typer.Exit(1) from None
    except RuntimeMountSpecificationError as e:
        error(f"Internal runtime mount construction failed: {e}")
        raise typer.Exit(1) from None
    finally:
        cleanup_docker_proxy(docker_mode, config)

    if result.stderr:
        err_console.print(result.stderr, end="")

    raise typer.Exit(result.returncode)


def _print_resource_table(title: str, value_header: str, entries: dict[str, list[str]]) -> None:
    table = Table(
        title=title,
        title_style="table.title",
        show_header=True,
        header_style="table.header",
        border_style="border",
    )

    table.add_column("Category", style="table.category", width=15)
    table.add_column(value_header, style="table.value")

    filtered = [(cat, items) for cat, items in entries.items() if items]
    for i, (category, items) in enumerate(filtered):
        table.add_row(category.title(), items[0])
        for item in items[1:]:
            table.add_row("", item)
        if i < len(filtered) - 1:
            table.add_row("", "")

    console.print(table)


def _print_docker_table(title: str, columns: list[str], output: str) -> bool:
    rows = [line.split("\t") for line in output.strip().splitlines() if line.strip()]
    if not rows:
        return False

    table = Table(
        title=title,
        title_style="table.title",
        show_header=True,
        header_style="table.header",
        border_style="border",
    )
    for index, column in enumerate(columns):
        style = "table.category" if index == 0 else "table.value"
        table.add_column(column, style=style)
    for row in rows:
        table.add_row(*row)
    console.print(table)
    return True


def _list_existing_volumes() -> dict[str, list[str]]:
    return {
        cat: vols for cat in VOLUME_CATEGORIES if (vols := get_existing_volumes_by_category(cat))
    }


def _list_existing_sync_paths(config: AppConfig | None = None) -> dict[str, list[str]]:
    return {
        cat: [str(p) for p in paths]
        for cat in SYNC_PATHS
        if (paths := get_existing_sync_paths_by_category(cat, config))
    }


def _load_optional_config() -> AppConfig | None:
    try:
        return load_config()
    except ConfigNotFoundError:
        return None
    except ConfigValidationError as e:
        # Destructive callers (clean volumes/all) must not guess a root from
        # a broken config — abort instead of falling back to the default.
        error(str(e))
        raise typer.Exit(1) from e


def status() -> None:
    """Show container, volume, network, and service status."""
    config: AppConfig | None = None

    # Check Docker availability
    try:
        docker_check = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        error("Docker is not installed or not on PATH.")
        raise typer.Exit(1) from None
    if docker_check.returncode != 0:
        error("Docker is not available. Is the Docker daemon running?")
        raise typer.Exit(1)

    # Configuration
    rule("Configuration")
    try:
        config = load_config()
        status_line("CODE_DIR", str(config.code_dir), value_style="path")
    except ConfigNotFoundError:
        warning("Configuration not found. Run 'djinn init' to create one.")

    blank()

    # Containers
    rule("Containers")
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=djinn",
            "--filter",
            "name=mcp-",
            "--format",
            "{{.Names}}\t{{.Status}}\t{{.Image}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if not _print_docker_table("Djinn Containers", ["Names", "Status", "Image"], result.stdout):
        err_console.print("  No containers found")

    blank()

    # Volumes
    rule("Volumes")
    volume_entries = _list_existing_volumes()
    if volume_entries:
        _print_resource_table("Djinn Volumes", "Volume", volume_entries)
    else:
        err_console.print("  No volumes found")

    blank()

    # Synced Paths
    rule("Synced Paths")
    status_line("Root", str(get_config_root(config)), value_style="path")
    sync_entries = _list_existing_sync_paths(config)
    if sync_entries:
        _print_resource_table("Djinn Sync Paths", "Path", sync_entries)
    else:
        err_console.print("  No sync paths found")

    blank()

    # Networks
    rule("Networks")
    result = subprocess.run(
        [
            "docker",
            "network",
            "ls",
            "--filter",
            "name=djinn",
            "--format",
            "{{.Name}}\t{{.Driver}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if not _print_docker_table("Djinn Networks", ["Name", "Driver"], result.stdout):
        err_console.print("  No networks found")

    blank()

    # Service Status
    rule("Services")

    # Docker Proxy Status
    if is_container_running("djinn-docker-proxy"):
        status_line("Docker Proxy", "Running", "status.enabled")
    else:
        status_line("Docker Proxy", "Not running", "status.disabled")

    # MCP Gateway Status
    if is_container_running("mcp-gateway"):
        status_line("MCP Gateway", "Running", "status.enabled")
    else:
        status_line("MCP Gateway", "Not running", "status.disabled")


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
        # Teardown intentionally uses best-effort env (config=None): `docker compose
        # down` acts by project label, so it must work even when config.toml is
        # absent or broken. clean_all keeps compose_down config-free too; only
        # sync-path cleanup loads optional config to resolve the correct root.
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
        typer.Option(
            "--credentials",
            help="Clear credential sync paths (claude, gemini, codex, opencode, gh, age)",
        ),
    ] = False,
    repo_dotfiles: Annotated[
        bool,
        typer.Option("--repo-dotfiles", help="Clear repo-dotfiles sync path"),
    ] = False,
    cache: Annotated[
        bool,
        typer.Option("--cache", help="Delete cache volumes (uv-cache, tools-cache, vscode-server)"),
    ] = False,
    data: Annotated[
        bool,
        typer.Option("--data", help="Delete data volumes (opencode-data, vscode-workspaces)"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation for sync path clearing"),
    ] = False,
    name: Annotated[
        str | None,
        typer.Argument(help="Specific volume name to delete"),
    ] = None,
) -> None:
    """Manage Docker volumes (delete) and sync paths (clear contents).

    Without options, lists volumes and sync paths grouped by category.
    Sync-path categories (credentials, repo-dotfiles) clear contents of
    bind-mount directories under $DJINN_CONFIG_ROOT — if you mirror this directory
    across machines, the deletion will propagate.
    Volume categories (cache, data) delete Docker named volumes locally.
    With a volume name argument, deletes that specific named volume.

    Examples:
        djinn clean volumes                    # List volumes and sync paths
        djinn clean volumes --credentials      # Clear credential sync paths
        djinn clean volumes --cache            # Delete cache volumes
        djinn clean volumes djinn-uv-cache     # Delete specific volume
    """
    if name:
        if not name.startswith("djinn-"):
            error(f"Refusing to delete volume '{name}': only djinn-* volumes are managed")
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

    selected: list[str] = []
    if credentials:
        selected.append("credentials")
    if repo_dotfiles:
        selected.append("repo-dotfiles")
    if cache:
        selected.append("cache")
    if data:
        selected.append("data")

    if not selected:
        config = _load_optional_config()
        rule("Volumes by category")
        blank()
        volume_entries = _list_existing_volumes()
        if volume_entries:
            _print_resource_table("Djinn Volumes", "Volume", volume_entries)
        else:
            err_console.print("  No volumes found")
        rule("Sync paths by category")
        blank()
        sync_entries = _list_existing_sync_paths(config)
        if sync_entries:
            _print_resource_table("Djinn Sync Paths", "Path", sync_entries)
        else:
            err_console.print("  No sync paths found")
        blank()
        err_console.print(
            "Use --credentials, --repo-dotfiles, --cache, or --data to clean.",
        )
        return

    config = _load_optional_config()
    sync_selected = [c for c in selected if c in SYNC_PATHS]
    if sync_selected and not force:
        warning(
            "Clearing sync paths deletes their contents; if you mirror them across "
            "machines the deletion will propagate.",
        )
        typer.confirm(
            f"Really clear {', '.join(sync_selected)} sync path contents?",
            abort=True,
        )

    for category in selected:
        if category in VOLUME_CATEGORIES:
            volumes = get_existing_volumes_by_category(category)
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
        else:
            paths = get_existing_sync_paths_by_category(category, config)
            if not paths:
                warning(f"No existing sync paths in category '{category}'")
                continue
            info(f"Clearing {category} sync paths...")
            for path in paths:
                if clear_sync_path(path):
                    success(f"  Cleared: {path}")
                else:
                    error(f"  Failed: {path}")


@clean_app.command("all")
def clean_all(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Remove EVERYTHING: containers, volumes, sync path contents, and network.

    This is a destructive operation that removes:
    - All djinn containers
    - All djinn named volumes (cache, data)
    - All contents of sync paths under $DJINN_CONFIG_ROOT — if you mirror them
      across machines the deletion will propagate.
    - The djinn-network

    """
    if not force:
        confirm = typer.confirm(
            "This will delete ALL containers, volumes, sync path contents, and the "
            "network. If you mirror sync paths across machines the deletion will "
            "propagate. Continue?",
            default=False,
        )
        if not confirm:
            info("Aborted.")
            raise typer.Exit(0)

    config = _load_optional_config()

    info("Stopping and removing containers...")
    down_result = compose_down()
    if not down_result.success:
        warning(f"Failed to stop containers: {down_result.stderr.strip() or 'unknown error'}")
        warning("Proceeding with cleanup despite container stop failure")

    info("Deleting all volumes...")
    all_volumes = [v for vols in VOLUME_CATEGORIES.values() for v in vols]
    results = delete_volumes(all_volumes)
    for vol, deleted in results.items():
        if deleted:
            success(f"  Deleted: {vol}")
        else:
            warning(f"  Failed: {vol}")

    info("Clearing all sync paths...")
    for category in SYNC_PATHS:
        for path in get_existing_sync_paths_by_category(category, config):
            if clear_sync_path(path):
                success(f"  Cleared: {path}")
            else:
                warning(f"  Failed: {path}")

    info("Removing network...")
    if network_exists(DJINN_NETWORK):
        if delete_network(DJINN_NETWORK):
            success(f"  Deleted: {DJINN_NETWORK}")
    else:
        err_console.print(f"  {DJINN_NETWORK} does not exist")

    blank()
    success("Cleanup complete.")


def audit(
    tail: Annotated[
        int,
        typer.Option("--tail", "-n", help="Number of log lines to show"),
    ] = 50,
) -> None:
    """Show Docker proxy audit log."""
    if not is_container_running("djinn-docker-proxy"):
        error("Docker Proxy is not running.")
        err_console.print("Start with: djinn start --docker")
        raise typer.Exit(1)

    rule(f"Docker Proxy Audit Log (last {tail} lines):")
    blank()

    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), "djinn-docker-proxy"],
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

    blank()
    success("Update completed successfully")


def enter() -> None:
    """Open a new shell in a running container."""
    if not sys.stdin.isatty():
        error("Cannot enter container: no TTY available (stdin is not a terminal)")
        raise typer.Exit(1)

    containers = get_running_containers("djinn")
    if not containers:
        error("No running Djinn container found.")
        err_console.print("Start one with: djinn start")
        raise typer.Exit(1)

    container = containers[0]
    info(f"Opening new Zsh session in: {container}")
    blank()

    result = subprocess.run(
        ["docker", "exec", "-it", container, "zsh"],
        check=False,
    )
    raise typer.Exit(result.returncode)
