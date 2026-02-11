"""Configuration models and utilities for AI Dev Base CLI."""

from ai_dev_base.config.defaults import (
    DEFAULT_AGENTS,
    VOLUME_CATEGORIES,
    get_all_volumes,
)
from ai_dev_base.config.loader import (
    ConfigNotFoundError,
    ConfigValidationError,
    ensure_config_dir,
    get_bundled_agents_path,
    load_agents,
    load_config,
    save_config,
)
from ai_dev_base.config.models import (
    AgentConfig,
    AgentsConfig,
    AppConfig,
    ResourceLimits,
    ShellConfig,
)

__all__ = [
    # Models
    "AgentConfig",
    "AgentsConfig",
    "AppConfig",
    "ResourceLimits",
    "ShellConfig",
    # Defaults
    "DEFAULT_AGENTS",
    "VOLUME_CATEGORIES",
    "get_all_volumes",
    # Loader
    "ConfigNotFoundError",
    "ConfigValidationError",
    "ensure_config_dir",
    "get_bundled_agents_path",
    "load_agents",
    "load_config",
    "save_config",
]
