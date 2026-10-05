from __future__ import annotations

import os
import pty
import select
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from djinn_in_a_box.config.ssh import GitConfig
from djinn_in_a_box.core.git_agent import (
    AgentFilter,
    agent_keys,
    filter_request,
    request_agent,
    ssh_string,
    start_agent,
    stop_agent,
)
from djinn_in_a_box.core.ssh_delivery import GitSSHError, public_blob, read_public_delivery


@contextmanager
def filtered_agent(config):
    root = Path(os.environ["XDG_RUNTIME_DIR"])
    private = root / "private.sock"
    exported = root / "auth.sock"
    expected = read_public_delivery(config).blobs
    agent = start_agent(config, private, expected)
    server = AgentFilter(exported, private, expected)
    thread = threading.Thread(target=server.serve)
    thread.start()
    try:
        yield private, exported, expected
    finally:
        server.stop.set()
        thread.join(timeout=2)
        stop_agent(agent)


def test_declared_only_with_ambient_and_default_keys(git_inputs, monkeypatch):
    identities = git_inputs.git.identities
    ambient_config = GitConfig(identities={"ambient": identities["git-personal"]})
    ambient = Path(os.environ["XDG_RUNTIME_DIR"]) / "ambient.sock"
    ambient_keys = frozenset({public_blob(identities["git-personal"].public_key_file.read_text())})
    agent = start_agent(ambient_config, ambient, ambient_keys)
    try:
        monkeypatch.setenv("SSH_AUTH_SOCK", str(ambient))
        # A real default-key filename is present but is never imported.
        (Path.home() / ".ssh" / "id_ed25519").write_bytes(
            identities["git-personal"].key_file.read_bytes()
        )
        config = GitConfig(identities={"git-work": identities["git-work"]})
        with filtered_agent(config) as (private, exported, expected):
            assert agent_keys(private) == expected
            assert agent_keys(exported) == expected
            assert expected.isdisjoint(ambient_keys)
        assert agent_keys(ambient) == ambient_keys
    finally:
        stop_agent(agent)


@pytest.mark.parametrize("message_type", [17, 18, 19, 20, 21, 22, 23, 25, 26, 27, 255])
def test_filter_denies_mutations_providers_extensions(git_inputs, monkeypatch, message_type):
    with filtered_agent(git_inputs.git) as (private, exported, expected):
        from djinn_in_a_box.core import git_agent

        original = git_agent.request_agent
        forwarded = []

        def capture(path, payload):
            if path == private and payload == bytes([message_type]):
                forwarded.append(payload)
                return b"\x06"
            return original(path, payload)

        monkeypatch.setattr(git_agent, "request_agent", capture)
        assert request_agent(exported, bytes([message_type])) == b"\x05"
        assert forwarded == []
        assert agent_keys(private) == expected


def test_filter_denies_unlisted_signing_and_malformed_frames(git_inputs, monkeypatch):
    work = git_inputs.git.identities["git-work"]
    personal = git_inputs.git.identities["git-personal"]
    config = GitConfig(identities={"git-work": work})
    with filtered_agent(config) as (private, exported, expected):
        from djinn_in_a_box.core import git_agent

        original = git_agent.request_agent
        forwarded = []

        def capture(path, payload):
            if path == private and payload[:1] == b"\x0d":
                forwarded.append(payload)
                return b"\x0e" + ssh_string(b"unexpected signature")
            return original(path, payload)

        monkeypatch.setattr(git_agent, "request_agent", capture)
        other = public_blob(personal.public_key_file.read_text())
        payload = b"\x0d" + ssh_string(other) + ssh_string(b"message") + struct.pack(">I", 0)
        assert request_agent(exported, payload) == b"\x05"
        for malformed in (
            b"\x0bextra",
            b"\x0d",
            b"\x0d" + ssh_string(next(iter(expected))) + ssh_string(b"data") + struct.pack(">I", 8),
        ):
            assert filter_request(private, expected, malformed) == b"\x05"
        assert agent_keys(private) == expected
        assert forwarded == []


def test_signature_verifies_with_copied_public_trust(git_inputs, tmp_path):
    delivery = read_public_delivery(git_inputs.git)
    signers = tmp_path / "allowed_signers"
    signers.write_text(delivery.files["allowed_signers"])
    message = tmp_path / "message"
    message.write_bytes(b"disposable Git signing payload\n")
    with filtered_agent(git_inputs.git) as (_, exported, _):
        environment = {**os.environ, "SSH_AUTH_SOCK": str(exported)}
        subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-f",
                str(git_inputs.git.identities["git-work"].public_key_file),
                "-n",
                "git",
                str(message),
            ],
            env=environment,
            capture_output=True,
            check=True,
        )
    verified = subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "verify",
            "-f",
            str(signers),
            "-I",
            "signer@example.com",
            "-n",
            "git",
            "-s",
            str(message) + ".sig",
        ],
        input=message.read_bytes(),
        capture_output=True,
    )
    assert verified.returncode == 0, verified.stderr


def test_mismatched_private_public_key_stops_agent(git_inputs):
    work = git_inputs.git.identities["git-work"]
    personal = git_inputs.git.identities["git-personal"]
    config = GitConfig(identities={"work": work.model_copy(update={"key_file": personal.key_file})})
    socket = Path(os.environ["XDG_RUNTIME_DIR"]) / "mismatch.sock"
    with pytest.raises(GitSSHError, match="do not match"):
        start_agent(config, socket, read_public_delivery(config).blobs)
    with pytest.raises(OSError):
        agent_keys(socket)


def test_encrypted_key_no_terminal_refuses_with_remedy(git_inputs, monkeypatch):
    work = git_inputs.git.identities["git-work"]
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-p",
            "-P",
            "",
            "-N",
            "throwaway-passphrase",
            "-f",
            str(work.key_file),
        ],
        capture_output=True,
        check=True,
    )
    config = GitConfig(identities={"work": work})
    socket = Path(os.environ["XDG_RUNTIME_DIR"]) / "encrypted.sock"
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(GitSSHError, match="host terminal"):
        start_agent(config, socket, read_public_delivery(config).blobs)
    with pytest.raises(OSError):
        agent_keys(socket)


def test_shared_private_keys_loaded_once(git_inputs, monkeypatch):
    from djinn_in_a_box.core import git_agent

    work = git_inputs.git.identities["git-work"]
    config = GitConfig(identities={"work": work, "same": work})
    original = subprocess.run
    loads = []

    def capture(command, **kwargs):
        if command[0] == "ssh-add":
            loads.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(git_agent.subprocess, "run", capture)
    with filtered_agent(config):
        assert len(loads) == 1


def test_encrypted_key_prompts_on_host_terminal(git_inputs):
    work = git_inputs.git.identities["git-work"]
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-p",
            "-P",
            "",
            "-N",
            "throwaway-passphrase",
            "-f",
            str(work.key_file),
        ],
        capture_output=True,
        check=True,
    )
    driver = """
import fcntl, json, os, sys, termios
from pathlib import Path
from djinn_in_a_box.config.ssh import GitConfig
from djinn_in_a_box.core.git_agent import start_agent, stop_agent, agent_keys
from djinn_in_a_box.core.ssh_delivery import read_public_delivery
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
config = GitConfig.model_validate(json.loads(sys.argv[1]))
expected = read_public_delivery(config).blobs
path = Path(os.environ["XDG_RUNTIME_DIR"]) / "terminal.sock"
agent = start_agent(config, path, expected)
try:
    assert agent_keys(path) == expected
    print("HOST_PROMPT_OK", flush=True)
finally:
    stop_agent(agent)
"""
    master, slave = pty.openpty()
    config = GitConfig(identities={"work": work})
    process = subprocess.Popen(
        [sys.executable, "-c", driver, config.model_dump_json()],
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    output = b""
    sent = False
    deadline = time.monotonic() + 12
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output += os.read(master, 4096)
                except OSError:
                    break
                if b"Enter passphrase" in output and not sent:
                    os.write(master, b"throwaway-passphrase\n")
                    sent = True
            if process.poll() is not None:
                break
        assert process.wait(timeout=2) == 0, output
        assert sent and b"HOST_PROMPT_OK" in output, output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
