from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from test_djinn_apt_root import ROOT, executable
from test_output_lib import assert_plain_startup_output

from djinn_in_a_box.config.declarations import DJINN_ENV_KEYS
from djinn_in_a_box.core.docker import MANAGED_VOLUME_REPAIR_TARGETS


def package_environment(**extra):
    env = dict(os.environ)
    env.pop("DJINN_EGRESS_ALLOW", None)
    env.update(extra)
    return env


@pytest.mark.parametrize("command", [False, True])
@pytest.mark.parametrize(
    "marker", [None, "", "203.0.113.10/32/tcp/443"], ids=["unset", "empty", "allowlist"],
)
@pytest.mark.parametrize("case", ["success", "failure", "missing"])
def test_packages_section_order_and_failure_continue(tmp_path, case, marker, command):
    source = (ROOT / "scripts/entrypoint.sh").read_text()
    # Check the complete entrypoint before slicing, including any section moved
    # outside the harness boundary.
    assert source.count('ui_section "Packages"') == 1
    assert (
        source.index('ui_section "MCP"')
        < source.index('ui_section "Codex daemon"')
        < source.index('ui_section "Packages"')
        < source.index('ui_section "Tools"')
    )
    start = source.index('ui_section "MCP"')
    end = source.index('ui_section "Security"')
    section = source[start:end]
    assert section.index('ui_section "MCP"') < section.index('ui_section "Packages"')
    assert section.index('ui_section "Packages"') < section.index('ui_section "Tools"')
    assert 'PACKAGE_REPLAY="${PACKAGE_REPLAY:-/home/dev/package-replay.sh}"' in section
    assert 'source "$PACKAGE_REPLAY"' not in section
    assert "PACKAGE_REPLAY" in DJINN_ENV_KEYS
    mcp = tmp_path / "mcp.sh"
    mcp.write_text("register_mcp_servers() { print MCP_RAN; }\n")
    policy = tmp_path / "policy.py"
    policy.write_text('print("POLICY_RAN")\n')
    replay = tmp_path / "replay.sh"
    if case != "missing":
        executable(
            replay,
            '#!/bin/bash\nitems=(REPLAY_RAN)\necho "${items[0]}"\n'
            + ("exit 7\n" if case == "failure" else "exit 0\n"),
        )
    tools = tmp_path / ".tools"
    tools.mkdir()
    executable(tools / "install.sh", "#!/bin/sh\necho TOOLS_RAN\n")
    harness = tmp_path / "startup.zsh"
    harness.write_text(
        "set -euo pipefail\n"
        'ui_section() { print -u2 -- "SECTION $1"; }\n'
        'ui_warn() { print -u2 -- "WARN $1"; }\n'
        'ui_info() { print -u2 -- "INFO $1"; }\n' + section + "\nprint FOLLOWING_STARTUP_STEP\n"
    )
    child_env = package_environment(
        HOME=str(tmp_path),
        MCP_REGISTER=str(mcp),
        PACKAGE_REPLAY=str(replay),
        CODEX_DAEMON_POLICY_HELPER=str(policy),
    )
    if marker is not None:
        child_env["DJINN_EGRESS_ALLOW"] = marker
    result = subprocess.run(
        ["zsh", str(harness)] + (["-c", "true"] if command else []),
        env=child_env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "FOLLOWING_STARTUP_STEP\n"
    lines = result.stderr.splitlines()
    prefix = ["SECTION MCP", "MCP_RAN"]
    if not command:
        prefix += ["SECTION Codex daemon", "POLICY_RAN"]
    prefix += ["SECTION Packages"]
    assert lines[:len(prefix)] == prefix
    assert lines[-2:] == ["SECTION Tools", "TOOLS_RAN"]
    assert ("REPLAY_RAN" in lines) == (not marker and case in ("success", "failure"))
    assert result.stderr.count("WARN") == int(not marker and case == "failure")
    if not marker and case == "failure":
        assert "continuing startup" in result.stderr
    info_line = "INFO Packages: skipped in allowlist mode (no network)."
    if marker:
        assert lines[lines.index("SECTION Packages") + 1] == info_line
        assert result.stderr.count(info_line) == 1
        assert "WARN" not in result.stderr
    else:
        assert not any(line.startswith("INFO Packages: skipped") for line in lines)


def test_packages_ownership_target():
    assert Path("/var/lib/djinn-packages") not in MANAGED_VOLUME_REPAIR_TARGETS
    assert len(MANAGED_VOLUME_REPAIR_TARGETS) == 5
    source = (ROOT / "scripts/entrypoint.sh").read_text()
    loop = source[source.index("for dir in "):source.index("OWNERSHIP_REPAIR_HELPER=")]
    assert "djinn-packages" not in loop
    assert "~/workspaces; do" in loop


@pytest.mark.parametrize("command", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_codex_daemon_step_guard_order_arguments_and_failure_continue(tmp_path, command, failure):
    source = (ROOT / "scripts/entrypoint.sh").read_text()
    assert source.count('ui_section "Codex daemon"') == 1
    section = source[source.index('ui_section "MCP"'):source.index('ui_section "Tools"')]
    assert "CODEX_DAEMON_POLICY_HELPER" in DJINN_ENV_KEYS
    assert "CODEX_HOME" in DJINN_ENV_KEYS
    mcp = tmp_path / "mcp.sh"
    mcp.write_text("register_mcp_servers() { print MCP_RAN; }\n")
    helper = tmp_path / "policy with spaces.py"
    helper.write_text(
        "import sys\n"
        + f"assert sys.argv[1:] == ['--codex-home', {str(tmp_path / '.codex')!r}]\n"
        + "print('POLICY_RAN')\n"
        + "print('POLICY_REASON on stderr', file=sys.stderr)\n"
        + ("sys.exit(1)\n" if failure else "")
    )
    replay = tmp_path / "replay.sh"
    executable(replay, "#!/bin/sh\necho REPLAY_RAN\n")
    harness = tmp_path / "startup.zsh"
    harness.write_text(
        "set -euo pipefail\n"
        'ui_section() { print -u2 -- "SECTION $1"; }\n'
        'ui_warn() { print -u2 -- "WARN $1"; }\n'
        'ui_info() { print -u2 -- "INFO $1"; }\n'
        + section + "\nprint FOLLOWING_STARTUP_STEP\n"
    )
    result = subprocess.run(
        ["zsh", str(harness)] + (["-c", "true"] if command else []),
        env=package_environment(
            HOME=str(tmp_path),
            MCP_REGISTER=str(mcp),
            PACKAGE_REPLAY=str(replay),
            CODEX_DAEMON_POLICY_HELPER=str(helper),
        ),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "FOLLOWING_STARTUP_STEP\n"
    assert_plain_startup_output(result.stderr)
    assert ("POLICY_RAN" in result.stderr) is not command
    # The helper's own reason must reach the startup log, not only the generic warning.
    assert ("POLICY_REASON on stderr" in result.stderr) is not command
    assert ("SECTION Codex daemon" in result.stderr) is not command
    assert result.stderr.count("WARN ") == int(failure and not command)
    if not command:
        assert result.stderr.index("MCP_RAN") < result.stderr.index("POLICY_RAN")
        assert result.stderr.index("POLICY_RAN") < result.stderr.index("SECTION Packages")
    if failure and not command:
        assert "Codex daemon policy failed; continuing startup." in result.stderr
    assert "REPLAY_RAN" in result.stderr
