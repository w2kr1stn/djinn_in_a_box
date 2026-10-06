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
from typing import TYPE_CHECKING, Any, cast

from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.ssh import HostctlConfig, duration_minutes
from djinn_in_a_box.config.volumes import HOSTCTL_STATE_VOLUME
from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE

if TYPE_CHECKING:
    from djinn_in_a_box.core.docker import RunResult

HELPER_NAME = "djinn-hostctl"
HELPER_NETWORK = "djinn-network"
HELPER_IMAGE = (
    "tailscale/tailscale:v1.102.5@"
    "sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065"
)
GENERATION_LABEL = "djinn.hostctl.generation"
SUPERVISOR = "/djinn-hostctl-supervisor"
SUPERVISOR_IMAGE = "djinn-hostctl-supervisor:1"
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
        raise HostctlError("hostctl supervisor is not installed; run djinn build") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise HostctlError("supervisor must be an owner-only regular file in protected storage")
    if not info.st_mode & stat.S_IXUSR:
        raise HostctlError("supervisor is not executable")
    from djinn_in_a_box.core.host_runtime import private_directory

    private_directory(path.parent)
    return path


def build_supervisor(*, no_cache: bool = False) -> RunResult:
    """Build the static supervisor for the host architecture, streaming the log.

    It is no Compose service, so bake does not see it. BuildKit derives TARGETARCH
    and TARGETVARIANT from the build platform.
    """
    from djinn_in_a_box.core.docker import (
        _build_progress,  # pyright: ignore[reportPrivateUsage]
        _run_streamed,  # pyright: ignore[reportPrivateUsage]
    )
    from djinn_in_a_box.core.paths import get_project_root

    cmd = [DOCKER_EXECUTABLE, "buildx", "build", "--progress", _build_progress(), "--load"]
    if no_cache:
        cmd.append("--no-cache")
    cmd.extend(["-f", "Dockerfile.hostctl-helper", "-t", SUPERVISOR_IMAGE, "."])
    return _run_streamed(cmd, cwd=get_project_root())


def install_supervisor(image: str = SUPERVISOR_IMAGE) -> Path:
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


def save_private(name: str, value: dict[str, Any]) -> None:
    root = state_root()
    temporary = root / (name + ".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(root / name)


def cached_trust() -> dict[str, Any] | None:
    path = state_root(create=False) / "trust.json"
    if not path.exists():
        return None
    raw: Any = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise HostctlError("cached peer trust is malformed")
    value = cast(dict[str, Any], raw)
    if not value.get("peers") or not value.get("generation"):
        raise HostctlError("cached peer trust is malformed")
    return value


def prepare_trust(gen: str, node: dict[str, Any]) -> dict[str, Any]:
    from djinn_in_a_box.core.ssh_delivery import peer_snapshot

    opening = json.loads((state_root() / "opening.json").read_text())
    if opening.get("generation") != gen:
        raise HostctlError("opening generation changed")
    return peer_snapshot(HostctlConfig.model_validate(opening["config"]), node, gen)


def verify_dev(dev_id: str | None) -> None:
    from djinn_in_a_box.core.host_runtime import inspect_dev

    actual = inspect_dev("djinn", DOCKER_EXECUTABLE)
    current = actual[0] if actual and actual[1] else None
    if current != dev_id:
        raise HostctlError("dev was removed or replaced during assessment")


def admission_assessment(trust: dict[str, Any]) -> dict[str, Any] | None:
    """Slow checks outside the control lock; commit verifies ID and transition."""
    from djinn_in_a_box.core.host_sealing import inspect_assessment, require_blocked, run_probe

    opening = json.loads((state_root() / "opening.json").read_text())
    if opening["generation"] != trust["generation"]:
        raise HostctlError("opening generation changed")
    assessment = inspect_assessment()
    creator = opening.get("creator")
    if creator:
        actual = host_runtime_dev()
        if actual is None or not actual[1]:
            return None
        if actual[2] != creator:
            raise HostctlError("external dev replaced pending creation")
    elif opening.get("dev_id") != assessment.dev_id:
        raise HostctlError("dev was removed or replaced during window opening")
    assessment.require(allow_unsealed=bool(opening.get("allow_unsealed")) and not creator)
    rows = run_probe(assessment.dev_id, trust) if assessment.dev_id else []
    require_blocked(rows)
    return {
        "generation": trust["generation"],
        "dev_id": assessment.dev_id,
        "creator": creator,
        "sealing": assessment.state,
        "causes": assessment.causes,
        "probe": rows,
    }


def host_runtime_dev() -> tuple[str, bool, str] | None:
    from djinn_in_a_box.core.host_runtime import inspect_dev

    return inspect_dev("djinn", DOCKER_EXECUTABLE)


def guard_dev_start(planned: dict[str, Any], creator: str) -> None:
    """Close before unsealed creation; sealed creation stays paused until delivery checks."""
    from djinn_in_a_box.core.console import warning
    from djinn_in_a_box.core.host_sealing import assess

    checked = assess(planned)
    with control_guard():
        helper = inspect_helper()
        if helper is None or not helper["State"]["Running"]:
            return
        if checked.causes or checked.errors:
            reason = "; ".join((*checked.causes, *checked.errors))
            control_journal("dev-start-unsealed", generation=generation(helper), reason=reason)
            stop_helper_locked()
            journal("dev-start-closed", generation=generation(helper), reason=reason)
            warning(f"Hostctl window closed before unsealed dev start: {reason}")
            return
        opening = json.loads((state_root() / "opening.json").read_text())
        if opening["generation"] != generation(helper):
            raise HostctlError("dev start window generation changed")
        reply = json.loads(
            command("exec", str(helper["Id"]), SUPERVISOR, "pause", generation(helper))
        )
        if (
            reply.get("generation") != generation(helper)
            or reply.get("paused") is not True
            or reply.get("closed")
        ):
            stop_helper_locked()
            raise HostctlError("helper did not acknowledge admission pause")
        opening.update(creator=creator, allow_unsealed=False)
        save_private("opening.json", opening)
        journal("dev-start-paused", generation=generation(helper), creator=creator)


def resume_locked(helper: dict[str, Any]) -> None:
    journal("dev-start-ready", generation=generation(helper))
    reply = json.loads(command("exec", str(helper["Id"]), SUPERVISOR, "resume", generation(helper)))
    if (
        reply.get("generation") != generation(helper)
        or reply.get("paused") is not False
        or reply.get("closed")
    ):
        raise HostctlError("helper did not acknowledge admission resume")


def commit_assessment(checked: dict[str, Any]) -> None:
    verify_dev(checked["dev_id"])
    opening = json.loads((state_root() / "opening.json").read_text())
    if (
        opening["generation"] != checked["generation"]
        or opening.get("creator") != checked["creator"]
    ):
        raise HostctlError("dev transition changed during assessment")
    if checked["creator"]:
        actual = host_runtime_dev()
        if actual is None or actual[2] != checked["creator"]:
            raise HostctlError("creator dev ID changed during assessment")
    opening["dev_id"] = checked["dev_id"]
    opening.pop("creator", None)
    save_private("opening.json", opening)
    save_private("assessment.json", checked)


def admit_locked(
    helper: dict[str, Any], trust: dict[str, Any], *, sealing: str = "deferred"
) -> None:
    """Commit once, after enrollment, trust and journal readiness; caller holds the lock."""
    from djinn_in_a_box.core import host_runtime
    from djinn_in_a_box.core.ssh_delivery import tailnet_files, write_public_files

    gen = generation(helper)
    if trust.get("generation") != gen:
        raise HostctlError("trust generation changed")
    # B2 keeps the existing private-network firewall rules; no allowlist expansion.
    import ipaddress

    networks = helper.get("NetworkSettings", {}).get("Networks", {})
    bridge = next((row.get("IPAddress") for row in networks.values() if row.get("IPAddress")), None)
    if bridge is None or not any(
        ipaddress.ip_address(bridge) in ipaddress.ip_network(cidr)
        for cidr in ("172.16.0.0/12", "192.168.0.0/16", "10.0.0.0/8")
    ):
        raise HostctlError(
            "helper bridge IP is outside the firewall's private-network rules; "
            "use an RFC1918 Docker subnet"
        )
    root = host_runtime.runtime_root()
    runtime = host_runtime.read_state(root)
    if runtime is not None and runtime.get("hostctl_hosts"):
        config = HostctlConfig.model_validate({"hosts": runtime["hostctl_hosts"]})
        write_public_files(root / "public", tailnet_files(config, trust))
    save_private("trust.json", trust)
    journal("relay-ready", generation=gen, peers=len(trust["peers"]), sealing=sealing)
    output = command(
        "exec", str(helper["Id"]), SUPERVISOR, "admit", gen, json.dumps(trust["routes"])
    )
    value = json.loads(output)
    if value.get("generation") != gen or value.get("admission") is not True or value.get("closed"):
        raise HostctlError("helper did not acknowledge relay admission")


def open_window(
    config: AppConfig, duration: str | None = None, *, allow_unsealed: bool = False
) -> str:
    from djinn_in_a_box.core import docker, host_runtime
    from djinn_in_a_box.core.host_sealing import inspect_assessment

    minutes = duration_minutes(config.hostctl.default_duration if duration is None else duration)
    if not config.hostctl.hosts:
        raise HostctlError("Declare at least one [hostctl.hosts.<name>] with address and user")
    assessment = inspect_assessment()
    try:
        assessment.require(allow_unsealed=allow_unsealed)
    except HostctlError:
        control_journal("on-refused", causes=assessment.causes, errors=assessment.errors)
        raise
    binary = supervisor_path()
    deadline = datetime.now(UTC) + timedelta(minutes=minutes)
    boot_deadline = time.clock_gettime_ns(time.CLOCK_BOOTTIME) + minutes * 60 * 10**9
    with control_guard():
        helper = inspect_helper()
        if helper is not None and helper["State"]["Running"]:
            control_journal("on-refused", generation=generation(helper), reason="already open")
            raise HostctlError("Window is already open; use djinn hostctl limit <minutes>")
        control_journal("on-attempt", allow_unsealed=allow_unsealed)
        verify_dev(assessment.dev_id)
        if allow_unsealed and assessment.causes:
            journal(
                "unsealed-override",
                causes=assessment.causes,
                boundary="dev has host authority; window is an operating aid only",
            )
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
        save_private(
            "opening.json",
            {
                "generation": gen,
                "config": config.hostctl.model_dump(),
                "allow_unsealed": allow_unsealed,
                "dev_id": assessment.dev_id,
            },
        )
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
                sealing=assessment.state,
                causes=assessment.causes,
                relay="closed pending enrollment, trust and journal readiness",
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
        command("stop", "-t", str(STOP_SECONDS), identity, timeout=STOP_SECONDS + 5)
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
    from djinn_in_a_box.core.host_sealing import inspect_assessment

    assessment = inspect_assessment()
    result: dict[str, Any] = {
        "state": "closed",
        "sealing": assessment.state,
        "sealing_causes": assessment.causes,
        "sealing_errors": assessment.errors,
        "dev_id": assessment.dev_id,
        "relay": "closed",
    }
    helper = inspect_helper()
    if helper is None:
        result["helper"] = "absent"
        result["node"] = "unknown (helper is off)"
        result["trust"] = cached_trust()
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
        result["relay"] = "unknown (helper admission unavailable)"
        try:
            result["node"] = node_status(helper)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            result["node_error"] = str(exc)
        try:
            live = json.loads(
                command("exec", str(helper["Id"]), SUPERVISOR, "status", generation(helper))
            )
            if live.get("generation") != generation(helper) or not isinstance(
                live.get("admission"), bool
            ):
                raise HostctlError("helper admission response is malformed")
            result["relay"] = "closed pending readiness"
            if live["admission"] and not live.get("paused") and not live.get("closed"):
                result["state"] = "open"
                result["relay"] = "open (:1080; declared peers, TCP 22 only)"
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            result["relay_error"] = str(exc)
    else:
        result["node"] = "unknown (helper is off)"
    trust = cached_trust()
    result["trust"] = trust
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
