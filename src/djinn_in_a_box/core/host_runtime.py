"""Generation-owned dev creation, Git delivery and desktop helper observation."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pwd
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, cast

from djinn_in_a_box.core.console import warning
from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE
from djinn_in_a_box.core.git_agent import AgentFilter, agent_keys, start_agent, stop_agent
from djinn_in_a_box.core.ssh_delivery import (
    AGENT_TARGET,
    GIT_ENVIRONMENT,
    SSH_TARGET,
    GitSSHError,
    read_public_delivery,
    with_tailnet,
    write_public_delivery,
)

if TYPE_CHECKING:
    from djinn_in_a_box.config.models import AppConfig
    from djinn_in_a_box.core.docker import ComposeFragment

GENERATION_LABEL = "djinn.git-agent.generation"
STARTUP_SECONDS = 60
CONTAINER_USER_UID = 1000
"""Must match the ``USER_UID`` build argument used by the dev image."""


def private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise GitSSHError(f"Git runtime directory must be owned by you with mode 0700: {path}")
    return path


def runtime_root(*, create: bool = False) -> Path:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    root = home / ".local" / "state" / "djinn" / "runtime" / "git-agent"
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


def inspect_object(
    name: str,
    docker_path: str = DOCKER_EXECUTABLE,
    resource: str = "container",
    *,
    timeout: float = 5,
) -> dict[str, Any] | None:
    result = subprocess.run(
        [docker_path, resource, "inspect", name],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        cwd="/",
        stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        # The daemon reports "No such container/image: X" but "get X: no such volume".
        if "no such" in result.stderr.lower():
            return None
        raise GitSSHError("Docker inspection failed; runtime ownership is unknown")
    try:
        value = json.loads(result.stdout)[0]
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return cast(dict[str, Any], value)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GitSSHError("invalid Docker inspection for runtime observation") from exc


def inspect_dev(name: str, docker_path: str | None = None) -> tuple[str, bool, str] | None:
    data = inspect_object(name, docker_path or DOCKER_EXECUTABLE)
    if data is None:
        return None
    try:
        labels: dict[str, Any] = data["Config"].get("Labels") or {}
        return (
            str(data["Id"]),
            bool(data["State"]["Running"]),
            str(labels.get(GENERATION_LABEL, "")),
        )
    except (KeyError, TypeError) as exc:
        raise GitSSHError("invalid Docker dev ownership") from exc


def acquire_creation_lock(root: Path) -> int:
    fd = os.open(root / "creator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise GitSSHError(
            "another dev container creation is pending; retry after it completes"
        ) from exc
    return fd


@contextlib.contextmanager
def creation_guard(root: Path | None = None) -> Iterator[Path]:
    """One nonblocking guard for creators, observers and config-independent clean."""
    from djinn_in_a_box.core.hostctl import control_guard

    with control_guard():
        root = root or runtime_root(create=True)
        fd = acquire_creation_lock(root)
        try:
            yield root
        finally:
            os.close(fd)


def read_state(root: Path) -> dict[str, Any] | None:
    try:
        return json.loads((root / "state.json").read_text())
    except FileNotFoundError:
        return None


def _save(root: Path, state: dict[str, Any]) -> None:
    temporary = root / "state.tmp"
    from djinn_in_a_box.core.hostctl import control_guard

    with control_guard():
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        temporary.replace(root / "state.json")


def inspect_owned_resource(
    record: dict[str, Any], generation: str, docker_path: str
) -> dict[str, Any] | None:
    actual = inspect_object(record["id"], docker_path)
    if actual is None:
        return None
    labels: dict[str, Any] = actual.get("Config", {}).get("Labels") or {}
    if (
        actual.get("Id") != record["id"]
        or labels.get(GENERATION_LABEL) != generation
        or labels.get("com.docker.compose.project") != "djinn-in-a-box"
        or labels.get("com.docker.compose.service") != record["service"]
    ):
        raise GitSSHError("resource ownership changed; preserving it")
    return actual


def _command(docker_path: str, *args: str) -> None:
    result = subprocess.run(
        [docker_path, *args],
        capture_output=True,
        text=True,
        timeout=5,
        cwd="/",
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        raise GitSSHError(result.stderr.strip() or "Docker runtime operation failed")


def cleanup_owned(
    root: Path, generation: str, docker_path: str, *, terminate_dev: bool = False
) -> bool:
    """Called under the guard. Preserve replacements, pending owners and unknown state."""
    state = read_state(root)
    if state is None or state["generation"] != generation:
        return False
    actual = inspect_dev(state["container_name"], docker_path)
    if actual is not None:
        if actual[2] != generation or state.get("dev_id", actual[0]) != actual[0]:
            return False
        if actual[1] and not terminate_dev:
            return False
        if terminate_dev:
            _command(docker_path, "rm", "-f", actual[0])
            if inspect_dev(state["container_name"], docker_path) is not None:
                return False
    for record in state.get("resources", {}).values():
        if inspect_owned_resource(record, generation, docker_path) is not None:
            _command(docker_path, "rm", "-f", record["id"])
    for name in state.get("volumes", []):
        volume = inspect_object(name, docker_path, "volume")
        if volume is None:
            continue
        labels: dict[str, Any] = volume.get("Labels") or {}
        if labels.get(GENERATION_LABEL) != generation:
            raise GitSSHError(f"runtime volume ownership changed: {name}")
        users = subprocess.run(
            [docker_path, "ps", "-aq", "--filter", f"volume={name}"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=5,
            cwd="/",
            check=False,
        )
        if users.returncode or users.stdout.strip():
            raise GitSSHError(f"runtime volume still consumed or inspection failed: {name}")
        _command(docker_path, "volume", "rm", name)
    return True


def remove_runtime_volume(name: str, docker_path: str) -> None:
    """Under the guard, remove only a project-owned volume with no consumers."""
    volume = inspect_object(name, docker_path, "volume")
    if volume is None:
        return
    labels: dict[str, Any] = volume.get("Labels") or {}
    if labels.get("com.docker.compose.project") != "djinn-in-a-box" or not labels.get(
        GENERATION_LABEL
    ):
        raise GitSSHError(f"runtime volume ownership is unknown: {name}")
    result = subprocess.run(
        [docker_path, "ps", "-aq", "--filter", f"volume={name}"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=5,
        cwd="/",
        check=False,
    )
    if result.returncode or result.stdout.strip():
        raise GitSSHError(f"runtime volume consumed or inspection unavailable: {name}")
    _command(docker_path, "volume", "rm", name)


def clear_state(root: Path, generation: str) -> None:
    state = read_state(root)
    if state is None or state["generation"] != generation:
        return
    for name in (
        "private/agent.sock",
        "export/auth.sock",
        "ready",
        "attached",
        "loaded",
        "state.json",
    ):
        (root / name).unlink(missing_ok=True)


@dataclass
class GitRuntime:
    root: Path | None
    generation: str
    observer: subprocess.Popen[bytes] | None = None
    agent: subprocess.Popen[bytes] | None = None
    detached: bool = False
    owns_generation: bool = False
    git_enabled: bool = True
    ssh_enabled: bool = True
    docker_path: str = DOCKER_EXECUTABLE
    resources: dict[str, dict[str, str]] = field(
        default_factory=lambda: dict[str, dict[str, str]]()
    )

    lock_fd: int = -1
    handoff: threading.Thread | None = field(default=None, repr=False)
    creation_done: threading.Event = field(default_factory=threading.Event, repr=False)
    fd_guard: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def release_creator_lock(self) -> None:
        with self.fd_guard:
            if self.lock_fd != -1:
                os.close(self.lock_fd)
                self.lock_fd = -1

    def begin_creation(self) -> None:
        """Release the creator's inherited guard only after actual-ID attachment.

        The foreground Compose client can block for hours. Keeping this descriptor
        until client exit would prevent clean from stopping that session; giving it
        up during preparation would permit a second creator if the observer dies.
        """
        if self.root is None:
            return

        root = self.root

        def handoff() -> None:
            while not self.creation_done.wait(0.05):
                # A degraded desktop-only start still needs an actual-ID handoff.
                # The creator holds the guard while this thread records ownership.
                if self.observer is None or self.observer.poll() is not None:
                    try:
                        with self.fd_guard:
                            if self.lock_fd == -1:
                                return
                            state = read_state(root)
                            if state is None or state["generation"] != self.generation:
                                return
                            actual = inspect_dev(state["container_name"], self.docker_path)
                            if actual and actual[1] and actual[2] == self.generation:
                                state["dev_id"] = actual[0]
                                _save(root, state)
                                self.release_creator_lock()
                                return
                    except (GitSSHError, OSError, subprocess.SubprocessError):
                        continue  # Unknown dev state keeps the preparation guard.
                if (root / "attached").exists():
                    state = read_state(root)
                    if (
                        state is not None
                        and state["generation"] == self.generation
                        and state.get("dev_id")
                    ):
                        self.release_creator_lock()
                        return

        self.handoff = threading.Thread(target=handoff, daemon=True)
        self.handoff.start()

    def add_to_fragment(self, fragment: ComposeFragment) -> None:
        if self.root is None:
            return
        service = fragment["services"]["dev"]
        service.setdefault("labels", {})[GENERATION_LABEL] = self.generation
        if not self.ssh_enabled:
            return
        volumes = service.setdefault("volumes", [])
        mounts = [(self.root / "public", SSH_TARGET)]
        if self.git_enabled:
            mounts.append((self.root / "export", AGENT_TARGET))
        for source, target in mounts:
            volumes.append(
                {
                    "type": "bind",
                    "source": str(source).replace("$", "$$"),
                    "target": str(target),
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            )
        if self.git_enabled:
            service.setdefault("environment", {}).update(GIT_ENVIRONMENT)

    def register(self, service: str, actual: dict[str, Any], volume: str | None = None) -> None:
        assert self.root is not None
        state = read_state(self.root)
        if state is None or state["generation"] != self.generation:
            raise GitSSHError("runtime generation changed during preparation")
        record = {"id": actual["Id"], "service": service}
        labels: dict[str, Any] = actual.get("Config", {}).get("Labels") or {}
        if (
            labels.get(GENERATION_LABEL) != self.generation
            or labels.get("com.docker.compose.project") != "djinn-in-a-box"
            or labels.get("com.docker.compose.service") != service
        ):
            raise GitSSHError("new resource ownership is unverified")
        self.resources[service] = record
        state["resources"] = self.resources
        if volume is not None and volume not in state["volumes"]:
            state["volumes"].append(volume)
        _save(self.root, state)

    def register_volume(self, name: str, *, timeout: float = 5) -> None:
        assert self.root is not None
        state = read_state(self.root)
        actual = inspect_object(name, self.docker_path, "volume", timeout=timeout)
        if state is None or state["generation"] != self.generation:
            raise GitSSHError("runtime generation changed")
        if actual is None:
            return
        labels: dict[str, Any] = actual.get("Labels") or {}
        if labels.get(GENERATION_LABEL) != self.generation:
            raise GitSSHError("runtime volume ownership is unknown")
        if name not in state["volumes"]:
            state["volumes"].append(name)
            _save(self.root, state)

    def discard_volume(self, name: str) -> None:
        assert self.root is not None
        state = read_state(self.root)
        if state is None or state["generation"] != self.generation or name not in state["volumes"]:
            return
        volume = inspect_object(name, self.docker_path, "volume")
        if volume is not None:
            labels: dict[str, Any] = volume.get("Labels") or {}
            if labels.get(GENERATION_LABEL) != self.generation:
                raise GitSSHError("runtime volume ownership changed")
            result = subprocess.run(
                [self.docker_path, "ps", "-aq", "--filter", f"volume={name}"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=5,
                cwd="/",
                check=False,
            )
            if result.returncode or result.stdout.strip():
                raise GitSSHError("runtime volume consumed or inspection unavailable")
            _command(self.docker_path, "volume", "rm", name)
        state["volumes"].remove(name)
        _save(self.root, state)

    def forget(self, service: str) -> None:
        assert self.root is not None
        state = read_state(self.root)
        if state and state["generation"] == self.generation:
            self.resources.pop(service, None)
            state["resources"] = self.resources
            _save(self.root, state)

    def retain(self) -> None:
        if self.root is None:
            return
        deadline = time.monotonic() + 10
        if self.observer is None:
            if self.git_enabled:
                raise GitSSHError("dev observer is unavailable")
            with self.fd_guard:
                actual = inspect_dev("djinn", self.docker_path)
                if actual is None or actual[2] != self.generation:
                    raise GitSSHError("dev creation ownership is unverified")
                state = read_state(self.root)
                if state is None or state["generation"] != self.generation:
                    raise GitSSHError("runtime generation changed")
                if self.lock_fd != -1:
                    state["dev_id"] = actual[0]
                    _save(self.root, state)
                elif state.get("dev_id") != actual[0]:
                    raise GitSSHError("dev creation ownership is unverified")
                self.release_creator_lock()
                self.detached = True
                return
        while not (self.root / "attached").exists():
            if self.observer.poll() is not None or time.monotonic() >= deadline:
                raise GitSSHError("host observer could not attach to the new dev container")
            time.sleep(0.05)
        self.release_creator_lock()
        self.detached = True

    def close(self) -> None:
        if self.root is None or not self.owns_generation:
            return
        if self.observer is not None and self.observer.poll() is None:
            self.observer.terminate()
            try:
                self.observer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.observer.kill()
                self.observer.wait(timeout=3)
        if self.agent is not None:
            stop_agent(self.agent)
        try:
            with creation_guard(self.root):
                if cleanup_owned(self.root, self.generation, self.docker_path, terminate_dev=True):
                    clear_state(self.root, self.generation)
        except (GitSSHError, OSError, subprocess.SubprocessError) as exc:
            warning(f"Runtime cleanup preserved resources: {exc}")


@contextlib.contextmanager
def git_runtime(config: AppConfig, container_name: str) -> Iterator[GitRuntime]:
    """Always serialize dev creation; Git delivery is optional within that owner."""
    git_enabled = bool(config.git.identities)
    ssh_enabled = git_enabled or bool(config.hostctl.hosts)
    if ssh_enabled and os.getuid() != CONTAINER_USER_UID:
        raise GitSSHError(
            f"Generated SSH delivery requires host numeric UID {CONTAINER_USER_UID}, "
            "matching the dev image"
        )
    root = runtime_root(create=True)
    fd = acquire_creation_lock(root)
    runtime = GitRuntime(
        root,
        uuid.uuid4().hex,
        git_enabled=git_enabled,
        ssh_enabled=ssh_enabled,
        docker_path=DOCKER_EXECUTABLE,
        lock_fd=fd,
    )
    try:
        # Inspect before any stale resource mutation, including without Git identities.
        actual = inspect_dev(container_name, runtime.docker_path)
        previous = read_state(root)
        # A host or Docker restart leaves the recorded dev stopped and its observer gone;
        # reclaim exactly that generation like `djinn clean` would. A running or foreign dev
        # keeps the refusal, and a surviving observer refuses below.
        stale_dev = (
            actual is not None
            and not actual[1]
            and previous is not None
            and actual[2] == previous["generation"]
        )
        if actual is not None and not stale_dev:
            raise GitSSHError(
                "a dev container already owns the runtime; clean it from the host first"
            )
        if previous is not None:
            if process_token(previous["observer_pid"]) == previous["observer_token"]:
                raise GitSSHError(
                    "a dev container already owns the runtime; clean it from the host first"
                )
            if not cleanup_owned(
                root, previous["generation"], runtime.docker_path, terminate_dev=stale_dev
            ):
                raise GitSSHError("previous runtime ownership is unknown")
            if previous.get("agent_pid", -1) > 0:
                stop_owned_process(previous["agent_pid"], previous["agent_token"])
            clear_state(root, previous["generation"])
        runtime.owns_generation = True
        for name in ("ready", "attached", "loaded"):
            (root / name).unlink(missing_ok=True)
        blobs: frozenset[bytes] = frozenset()
        if git_enabled:
            private_directory(root / "private")
            private_directory(root / "export")
            (root / "export/auth.sock").unlink(missing_ok=True)
        if ssh_enabled:
            from djinn_in_a_box.core.hostctl import cached_trust, control_guard

            delivery = read_public_delivery(config.git)
            blobs = delivery.blobs
            with control_guard():
                write_public_delivery(
                    root / "public", with_tailnet(delivery, config.hostctl, cached_trust())
                )
        state: dict[str, Any] = {
            "generation": runtime.generation,
            "container_name": container_name,
            "creator_pid": os.getpid(),
            "creator_token": process_token(os.getpid()),
            "observer_pid": -1,
            "observer_token": "",
            "agent_pid": -1,
            "agent_token": "",
            "keys": [blob.hex() for blob in sorted(blobs)],
            "git_enabled": git_enabled,
            "hostctl_hosts": {
                name: host.model_dump() for name, host in config.hostctl.hosts.items()
            },
            "docker_path": runtime.docker_path,
            "resources": {},
            "volumes": [],
        }

        def observe_start(agent: subprocess.Popen[bytes] | None = None) -> None:
            runtime.agent = agent
            if agent is not None:
                state.update(agent_pid=agent.pid, agent_token=process_token(agent.pid))
            _save(root, state)
            with (root / "observer.log").open("ab") as log:
                runtime.observer = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "djinn_in_a_box.core.host_runtime",
                        str(root),
                        str(fd),
                        runtime.docker_path,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    start_new_session=True,
                    pass_fds=(fd,),
                    cwd="/",
                )

        if git_enabled:
            runtime.agent = start_agent(
                config.git, root / "private/agent.sock", blobs, observe_start
            )
        else:
            try:
                observe_start()
            except OSError as exc:
                warning(f"Desktop observer failed: {exc}; desktop endpoints will be omitted")
        (root / "loaded").touch(mode=0o600)
        deadline = time.monotonic() + 5
        while runtime.observer is not None and not (root / "ready").exists():
            if runtime.observer.poll() is not None or time.monotonic() >= deadline:
                if git_enabled:
                    raise GitSSHError(
                        "Git agent protocol filter failed to start; see host observer.log"
                    )
                warning("Desktop observer failed; desktop endpoints will be omitted")
                runtime.observer.terminate()
                runtime.observer.wait(timeout=5)
                runtime.observer = None
                break
            time.sleep(0.02)
        # Both creator and observer keep this flock during preparation. Attachment
        # releases the creator copy via begin_creation/retain while dev runs.
        yield runtime
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, GitSSHError):
            raise
        raise GitSSHError(f"Host runtime preparation failed: {exc}") from exc
    finally:
        runtime.creation_done.set()
        if runtime.handoff is not None:
            runtime.handoff.join(timeout=1)
        runtime.release_creator_lock()
        if runtime.owns_generation and not runtime.detached:
            runtime.close()


def observe(root: Path, lock_fd: int, docker_path: str) -> None:
    if not Path(docker_path).is_absolute():
        raise GitSSHError("observer requires a pinned absolute Docker executable")
    state = read_state(root)
    assert state is not None
    generation = state["generation"]
    state.update(observer_pid=os.getpid(), observer_token=process_token(os.getpid()))
    _save(root, state)
    allowed = frozenset(bytes.fromhex(key) for key in state["keys"])
    server = None
    worker = None
    if state["git_enabled"]:
        server = AgentFilter(root / "export/auth.sock", root / "private/agent.sock", allowed)
        worker = threading.Thread(target=server.serve, daemon=True)
        worker.start()
    stop = threading.Event()

    def stopping(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    (root / "ready").touch(mode=0o600)
    deadline = time.monotonic() + 120
    loading = True
    dev_id = None
    running = True
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
            try:
                actual = inspect_dev(state["container_name"], docker_path)
            except (GitSSHError, OSError, subprocess.SubprocessError):
                continue  # Unknown state never authorizes cleanup.
            if dev_id is None:
                if actual and actual[2] == generation and actual[1]:
                    dev_id = actual[0]
                    fresh = read_state(root)
                    if fresh is None or fresh["generation"] != generation:
                        break
                    fresh["dev_id"] = dev_id
                    _save(root, fresh)
                    (root / "attached").touch(mode=0o600)
                    os.close(lock_fd)
                    lock_fd = -1
                elif (
                    time.monotonic() >= deadline
                    or process_token(state["creator_pid"]) != state["creator_token"]
                ):
                    break
            else:
                with creation_guard(root):
                    fresh = read_state(root)
                    actual = inspect_dev(state["container_name"], docker_path)
                    if fresh is None or fresh["generation"] != generation:
                        break
                    if actual is None or actual[0] != dev_id or actual[2] != generation:
                        break
                    if actual[1] != running:
                        for service, record in fresh["resources"].items():
                            if service.endswith("-helper") and inspect_owned_resource(
                                record, generation, docker_path
                            ):
                                _command(
                                    docker_path, "start" if actual[1] else "stop", record["id"]
                                )
                        running = actual[1]
                        if not running and server is not None:
                            server.stop.set()
                            stop_owned_process(state["agent_pid"], state["agent_token"])
                            server = None
                        if not running and not fresh["resources"]:
                            break  # Preserve Git-only release-on-stop semantics.
            if server is not None and agent_keys(root / "private/agent.sock") != allowed:
                raise GitSSHError("dedicated Git agent key set changed")
    finally:
        if server is not None:
            server.stop.set()
        if worker is not None:
            worker.join(timeout=1)
        if state["agent_pid"] > 0:
            stop_owned_process(state["agent_pid"], state["agent_token"])
        if lock_fd != -1:
            os.close(lock_fd)
        try:
            with creation_guard(root):
                if cleanup_owned(root, generation, docker_path):
                    clear_state(root, generation)
                else:
                    # A stopped Git-only dev keeps no runtime resources.
                    fresh = read_state(root)
                    inspect_dev(state["container_name"], docker_path)
                    if fresh and fresh["generation"] == generation and not fresh["resources"]:
                        clear_state(root, generation)
        except (GitSSHError, OSError, subprocess.SubprocessError) as exc:
            warning(f"Observer cleanup preserved resources: {exc}")


def start_hostctl_observer(helper_id: str, generation: str, docker_path: str) -> None:
    """The same detached observer entry point also serves standalone windows."""
    from djinn_in_a_box.core.hostctl import state_root

    with (state_root() / "observer.log").open("ab") as log:
        subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "djinn_in_a_box.core.host_runtime",
                "--hostctl",
                helper_id,
                generation,
                docker_path,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,
            cwd="/",
        )


def observe_hostctl(helper_id: str, generation: str, docker_path: str) -> None:
    """Enroll and report; helper PID 1 remains the only expiry timer."""
    from djinn_in_a_box.core import hostctl

    if not Path(docker_path).is_absolute():
        raise GitSSHError("observer requires a pinned absolute Docker executable")
    hostctl.DOCKER_EXECUTABLE = docker_path
    hostctl.HELPER_NAME = helper_id
    root = hostctl.state_root()
    enrollment = None
    dev_id = None
    admitted = False
    try:
        while True:
            helper = hostctl.inspect_helper()
            if (
                helper is None
                or helper["Id"] != helper_id
                or hostctl.generation(helper) != generation
            ):
                return
            if not helper["State"]["Running"]:
                with hostctl.control_guard():
                    fresh = hostctl.inspect_helper()
                    if fresh and fresh["Id"] == helper_id:
                        hostctl.reconcile(fresh)
                return
            observation = {
                "generation": generation,
                "observer_pid": os.getpid(),
                "observer_token": process_token(os.getpid()),
                "time": time.time(),
            }
            node: dict[str, Any] = {}
            try:
                node = hostctl.node_status(helper)
                observation["node_state"] = node["BackendState"]
                if enrollment is None:
                    enrollment = subprocess.Popen(
                        [
                            docker_path,
                            "exec",
                            helper_id,
                            "/usr/local/bin/tailscale",
                            "--socket=" + hostctl.TAILSCALE_SOCKET,
                            "up",
                            "--reset",
                            "--hostname=" + hostctl.machine_name(),
                            "--accept-dns=false",
                            "--accept-routes=false",
                            "--ssh=false",
                            "--timeout=30s",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        cwd="/",
                    )
                if enrollment.poll() is not None:
                    observation["enrollment_exit"] = enrollment.returncode
                if not admitted and enrollment.poll() == 0 and node["BackendState"] == "Running":
                    trust = hostctl.prepare_trust(generation, node)
                    with hostctl.control_guard():
                        fresh = hostctl.inspect_helper()
                        if (
                            fresh is None
                            or fresh["Id"] != helper_id
                            or hostctl.generation(fresh) != generation
                            or not fresh["State"]["Running"]
                        ):
                            return
                        hostctl.admit_locked(fresh, trust)
                        admitted = True
                    observation["relay"] = "open"
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                observation["error"] = str(exc)
                if (
                    enrollment is not None
                    and enrollment.poll() == 0
                    and node.get("BackendState") == "Running"
                ):
                    with hostctl.control_guard():
                        fresh = hostctl.inspect_helper()
                        if (
                            fresh
                            and fresh["Id"] == helper_id
                            and hostctl.generation(fresh) == generation
                        ):
                            hostctl.control_journal(
                                "relay-refused", generation=generation, reason=str(exc)
                            )
                            hostctl.stop_helper_locked()
                    return
            actual = inspect_dev("djinn", docker_path)
            if actual and actual[1] and dev_id is None:
                dev_id = actual[0]
            if dev_id is not None and (actual is None or not actual[1] or actual[0] != dev_id):
                with hostctl.control_guard():
                    fresh = hostctl.inspect_helper()
                    if fresh and fresh["Id"] == helper_id:
                        hostctl.stop_helper_locked()
                        hostctl.journal("external-dev-teardown", generation=generation)
                return
            temporary = root / f"observation-{generation}.tmp"
            temporary.write_text(json.dumps(observation))
            temporary.chmod(0o600)
            with hostctl.control_guard():
                fresh = hostctl.inspect_helper()
                if (
                    fresh is None
                    or fresh["Id"] != helper_id
                    or hostctl.generation(fresh) != generation
                    or not fresh["State"]["Running"]
                ):
                    temporary.unlink(missing_ok=True)
                    return
                temporary.replace(root / "observation.json")
            time.sleep(0.5)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        warning(f"Hostctl observation gap: {exc}")
        with hostctl.control_guard():
            fresh = hostctl.inspect_helper()
            if fresh and fresh["Id"] == helper_id:
                hostctl.stop_helper_locked()
    finally:
        if enrollment is not None and enrollment.poll() is None:
            enrollment.terminate()
            try:
                enrollment.wait(timeout=3)
            except subprocess.TimeoutExpired:
                enrollment.kill()
                enrollment.wait(timeout=3)


if __name__ == "__main__":
    if sys.argv[1] == "--hostctl":
        observe_hostctl(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        observe(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3])
