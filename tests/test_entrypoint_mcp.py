"""Tests for the sourceable MCP registration script."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mcp-register.sh"


def run_register(tmp_path: Path, config: dict[str, object]) -> subprocess.CompletedProcess[str]:
    config_path = tmp_path / "mcp-servers.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    curl.chmod(0o755)

    jq = shutil.which("jq")
    assert jq is not None
    zsh = shutil.which("zsh")
    assert zsh is not None

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "CODEX_CONFIG": str(tmp_path / ".codex" / "config.toml"),
        "MCP_SERVERS_CONFIG": str(config_path),
        "PATH": f"{bin_dir}:{Path(jq).parent}:/usr/bin:/bin",
    }

    return subprocess.run(
        [
            zsh,
            "-c",
            f"set -euo pipefail; source {SCRIPT}; register_mcp_servers",
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def test_registers_each_canonical_server_individually(tmp_path: Path) -> None:
    result = run_register(
        tmp_path,
        {
            "docker-gateway": {
                "transport": "streamable-http",
                "url": "http://mcp-gateway:8811/mcp",
                "enabled": True,
            },
            "local-http": {
                "transport": "streamable-http",
                "url": "http://mcp.example:8847/mcp",
                "enabled": True,
                "tool_timeout_sec": 10,
            },
            "local-sse": {
                "transport": "sse",
                "url": "http://host.docker.internal:8765/sse",
                "enabled": False,
            },
        },
    )

    assert result.returncode == 0, result.stderr
    assert "✓ docker-gateway (streamable-http)" in result.stdout
    assert "✓ local-http (streamable-http)" in result.stdout
    assert "- local-sse (disabled)" in result.stdout
    assert (
        "Skipping invalid server name: local-sse\ndocker-gateway\nlocal-http"
        not in result.stdout
    )
    assert "Summary: 2 registered, 1 disabled, 0 skipped, 0 legacy" in result.stdout

    codex_config = tmp_path / ".codex" / "config.toml"
    content = codex_config.read_text(encoding="utf-8")
    assert '[mcp_servers.docker-gateway]' in content
    assert 'url = "http://mcp-gateway:8811/mcp"' in content
    assert "enabled = true" in content
    assert "[mcp_servers.local-http]" in content
    assert 'url = "http://mcp.example:8847/mcp"' in content
    assert "tool_timeout_sec = 10" in content
    assert "[mcp_servers.local-sse]" not in content


def test_legacy_type_schema_is_normalized_with_warning(tmp_path: Path) -> None:
    result = run_register(
        tmp_path,
        {
            "local-http": {
                "type": "http",
                "url": "http://mcp.example:8847/mcp",
            },
            "old-sse": {
                "type": "sse",
                "url": "http://host.docker.internal:8765/sse",
            },
        },
    )

    assert result.returncode == 0, result.stderr
    assert "legacy 'type' key detected" in result.stdout
    assert "✓ local-http (streamable-http)" in result.stdout
    assert "✓ old-sse (sse)" in result.stdout
    assert "Summary: 2 registered, 0 disabled, 0 skipped, 2 legacy" in result.stdout

    codex_config = tmp_path / ".codex" / "config.toml"
    content = codex_config.read_text(encoding="utf-8")
    assert "[mcp_servers.local-http]" in content
    assert "[mcp_servers.old-sse]" not in content


def test_codex_fallback_preserves_unrelated_config_and_replaces_section(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    codex_config = codex_dir / "config.toml"
    codex_config.write_text(
        """[features]
rmcp_client = true

[mcp_servers.local-http]
url = "http://old.example/mcp"

[projects."/home/dev/projects"]
trust_level = "trusted"
""",
        encoding="utf-8",
    )

    result = run_register(
        tmp_path,
        {
            "local-http": {
                "transport": "streamable-http",
                "url": "http://mcp.example:8847/mcp",
                "enabled": True,
            },
        },
    )

    assert result.returncode == 0, result.stderr
    content = codex_config.read_text(encoding="utf-8")
    assert content.count("[mcp_servers.local-http]") == 1
    assert 'url = "http://old.example/mcp"' not in content
    assert 'url = "http://mcp.example:8847/mcp"' in content
    assert '[projects."/home/dev/projects"]' in content
    assert 'trust_level = "trusted"' in content


def test_invalid_timeout_is_skipped(tmp_path: Path) -> None:
    result = run_register(
        tmp_path,
        {
            "local-http": {
                "transport": "streamable-http",
                "url": "http://mcp.example:8847/mcp",
                "enabled": True,
                "tool_timeout_sec": "soon",
            },
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Skipping invalid tool_timeout_sec for local-http: soon" in result.stdout
    assert "Summary: 0 registered, 0 disabled, 1 skipped, 0 legacy" in result.stdout
