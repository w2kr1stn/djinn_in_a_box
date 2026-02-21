"""Configuration loading for AI Dev Base.

Provides TOML-based configuration loading with:
- Automatic validation via Pydantic models
- Fallback to bundled defaults for agents
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import ValidationError

from ai_dev_base.config.defaults import DEFAULT_AGENTS
from ai_dev_base.config.models import AgentConfig, AppConfig
from ai_dev_base.core.console import warning
from ai_dev_base.core.paths import (
    AGENTS_FILE,
    CONFIG_FILE,
    get_project_root,
)


class ConfigNotFoundError(FileNotFoundError):
    """Raised when config file is missing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"Configuration not found: {path}\nRun 'codeagent init' to create configuration."
        )


class ConfigValidationError(ValueError):
    """Raised when config validation fails."""


def _format_validation_errors(e: ValidationError) -> str:
    """Format Pydantic validation errors as indented bullet list."""
    return "\n".join(
        f"  - {'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
    )


def load_config(path: Path | None = None) -> AppConfig:
    """Load application config from TOML file.

    Loads configuration from the specified path or the default location
    (~/.config/ai-dev-base/config.toml). The TOML structure is flattened
    and validated with Pydantic.

    Args:
        path: Custom config file path. Defaults to CONFIG_FILE.

    Returns:
        Validated AppConfig instance.

    Raises:
        ConfigNotFoundError: If config file does not exist.
        ConfigValidationError: If config is invalid.
    """
    config_path = path or CONFIG_FILE

    if not config_path.exists():
        raise ConfigNotFoundError(config_path)

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigValidationError(f"Invalid TOML syntax in {config_path}: {e}") from e

    # Transform nested TOML structure to flat Pydantic model
    # [general] -> top-level, [shell] -> shell, [resources] -> resources
    try:
        config_dict = _transform_toml_to_config(data)
        return AppConfig(**config_dict)
    except ValidationError as e:
        raise ConfigValidationError(
            f"Configuration validation failed for {config_path}:\n{_format_validation_errors(e)}"
        ) from e


def _transform_toml_to_config(data: dict[str, Any]) -> dict[str, Any]:
    """Transform TOML structure to AppConfig-compatible dict.

    Flattens [general] section to top-level fields. Unknown sections/keys
    are caught by Pydantic's extra="forbid" with proper error messages.
    """
    general = data.get("general", {})
    return {**general, **{k: v for k, v in data.items() if k != "general"}}


def load_agents(path: Path | None = None) -> dict[str, AgentConfig]:
    """Load agent configurations with fallback to defaults.

    Priority (first existing wins):
    1. Specified path (if provided)
    2. User's ~/.config/ai-dev-base/agents.toml
    3. Bundled config/agents.toml in project root
    4. DEFAULT_AGENTS from defaults.py

    Args:
        path: Custom agents file path. Defaults to automatic discovery.

    Returns:
        Dict mapping agent names to AgentConfig.
    """
    # Priority 1: Explicit path
    if path is not None:
        if path.exists():
            return _load_agents_from_toml(path)
        # If explicit path given but missing, raise error
        raise ConfigNotFoundError(path)

    # Priority 2: User config directory
    if AGENTS_FILE.exists():
        return _load_agents_from_toml(AGENTS_FILE)

    # Priority 3: Bundled config in project root
    try:
        bundled_path = get_project_root() / "config" / "agents.toml"
        if bundled_path.exists():
            return _load_agents_from_toml(bundled_path)
    except FileNotFoundError:
        # No project root found, fall through to defaults
        pass

    # Priority 4: Built-in defaults
    warning("No agents.toml found, using built-in defaults")
    return dict(DEFAULT_AGENTS)


def _load_agents_from_toml(path: Path) -> dict[str, AgentConfig]:
    """Load agents from a TOML file.

    Expected format:
        [agents.claude]
        binary = "claude"
        description = "Anthropic Claude Code CLI"
        ...

    Args:
        path: Path to agents.toml file.

    Returns:
        Dict mapping agent names to AgentConfig.

    Raises:
        ConfigValidationError: If TOML is invalid or agents malformed.
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigValidationError(f"Invalid TOML syntax in {path}: {e}") from e

    agents_data = data.get("agents", {})
    if not agents_data:
        raise ConfigValidationError(
            f"No agents defined in {path}. Expected [agents.<name>] sections."
        )

    try:
        return {name: AgentConfig(**agent_data) for name, agent_data in agents_data.items()}
    except ValidationError as e:
        raise ConfigValidationError(
            f"Invalid agent configuration in {path}:\n{_format_validation_errors(e)}"
        ) from e


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Save configuration to TOML file.

    Transforms AppConfig back to nested TOML structure and writes it.
    Creates parent directories if they do not exist.

    Args:
        config: AppConfig to save.
        path: Target path. Defaults to CONFIG_FILE.
    """
    config_path = path or CONFIG_FILE

    # Ensure parent directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Transform AppConfig to TOML structure
    toml_data = _transform_config_to_toml(config)

    # Atomic write: write to temp file then rename to avoid corruption on interrupt
    fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(toml_data, f)
        os.replace(tmp_path, config_path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def _transform_config_to_toml(config: AppConfig) -> dict[str, Any]:
    """Transform AppConfig to TOML-compatible nested structure."""
    data = config.model_dump(mode="json", exclude_none=True)
    # Wrap top-level fields into [general] section (TOML layout convention)
    return {
        "general": {
            "code_dir": data.pop("code_dir"),
            "timezone": data.pop("timezone"),
        },
        **data,
    }
