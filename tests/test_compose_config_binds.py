"""Compose-level coverage for how the selected workflow sources are mounted."""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]

# Default workflow source (config/claude); other sources are a follow-up after issue #165.
_ALLOWED_RW = frozenset(
    {
        "./config/claude:/home/dev/.claude_seed",
        "./config/claude/skills:/home/dev/.claude/skills",
        "./config/claude/commands:/home/dev/.claude/commands",
        "./config/claude/agents:/home/dev/.claude/agents",
        "./config/claude/context:/home/dev/.claude/context",
        "./config/claude/scripts:/home/dev/.claude/scripts",
        "./config/claude/AGENTS.md:/home/dev/.claude/AGENTS.md",
    }
)


def _string_volumes() -> list[str]:
    compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
    entries = list(compose.get("x-common-volumes") or [])
    for service in (compose.get("services") or {}).values():
        entries.extend((service or {}).get("volumes") or [])
    return [entry for entry in entries if isinstance(entry, str)]


def test_config_binds_are_read_only_except_the_default_source() -> None:
    checked = 0
    for entry in _string_volumes():
        source = entry.split(":", 1)[0]
        if source != "./config" and not source.startswith("./config/"):
            continue
        if entry in _ALLOWED_RW:
            continue
        checked += 1
        assert entry.endswith(":ro"), entry
    assert checked >= 1


def test_allowed_rw_binds_exist_verbatim() -> None:
    entries = _string_volumes()
    for entry in sorted(_ALLOWED_RW):
        assert entry in entries


def test_opencode_seed_view_is_read_only() -> None:
    assert "./config/opencode:/home/dev/.opencode/seed:ro" in _string_volumes()
