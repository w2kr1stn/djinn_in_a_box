"""Configuration models and utilities for AI Dev Base CLI.

This module provides type-safe Pydantic models for configuration management,
including agent definitions, resource limits, and application settings.
"""

from ai_dev_base.config.models import (
    AgentConfig,
    AgentsConfig,
    AppConfig,
    ResourceLimits,
    ShellConfig,
)

__all__ = [
    "AgentConfig",
    "AgentsConfig",
    "AppConfig",
    "ResourceLimits",
    "ShellConfig",
]
