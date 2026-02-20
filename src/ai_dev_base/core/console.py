"""Rich console output helpers — styled terminal output via TodAI theme.

Status messages go to stderr to keep stdout clean for agent output.
"""

from rich.console import Console
from rich.table import Table

from ai_dev_base.core.theme import ICONS, TODAI_THEME

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


def print_volume_table(volumes: dict[str, list[str]]) -> None:
    """Print a formatted volume table to stdout."""
    table = Table(
        title="AI Dev Volumes",
        title_style="table.title",
        show_header=True,
        header_style="table.header",
    )

    table.add_column("Category", style="table.category", width=15)
    table.add_column("Volume", style="table.value")

    entries = [(cat, vols) for cat, vols in volumes.items() if vols]
    for i, (category, volume_list) in enumerate(entries):
        table.add_row(category.title(), volume_list[0])
        for vol in volume_list[1:]:
            table.add_row("", vol)
        if i < len(entries) - 1:
            table.add_row("", "")

    console.print(table)


def header(title: str) -> None:
    """Print a section header to stderr."""
    err_console.print(f"[header]{title}:[/header]")
