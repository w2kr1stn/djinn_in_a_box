"""Configuration commands — init, show, path, set, and edit."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig, ResourceLimits, ShellConfig
from djinn_in_a_box.core.console import console, error, info, success, warning
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.docker import ensure_host_env
from djinn_in_a_box.core.exceptions import ConfigNotFoundError, ConfigValidationError
from djinn_in_a_box.core.hostinfo import detect_timezone, suggest_resources
from djinn_in_a_box.core.paths import CONFIG_DIR, CONFIG_FILE, get_project_root
from djinn_in_a_box.core.seeding import SeedingError, seed_config

ALLOWED_CONFIG_KEYS: tuple[str, ...] = (
    "general.code_dir",
    "general.timezone",
    "general.config_root",
    "resources.cpu_limit",
    "resources.memory_limit",
    "resources.cpu_reservation",
    "resources.memory_reservation",
    "shell.skip_mounts",
    "shell.omp_theme_path",
)


def init_config(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Overwrite existing configuration without prompting.",
        ),
    ] = False,
) -> None:
    """Initialize configuration in ~/.config/djinn_in_a_box/.

    Creates config.toml with user-provided settings through interactive prompts.
    Run this once before using other commands.

    [info.bold]Example:[/info.bold]

        djinn init              # Interactive setup

        djinn init --force      # Overwrite existing config
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        error(f"Failed to create configuration directory {CONFIG_DIR}: {e}")
        warning(f"Check that {CONFIG_DIR.parent} is writable, then retry.")
        raise typer.Exit(1) from e

    # Check for existing config
    if CONFIG_FILE.exists() and not force:
        warning(f"Configuration already exists: {CONFIG_FILE}")
        if not typer.confirm("Overwrite existing configuration?"):
            raise typer.Abort()

    # Interactive prompts
    info("Djinn in a Box Configuration Setup")
    console.print()

    code_dir = typer.prompt(
        "Projects directory (mounted as ~/projects in container)",
        default=str(Path.home() / "projects"),
    )

    timezone = typer.prompt(
        "Timezone (for container)",
        default=detect_timezone(),
    )

    suggested_resources = suggest_resources()
    if typer.confirm(
        "Configure advanced options (resources, shell mounts)?",
        default=False,
    ):
        try:
            resources = ResourceLimits(
                cpu_limit=typer.prompt(
                    "CPU limit",
                    default=suggested_resources.cpu_limit,
                    type=int,
                ),
                memory_limit=typer.prompt(
                    "Memory limit",
                    default=suggested_resources.memory_limit,
                ),
                cpu_reservation=typer.prompt(
                    "CPU reservation",
                    default=suggested_resources.cpu_reservation,
                    type=int,
                ),
                memory_reservation=typer.prompt(
                    "Memory reservation",
                    default=suggested_resources.memory_reservation,
                ),
            )
        except ValidationError as e:
            for err in e.errors():
                error(str(err.get("msg", e)))
            raise typer.Exit(1) from e
        shell = ShellConfig(
            skip_mounts=typer.confirm(
                "Skip host shell config mounts?",
                default=False,
            )
        )
    else:
        resources = suggested_resources
        shell = ShellConfig()

    # Validate code_dir exists or offer to create it
    code_path = Path(code_dir).expanduser()
    if not code_path.exists():
        if typer.confirm(f"Directory {code_path} does not exist. Create it?"):
            try:
                code_path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                error(f"Failed to create projects directory {code_path}: {e}")
                warning(f"Check that {code_path.parent} is writable, then retry.")
                raise typer.Exit(1) from e
            success(f"Created directory: {code_path}")
        else:
            error("Cannot proceed without a valid projects directory.")
            raise typer.Exit(1)

    # Create configuration — surface validation errors (e.g. a non-IANA timezone)
    # as a clean message instead of a raw pydantic traceback.
    try:
        config = AppConfig(
            code_dir=code_path,
            timezone=timezone,
            resources=resources,
            shell=shell,
        )
    except ValidationError as e:
        for err in e.errors():
            error(str(err.get("msg", e)))
        raise typer.Exit(1) from e

    # Save configuration
    try:
        save_config(config)
    except OSError as e:
        error(f"Failed to write configuration to {CONFIG_FILE}: {e}")
        warning(f"Check that {CONFIG_FILE.parent} is writable, then retry.")
        raise typer.Exit(1) from e
    success(f"Configuration written to {CONFIG_FILE}")

    # Provision host bind-mount sources so the first compose run finds them
    # (config-root credential subdirs, ~/.djinn/{sessions,backups}, ~/.ssh, ~/.gitconfig).
    try:
        ensure_host_env(config)
    except OSError as e:
        error(f"Failed to provision host directories: {e}")
        warning("Check that your home and config-root paths are writable, then retry.")
        raise typer.Exit(1) from e
    success(f"Host directories provisioned under {config.config_root}")

    # Outside the try: a missing repo marker is an install problem, not a
    # writability problem — its own FileNotFoundError message must surface as-is.
    project_root = get_project_root()
    try:
        created = seed_config(project_root)
    except SeedingError as e:
        error(str(e))
        warning("Follow the remedy above, then retry.")
        raise typer.Exit(1) from e
    except PermissionError as e:
        error(f"Failed to seed default configuration: {e}")
        warning(
            f'Fix ownership with `sudo chown -R "$(id -u):$(id -g)" '
            f"{project_root / 'config'}`, then retry."
        )
        raise typer.Exit(1) from e
    except OSError as e:
        error(f"Failed to seed default configuration: {e}")
        warning("Check that the project config paths are writable, then retry.")
        raise typer.Exit(1) from e
    if created:
        success(f"Seeded {len(created)} default config file(s)")

    console.print()
    console.print("[primary.bold]Next steps:[/primary.bold]")
    console.print("  [muted]1.[/muted] djinn build    [muted]# Build the Docker image[/muted]")
    console.print("  [muted]2.[/muted] djinn auth     [muted]# Authenticate with AI[/muted]")
    console.print("  [muted]3.[/muted] djinn start    [muted]# Start development shell[/muted]")
    console.print(
        "  [muted]4.[/muted] (optional) mcpgateway start   "
        "[muted]# MCP tools — not required[/muted]"
    )


def _allowed_keys_message() -> str:
    return ", ".join(ALLOWED_CONFIG_KEYS)


def _parse_int_config_value(key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as e:
        error(f"Invalid value for {key}: {value!r}. Expected an integer.")
        raise typer.Exit(1) from e


def _parse_bool_config_value(key: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    error(f"Invalid value for {key}: {value!r}. Expected true or false.")
    raise typer.Exit(1)


def _build_config(
    config: AppConfig,
    *,
    code_dir: Path | None = None,
    timezone: str | None = None,
    config_root: Path | None = None,
    resources: ResourceLimits | None = None,
    shell: ShellConfig | None = None,
) -> AppConfig:
    return AppConfig(
        code_dir=config.code_dir if code_dir is None else code_dir,
        timezone=config.timezone if timezone is None else timezone,
        config_root=config.config_root if config_root is None else config_root,
        resources=config.resources if resources is None else resources,
        shell=config.shell if shell is None else shell,
    )


def _set_config_value(config: AppConfig, key: str, value: str) -> AppConfig:
    if key == "general.code_dir":
        code_dir = Path(value).expanduser()
        if not code_dir.is_dir():
            error(f"Projects directory does not exist or is not a directory: {code_dir}")
            warning(f"Create it first: mkdir -p {code_dir}")
            warning("Or run `djinn init` to create it interactively.")
            raise typer.Exit(1)
        return _build_config(config, code_dir=code_dir)
    if key == "general.timezone":
        return _build_config(config, timezone=value)
    if key == "general.config_root":
        config_root = Path(value).expanduser()
        if config_root != config.config_root:
            warning(
                f"Existing credentials/config remain at {config.config_root}; "
                f"new empty directories will be provisioned at {config_root}."
            )
        return _build_config(config, config_root=config_root)
    if key == "resources.cpu_limit":
        resources = ResourceLimits(
            cpu_limit=_parse_int_config_value(key, value),
            memory_limit=config.resources.memory_limit,
            cpu_reservation=config.resources.cpu_reservation,
            memory_reservation=config.resources.memory_reservation,
        )
        return _build_config(config, resources=resources)
    if key == "resources.memory_limit":
        resources = ResourceLimits(
            cpu_limit=config.resources.cpu_limit,
            memory_limit=value,
            cpu_reservation=config.resources.cpu_reservation,
            memory_reservation=config.resources.memory_reservation,
        )
        return _build_config(config, resources=resources)
    if key == "resources.cpu_reservation":
        resources = ResourceLimits(
            cpu_limit=config.resources.cpu_limit,
            memory_limit=config.resources.memory_limit,
            cpu_reservation=_parse_int_config_value(key, value),
            memory_reservation=config.resources.memory_reservation,
        )
        return _build_config(config, resources=resources)
    if key == "resources.memory_reservation":
        resources = ResourceLimits(
            cpu_limit=config.resources.cpu_limit,
            memory_limit=config.resources.memory_limit,
            cpu_reservation=config.resources.cpu_reservation,
            memory_reservation=value,
        )
        return _build_config(config, resources=resources)
    if key == "shell.skip_mounts":
        shell = ShellConfig(
            skip_mounts=_parse_bool_config_value(key, value),
            omp_theme_path=config.shell.omp_theme_path,
        )
        return _build_config(config, shell=shell)
    if key == "shell.omp_theme_path":
        normalized = value.strip().lower()
        shell = ShellConfig(
            skip_mounts=config.shell.skip_mounts,
            omp_theme_path=None
            if normalized in {"", "none", "null"}
            else Path(value).expanduser(),
        )
        return _build_config(config, shell=shell)

    error(f"Unknown configuration key: {key}")
    error(f"Allowed keys: {_allowed_keys_message()}")
    raise typer.Exit(1)


def _format_config_value(config: AppConfig, key: str) -> str:
    if key == "general.code_dir":
        return str(config.code_dir)
    if key == "general.timezone":
        return config.timezone
    if key == "general.config_root":
        return str(config.config_root)
    if key == "resources.cpu_limit":
        return str(config.resources.cpu_limit)
    if key == "resources.memory_limit":
        return config.resources.memory_limit
    if key == "resources.cpu_reservation":
        return str(config.resources.cpu_reservation)
    if key == "resources.memory_reservation":
        return config.resources.memory_reservation
    if key == "shell.skip_mounts":
        return str(config.shell.skip_mounts)
    if key == "shell.omp_theme_path":
        return str(config.shell.omp_theme_path)

    msg = f"Unknown configuration key: {key}"
    raise ValueError(msg)


@handle_config_errors
def config_set(
    key: Annotated[str, typer.Argument(help="Configuration key to update.")],
    value: Annotated[str, typer.Argument(help="New value.")],
) -> None:
    """Set one supported configuration value."""
    config = load_config()

    try:
        updated = _set_config_value(config, key, value)
    except ValidationError as e:
        for err in e.errors():
            error(str(err.get("msg", e)))
        raise typer.Exit(1) from e

    try:
        save_config(updated)
    except OSError as e:
        error(f"Failed to write configuration to {CONFIG_FILE}: {e}")
        warning(f"Check that {CONFIG_FILE.parent} is writable, then retry.")
        raise typer.Exit(1) from e
    success(f"{key} = {_format_config_value(updated, key)}")


def config_edit() -> None:
    """Open the configuration file in $EDITOR and validate it afterward."""
    editor = os.environ.get("EDITOR", "vi")
    try:
        command = shlex.split(editor)
    except ValueError as e:
        error(f"Cannot run editor {editor!r} ($EDITOR): {e}")
        raise typer.Exit(1) from e
    if not command:
        command = ["vi"]
    try:
        completed = subprocess.run([*command, str(CONFIG_FILE)], check=False)  # noqa: S603
    except (FileNotFoundError, PermissionError) as e:
        error(f"Cannot run editor {editor!r} ($EDITOR): {e}")
        raise typer.Exit(1) from e
    if completed.returncode != 0:
        error(f"Editor exited with status {completed.returncode}: {editor}")
        raise typer.Exit(1)

    try:
        load_config()
    except (ConfigNotFoundError, ConfigValidationError) as e:
        warning(f"Configuration problem after edit: {type(e).__name__}: {e}")


@handle_config_errors
def config_show(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            "-j",
            help="Output configuration as JSON.",
        ),
    ] = False,
) -> None:
    """Show current configuration.

    Displays all settings from ~/.config/djinn_in_a_box/config.toml.

    [info.bold]Example:[/info.bold]

        djinn config show           # Human-readable output

        djinn config show --json    # JSON output for scripting
    """
    config = load_config()

    if json_output:
        # Output as JSON (mode="json" ensures Path objects are serialized as strings)
        output = json.dumps(config.model_dump(mode="json"), indent=2)
        console.print(output, highlight=False)
    else:
        # Human-readable output
        info("Current Configuration")
        console.print(f"  [muted]Config file:[/muted] {CONFIG_FILE}")
        console.print()

        console.print("[primary.bold]General[/primary.bold]")
        console.print(f"  code_dir:  {config.code_dir}")
        console.print(f"  timezone:  {config.timezone}")
        console.print()

        console.print("[primary.bold]Resources[/primary.bold]")
        console.print(f"  cpu_limit:          {config.resources.cpu_limit}")
        console.print(f"  memory_limit:       {config.resources.memory_limit}")
        console.print(f"  cpu_reservation:    {config.resources.cpu_reservation}")
        console.print(f"  memory_reservation: {config.resources.memory_reservation}")
        console.print()

        console.print("[primary.bold]Shell[/primary.bold]")
        console.print(f"  skip_mounts: {config.shell.skip_mounts}")
        if config.shell.omp_theme_path:
            console.print(f"  omp_theme_path: {config.shell.omp_theme_path}")


def config_path() -> None:
    """Show configuration file path.

    Outputs the path to the main configuration file.
    Useful for scripting and manual editing.

    [info.bold]Example:[/info.bold]

        djinn config path           # Show path

        vim $(djinn config path)    # Edit config directly
    """
    # Raw path for scripting — no Rich path-highlighting (breaks $(...) consumers
    # when color output is forced).
    console.print(str(CONFIG_FILE), highlight=False)
