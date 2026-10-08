"""Trusted agent Docker profile and workspace delivery data; no runtime operations."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, cast

IMAGE = (
    "docker:29-dind-rootless@"
    "sha256:3acba49741f1aedb28125ffb2307faaec8e870ebb9343de41c21ac549bd6abf0"
)
CACHE = "djinn-agent-docker"
ENDPOINT_PREFIX = "djinn-agent-docker-endpoint-"
STOP_GRACE_SECONDS = 20
STOP_TIMEOUT_SECONDS = STOP_GRACE_SECONDS + 5
ENDPOINT = "/home/rootless/.djinn-docker"
DEV_ENDPOINT = "/run/djinn/agent-docker"
DATA_ROOT = "/home/rootless/.local/share/docker"
SOCKET = ENDPOINT + "/socket/docker.sock"
DOCKER_HOST = "unix://" + DEV_ENDPOINT + "/socket/docker.sock"
SELECTORS = frozenset(
    {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_TLS_CERTDIR", "DOCKER_CERT_PATH"}
)
ENDPOINT_OPTIONS = {"type": "tmpfs", "device": "tmpfs", "o": "uid=1000,gid=1000,mode=0700,size=1m"}
COMMAND = [
    "dockerd",
    "--host=unix://" + SOCKET,
    "--config-file=/dev/null",
    "--data-root=" + DATA_ROOT,
    "--storage-driver=overlay2",
    "--tls=false",
]
INTERNAL_TARGETS = (
    "/home/rootless",
    "/run",
    "/bin",
    "/sbin",
    "/usr",
    "/etc",
    "/lib",
    "/lib64",
    "/var/lib/docker",
)

# Engine defaults plus the measured rootless relaxations. Delivery is checked
# separately; resource values and network identity come from trusted preparation.
HOST_PROFILE: dict[str, Any] = {
    "Privileged": False,
    "CapAdd": None,
    "CapDrop": None,
    "Devices": [
        {
            "PathOnHost": "/dev/net/tun",
            "PathInContainer": "/dev/net/tun",
            "CgroupPermissions": "rwm",
        }
    ],
    "DeviceRequests": None,
    "DeviceCgroupRules": None,
    "SecurityOpt": ["seccomp=unconfined"],
    "MaskedPaths": [],
    "ReadonlyPaths": [],
    "PidMode": "",
    "UTSMode": "",
    "UsernsMode": "",
    "IpcMode": "private",
    "CgroupnsMode": "private",
    "Cgroup": "",
    "CgroupParent": "",
    "PortBindings": {},
    "PublishAllPorts": False,
    "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
    "Tmpfs": {"/var/lib/docker": ""},
    "ReadonlyRootfs": False,
    "AutoRemove": False,
    "VolumeDriver": "",
    "VolumesFrom": None,
    "Runtime": "runc",
    "Isolation": "",
    "ShmSize": 67108864,
    "GroupAdd": None,
    "Links": None,
    "Dns": None,
    "DnsOptions": None,
    "DnsSearch": None,
    "ExtraHosts": [],
    "OomScoreAdj": 0,
    "CpuShares": 0,
    "CpuPeriod": 0,
    "CpuQuota": 0,
    "CpuRealtimePeriod": 0,
    "CpuRealtimeRuntime": 0,
    "CpusetCpus": "",
    "CpusetMems": "",
    "BlkioWeight": 0,
    "BlkioWeightDevice": None,
    "BlkioDeviceReadBps": None,
    "BlkioDeviceWriteBps": None,
    "BlkioDeviceReadIOps": None,
    "BlkioDeviceWriteIOps": None,
    "MemorySwappiness": None,
    "OomKillDisable": None,
    "PidsLimit": None,
    "Ulimits": None,
    "CpuCount": 0,
    "CpuPercent": 0,
    "IOMaximumIOps": 0,
    "IOMaximumBandwidth": 0,
}


def mount_delivery(mounts: list[dict[str, Any]]) -> list[tuple[str, str, str, bool]]:
    if any(not isinstance(m["RW"], bool) for m in mounts):
        raise ValueError("mount writability unavailable")
    return sorted(
        (m["Type"], m["Name"] if m["Type"] == "volume" else m["Source"], m["Destination"], m["RW"])
        for m in mounts
    )


@dataclass(frozen=True)
class EndpointVerification:
    kind: str
    detail: str
    in_use: bool = False
    errors: tuple[str, ...] = ()
    companion_id: str | None = None
    generation: str | None = None
    network_id: str | None = None
    fingerprint: str | None = None
    health: str = "unknown"
    version: str = "unknown"
    cache: str = "unknown"

    @property
    def evidence(self) -> dict[str, str] | None:
        if self.kind != "agent":
            return None
        return {
            "id": str(self.companion_id),
            "generation": str(self.generation),
            "network_id": str(self.network_id),
            "fingerprint": str(self.fingerprint),
        }


def inspected_environment(entries: object) -> dict[str, str]:
    # Engine inspect retains Compose's unset environment entries as bare names.
    if not isinstance(entries, list):
        raise ValueError("environment inspection unavailable")
    environment: dict[str, str] = {}
    for entry in cast(list[object], entries):
        if not isinstance(entry, str):
            raise ValueError("malformed environment entry")
        name, _, value = entry.partition("=")
        if not name or name in environment:
            raise ValueError("missing or duplicate environment key")
        environment[name] = value
    return environment


def endpoint_selection(dev: dict[str, Any] | None) -> EndpointVerification:
    """Classify inspected selectors before collecting managed-agent evidence."""
    if dev is None:
        return EndpointVerification("none", "none (no running dev)")
    managed = False
    try:
        managed = any(m["Destination"] == DEV_ENDPOINT for m in dev["Mounts"])
        entries = dev["Config"]["Env"]
        env = inspected_environment(entries)
        endpoint = env.get("DOCKER_HOST", "")
        managed = managed or endpoint == DOCKER_HOST
        # Never display credentials embedded in an arbitrary endpoint or context.
        if any(env.get(key) for key in SELECTORS - {"DOCKER_HOST"}):
            raise ValueError("Docker context/TLS selector overrides inspected endpoint")
        if managed:
            return EndpointVerification("unknown", DOCKER_HOST, in_use=True)
        if endpoint and endpoint != "unix:///var/run/docker.sock":
            raise ValueError("unverified Docker endpoint")
        if endpoint == "unix:///var/run/docker.sock" or any(
            m["Destination"] == "/var/run/docker.sock" for m in dev["Mounts"]
        ):
            return EndpointVerification("host-direct", "host-direct (host Docker socket)")
        return EndpointVerification("none", "none")
    except (KeyError, TypeError, ValueError) as exc:
        return EndpointVerification("unknown", "unknown", in_use=managed, errors=(str(exc),))


def verify_endpoint(
    dev: dict[str, Any],
    state: dict[str, Any],
    companion: dict[str, Any],
    image: dict[str, Any],
    endpoint: dict[str, Any],
    cache: dict[str, Any],
    network: dict[str, Any],
    consumers: list[str],
    *,
    planned_generation: str | None = None,
) -> EndpointVerification:
    """Pure verification over host-owned state and host-inspected evidence."""
    from djinn_in_a_box.core import docker, host_runtime

    try:
        manifest = state["agent_docker"]
        gen = state["generation"]
        labels = dev["Config"]["Labels"]
        record = state["resources"]["agent-docker"]
        if (
            not gen
            or labels[host_runtime.GENERATION_LABEL] != gen
            or (
                planned_generation != gen
                if planned_generation is not None
                else state["dev_id"] != dev["Id"]
            )
            or dev["State"]["Running"] is not True
            or (planned_generation is None and dev["Name"] != "/" + state["container_name"])
            or companion["Id"] != record["id"]
            or record["service"] != "agent-docker"
            or companion["Name"] != "/" + manifest["name"]
            or companion["Config"]["Labels"][host_runtime.GENERATION_LABEL] != gen
            or companion["Config"]["Labels"]["com.docker.compose.project"] != record["project"]
            or companion["Config"]["Labels"]["com.docker.compose.service"] != "agent-docker"
            or manifest["image"] != IMAGE
            or image["Id"] != manifest["image_id"]
            or "docker@" + IMAGE.split("@", 1)[1] not in image["RepoDigests"]
        ):
            raise ValueError("agent Docker generation/identity differs")
        docker.require_agent_profile(companion, manifest)
        if (
            companion["State"]["Running"] is not True
            or companion["State"]["Health"]["Status"] != "healthy"
        ):
            raise ValueError("agent Docker is not running and healthy")
        if (
            endpoint["Name"] != ENDPOINT_PREFIX + gen
            or endpoint["Name"] != manifest["endpoint_volume"]
            or endpoint["Driver"] != "local"
            or endpoint["Options"] != ENDPOINT_OPTIONS
            or endpoint["Labels"][host_runtime.GENERATION_LABEL] != gen
            or endpoint["Labels"]["com.docker.compose.project"] != record["project"]
            or cache["Name"] != manifest["cache_volume"]
            or cache["Driver"] != "local"
            or cache["Options"] not in (None, {})
        ):
            raise ValueError("agent Docker storage differs")
        if (
            network["Id"] != manifest["network_id"]
            or network["Name"] != manifest["network"]
            or set(dev["NetworkSettings"]["Networks"]) != {manifest["network"]}
            or dev["NetworkSettings"]["Networks"][manifest["network"]]["NetworkID"] != network["Id"]
        ):
            raise ValueError("agent Docker network differs")
        expected_users = {companion["Id"]}
        if planned_generation is None:
            expected_users.add(dev["Id"])
        elif state.get("dev_id"):
            expected_users.add(state["dev_id"])
        if set(consumers) != expected_users or len(consumers) != len(expected_users):
            raise ValueError("agent Docker endpoint has unexpected consumers")
        selection = endpoint_selection(dev)
        env = inspected_environment(dev["Config"]["Env"])
        if selection.errors or env.get("DOCKER_HOST") != DOCKER_HOST:
            raise ValueError("dev Docker selectors differ")
        dev_mounts = dev["Mounts"]
        delivery = mount_delivery(dev_mounts)
        endpoint_mount = ("volume", endpoint["Name"], DEV_ENDPOINT, False)
        if delivery.count(endpoint_mount) != 1:
            raise ValueError("dev endpoint is not the managed read-only volume")
        for mount in dev_mounts:
            target = PurePosixPath(mount["Destination"])
            managed_target = PurePosixPath(DEV_ENDPOINT)
            if target != managed_target and (
                target.is_relative_to(managed_target) or managed_target.is_relative_to(target)
            ):
                raise ValueError("dev endpoint is shadowed")
            if mount.get("Name") == endpoint["Name"] and mount["Destination"] != DEV_ENDPOINT:
                raise ValueError("dev endpoint has an alias")
        for row in manifest["mounts"]:
            if row[2] in (ENDPOINT, DATA_ROOT):
                continue
            if tuple(row) not in delivery:
                raise ValueError("companion workspace exceeds dev delivery")
        for mount in companion["Mounts"]:
            if mount["Destination"] not in (ENDPOINT, DATA_ROOT) and not any(
                all(
                    mount.get(key) == candidate.get(key)
                    for key in ("Type", "Name", "Source", "Destination", "RW", "Propagation")
                )
                for candidate in dev_mounts
            ):
                raise ValueError("companion workspace backing exceeds dev delivery")
            if mount["Type"] == "volume" and mount["Name"] in (endpoint["Name"], cache["Name"]):
                volume = endpoint if mount["Name"] == endpoint["Name"] else cache
                if mount["Source"] != volume["Mountpoint"] or mount["Driver"] != "local":
                    raise ValueError("companion volume backing differs")
        for mount in dev_mounts:
            if mount.get("Name") == endpoint["Name"] and mount["Source"] != endpoint["Mountpoint"]:
                raise ValueError("dev endpoint backing differs")
        profile = {
            "Config": {
                key: companion["Config"][key]
                for key in ("Env", "Cmd", "Entrypoint", "User", "Healthcheck")
            },
            "HostConfig": {
                key: companion["HostConfig"][key]
                for key in (*HOST_PROFILE, *manifest["limits"], "NetworkMode", "MemorySwap")
            },
            "Mounts": companion["Mounts"],
            "Networks": companion["NetworkSettings"]["Networks"],
            "endpoint": endpoint,
            "cache": cache,
            "delivery": delivery,
        }
        fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        version = dict(item.split("=", 1) for item in manifest["environment"])["DOCKER_VERSION"]
        return EndpointVerification(
            "agent",
            DOCKER_HOST,
            True,
            companion_id=companion["Id"],
            generation=gen,
            network_id=network["Id"],
            fingerprint=fingerprint,
            health="running / healthy",
            version=version,
            cache=cache["Name"],
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        return EndpointVerification("unknown", DOCKER_HOST, True, errors=(str(exc),))


@dataclass(frozen=True)
class WorkspaceMount:
    kind: str
    source: str
    target: str
    read_only: bool = False

    def compose(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "type": self.kind,
            "source": self.source.replace("$", "$$"),
            "target": self.target.replace("$", "$$"),
            "read_only": self.read_only,
        }
        if self.kind == "bind":
            entry["bind"] = {"create_host_path": False, "propagation": "rprivate"}
        return entry

    def require_safe_target(self) -> None:
        target = PurePosixPath(self.target)
        for name in INTERNAL_TARGETS:
            internal = PurePosixPath(name)
            if (
                target == internal
                or target.is_relative_to(internal)
                or internal.is_relative_to(target)
            ):
                raise ValueError(
                    f"Workspace target {target} shadows agent Docker internals ({name})"
                )
