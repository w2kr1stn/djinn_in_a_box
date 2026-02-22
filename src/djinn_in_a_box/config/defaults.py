"""Default values and constants for Djinn in a Box configuration."""

from __future__ import annotations

from typing import Final

from djinn_in_a_box.config.models import AgentConfig

VOLUME_CATEGORIES: Final[dict[str, list[str]]] = {
    "credentials": [
        "djinn-claude-config",
        "djinn-gemini-config",
        "djinn-codex-config",
        "djinn-opencode-config",
        "djinn-gh-config",
    ],
    "tools": [
        "djinn-azure-config",
        "djinn-pulumi-config",
        "djinn-tools-cache",
    ],
    "cache": [
        "djinn-uv-cache",
        "djinn-vscode-server",
    ],
    "data": [
        "djinn-opencode-data",
        "djinn-vscode-workspaces",
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
