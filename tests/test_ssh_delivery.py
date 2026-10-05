from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.ssh import GitConfig, GitIdentity
from djinn_in_a_box.core.ssh_delivery import (
    AGENT_SOCKET,
    GitSSHError,
    public_blob,
    read_public_delivery,
    write_public_delivery,
)


def test_original_names_aliases_and_no_private_paths(git_inputs, tmp_path):
    delivery = read_public_delivery(git_inputs.git)
    directory = tmp_path / "public"
    write_public_delivery(directory, delivery)
    inode = directory.stat().st_ino
    write_public_delivery(directory, delivery)
    assert directory.stat().st_ino == inode
    for alias, identity in git_inputs.git.identities.items():
        assert (directory / identity.public_key_file.name).read_text().split()[
            :2
        ] == identity.public_key_file.read_text().split()[:2]
        result = subprocess.run(
            ["ssh", "-G", "-F", str(directory / "config"), alias],
            capture_output=True,
            text=True,
            check=True,
        )
        block = delivery.files["config"].split(f"Host {alias}\n", 1)[1].split("Host ", 1)[0]
        assert "    IdentitiesOnly yes\n" in block
        assert "hostname git.example.com\n" in result.stdout
        assert "user git\n" in result.stdout
        assert "identitiesonly yes\n" in result.stdout
        assert f"identityagent {AGENT_SOCKET}\n" in result.stdout
        assert f"identityfile /home/dev/.ssh/{identity.public_key_file.name}\n" in result.stdout
        assert "stricthostkeychecking true\n" in result.stdout
    serialized = "\n".join(delivery.files.values())
    assert str(Path.home()) not in serialized
    assert "PRIVATE KEY" not in serialized
    assert json.loads(delivery.files["git.json"])["signing_key"] == "/home/dev/.ssh/work_git.pub"


def test_selected_hashed_marked_git_trust(git_inputs):
    known = Path.home() / ".ssh" / "known_hosts"
    public = " ".join(git_inputs.git.identities["git-work"].public_key_file.read_text().split()[:2])
    known.write_text(known.read_text() + f"@cert-authority git.example.com {public}\n")
    subprocess.run(["ssh-keygen", "-H", "-f", str(known)], capture_output=True, check=True)
    delivery = read_public_delivery(git_inputs.git)
    selected = delivery.files["known_hosts"]
    assert "|1|" in selected
    assert "@cert-authority " in selected
    output = known.parent / "selected"
    output.write_text(selected)
    for hostname, expected in (("git.example.com", 0), ("other.example.com", 1)):
        found = subprocess.run(
            ["ssh-keygen", "-F", hostname, "-f", str(output)], capture_output=True
        )
        assert found.returncode == expected
    assert delivery.files["allowed_signers"] == git_inputs.git.allowed_signers_file.read_text()


def test_missing_trust_does_not_trust_alias(git_inputs):
    known = Path.home() / ".ssh" / "known_hosts"
    known.write_text(known.read_text().replace("git.example.com", "git-work"))
    with pytest.raises(GitSSHError, match="Missing trusted known_hosts entry for git.example.com"):
        read_public_delivery(git_inputs.git)


def test_conflicting_public_basename_refused(git_inputs, tmp_path):
    second = git_inputs.git.identities["git-personal"]
    other = tmp_path / "work_git.pub"
    other.write_text(second.public_key_file.read_text())
    conflicting = second.model_copy(update={"public_key_file": other})
    config = git_inputs.git.model_copy(
        update={
            "identities": {
                "git-work": git_inputs.git.identities["git-work"],
                "git-personal": conflicting,
            }
        }
    )
    with pytest.raises(GitSSHError, match="same filename"):
        read_public_delivery(config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("hostname", "git.example.com\nIdentityFile /secret"),
        ("user", "-root"),
        ("hostname", "*.example.com"),
        ("public_key_file", "/tmp/config"),
        ("public_key_file", "/tmp/git.json"),
        ("key_file", "relative"),
    ],
)
def test_unsafe_identity_inputs_refused(git_inputs, field, value):
    data = git_inputs.git.identities["git-work"].model_dump()
    with pytest.raises(ValidationError):
        GitIdentity.model_validate({**data, field: value})


@pytest.mark.parametrize(
    "text",
    [
        "ssh-ed25519 invalid",
        "sk-ssh-ed25519@openssh.com AAAA",
        "ssh-ed25519-cert-v01@openssh.com AAAA",
        "ssh-ed25519 AAAA",
    ],
)
def test_malformed_hardware_certificate_keys_refused(text):
    with pytest.raises(GitSSHError):
        public_blob(text)


def test_git_tables_roundtrip_and_scalar_preservation(git_inputs, tmp_path):
    from djinn_in_a_box.commands.config import _set_config_value

    path = tmp_path / "config.toml"
    save_config(git_inputs, path)
    loaded = load_config(path)
    assert loaded.git == git_inputs.git
    for key, value in (
        ("general.timezone", "UTC"),
        ("git.signing_identity", "git-personal"),
        ("git.allowed_signers_file", "none"),
    ):
        changed = _set_config_value(loaded, key, value)
        assert changed.git.identities == loaded.git.identities
    with pytest.raises(ValidationError):
        GitConfig(identities=loaded.git.identities, signing_identity="unknown")
    with pytest.raises(ValidationError):
        GitConfig.model_validate({"identities": {"git-*": loaded.git.identities["git-work"]}})
