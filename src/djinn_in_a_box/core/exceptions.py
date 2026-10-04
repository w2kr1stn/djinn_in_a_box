"""Exception types shared across Djinn's core layers."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from djinn_in_a_box.config.declarations import DeclarationSet
    from djinn_in_a_box.config.models import AppConfig


class ConfigNotFoundError(FileNotFoundError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"Configuration not found: {path}\nRun 'djinn init' to create configuration."
        )


class ConfigValidationError(ValueError):
    def __init__(
        self, message: str, *, declarations: DeclarationSet | None = None,
        reservation_config: AppConfig | None = None,
    ) -> None:
        super().__init__(message)
        self.declarations = declarations
        self.reservation_config = reservation_config


class ZoneConfigurationError(ConfigValidationError):
    pass


class ZoneRootValidationError(ZoneConfigurationError):
    pass


class MountSpecificationError(ValueError):
    """Raised when a ``--mount`` value cannot be resolved or parsed."""


class SopsAgeKeyFileError(MountSpecificationError):
    """Raised when the configured SOPS age identity file cannot be mounted safely."""


class DeclarationSpecificationError(MountSpecificationError):
    """Raised before creation when a configured declaration cannot be applied."""


class RuntimeMountSpecificationError(RuntimeError):
    """Raised when an internal runtime mount builder emits invalid arguments."""
