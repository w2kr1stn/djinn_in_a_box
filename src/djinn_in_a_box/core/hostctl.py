"""Host-only control of the helper; deadline authority stays inside PID 1."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import pwd
import socket
import stat
import subprocess
import tarfile
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.ssh import duration_minutes
from djinn_in_a_box.config.volumes import HOSTCTL_STATE_VOLUME
from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE

HELPER_NAME = "djinn-hostctl"
HELPER_NETWORK = "djinn-network"
HELPER_IMAGE = (
    "tailscale/tailscale:v1.102.5@"
    "sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065"
)
GENERATION_LABEL = "djinn.hostctl.generation"
SUPERVISOR = "/djinn-hostctl-supervisor"
TAILSCALE_SOCKET = "/run/djinn-hostctl/tailscaled.sock"
WINDOW_PATH = "/var/lib/tailscale/djinn-hostctl/window.json"
STOP_SECONDS = 3
JOURNAL_BYTES = 1024 * 1024
_held = threading.local()


class HostctlError(RuntimeError):
    pass


def state_root(*, create: bool = True) -> Path:
    from djinn_in_a_box.core.host_runtime import private_directory

    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    state = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local/state")))
    if not state.is_absolute():
        raise HostctlError("XDG_STATE_HOME must be an absolute host path")
    root = state / "djinn/hostctl"
    if create:
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = root.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise HostctlError("Djinn state parent must be owned by you and not writable by others")
        private_directory(root)
    return root


@contextlib.contextmanager
def control_guard() -> Iterator[None]:
    """Short, reentrant transition lock shared with dev transitions and clean."""
    if getattr(_held, "active", False):
        yield
        return
    root = state_root()
    fd = os.open(root / "control.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = None
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is None:
                    deadline = time.monotonic() + 15
                if time.monotonic() >= deadline:
                    raise HostctlError(
                        "host control is busy; retry after the transition completes"
                    ) from None
                time.sleep(0.05)
        _held.active = True
        yield
    finally:
        _held.active = False
        os.close(fd)


def command(*args: str, timeout: float = 5) -> str:
    from djinn_in_a_box.core.docker import _run_captured  # pyright: ignore[reportPrivateUsage]

    result = _run_captured([DOCKER_EXECUTABLE, *args], cwd=Path("/"), timeout=timeout)
    if not result.success:
        # Docker errors may contain auth URLs; they never enter a journal or log.
        raise HostctlError(f"Docker {args[0]} failed (exit {result.returncode})")
    return result.stdout


def inspect_helper() -> dict[str, Any] | None:
    from djinn_in_a_box.core.host_runtime import inspect_object

    value = inspect_object(HELPER_NAME, DOCKER_EXECUTABLE)
    if value is not None:
        labels: dict[str, Any] = value.get("Config", {}).get("Labels") or {}
        if not labels.get(GENERATION_LABEL):
            raise HostctlError("helper ownership is unknown; preserving the container")
    return value


def generation(helper: dict[str, Any]) -> str:
    return str(helper["Config"]["Labels"][GENERATION_LABEL])


def journal(event: str, **fields: Any) -> None:
    root = state_root()
    fd = os.open(root / "journal.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        path = root / "journal.jsonl"
        if path.exists() and path.stat().st_size >= JOURNAL_BYTES:
            for index in (2, 1):
                source = root / f"journal.jsonl.{index}"
                if source.exists():
                    source.replace(root / f"journal.jsonl.{index + 1}")
            path.replace(root / "journal.jsonl.1")
        out = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(out)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise HostctlError("journal must be an owner-only regular file")
            data = json.dumps({"time": datetime.now(UTC).isoformat(), "event": event, **fields})
            with os.fdopen(out, "a", closefd=False) as stream:
                stream.write(data + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(out)
    finally:
        os.close(fd)


def reconcile(helper: dict[str, Any]) -> None:
    """Retain exact helper expiry events, deduplicated under the control lock."""
    root = state_root()
    gen = generation(helper)
    marker = root / "observed-expiry.json"
    seen: list[str] = json.loads(marker.read_text()) if marker.exists() else []
    if gen in seen:
        return
    logs = command("logs", "--tail", "100", str(helper["Id"]))
    for line in logs.splitlines():
        try:
            raw_event: Any = json.loads(line)
            if not isinstance(raw_event, dict):
                continue
            event = cast(dict[str, Any], raw_event)
        except ValueError:
            continue
        if event.get("event") == "expiry" and event.get("generation") == gen:
            # Marker written only after durable append. Journal scanning closes the
            # append/marker crash gap; never infer an expiry timestamp from exit.
            rows = (root / "journal.jsonl").read_text() if (root / "journal.jsonl").exists() else ""
            if not any(
                row.get("event") == "expiry" and row.get("generation") == gen
                for row in (json.loads(line) for line in rows.splitlines())
            ):
                journal(
                    "expiry",
                    generation=gen,
                    deadline=event.get("deadline"),
                    helper_time=event.get("time"),
                    state_error=event.get("state_error"),
                )
            temporary = root / "observed-expiry.tmp"
            temporary.write_text(json.dumps([*seen[-99:], gen]))
            temporary.chmod(0o600)
            temporary.replace(marker)
            return


def control_journal(event: str, **fields: Any) -> None:
    """A control-side logging failure requests closure, even during off/limit."""
    try:
        journal(event, **fields)
    except (OSError, RuntimeError):
        stop_helper_locked()
        raise


def supervisor_path() -> Path:
    path = state_root() / "bin/supervisor"
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise HostctlError(
            "Build and install Dockerfile.hostctl-helper first; see CONTRIBUTING.md"
        ) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HostctlError("supervisor must be an owner-only regular file in protected storage")
    if not info.st_mode & stat.S_IXUSR:
        raise HostctlError("supervisor is not executable")
    from djinn_in_a_box.core.host_runtime import private_directory

    private_directory(path.parent)
    return path


def install_supervisor(image: str = "djinn-hostctl-supervisor:1") -> Path:
    """Extract a built static executable; runtime always uses the official image."""
    from djinn_in_a_box.core.host_runtime import private_directory

    with control_guard():
        root = private_directory(state_root() / "bin")
        temporary = root / "supervisor.tmp"
        name = "djinn-hostctl-extract-" + uuid.uuid4().hex
        container = command("create", "--name", name, image, SUPERVISOR).strip()
        try:
            command("cp", f"{container}:{SUPERVISOR}", str(temporary))
            temporary.chmod(0o500)
            temporary.replace(root / "supervisor")
        finally:
            command("rm", container)
            temporary.unlink(missing_ok=True)
        return supervisor_path()


def run_argv(binary: Path, gen: str, deadline: datetime, boot_deadline: int) -> list[str]:
    if not binary.is_absolute() or any(char in str(binary) for char in ",\r\n\x00"):
        raise HostctlError("supervisor bind source must be a literal absolute Docker host path")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return [
        DOCKER_EXECUTABLE,
        "run",
        "--detach",
        "--name",
        HELPER_NAME,
        "--label",
        f"{GENERATION_LABEL}={gen}",
        "--network",
        HELPER_NETWORK,
        "--network-alias",
        "djinn-hostctl",
        "--restart",
        "no",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
        "--mount",
        f"type=volume,src={HOSTCTL_STATE_VOLUME},dst=/var/lib/tailscale",
        "--mount",
        f"type=bind,src={binary},dst={SUPERVISOR},readonly",
        "--entrypoint",
        SUPERVISOR,
        HELPER_IMAGE,
        "run",
        "--generation",
        gen,
        "--boot-id",
        boot_id,
        "--deadline",
        deadline.isoformat().replace("+00:00", "Z"),
        "--boottime-deadline",
        str(boot_deadline),
    ]


def read_window(helper: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        [DOCKER_EXECUTABLE, "cp", f"{helper['Id']}:{WINDOW_PATH}", "-"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=5,
        cwd="/",
    )
    if result.returncode:
        raise HostctlError("helper window state is unavailable")
    try:
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            members = archive.getmembers()
            if len(members) != 1 or not members[0].isfile() or members[0].size > 4096:
                raise ValueError("invalid state archive")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise ValueError("missing window")
            raw_value: Any = json.load(stream)
            if not isinstance(raw_value, dict):
                raise ValueError("window must be an object")
            value = cast(dict[str, Any], raw_value)
        if value.get("generation") != generation(helper):
            raise ValueError("generation mismatch")
        datetime.fromisoformat(value["deadline"])
        int(value["boottime_deadline_ns"])
        return value
    except (ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        raise HostctlError("helper window state is malformed") from exc


def open_window(
    config: AppConfig, duration: str | None = None, *, allow_unsealed: bool = False
) -> str:
    from djinn_in_a_box.core import docker, host_runtime

    minutes = duration_minutes(config.hostctl.default_duration if duration is None else duration)
    if not config.hostctl.hosts:
        raise HostctlError("Declare at least one [hostctl.hosts.<name>] with address and user")
    binary = supervisor_path()
    deadline = datetime.now(UTC) + timedelta(minutes=minutes)
    boot_deadline = time.clock_gettime_ns(time.CLOCK_BOOTTIME) + minutes * 60 * 10**9
    with control_guard():
        helper = inspect_helper()
        if helper is not None and helper["State"]["Running"]:
            control_journal("on-refused", generation=generation(helper), reason="already open")
            raise HostctlError("Window is already open; use djinn hostctl limit <minutes>")
        control_journal("on-attempt", allow_unsealed=allow_unsealed)
        if helper is not None:
            reconcile(helper)
            command("rm", str(helper["Id"]))
        if not docker.ensure_network(HELPER_NETWORK):
            journal("on-refused", reason="network ensure failed")
            raise HostctlError("Could not ensure the helper network")
        if host_runtime.inspect_object(HELPER_NETWORK, DOCKER_EXECUTABLE, "network") is None:
            journal("on-refused", reason="network verification failed")
            raise HostctlError("Helper network could not be verified")
        gen = uuid.uuid4().hex
        argv = run_argv(binary, gen, deadline, boot_deadline)
        command(*argv[1:], timeout=15)
        helper = inspect_helper()
        if helper is None or generation(helper) != gen:
            raise HostctlError("Helper creation could not be verified")
        try:
            journal(
                "on",
                generation=gen,
                deadline=deadline.isoformat(),
                allow_unsealed=allow_unsealed,
                sealing="unchecked (B1)",
                relay="absent (B1)",
            )
            host_runtime.start_hostctl_observer(str(helper["Id"]), gen, DOCKER_EXECUTABLE)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            stop_helper_locked()
            raise
    return gen


def stop_helper_locked(*, remove: bool = False) -> None:
    helper = inspect_helper()
    if helper is None:
        return
    identity = str(helper["Id"])
    gen = generation(helper)
    if helper["State"]["Running"]:
        command("stop", "--time", str(STOP_SECONDS), identity, timeout=STOP_SECONDS + 5)
    current = inspect_helper()
    if current is None or current["Id"] != identity or generation(current) != gen:
        raise HostctlError("Helper changed during teardown; preserving resources")
    if current["State"]["Running"]:
        raise HostctlError("Helper termination could not be verified")
    reconcile(current)
    if remove:
        command("rm", identity)
        if inspect_helper() is not None:
            raise HostctlError("Helper removal could not be verified")


def close_window() -> None:
    with control_guard():
        helper = inspect_helper()
        control_journal("off-attempt", generation=generation(helper) if helper else None)
        try:
            stop_helper_locked()
        except (RuntimeError, OSError, subprocess.SubprocessError):
            journal("off-failed", generation=generation(helper) if helper else None)
            raise
        journal("off", generation=generation(helper) if helper else None)


def limit_window(minutes: int) -> dict[str, Any]:
    if not 1 <= minutes <= 1440:
        raise HostctlError("minutes must be a positive integer up to 1440")
    with control_guard():
        control_journal("limit-attempt", minutes=minutes)
        helper = inspect_helper()
        if helper is None or not helper["State"]["Running"]:
            journal("limit-refused", reason="closed")
            raise HostctlError("Window is closed; use djinn hostctl on")
        gen = generation(helper)
        output = command("exec", str(helper["Id"]), SUPERVISOR, "set-deadline", gen, str(minutes))
        try:
            raw_value: Any = json.loads(output)
            if not isinstance(raw_value, dict):
                raise ValueError("helper response must be an object")
            value = cast(dict[str, Any], raw_value)
            if value.get("generation") != gen or value.get("closed") is not False:
                raise ValueError("invalid acknowledgement")
            datetime.fromisoformat(value["deadline"])
            int(value["boottime_deadline_ns"])
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HostctlError("Helper did not acknowledge the effective deadline") from exc
        try:
            journal("limit", generation=gen, deadline=value["deadline"], minutes=minutes)
        except (OSError, RuntimeError):
            stop_helper_locked()
            raise
        return value


def node_status(helper: dict[str, Any]) -> dict[str, Any]:
    output = command(
        "exec",
        str(helper["Id"]),
        "/usr/local/bin/tailscale",
        "--socket=" + TAILSCALE_SOCKET,
        "status",
        "--json",
    )
    raw_value: Any = json.loads(output)
    if not isinstance(raw_value, dict):
        raise HostctlError("node response must be an object")
    value = cast(dict[str, Any], raw_value)
    if not isinstance(value.get("BackendState"), str):
        raise HostctlError("node state is malformed")
    return value


def snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": "closed",
        "sealing": "unchecked (B1)",
        "relay": "absent (B1); dev has no helper tailnet route",
    }
    helper = inspect_helper()
    if helper is None:
        result["helper"] = "absent"
        result["node"] = "unknown (helper is off)"
        return result
    result["helper"] = str(helper["Id"])
    result["generation"] = generation(helper)
    result["running"] = helper["State"]["Running"]
    try:
        result["window"] = read_window(helper)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result["window_error"] = str(exc)
    if helper["State"]["Running"]:
        result["state"] = "opening"
        try:
            result["node"] = node_status(helper)
            if result["node"]["BackendState"] == "Running":
                result["state"] = "open"
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            result["node_error"] = str(exc)
    else:
        result["node"] = "unknown (helper is off)"
    observation = state_root(create=False) / "observation.json"
    if observation.exists():
        value = json.loads(observation.read_text())
        if value.get("generation") == generation(helper):
            result["observation"] = value
    if helper["State"]["Running"]:
        from djinn_in_a_box.core.host_runtime import process_token

        value: dict[str, Any] = result.get("observation") or {}
        result["observation_gap"] = (
            not value
            or time.time() - value.get("time", 0) > 10
            or process_token(value.get("observer_pid", -1)) != value.get("observer_token")
        )
    return result


def delete_state_locked() -> None:
    """Only all-clean may call this, after verified helper teardown/removal."""
    from djinn_in_a_box.core.host_runtime import inspect_object

    stop_helper_locked(remove=True)
    if inspect_object(HOSTCTL_STATE_VOLUME, DOCKER_EXECUTABLE, "volume") is not None:
        command("volume", "rm", HOSTCTL_STATE_VOLUME)
        if inspect_object(HOSTCTL_STATE_VOLUME, DOCKER_EXECUTABLE, "volume") is not None:
            raise HostctlError("Protected state deletion could not be verified")


def machine_name() -> str:
    import re

    name = re.sub(r"[^a-z0-9-]", "-", socket.gethostname().lower()).strip("-")[:56]
    return "djinn-" + (name or "machine")
