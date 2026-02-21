"""Pydantic models for AI Dev Base configuration.

Provides type-safe configuration models with validation and sensible defaults.
All models use Pydantic v2 with strict typing and TOML-compatible serialization.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def validate_memory_format(value: str) -> str:
    """Validate and normalize Docker memory format (e.g. '12G', '4096m' -> '4096M')."""
    pattern = r"^[1-9]\d*[GMKgmk]$"
    if not re.match(pattern, value):
        msg = (
            f"Invalid memory format: '{value}'. "
            "Expected format like '12G', '4096M', '512K' (positive number + G/M/K suffix)"
        )
        raise ValueError(msg)
    # Normalize to uppercase suffix
    return value[:-1] + value[-1].upper()


class AgentConfig(BaseModel):
    """Configuration for a CLI coding agent.

    Defines how to invoke a specific agent (Claude, Gemini, Codex, etc.)
    including the binary name, various flags for different modes, and
    prompt injection template.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    binary: Annotated[str, Field(min_length=1)]
    """Executable binary name (e.g., 'claude', 'gemini')."""

    description: str = ""
    """Human-readable description of the agent."""

    headless_flags: list[str] = Field(default_factory=list)
    """Flags for headless/non-interactive mode (e.g., ['-p'])."""

    read_only_flags: list[str] = Field(default_factory=list)
    """Flags for read-only/plan mode (e.g., ['--permission-mode', 'plan'])."""

    write_flags: list[str] = Field(default_factory=list)
    """Flags to enable file modifications (e.g., ['--dangerously-skip-permissions'])."""

    json_flags: list[str] = Field(default_factory=list)
    """Flags for JSON output format (e.g., ['--output-format', 'json'])."""

    model_flag: str = "--model"
    """Flag for specifying the model (e.g., '--model', '-m')."""

    prompt_template: str = '"$AGENT_PROMPT"'
    """Shell template for prompt injection. Uses env var expansion at runtime."""


class ResourceLimits(BaseModel):
    """Docker resource limits configuration.

    Defines CPU and memory limits and reservations for Docker containers.
    Memory values use Docker's format (e.g., '12G', '4096M').
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_limit: Annotated[int, Field(ge=1, le=128)] = 6
    """Maximum CPU cores allocated to the container."""

    memory_limit: str = "12G"
    """Maximum memory allocated (e.g., '12G', '16384M')."""

    cpu_reservation: Annotated[int, Field(ge=1, le=128)] = 2
    """Reserved CPU cores guaranteed to the container."""

    memory_reservation: str = "4G"
    """Reserved memory guaranteed (e.g., '4G', '4096M')."""

    @field_validator("memory_limit", "memory_reservation", mode="after")
    @classmethod
    def validate_memory(cls, value: str) -> str:
        """Validate memory format strings."""
        return validate_memory_format(value)

    @model_validator(mode="after")
    def validate_reservations(self) -> ResourceLimits:
        """Ensure reservations do not exceed limits."""
        if self.cpu_reservation > self.cpu_limit:
            msg = (
                f"cpu_reservation ({self.cpu_reservation}) cannot exceed "
                f"cpu_limit ({self.cpu_limit})"
            )
            raise ValueError(msg)
        units = {"K": 1024, "M": 1024**2, "G": 1024**3}
        mem_res = int(self.memory_reservation[:-1]) * units[self.memory_reservation[-1]]
        mem_lim = int(self.memory_limit[:-1]) * units[self.memory_limit[-1]]
        if mem_res > mem_lim:
            msg = (
                f"memory_reservation ({self.memory_reservation}) cannot exceed "
                f"memory_limit ({self.memory_limit})"
            )
            raise ValueError(msg)
        return self


class ShellConfig(BaseModel):
    """Shell mounting configuration for the development container.

    Controls whether host shell configurations (zshrc, oh-my-zsh, oh-my-posh)
    are mounted into the container.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skip_mounts: bool = False
    """Skip mounting host shell configs (zshrc, oh-my-zsh, oh-my-posh)."""

    omp_theme_path: Path | None = None
    """Custom Oh My Posh theme file path. If None, uses default location."""

    @field_validator("omp_theme_path", mode="before")
    @classmethod
    def expand_omp_theme_path(cls, value: str | Path | None) -> Path | None:
        """Expand tilde and validate Oh My Posh theme path."""
        if value is None:
            return None

        path = Path(value).expanduser() if isinstance(value, str) else value.expanduser()

        # Path existence is not validated here because:
        # 1. The theme file might not exist yet during config creation
        # 2. Skip_mounts=True makes this field irrelevant
        # Validation of existence should happen at runtime when needed

        return path


class AppConfig(BaseModel):
    """Main application configuration for AI Dev Base.

    This is the root configuration model that combines all settings:
    project directory, timezone, resource limits, shell options, and
    optionally agent overrides.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code_dir: Path
    """Projects directory to mount as ~/projects in the container. Required."""

    timezone: str = "Europe/Berlin"
    """Container timezone (TZ environment variable)."""

    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    """Docker resource limits and reservations."""

    shell: ShellConfig = Field(default_factory=ShellConfig)
    """Shell mounting configuration."""

    @field_validator("code_dir", mode="before")
    @classmethod
    def validate_code_dir(cls, value: str | Path) -> Path:
        """Expand tilde and validate code_dir exists as a directory."""
        path = Path(value).expanduser()
        if not path.exists():
            msg = f"code_dir does not exist: {path}"
            raise ValueError(msg)
        if not path.is_dir():
            msg = f"code_dir is not a directory: {path}"
            raise ValueError(msg)
        return path
