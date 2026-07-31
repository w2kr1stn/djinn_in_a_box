"""Exception types shared across Djinn's core layers."""

from pathlib import Path


class ConfigNotFoundError(FileNotFoundError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"Configuration not found: {path}\nRun 'djinn init' to create configuration."
        )


class ConfigValidationError(ValueError):
    pass


class MountSpecificationError(ValueError):
    """Raised when a ``--mount`` value cannot be resolved or parsed."""


class RuntimeMountSpecificationError(RuntimeError):
    """Raised when an internal runtime mount builder emits invalid arguments."""
