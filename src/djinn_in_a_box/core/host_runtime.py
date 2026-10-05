"""Host-owned Git runtime and detached observation of the actual dev container ID."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, cast

from djinn_in_a_box.core.git_agent import AgentFilter, agent_keys, start_agent, stop_agent
from djinn_in_a_box.core.ssh_delivery import (
    AGENT_TARGET,
    GIT_ENVIRONMENT,
    SSH_TARGET,
    GitSSHError,
    read_public_delivery,
    write_public_delivery,
)

if TYPE_CHECKING:
    from djinn_in_a_box.config.models import AppConfig
    from djinn_in_a_box.core.docker import ComposeFragment

GENERATION_LABEL = "djinn.git-agent.generation"
STARTUP_SECONDS = 60


def private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise GitSSHError(f"Git runtime directory must be owned by you with mode 0700: {path}")
    return path


def runtime_root(*, create: bool = False) -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        base = Path(xdg)
        if not base.is_absolute():
            raise GitSSHError("XDG_RUNTIME_DIR must be an absolute owner-only directory")
        info = base.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise GitSSHError("XDG_RUNTIME_DIR must be an owner-only directory (0700)")
        root = base / "djinn" / "git-agent"
    else:
        root = Path.home() / ".local" / "state" / "djinn" / "runtime" / "git-agent"
    if create:
        private_directory(root.parent)
        private_directory(root)
    return root


def process_token(pid: int) -> str | None:
    """Linux process birth identity prevents signalling an unrelated reused PID."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        fields = text.rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def stop_owned_process(pid: int, token: str) -> None:
    if process_token(pid) != token:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while process_token(pid) == token and time.monotonic() < deadline:
        time.sleep(0.05)
    if process_token(pid) == token:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def inspect_dev(name: str) -> tuple[str, bool, str] | None:
    result = subprocess.run(
        ["docker", "inspect", name],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        # A missing container is definitive; a daemon/control failure is unknown.
        if "No such" in result.stderr:
            return None
        raise GitSSHError("Docker inspection failed; Git agent observation is unavailable")
    try:
        data = json.loads(result.stdout)[0]
        return (
            str(data["Id"]),
            bool(data["State"]["Running"]),
            str(cast(dict[str, Any], data["Config"].get("Labels") or {}).get(GENERATION_LABEL, "")),
        )
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GitSSHError("invalid Docker inspection for Git agent observation") from exc


@dataclass
class GitRuntime:
    root: Path
    generation: str
    observer: subprocess.Popen[bytes] | None = None
    agent: subprocess.Popen[bytes] | None = None
    detached: bool = False
    owns_generation: bool = False

    def add_to_fragment(self, fragment: ComposeFragment) -> None:
        service = fragment["services"]["dev"]
        volumes = service.setdefault("volumes", [])
        for source, target in (
            (self.root / "public", SSH_TARGET),
            (self.root / "export", AGENT_TARGET),
        ):
            volumes.append(
                {
                    "type": "bind",
                    "source": str(source).replace("$", "$$"),
                    "target": str(target),
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            )
        service.setdefault("environment", {}).update(GIT_ENVIRONMENT)
        service["labels"] = {GENERATION_LABEL: self.generation}

    def retain(self) -> None:
        if self.observer is not None:
            # `up -d` completed; require the observer to have bound the actual ID.
            deadline = time.monotonic() + 10
            while not (self.root / "attached").exists():
                if self.observer.poll() is not None or time.monotonic() >= deadline:
                    raise GitSSHError("Git agent could not observe the new dev container")
                time.sleep(0.05)
        self.detached = True

    def close(self) -> None:
        if self.observer is not None:
            self.observer.terminate()
            try:
                self.observer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.observer.kill()
                self.observer.wait(timeout=3)
        if self.agent is not None:
            stop_agent(self.agent)
        lock_fd = os.open(self.root / "creator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # A replacement creator/observer owns the shared paths now.
            state_path = self.root / "state.json"
            if (
                state_path.exists()
                and json.loads(state_path.read_text())["generation"] != self.generation
            ):
                return
            for name in (
                "export/auth.sock",
                "private/agent.sock",
                "ready",
                "attached",
                "loaded",
                "state.json",
            ):
                (self.root / name).unlink(missing_ok=True)
        finally:
            os.close(lock_fd)


@contextlib.contextmanager
def git_runtime(config: AppConfig, container_name: str) -> Iterator[GitRuntime]:
    """Serialize preparation and hand pending creation to a detached host observer."""
    if os.getuid() != 1000:
        raise GitSSHError("Git agent socket requires host numeric UID 1000, matching the dev image")
    root = runtime_root(create=True)
    lock_fd = os.open(root / "creator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    runtime = GitRuntime(root, uuid.uuid4().hex)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GitSSHError(
                "another dev container creation is pending; retry after it completes"
            ) from exc
        state_path = root / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if process_token(state["observer_pid"]) == state["observer_token"]:
                raise GitSSHError(
                    "a dev container already owns the Git agent; clean it from the host first"
                )
            stop_owned_process(state["agent_pid"], state["agent_token"])
        runtime.owns_generation = True
        private_directory(root / "private")
        private_directory(root / "export")
        for name in ("export/auth.sock", "ready", "attached", "loaded", "state.json"):
            (root / name).unlink(missing_ok=True)
        delivery = read_public_delivery(config.git)
        write_public_delivery(root / "public", delivery)

        def observe_start(agent: subprocess.Popen[bytes]) -> None:
            runtime.agent = agent
            # Persist only public blobs and process identities in host-only storage.
            state = {
                "generation": runtime.generation,
                "agent_pid": agent.pid,
                "agent_token": process_token(agent.pid),
                "container_name": container_name,
                "creator_pid": os.getpid(),
                "creator_token": process_token(os.getpid()),
                "observer_pid": -1,
                "observer_token": "",
                "keys": [blob.hex() for blob in sorted(delivery.blobs)],
            }
            state_path.write_text(json.dumps(state))
            state_path.chmod(0o600)
            with (root / "observer.log").open("ab") as log:
                runtime.observer = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "djinn_in_a_box.core.host_runtime",
                        str(root),
                        str(lock_fd),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    start_new_session=True,
                    pass_fds=(lock_fd,),
                )

        runtime.agent = start_agent(
            config.git,
            root / "private" / "agent.sock",
            delivery.blobs,
            observe_start,
        )
        (root / "loaded").touch(mode=0o600)
        assert runtime.observer is not None
        deadline = time.monotonic() + 5
        while not (root / "ready").exists():
            if runtime.observer.poll() is not None or time.monotonic() >= deadline:
                raise GitSSHError(
                    "Git agent protocol filter failed to start; see host observer.log"
                )
            time.sleep(0.02)
        # The observer keeps the inherited flock until it observes the new ID or times out.
        os.close(lock_fd)
        lock_fd = -1
        yield runtime
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, GitSSHError):
            raise
        raise GitSSHError(f"Git agent preparation failed: {exc}") from exc
    finally:
        if lock_fd != -1:
            os.close(lock_fd)
        if runtime.owns_generation and not runtime.detached:
            runtime.close()


def observe(root: Path, lock_fd: int) -> None:
    state = json.loads((root / "state.json").read_text())
    state.update(observer_pid=os.getpid(), observer_token=process_token(os.getpid()))
    (root / "state.json").write_text(json.dumps(state))
    allowed = frozenset(bytes.fromhex(key) for key in state["keys"])
    server = AgentFilter(root / "export" / "auth.sock", root / "private" / "agent.sock", allowed)
    worker = threading.Thread(target=server.serve, daemon=True)
    stop = threading.Event()

    def stopping(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    worker.start()
    (root / "ready").touch(mode=0o600)
    deadline = time.monotonic() + 120
    loading = True
    dev_id: str | None = None
    try:
        while not stop.wait(0.25):
            if loading:
                if not (root / "loaded").exists():
                    if (
                        time.monotonic() >= deadline
                        or process_token(state["creator_pid"]) != state["creator_token"]
                    ):
                        break
                    continue
                loading = False
                deadline = time.monotonic() + STARTUP_SECONDS
            actual = inspect_dev(state["container_name"])
            if dev_id is None:
                if actual and actual[2] == state["generation"] and actual[1]:
                    dev_id = actual[0]
                    state["dev_id"] = dev_id
                    (root / "state.json").write_text(json.dumps(state))
                    (root / "attached").touch(mode=0o600)
                    os.close(lock_fd)
                    lock_fd = -1
                elif (
                    time.monotonic() >= deadline
                    or process_token(state["creator_pid"]) != state["creator_token"]
                ):
                    break
            elif actual is None or actual[0] != dev_id or not actual[1]:
                break
            if agent_keys(root / "private" / "agent.sock") != allowed:
                raise GitSSHError("dedicated Git agent key set changed")
    finally:
        server.stop.set()
        worker.join(timeout=1)
        stop_owned_process(state["agent_pid"], state["agent_token"])
        if lock_fd == -1:
            lock_fd = os.open(root / "creator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        for name in ("private/agent.sock", "ready", "attached", "loaded", "state.json"):
            (root / name).unlink(missing_ok=True)
        if lock_fd != -1:
            os.close(lock_fd)


if __name__ == "__main__":
    observe(Path(sys.argv[1]), int(sys.argv[2]))
