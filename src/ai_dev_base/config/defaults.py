"""Default values and constants for AI Dev Base configuration.

This module provides:
- VOLUME_CATEGORIES: Docker volume categorization for cleanup commands
- DEFAULT_AGENTS: Pre-configured agent definitions

All defaults can be imported without side effects.
"""

from __future__ import annotations

from typing import Final

from ai_dev_base.config.models import AgentConfig

# =============================================================================
# Volume Categories (for clean command)
# =============================================================================

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
"""Volume categories for selective cleanup.

Categories:
- credentials: AI agent auth tokens (claude, gemini, codex, opencode, gh)
- tools: Tool installations & configs (az, pulumi, tools-cache)
- cache: Package caches and IDE state (uv, vscode-server)
- data: Application data (opencode-data, vscode-workspaces)
"""


def get_all_volumes() -> list[str]:
    """Get flat list of all known volume names.

    Returns:
        List of all volume names across all categories.

    Example:
        >>> volumes = get_all_volumes()
        >>> len(volumes)
        12
        >>> "ai-dev-claude-config" in volumes
        True
    """
    return [vol for vols in VOLUME_CATEGORIES.values() for vol in vols]


# =============================================================================
# Default Agent Configurations
# =============================================================================

DEFAULT_AGENTS: Final[dict[str, AgentConfig]] = {
    "claude": AgentConfig(
        binary="claude",
        description="Anthropic Claude Code CLI",
        headless_flags=["-p"],
        read_only_flags=["--permission-mode", "plan"],
        write_flags=["--dangerously-skip-permissions"],
        json_flags=["--output-format", "json"],
        model_flag="--model",
        prompt_template='"$AGENT_PROMPT"',
    ),
    "gemini": AgentConfig(
        binary="gemini",
        description="Google Gemini CLI",
        headless_flags=["-p"],
        read_only_flags=[],
        write_flags=[],
        json_flags=["--output-format", "json"],
        model_flag="-m",
        prompt_template='"$AGENT_PROMPT"',
    ),
    "codex": AgentConfig(
        binary="codex",
        description="OpenAI Codex CLI",
        headless_flags=["exec"],
        read_only_flags=[],
        write_flags=["--full-auto"],
        json_flags=["--json"],
        model_flag="--model",
        prompt_template='"$AGENT_PROMPT"',
    ),
    "opencode": AgentConfig(
        binary="opencode",
        description="Anomaly OpenCode CLI",
        headless_flags=["run"],
        read_only_flags=["--agent", "plan"],
        write_flags=[],
        json_flags=["--format", "json"],
        model_flag="-m",
        prompt_template='"$AGENT_PROMPT"',
    ),
}
"""Default agent configurations matching config/agents.json.

Each agent is configured with:
- binary: The executable name
- description: Human-readable description
- headless_flags: Flags for non-interactive/headless mode
- read_only_flags: Flags for read-only/plan mode
- write_flags: Flags to enable file modifications
- json_flags: Flags for JSON output format
- model_flag: Flag for model selection (--model or -m)
- prompt_template: Shell template for prompt injection
"""
