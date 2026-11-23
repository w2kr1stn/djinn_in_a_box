"""Host Docker CLI resolution and inspection diagnostics."""

import shutil
from pathlib import Path
from typing import Literal

DOCKER_EXECUTABLE = str(Path(shutil.which("docker") or "/usr/bin/docker").absolute())

type InspectResource = Literal["container", "volume", "network", "image"]


def resource_absent(stderr: str, resource: InspectResource, name: str) -> bool:
    """Match a complete Docker diagnostic for this one object."""
    message = stderr.rstrip("\r\n")
    for prefix in ("Error response from daemon: ", "Error: "):
        if message.startswith(prefix):
            message = message.removeprefix(prefix)
            break
    if resource == "network":
        return message == f"network {name} not found"
    if resource == "volume":
        return message == f"get {name}: no such volume"
    expected = f"No such {resource}: {name}"
    if resource == "image" and ":" not in name.rsplit("/", 1)[-1] and "@" not in name:
        return message in (expected, expected + ":latest")
    return message == expected
