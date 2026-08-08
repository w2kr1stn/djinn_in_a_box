"""Pydantic models for Djinn in a Box configuration.

Provides type-safe configuration models with validation and sensible defaults.
All models use Pydantic v2 with strict typing and TOML-compatible serialization.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def validate_memory_format(value: str) -> str:
    """Validate and normalize Docker memory format (e.g. '8G', '4096m' -> '4096M')."""
    pattern = r"^[1-9]\d*[GMKgmk]$"
    if not re.match(pattern, value):
        msg = (
            f"Invalid memory format: '{value}'. "
            "Expected format like '8G', '4096M', '512K' (positive number + G/M/K suffix)"
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

    default_model: Annotated[str, Field(min_length=1)] | None = None
    """Model used when the invocation does not provide an explicit override."""

    prompt_template: str = '"$AGENT_PROMPT"'
    """Shell template for prompt injection. Uses env var expansion at runtime."""


class ResourceLimits(BaseModel):
    """Docker resource limits configuration.

    Defines CPU and memory limits and reservations for Docker containers.
    Memory values use Docker's format (e.g., '8G', '4096M').
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_limit: Annotated[int, Field(ge=1, le=128)] = 4
    """Maximum CPU cores allocated to the container."""

    memory_limit: str = "8G"
    """Maximum memory allocated (e.g., '8G', '8192M')."""

    cpu_reservation: Annotated[int, Field(ge=1, le=128)] = 1
    """Reserved CPU cores guaranteed to the container."""

    memory_reservation: str = "2G"
    """Reserved memory guaranteed (e.g., '2G', '2048M')."""

    @field_validator("memory_limit", "memory_reservation", mode="after")
    @classmethod
    def validate_memory(cls, value: str) -> str:
        return validate_memory_format(value)

    @model_validator(mode="after")
    def validate_reservations(self) -> ResourceLimits:
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
    """Custom Oh My Posh theme file path. If None, no OMP theme is mounted."""

    @field_validator("omp_theme_path", mode="before")
    @classmethod
    def expand_omp_theme_path(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None

        path = Path(value).expanduser() if isinstance(value, str) else value.expanduser()

        # Path existence is not validated here because:
        # 1. The theme file might not exist yet during config creation
        # 2. Skip_mounts=True makes this field irrelevant
        # Validation of existence should happen at runtime when needed

        return path


ConfigSyncSource = Literal["claude", "codex", "opencode"]


class ConfigSyncConfig(BaseModel):
    """Agent workflow synchronization configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: ConfigSyncSource = "claude"
    """Native workflow tree used as the single source of truth."""


class AppConfig(BaseModel):
    """Main application configuration for Djinn in a Box.

    This is the root configuration model that combines all settings:
    project directory, timezone, resource limits, shell options, and
    optionally agent overrides.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code_dir: Path
    """Projects directory to mount as ~/projects in the container. Required."""

    timezone: str = "UTC"
    """Container timezone (TZ environment variable)."""

    config_root: Path = Field(default_factory=lambda: Path.home() / ".djinn" / "config")
    """Root for config/credential bind-mounts (DJINN_CONFIG_ROOT). Local, per-host."""

    shared_root: Path | None = None
    """Optional root for mirrorable, non-backed-up agent data."""

    local_root: Path | None = None
    """Optional root for host-local, rebuildable agent data."""

    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    """Docker resource limits and reservations."""

    shell: ShellConfig = Field(default_factory=ShellConfig)
    """Shell mounting configuration."""

    config_sync: ConfigSyncConfig = Field(default_factory=ConfigSyncConfig)
    """Agent workflow synchronization configuration."""

    @field_validator("timezone", mode="after")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        from zoneinfo import available_timezones

        if value not in available_timezones():
            msg = f"Unknown timezone: '{value}'. Use IANA format (e.g., 'UTC', 'America/New_York')"
            raise ValueError(msg)
        return value

    @field_validator("code_dir", mode="before")
    @classmethod
    def validate_code_dir(cls, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.exists():
            msg = f"code_dir does not exist: {path}"
            raise ValueError(msg)
        if not path.is_dir():
            msg = f"code_dir is not a directory: {path}"
            raise ValueError(msg)
        return path

    @field_validator("config_root", mode="before")
    @classmethod
    def expand_config_root(cls, value: str | Path) -> Path:
        path = Path(value) if isinstance(value, str) else value
        return path.expanduser().resolve()

    @field_validator("shared_root", "local_root", mode="before")
    @classmethod
    def expand_optional_zone_root(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        path = Path(value) if isinstance(value, str) else value
        return path.expanduser().resolve()
