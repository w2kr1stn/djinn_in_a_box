"""CLI command for AI agent sessions."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, cast

import typer

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core.config_workflow import (
    WorkflowDeliveryTarget,
    prepare_config_workflow,
)
from djinn_in_a_box.core.console import console, err_console, error, warning
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.docker import WorkflowImageCompatibility, get_config_root
from djinn_in_a_box.core.paths import get_project_root
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
        str | None,
        typer.Option("--model", "-m", help="Model override"),
    ] = None,
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
        target = mgr.resolve_target()
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from None

    container_image_compatibility: WorkflowImageCompatibility | None = None
    if target.container_mode:
        container_image_compatibility = mgr.workflow_image_compatible(target)
        if container_image_compatibility is WorkflowImageCompatibility.UNKNOWN:
            error("Docker daemon/container not reachable.")
            warning("Retry.")
            raise typer.Exit(1)
        if container_image_compatibility is WorkflowImageCompatibility.INCOMPATIBLE:
            error("Workflow image is incompatible.")
            warning("Rebuild/recreate required.")
            raise typer.Exit(1)

    if agent in {"claude", "codex", "opencode"}:
        config = load_config()
        delivery_targets: tuple[WorkflowDeliveryTarget, ...] = ()
        selected_agent = cast(ConfigSyncSource, agent)
        if target.container_mode:
            if agent in {"claude", "codex"}:
                delivery_targets = (
                    WorkflowDeliveryTarget(
                        selected_agent, get_config_root(config) / selected_agent
                    ),
                )
        else:
            host_roots = {
                "claude": Path.home() / ".claude",
                "codex": Path.home() / ".codex",
                "opencode": Path.home() / ".config" / "opencode",
            }
            delivery_targets = (
                WorkflowDeliveryTarget(selected_agent, host_roots[selected_agent], provision=True),
            )
        workflow = prepare_config_workflow(
            get_project_root(),
            delivery_targets,
            config_snapshot=config,
            require_compose_host_env=target.container_mode,
            container_image_compatibility=container_image_compatibility,
        )
        if not workflow.success:
            problem = workflow.problems[0]
            error(problem.message)
            warning(problem.remedy)
            raise typer.Exit(1)
        if target.container_mode and agent == "opencode":
            refreshed = mgr.refresh_opencode_workflow(target)
            if not refreshed.success:
                error(refreshed.stderr or "OpenCode workflow refresh failed")
                raise typer.Exit(1)

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
        mgr.preflight_check(agent=agent, target=target)
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
                target=target,
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
                target=target,
            )
            if result.stderr:
                err_console.print(result.stderr, end="")
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from None

    raise typer.Exit(result.returncode)
