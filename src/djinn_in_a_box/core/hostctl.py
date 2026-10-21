"""Host-only control of the helper; deadline authority stays inside PID 1."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.ssh import HostctlConfig, HostctlHost, duration_minutes, ssh_token
from djinn_in_a_box.config.volumes import HOSTCTL_STATE_VOLUME
from djinn_in_a_box.core import post_mortem
from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE
from djinn_in_a_box.core.lock_holders import describe_holders
from djinn_in_a_box.core.signals import signals_as_interrupt as _signals_as_interrupt

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


class HostctlBusy(HostctlError):  # noqa: N818
    """Host control is held by another process (raised only by ``control_guard``'s busy path)."""


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
def control_guard(*, timeout: float = 15) -> Iterator[None]:
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
                    deadline = time.monotonic() + timeout
                if time.monotonic() >= deadline:
                    message = "host control is busy; retry after the transition completes"
                    holder = describe_holders(root / "control.lock") if timeout > 0 else None
                    raise HostctlBusy(f"{message}; {holder}" if holder else message) from None
                time.sleep(0.05)
        _held.active = True
        _held.descriptor = fd
        yield
    finally:
        _held.active = False
        _held.descriptor = None
        os.close(fd)


def held_control_descriptor() -> int:
    """Return this thread's held control fd without opening or acquiring anything."""
    descriptor = getattr(_held, "descriptor", None)
    if descriptor is None:
        raise HostctlError("no host control lock is held by this thread")
    return descriptor


def command(*args: str, timeout: float = 5) -> str:
    from djinn_in_a_box.core.docker import _run_captured  # pyright: ignore[reportPrivateUsage]

    result = _run_captured([DOCKER_EXECUTABLE, *args], cwd=Path("/"), timeout=timeout)
    if not result.success:
        # Docker errors may contain auth URLs; they never enter a journal or log.
        raise HostctlError(f"Docker {args[0]} failed (exit {result.returncode})")
    return result.stdout


def inspect_helper(*, strict: bool = False) -> dict[str, Any] | None:
    from djinn_in_a_box.core.host_runtime import inspect_object

    value = (
        inspect_object(HELPER_NAME, DOCKER_EXECUTABLE, missing="No such container")
        if strict else inspect_object(HELPER_NAME, DOCKER_EXECUTABLE)
    )
    if value is not None:
        labels: dict[str, Any] = value.get("Config", {}).get("Labels") or {}
        if not labels.get(GENERATION_LABEL):
            raise HostctlError("helper ownership is unknown; preserving the container")
    return value


def generation(helper: dict[str, Any]) -> str:
    return str(helper["Config"]["Labels"][GENERATION_LABEL])


def journal(event: str, *, wait: float | None = None, **fields: Any) -> bool:
    root = state_root()
    fd = os.open(root / "journal.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if wait is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    time.sleep(min(0.05, remaining))
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
            return True
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
    from djinn_in_a_box.core import docker
    from djinn_in_a_box.core.host_runtime import inspect_dev

    actual = inspect_dev(docker.service_container_name("dev"), DOCKER_EXECUTABLE)
    current = actual[0] if actual and actual[1] else None
    if current != dev_id:
        raise HostctlError("dev was removed or replaced during assessment")


def verify_agent(dev_id: str | None, expected: dict[str, str] | None) -> None:
    from djinn_in_a_box.core import docker, host_runtime

    if expected is None:
        return
    actual = host_runtime.inspect_object(str(dev_id), DOCKER_EXECUTABLE)
    if actual is None:
        raise HostctlError("dev disappeared during companion verification")
    current = docker.inspect_agent_endpoint(actual)
    if current.errors or current.evidence != expected:
        raise HostctlError("agent Docker companion changed or is unverifiable")


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
    if opening.get("agent") != assessment.agent:
        raise HostctlError("agent Docker changed during window opening")
    rows = run_probe(assessment.dev_id, trust) if assessment.dev_id else []
    if assessment.agent:
        rows.extend(run_probe(assessment.agent["id"], trust))
    require_blocked(rows)
    return {
        "generation": trust["generation"],
        "dev_id": assessment.dev_id,
        "creator": creator,
        "sealing": assessment.state,
        "causes": assessment.causes,
        "probe": rows,
        "agent": assessment.agent,
    }


def host_runtime_dev() -> tuple[str, bool, str] | None:
    from djinn_in_a_box.core import docker
    from djinn_in_a_box.core.host_runtime import inspect_dev

    return inspect_dev(docker.service_container_name("dev"), DOCKER_EXECUTABLE)


def guard_dev_start(planned: dict[str, Any], creator: str) -> None:
    """Close before unsealed creation; sealed creation stays paused until delivery checks."""
    from djinn_in_a_box.core.console import warning
    from djinn_in_a_box.core.host_sealing import assess

    checked = assess(planned, planned_generation=creator)
    with teardown_markers(), control_guard():
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
        opening.update(creator=creator, allow_unsealed=False, agent=checked.agent)
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
    verify_agent(checked["dev_id"], checked.get("agent"))
    opening = json.loads((state_root() / "opening.json").read_text())
    if (
        opening["generation"] != checked["generation"]
        or opening.get("creator") != checked["creator"]
        or opening.get("agent") != checked.get("agent")
    ):
        raise HostctlError("dev transition changed during assessment")
    if checked["creator"]:
        actual = host_runtime_dev()
        if actual is None or actual[2] != checked["creator"]:
            raise HostctlError("creator dev ID changed during assessment")
    opening["dev_id"] = checked["dev_id"]
    opening["agent"] = checked.get("agent")
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
        verify_agent(assessment.dev_id, assessment.agent)
        if allow_unsealed and assessment.causes:
            journal(
                "unsealed-override",
                causes=assessment.causes,
                boundary="dev has host authority; window is an operating aid only",
            )
        if helper is not None:
            reconcile(helper)
            _record_stopped_helper(helper)
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
                "agent": assessment.agent,
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


@contextlib.contextmanager
def teardown_markers(*, root: Path | None = None) -> Iterator[None]:
    """Defer diagnostic persistence until the caller has attempted its closure journal."""
    pending: list[tuple[str, str]] = []
    previous = getattr(_held, "teardown_markers", None)
    _held.teardown_markers = pending
    try:
        yield
    finally:
        _held.teardown_markers = previous
        for identity, started_at in pending:
            with contextlib.suppress(Exception):
                post_mortem.mark_terminated(
                    DOCKER_EXECUTABLE, identity, started_at,
                    blocking=False, root=root,
                )


def _helper_marker(identity: str, started_at: str) -> None:
    pending = getattr(_held, "teardown_markers", None)
    if pending is not None:
        pending.append((identity, started_at))
    else:
        with contextlib.suppress(Exception):
            post_mortem.mark_terminated(
                DOCKER_EXECUTABLE, identity, started_at, blocking=False,
            )


def _record_stopped_helper(helper: dict[str, Any]) -> None:
    """Only an expiry from this run exempts an otherwise eligible stopped helper."""
    expired = False
    try:
        started = datetime.fromisoformat(helper["State"]["StartedAt"])
        logs = command("logs", "--tail", "100", str(helper["Id"]))
        for line in logs.splitlines():
            try:
                raw_event: Any = json.loads(line)
                if not isinstance(raw_event, dict):
                    continue
                event = cast(dict[str, Any], raw_event)
                if (
                    event.get("event") == "expiry"
                    and event.get("generation") == generation(helper)
                    and datetime.fromisoformat(event["time"]) >= started
                ):
                    expired = True
                    break
            except (ValueError, KeyError, TypeError):
                continue
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError):
        pass
    if not expired:
        with contextlib.suppress(Exception):
            post_mortem.record(
                "hostctl-helper", docker_path=DOCKER_EXECUTABLE,
                candidates=[post_mortem.Candidate(str(helper["Id"]), True, str(helper["Id"]))],
                hostctl_generation=generation(helper),
            )


def stop_and_verify(
    identity: str, gen: str, *, strict: bool,
) -> dict[str, Any] | None:
    """Stop an inspected ID; None means it disappeared or was replaced after the stop."""
    try:
        command("stop", "-t", str(STOP_SECONDS), identity, timeout=STOP_SECONDS + 5)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        if not strict:
            raise
        # A timed-out stop may have taken effect. Only a verified outcome closes it.
        current = inspect_helper(strict=True) if strict else inspect_helper()
        if (
            current is None or current["Id"] != identity or generation(current) != gen
        ):
            return None
        if current["State"]["Running"]:
            raise
        return current
    current = inspect_helper(strict=True) if strict else inspect_helper()
    if current is None or current["Id"] != identity or generation(current) != gen:
        return None
    if current["State"]["Running"]:
        raise HostctlError("Helper termination could not be verified")
    return current


def stop_helper_locked(*, remove: bool = False) -> None:
    helper = inspect_helper()
    if helper is None:
        return
    identity = str(helper["Id"])
    gen = generation(helper)
    was_running = helper["State"]["Running"]
    if was_running:
        started_at = str(helper["State"].get("StartedAt", ""))
        try:
            current = stop_and_verify(identity, gen, strict=False)
        finally:
            if started_at:
                _helper_marker(identity, started_at)
    else:
        current = inspect_helper()
    if current is None or current["Id"] != identity or generation(current) != gen:
        raise HostctlError("Helper changed during teardown; preserving resources")
    if current["State"]["Running"]:
        raise HostctlError("Helper termination could not be verified")
    reconcile(current)
    if remove:
        if not was_running:
            _record_stopped_helper(current)
        command("rm", identity)
        if inspect_helper() is not None:
            raise HostctlError("Helper removal could not be verified")


def close_window() -> None:
    with teardown_markers(), control_guard():
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
        "sealing_cause_details": assessment.cause_details,
        "sealing_error_details": assessment.error_details,
        "dev_id": assessment.dev_id,
        "agent": assessment.agent,
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


CHECK_NO_TERMINAL = "hostctl check requires a terminal on stdin and stderr"
CHECK_NO_SSH = "ssh client not found on host PATH"
CHECK_NOT_OPEN = "hostctl window is not open or did not answer; run djinn hostctl status"
CHECK_TRUST = "cached peer trust does not match the helper generation"
CHECK_OPENING = "opening configuration does not match the helper generation"
CHECK_INCOMPLETE = "hostctl check state is incomplete; run djinn doctor"


def _stdio_is_terminal() -> bool:
    """Both streams the owner needs for the approval prompt; stdout may be redirected."""
    return sys.stdin.isatty() and sys.stderr.isatty()


def _live_helper() -> tuple[str, str]:
    """Exact helper ID and generation of a running, admitted, unpaused window."""
    helper = inspect_helper()
    if helper is None or not helper.get("State", {}).get("Running"):
        raise HostctlError(CHECK_NOT_OPEN)
    identity = str(helper.get("Id") or "")
    gen = generation(helper)
    if not re.fullmatch(r"[0-9a-f]{64}", identity) or not re.fullmatch(r"[0-9a-f]{32}", gen):
        raise HostctlError(CHECK_INCOMPLETE)
    try:
        output = command("exec", identity, SUPERVISOR, "status", gen)
    except HostctlError:
        raise HostctlError(CHECK_NOT_OPEN) from None
    try:
        decoded: Any = json.loads(output)
    except ValueError:
        raise HostctlError(CHECK_INCOMPLETE) from None
    live = cast(dict[str, Any], decoded)
    if (
        not isinstance(decoded, dict)
        or live.get("generation") != gen
        or not isinstance(live.get("admission"), bool)
        or not isinstance(live.get("paused"), bool)
    ):
        raise HostctlError(CHECK_INCOMPLETE)
    if not live["admission"] or live["paused"]:
        raise HostctlError(CHECK_NOT_OPEN)
    return identity, gen


def _frozen_peer(host: HostctlHost, peer: Any) -> dict[str, Any]:
    from djinn_in_a_box.core.ssh_delivery import GitSSHError, public_blob, tailnet_ip

    if not isinstance(peer, dict):
        raise HostctlError(CHECK_INCOMPLETE)
    value = cast(dict[str, Any], peer)
    ip, canonical, keys = value.get("ip"), value.get("host_key_alias"), value.get("keys")
    if value.get("address") != host.address or not isinstance(ip, str):
        raise HostctlError(CHECK_INCOMPLETE)
    if not isinstance(canonical, str) or not isinstance(keys, list) or not keys:
        raise HostctlError(CHECK_INCOMPLETE)
    try:
        tailnet_ip(ip)
        ssh_token(canonical)
        for key in cast(list[Any], keys):
            if not isinstance(key, str):
                raise HostctlError(CHECK_INCOMPLETE)
            public_blob(key)
    except (GitSSHError, ValueError):
        raise HostctlError(CHECK_INCOMPLETE) from None
    return {
        "address": host.address,
        "ip": ip,
        "host_key_alias": canonical,
        "keys": [" ".join(str(key).split()[:2]) for key in cast(list[Any], keys)],
    }


def _check_targets(host: str | None, gen: str) -> tuple[HostctlConfig, dict[str, Any]]:
    """Selected declarations and validated peers, solely from this opening's frozen state."""
    try:
        trust = cached_trust()
    except (HostctlError, ValueError, OSError):
        raise HostctlError(CHECK_INCOMPLETE) from None
    if trust is None or trust.get("generation") != gen:
        raise HostctlError(CHECK_TRUST)
    try:
        opening: Any = json.loads((state_root(create=False) / "opening.json").read_text())
    except FileNotFoundError:
        raise HostctlError(CHECK_OPENING) from None
    except (ValueError, OSError):
        raise HostctlError(CHECK_INCOMPLETE) from None
    if not isinstance(opening, dict):
        raise HostctlError(CHECK_INCOMPLETE)
    frozen_opening = cast(dict[str, Any], opening)
    if frozen_opening.get("generation") != gen:
        raise HostctlError(CHECK_OPENING)
    try:
        config = HostctlConfig.model_validate(frozen_opening.get("config"))
    except ValueError:
        raise HostctlError(CHECK_INCOMPLETE) from None
    if not config.hosts:
        raise HostctlError(CHECK_INCOMPLETE)
    if host is not None and host not in config.hosts:
        raise HostctlError(f"unknown hostctl alias: {host}")
    selected = [host] if host is not None else list(config.hosts)
    peers: Any = trust.get("peers")
    if not isinstance(peers, dict):
        raise HostctlError(CHECK_INCOMPLETE)
    frozen = {
        alias: _frozen_peer(config.hosts[alias], cast(dict[str, Any], peers).get(alias))
        for alias in selected
    }
    return HostctlConfig(hosts={alias: config.hosts[alias] for alias in selected}), {
        "peers": frozen
    }


def _write_private_file(path: Path, text: str) -> None:
    # O_CREAT|O_EXCL already fails on any existing path, a planted symlink included (POSIX).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


def _remove_tree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except KeyboardInterrupt:
        shutil.rmtree(path, ignore_errors=True)
        raise


def _run_check_ssh(ssh: str, config_path: Path, alias: str) -> int:
    """The owner's terminal is inherited: nothing is captured, read or timed out here."""
    try:
        argv = [ssh, "-F", str(config_path), "-T", "-n", alias, "true"]
        return subprocess.run(argv, check=False).returncode
    except OSError:
        raise HostctlError("ssh client could not be started") from None


def check_hosts(host: str | None, report: Callable[[str], None]) -> int:
    """Run one host-terminal SSH check per selected frozen alias; 0 only if all succeed."""
    from djinn_in_a_box.core.ssh_delivery import host_check_files

    if not _stdio_is_terminal():
        raise HostctlError(CHECK_NO_TERMINAL)
    found = shutil.which("ssh")
    if found is None:
        raise HostctlError(CHECK_NO_SSH)
    ssh = os.path.abspath(found)
    with _signals_as_interrupt():
        with control_guard():
            helper_id, gen = _live_helper()
            selected, frozen = _check_targets(host, gen)
        path = state_root() / f"check-{gen}-{secrets.token_hex(8)}"
        try:
            os.mkdir(path, 0o700)
            os.chmod(path, 0o700)
            files = host_check_files(
                selected,
                frozen,
                docker=DOCKER_EXECUTABLE,
                helper_id=helper_id,
                supervisor=SUPERVISOR,
                generation=gen,
                directory=path,
            )
            for name, text in files.items():
                _write_private_file(path / name, text)
            failed = False
            for alias in selected.hosts:
                report(f"Checking {alias}...")
                try:
                    code = _run_check_ssh(ssh, path / "config", alias)
                except KeyboardInterrupt:
                    report(f"{alias}: check interrupted")
                    raise
                if code == 0:
                    report(f"{alias}: approved and reachable")
                else:
                    report(f"{alias}: check failed (ssh exit {code})")
                    failed = True
            return 1 if failed else 0
        finally:
            _remove_tree(path)
            if os.path.lexists(path):
                report(f"Could not remove {path}; it holds only public SSH configuration")


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
