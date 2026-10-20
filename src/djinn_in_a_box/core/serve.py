"""Host-side gates and the frozen container-helper protocol for ``djinn serve``."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess  # nosec B404
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import urlsplit

from djinn_in_a_box.config.models import AppConfig, ServeConfig
from djinn_in_a_box.core import hostctl
from djinn_in_a_box.core.docker import (
    DOCKER_EXECUTABLE,
    OPENCODE_CONTAINER_PORT,
    RunResult,
    _run_captured,  # pyright: ignore[reportPrivateUsage]
    service_container_name,
)
from djinn_in_a_box.core.host_runtime import private_directory
from djinn_in_a_box.core.ssh_delivery import GitSSHError

CLAUDE: Final = "claude"
OPENCODE: Final = "opencode"
# Frozen copies; test_protocol_enums_match_helper compares the helper loaded by file path.
NAMES: Final = (CLAUDE, OPENCODE)
STATES: Final = ("running", "degraded", "exited", "stopped")
START_RESULTS: Final = ("started", "already-running", "restarted", "exited")
STOP_RESULTS: Final = ("stopped", "not-running", "survived")
ERROR_CODES: Final = (
    "state-dir",
    "state-path",
    "env-root",
    "password",
    "service-json",
    "service-config",
    "remote-config",
    "spawn",
    "not-registered",
    "busy",
    "timeout",
    "usage",
)
FUNNEL_WARNING: Final = "WARNING: Tailscale Funnel is enabled for this entry (public internet)"


class ServeError(RuntimeError):
    """A refusal or failed query, with optional literal subprocess diagnostics."""

    def __init__(self, message: str, *, code: str = "gate", detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class ContainerState(Enum):
    RUNNING = "running"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContainerInfo:
    state: ContainerState
    id: str = ""
    env: tuple[str, ...] = ()
    ports: object = None
    mounts: object = None
    network_mode: object = None
    networks: object = None

    @property
    def detached(self) -> bool:
        return "DJINN_DETACHED=true" in self.env


def detect_container(*, timeout: float = 10) -> ContainerInfo:
    """Inspect once; a Docker failure must never masquerade as an absent container."""
    try:
        result = _run_captured(
            [
                DOCKER_EXECUTABLE,
                "inspect",
                "--type",
                "container",
                service_container_name("dev"),
                "--format",
                "{{json .}}",
            ],
            timeout=timeout,
        )
        if not result.success:
            state = (
                ContainerState.ABSENT
                if "No such container" in result.stderr
                else ContainerState.UNKNOWN
            )
            return ContainerInfo(state)
        raw = json.loads(result.stdout)
        if not isinstance(raw, dict):
            return ContainerInfo(ContainerState.UNKNOWN)
        data = cast(dict[str, Any], raw)
        cid = data["Id"]
        running = data["State"]["Running"]
        env = data["Config"]["Env"]
        host = data["HostConfig"]
        if (
            not isinstance(cid, str)
            or re.fullmatch(r"[0-9a-f]{64}", cid) is None
            or type(running) is not bool
            or not isinstance(env, list)
            or not all(isinstance(v, str) for v in cast(list[object], env))
            or not isinstance(host, dict)
        ):
            return ContainerInfo(ContainerState.UNKNOWN)
        host = cast(dict[str, Any], host)
        return ContainerInfo(
            ContainerState.RUNNING if running else ContainerState.ABSENT,
            cid,
            tuple(cast(list[str], env)),
            host.get("PortBindings"),
            data.get("Mounts"),
            host.get("NetworkMode"),
            data.get("NetworkSettings", {}).get("Networks"),
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, AttributeError):
        return ContainerInfo(ContainerState.UNKNOWN)


def check_opencode_port(info: ContainerInfo, port: int) -> bool:
    return info.ports == {
        f"{OPENCODE_CONTAINER_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
    }


def check_opencode_roots(info: ContainerInfo) -> None:
    for entry in info.env:
        name, sep, value = entry.partition("=")
        if sep and (
            (name == "HOME" and os.path.normpath(value) != "/home/dev")
            or name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "OPENCODE_CONFIG_DIR")
        ):
            raise ServeError(
                f"The container environment relocates OpenCode's settings ({name}); refusing."
            )
    mounts: object = info.mounts
    if not isinstance(mounts, list):
        raise ServeError("Could not inspect OpenCode's settings mounts; refusing.")
    roots = ("/home/dev/.config/opencode", "/home/dev/.local/state/opencode")
    for mount in cast(list[object], mounts):
        if not isinstance(mount, dict):
            raise ServeError("Could not inspect OpenCode's settings mounts; refusing.")
        destination = cast(dict[str, object], mount).get("Destination")
        if not isinstance(destination, str) or not destination.startswith("/"):
            raise ServeError("Could not inspect OpenCode's settings mounts; refusing.")
        dest = os.path.normpath(destination)
        if any(os.path.commonpath((dest, root)) in (dest, root) for root in roots):
            raise ServeError(
                f"A mount covers OpenCode's service settings ({destination}); the server password "
                "would persist there. Remove that mount, then recreate the container."
            )


def check_opencode_networks(info: ContainerInfo) -> None:
    unknown = "Could not inspect the container's networks; refusing to expose OpenCode."
    mode = info.network_mode
    if not isinstance(mode, str) or not mode:
        raise ServeError(unknown)
    if mode in ("host", "none", "bridge", "default") or mode.startswith("container:"):
        raise ServeError(f"The container does not use a bridge network ({mode}); refusing.")
    attached: object = info.networks
    if not isinstance(attached, dict) or not attached:
        raise ServeError(unknown)
    networks = cast(dict[str, object], attached)
    # NetworkMode may be a network ID; resolve it against the attached network metadata.
    if mode not in networks and not any(
        isinstance(v, dict) and cast(dict[str, object], v).get("NetworkID") == mode
        for v in networks.values()
    ):
        raise ServeError(unknown)
    for name in networks:
        try:
            result = _run_captured([DOCKER_EXECUTABLE, "network", "inspect", name], timeout=5)
            data = json.loads(result.stdout) if result.success else None
            if (
                not isinstance(data, list)
                or len(cast(list[object], data)) != 1
                or not isinstance(data[0], dict)
            ):
                raise ServeError(unknown)
            network = cast(dict[str, Any], data[0])
            if network.get("Driver") != "bridge":
                raise ServeError(
                    f"The container does not use a bridge network ({network.get('Driver')}); "
                    "refusing."
                )
            options = network.get("Options")
            if not isinstance(options, dict):
                raise ServeError(unknown)
            for option, value in cast(dict[str, object], options).items():
                if option == "com.docker.network.bridge.trusted_host_interfaces" or (
                    option
                    in (
                        "com.docker.network.bridge.gateway_mode_ipv4",
                        "com.docker.network.bridge.gateway_mode_ipv6",
                    )
                    and value in ("routed", "nat-unprotected")
                ):
                    raise ServeError(
                        f"Network {name} allows direct routing to the container ({option}); "
                        "OpenCode would be reachable from the LAN. Refusing."
                    )
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
            raise ServeError(unknown) from exc


def engine_supports_loopback_isolation() -> None:
    unknown = "Could not determine the Docker Engine version; refusing to expose OpenCode."
    try:
        result = _run_captured(
            [DOCKER_EXECUTABLE, "version", "--format", "{{.Server.Version}}"],
            timeout=5,
        )
        version = result.stdout.strip()
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version) if result.success else None
        if match is None:
            raise ServeError(unknown)
        if tuple(int(v) for v in match.groups()) < (28, 3, 3):
            raise ServeError(
                f"Docker Engine {version} is older than 28.3.3; OpenCode could be reachable from "
                "the LAN (container address or CVE-2025-54388). Update Docker."
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServeError(unknown) from exc


def password_path() -> Path:
    """Use hostctl.state_root's absolute XDG path policy without creating hostctl state."""
    try:
        return hostctl.state_root(create=False).parent / "serve" / "opencode-password"
    except hostctl.HostctlError as exc:
        raise ServeError(str(exc)) from exc


def read_password(path: Path | None = None) -> str:
    path = password_path() if path is None else path
    message = f"OpenCode password file {path} is not a private regular file; fix or delete it"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or not 20 <= info.st_size <= 256
            ):
                raise ServeError(message)
            return stream.read(257).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ServeError(message) from exc


def ensure_password() -> str:
    path = password_path()
    try:
        # Repeat hostctl's shared-parent check; do not create the hostctl leaf.
        parent = path.parent.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ServeError("Djinn state parent must be owned by you and not writable by others")
        private_directory(path.parent)
        if path.exists() or path.is_symlink():
            return read_password(path)
        tmp = path.with_name(f".opencode-password.{secrets.token_hex(12)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32))
                stream.flush()
                os.fsync(stream.fileno())
            # A concurrent creator wins; read and validate its file below.
            with contextlib.suppress(FileExistsError):
                os.link(tmp, path)
        finally:
            tmp.unlink()
        return read_password(path)
    except (OSError, GitSSHError) as exc:
        raise ServeError(f"Could not create a private OpenCode password file: {path}") from exc


@cache
def helper_source() -> str:
    try:
        source = Path(__file__).with_name("serve_helper.py").read_text()
    except (OSError, UnicodeError) as exc:
        raise ServeError("Could not read the serve helper", code="protocol") from exc
    if len(source.encode()) > 100_000:
        raise ServeError("The serve helper exceeds the exec argument size limit")
    return source


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return cast(dict[str, Any], value)


def _nullable(value: object, expected: type) -> bool:
    return value is None or type(value) is expected


def _validate_reply(data: dict[str, Any], args: list[str]) -> None:
    if args[0] == "status":
        if type(data["busy"]) is not bool or not isinstance(data["pid1_start"], str):
            raise ValueError("invalid status")
        servers = _object(data["servers"])
        if set(servers) != set(NAMES):
            raise ValueError("invalid server names")
        for name in NAMES:
            row = _object(servers[name])
            if (
                row["state"] not in STATES
                or not _nullable(row["pid"], int)
                or any(not _nullable(row[k], str) for k in ("reason", "since", "last_line"))
            ):
                raise ValueError("invalid server state")
            if name == OPENCODE and any(
                type(row[k]) is not bool
                for k in ("listening", "discovery_pid_ok", "foreign_listener")
            ):
                raise ValueError("invalid listener state")
    elif args[0] == "stop":
        results = _object(data["results"])
        if set(results) != set(args[2::2]):
            raise ValueError("invalid stop names")
        for value in results.values():
            row = _object(value)
            if row["result"] not in STOP_RESULTS or not _nullable(row["pid"], int):
                raise ValueError("invalid stop result")
    else:
        if (
            data["name"] != args[0].removesuffix("-start")
            or data["result"] not in START_RESULTS
            or not _nullable(data["pid"], int)
            or not _nullable(data["since"], str)
            or not isinstance(data["log_tail"], list)
            or not all(isinstance(v, str) for v in cast(list[object], data["log_tail"]))
        ):
            raise ValueError("invalid start result")
        if args[0] == "opencode-start" and type(data["stopped_previous"]) is not bool:
            raise ValueError("invalid previous service result")


def exec_helper(
    cid: str,
    args: list[str],
    *,
    stdin: str | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    """The sole helper exec builder. Only OpenCode start may receive stdin."""
    if (
        re.fullmatch(r"[0-9a-f]{64}", cid) is None
        or not args
        or args[0] not in ("status", "stop", "claude-start", "opencode-start")
    ):
        raise ServeError("Invalid helper invocation", code="protocol")
    if (args[0] == "opencode-start") != (stdin is not None):
        raise ServeError("Invalid helper input", code="protocol")
    argv = [
        DOCKER_EXECUTABLE,
        "exec",
        *(["-i"] if stdin is not None else []),
        cid,
        "python3",
        "-I",
        "-c",
        helper_source(),
        *args,
    ]
    try:
        if stdin is None:
            result = _run_captured(argv, timeout=timeout)
        else:
            # Fixed argv; the password travels only on stdin.
            completed = subprocess.run(  # nosec B603
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            result = RunResult(completed.returncode, completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired:
        raise ServeError("helper timed out", code="exec-timeout") from None
    except (OSError, subprocess.SubprocessError):
        raise ServeError("helper failed", code="protocol") from None
    if result.returncode == 124:
        raise ServeError("helper timed out", code="exec-timeout")
    try:
        data = _object(json.loads(result.stdout))
        if data.get("ok") is False and result.returncode == 1:
            if data["code"] not in ERROR_CODES or not isinstance(data["message"], str):
                raise ValueError("invalid error")
            raise ServeError(data["message"], code=data["code"])
        if data.get("ok") is not True or not result.success:
            raise ValueError("invalid result")
        _validate_reply(data, args)
        return data
    except (ValueError, TypeError, KeyError):
        raise ServeError("helper failed", code="protocol", detail=result.stderr[:200]) from None


def _layers(root: dict[str, Any]) -> list[dict[str, Any]]:
    layers = [root]
    for value in _object(root.get("Foreground", {})).values():
        layers.extend(_layers(_object(value)))
    return layers


def _target_port(target: str) -> int | None:
    try:
        return urlsplit(target if "://" in target else "//" + target).port
    except ValueError:
        return None


def tailnet_state(port: int, *, timeout: float = 5) -> tuple[str | None, str, bool]:
    binary = shutil.which("tailscale")
    if binary is None:
        return None, "tailscale not found on PATH", False
    try:
        result = _run_captured([binary, "serve", "status", "--json"], timeout=timeout)
        if not result.success:
            return None, f"tailscale serve status failed (exit {result.returncode})", False
        layers = _layers(_object(json.loads(result.stdout)))
        funnel_keys: set[str] = set()
        for layer in layers:
            funnel_keys.update(
                k for k, v in _object(layer.get("AllowFunnel", {})).items() if v is True
            )
        candidates: list[tuple[str, str]] = []
        matching: set[str] = set()
        for layer in layers:
            tcp = _object(layer.get("TCP", {}))
            for key, value in _object(layer.get("Web", {})).items():
                host, sep, https_port = key.rpartition(":")
                if not sep:
                    raise ValueError("invalid web key")
                handlers = _object(_object(value).get("Handlers", {}))
                for path, handler in handlers.items():
                    target = _object(handler).get("Proxy")
                    if not isinstance(target, str):
                        continue
                    if _target_port(target) == port:
                        matching.add(key)
                    strict = target.rstrip("/") in (
                        f"http://127.0.0.1:{port}",
                        f"http://localhost:{port}",
                        f"http://[::1]:{port}",
                    )
                    if (
                        path == "/"
                        and strict
                        and _object(tcp.get(https_port, {})).get("HTTPS") is True
                    ):
                        url = f"https://{host}" + ("" if https_port == "443" else f":{https_port}")
                        # DNS labels also need an alphanumeric first character.
                        if (
                            re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?", url)
                            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host)
                            and https_port.isdigit()
                            and 1 <= int(https_port) <= 65535
                        ):
                            candidates.append((key, url))
            for tcp_port, value in tcp.items():
                forward = _object(value).get("TCPForward")
                if isinstance(forward, str) and _target_port(forward) == port:
                    matching.update(
                        key for key in funnel_keys if key.rpartition(":")[2] == tcp_port
                    )
        funnel = bool(funnel_keys & matching)
        if candidates:
            return sorted(candidates)[0][1], "", funnel
        return None, f"no Serve entry for http://127.0.0.1:{port}", funnel
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None, "unreadable output", False


def claude_prefix_slug() -> str:
    short = socket.gethostname().split(".")[0].lower()
    return re.sub("[^a-z0-9-]", "-", short).strip("-") or "djinn-host"


@dataclass(frozen=True)
class Snapshot:
    container: ContainerInfo
    port: int
    published: bool = False
    busy: bool = False
    servers: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    url: str | None = None
    reason: str = "not queried"
    funnel: bool = False


def collect_snapshot(config: AppConfig | None, *, quick: bool = False) -> Snapshot:
    port = (
        config.serve.opencode_host_port if config is not None else ServeConfig().opencode_host_port
    )
    info = detect_container(timeout=3 if quick else 10)
    if info.state != ContainerState.RUNNING or not info.detached:
        return Snapshot(info, port)
    data = exec_helper(info.id, ["status"], timeout=5 if quick else 15)
    url, reason, funnel = (None, "not queried", False)
    if not quick or data["servers"][OPENCODE]["state"] == "running":
        url, reason, funnel = tailnet_state(port, timeout=2 if quick else 5)
    return Snapshot(
        info,
        port,
        check_opencode_port(info, port),
        data["busy"],
        data["servers"],
        url,
        reason,
        funnel,
    )
