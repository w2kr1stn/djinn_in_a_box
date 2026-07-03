"""Rich console output helpers — styled terminal output.

Status messages go to stderr to keep stdout clean for agent output.
"""

from rich.console import Console

from djinn_in_a_box.core.theme import DJINN_THEME, ICONS

console: Console = Console(theme=DJINN_THEME)
"""Main console for stdout output (tables, agent output)."""

err_console: Console = Console(stderr=True, theme=DJINN_THEME)
"""Error console for stderr output (status messages, progress)."""


def status_line(label: str, value: str, style: str = "status.enabled") -> None:
    """Print a formatted status line to stderr (e.g., '   Projects:  /path')."""
    # Calculate padding to align values (longest label is ~10 chars)
    padding = max(0, 10 - len(label))
    err_console.print(f"   [{style}]{label}:[/{style}]{' ' * padding} {value}")


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
