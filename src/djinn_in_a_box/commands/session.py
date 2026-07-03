"""CLI command for AI agent sessions."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from djinn_in_a_box.core.console import console, err_console, error
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.session import SessionManager


@handle_config_errors
def session(
    project: Annotated[
        str,
        typer.Option("--project", "-p", help="Project namespace for session isolation"),
    ] = "default",
    agent: Annotated[
        str,
        typer.Option("--agent", "-a", help="Agent to use (claude, gemini, codex, opencode)"),
    ] = "claude",
    model: Annotated[
        str,
        typer.Option("--model", "-m", help="Model override"),
    ] = "sonnet",
    prompt: Annotated[
        str | None,
        typer.Option("--prompt", help="Prompt for headless mode (omit for interactive)"),
    ] = None,
    timeout: Annotated[
        int,
        typer.Option("--timeout", "-t", help="Timeout in seconds (headless only)"),
    ] = 300,
    create: Annotated[
        bool,
        typer.Option("--create", help="Create the session workspace if it is missing"),
    ] = False,
) -> None:
    """Start an interactive or headless AI agent session.

    Sessions run in the Djinn container via docker exec. The consumer
    prepares a workspace at ~/.djinn/sessions/<project>/ with CLAUDE.md
    and context files.

    Interactive mode (default): Opens a terminal session with the agent.
    Headless mode (--prompt): Runs the agent with a prompt, captures output.

    Examples:

        # Interactive session for my-project
        djinn session --project my-project

        # Headless query
        djinn session --project my-project --prompt "Explain the codebase"

        # Different agent and model
        djinn session --project my-project --agent gemini --model gemini-2.5-flash
    """
    sessions_base = Path.home() / ".djinn" / "sessions"
    workspace = sessions_base / project

    try:
        mgr = SessionManager(project)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from None

    sessions_root = sessions_base.resolve()
    resolved_workspace = workspace.resolve()
    try:
        resolved_workspace.relative_to(sessions_root)
    except ValueError:
        error(f"Session workspace escapes sessions root: {workspace}")
        raise typer.Exit(1) from None

    if create and not workspace.exists():
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            error(f"Failed to create session workspace: {e}")
            raise typer.Exit(1) from e

    if workspace.exists() and not workspace.is_dir():
        error(f"Session workspace exists but is not a directory: {workspace}")
        err_console.print("Remove or rename it, then retry.")
        raise typer.Exit(1)

    if not workspace.is_dir():
        error(f"Session workspace not found: {workspace}")
        err_console.print(f"Create it first: mkdir -p {workspace}")
        raise typer.Exit(1)

    try:
        mgr.preflight_check()
    except RuntimeError as e:
        error(str(e))
        raise typer.Exit(1) from None

    try:
        if prompt is not None:
            # Headless mode
            result = mgr.run_headless(
                workspace_dir=workspace,
                prompt=prompt,
                agent=agent,
                model=model,
                timeout=timeout,
            )
            if result.stdout:
                console.print(result.stdout, end="")
            if result.stderr:
                err_console.print(result.stderr, end="")
        else:
            # Interactive mode
            if not sys.stdin.isatty():
                error("Interactive session requires a TTY")
                raise typer.Exit(1)
            result = mgr.run_interactive(
                workspace_dir=workspace,
                agent=agent,
                model=model,
            )
            if result.stderr:
                err_console.print(result.stderr, end="")
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from None

    raise typer.Exit(result.returncode)
