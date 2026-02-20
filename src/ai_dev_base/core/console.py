"""Rich console output helpers for AI Dev Base CLI.

Provides consistent, styled terminal output using the TodAI Design System.
Status messages go to stderr to keep stdout clean for agent output.

Style Mapping (TodAI Theme):
    - "error"          -> Bold red (#9c0136) for errors
    - "success"        -> Green (#03b971) for success messages
    - "warning"        -> Orange (#f5b332) for warnings
    - "info"           -> Blue (#0e8ac8) for info messages
    - "header"         -> Bold blue for section headers
    - "status.enabled" -> Green for enabled status
    - "status.disabled"-> Orange for disabled status
    - "table.title"    -> Bold blue for table titles
    - "table.category" -> Orange for category labels
    - "table.value"    -> Muted for values
"""

from rich.console import Console
from rich.table import Table

from ai_dev_base.core.theme import ICONS, TODAI_THEME

# =============================================================================
# Console Singletons
# =============================================================================

console: Console = Console(theme=TODAI_THEME)
"""Main console for stdout output (tables, agent output)."""

err_console: Console = Console(stderr=True, theme=TODAI_THEME)
"""Error console for stderr output (status messages, progress)."""


# =============================================================================
# Status Line Output
# =============================================================================


def status_line(label: str, value: str, style: str = "status.enabled") -> None:
    """Print a formatted status line to stderr.

    Matches the original Bash format from dev.sh:
        echo -e "   ${GREEN}Projects:${NC}  $CODE_DIR"

    Format: "   {label}:  {value}"
    - 3 spaces prefix for indentation
    - Label in specified color
    - Colon and 2 spaces separator
    - Value in default color

    Args:
        label: The label text (e.g., "Projects", "Docker", "Firewall").
        value: The value to display.
        style: Rich style for the label (TodAI Theme). Supported values:
            - "status.enabled" (default) - enabled/active status (green)
            - "status.disabled" - disabled/warning status (orange)
            - "status.error" - error status (red)
            - "info" - info/header (blue)
            - Legacy color names ("green", "yellow", "red", "blue") also work.

    Example:
        >>> status_line("Projects", "/path/to/code")
        # Output to stderr:    Projects:  /path/to/code

        >>> status_line("Docker", "Disabled", "status.disabled")
        # Output to stderr:    Docker:  Disabled
    """
    # Calculate padding to align values (longest label is ~10 chars)
    padding = max(0, 10 - len(label))
    err_console.print(f"   [{style}]{label}:[/{style}]{' ' * padding} {value}")


# =============================================================================
# Message Functions
# =============================================================================


def error(message: str) -> None:
    """Print error message to stderr with error styling.

    Displays a bold red error message with an error icon prefix.

    Args:
        message: The error message to display.

    Example:
        >>> error("Mount path does not exist")
        # Output to stderr: x Error: Mount path does not exist
    """
    err_console.print(f"[error]{ICONS['error']} Error: {message}[/error]")


def success(message: str) -> None:
    """Print success message to stderr with success styling.

    Displays a green success message with a checkmark icon prefix.

    Args:
        message: The success message to display.

    Example:
        >>> success("Build complete")
        # Output to stderr: checkmark Build complete
    """
    err_console.print(f"[success]{ICONS['success']} {message}[/success]")


def info(message: str) -> None:
    """Print info message to stderr with info styling.

    Displays a blue info message with an info icon prefix.

    Args:
        message: The info message to display.

    Example:
        >>> info("Starting AI Dev environment...")
        # Output to stderr: i Starting AI Dev environment...
    """
    err_console.print(f"[info]{ICONS['info']} {message}[/info]")


def warning(message: str) -> None:
    """Print warning message to stderr with warning styling.

    Displays an orange warning message with a warning icon prefix.

    Args:
        message: The warning message to display.

    Example:
        >>> warning("Config file not found, using defaults")
        # Output to stderr: ! Warning: Config file not found, using defaults
    """
    err_console.print(f"[warning]{ICONS['warning']} Warning: {message}[/warning]")


# =============================================================================
# Blank Line Output
# =============================================================================


def blank() -> None:
    """Print a blank line to stderr.

    Useful for spacing between output sections.
    """
    err_console.print()


# =============================================================================
# Table Creation
# =============================================================================


def print_volume_table(volumes: dict[str, list[str]]) -> None:
    """Print a formatted volume table to stdout.

    Args:
        volumes: Dictionary mapping category names to lists of volume names.
    """
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


# =============================================================================
# Header Output
# =============================================================================


def header(title: str) -> None:
    """Print a section header to stderr with header styling.

    Displays a bold blue header text.

    Args:
        title: The header title (without colon - it will be added).

    Example:
        >>> header("Configuration")
        # Output to stderr: Configuration:
    """
    err_console.print(f"[header]{title}:[/header]")
