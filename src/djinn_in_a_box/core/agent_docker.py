"""Trusted agent Docker profile and workspace delivery data; no runtime operations."""

from dataclasses import dataclass
from pathlib import PurePosixPath

IMAGE = (
    "docker:29-dind-rootless@"
    "sha256:3acba49741f1aedb28125ffb2307faaec8e870ebb9343de41c21ac549bd6abf0"
)
CACHE = "djinn-agent-docker"
ENDPOINT_PREFIX = "djinn-agent-docker-endpoint-"
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
