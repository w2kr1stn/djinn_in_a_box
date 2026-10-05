from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from djinn_in_a_box.core.ssh_delivery import GIT_ENVIRONMENT, read_public_delivery

ROOT = Path(__file__).resolve().parents[1]


def git_value(home, name):
    return subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(home / ".gitconfig_local"),
            "--get",
            name,
        ],
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize("selected", [True, False])
def test_manifest_signing_explicit_and_ignore_independent(git_inputs, selected):
    config = git_inputs.git
    if not selected:
        config = config.model_copy(update={"signing_identity": None})
    home = Path.home()
    manifest = home / "manifest.json"
    manifest.write_text(read_public_delivery(config).files["git.json"])
    (home / ".gitignore_global").write_text("*.tmp\n")
    (home / ".gitconfig_local").write_text("[user]\n signingkey = stale-first-key\n")
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "git-config.py"),
        ],
        env={**os.environ, "DJINN_GIT_MANIFEST": str(manifest)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert git_value(home, "gpg.format").stdout.strip() == "ssh"
    signing = git_value(home, "user.signingkey")
    assert (
        signing.stdout.strip() == "/home/dev/.ssh/work_git.pub"
        if selected
        else signing.returncode == 1
    )
    assert (
        git_value(home, "gpg.ssh.allowedSignersFile").stdout.strip()
        == "/home/dev/.ssh/allowed_signers"
    )
    assert git_value(home, "core.excludesfile").stdout.strip() == str(home / ".gitignore_global")
    assert git_value(home, "commit.gpgsign").returncode == 1


def test_manifest_rejects_private_selector(git_inputs):
    manifest = Path.home() / "bad-manifest.json"
    data = json.loads(read_public_delivery(git_inputs.git).files["git.json"])
    data["signing_key"] = "/home/dev/.ssh/work_git"
    manifest.write_text(json.dumps(data))
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/git-config.py")],
        env={**os.environ, "DJINN_GIT_MANIFEST": str(manifest)},
        capture_output=True,
    )
    assert result.returncode != 0
    assert not (Path.home() / ".gitconfig_local").exists()


def test_git_manifest_helper_packaged_and_env_registered():
    from djinn_in_a_box.config.declarations import RESERVED_ENVIRONMENT

    dockerfile = (ROOT / "Dockerfile").read_text()
    entrypoint = (ROOT / "scripts/entrypoint.sh").read_text()
    assert "COPY --chown=dev:dev scripts/git-config.py /home/dev/git-config.py" in dockerfile
    assert 'python3 "$GIT_CONFIG_HELPER"' in entrypoint
    assert "*_github.pub" not in entrypoint
    assert set(GIT_ENVIRONMENT) <= RESERVED_ENVIRONMENT.keys()
    assert "GIT_CONFIG_HELPER" in RESERVED_ENVIRONMENT
