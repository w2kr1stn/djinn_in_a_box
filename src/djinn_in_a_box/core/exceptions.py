"""Exception types for Djinn in a Box configuration errors."""

from pathlib import Path


class ConfigNotFoundError(FileNotFoundError):
    """Raised when config file is missing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"Configuration not found: {path}\nRun 'djinn init' to create configuration."
        )


class ConfigValidationError(ValueError):
    """Raised when config validation fails."""
