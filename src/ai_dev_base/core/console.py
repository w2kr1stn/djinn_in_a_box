"""Rich console output helpers for AI Dev Base CLI.

Provides consistent, styled terminal output that matches the original
Bash script color scheme. Status messages go to stderr to keep stdout
clean for agent output.

Color Mapping (from scripts/colors.sh):
    - RED    -> "red"     (errors)
    - GREEN  -> "green"   (success, enabled status)
    - YELLOW -> "yellow"  (warnings, disabled status)
    - BLUE   -> "blue"    (info, headers)
"""

from rich.console import Console
from rich.table import Table

# =============================================================================
# Console Singletons
# =============================================================================

console: Console = Console()
"""Main console for stdout output (tables, agent output)."""

err_console: Console = Console(stderr=True)
"""Error console for stderr output (status messages, progress)."""


# =============================================================================
# Status Line Output
# =============================================================================


def status_line(label: str, value: str, style: str = "green") -> None:
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
        style: Rich style for the label. Supported values:
            - "green" (default) - enabled/active status
            - "yellow" - disabled/warning status
            - "red" - error status
            - "blue" - info/header

    Example:
        >>> status_line("Projects", "/path/to/code")
        # Output to stderr:    Projects:  /path/to/code

        >>> status_line("Docker", "Disabled", "yellow")
        # Output to stderr:    Docker:  Disabled
    """
    # Calculate padding to align values (longest label is ~10 chars)
    padding = max(0, 10 - len(label))
    err_console.print(f"   [{style}]{label}:[/{style}]{' ' * padding} {value}")


# =============================================================================
# Message Functions
# =============================================================================


def error(message: str) -> None:
    """Print error message to stderr in red.

    Matches: echo -e "${RED}Error: ...${NC}"

    Args:
        message: The error message to display.

    Example:
        >>> error("Mount path does not exist")
        # Output to stderr: Error: Mount path does not exist
    """
    err_console.print(f"[red]Error: {message}[/red]")


def success(message: str) -> None:
    """Print success message to stderr in green.

    Matches: echo -e "${GREEN}Done! ...${NC}"

    Args:
        message: The success message to display.

    Example:
        >>> success("Build complete")
        # Output to stderr: Build complete
    """
    err_console.print(f"[green]{message}[/green]")


def info(message: str) -> None:
    """Print info message to stderr in blue.

    Matches: echo -e "${BLUE}Building ai-dev-base image...${NC}"

    Args:
        message: The info message to display.

    Example:
        >>> info("Starting AI Dev environment...")
        # Output to stderr: Starting AI Dev environment...
    """
    err_console.print(f"[blue]{message}[/blue]")


def warning(message: str) -> None:
    """Print warning message to stderr in yellow.

    Matches: echo -e "${YELLOW}Warning: ...${NC}"

    Args:
        message: The warning message to display.

    Example:
        >>> warning("Config file not found, using defaults")
        # Output to stderr: Warning: Config file not found, using defaults
    """
    err_console.print(f"[yellow]Warning: {message}[/yellow]")


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


def create_volume_table(volumes: dict[str, list[str]]) -> Table:
    """Create a Rich table for volume listing.

    Creates a formatted table showing Docker volumes organized by category.
    Matches the output format from dev.sh list_volumes function.

    Args:
        volumes: Dictionary mapping category names to lists of volume names.
            Categories should be: "credentials", "tools", "cache", "data".
            Volume names should be the short names (without "ai-dev-" prefix).

    Returns:
        A configured Rich Table ready for printing to stdout.

    Example:
        >>> volumes = {
        ...     "credentials": ["claude-config", "gemini-config"],
        ...     "tools": ["azure-config", "pulumi-config"],
        ...     "cache": ["uv-cache"],
        ... }
        >>> table = create_volume_table(volumes)
        >>> console.print(table)
    """
    table = Table(
        title="AI Dev Volumes",
        title_style="blue bold",
        show_header=True,
        header_style="bold",
    )

    table.add_column("Category", style="yellow", width=15)
    table.add_column("Volume", style="dim")

    # Category display names matching dev.sh
    category_names: dict[str, str] = {
        "credentials": "Credentials",
        "tools": "Tool Configs",
        "cache": "Cache",
        "data": "Data",
    }

    # Process categories in consistent order
    for category in ["credentials", "tools", "cache", "data"]:
        if category in volumes and volumes[category]:
            display_name = category_names.get(category, category.title())
            volume_list = volumes[category]

            # Add first row with category name
            table.add_row(display_name, volume_list[0])

            # Add remaining volumes with empty category column
            for vol in volume_list[1:]:
                table.add_row("", vol)

            # Add separator between categories (except last)
            if category != "data":
                table.add_row("", "")

    return table


def print_volume_table(volumes: dict[str, list[str]]) -> None:
    """Print a volume table to stdout.

    Convenience function that creates and prints a volume table.

    Args:
        volumes: Dictionary mapping category names to lists of volume names.
            See create_volume_table() for details.

    Example:
        >>> volumes = {"credentials": ["claude-config"], "cache": ["uv-cache"]}
        >>> print_volume_table(volumes)
    """
    table = create_volume_table(volumes)
    console.print(table)


# =============================================================================
# Header Output
# =============================================================================


def header(title: str) -> None:
    """Print a section header to stderr in blue.

    Matches: echo -e "${BLUE}Configuration:${NC}"

    Args:
        title: The header title (without colon - it will be added).

    Example:
        >>> header("Configuration")
        # Output to stderr: Configuration:
    """
    err_console.print(f"[blue]{title}:[/blue]")
