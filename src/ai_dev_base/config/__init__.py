"""Configuration models and utilities for AI Dev Base CLI.

This module provides type-safe Pydantic models for configuration management,
including agent definitions, resource limits, and application settings.
"""

from ai_dev_base.config.defaults import (
    DEFAULT_AGENTS,
    DEFAULT_RESOURCES,
    VOLUME_CATEGORIES,
    get_all_volumes,
    get_volumes_by_category,
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
    "DEFAULT_RESOURCES",
    "VOLUME_CATEGORIES",
    "get_all_volumes",
    "get_volumes_by_category",
]
