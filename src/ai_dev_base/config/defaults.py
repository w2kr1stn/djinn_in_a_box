"""Default values and constants for AI Dev Base configuration."""

from __future__ import annotations

from typing import Final

from ai_dev_base.config.models import AgentConfig

VOLUME_CATEGORIES: Final[dict[str, list[str]]] = {
    "credentials": [
        "ai-dev-claude-config",
        "ai-dev-gemini-config",
        "ai-dev-codex-config",
        "ai-dev-opencode-config",
        "ai-dev-gh-config",
    ],
    "tools": [
        "ai-dev-azure-config",
        "ai-dev-pulumi-config",
        "ai-dev-tools-cache",
    ],
    "cache": [
        "ai-dev-uv-cache",
        "ai-dev-vscode-server",
    ],
    "data": [
        "ai-dev-opencode-data",
        "ai-dev-vscode-workspaces",
    ],
}
"""Volume categories for selective cleanup."""


DEFAULT_AGENTS: Final[dict[str, AgentConfig]] = {
    "claude": AgentConfig(
        binary="claude",
        description="Anthropic Claude Code CLI",
        headless_flags=["-p"],
        read_only_flags=["--permission-mode", "plan"],
        write_flags=["--dangerously-skip-permissions"],
        json_flags=["--output-format", "json"],
        model_flag="--model",
    ),
    "gemini": AgentConfig(
        binary="gemini",
        description="Google Gemini CLI",
        headless_flags=["-p"],
        json_flags=["--output-format", "json"],
        model_flag="-m",
    ),
    "codex": AgentConfig(
        binary="codex",
        description="OpenAI Codex CLI",
        headless_flags=["exec"],
        write_flags=["--full-auto"],
        json_flags=["--json"],
        model_flag="--model",
    ),
    "opencode": AgentConfig(
        binary="opencode",
        description="Anomaly OpenCode CLI",
        headless_flags=["run"],
        read_only_flags=["--agent", "plan"],
        json_flags=["--format", "json"],
        model_flag="-m",
    ),
}
"""Default agent configurations (used when no user agents.toml exists)."""
