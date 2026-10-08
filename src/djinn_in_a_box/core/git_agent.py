"""Dedicated OpenSSH agent and a bounded list/sign-only protocol filter."""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from djinn_in_a_box.config.ssh import GitConfig
from djinn_in_a_box.core.ssh_delivery import GitSSHError, take_string

MAX_MESSAGE = 262144
FAILURE = b"\x05"


def ssh_string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def receive_exact(stream: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = stream.recv(size - len(data))
        if not part:
            raise EOFError
        data.extend(part)
    return bytes(data)


def receive_message(stream: socket.socket) -> bytes:
    size = struct.unpack(">I", receive_exact(stream, 4))[0]
    if size < 1 or size > MAX_MESSAGE:
        raise GitSSHError("invalid agent message size")
    return receive_exact(stream, size)


def request_agent(path: Path, payload: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(5)
        stream.connect(str(path))
        stream.sendall(ssh_string(payload))
        return receive_message(stream)


def agent_keys(path: Path) -> frozenset[bytes]:
    response = request_agent(path, b"\x0b")
    if response[:1] != b"\x0c" or len(response) < 5:
        raise GitSSHError("agent did not return its public identities")
    count = struct.unpack(">I", response[1:5])[0]
    rest = response[5:]
    keys: set[bytes] = set()
    for _ in range(count):
        blob, rest = take_string(rest)
        _, rest = take_string(rest)
        keys.add(blob)
    if rest:
        raise GitSSHError("malformed agent identity response")
    return frozenset(keys)


def filter_request(private_socket: Path, allowed: frozenset[bytes], payload: bytes) -> bytes:
    if payload == b"\x0b":
        return (
            b"\x0c"
            + struct.pack(">I", len(allowed))
            + b"".join(ssh_string(blob) + ssh_string(b"") for blob in sorted(allowed))
        )
    if payload[:1] != b"\x0d":
        return FAILURE
    try:
        blob, rest = take_string(payload[1:])
        _, flags = take_string(rest)
        if blob not in allowed or len(flags) != 4 or struct.unpack(">I", flags)[0] not in {0, 2, 4}:
            return FAILURE
        response = request_agent(private_socket, payload)
        return response if response[:1] == b"\x0e" else FAILURE
    except (GitSSHError, OSError, EOFError):
        return FAILURE


class AgentFilter:
    """Clients can list/sign declared keys; every mutating or extension request fails."""

    def __init__(self, exported: Path, private: Path, allowed: frozenset[bytes]) -> None:
        self.exported = exported
        self.private = private
        self.allowed = allowed
        self.stop = threading.Event()
        self.clients = threading.BoundedSemaphore(16)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(exported))
        exported.chmod(0o600)
        self.listener.listen(16)
        self.listener.settimeout(0.2)

    def serve(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    stream, _ = self.listener.accept()
                except TimeoutError:
                    continue
                if not self.clients.acquire(blocking=False):
                    stream.close()
                    continue
                threading.Thread(target=self._client, args=(stream,), daemon=True).start()
        finally:
            self.listener.close()
            self.exported.unlink(missing_ok=True)

    def _client(self, stream: socket.socket) -> None:
        try:
            with stream:
                stream.settimeout(5)
                while not self.stop.is_set():
                    payload = receive_message(stream)
                    stream.sendall(ssh_string(filter_request(self.private, self.allowed, payload)))
        except (OSError, EOFError, GitSSHError):
            return
        finally:
            self.clients.release()


def start_agent(
    config: GitConfig,
    private_socket: Path,
    expected: frozenset[bytes],
    on_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> subprocess.Popen[bytes]:
    private_socket.unlink(missing_ok=True)
    agent = subprocess.Popen(
        ["ssh-agent", "-D", "-a", str(private_socket)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        if on_start is not None:
            on_start(agent)
        deadline = time.monotonic() + 5
        while not private_socket.exists():
            if agent.poll() is not None or time.monotonic() >= deadline:
                raise GitSSHError("dedicated Git ssh-agent failed to start")
            time.sleep(0.02)
        if agent_keys(private_socket):
            raise GitSSHError("new dedicated Git agent must start empty")
        environment = {
            **os.environ,
            "SSH_AUTH_SOCK": str(private_socket),
            "SSH_ASKPASS_REQUIRE": "never",
        }
        for key in ("SSH_AGENT_PID", "SSH_ASKPASS", "DISPLAY"):
            environment.pop(key, None)
        key_files = list(dict.fromkeys(i.key_file for i in config.identities.values()))
        if key_files:
            # ssh-add reads passphrases from the host controlling terminal and tries the
            # last entered passphrase on following files; shared passphrases prompt once.
            result = subprocess.run(
                ["ssh-add", *map(str, key_files)],
                env=environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=None if sys.stdin.isatty() else subprocess.DEVNULL,
                start_new_session=not sys.stdin.isatty(),
                timeout=120 if sys.stdin.isatty() else 10,
            )
            if result.returncode:
                raise GitSSHError(
                    "Could not load a declared Git key. Start Djinn on a host terminal to unlock "
                    "encrypted keys; check the key path and its permissions."
                )
        if agent_keys(private_socket) != expected:
            raise GitSSHError("loaded agent keys do not match the declared public keys")
        return agent
    except BaseException:
        stop_agent(agent)
        raise


def stop_agent(agent: subprocess.Popen[bytes]) -> None:
    if agent.poll() is None:
        agent.terminate()
        try:
            agent.wait(timeout=3)
        except subprocess.TimeoutExpired:
            agent.kill()
            agent.wait(timeout=3)
