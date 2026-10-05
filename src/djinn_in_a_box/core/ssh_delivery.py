"""Render the public-only SSH directory. This never reads private key contents."""

from __future__ import annotations

import base64
import binascii
import json
import os
import shlex
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from djinn_in_a_box.config.ssh import GitConfig
from djinn_in_a_box.core.exceptions import RuntimeMountSpecificationError

SSH_TARGET = Path("/home/dev/.ssh")
AGENT_TARGET = Path("/run/djinn-git-agent")
AGENT_SOCKET = str(AGENT_TARGET / "auth.sock")
GIT_MANIFEST = str(SSH_TARGET / "git.json")
MANAGED_SSH_TARGETS = (SSH_TARGET, AGENT_TARGET, Path("/home/dev/.gitconfig_local"))
GIT_ENVIRONMENT = {"SSH_AUTH_SOCK": AGENT_SOCKET, "DJINN_GIT_MANIFEST": GIT_MANIFEST}


class GitSSHError(RuntimeMountSpecificationError):
    """Invalid inputs or unavailable dedicated Git runtime."""


def take_string(data: bytes) -> tuple[bytes, bytes]:
    if len(data) < 4:
        raise GitSSHError("malformed SSH key/message")
    size = struct.unpack(">I", data[:4])[0]
    if size > len(data) - 4:
        raise GitSSHError("malformed SSH key/message")
    return data[4 : 4 + size], data[4 + size :]


def public_blob(text: str) -> bytes:
    """Validate ordinary OpenSSH public keys, excluding hardware keys and certificates."""
    parts = text.strip().split()
    if len(parts) < 2 or "\n" in text.strip():
        raise GitSSHError("expected one OpenSSH public key")
    algorithm = parts[0]
    if algorithm not in {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
    }:
        raise GitSSHError(
            "unsupported public key (hardware keys and certificates are out of scope)"
        )
    try:
        blob = base64.b64decode(parts[1], validate=True)
        name, rest = take_string(blob)
        if name.decode("ascii") != algorithm:
            raise GitSSHError("public key algorithm does not match its blob")
        first, rest = take_string(rest)
        if algorithm == "ssh-ed25519":
            if len(first) != 32:
                raise GitSSHError("malformed Ed25519 public key")
        elif algorithm == "ssh-rsa":
            second, rest = take_string(rest)
            if not first or not second or not int.from_bytes(first) or not int.from_bytes(second):
                raise GitSSHError("malformed RSA public key")
        else:
            second, rest = take_string(rest)
            curve = algorithm.removeprefix("ecdsa-sha2-")
            sizes = {"nistp256": 65, "nistp384": 97, "nistp521": 133}
            if first != curve.encode() or len(second) != sizes[curve] or second[0] != 4:
                raise GitSSHError("malformed ECDSA public key")
        if rest:
            raise GitSSHError("trailing data in public key")
        return blob
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise GitSSHError(f"invalid public key: {exc}") from exc


@dataclass(frozen=True)
class PublicDelivery:
    files: dict[str, str]
    blobs: frozenset[bytes]


def read_public_delivery(config: GitConfig) -> PublicDelivery:
    """Validate public selectors and extract trust by the real hostnames, never aliases."""
    files: dict[str, str] = {}
    blobs: set[bytes] = set()
    aliases: dict[str, dict[str, str]] = {}
    blocks: list[str] = []
    for alias, identity in config.identities.items():
        text = identity.public_key_file.read_text()
        blob = public_blob(text)
        check = subprocess.run(
            ["ssh-keygen", "-l", "-f", str(identity.public_key_file)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if check.returncode:
            raise GitSSHError(f"invalid public key for Git alias {alias}")
        normalized = " ".join(text.split()[:2]) + "\n"
        name = identity.public_key_file.name
        if name in files and public_blob(files[name]) != blob:
            raise GitSSHError(f"different public keys have the same filename: {name}")
        files[name] = normalized
        blobs.add(blob)
        public_path = str(SSH_TARGET / name)
        aliases[alias] = {
            "hostname": identity.hostname,
            "user": identity.user,
            "public_key_file": public_path,
        }
        blocks.append(
            f"Host {alias}\n    HostName {identity.hostname}\n    User {identity.user}\n"
            f"    IdentityAgent {AGENT_SOCKET}\n    IdentityFile {public_path}\n"
            "    IdentitiesOnly yes\n    UserKnownHostsFile /home/dev/.ssh/known_hosts\n"
            "    GlobalKnownHostsFile /dev/null\n    StrictHostKeyChecking yes\n"
            "    UpdateHostKeys no\n    ForwardAgent no\n"
        )
    trust: list[str] = []
    host_known_hosts = Path.home() / ".ssh" / "known_hosts"
    for hostname in dict.fromkeys(i.hostname for i in config.identities.values()):
        result = subprocess.run(
            ["ssh-keygen", "-F", hostname, "-f", str(host_known_hosts)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        matches = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
        if (
            result.returncode
            or not matches
            or all(line.startswith("@revoked ") for line in matches)
        ):
            raise GitSSHError(
                f"Missing trusted known_hosts entry for {hostname}; "
                "verify its host key on the host "
                "and add it to ~/.ssh/known_hosts before starting Djinn."
            )
        for line in matches:
            parts = line.split()
            index = 1 if parts[0].startswith("@") else 0
            if len(parts) < index + 3:
                raise GitSSHError(f"malformed known_hosts entry for {hostname}")
            public_blob(" ".join(parts[index + 1 : index + 3]))
            # Keep hashes/markers; narrow plaintext multi-host/wildcard entries.
            if not parts[index].startswith("|"):
                parts[index] = hostname
            trust.append(" ".join(parts[: index + 3]))
    files["known_hosts"] = "".join(line + "\n" for line in dict.fromkeys(trust))
    files["tailnet_known_hosts"] = ""
    # Exact aliases precede defaults; defaults do not enable undeclared identities.
    files["config"] = (
        "\n".join(blocks) + "\nHost *\n    IdentityAgent none\n    IdentityFile none\n"
        "    IdentitiesOnly yes\n    ForwardAgent no\n"
    )
    if config.allowed_signers_file is not None:
        signers = config.allowed_signers_file.read_text()
        for line in signers.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = shlex.split(line)
            found = next(
                (i for i, part in enumerate(parts) if part.startswith(("ssh-", "ecdsa-", "sk-"))),
                None,
            )
            if found is None or found < 1 or found + 1 >= len(parts):
                raise GitSSHError("invalid allowed_signers public trust file")
            public_blob(" ".join(parts[found : found + 2]))
        files["allowed_signers"] = signers
    signing_key = None
    if config.signing_identity is not None:
        signing_key = str(
            SSH_TARGET / config.identities[config.signing_identity].public_key_file.name
        )
    files["git.json"] = (
        json.dumps(
            {
                "identities": aliases,
                "signing_key": signing_key,
                "allowed_signers": str(SSH_TARGET / "allowed_signers")
                if config.allowed_signers_file
                else None,
            }
        )
        + "\n"
    )
    return PublicDelivery(files, frozenset(blobs))


def write_public_delivery(directory: Path, delivery: PublicDelivery) -> None:
    """Keep the bind-mounted directory inode stable; atomically replace its files."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise GitSSHError("public SSH delivery directory must be owned by the host user")
    directory.chmod(0o700)
    for name, contents in delivery.files.items():
        fd, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(contents)
            os.chmod(temporary, 0o600)
            os.replace(temporary, directory / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    for path in directory.iterdir():
        if path.name not in delivery.files:
            path.unlink()
