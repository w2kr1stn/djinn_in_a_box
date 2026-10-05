"""Generate container-local Git config using Git's writer and public selectors."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


def generate(manifest: Path, home: Path) -> None:
    data = json.loads(manifest.read_text())
    settings: dict[str, str] = {"gpg.format": "ssh"}
    for field, setting in (
        ("signing_key", "user.signingkey"),
        ("allowed_signers", "gpg.ssh.allowedSignersFile"),
    ):
        value = data.get(field)
        if value is not None:
            if (
                not isinstance(value, str)
                or Path(value).parent != Path("/home/dev/.ssh")
                or any(char in value for char in "\n\r\x00")
            ):
                raise ValueError(f"invalid public Git manifest field: {field}")
            if field == "signing_key" and value not in {
                identity["public_key_file"] for identity in data["identities"].values()
            }:
                raise ValueError("signing key is not a declared public selector")
            settings[setting] = value
    ignore = home / ".gitignore_global"
    if ignore.is_file():
        settings["core.excludesfile"] = str(ignore)
    fd, temporary = tempfile.mkstemp(dir=home)
    os.close(fd)
    try:
        for key, value in settings.items():
            subprocess.run(["git", "config", "--file", temporary, key, value], check=True)
        os.replace(temporary, home / ".gitconfig_local")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    generate(Path(os.environ["DJINN_GIT_MANIFEST"]), Path.home())
