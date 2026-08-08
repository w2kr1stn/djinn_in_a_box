"""Default values and constants for Djinn in a Box configuration."""

from __future__ import annotations

from typing import Final

from djinn_in_a_box.config.models import AgentConfig

VOLUME_CATEGORIES: Final[dict[str, list[str]]] = {
    "cache": [
        "djinn-uv-cache",
        "djinn-tools-cache",
        "djinn-vscode-server",
    ],
    "data": [
        "djinn-opencode-data",
        "djinn-vscode-workspaces",
    ],
}
"""Named-volume categories (host-local, not synced across machines)."""


SYNC_PATHS: Final[dict[str, list[str]]] = {
    "credentials": [
        "claude",
        "gemini",
        "codex",
        "opencode",
        "gh",
        # "age" persists age encryption identities at ~/.config/age (age -i);
        # SOPS users point SOPS_AGE_KEY_FILE here. A key store, not a coding CLI.
        "age",
    ],
    "repo-dotfiles": [
        "repo-dotfiles",
    ],
}
"""Bind-mount subdirectories under ${DJINN_CONFIG_ROOT} (optionally mirrored across
machines by the user)."""


DEFAULT_ZONES: Final[dict[str, dict[str, list[str]]]] = {
    "claude": {
        "local": [
            "jobs",
            "cache",
            "file-history",
            "ide",
            "paste-cache",
            "session-env",
            "shell-snapshots",
            "tasks",
            "telemetry",
            "work",
            "sessions",
            "daemon",
            "plugins/marketplaces",
            "plugins/cache",
        ],
        "shared": ["projects", "transcripts"],
    },
    "codex": {
        "local": [
            ".tmp",
            "tmp",
            "cache",
            "log",
            "mcp-oauth-locks",
            "shell_snapshots",
            "plugins/cache",
        ],
        "shared": ["sessions"],
    },
    "opencode": {
        "local": ["node_modules", "native"],
        "shared": [],
    },
    "gemini": {
        "local": ["tmp"],
        "shared": ["history"],
    },
    "gh": {
        "local": [],
        "shared": [],
    },
    "age": {
        "local": [],
        "shared": [],
    },
}


KNOWN_CONFIG_ROOT_ENTRIES: Final[dict[str, frozenset[str]]] = {
    "claude": frozenset(
        {
            ".credentials.json",
            "claude.json",
            "settings.json",
            "secrets",
            "plugins",
            "agents",
            "commands",
            "context",
            "scripts",
            "skills",
            "CLAUDE.md",
            "AGENTS.md",
            "backups",
            "history.jsonl",
            "bin-shellcheck",
            "daemon.log",
            "daemon.lock",
            "daemon.status.json",
            "stats-cache.json",
            "gh-pr-status-cache.json",
            ".last-cleanup",
            ".last-update-result.json",
            "plugin-catalog-cache.json",
        }
    ),
    "codex": frozenset(
        {
            "auth.json",
            "config.toml",
            "config.example.toml",
            "hooks.json",
            "hooks",
            "rules",
            "memories",
            "version.json",
            ".djinn-workflow-state.json",
            ".personality_migration",
            ".sandbox_migration",
            "agents",
            "context",
            "scripts",
            "skills",
            "CLAUDE.md",
            "AGENTS.md",
            "backups",
            "logs_2.sqlite",
            "state_5.sqlite",
            "goals_1.sqlite",
            "memories_1.sqlite",
            "models_cache.json",
            "history.jsonl",
            "session_index.jsonl",
            "installation_id",
        }
    ),
    "opencode": frozenset(
        {
            "auth.json",
            "mcp-auth.json",
            ".opencode.json",
            "package.json",
            "package-lock.json",
            "seed",
            ".gitignore",
        }
    ),
    "gemini": frozenset({"settings.json", "installation_id"}),
    "gh": frozenset({"hosts.yml"}),
    "age": frozenset({"keys.txt"}),
}
"""Known top-level agent configuration entries retained in the config zone."""


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
        read_only_flags=["--sandbox", "read-only"],
        write_flags=["--full-auto"],
        json_flags=["--json"],
        model_flag="--model",
    ),
    "opencode": AgentConfig(
        binary="opencode",
        description="OpenCode CLI",
        headless_flags=["run"],
        read_only_flags=["--agent", "plan"],
        json_flags=["--format", "json"],
        model_flag="-m",
    ),
}
"""Default agent configurations (used when no user agents.toml exists)."""
