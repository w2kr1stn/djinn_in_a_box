"""Interactive installation diagnosis."""

from enum import StrEnum
from typing import Annotated

import typer
from pydantic import ValidationError

from djinn_in_a_box.core.assistant import AssistantError, run_audit
from djinn_in_a_box.core.console import error


class AgentName(StrEnum):
    claude = "claude"
    codex = "codex"
    opencode = "opencode"


def audit(
    occasion: Annotated[str | None, typer.Argument(help="Symptom or error to investigate.")] = None,
    agent: Annotated[AgentName | None, typer.Option("--agent", help="Assistant agent.")] = None,
) -> None:
    """Diagnose and repair the installation with an interactive assistant."""
    try:
        status = run_audit(occasion, agent.value if agent else None)
    except (AssistantError, OSError, ValidationError) as exc:
        error(str(exc))
        raise typer.Exit(1) from None
    if status:
        raise typer.Exit(status)
