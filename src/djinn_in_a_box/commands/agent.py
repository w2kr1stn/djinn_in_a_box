"""Agent execution commands — run agents in headless mode within Docker containers."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, ParamSpec, TypeVar, cast

import typer
from rich.markup import escape
from rich.table import Table

from djinn_in_a_box.config.loader import load_agents, load_config
from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core.agent_runner import (
    UnknownAgentError,
    run_headless_agent,
)
from djinn_in_a_box.core.agent_runner import build_agent_command as build_agent_command
from djinn_in_a_box.core.config_workflow import (
    WorkflowDeliveryTarget,
    prepare_config_workflow,
    print_workflow_notices,
)
from djinn_in_a_box.core.console import (
    console,
    err_console,
    error,
    escape_nonprintable,
    info,
    print_captured,
    rule,
    status_line,
    warning,
)
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.docker import (
    ContainerMount,
    DockerMode,
    MountCollisionError,
    RunRefusedError,
    get_config_root,
    resolve_container_mounts,
    resolve_docker_mode,
    validate_managed_bind_sources,
    workflow_image_compatible,
)
from djinn_in_a_box.core.egress import EgressPlan, plan_egress
from djinn_in_a_box.core.exceptions import (
    EgressSpecificationError,
    MountSpecificationError,
    RuntimeMountSpecificationError,
)
from djinn_in_a_box.core.paths import get_project_root


def _agent_table(title: str) -> Table:
    return Table(
        title=title,
        title_style="table.title",
        header_style="table.header",
        border_style="border",
    )


def _show_run_status(
    agent: str,
    mounts: tuple[ContainerMount, ...],
    *,
    write: bool,
    json_output: bool,
    model: str | None = None,
    docker_mode: DockerMode,
    firewall: bool,
    timeout: int | None,
    egress: EgressPlan | None = None,
) -> None:
    """Render CLI-only execution status after runner prerequisites pass."""

    # Allowlist callers read Djinn's leading `djinn-egress:` block from stderr, so no
    # caller-derived value may start a line of its own there.
    def shown(value: object) -> str:
        return escape_nonprintable(str(value)) if egress is not None else str(value)

    err_console.print()
    info(f"Running {escape(shown(agent)) if egress is not None else agent} (headless)...")
    err_console.print()

    status_line("Agent", shown(agent))
    for mount in mounts:
        mode = "ro" if mount.read_only else "rw"
        status_line(
            "Mount",
            shown(f"{mount.source} -> {mount.target} ({mode})"),
            value_style="path",
        )

    workspace = next(
        (mount.source for mount in mounts if mount.target == Path("/home/dev/workspace")),
        None,
    )
    if workspace is not None:
        status_line("Workspace", shown(workspace), value_style="path")

    if model:
        status_line("Model", shown(model))

    if write:
        status_line("Mode", "Read/Write (--write)", "status.disabled")
    else:
        status_line("Mode", "Read-only (plan/analysis)", "status.enabled")

    if docker_mode is DockerMode.DIRECT:
        status_line("Docker", "Enabled (DIRECT)", "warning")
    if egress is not None:
        status_line("Firewall", f"Allowlist ({len(egress.rules)} destinations)", soft_wrap=True)
        for entry, rules in egress.entries:
            status_line(
                "Egress", f"{entry} -> {', '.join(rule.describe() for rule in rules)}",
                soft_wrap=True,
            )
    elif firewall:
        status_line("Firewall", "Enabled")
    if json_output:
        status_line("Output", "JSON")
    if timeout:
        status_line("Timeout", f"{timeout}s")

    err_console.print()


P = ParamSpec("P")
R = TypeVar("R")


def _unwrapped_stderr_with_allow(func: Callable[P, R]) -> Callable[P, R]:
    """Print stderr without hard wrapping while --allow is in effect.

    Allowlist callers read Djinn's leading `djinn-egress:` block from stderr. A wrapped
    host line could start with that prefix through a long caller value; without hard
    wrapping only a line break can, and caller values are printed with those escaped.
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if kwargs.get("allow") is None:
            return func(*args, **kwargs)
        previous = err_console.soft_wrap
        err_console.soft_wrap = True
        try:
            return func(*args, **kwargs)
        finally:
            err_console.soft_wrap = previous

    return wrapper


@handle_config_errors
@_unwrapped_stderr_with_allow
def run(
    agent: Annotated[
        str,
        typer.Argument(help="Agent to run: claude, codex, opencode, claude-isolated"),
    ],
    prompt: Annotated[
        str,
        typer.Argument(help="Prompt to send to the agent"),
    ],
    write: Annotated[
        bool,
        typer.Option("--write", "-w", help="Allow file modifications"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="JSON output format"),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Model override (e.g., sonnet)"),
    ] = None,
    docker: Annotated[
        bool,
        typer.Option(
            "--docker", "-d",
            help="Refused for runs; the rootless agent Docker daemon belongs to the dev",
        ),
    ] = False,
    docker_direct: Annotated[
        bool,
        typer.Option("--docker-direct", help="Enable host Docker daemon access"),
    ] = False,
    firewall: Annotated[
        bool,
        typer.Option("--firewall", "-f", help="Enable network firewall"),
    ] = False,
    allow: Annotated[
        list[str] | None,
        typer.Option(
            "--allow",
            help=escape("Allow outbound HOST[/PREFIX][:PORT]; repeatable, "
                        "replaces the default firewall list"),
        ),
    ] = None,
    here: Annotated[
        bool,
        typer.Option("--here", help="Mount current directory as ~/workspace"),
    ] = False,
    mount: Annotated[
        list[str] | None,
        typer.Option(
            "--mount",
            help="Host directory to mount; repeatable: SRC[:DST[:ro|rw]]",
        ),
    ] = None,
    timeout: Annotated[
        int | None,
        typer.Option("--timeout", "-t", help="Timeout in seconds"),
    ] = None,
) -> None:
    """Run an agent in headless mode (non-interactive).

    The agent runs in an ephemeral Docker container, processes the prompt,
    outputs the result to stdout, and exits. Status information is written
    to stderr to keep stdout clean for agent output.

    By default, the current working directory is mounted as ~/workspace
    in the container (implicit --here behavior). Use --here to request that
    workspace explicitly alongside repeatable --mount values.

    Examples:

        # Simple read-only query
        djinn run claude "Explain this code"

        # Allow file modifications
        djinn run claude "Fix the bug in main.py" --write

        # Use a specific model with JSON output
        djinn run claude "Refactor this file" --write --model sonnet --json

        # With a timeout
        djinn run claude "Review the build setup" --timeout 300

        # Restrict outbound traffic to a pinned provider endpoint
        djinn run claude "Explain this code" --allow api.anthropic.com:443

        # Mount the current directory and two additional directories
        djinn run claude "Compare these projects" --here \\
            --mount ~/other-project --mount ~/reference:/home/dev/reference:ro
    """
    try:
        docker_mode = resolve_docker_mode(docker, docker_direct)
    except ValueError as e:
        error(str(e), soft_wrap=allow is not None)
        raise typer.Exit(1) from None

    egress = None
    if allow is not None:
        if docker_mode is not DockerMode.NONE:
            error("--allow cannot be combined with --docker or --docker-direct", soft_wrap=True)
            raise typer.Exit(1)
        try:
            egress = plan_egress(allow)
        except EgressSpecificationError as e:
            error(escape(str(e)), soft_wrap=True)
            raise typer.Exit(1) from None

    if docker_mode is DockerMode.AGENT:
        error(
            "djinn run cannot use --docker: the rootless agent Docker daemon belongs to the "
            "dev runtime. Use --docker-direct (host Docker authority) or start a dev with "
            "`djinn start --docker` and work there."
        )
        raise typer.Exit(1)

    checked_config = None
    delivery_targets: tuple[WorkflowDeliveryTarget, ...] = ()
    config = load_config()
    try:
        validate_managed_bind_sources(config)
    except MountSpecificationError as e:
        message = escape_nonprintable(str(e)) if egress is not None else str(e)
        error(escape(message), soft_wrap=egress is not None)
        raise typer.Exit(1) from None
    checked_config = config
    if agent in {"claude", "codex"}:
        selected_agent = cast(ConfigSyncSource, agent)
        delivery_targets = (
            WorkflowDeliveryTarget(selected_agent, get_config_root(config) / selected_agent),
        )
    workflow = prepare_config_workflow(
        get_project_root(),
        delivery_targets,
        config_snapshot=config,
        require_compose_host_env=True,
        container_image_compatibility=workflow_image_compatible(),
    )
    print_workflow_notices(workflow)
    if not workflow.success:
        problem = workflow.problems[0]
        message = escape_nonprintable(problem.message) if egress is not None else problem.message
        remedy = escape_nonprintable(problem.remedy) if egress is not None else problem.remedy
        error(escape(message), soft_wrap=egress is not None)
        warning(escape(remedy))
        raise typer.Exit(1)

    try:
        resolved_mounts = resolve_container_mounts(
            tuple(mount or ()), here=here or not mount
        )
    except (MountSpecificationError, FileNotFoundError, NotADirectoryError) as e:
        message = escape_nonprintable(str(e)) if egress is not None else str(e)
        error(escape(message), soft_wrap=egress is not None)
        raise typer.Exit(1) from None

    try:
        result = run_headless_agent(
            agent,
            prompt,
            write=write,
            json_output=json_output,
            model=model,
            docker_mode=docker_mode,
            firewall=firewall,
            egress=egress,
            resolved_mounts=resolved_mounts,
            timeout=timeout,
            app_config=checked_config,
            on_ready=lambda mounts: _show_run_status(
                agent,
                mounts,
                write=write,
                json_output=json_output,
                model=model,
                docker_mode=docker_mode,
                firewall=firewall,
                egress=egress,
                timeout=timeout,
            ),
        )
    except UnknownAgentError as e:
        if egress is not None:
            message = f"{e}; available agents: {', '.join(e.available)}"
            error(escape(escape_nonprintable(message)), soft_wrap=True)
        else:
            error(str(e))
            console.print(f"Available agents: {', '.join(e.available)}")
        raise typer.Exit(1) from None
    except RunRefusedError as e:
        message = escape_nonprintable(str(e)) if egress is not None else str(e)
        error(escape(message), soft_wrap=egress is not None)
        raise typer.Exit(1) from None
    except RuntimeMountSpecificationError as e:
        message = f"Internal runtime mount construction failed: {e}"
        if egress is not None:
            message = escape_nonprintable(message)
        error(escape(message), soft_wrap=egress is not None)
        raise typer.Exit(1) from None
    except (MountCollisionError, MountSpecificationError, EgressSpecificationError) as e:
        message = str(e)
        if egress is not None:
            message = escape_nonprintable(message)
        error(escape(message), soft_wrap=egress is not None)
        raise typer.Exit(1) from None

    if result.stdout:
        print_captured(result.stdout, err=False)

    if result.stderr:
        print_captured(result.stderr)

    raise typer.Exit(result.returncode)


@handle_config_errors
def agents(
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show detailed configuration"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """List available agents.

    Shows all configured agents with their descriptions. Use --verbose
    for detailed configuration information or --json for machine-readable output.

    Examples:

        # Simple list
        djinn agents

        # Detailed view
        djinn agents --verbose

        # JSON output for scripting
        djinn agents --json
    """
    agent_configs = load_agents()

    if json_output:
        data = {name: cfg.model_dump() for name, cfg in agent_configs.items()}
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        return

    if verbose:
        for name, cfg in sorted(agent_configs.items()):
            table = _agent_table(name)
            table.add_column("Setting", style="table.category")
            table.add_column("Value", style="table.value")
            table.add_row("Description", cfg.description or cfg.binary)
            table.add_row("Binary", cfg.binary)
            table.add_row("Model flag", cfg.model_flag)
            if cfg.default_model:
                table.add_row("Default model", cfg.default_model)
            if cfg.headless_flags:
                table.add_row("Headless", " ".join(cfg.headless_flags))
            if cfg.write_flags:
                table.add_row("Write mode", " ".join(cfg.write_flags))
            if cfg.read_only_flags:
                table.add_row("Read-only", " ".join(cfg.read_only_flags))
            if cfg.json_flags:
                table.add_row("JSON flags", " ".join(cfg.json_flags))
            console.print(table)
            console.print()
    else:
        rule("Available Agents")
        console.print()
        table = _agent_table("Available Agents")
        table.add_column("Agent", style="table.category")
        table.add_column("Description", style="table.value")
        for name, cfg in sorted(agent_configs.items()):
            desc = cfg.description or cfg.binary
            table.add_row(name, desc)
        console.print(table)
