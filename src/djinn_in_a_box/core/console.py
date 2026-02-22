"""Rich console output helpers — styled terminal output via TodAI theme.

Status messages go to stderr to keep stdout clean for agent output.
"""

from rich.console import Console

from djinn_in_a_box.core.theme import ICONS, TODAI_THEME

console: Console = Console(theme=TODAI_THEME)
"""Main console for stdout output (tables, agent output)."""

err_console: Console = Console(stderr=True, theme=TODAI_THEME)
"""Error console for stderr output (status messages, progress)."""


def status_line(label: str, value: str, style: str = "status.enabled") -> None:
    """Print a formatted status line to stderr (e.g., '   Projects:  /path')."""
    # Calculate padding to align values (longest label is ~10 chars)
    padding = max(0, 10 - len(label))
    err_console.print(f"   [{style}]{label}:[/{style}]{' ' * padding} {value}")


def error(message: str) -> None:
    """Print error message to stderr."""
    err_console.print(f"[error]{ICONS['error']} Error: {message}[/error]")


def success(message: str) -> None:
    """Print success message to stderr."""
    err_console.print(f"[success]{ICONS['success']} {message}[/success]")


def info(message: str) -> None:
    """Print info message to stderr."""
    err_console.print(f"[info]{ICONS['info']} {message}[/info]")


def warning(message: str) -> None:
    """Print warning message to stderr."""
    err_console.print(f"[warning]{ICONS['warning']} Warning: {message}[/warning]")


def blank() -> None:
    """Print a blank line to stderr."""
    err_console.print()


def header(title: str) -> None:
    """Print a section header to stderr."""
    err_console.print(f"[header]{title}:[/header]")
