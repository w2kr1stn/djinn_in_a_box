"""Configuration loading and migration for AI Dev Base.

Provides TOML-based configuration loading with:
- Automatic validation via Pydantic models
- Fallback to bundled defaults for agents
- Migration from legacy .env format
"""

from __future__ import annotations

import json
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
            f"Configuration not found: {path}\n"
            "Run 'codeagent init' to create configuration."
        )


class ConfigValidationError(ValueError):
    """Raised when config validation fails."""

    def __init__(
        self, message: str, errors: list[dict[str, Any]] | None = None
    ) -> None:
        self.errors: list[dict[str, Any]] = list(errors) if errors else []
        super().__init__(message)


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
        raise ConfigValidationError(
            f"Invalid TOML syntax in {config_path}: {e}"
        ) from e

    # Transform nested TOML structure to flat Pydantic model
    # [general] -> top-level, [shell] -> shell, [resources] -> resources
    try:
        config_dict = _transform_toml_to_config(data)
        return AppConfig(**config_dict)
    except ValidationError as e:
        raise ConfigValidationError(
            f"Configuration validation failed for {config_path}:\n"
            f"{_format_validation_errors(e)}",
            errors=[dict(err) for err in e.errors()],
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

    # Extract [general] section to top-level
    general = data.get("general", {})
    if "code_dir" in general:
        result["code_dir"] = general["code_dir"]
    if "timezone" in general:
        result["timezone"] = general["timezone"]

    # Pass through nested sections
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
        raise ConfigValidationError(
            f"Invalid TOML syntax in {path}: {e}"
        ) from e

    agents_data = data.get("agents", {})
    if not agents_data:
        raise ConfigValidationError(
            f"No agents defined in {path}. "
            "Expected [agents.<name>] sections."
        )

    try:
        agents_config = AgentsConfig(agents={
            name: AgentConfig(**agent_data)
            for name, agent_data in agents_data.items()
        })
        return agents_config.agents
    except ValidationError as e:
        raise ConfigValidationError(
            f"Invalid agent configuration in {path}:\n"
            f"{_format_validation_errors(e)}",
            errors=[dict(err) for err in e.errors()],
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

    with open(config_path, "wb") as f:
        tomli_w.dump(toml_data, f)


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


def save_agents(agents: dict[str, AgentConfig], path: Path | None = None) -> None:
    """Save agent configurations to TOML file.

    Args:
        agents: Dict mapping agent names to AgentConfig.
        path: Target path. Defaults to AGENTS_FILE.

    Example:
        >>> from ai_dev_base.config.defaults import DEFAULT_AGENTS
        >>> save_agents(DEFAULT_AGENTS)  # Saves to ~/.config/ai-dev-base/agents.toml
    """
    agents_path = path or AGENTS_FILE

    # Ensure parent directory exists
    agents_path.parent.mkdir(parents=True, exist_ok=True)

    # Transform to TOML structure
    toml_data = _transform_agents_to_toml(agents)

    with open(agents_path, "wb") as f:
        tomli_w.dump(toml_data, f)


def _transform_agents_to_toml(agents: dict[str, AgentConfig]) -> dict[str, Any]:
    """Transform agent dict to TOML-compatible structure."""
    return {
        "agents": {
            name: {
                "binary": agent.binary,
                "description": agent.description,
                "headless_flags": agent.headless_flags,
                "read_only_flags": agent.read_only_flags,
                "write_flags": agent.write_flags,
                "json_flags": agent.json_flags,
                "model_flag": agent.model_flag,
                "prompt_template": agent.prompt_template,
            }
            for name, agent in agents.items()
        }
    }


# =============================================================================
# Migration from Legacy .env Format
# =============================================================================


def migrate_from_env(
    env_path: Path,
    agents_json_path: Path | None = None,
) -> tuple[AppConfig, dict[str, AgentConfig]]:
    """Migrate configuration from legacy .env file format.

    Reads settings from a .env file and optionally an agents.json file,
    converting them to the new TOML-based configuration format.

    Environment variable mapping:
        CODE_DIR -> general.code_dir
        TZ -> general.timezone
        SKIP_SHELL_MOUNTS -> shell.skip_mounts
        OMP_THEME_PATH -> shell.omp_theme_path
        CPU_LIMIT -> resources.cpu_limit
        MEMORY_LIMIT -> resources.memory_limit
        CPU_RESERVATION -> resources.cpu_reservation
        MEMORY_RESERVATION -> resources.memory_reservation

    Args:
        env_path: Path to .env file.
        agents_json_path: Optional path to agents.json file.

    Returns:
        Tuple of (AppConfig, dict of AgentConfig).

    Raises:
        ConfigNotFoundError: If .env file does not exist.
        ConfigValidationError: If migration fails validation.

    Example:
        >>> config, agents = migrate_from_env(Path(".env"))
        >>> config.code_dir
        PosixPath('/home/user/projects')
    """
    if not env_path.exists():
        raise ConfigNotFoundError(env_path)

    # Parse .env file
    env_values = _parse_env_file(env_path)

    # Build config dict from env values
    config_dict = _build_config_from_env(env_values)

    # Validate and create AppConfig
    try:
        config = AppConfig(**config_dict)
    except ValidationError as e:
        raise ConfigValidationError(
            f"Migration validation failed:\n{_format_validation_errors(e)}",
            errors=[dict(err) for err in e.errors()],
        ) from e

    # Load agents from JSON if provided, otherwise use defaults
    if agents_json_path is not None and agents_json_path.exists():
        agents = _load_agents_from_json(agents_json_path)
    else:
        agents = dict(DEFAULT_AGENTS)

    return config, agents


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dictionary.

    Handles:
    - Comments (lines starting with #)
    - Empty lines
    - KEY=VALUE format
    - Quoted values (single or double)
    - Values with = in them

    Args:
        path: Path to .env file.

    Returns:
        Dict of environment variable names to values.
    """
    result: dict[str, str] = {}
    content = path.read_text()

    for line in content.splitlines():
        line = line.strip()

        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue

        # Parse KEY=VALUE
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        # Remove surrounding quotes
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]

        result[key] = value

    return result


def _build_config_from_env(env: dict[str, str]) -> dict[str, Any]:
    """Build AppConfig-compatible dict from parsed .env values."""
    result: dict[str, Any] = {}

    # Required: code_dir
    if "CODE_DIR" in env:
        result["code_dir"] = env["CODE_DIR"]
    else:
        raise ConfigValidationError(
            "Required setting 'CODE_DIR' not found in .env file.\n"
            "Add: CODE_DIR=/path/to/your/projects"
        )

    # Optional: timezone
    if "TZ" in env:
        result["timezone"] = env["TZ"]

    # Optional: shell config
    shell_config: dict[str, Any] = {}
    if "SKIP_SHELL_MOUNTS" in env:
        # Parse boolean from string
        shell_config["skip_mounts"] = _parse_bool(env["SKIP_SHELL_MOUNTS"])
    if "OMP_THEME_PATH" in env:
        shell_config["omp_theme_path"] = env["OMP_THEME_PATH"]
    if shell_config:
        result["shell"] = shell_config

    # Optional: resource limits
    resources_config: dict[str, Any] = {}
    if "CPU_LIMIT" in env:
        try:
            resources_config["cpu_limit"] = int(env["CPU_LIMIT"])
        except ValueError as e:
            raise ConfigValidationError(
                f"Invalid CPU_LIMIT value '{env['CPU_LIMIT']}': must be an integer"
            ) from e
    if "MEMORY_LIMIT" in env:
        resources_config["memory_limit"] = env["MEMORY_LIMIT"]
    if "CPU_RESERVATION" in env:
        try:
            resources_config["cpu_reservation"] = int(env["CPU_RESERVATION"])
        except ValueError as e:
            raise ConfigValidationError(
                f"Invalid CPU_RESERVATION value '{env['CPU_RESERVATION']}': must be an integer"
            ) from e
    if "MEMORY_RESERVATION" in env:
        resources_config["memory_reservation"] = env["MEMORY_RESERVATION"]
    if resources_config:
        result["resources"] = resources_config

    return result


def _parse_bool(value: str) -> bool:
    """Parse a boolean from string representation."""
    return value.lower() in ("true", "1", "yes", "on")


def _load_agents_from_json(path: Path) -> dict[str, AgentConfig]:
    """Load agents from legacy agents.json format.

    Args:
        path: Path to agents.json file.

    Returns:
        Dict mapping agent names to AgentConfig.

    Raises:
        ConfigValidationError: If JSON is invalid or agents malformed.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigValidationError(
            f"Invalid JSON syntax in {path}: {e}"
        ) from e

    agents_data = data.get("agents", {})
    if not agents_data:
        raise ConfigValidationError(
            f"No agents defined in {path}. "
            "Expected 'agents' object."
        )

    result: dict[str, AgentConfig] = {}
    for name, agent_data in agents_data.items():
        # Filter out JSON-schema specific fields
        agent_dict = {
            k: v for k, v in agent_data.items()
            if k in AgentConfig.model_fields
        }
        try:
            result[name] = AgentConfig(**agent_dict)
        except ValidationError as e:
            raise ConfigValidationError(
                f"Invalid agent '{name}' in {path}:\n"
                f"{_format_validation_errors(e)}",
                errors=[dict(err) for err in e.errors()],
            ) from e

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


def config_exists() -> bool:
    """Check if main config file exists.

    Returns:
        True if ~/.config/ai-dev-base/config.toml exists.
    """
    return CONFIG_FILE.exists()


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
