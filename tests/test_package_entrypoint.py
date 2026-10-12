from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from test_djinn_apt_root import ROOT, executable

from djinn_in_a_box.config.declarations import DJINN_ENV_KEYS
from djinn_in_a_box.core.docker import MANAGED_VOLUME_REPAIR_TARGETS


@pytest.mark.parametrize("case", ["success", "failure", "missing"])
def test_packages_section_order_and_failure_continue(tmp_path, case):
    source = (ROOT / "scripts/entrypoint.sh").read_text()
    # Check the complete entrypoint before slicing, including any section moved
    # outside the harness boundary.
    assert source.count('ui_section "Packages"') == 1
    assert (
        source.index('ui_section "MCP"')
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
        'ui_warn() { print -u2 -- "WARN $1"; }\n' + section + "\nprint FOLLOWING_STARTUP_STEP\n"
    )
    result = subprocess.run(
        ["zsh", str(harness)],
        env=dict(os.environ, HOME=str(tmp_path), MCP_REGISTER=str(mcp), PACKAGE_REPLAY=str(replay)),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "FOLLOWING_STARTUP_STEP\n"
    lines = result.stderr.splitlines()
    assert lines[:3] == ["SECTION MCP", "MCP_RAN", "SECTION Packages"]
    assert lines[-2:] == ["SECTION Tools", "TOOLS_RAN"]
    assert ("REPLAY_RAN" in lines) == (case != "missing")
    assert result.stderr.count("WARN") == (1 if case == "failure" else 0)
    if case == "failure":
        assert "continuing startup" in result.stderr


def test_packages_ownership_target():
    target = Path("/home/dev/.local/share/djinn-packages")
    assert MANAGED_VOLUME_REPAIR_TARGETS[-1] == target
    source = (ROOT / "scripts/entrypoint.sh").read_text()
    assert "~/workspaces ~/.local/share/djinn-packages; do" in source
