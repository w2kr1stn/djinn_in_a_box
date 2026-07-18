"""Tests for the sourceable shell output library."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "output-lib.sh"
RAW_UI_MARKERS = ("✓", "⚠", "✗", "ℹ", "↻", "⊕", "✕", "🔒", "📡", "🌐")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def assert_plain_startup_output(output: str) -> None:
    assert "\x1b[" not in output
    for marker in RAW_UI_MARKERS:
        assert marker not in output


def strip_ansi(output: str) -> str:
    return ANSI_RE.sub("", output)


def run_output_lib(env_updates: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
    zsh = shutil.which("zsh")
    assert zsh is not None

    env = {**os.environ}
    for key, value in env_updates.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

    return subprocess.run(
        [
            zsh,
            "-c",
            (
                f"set -euo pipefail; source {shlex.quote(str(SCRIPT))}; "
                "ui_section 'Section'; ui_ok 'Ready'; ui_warn 'Careful'; "
                "ui_err 'Broken'; ui_info 'Details'; ui_item '↻' 'Synced'"
            ),
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def run_output_lib_command(
    command: str, env_updates: dict[str, str | None]
) -> subprocess.CompletedProcess[str]:
    zsh = shutil.which("zsh")
    assert zsh is not None

    env = {**os.environ}
    for key, value in env_updates.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

    return subprocess.run(
        [
            zsh,
            "-c",
            f"set -euo pipefail; source {shlex.quote(str(SCRIPT))}; {command}",
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def test_no_color_forces_plain_ascii_markers() -> None:
    result = run_output_lib({"NO_COLOR": "1", "DJINN_FORCE_UI_COLOR": "1"})

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert_plain_startup_output(result.stderr)
    assert "\n- Section" in result.stderr
    assert "[ok] Ready" in result.stderr
    assert "[warn] Careful" in result.stderr
    assert "[err] Broken" in result.stderr
    assert "[info] Details" in result.stderr
    assert "[sync] Synced" in result.stderr


def test_empty_no_color_still_allows_forced_ansi_256_color() -> None:
    result = run_output_lib(
        {"NO_COLOR": "", "DJINN_FORCE_UI_COLOR": "1", "COLORTERM": None}
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "\x1b[38;5;24m" in result.stderr
    assert "\n\x1b[38;5;24m─ " in result.stderr
    assert "\x1b[38;5;155m✓\x1b[0m Ready" in result.stderr


def test_non_tty_stderr_defaults_to_plain_ascii_markers() -> None:
    result = run_output_lib({"NO_COLOR": None, "DJINN_FORCE_UI_COLOR": None})

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "\x1b[" not in result.stderr
    assert "[ok] Ready" in result.stderr
    assert "[warn] Careful" in result.stderr
    assert "[err] Broken" in result.stderr
    assert "[info] Details" in result.stderr


def test_force_ui_color_test_hook_emits_ansi_256_and_icons_without_colorterm() -> None:
    result = run_output_lib(
        {"NO_COLOR": None, "DJINN_FORCE_UI_COLOR": "1", "COLORTERM": None}
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "\x1b[38;5;24m" in result.stderr
    assert "\x1b[38;5;73mSection" in result.stderr
    assert "\x1b[38;5;155m✓\x1b[0m Ready" in result.stderr
    assert "\x1b[38;5;227m⚠\x1b[0m Careful" in result.stderr
    assert "\x1b[38;5;203m✗\x1b[0m Broken" in result.stderr
    assert "\x1b[0;34mℹ\x1b[0m Details" in result.stderr
    assert "\x1b[0;34m↻\x1b[0m Synced" in result.stderr


def test_force_ui_color_uses_truecolor_when_colorterm_supports_it() -> None:
    result = run_output_lib(
        {"NO_COLOR": None, "DJINN_FORCE_UI_COLOR": "1", "COLORTERM": "truecolor"}
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "\x1b[38;2;41;82;109m" in result.stderr
    assert "\x1b[38;2;105;185;161mSection" in result.stderr
    assert "\x1b[38;2;193;255;98m✓\x1b[0m Ready" in result.stderr
    assert "\x1b[0;34mℹ\x1b[0m Details" in result.stderr
    assert "\x1b[0;34m↻\x1b[0m Synced" in result.stderr


def test_ui_section_uses_full_columns_width() -> None:
    result = run_output_lib(
        {
            "NO_COLOR": None,
            "DJINN_FORCE_UI_COLOR": "1",
            "COLORTERM": "truecolor",
            "COLUMNS": "100",
            "DJINN_TERM_WIDTH": None,
        }
    )

    assert result.returncode == 0, result.stderr
    section_line = strip_ansi(result.stderr).splitlines()[1]
    assert section_line.startswith("─ Section ")
    assert len(section_line) == 100


def test_ui_section_prefers_djinn_term_width_over_columns() -> None:
    result = run_output_lib(
        {
            "NO_COLOR": None,
            "DJINN_FORCE_UI_COLOR": "1",
            "COLORTERM": "truecolor",
            "COLUMNS": "100",
            "DJINN_TERM_WIDTH": "97",
        }
    )

    assert result.returncode == 0, result.stderr
    section_line = strip_ansi(result.stderr).splitlines()[1]
    assert section_line.startswith("─ Section ")
    assert len(section_line) == 97


def test_ui_info_and_default_item_markers_are_basic_blue() -> None:
    result = run_output_lib(
        {"NO_COLOR": None, "DJINN_FORCE_UI_COLOR": "1", "COLORTERM": "truecolor"}
    )

    assert result.returncode == 0, result.stderr
    assert "\x1b[0;34mℹ\x1b[0m Details" in result.stderr
    assert "\x1b[0;34m↻\x1b[0m Synced" in result.stderr
    assert "\x1b[38;2;134;8;184m↻" not in result.stderr
    assert "\x1b[38;5;91m↻" not in result.stderr


def test_ui_boxed_uses_muted_truecolor_for_head_body_and_foot() -> None:
    result = run_output_lib_command(
        "printf 'Removed MCP server local-http\\nFile modified: /home/dev/.claude.json\\n' | "
        "ui_boxed 'claude mcp'",
        {"NO_COLOR": None, "DJINN_FORCE_UI_COLOR": "1", "COLORTERM": "truecolor"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    muted = "\x1b[38;2;51;54;118m"
    lines = result.stderr.splitlines()
    assert lines[0].startswith(f"  {muted}╭─\x1b[0m claude mcp {muted}─")
    assert lines[1] == f"  {muted}│\x1b[0m Removed MCP server local-http"
    assert lines[2] == f"  {muted}│\x1b[0m File modified: /home/dev/.claude.json"
    assert lines[3].startswith(f"  {muted}╰")


def test_ui_boxed_uses_muted_ansi_256_without_truecolor() -> None:
    result = run_output_lib_command(
        "printf 'Added HTTP MCP server local-http with URL: http://mcp.example:8847/mcp\\n' | "
        "ui_boxed 'claude mcp'",
        {"NO_COLOR": None, "DJINN_FORCE_UI_COLOR": "1", "COLORTERM": None},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "\x1b[38;5;60m╭─\x1b[0m claude mcp \x1b[38;5;60m─" in result.stderr
    assert (
        "\x1b[38;5;60m│\x1b[0m Added HTTP MCP server local-http with URL: "
        "http://mcp.example:8847/mcp"
    ) in result.stderr
    assert "\x1b[38;5;60m╰" in result.stderr


def test_ui_boxed_plain_mode_uses_ascii_frame() -> None:
    result = run_output_lib_command(
        "printf 'Removed MCP server local-http\\n' | ui_boxed 'claude mcp'",
        {"NO_COLOR": "1", "DJINN_FORCE_UI_COLOR": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "  +- claude mcp ----------------------------",
        "  | Removed MCP server local-http",
        "  +---------------------------------------",
    ]


def test_ui_boxed_empty_input_renders_nothing() -> None:
    result = run_output_lib_command(
        "printf '' | ui_boxed 'claude mcp'",
        {"NO_COLOR": None, "DJINN_FORCE_UI_COLOR": "1", "COLORTERM": "truecolor"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_ui_boxed_long_line_has_no_right_edge() -> None:
    long_url = "http://mcp.example:8847/" + ("very-long-path/" * 12) + "mcp"
    result = run_output_lib_command(
        f"printf '%s\\n' {shlex.quote(long_url)} | ui_boxed 'claude mcp'",
        {"NO_COLOR": None, "DJINN_FORCE_UI_COLOR": "1", "COLORTERM": "truecolor"},
    )

    assert result.returncode == 0, result.stderr
    body_line = strip_ansi(result.stderr).splitlines()[1]
    assert body_line == f"  │ {long_url}"
    assert not body_line.endswith("│")


def test_entrypoint_continues_with_plain_startup_messages_when_output_lib_absent(
    tmp_path: Path,
) -> None:
    zsh = shutil.which("zsh")
    assert zsh is not None

    missing_output_lib = tmp_path / "missing-output-lib.sh"
    missing_seed_lib = tmp_path / "missing-seed-lib.sh"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OUTPUT_LIB": str(missing_output_lib),
        "SEED_LIB": str(missing_seed_lib),
        "ENABLE_FIREWALL": "false",
    }

    result = subprocess.run(
        [zsh, "-n", str(ROOT / "scripts" / "entrypoint.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    result = subprocess.run(
        [zsh, str(ROOT / "scripts" / "entrypoint.sh")],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    assert "[warn] output library not found" in result.stderr
    assert "[info] Seed & Config" in result.stderr
    assert "[err] seed library not found" in result.stderr


def test_entrypoint_security_section_uses_plain_ascii_markers(tmp_path: Path) -> None:
    zsh = shutil.which("zsh")
    jq = shutil.which("jq")
    assert zsh is not None
    assert jq is not None

    mcp_config = tmp_path / "mcp-servers.json"
    mcp_config.write_text("{}", encoding="utf-8")
    opencode_seed = tmp_path / ".opencode" / "seed"
    opencode_seed.mkdir(parents=True)
    instructions = b"OpenCode instructions.\n"
    (opencode_seed / "AGENTS.md").write_bytes(instructions)
    legacy_opencode_settings = b'{"personal":true}\n'
    (opencode_seed / ".opencode.json").write_bytes(legacy_opencode_settings)
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / ".djinn-config-sync.json").write_text(
        json.dumps(
            {
                "source": "opencode",
                "items": [
                    {
                        "path": "opencode/AGENTS.md",
                        "content_hash": hashlib.sha256(instructions).hexdigest(),
                        "executable": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OUTPUT_LIB": str(ROOT / "scripts" / "output-lib.sh"),
        "SEED_LIB": str(ROOT / "scripts" / "seed-lib.sh"),
        "MCP_REGISTER": str(ROOT / "scripts" / "mcp-register.sh"),
        "SETTINGS_COPY_HELPER": str(ROOT / "scripts" / "settings-copy.py"),
        "WORKFLOW_PUBLISHER": str(
            ROOT / "src" / "djinn_in_a_box" / "core" / "workflow_publisher.py"
        ),
        "DJINN_CANONICAL_ROOT": str(canonical),
        "OPENCODE_WORKFLOW_VIEW": str(opencode_seed),
        "OPENCODE_RUNTIME_ROOT": str(tmp_path / "runtime-opencode"),
        "MCP_SERVERS_CONFIG": str(mcp_config),
        "NO_COLOR": "1",
        "ENABLE_FIREWALL": "false",
        "DOCKER_DIRECT": "false",
        "PATH": f"{Path(jq).parent}:{os.environ['PATH']}",
    }
    env.pop("DOCKER_HOST", None)
    env.pop("DJINN_FORCE_UI_COLOR", None)

    result = subprocess.run(
        [zsh, str(ROOT / "scripts" / "entrypoint.sh"), "-c", "true"],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert_plain_startup_output(result.stderr)
    assert "\n- Security" in result.stderr
    assert "[warn] Firewall:     Disabled" in result.stderr
    assert "[warn] Docker Access: Disabled" in result.stderr
    assert "[info] Enable with: djinn start --docker" in result.stderr
    assert "[warn] MCP Gateway:  Not connected" in result.stderr
    persistent_settings = tmp_path / ".opencode" / ".opencode.json"
    assert json.loads(persistent_settings.read_bytes())["personal"] is True
    assert (opencode_seed / ".opencode.json").read_bytes() == legacy_opencode_settings


def test_firewall_startup_uses_plain_ascii_markers(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    iptables = bin_dir / "iptables"
    iptables.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    iptables.chmod(0o755)
    getent = bin_dir / "getent"
    getent.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"ahostsv4\" ]; then\n"
        "  printf '203.0.113.10 STREAM %s\\n' \"$2\"\n"
        "  exit 0\n"
        "fi\n"
        "exec /usr/bin/getent \"$@\"\n",
        encoding="utf-8",
    )
    getent.chmod(0o755)

    env = {
        **os.environ,
        "OUTPUT_LIB": str(ROOT / "scripts" / "output-lib.sh"),
        "NO_COLOR": "1",
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    env.pop("DJINN_FORCE_UI_COLOR", None)

    result = subprocess.run(
        [bash, str(ROOT / "scripts" / "init-firewall.sh")],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert_plain_startup_output(result.stderr)
    assert "[info] Initializing firewall..." in result.stderr
    assert "[ok] Allowed network: 172.16.0.0/12" in result.stderr
    assert "[ok] Allowed: registry.npmjs.org (203.0.113.10)" in result.stderr
    assert "[ok] Firewall initialized. Outbound traffic restricted to whitelist." in result.stderr


def test_output_lib_is_bash_compatible_and_resource_safe() -> None:
    """The lib is sourced by zsh (container) AND bash (host scripts like
    update-agents.sh); a re-source must not trip the readonly constants."""
    bash = shutil.which("bash")
    assert bash is not None

    syntax = subprocess.run(
        [bash, "-n", str(ROOT / "scripts" / "output-lib.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    script = (
        f'source "{ROOT}/scripts/output-lib.sh" && '
        f'source "{ROOT}/scripts/output-lib.sh" && '
        'DJINN_FORCE_UI_COLOR=1 ui_ok "bash works" && COLUMNS=50 ui_section "Test"'
    )
    result = subprocess.run(
        [bash, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "bash works" in result.stderr
    assert "Test" in result.stderr
