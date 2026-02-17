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
from ai_dev_base.config.models import AgentConfig, AgentsConfig, AppConfig
from ai_dev_base.core.paths import (
    AGENTS_FILE,
    CONFIG_DIR,
    CONFIG_FILE,
    get_project_root,
)

# =============================================================================
# Custom Exceptions
# =============================================================================


class ConfigNotFoundError(FileNotFoundError):
    """Raised when config file is missing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"Configuration not found: {path}\nRun 'codeagent init' to create configuration."
        )


class ConfigValidationError(ValueError):
    """Raised when config validation fails."""


# =============================================================================
# Configuration Loading
# =============================================================================


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

    Example:
        >>> config = load_config()
        >>> config.code_dir
        PosixPath('/home/user/projects')
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

    TOML structure:
        [general]
        code_dir = "/path"
        timezone = "Europe/Berlin"

        [shell]
        skip_mounts = false

        [resources]
        cpu_limit = 6

    Transformed to:
        {
            "code_dir": "/path",
            "timezone": "Europe/Berlin",
            "shell": {"skip_mounts": false},
            "resources": {"cpu_limit": 6}
        }
    """
    result: dict[str, Any] = {}

    known_sections = {"general", "shell", "resources"}
    unknown_sections = set(data.keys()) - known_sections
    if unknown_sections:
        from ai_dev_base.core.console import warning

        warning(f"Unknown config sections ignored: {', '.join(sorted(unknown_sections))}")

    # Extract [general] section to top-level
    general = data.get("general", {})
    known_general_keys = {"code_dir", "timezone"}
    unknown_general = set(general.keys()) - known_general_keys
    if unknown_general:
        from ai_dev_base.core.console import warning

        warning(f"Unknown keys in [general] ignored: {', '.join(sorted(unknown_general))}")

    if "code_dir" in general:
        result["code_dir"] = general["code_dir"]
    if "timezone" in general:
        result["timezone"] = general["timezone"]

    # Pass through nested sections (Pydantic extra="forbid" catches unknown keys)
    if "shell" in data:
        result["shell"] = data["shell"]
    if "resources" in data:
        result["resources"] = data["resources"]

    return result


def _format_validation_errors(error: ValidationError) -> str:
    """Format Pydantic validation errors for user display."""
    lines: list[str] = []
    for err in error.errors():
        loc = ".".join(str(item) for item in err["loc"])
        msg = err["msg"]
        lines.append(f"  - {loc}: {msg}")
    return "\n".join(lines)


# =============================================================================
# Agent Loading
# =============================================================================


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

    Example:
        >>> agents = load_agents()
        >>> agents["claude"].binary
        'claude'
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
    from ai_dev_base.core.console import warning

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
        agents_config = AgentsConfig(
            agents={name: AgentConfig(**agent_data) for name, agent_data in agents_data.items()}
        )
        return agents_config.agents
    except ValidationError as e:
        raise ConfigValidationError(
            f"Invalid agent configuration in {path}:\n{_format_validation_errors(e)}"
        ) from e


# =============================================================================
# Configuration Saving
# =============================================================================


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Save configuration to TOML file.

    Transforms AppConfig back to nested TOML structure and writes it.
    Creates parent directories if they do not exist.

    Args:
        config: AppConfig to save.
        path: Target path. Defaults to CONFIG_FILE.

    Example:
        >>> config = AppConfig(code_dir=Path.home() / "projects")
        >>> save_config(config)  # Saves to ~/.config/ai-dev-base/config.toml
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
    """Transform AppConfig to TOML-compatible nested structure.

    Transforms:
        AppConfig(code_dir="/path", timezone="UTC", shell=ShellConfig(...))

    To TOML structure:
        [general]
        code_dir = "/path"
        timezone = "UTC"

        [shell]
        skip_mounts = false
    """
    result: dict[str, Any] = {
        "general": {
            "code_dir": str(config.code_dir),
            "timezone": config.timezone,
        },
        "shell": {
            "skip_mounts": config.shell.skip_mounts,
        },
        "resources": {
            "cpu_limit": config.resources.cpu_limit,
            "memory_limit": config.resources.memory_limit,
            "cpu_reservation": config.resources.cpu_reservation,
            "memory_reservation": config.resources.memory_reservation,
        },
    }

    # Include optional shell fields only if set
    if config.shell.omp_theme_path is not None:
        result["shell"]["omp_theme_path"] = str(config.shell.omp_theme_path)

    return result


# =============================================================================
# Directory Utilities
# =============================================================================


def ensure_config_dir() -> Path:
    """Ensure config directory exists, return path.

    Creates ~/.config/ai-dev-base/ if it does not exist.

    Returns:
        Path to the config directory.

    Example:
        >>> config_dir = ensure_config_dir()
        >>> config_dir.exists()
        True
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def get_bundled_agents_path() -> Path | None:
    """Get path to bundled agents.toml if it exists.

    Returns:
        Path to config/agents.toml in project root, or None if not found.
    """
    try:
        bundled_path = get_project_root() / "config" / "agents.toml"
        return bundled_path if bundled_path.exists() else None
    except FileNotFoundError:
        return None
