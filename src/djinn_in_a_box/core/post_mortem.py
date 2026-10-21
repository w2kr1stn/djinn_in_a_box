"""Best-effort, host-private evidence for stopped containers about to be removed."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import re
import selectors
import stat
import subprocess
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from djinn_in_a_box.core import console, host_runtime

RECORD_LIMIT = 10
RECORD_TOTAL_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
LOG_TAIL_LINES = 200
LOG_TAIL_BYTES = 64 * 1024
RECORD_SECONDS = 15
STRING_BYTES = 2 * 1024
DETAIL_ITEMS = 5
MARKER_LIMIT = 128
_PROVENANCE_NAME = "provenance.json"
CGROUP_BASE = Path("/sys/fs/cgroup")

_TRIGGER = re.compile(r"[a-z][a-z0-9-]*")
_RECORD_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[a-z][a-z0-9-]*-[0-9a-f]{12}\.json")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_LABELS = (
    "djinn.git-agent.generation",
    "djinn.hostctl.generation",
    "com.docker.compose.project",
    "com.docker.compose.service",
)
_STATE = ("Status", "Running", "ExitCode", "OOMKilled", "Error", "StartedAt", "FinishedAt")
_LIMITS = (
    "Memory",
    "MemorySwap",
    "MemoryReservation",
    "NanoCpus",
    "CpuQuota",
    "CpuPeriod",
    "CpusetCpus",
    "PidsLimit",
)


@dataclass(frozen=True)
class Candidate:
    """A caller-approved removal target; pin names with expected_id when available."""

    identifier: str
    owned: bool
    expected_id: str | None = None


@dataclass(frozen=True)
class RecordResult:
    path: Path | None
    running: dict[str, str] = field(default_factory=lambda: dict[str, str]())


class _RecorderError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def records_root(*, create: bool) -> Path:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    root = home / ".local" / "state" / "djinn" / "post-mortem"
    if create:
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        host_runtime.private_directory(root)
    return root


def _private_file(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def _admitted(root: Path) -> list[tuple[Path, os.stat_result]]:
    files: list[tuple[Path, os.stat_result]] = []
    for path in root.iterdir():
        if _RECORD_NAME.fullmatch(path.name):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if _private_file(info):
                files.append((path, info))
    return sorted(files, key=lambda entry: entry[0].name)


def _code(exc: Exception) -> str:
    return exc.code if isinstance(exc, _RecorderError) else f"internal:{type(exc).__name__}"


def _emit(message: str) -> None:
    console.print_captured(console.escape_nonprintable(message), end="\n")


def notify_failure(code: str) -> None:
    try:
        _emit(f"Post-mortem record failed ({code})")
    except Exception:
        # A broken diagnostic output sink must not interfere with recovery either.
        return


def _root(root: Path | None, *, create: bool) -> Path:
    try:
        path = records_root(create=create) if root is None else root
        if create:
            host_runtime.private_directory(path)
        return path
    except Exception as exc:
        raise _RecorderError("directory") from exc


@contextlib.contextmanager
def _lock(root: Path, deadline: float, *, blocking: bool = True) -> Iterator[None]:
    fd: int | None = None
    try:
        try:
            fd = os.open(
                root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
            )
            if not _private_file(os.fstat(fd)):
                raise _RecorderError("lock")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if not blocking or remaining <= 0:
                        raise _RecorderError("lock") from None
                    time.sleep(min(0.02, remaining))
        except OSError as exc:
            raise _RecorderError("lock") from exc
        yield
    finally:
        if fd is not None:
            os.close(fd)


def _atomic_write(root: Path, name: str, payload: bytes) -> Path:
    temporary = root / f".record-{uuid.uuid4().hex}.tmp"
    created = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        target = root / name
        os.replace(temporary, target)
        return target
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def _markers(root: Path) -> dict[str, str]:
    try:
        fd = os.open(root / _PROVENANCE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    try:
        with os.fdopen(fd, "rb") as stream:
            if not _private_file(os.fstat(stream.fileno())):
                raise _RecorderError("marker")
            payload = stream.read(MAX_RECORD_BYTES + 1)
        if len(payload) > MAX_RECORD_BYTES:
            raise _RecorderError("marker")
        data: Any = json.loads(payload)
        if not isinstance(data, dict):
            raise _RecorderError("marker")
        data = cast(dict[str, Any], data)
        if len(data) > MARKER_LIMIT or any(not isinstance(v, str) for v in data.values()):
            raise _RecorderError("marker")
        return cast(dict[str, str], data)
    except Exception as exc:
        raise _RecorderError("marker") from exc


def _timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _RecorderError("deadline")
    return min(5, remaining)


def _stopped_same_run(docker_path: str, identity: str, started_at: str, deadline: float) -> bool:
    try:
        data = host_runtime.inspect_object(identity, docker_path, timeout=_timeout(deadline))
    except Exception as exc:
        raise _RecorderError("inspect") from exc
    return (
        data is not None
        and data.get("Id") == identity
        and data.get("State", {}).get("StartedAt") == started_at
        and data.get("State", {}).get("Running") is False
    )


def mark_terminated(
    docker_path: str,
    container_id: str,
    started_at: str,
    *,
    blocking: bool = True,
    root: Path | None = None,
) -> None:
    """Remember only verified Djinn teardown outcomes for a previously running episode."""
    try:
        deadline = time.monotonic() + RECORD_SECONDS
        if not _stopped_same_run(docker_path, container_id, started_at, deadline):
            return
        directory = _root(root, create=True)
        with _lock(directory, deadline, blocking=blocking):
            try:
                try:
                    markers = _markers(directory)
                except Exception:
                    notify_failure("marker")
                    markers = {}
                markers.pop(container_id, None)
                markers[container_id] = started_at
                markers = dict(list(markers.items())[-MARKER_LIMIT:])
                _atomic_write(directory, _PROVENANCE_NAME, json.dumps(markers).encode())
            except Exception as exc:
                raise _RecorderError("marker") from exc
    except Exception as exc:
        notify_failure(_code(exc))


def _cap(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")[:STRING_BYTES].decode(
            "utf-8", errors="ignore"
        )
    return value


def _cgroup(identity: str) -> dict[str, Any]:
    if not _CONTAINER_ID.fullmatch(identity):
        return {"unavailable": "invalid-id"}
    try:
        for directory in (
            CGROUP_BASE / "system.slice" / f"docker-{identity}.scope",
            CGROUP_BASE / "docker" / identity,
        ):
            if not directory.is_dir():
                continue
            result: dict[str, Any] = {}
            for name in ("memory.current", "memory.peak", "memory.max", "memory.events"):
                try:
                    with (directory / name).open("rb") as stream:
                        result[name] = stream.read(STRING_BYTES).decode("utf-8", errors="replace")
                except OSError:
                    result[name] = {"unavailable": "read"}
            return result
        return {"unavailable": "missing"}
    except OSError:
        return {"unavailable": "read"}


def _logs(docker_path: str, identity: str, deadline: float) -> tuple[str, str | None]:
    _timeout(deadline)
    call_deadline = min(deadline, time.monotonic() + 5)
    expiry_code = "deadline" if call_deadline == deadline else "logs"
    ring = bytearray()
    problem: str | None = None
    child = subprocess.Popen(
        [docker_path, "logs", "--tail", str(LOG_TAIL_LINES), identity],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        cwd="/",
    )
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            # Reserve part of the same deadline for terminating/reaping our own child.
            read_deadline = max(time.monotonic(), call_deadline - 0.1)
            while True:
                remaining = read_deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    problem = expiry_code
                    break
                chunk = os.read(child.stdout.fileno(), 8192)
                if not chunk:
                    break
                ring.extend(chunk)
                del ring[:-LOG_TAIL_BYTES]
        if problem is None:
            try:
                if child.wait(timeout=max(0, call_deadline - time.monotonic())):
                    problem = "logs"
            except subprocess.TimeoutExpired:
                problem = expiry_code
    finally:
        try:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=min(0.05, max(0, call_deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    child.kill()
                    # No wait started by the recorder may outlive its deadline.
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=max(0, call_deadline - time.monotonic()))
        finally:
            if child.stdout is not None:
                child.stdout.close()
    return ring.decode("utf-8", errors="replace"), problem


def _subject(data: dict[str, Any], markers: dict[str, str]) -> bool:
    state = data.get("State", {})
    config = data.get("Config", {})
    labels: dict[str, Any] = config.get("Labels") or {}
    identity = data.get("Id")
    return (
        isinstance(identity, str)
        and bool(identity)
        and state.get("Running") is False
        and str(labels.get("com.docker.compose.oneoff", "")).lower() != "true"
        and data.get("HostConfig", {}).get("AutoRemove") is not True
        and labels.get("com.docker.compose.service") != "agent-docker-firewall"
        and not (identity in markers and markers[identity] == state.get("StartedAt"))
    )


def _snapshot(data: dict[str, Any]) -> dict[str, Any]:
    labels: dict[str, Any] = data.get("Config", {}).get("Labels") or {}
    return {
        "id": _cap(data["Id"]),
        "name": _cap(data.get("Name")),
        "service": _cap(labels.get("com.docker.compose.service")),
        "Image": _cap(data.get("Image")),
        "Config": {"Image": _cap(data.get("Config", {}).get("Image"))},
        "labels": {key: _cap(labels.get(key)) for key in _LABELS},
        "State": {key: _cap(data.get("State", {}).get(key)) for key in _STATE},
        "RestartCount": data.get("RestartCount"),
        "HostConfig": {key: _cap(data.get("HostConfig", {}).get(key)) for key in _LIMITS},
        "cgroup": _cgroup(data["Id"]),
        "log_tail": "",
        "errors": {},
    }


def _key(
    subjects: list[dict[str, Any]],
    observed: dict[str, Any] | None,
    generation: str | None,
    incident: str = "",
) -> str:
    if subjects:
        material = "\n".join(sorted(f"{s['id']}|{s['State']['FinishedAt']}" for s in subjects))
    else:
        assert observed is not None
        material = (
            f"observed|{generation or ''}|{observed.get('expected_id') or ''}|"
            f"{observed.get('actual_id') or 'absent'}{incident}"
        )
    return hashlib.sha256(material.encode()).hexdigest()[:12]


def _serialize(data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode()
    for subject in data["subjects"]:
        if len(payload) <= MAX_RECORD_BYTES:
            break
        subject["log_tail"] = None
        subject["log_tail_dropped"] = "record-size"
        payload = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode()
    if len(payload) > MAX_RECORD_BYTES:
        raise _RecorderError("write")
    return payload


def _prune(root: Path, keep: Path) -> None:
    files = _admitted(root)
    count = len(files)
    total = sum(info.st_size for _, info in files)
    for path, info in files:
        if count <= RECORD_LIMIT and total <= RECORD_TOTAL_BYTES:
            break
        if path == keep:
            continue
        path.unlink()
        count -= 1
        total -= info.st_size


def record(
    trigger: str,
    *,
    docker_path: str,
    candidates: Iterable[Candidate],
    observed: dict[str, Any] | None = None,
    runtime_generation: str | None = None,
    hostctl_generation: str | None = None,
    root: Path | None = None,
    detail: dict[str, str | list[str]] | None = None,
) -> RecordResult | None:
    """Collect only the caller's candidates; diagnostics never escape into teardown."""
    try:
        deadline = time.monotonic() + RECORD_SECONDS
        directory = _root(root, create=True)
        errors: dict[str, str] = {}
        try:
            markers = _markers(directory)
        except Exception:
            markers = {}
            errors["provenance"] = "marker"
        subjects: list[dict[str, Any]] = []
        running: dict[str, str] = {}
        seen: set[str] = set()
        for index, candidate in enumerate(candidates):
            if not candidate.owned:
                continue
            try:
                actual = host_runtime.inspect_object(
                    candidate.identifier,
                    docker_path,
                    timeout=_timeout(deadline),
                )
            except Exception as exc:
                errors[f"candidate:{index}"] = (
                    "deadline" if time.monotonic() >= deadline else "inspect"
                )
                if isinstance(exc, _RecorderError):
                    errors[f"candidate:{index}"] = exc.code
                continue
            if actual is None:
                continue
            identity = actual["Id"]
            if candidate.expected_id is not None and identity != candidate.expected_id:
                continue
            if identity in seen:
                continue
            seen.add(identity)
            state = actual.get("State", {})
            if state.get("Running") is True:
                running[identity] = str(state.get("StartedAt", ""))
            if not _subject(actual, markers):
                continue
            subject = _snapshot(actual)
            try:
                subject["log_tail"], problem = _logs(docker_path, identity, deadline)
            except Exception as exc:
                problem = (
                    "deadline"
                    if isinstance(exc, _RecorderError) and exc.code == "deadline"
                    else "logs"
                )
            if problem:
                subject["errors"]["log_tail"] = problem
                errors[f"logs:{index}"] = problem
            subjects.append(subject)
        if time.monotonic() >= deadline:
            errors["collection"] = "deadline"
        for code in sorted(set(errors.values())):
            _emit(f"Post-mortem record failed ({code})")
        if not subjects and observed is None:
            return RecordResult(None, running)
        if not _TRIGGER.fullmatch(trigger):
            raise _RecorderError("write")
        observation = (
            {key: _cap(observed.get(key)) for key in ("state", "expected_id", "actual_id")}
            if observed is not None
            else None
        )
        data: dict[str, Any] = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "trigger": trigger,
            "runtime_generation": _cap(runtime_generation),
            "hostctl_generation": _cap(hostctl_generation),
            "observed": observation,
            "subjects": subjects,
            "errors": errors,
        }
        if detail is not None:
            data["detail"] = {
                name: [_cap(item) for item in value[:DETAIL_ITEMS]]
                if isinstance(value, list)
                else _cap(value)
                for name, value in detail.items()
            }
        if "deadline" in errors.values():
            data["incomplete"] = "deadline"
        # Two observer exits are two incidents unless generation and cause agree as well.
        incident = (
            f"|{runtime_generation or ''}|{observation['state'] if observation else ''}"
            if trigger == "observer-exit"
            else ""
        )
        key = _key(subjects, observation, hostctl_generation, incident)
        payload = _serialize(data)
        with _lock(directory, deadline):
            matched = next(
                (path for path, _ in _admitted(directory) if path.name.endswith(f"-{key}.json")),
                None,
            )
            if matched is None:
                name = f"{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{trigger}-{key}.json"
                try:
                    matched = _atomic_write(directory, name, payload)
                except Exception as exc:
                    raise _RecorderError("write") from exc
                try:
                    _prune(directory, matched)
                except Exception:
                    _emit("Post-mortem record failed (prune)")
        _emit(f"Post-mortem record: {matched}")
        return RecordResult(matched, running)
    except Exception as exc:
        notify_failure(_code(exc))
        return None


def latest() -> tuple[int, Path | None, str | None]:
    """Read-only, daemon-independent doctor summary using the same admission rule."""
    try:
        root = _root(None, create=False)
        try:
            info = root.lstat()
        except FileNotFoundError:
            return 0, None, None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return 0, None, "directory"
        files = _admitted(root)
        return len(files), files[-1][0] if files else None, None
    except Exception as exc:
        return 0, None, _code(exc)
