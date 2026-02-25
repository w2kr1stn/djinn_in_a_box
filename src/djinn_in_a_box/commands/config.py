"""Configuration commands — init, show, and path."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

import typer

from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.console import console, error, info, success, warning
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.paths import AGENTS_FILE, CONFIG_DIR, CONFIG_FILE, get_project_root


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
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

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
        default="Europe/Berlin",
    )

    # Validate code_dir exists or offer to create it
    code_path = Path(code_dir).expanduser()
    if not code_path.exists():
        if typer.confirm(f"Directory {code_path} does not exist. Create it?"):
            code_path.mkdir(parents=True, exist_ok=True)
            success(f"Created directory: {code_path}")
        else:
            error("Cannot proceed without a valid projects directory.")
            raise typer.Exit(1)

    # Create configuration
    config = AppConfig(code_dir=code_path, timezone=timezone)

    # Save configuration
    save_config(config)
    success(f"Configuration written to {CONFIG_FILE}")

    # Copy bundled agents.toml if user config does not exist
    if not AGENTS_FILE.exists():
        try:
            bundled = get_project_root() / "config" / "agents.toml"
        except FileNotFoundError:
            bundled = None

        if bundled and bundled.exists():
            shutil.copy(bundled, AGENTS_FILE)
            success(f"Agent definitions copied to {AGENTS_FILE}")
        else:
            warning("Bundled agents.toml not found. Using built-in defaults.")

    console.print()
    console.print("[primary.bold]Next steps:[/primary.bold]")
    console.print("  [muted]1.[/muted] djinn build    [muted]# Build the Docker image[/muted]")
    console.print("  [muted]2.[/muted] djinn auth     [muted]# Authenticate with AI[/muted]")
    console.print("  [muted]3.[/muted] djinn start    [muted]# Start development shell[/muted]")


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
        console.print(output)
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
    console.print(str(CONFIG_FILE))
