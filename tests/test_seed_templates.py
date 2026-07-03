from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from djinn_in_a_box.core.seeding import SEED_MANIFEST

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED_ROOT = PROJECT_ROOT / "templates" / "seed"
PRIVATE_TOKENS_PATH = PROJECT_ROOT / "config" / "private-tokens.txt"


def _seed_files() -> list[Path]:
    return sorted(path for path in SEED_ROOT.rglob("*") if path.is_file())


def _private_tokens() -> list[str]:
    if not PRIVATE_TOKENS_PATH.exists():
        pytest.skip("private token list not present; skipping local-only leak gate")

    tokens: list[str] = []
    for line in PRIVATE_TOKENS_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            tokens.append(stripped.lower())
    return tokens


def test_seed_templates_contain_no_personal_tokens() -> None:
    forbidden = _private_tokens()
    offenders: list[str] = []
    for path in _seed_files():
        text = path.read_text(encoding="utf-8").lower()
        relative = path.relative_to(SEED_ROOT)
        for token in forbidden:
            if token in text:
                offenders.append(f"{relative}: {token}")

    assert offenders == []


def test_seed_templates_contain_no_generic_identity_leaks() -> None:
    patterns = {
        "email address": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
        "non-container home path": re.compile(r"/home/(?!dev\b)[A-Za-z0-9._-]+"),
        "ipv4 address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    }
    offenders: list[str] = []
    for path in _seed_files():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(SEED_ROOT)
        for label, pattern in patterns.items():
            if pattern.search(text):
                offenders.append(f"{relative}: {label}")

    assert offenders == []


def test_claude_settings_json_is_minimal_and_safe() -> None:
    settings_path = SEED_ROOT / "config" / "claude" / "settings.json"
    raw = settings_path.read_text(encoding="utf-8")
    parsed: object = json.loads(raw)

    assert isinstance(parsed, dict)
    for key in ("bypassPermissions", "hooks", "statusLine", "defaultMode"):
        assert key not in raw


def test_mcp_servers_json_is_empty_registry() -> None:
    registry: object = json.loads(
        (SEED_ROOT / "config" / "mcp-servers.json").read_text(encoding="utf-8")
    )
    assert registry == {}


def test_manifest_sources_exist_in_seed_templates() -> None:
    for entry in SEED_MANIFEST:
        source = SEED_ROOT / entry.source
        if entry.kind == "file":
            assert source.is_file(), f"missing seed file: {entry.source}"
        else:
            assert source.is_dir(), f"missing seed directory: {entry.source}"


def test_seed_claude_references_no_unshipped_surfaces() -> None:
    text = (SEED_ROOT / "config" / "claude" / "CLAUDE.md").read_text(encoding="utf-8")
    lowered = text.lower()

    for token in ("skills/", "commands/", "agents/", "scripts/", "hook"):
        assert token not in lowered
    assert re.search(r"\b[\w.-]+\.(?:py|sh)\b", text) is None
