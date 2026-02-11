"""Main CLI entry point for codeagent.

Provides commands for managing AI development containers and running
CLI coding agents (Claude, Gemini, Codex, OpenCode).

Usage:
    codeagent --version         Show version and exit
    codeagent --help            Show help message
    codeagent --install-completion  Install shell completion

    # First-time setup
    codeagent init              Initialize configuration interactively
    codeagent config show       Show current configuration
    codeagent config path       Show configuration file path

    # Container lifecycle
    codeagent build             Build the Docker image
    codeagent start             Start the development container
    codeagent auth              Authenticate with AI services
    codeagent status            Show container status
    codeagent audit             Show container logs
    codeagent update            Update the Docker image
    codeagent enter             Enter the container shell

    # Agent execution
    codeagent run <agent> <prompt>  Run an agent with a prompt
    codeagent agents            List available agents

    # Cleanup
    codeagent clean volumes     Remove Docker volumes
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

import typer

from ai_dev_base import __version__
from ai_dev_base.commands.agent import agents, run
from ai_dev_base.commands.container import (
    audit,
    auth,
    build,
    clean_app,
    enter,
    start,
    status,
    update,
)
from ai_dev_base.config import (
    ensure_config_dir,
    get_bundled_agents_path,
    load_config,
    save_config,
)
from ai_dev_base.config.models import AppConfig, ResourceLimits, ShellConfig
from ai_dev_base.core.console import console, error, info, success, warning
from ai_dev_base.core.decorators import handle_config_errors
from ai_dev_base.core.paths import AGENTS_FILE, CONFIG_FILE

# =============================================================================
# Main Application
# =============================================================================

app = typer.Typer(
    name="codeagent",
    help="AI Dev Base CLI - Manage AI development containers",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


# =============================================================================
# Version Callback
# =============================================================================


def _version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        typer.echo(f"codeagent {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """AI Dev Base CLI - Manage AI development containers.

    The AI Dev Base provides a containerized development environment
    with pre-configured CLI coding agents (Claude, Gemini, Codex, OpenCode).

    [info.bold]Quick start:[/info.bold]

        codeagent init            # First-time setup

        codeagent build           # Build the Docker image

        codeagent start           # Start development shell

        codeagent run claude "Hello world"  # Run an agent
    """


# =============================================================================
# Init Command
# =============================================================================


@app.command("init")
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
    """Initialize configuration in ~/.config/ai-dev-base/.

    Creates config.toml with user-provided settings through interactive prompts.
    Run this once before using other commands.

    [info.bold]Example:[/info.bold]

        codeagent init              # Interactive setup

        codeagent init --force      # Overwrite existing config
    """
    ensure_config_dir()

    # Check for existing config
    if CONFIG_FILE.exists() and not force:
        warning(f"Configuration already exists: {CONFIG_FILE}")
        if not typer.confirm("Overwrite existing configuration?"):
            raise typer.Abort()

    # Interactive prompts
    info("AI Dev Base Configuration Setup")
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
    config = AppConfig(
        code_dir=code_path,
        timezone=timezone,
        resources=ResourceLimits(),
        shell=ShellConfig(),
    )

    # Save configuration
    save_config(config)
    success(f"Configuration written to {CONFIG_FILE}")

    # Copy bundled agents.toml if user config does not exist
    if not AGENTS_FILE.exists():
        bundled_path = get_bundled_agents_path()
        if bundled_path is not None and bundled_path.exists():
            shutil.copy(bundled_path, AGENTS_FILE)
            success(f"Agent definitions copied to {AGENTS_FILE}")
        else:
            warning("Bundled agents.toml not found. Using built-in defaults.")

    console.print()
    console.print("[primary.bold]Next steps:[/primary.bold]")
    console.print("  [muted]1.[/muted] codeagent build    [muted]# Build the Docker image[/muted]")
    console.print("  [muted]2.[/muted] codeagent auth     [muted]# Authenticate with AI[/muted]")
    console.print("  [muted]3.[/muted] codeagent start    [muted]# Start development shell[/muted]")


# =============================================================================
# Config Subcommand Group
# =============================================================================

config_app = typer.Typer(
    help="Manage configuration files.",
    no_args_is_help=True,
)


@config_app.command("show")
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

    Displays all settings from ~/.config/ai-dev-base/config.toml.

    [info.bold]Example:[/info.bold]

        codeagent config show           # Human-readable output

        codeagent config show --json    # JSON output for scripting
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


@config_app.command("path")
def config_path() -> None:
    """Show configuration file path.

    Outputs the path to the main configuration file.
    Useful for scripting and manual editing.

    [info.bold]Example:[/info.bold]

        codeagent config path           # Show path

        vim $(codeagent config path)    # Edit config directly
    """
    console.print(str(CONFIG_FILE))


# Register config subcommand group
app.add_typer(config_app, name="config")

# =============================================================================
# Container Lifecycle Commands
# =============================================================================

app.command()(build)
app.command()(start)
app.command()(auth)
app.command()(status)
app.command()(audit)
app.command()(update)
app.command()(enter)

# =============================================================================
# Clean Subcommand Group
# =============================================================================

app.add_typer(clean_app, name="clean")

# =============================================================================
# Agent Execution Commands
# =============================================================================

app.command()(run)
app.command()(agents)


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    app()
