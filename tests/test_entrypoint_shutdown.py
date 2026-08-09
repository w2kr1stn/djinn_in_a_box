"""Tests for the entrypoint's shutdown path — settings persistence on both exits.

The interactive shell exiting normally and `docker stop` (SIGTERM to PID 1) must
both reach the reverse-sync. The detached container has no other shutdown path,
so a regression here loses settings silently.

These run the real section lifted out of ``scripts/entrypoint.sh`` with the sync
helpers stubbed, so the control flow under test is the shipped one.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "entrypoint.sh"
SECTION_MARKER = "# Interactive Shell (reverse-sync settings on exit)"

requires_zsh = pytest.mark.skipif(
    shutil.which("zsh") is None, reason="zsh is not installed on this host"
)


def _shutdown_section() -> str:
    """The shipped shutdown section, from its banner to the end of the file."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    marker = text.index(SECTION_MARKER)
    return text[text.rindex("# ====", 0, marker) :]


def _harness(tmp_path: Path) -> Path:
    """Wrap the real section in stubs so it can run outside a container."""
    log = tmp_path / "sync.log"
    helper = tmp_path / "settings_copy.py"
    helper.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

    script = tmp_path / "harness.sh"
    script.write_text(
        "#!/bin/zsh\n"
        "set -euo pipefail\n"
        f'SYNC_LOG="{log}"\n'
        f'SETTINGS_COPY_HELPER="{helper}"\n'
        'OPENCODE_RUNTIME_SETTINGS="/dev/null"\n'
        'OPENCODE_PERSISTENT_SETTINGS="/dev/null"\n'
        'reverse_sync_file() { echo "file:$1" >> "$SYNC_LOG"; }\n'
        'reverse_sync_claude_settings() { echo "claude:$1" >> "$SYNC_LOG"; }\n'
        "ui_warn() { :; }\n"
        "\n" + _shutdown_section(),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _sync_lines(tmp_path: Path) -> list[str]:
    log = tmp_path / "sync.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


@requires_zsh
def test_normal_shell_exit_persists_state_and_keeps_the_exit_code(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(_harness(tmp_path)), "-c", "exit 7"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 7, result.stderr
    assert len(_sync_lines(tmp_path)) == 3


@requires_zsh
def test_sigterm_persists_state_before_exiting(tmp_path: Path) -> None:
    """`docker stop` is the only shutdown a detached container ever gets."""
    process = subprocess.Popen(
        [str(_harness(tmp_path)), "-c", "sleep 60"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_until_shell_started(process)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=15)
    finally:
        if process.poll() is None:  # pragma: no cover - only on regression
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=5)

    assert process.returncode == 128 + signal.SIGTERM
    # Exactly three: the signal path must not double-run the normal path.
    assert len(_sync_lines(tmp_path)) == 3


@requires_zsh
def test_sigterm_persists_state_only_once(tmp_path: Path) -> None:
    """Repeated signals must not re-run the sync — the guard flag is load-bearing."""
    process = subprocess.Popen(
        [str(_harness(tmp_path)), "-c", "sleep 60"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_until_shell_started(process)
        for _ in range(3):
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        process.wait(timeout=15)
    finally:
        if process.poll() is None:  # pragma: no cover - only on regression
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=5)

    assert len(_sync_lines(tmp_path)) == 3


def _wait_until_shell_started(process: subprocess.Popen[str]) -> None:
    """Let the harness reach `wait` before signalling it.

    Signalling earlier would race the trap installation and test nothing.
    """
    time.sleep(0.5)
    if process.poll() is not None:  # pragma: no cover - only on regression
        raise AssertionError("harness exited before it could be signalled")
