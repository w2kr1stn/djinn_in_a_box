"""Tests for the sourceable MCP registration script."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mcp-register.sh"
RAW_UI_MARKERS = ("✓", "⚠", "✗", "ℹ", "↻", "⊕", "✕")


def assert_plain_startup_output(output: str) -> None:
    assert "\x1b[" not in output
    for marker in RAW_UI_MARKERS:
        assert marker not in output


def run_register(
    tmp_path: Path,
    config: dict[str, object],
    env_updates: dict[str, str] | None = None,
    bin_scripts: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    config_path = tmp_path / "mcp-servers.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    curl.chmod(0o755)
    if bin_scripts is not None:
        for name, content in bin_scripts.items():
            script = bin_dir / name
            script.write_text(content, encoding="utf-8")
            script.chmod(0o755)

    jq = shutil.which("jq")
    assert jq is not None
    zsh = shutil.which("zsh")
    assert zsh is not None

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "CODEX_CONFIG": str(tmp_path / ".codex" / "config.toml"),
        "MCP_SERVERS_CONFIG": str(config_path),
        "NO_COLOR": "1",
        "WORKFLOW_PUBLISHER": str(ROOT / "src/djinn_in_a_box/core/workflow_publisher.py"),
        "PATH": f"{bin_dir}:{Path(jq).parent}:/usr/bin:/bin",
    }
    env.pop("DJINN_FORCE_UI_COLOR", None)
    if env_updates is not None:
        env.update(env_updates)

    (tmp_path / ".config/opencode").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            zsh,
            "-c",
            "set -euo pipefail; "
            f"source {shlex.quote(str(SCRIPT))}; register_mcp_servers",
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
            "remote-http": {
                "transport": "streamable-http",
                "url": "http://remote.example:8847/mcp",
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
    assert result.stdout == ""
    assert_plain_startup_output(result.stderr)
    assert "[ok] remote-http (streamable-http)" in result.stderr
    assert "[ok] local-http (streamable-http)" in result.stderr
    assert "[off] local-sse (disabled)" in result.stderr
    assert (
        "Skipping invalid server name: local-sse\nremote-http\nlocal-http"
        not in result.stderr
    )
    assert "Summary: 2 registered, 1 disabled, 0 skipped" in result.stderr

    codex_config = tmp_path / ".codex" / "config.toml"
    content = codex_config.read_text(encoding="utf-8")
    assert '[mcp_servers.remote-http]' in content
    assert 'url = "http://remote.example:8847/mcp"' in content
    assert "enabled = true" in content
    assert "[mcp_servers.local-http]" in content
    assert 'url = "http://mcp.example:8847/mcp"' in content
    assert "tool_timeout_sec = 10" in content
    assert "[mcp_servers.local-sse]" not in content


def test_codex_registration_preserves_unrelated_config_and_replaces_section(tmp_path: Path) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    codex_config = codex_dir / "config.toml"
    codex_config.write_text(
        """[mcp_servers.local-http]
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
    assert result.stdout == ""
    assert_plain_startup_output(result.stderr)
    content = codex_config.read_text(encoding="utf-8")
    assert content.count("[mcp_servers.local-http]") == 1
    assert 'url = "http://old.example/mcp"' not in content
    assert 'url = "http://mcp.example:8847/mcp"' in content
    assert '[projects."/home/dev/projects"]' in content
    assert 'trust_level = "trusted"' in content


def test_registry_skips_an_entry_without_transport(tmp_path: Path) -> None:
    result = run_register(
        tmp_path,
        {
            "missing-transport": {
                "type": "http",
                "url": "http://mcp.example/mcp",
                "enabled": True,
            },
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Skipping invalid transport for missing-transport" in result.stderr
    assert "Summary: 0 registered, 0 disabled, 1 skipped" in result.stderr
    assert not (tmp_path / ".config/opencode/opencode.json").exists()
    assert not (tmp_path / ".codex/config.toml").exists()


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
    assert result.stdout == ""
    assert_plain_startup_output(result.stderr)
    assert "Skipping invalid tool_timeout_sec for local-http: soon" in result.stderr
    assert "Summary: 0 registered, 0 disabled, 1 skipped" in result.stderr


def test_claude_mcp_output_is_boxed_without_changing_status_flow(tmp_path: Path) -> None:
    result = run_register(
        tmp_path,
        {
            "local-http": {
                "transport": "streamable-http",
                "url": "http://mcp.example:8847/mcp",
                "enabled": True,
            },
        },
        bin_scripts={
            "claude": """#!/bin/sh
if [ "$1" = "mcp" ] && [ "$2" = "remove" ]; then
  printf 'Removed MCP server %s\\n' "$5"
  printf 'File modified: %s/.claude.json\\n' "$HOME" >&2
  exit 17
fi
if [ "$1" = "mcp" ] && [ "$2" = "add" ]; then
  printf 'Added HTTP MCP server %s with URL: %s\\n' "$7" "$8"
  exit 19
fi
exit 64
""",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert_plain_startup_output(result.stderr)
    assert "  | Removed MCP server local-http" in result.stderr
    assert f"  | File modified: {tmp_path}/.claude.json" in result.stderr
    assert (
        "  | Added HTTP MCP server local-http with URL: "
        "http://mcp.example:8847/mcp"
    ) in result.stderr
    assert "\nRemoved MCP server local-http" not in result.stderr
    assert "\nFile modified:" not in result.stderr
    assert "\nAdded HTTP MCP server" not in result.stderr
    assert "[ok] local-http (streamable-http)" in result.stderr
    assert "Summary: 1 registered, 0 disabled, 0 skipped" in result.stderr


def test_plain_fallback_when_output_lib_is_absent(tmp_path: Path) -> None:
    result = run_register(
        tmp_path,
        {
            "local-http": {
                "transport": "streamable-http",
                "url": "http://mcp.example:8847/mcp",
                "enabled": True,
            },
        },
        {"OUTPUT_LIB": str(tmp_path / "missing-output-lib.sh")},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert_plain_startup_output(result.stderr)
    assert "[warn] output library not found" in result.stderr
    assert "[ok] local-http (streamable-http)" in result.stderr


def test_opencode_mcp_uses_v2_carrier_and_preserves_provider(tmp_path: Path) -> None:
    root = tmp_path / ".config/opencode"
    root.mkdir(parents=True)
    config = root / "opencode.json"
    personal = {"providers": {"local": {"settings": {"baseURL": "http://model.example/v1"}}}}
    carrier = tmp_path / "carrier"
    carrier.mkdir()
    (carrier / "opencode.json").write_text(json.dumps({**personal, "mcp": {"servers": {
        "off": {"type": "remote", "url": "http://off.example/mcp"},
        "sse": {"type": "remote", "url": "http://old.example/mcp"},
        "personal": {"type": "remote", "url": "http://personal.example/mcp"},
    }}}))
    copied = subprocess.run(
        ["python3", str(ROOT / "src/djinn_in_a_box/core/workflow_publisher.py"),
         "personal-carrier", "copy", "--carrier", str(carrier), "--runtime", str(root)],
        capture_output=True, text=True, check=False,
    )
    assert copied.returncode == 0, copied.stderr
    result = run_register(tmp_path, {
        "remote": {"transport": "streamable-http", "url": "http://mcp.example/mcp"},
        "off": {"transport": "streamable-http", "url": "http://off.example/mcp", "enabled": False},
        "sse": {"transport": "sse", "url": "http://sse.example/sse"},
    })
    assert result.returncode == 0, result.stderr
    output = json.loads(config.read_text())
    assert output["providers"] == personal["providers"]
    assert output["mcp"]["servers"] == {
        "remote": {"type": "remote", "url": "http://mcp.example/mcp"},
        "personal": {"type": "remote", "url": "http://personal.example/mcp"},
    }
    assert "OpenCode does not support sse, skipped" in result.stderr
    assert not (root / ".opencode.json").exists()


@pytest.mark.parametrize(
    "transport,enabled", (("streamable-http", True), ("streamable-http", False), ("sse", True))
)
def test_opencode_mcp_never_writes_through_fixed_temp_link(tmp_path, transport, enabled):
    root = tmp_path / ".config/opencode"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"sentinel")
    (root / "opencode.json.tmp").symlink_to(outside)
    config = root / "opencode.json"
    config.write_text('{"provider":{"local":{}},"mcp":{"servers":{"fixture":{"old":true}}}}')
    result = run_register(
        tmp_path,
        {"fixture": {"url": "https://mcp.example/mcp", "transport": transport, "enabled": enabled}},
    )
    assert result.returncode == 0, result.stderr
    assert outside.read_bytes() == b"sentinel"
    assert (root / "opencode.json.tmp").is_symlink()
    assert not config.is_symlink()
    assert config.stat().st_mode & 0o777 == 0o600
    data = json.loads(config.read_bytes())
    assert data["provider"] == {"local": {}}
    if transport == "streamable-http" and enabled:
        assert data["mcp"]["servers"]["fixture"] == {
            "type": "remote",
            "url": "https://mcp.example/mcp",
        }
    else:
        assert "fixture" not in data["mcp"]["servers"]


def test_missing_main_mcp_uses_empty_object_and_private_install(tmp_path):
    result = run_register(
        tmp_path, {"fixture": {"url": "https://mcp.example/mcp", "transport": "streamable-http"}}
    )
    assert result.returncode == 0, result.stderr
    path = tmp_path / ".config/opencode/opencode.json"
    assert json.loads(path.read_bytes()) == {
        "mcp": {"servers": {"fixture": {"type": "remote", "url": "https://mcp.example/mcp"}}}
    }
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("fault", ("capture", "install"))
def test_opencode_mcp_failed_write_warns_and_preserves_main(tmp_path, fault):
    root = tmp_path / ".config/opencode"
    root.mkdir(parents=True)
    main = root / "opencode.json"
    original = b'{"provider":{"local":{}}}'
    main.write_bytes(original)
    env_updates = {}
    bin_scripts = {}
    if fault == "capture":
        bin_scripts["mktemp"] = "#!/bin/sh\nexit 1\n"
    else:
        helper = tmp_path / "refuse.py"
        helper.write_text("raise SystemExit(1)\n")
        env_updates["WORKFLOW_PUBLISHER"] = str(helper)
    result = run_register(
        tmp_path,
        {"fixture": {"transport": "streamable-http", "url": "https://mcp.example/mcp"}},
        env_updates=env_updates,
        bin_scripts=bin_scripts,
    )
    assert result.returncode == 0, result.stderr
    assert main.read_bytes() == original
    assert "OpenCode: could not" in result.stderr
    assert list(root.iterdir()) == [main]


@pytest.mark.parametrize("original", (b"", b" \t\n"), ids=("empty", "whitespace"))
def test_opencode_mcp_empty_json_warns_and_preserves_main(tmp_path: Path, original: bytes) -> None:
    main = tmp_path / ".config/opencode/opencode.json"
    main.parent.mkdir(parents=True)
    main.write_bytes(original)
    result = run_register(
        tmp_path,
        {"fixture": {"transport": "streamable-http", "url": "https://mcp.example/mcp"}},
    )
    assert result.returncode == 0, result.stderr
    assert main.read_bytes() == original
    assert "OpenCode: could not install MCP configuration" in result.stderr
