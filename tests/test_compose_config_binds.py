"""Compose-level coverage for how the selected workflow sources are mounted."""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any, cast

import pytest
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


def _compose(path: Path | None = None) -> dict[str, Any]:
    return yaml.safe_load((path or _ROOT / "docker-compose.yml").read_text())


def _binds(compose: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """Every bind as (source, target, read_only), from short and long volume syntax."""
    entries: list[object] = list(compose.get("x-common-volumes") or [])
    services = cast(dict[str, dict[str, Any] | None], compose.get("services") or {})
    for service in services.values():
        entries.extend((service or {}).get("volumes") or [])
    binds: list[tuple[str, str, bool]] = []
    for entry in entries:
        if isinstance(entry, str):
            source, target, *mode = entry.split(":")
            binds.append((source, target, mode == ["ro"]))
        elif isinstance(entry, dict):
            mount = cast(dict[str, object], entry)
            if mount.get("type") == "bind":
                binds.append(
                    (str(mount["source"]), str(mount["target"]), bool(mount.get("read_only")))
                )
    return binds


def _writable_config_binds(compose: dict[str, Any]) -> list[str]:
    return [
        f"{source}:{target}"
        for raw_source, target, read_only in _binds(compose)
        for source in ("./" + posixpath.normpath(raw_source),)
        if (source == "./config" or source.startswith("./config/"))
        and not read_only
        and f"{source}:{target}" not in _ALLOWED_RW
    ]


def test_config_binds_are_read_only_except_the_default_source() -> None:
    paths = sorted(_ROOT.glob("docker-compose*.yml"))
    assert paths
    checked = []
    for path in paths:
        compose = _compose(path)
        checked.extend(
            source for source, _, read_only in _binds(compose)
            if posixpath.normpath(source).split("/")[0] == "config" and read_only
        )
        assert _writable_config_binds(compose) == [], path.name
    assert checked


@pytest.mark.parametrize("source", ["./config", "config", "./config/", "config/",
                                    "config/opencode", "./config/opencode/"])
def test_writable_config_source_spellings_are_refused(source: str) -> None:
    compose = {"services": {"dev": {"volumes": [f"{source}:/workflow"]}}}
    assert _writable_config_binds(compose)


def test_every_compose_overlay_is_audited(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(f"{__name__}._ROOT", tmp_path)
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  dev:\n    volumes: [./config/opencode:/workflow:ro]\n"
    )
    (tmp_path / "docker-compose.extra.yml").write_text(
        "services:\n  dev:\n    volumes: [config/codex/:/workflow]\n"
    )
    with pytest.raises(AssertionError, match="docker-compose.extra.yml"):
        test_config_binds_are_read_only_except_the_default_source()


def test_long_syntax_writable_config_bind_is_refused() -> None:
    compose = _compose()
    compose["services"]["dev"]["volumes"] = [
        *compose["services"]["dev"]["volumes"],
        {"type": "bind", "source": "./config/codex", "target": "/home/dev/.codex_seed"},
    ]

    assert _writable_config_binds(compose) == ["./config/codex:/home/dev/.codex_seed"]


def test_allowed_rw_binds_exist_verbatim() -> None:
    entries = {
        f"{source}:{target}" for source, target, read_only in _binds(_compose()) if not read_only
    }
    for entry in sorted(_ALLOWED_RW):
        assert entry in entries


def test_opencode_seed_view_is_read_only() -> None:
    assert ("./config/opencode", "/home/dev/.opencode/seed", True) in _binds(_compose())
