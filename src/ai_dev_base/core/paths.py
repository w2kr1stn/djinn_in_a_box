"""Path constants and utilities for AI Dev Base CLI.

This module provides XDG-compliant configuration paths and utilities
for resolving mount paths used in Docker container operations.
"""

from pathlib import Path

# =============================================================================
# XDG-Compliant Configuration Paths
# =============================================================================

CONFIG_DIR: Path = Path.home() / ".config" / "ai-dev-base"
"""XDG-compliant configuration directory (~/.config/ai-dev-base/)."""

CONFIG_FILE: Path = CONFIG_DIR / "config.toml"
"""Main configuration file path."""

AGENTS_FILE: Path = CONFIG_DIR / "agents.toml"
"""Agent definitions file path (optional user override)."""


# =============================================================================
# Project Root Discovery
# =============================================================================


def get_project_root() -> Path:
    """Find the ai-dev-base project root directory.

    Searches upward from this module's location for a directory containing
    docker-compose.yml. This is the canonical marker for the project root.

    Returns:
        Path to the project root directory containing docker-compose.yml.

    Raises:
        FileNotFoundError: If no docker-compose.yml can be found in any
            parent directory.

    Example:
        >>> root = get_project_root()
        >>> (root / "docker-compose.yml").exists()
        True
    """
    # Start from this module's directory
    current = Path(__file__).resolve().parent

    # Traverse upward looking for docker-compose.yml
    while current != current.parent:
        if (current / "docker-compose.yml").exists():
            return current
        current = current.parent

    # Check root as final option
    if (current / "docker-compose.yml").exists():
        return current

    msg = (
        "Could not find ai-dev-base project root. "
        "No docker-compose.yml found in any parent directory of "
        f"{Path(__file__).resolve()}"
    )
    raise FileNotFoundError(msg)


# =============================================================================
# Configuration Directory Access
# =============================================================================


def get_config_dir(*, create: bool = True) -> Path:
    """Get the XDG-compliant configuration directory.

    Returns the configuration directory path (~/.config/ai-dev-base/).
    By default, creates the directory if it does not exist.

    Args:
        create: If True (default), create the directory if it does not exist.

    Returns:
        Path to the configuration directory.

    Example:
        >>> config_dir = get_config_dir()
        >>> config_dir.exists()
        True
        >>> str(config_dir).endswith('.config/ai-dev-base')
        True
    """
    if create and not CONFIG_DIR.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    return CONFIG_DIR


# =============================================================================
# Mount Path Resolution
# =============================================================================


def resolve_mount_path(path: str | Path) -> Path:
    """Resolve and validate a mount path for Docker container mounts.

    Handles the following path formats:
    - Tilde expansion: ~ -> home directory
    - Relative paths: resolved against current working directory
    - Absolute paths: normalized

    This mirrors the path resolution logic from the original dev.sh script:
    ```bash
    PARSED_MOUNT_PATH="${PARSED_MOUNT_PATH/#\\~/$HOME}"
    if [[ -d "$PARSED_MOUNT_PATH" ]]; then
        PARSED_MOUNT_PATH="$(cd "$PARSED_MOUNT_PATH" && pwd)"
    ```

    Args:
        path: Path string or Path object to resolve. Can be:
            - "~" or "~/subdir" (tilde expansion)
            - "." or "./subdir" (relative path)
            - "/absolute/path" (absolute path)

    Returns:
        Fully resolved absolute Path object.

    Raises:
        FileNotFoundError: If the resolved path does not exist.
        NotADirectoryError: If the resolved path exists but is not a directory.

    Example:
        >>> # Tilde expansion
        >>> home = resolve_mount_path("~")
        >>> home == Path.home()
        True

        >>> # Relative path resolution
        >>> cwd = resolve_mount_path(".")
        >>> cwd == Path.cwd()
        True

        >>> # Absolute path normalization
        >>> tmp = resolve_mount_path("/tmp")
        >>> tmp.is_absolute()
        True
    """
    # Convert to Path if string
    path_obj = Path(path) if isinstance(path, str) else path

    # Expand tilde (~) to home directory
    if str(path_obj).startswith("~"):
        path_obj = path_obj.expanduser()

    # Resolve to absolute path (handles relative paths and normalizes)
    resolved = path_obj.resolve()

    # Validate existence
    if not resolved.exists():
        msg = f"Mount path does not exist: {resolved}"
        raise FileNotFoundError(msg)

    # Validate it's a directory
    if not resolved.is_dir():
        msg = f"Mount path is not a directory: {resolved}"
        raise NotADirectoryError(msg)

    return resolved
