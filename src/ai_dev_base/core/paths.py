"""Path constants and utilities for AI Dev Base CLI.

This module provides XDG-compliant configuration paths and utilities
for resolving mount paths used in Docker container operations.
"""

import functools
from pathlib import Path

CONFIG_DIR: Path = Path.home() / ".config" / "ai-dev-base"
"""XDG-compliant configuration directory (~/.config/ai-dev-base/)."""

CONFIG_FILE: Path = CONFIG_DIR / "config.toml"
"""Main configuration file path."""

AGENTS_FILE: Path = CONFIG_DIR / "agents.toml"
"""Agent definitions file path (optional user override)."""


@functools.cache
def get_project_root() -> Path:
    """Find the ai-dev-base project root directory.

    Searches upward from this module's location for a directory containing
    docker-compose.yml. This is the canonical marker for the project root.

    Returns:
        Path to the project root directory containing docker-compose.yml.

    Raises:
        FileNotFoundError: If no docker-compose.yml can be found in any
            parent directory.
    """
    # Start from this module's directory
    current = Path(__file__).resolve().parent

    # Traverse upward looking for docker-compose.yml
    while current != current.parent:
        if (current / "docker-compose.yml").exists():
            return current
        current = current.parent

    msg = (
        "Could not find ai-dev-base project root. "
        "No docker-compose.yml found in any parent directory of "
        f"{Path(__file__).resolve()}"
    )
    raise FileNotFoundError(msg)


def resolve_mount_path(path: str | Path) -> Path:
    """Resolve and validate a mount path for Docker container mounts.

    Handles tilde expansion, relative paths, and absolute paths.
    Raises FileNotFoundError or NotADirectoryError on invalid paths.
    """
    path_obj = Path(path) if isinstance(path, str) else path
    resolved = path_obj.expanduser().resolve()

    # Validate existence
    if not resolved.exists():
        msg = f"Mount path does not exist: {resolved}"
        raise FileNotFoundError(msg)

    # Validate it's a directory
    if not resolved.is_dir():
        msg = f"Mount path is not a directory: {resolved}"
        raise NotADirectoryError(msg)

    return resolved
