"""Exception types for AI Dev Base configuration errors."""

from pathlib import Path


class ConfigNotFoundError(FileNotFoundError):
    """Raised when config file is missing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"Configuration not found: {path}\nRun 'codeagent init' to create configuration."
        )


class ConfigValidationError(ValueError):
    """Raised when config validation fails."""
