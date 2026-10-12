from __future__ import annotations

import re
from pathlib import Path

from djinn_in_a_box.config.defaults import KNOWN_CONFIG_ROOT_ENTRIES
from djinn_in_a_box.core.docker import repo_owned_submount_targets

_ROOT = Path(__file__).resolve().parents[1]


def _docker_instructions():
    source = (_ROOT / "Dockerfile").read_text()
    return [
        line.strip()
        for line in re.sub(r"\\\n\s*", " ", source).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_package_image_delivery():
    instructions = _docker_instructions()
    copies = {
        "scripts/apt-get-shim.sh": ("--chmod=755", "/usr/local/bin/apt-get", "#!/bin/sh"),
        "scripts/djinn-apt-root.sh": (
            "--chmod=755",
            "/usr/local/libexec/djinn-apt-root",
            "#!/bin/sh",
        ),
        "scripts/package-replay.sh": (
            "--chown=dev:dev --chmod=755",
            "/home/dev/package-replay.sh",
            "#!/bin/bash",
        ),
    }
    for path, (flags, destination, shebang) in copies.items():
        matching = [line for line in instructions if line.startswith("COPY ") and path in line]
        assert matching == [f"COPY {flags} {path} {destination}"]
        assert (_ROOT / path).read_text().splitlines()[0] == shebang
    shim = instructions.index("COPY --chmod=755 scripts/apt-get-shim.sh /usr/local/bin/apt-get")
    apt_runs = [
        i for i, line in enumerate(instructions) if line.startswith("RUN ") and "apt-get" in line
    ]
    assert len(apt_runs) == 2 and all(index < shim for index in apt_runs)


def test_package_sudoers_delivery():
    instructions = _docker_instructions()
    user_run = next(line for line in instructions if line.startswith("RUN groupadd "))
    assert 'echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/$USERNAME' in user_run
    assert (
        'echo "$USERNAME ALL=(root) NOPASSWD: /usr/local/libexec/djinn-apt-root" '
        "> /etc/sudoers.d/djinn-apt"
    ) in user_run
    assert "chmod 0440 /etc/sudoers.d/djinn-apt" in user_run
    assert "visudo -cf /etc/sudoers.d/djinn-apt" in user_run
    assert instructions.index(user_run) < instructions.index("USER $USERNAME")


def test_declared_ownership_helper_is_packaged():
    dockerfile = (_ROOT / 'Dockerfile').read_text()
    assert (
        'COPY --chown=dev:dev scripts/ownership-repair.py /home/dev/ownership-repair.py'
        in dockerfile
    )
    assert (_ROOT / 'scripts/ownership-repair.py').is_file()


def test_runtime_delivery_packaging_uses_shared_publisher_and_canonical_mount() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text()
    compose = (_ROOT / "docker-compose.yml").read_text()
    entrypoint = (_ROOT / "scripts" / "entrypoint.sh").read_text()
    session = (_ROOT / "src" / "djinn_in_a_box" / "core" / "session.py").read_text()

    assert (
        "src/djinn_in_a_box/core/workflow_publisher.py /home/dev/workflow-publisher.py"
        in dockerfile
    )
    assert "scripts/settings-copy.py /home/dev/settings-copy.py" in dockerfile
    assert 'LABEL djinn.workflow.publisher="1"' in dockerfile
    assert "opencode-workflow" + "-delivery.py" not in dockerfile
    assert "./config:/home/dev/.djinn-canonical:ro" in compose
    assert "./config/claude/AGENTS.md:/home/dev/.claude/AGENTS.md" in compose
    assert "./templates/claude/CLAUDE.md:/home/dev/.claude/CLAUDE.md:ro" in compose
    assert (_ROOT / "templates/claude/CLAUDE.md").read_bytes() == b"@AGENTS.md\n"
    assert Path("/home/dev/.claude/CLAUDE.md") in repo_owned_submount_targets(
        Path("/home/dev/.claude")
    )
    assert "CLAUDE.md" in KNOWN_CONFIG_ROOT_ENTRIES["claude"]
    assert "/home/dev/workflow-publisher.py" in entrypoint
    assert "--canonical-root \"$CANONICAL_CONFIG_ROOT\"" in entrypoint
    assert "/home/dev/workflow-publisher.py" in session
    assert '"/home/dev/.djinn-canonical"' in session
