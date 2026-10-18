"""Offline native npm installation, including refusal of a corrupted binary archive."""

import base64
import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "assistant/install-opencode.sh"


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_native_install_verifies_registry_integrity(
    tmp_path: Path,
    architecture: str,
    corrupt: bool,
) -> None:
    archive = tmp_path / "agent.tgz"
    payload = b"#!/bin/sh\necho opencode v2.0.24\n"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("package/bin/opencode")
        member.size, member.mode = len(payload), 0o755
        tar.addfile(member, io.BytesIO(payload))
    integrity = base64.b64encode(hashlib.sha512(archive.read_bytes()).digest()).decode()
    metadata = tmp_path / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "dist": {
                    "tarball": "https://registry.npmjs.org/fixture.tgz",
                    "integrity": f"sha512-{integrity}",
                }
            }
        )
    )
    if corrupt:
        archive.write_bytes(archive.read_bytes() + b"corruption")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$INSTALL_CALLS"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        '    --proto|--proto-redir) shift 2;;\n'
        '    -fsSL) shift;;\n'
        '    -o) destination=$2; shift 2;;\n'
        '    *) url=$1; shift;;\n'
        '  esac\n'
        'done\n'
        'case "$url" in\n'
        '  https://registry.npmjs.org/fixture.tgz) cp "$INSTALL_ARCHIVE" "$destination";;\n'
        '  *) cp "$INSTALL_METADATA" "$destination";;\nesac\n'
    )
    curl.chmod(0o755)
    calls = tmp_path / "calls"
    destination = tmp_path / "installed"
    result = subprocess.run(
        ["sh", str(SCRIPT), architecture, "2.0.24", str(destination)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "INSTALL_METADATA": str(metadata),
            "INSTALL_ARCHIVE": str(archive),
            "INSTALL_CALLS": str(calls),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    arch = "x64-baseline" if architecture == "amd64" else "arm64"
    arguments = [shlex.split(line) for line in calls.read_text().splitlines()]
    assert len(arguments) == 2
    for call in arguments:
        assert call[call.index("--proto") + 1] == "=https"
        assert call[call.index("--proto-redir") + 1] == "=https"
    assert f"https://registry.npmjs.org/@opencode/cli-linux-{arch}/2.0.24" in arguments[0]
    assert "https://registry.npmjs.org/fixture.tgz" in arguments[1]
    installed = destination / "opencode"
    if corrupt:
        assert result.returncode != 0
        assert not installed.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert installed.read_bytes() == payload
        assert os.access(installed, os.X_OK)
