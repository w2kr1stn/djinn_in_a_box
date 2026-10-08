"""Host-side runtime assessment and trusted direct TCP diagnostics."""

from __future__ import annotations

import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from djinn_in_a_box.core import agent_docker, desktop, host_runtime, hostctl
from djinn_in_a_box.core import paths as host_paths
from djinn_in_a_box.core.paths import get_project_root

PROBE_IMAGE = (
    "python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a"
)
PREREQUISITE = "Host prerequisite: no forwarding from the Docker network into the tailnet"
PROBE_CODE = """import concurrent.futures, errno, json, socket, sys
def probe(row):
    host, address = row
    state = "unknown"
    with socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET) as s:
        s.settimeout(1.5)
        try:
            s.connect((address, 22))
            state = "reached"
        except OSError as e:
            if isinstance(e, TimeoutError) or e.errno in (errno.ECONNREFUSED, errno.ETIMEDOUT,
                                                        errno.ENETUNREACH, errno.EHOSTUNREACH):
                state = "blocked"
    return {"host": host, "address": address, "state": state}
with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
    print(json.dumps(list(pool.map(probe, json.loads(sys.argv[1])))))
"""


@dataclass(frozen=True)
class Assessment:
    dev_id: str | None
    causes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    agent: dict[str, str] | None = None

    @property
    def state(self) -> str:
        if self.errors:
            return "unknown"
        if self.causes:
            return "unsealed"
        return "sealed" if self.dev_id else "deferred"

    def require(self, *, allow_unsealed: bool = False) -> None:
        if self.errors or (self.causes and not allow_unsealed):
            raise hostctl.HostctlError(
                "Sealing refused: " + "; ".join((*self.causes, *self.errors))
            )


def canonical(path: str | Path) -> Path:
    """Resolve existing host ancestors as well as not-yet-created protected paths."""
    value = Path(path)
    if not value.is_absolute():
        raise ValueError(f"non-absolute host path: {value}")
    parent = value
    suffix: list[str] = []
    while not parent.exists():
        if parent.is_symlink():
            raise ValueError(f"dangling host alias: {parent}")
        suffix.insert(0, parent.name)
        parent = parent.parent
    return parent.resolve(strict=True).joinpath(*suffix)


def overlap(a: Path, b: Path) -> bool:
    return a.is_relative_to(b) or b.is_relative_to(a)


def host_file(path: Path) -> Path:
    """Local Docker hosts expose bind sources in the controller's filesystem."""
    return path


def protected_overlap(source: Path, protected: Path) -> bool:
    if overlap(source, protected):
        return True

    def same(a: Path, b: Path) -> bool:
        return (
            host_file(a).exists() and host_file(b).exists() and host_file(a).samefile(host_file(b))
        )

    return any(same(source, ancestor) for ancestor in (protected, *protected.parents)) or any(
        same(protected, ancestor) for ancestor in (source, *source.parents)
    )


def execution_paths() -> dict[str, Path]:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    config = Path(os.environ.get("DOCKER_CONFIG", str(home / ".docker")))
    paths = {
        "Djinn installation/build context": get_project_root(),
        "Djinn configuration": host_paths.CONFIG_DIR,
        "Djinn configuration file": host_paths.CONFIG_FILE,
        "Djinn agent definitions": host_paths.AGENTS_FILE,
        "Djinn zone definitions": host_paths.ZONES_FILE,
        "Python environment": Path(sys.prefix),
        "Python base environment": Path(sys.base_prefix),
        "Python executable": Path(sys.executable),
        "Docker CLI": Path(hostctl.DOCKER_EXECUTABLE),
        "Docker configuration": config,
        "Docker plugins": config / "cli-plugins",
    }
    # Python's effective import directories are part of its execution inputs.
    for entry in sys.path:
        paths[f"Python import directory {entry or os.getcwd()}"] = Path(entry or os.getcwd())
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if filename and Path(filename).is_absolute():
            paths[f"Python module {name}"] = Path(filename)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        paths[f"host PATH directory {entry or os.getcwd()}"] = Path(entry or os.getcwd())
    for entry in (
        "/usr/local/lib/docker/cli-plugins",
        "/usr/local/libexec/docker/cli-plugins",
        "/usr/lib/docker/cli-plugins",
        "/usr/libexec/docker/cli-plugins",
    ):
        paths[f"Docker plugin directory {entry}"] = Path(entry)
    config_file = config / "config.json"
    if config_file.exists():
        data = json.loads(config_file.read_text())
        for entry in data.get("cliPluginsExtraDirs", []):
            paths[f"Docker plugin directory {entry}"] = Path(entry)
    return {name: canonical(path) for name, path in paths.items()}


def _socket_is_docker(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(0.3)
        client.connect(str(path))
        client.sendall(b"GET /_ping HTTP/1.0\r\n\r\n")
        if b"200 OK" in client.recv(4096):
            return True
        raise OSError("unrecognized Unix socket response")


def docker_sockets() -> tuple[Path, ...]:
    paths = [
        Path("/run/docker.sock"),
        Path("/var/run/docker.sock"),
        Path(f"/run/user/{os.getuid()}/docker.sock"),
    ]
    endpoint = os.environ.get("DOCKER_HOST", "")
    if endpoint.startswith("unix://"):
        paths.append(Path(endpoint[7:]))
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    config = Path(os.environ.get("DOCKER_CONFIG", str(home / ".docker")))
    for meta in (config / "contexts/meta").glob("*/meta.json"):
        endpoint = (
            json.loads(meta.read_text()).get("Endpoints", {}).get("docker", {}).get("Host", "")
        )
        if endpoint.startswith("unix://"):
            paths.append(Path(endpoint[7:]))
    return tuple(dict.fromkeys(canonical(p) for p in paths))


def nested_sensitive_files(source: Path) -> list[Path]:
    """Inventory sockets and hard links; incomplete provenance cannot certify a bind."""
    output: list[Path] = []
    deadline = time.monotonic() + 5
    count = 0

    def failed(exc: OSError) -> None:
        raise exc

    for root, _dirs, files in os.walk(host_file(source), onerror=failed, followlinks=False):
        for name in files:
            count += 1
            if count > 250000 or time.monotonic() > deadline:
                raise ValueError(f"socket inventory incomplete for {source}")
            path = Path(root) / name
            info = path.stat()
            if stat.S_ISSOCK(info.st_mode) or info.st_nlink > 1:
                output.append(path)
    return output


def _desktop(actual: dict[str, Any]) -> desktop.DesktopInspection:
    from djinn_in_a_box.core import docker

    # Reuse the canonical live inspector with an explicit ID; it owns provenance.
    return docker.inspect_running_desktop(str(actual["Id"]), dev_inspect=actual)


def volume_bind_source(mount: dict[str, Any]) -> str | None:
    if "PlannedVolume" in mount:
        volume = mount["PlannedVolume"]
    else:
        volume = host_runtime.inspect_object(mount["Name"], hostctl.DOCKER_EXECUTABLE, "volume")
    if not isinstance(volume, dict):
        raise ValueError(f"volume storage provenance unknown: {mount['Name']}")
    volume = cast(dict[str, Any], volume)
    if volume.get("Driver") != "local":
        raise ValueError(f"volume storage provenance unknown: {mount['Name']}")
    options: Any = volume.get("Options") or {}
    if not isinstance(options, dict):
        raise ValueError(f"volume storage options unknown: {mount['Name']}")
    options = cast(dict[str, Any], options)
    if not options:
        return None
    if set(options.get("o", "").split(",")) & {"bind", "rbind"} and options.get("device"):
        return str(options["device"])
    raise ValueError(f"volume storage provenance unknown: {mount['Name']}")


def assess(actual: dict[str, Any] | None, *, planned_generation: str | None = None) -> Assessment:
    from djinn_in_a_box.core import docker

    if actual is None:
        return Assessment(None)
    causes: list[str] = []
    errors: list[str] = []
    identity = actual.get("Id")
    verified = docker.inspect_agent_endpoint(actual, planned_generation=planned_generation)
    errors.extend(verified.errors)
    try:
        if not isinstance(identity, str) or not identity or actual["State"]["Running"] is not True:
            raise ValueError("running dev ID/state unavailable")
        mounts = actual["Mounts"]
        env = actual["Config"]["Env"]
        networks = actual["NetworkSettings"]["Networks"]
        if (
            not isinstance(mounts, list)
            or not isinstance(env, list)
            or not isinstance(networks, dict)
        ):
            raise ValueError("dev mounts/environment/network attachments unavailable")
        mounts = cast(list[dict[str, Any]], mounts)
        env = cast(list[str], env)
        networks = cast(dict[str, Any], networks)
        environment = agent_docker.inspected_environment(env)
        home = canonical(pwd.getpwuid(os.getuid()).pw_dir)
        info = json.loads(hostctl.command("info", "--format", "{{json .}}"))
        data_root = canonical(info["DockerRootDir"])
        volume = host_runtime.inspect_object(
            hostctl.HOSTCTL_STATE_VOLUME, hostctl.DOCKER_EXECUTABLE, "volume"
        )
        backing = canonical(volume["Mountpoint"]) if volume is not None else data_root
        private = {
            "host controller/journal": canonical(hostctl.state_root(create=False)),
            "journal": canonical(hostctl.state_root(create=False) / "journal.jsonl"),
            "private agent": canonical(host_runtime.runtime_root() / "private"),
            "private agent socket": canonical(host_runtime.runtime_root() / "private/agent.sock"),
            "host runtime control": canonical(host_runtime.runtime_root() / "runtime.json"),
        }
        execution = execution_paths()
        sockets = docker_sockets()
        effective: list[dict[str, Any]] = []
        for raw_mount in cast(list[Any], mounts):
            if not isinstance(raw_mount, dict):
                errors.append("mount inspection uncertain: malformed mount object")
                continue
            mount = cast(dict[str, Any], raw_mount)
            try:
                if mount["Type"] == "volume" and mount.get("Name") != hostctl.HOSTCTL_STATE_VOLUME:
                    if (
                        verified.kind == "agent"
                        and mount["Destination"] == agent_docker.DEV_ENDPOINT
                    ):
                        effective.append(mount)
                        continue
                    source_alias = volume_bind_source(mount)
                    if source_alias is not None:
                        mount = {**mount, "Type": "bind", "Source": source_alias}
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                errors.append(f"volume inspection uncertain: {exc}")
            effective.append(mount)
        mounts = effective
        actual = {**actual, "Mounts": effective}
        inspection = _desktop(actual)
        for mount in mounts:
            try:
                before = len(causes)
                if mount["Type"] not in ("bind", "volume", "tmpfs"):
                    raise ValueError("unknown dev mount type")
                if mount["Type"] == "volume" and mount.get("Name") == hostctl.HOSTCTL_STATE_VOLUME:
                    causes.append("helper identity volume exposed to dev")
                    continue
                if mount["Type"] != "bind":
                    continue
                if not isinstance(mount["RW"], bool):
                    raise ValueError("bind writability unavailable")
                source = canonical(mount["Source"])
                if not host_file(source).exists():
                    raise ValueError(f"host bind source cannot be inspected: {mount['Source']}")
                label = f"{mount['Source']} -> {mount['Destination']}"
                if protected_overlap(source, Path("/")) and host_file(source).samefile(
                    host_file(Path("/"))
                ):
                    causes.append(f"host root bind: {label}")
                if protected_overlap(source, home) and (
                    source == home or not source.is_relative_to(home)
                ):
                    causes.append(f"user home or ancestor bind: {label}")
                if protected_overlap(source, backing) or protected_overlap(source, data_root):
                    causes.append(f"Docker data root/helper backing storage bind: {label}")
                for name, path in private.items():
                    if protected_overlap(source, path):
                        causes.append(f"exposed {name}: {label}")
                for path in sockets:
                    if protected_overlap(source, path):
                        causes.append(f"Docker socket {path}: {label}")
                if (
                    len(causes) == before
                    and label not in inspection.raw_sources
                    and host_file(source).exists()
                    and stat.S_ISSOCK(host_file(source).stat().st_mode)
                    and source not in sockets
                    and source != canonical(host_runtime.runtime_root() / "export/auth.sock")
                ):
                    try:
                        if _socket_is_docker(host_file(source)):
                            causes.append(f"relocated Docker socket: {label}")
                    except OSError:
                        errors.append(f"socket provenance unknown: {label}")
                if mount["RW"]:
                    for name, path in execution.items():
                        if protected_overlap(source, path):
                            causes.append(f"writable {name}: {label}")
                if (
                    len(causes) == before
                    and host_file(source).is_dir()
                    and source != canonical(host_runtime.runtime_root() / "export")
                    and not inspection.raw_sources
                ):
                    for candidate in nested_sensitive_files(source):
                        info = candidate.stat()
                        if not stat.S_ISSOCK(info.st_mode):
                            known = len(causes)
                            candidate_source = canonical(candidate)
                            for name, path in private.items():
                                if protected_overlap(candidate_source, path):
                                    causes.append(
                                        f"exposed {name} inode alias {candidate}: {label}"
                                    )
                            if mount["RW"]:
                                for name, path in execution.items():
                                    if protected_overlap(candidate_source, path):
                                        causes.append(
                                            f"writable {name} inode alias {candidate}: {label}"
                                        )
                            if len(causes) == known:
                                errors.append(f"hard-link provenance unknown: {candidate}: {label}")
                            continue
                        if any(
                            host_file(p).exists() and candidate.samefile(host_file(p))
                            for p in sockets
                        ) or _socket_is_docker(candidate):
                            causes.append(f"relocated Docker socket {candidate}: {label}")
                if (
                    len(causes) == before
                    and host_file(source).is_file()
                    and host_file(source).stat().st_nlink > 1
                ):
                    errors.append(f"hard-link provenance unknown: {label}")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"bind inspection uncertain: {exc}")
        endpoint = environment.get("DOCKER_HOST", "")
        if endpoint and verified.kind != "agent":
            causes.append("Unverified Docker endpoint exposed in dev environment")
        # A socket-bearing/proxy container on an attached network grants Docker
        # authority regardless of how this dev was originally started.
        for other_id in hostctl.command("ps", "-q").split():
            if other_id == identity:
                continue
            other = host_runtime.inspect_object(other_id, hostctl.DOCKER_EXECUTABLE)
            if other is None:
                raise ValueError("network peer disappeared during Docker inspection")
            if verified.kind == "agent" and other["Id"] == verified.companion_id:
                continue
            shared = set(networks).intersection(other["NetworkSettings"]["Networks"])
            published: dict[str, Any] = other["NetworkSettings"].get("Ports") or {}
            host_ports = any(
                binding["HostIp"] not in ("127.0.0.1", "::1")
                for bindings in published.values()
                for binding in cast(list[dict[str, Any]], bindings or [])
            )
            host_network = actual["HostConfig"]["NetworkMode"] == "host"
            if not shared and not host_network and not host_ports:
                continue
            image = other["Config"]["Image"]
            exposed: dict[str, Any] = other["Config"].get("ExposedPorts") or {}
            socket_mount = False
            if exposed:
                for mount in other["Mounts"]:
                    source_alias = (
                        mount["Source"]
                        if mount["Type"] == "bind"
                        else (volume_bind_source(mount) if mount["Type"] == "volume" else None)
                    )
                    if source_alias is None:
                        continue
                    source = canonical(source_alias)
                    if any(protected_overlap(source, p) for p in sockets) or (
                        stat.S_ISSOCK(host_file(source).stat().st_mode)
                        and _socket_is_docker(host_file(source))
                    ):
                        socket_mount = True
                        break
            if (
                "docker-socket-proxy" in image
                or "2375/tcp" in exposed
                or "2376/tcp" in exposed
                or (socket_mount and exposed)
            ):
                route = ", ".join(sorted(shared)) or (
                    "host network" if host_network else "host published ports"
                )
                causes.append(f"reachable Docker proxy {other.get('Name', other_id)} on {route}")
        for row in inspection.channels:
            if row.sealed_ok is False:
                causes.extend(f"{row.channel}: {reason}" for reason in row.reasons or (row.state,))
            elif row.sealed_ok is None:
                errors.append(f"{row.channel} inspection unknown: {row.detail}")
        causes.extend(f"raw host desktop endpoint: {source}" for source in inspection.raw_sources)
        if not inspection.raw_verified:
            errors.append("raw desktop endpoint inspection unknown")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        errors.append(f"inspection uncertain: {exc}")
    return Assessment(
        identity, tuple(dict.fromkeys(causes)), tuple(dict.fromkeys(errors)), verified.evidence
    )


def inspect_assessment(name: str | None = None) -> Assessment:
    from djinn_in_a_box.core import docker

    try:
        actual = host_runtime.inspect_object(
            name or docker.service_container_name("dev"), hostctl.DOCKER_EXECUTABLE
        )
        if actual is not None and actual.get("State", {}).get("Running") is False:
            return Assessment(None)
        return assess(actual)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return Assessment(None, errors=(f"dev inspection uncertain: {exc}",))


def probe_addresses(trust: dict[str, Any]) -> list[list[str]]:
    from djinn_in_a_box.core.ssh_delivery import tailnet_ip

    rows = [
        [host, tailnet_ip(address)]
        for host, peer in trust["peers"].items()
        for address in peer["ips"]
    ]
    if not rows or len(rows) > 4096 or len({tuple(r) for r in rows}) != len(rows):
        raise ValueError("missing, duplicate or excessive peer addresses")
    return rows


def run_probe(dev_id: str, trust: dict[str, Any], *, timeout: float = 30) -> list[dict[str, str]]:
    rows = probe_addresses(trust)
    name = "djinn-hostctl-probe-" + uuid.uuid4().hex
    try:
        raw = hostctl.command(
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            f"container:{dev_id}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "python3",
            PROBE_IMAGE,
            "-I",
            "-c",
            PROBE_CODE,
            json.dumps(rows),
            timeout=timeout,
        )
        raw_output: Any = json.loads(raw)
        if not isinstance(raw_output, list):
            raise ValueError("probe output is not a list")
        output = cast(list[Any], raw_output)
        if len(output) != len(rows):
            raise ValueError("probe output count mismatch")
        for expected, result in zip(rows, output, strict=True):
            if (
                not isinstance(result, dict)
                or set(cast(dict[str, Any], result)) != {"host", "address", "state"}
                or [result["host"], result["address"]] != expected
                or result["state"] not in ("reached", "blocked", "unknown")
            ):
                raise ValueError("invalid per-address probe output")
        return [{**row, "namespace": dev_id} for row in cast(list[dict[str, str]], output)]
    finally:
        # Also runs for KeyboardInterrupt and killed/timed-out docker clients.
        if host_runtime.inspect_object(name, hostctl.DOCKER_EXECUTABLE) is not None:
            hostctl.command("rm", "-f", name)
        if host_runtime.inspect_object(name, hostctl.DOCKER_EXECUTABLE) is not None:
            raise hostctl.HostctlError("probe cleanup could not be verified")


def require_blocked(rows: list[dict[str, str]]) -> None:
    failures = [
        f"{r.get('namespace', 'dev')} {r['host']} {r['address']}: {r['state']}"
        for r in rows
        if r["state"] != "blocked"
    ]
    if failures:
        raise hostctl.HostctlError(
            "Direct route refused: " + "; ".join(failures) + ". " + PREREQUISITE
        )
