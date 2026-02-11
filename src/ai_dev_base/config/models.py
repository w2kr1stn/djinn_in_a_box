"""Pydantic models for AI Dev Base configuration.

Provides type-safe configuration models with validation and sensible defaults.
All models use Pydantic v2 with strict typing and TOML-compatible serialization.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# =============================================================================
# Memory Format Validation
# =============================================================================


def validate_memory_format(value: str) -> str:
    """Validate Docker memory format string.

    Accepts formats like "12G", "4096M", "2g", "512m".

    Args:
        value: Memory string to validate.

    Returns:
        Validated memory string (uppercase suffix).

    Raises:
        ValueError: If format is invalid.
    """
    pattern = r"^[1-9]\d*[GMKgmk]$"
    if not re.match(pattern, value):
        msg = (
            f"Invalid memory format: '{value}'. "
            "Expected format like '12G', '4096M', '512K' (positive number + G/M/K suffix)"
        )
        raise ValueError(msg)
    # Normalize to uppercase suffix
    return value[:-1] + value[-1].upper()


def _memory_to_bytes(value: str) -> int:
    """Convert a memory string like '12G' to bytes for comparison."""
    units = {"K": 1024, "M": 1024**2, "G": 1024**3}
    return int(value[:-1]) * units[value[-1].upper()]


# =============================================================================
# Agent Configuration
# =============================================================================


class AgentConfig(BaseModel):
    """Configuration for a CLI coding agent.

    Defines how to invoke a specific agent (Claude, Gemini, Codex, etc.)
    including the binary name, various flags for different modes, and
    prompt injection template.

    Example:
        >>> agent = AgentConfig(
        ...     binary="claude",
        ...     description="Anthropic Claude Code CLI",
        ...     headless_flags=["-p"],
        ...     write_flags=["--dangerously-skip-permissions"],
        ... )
        >>> agent.binary
        'claude'
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
    )

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


# =============================================================================
# Resource Limits
# =============================================================================


class ResourceLimits(BaseModel):
    """Docker resource limits configuration.

    Defines CPU and memory limits and reservations for Docker containers.
    Memory values use Docker's format (e.g., '12G', '4096M').

    Example:
        >>> limits = ResourceLimits(cpu_limit=4, memory_limit="8G")
        >>> limits.cpu_limit
        4
        >>> limits.memory_limit
        '8G'
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
    )

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
        mem_res = _memory_to_bytes(self.memory_reservation)
        mem_lim = _memory_to_bytes(self.memory_limit)
        if mem_res > mem_lim:
            msg = (
                f"memory_reservation ({self.memory_reservation}) cannot exceed "
                f"memory_limit ({self.memory_limit})"
            )
            raise ValueError(msg)
        return self


# =============================================================================
# Shell Configuration
# =============================================================================


class ShellConfig(BaseModel):
    """Shell mounting configuration for the development container.

    Controls whether host shell configurations (zshrc, oh-my-zsh, oh-my-posh)
    are mounted into the container.

    Example:
        >>> shell = ShellConfig(skip_mounts=True)
        >>> shell.skip_mounts
        True
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
    )

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


# =============================================================================
# Main Application Configuration
# =============================================================================


class AppConfig(BaseModel):
    """Main application configuration for AI Dev Base.

    This is the root configuration model that combines all settings:
    project directory, timezone, resource limits, shell options, and
    optionally agent overrides.

    Example:
        >>> config = AppConfig(code_dir=Path.home() / "projects")
        >>> config.timezone
        'Europe/Berlin'
        >>> config.resources.cpu_limit
        6
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
    )

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
    def expand_code_dir(cls, value: str | Path) -> Path:
        """Expand tilde in code_dir path."""
        if isinstance(value, str):
            return Path(value).expanduser()
        return value.expanduser()

    @field_validator("code_dir", mode="after")
    @classmethod
    def validate_code_dir(cls, value: Path) -> Path:
        """Validate code_dir exists and is a directory."""
        # Expanduser should already be called by the 'before' validator
        if not value.exists():
            msg = f"code_dir does not exist: {value}"
            raise ValueError(msg)
        if not value.is_dir():
            msg = f"code_dir is not a directory: {value}"
            raise ValueError(msg)
        return value

    @field_validator("timezone", mode="after")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Basic timezone format validation.

        Validates that timezone looks like a valid IANA timezone identifier
        (e.g., 'Europe/Berlin', 'America/New_York', 'UTC').

        Full validation against the timezone database is not performed here
        to avoid adding extra dependencies.
        """
        # Basic format check: should be "Region/City" or simple like "UTC"
        if not value:
            msg = "Timezone cannot be empty"
            raise ValueError(msg)

        # Common simple timezones
        simple_zones = {"UTC", "GMT", "EST", "PST", "CST", "MST"}
        if value in simple_zones:
            return value

        # IANA format: "Continent/City" or "Continent/Region/City"
        parts = value.split("/")
        if len(parts) < 2:
            msg = (
                f"Invalid timezone format: '{value}'. "
                "Expected IANA format like 'Europe/Berlin' or 'America/New_York'"
            )
            raise ValueError(msg)

        return value


# =============================================================================
# Agents Container
# =============================================================================


class AgentsConfig(BaseModel):
    """Container for all agent configurations.

    Holds a dictionary of agent configurations keyed by agent name.
    This model is used for loading agents.toml files.

    Example:
        >>> agents_config = AgentsConfig(agents={
        ...     "claude": AgentConfig(binary="claude"),
        ...     "gemini": AgentConfig(binary="gemini"),
        ... })
        >>> "claude" in agents_config.agents
        True
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
    )

    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    """Dictionary of agent configurations keyed by agent name."""
