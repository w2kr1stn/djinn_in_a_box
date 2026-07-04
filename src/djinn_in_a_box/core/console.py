"""Rich console output helpers — styled terminal output.

Status messages go to stderr to keep stdout clean for agent output.
"""

from rich.console import Console
from rich.text import Text

from djinn_in_a_box.core.theme import DJINN_THEME, ICONS

console: Console = Console(theme=DJINN_THEME)
"""Main console for stdout output (tables, agent output)."""

err_console: Console = Console(stderr=True, theme=DJINN_THEME)
"""Error console for stderr output (status messages, progress)."""


def status_line(
    label: str,
    value: str,
    style: str = "status.enabled",
    *,
    value_style: str | None = None,
) -> None:
    """Print a formatted status line to stderr (e.g., '   Projects:  /path')."""
    # Calculate padding to align values (longest label is ~10 chars)
    padding = max(0, 10 - len(label))
    line = Text("   ")
    line.append(f"{label}:", style=style)
    line.append(f"{' ' * padding} ")
    line.append(value, style=value_style)
    err_console.print(line)


def error(message: str) -> None:
    err_console.print(f"[error]{ICONS['error']} Error: {message}[/error]")


def success(message: str) -> None:
    err_console.print(f"[success]{ICONS['success']} {message}[/success]")


def info(message: str) -> None:
    err_console.print(f"[info]{ICONS['info']} {message}[/info]")


def warning(message: str) -> None:
    err_console.print(f"[warning]{ICONS['warning']} Warning: {message}[/warning]")


def blank() -> None:
    err_console.print()


def header(title: str) -> None:
    err_console.print(f"[header]{title}:[/header]")


def rule(title: str = "") -> None:
    width = max(getattr(err_console, "width", 80), 20)
    err_console.print()
    if not title:
        err_console.print(Text("─" * width, style="border"))
        return

    suffix_len = max(width - len(title) - 3, 8)
    line = Text("─ ", style="border")
    line.append(title, style="primary.bold")
    line.append(f" {'─' * suffix_len}", style="border")
    err_console.print(line)
