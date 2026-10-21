from __future__ import annotations

import re
from pathlib import Path

from djinn_in_a_box.config.defaults import KNOWN_CONFIG_ROOT_ENTRIES
from djinn_in_a_box.core.docker import repo_owned_submount_targets

_ROOT = Path(__file__).resolve().parents[1]

# Byte-for-byte copy of templates/claude/CLAUDE.md: one literal per line of that file.
_BRIDGE = (
    b"@AGENTS.md\n"
    b"\n"
    b"Djinn note: global instructions and workflow files are authored only in the selected workflow source,\n"  # noqa: E501
    b"`config/<source>/` in the Djinn installation (`djinn config show` names it). Every other copy Djinn delivers from it\n"  # noqa: E501
    b"is generated and may be read-only; an edit there is reported as drift and blocks the next delivery. Inside the Djinn\n"  # noqa: E501
    b"container with the default source `claude`, the source is mounted at `~/.claude/` (for example\n"  # noqa: E501
    b"`~/.claude/AGENTS.md`); `~/.codex/` and `~/.config/opencode/` hold generated workflow copies, and\n"  # noqa: E501
    b"`~/.djinn-canonical/` and `~/.opencode/seed/` are read-only views.\n"
)


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
    assert user_run.count("RUN ") == 1
    users_before_run = [
        line for line in instructions[: instructions.index(user_run)] if line.startswith("USER ")
    ]
    assert not users_before_run or users_before_run[-1] in ("USER root", "USER 0")
    assert 'echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/$USERNAME' in user_run
    assert (
        'echo "$USERNAME ALL=(root) NOPASSWD: /usr/local/libexec/djinn-apt-root" '
        "> /etc/sudoers.d/djinn-apt"
    ) in user_run
    assert "chmod 0440 /etc/sudoers.d/djinn-apt" in user_run
    assert "visudo -cf /etc/sudoers.d/djinn-apt" in user_run
    assert instructions.index(user_run) < instructions.index("USER $USERNAME")
    assert "install -d -o root -g root -m 0755 /var/lib/djinn-packages" in user_run


def test_declared_ownership_helper_is_packaged():
    dockerfile = (_ROOT / 'Dockerfile').read_text()
    assert (
        'COPY --chown=dev:dev scripts/ownership-repair.py /home/dev/ownership-repair.py'
        in dockerfile
    )
    assert (_ROOT / 'scripts/ownership-repair.py').is_file()


def test_credential_helper_is_packaged_at_the_entrypoint_default() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text()
    entrypoint = (_ROOT / "scripts" / "entrypoint.sh").read_text()
    assert (
        "COPY --chown=dev:dev src/djinn_in_a_box/core/opencode_credentials.py "
        "/home/dev/opencode-credentials.py"
    ) in dockerfile
    assert (
        'OPENCODE_CREDENTIALS_HELPER="${OPENCODE_CREDENTIALS_HELPER:-'
        '/home/dev/opencode-credentials.py}"'
    ) in entrypoint
    assert "opencode-credentials.sh" not in dockerfile + entrypoint


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
    assert (
        "COPY --chown=dev:dev scripts/codex-daemon-policy.py /home/dev/codex-daemon-policy.py"
        in dockerfile
    )
    assert (
        'CODEX_DAEMON_POLICY_HELPER="${CODEX_DAEMON_POLICY_HELPER:-/home/dev/codex-daemon-policy.py}"'
        in entrypoint
    )
    assert 'LABEL djinn.workflow.publisher="2"' in dockerfile
    assert "opencode-workflow" + "-delivery.py" not in dockerfile
    assert "./config:/home/dev/.djinn-canonical:ro" in compose
    assert "./config/opencode:/home/dev/.opencode/seed:ro" in compose
    assert "./config/claude/AGENTS.md:/home/dev/.claude/AGENTS.md" in compose
    assert "./templates/claude/CLAUDE.md:/home/dev/.claude/CLAUDE.md:ro" in compose
    assert (_ROOT / "templates/claude/CLAUDE.md").read_bytes() == _BRIDGE
    assert _BRIDGE.splitlines()[0] == b"@AGENTS.md"
    assert Path("/home/dev/.claude/CLAUDE.md") in repo_owned_submount_targets(
        Path("/home/dev/.claude")
    )
    assert "CLAUDE.md" in KNOWN_CONFIG_ROOT_ENTRIES["claude"]
    assert "/home/dev/workflow-publisher.py" in entrypoint
    assert "--canonical-root \"$CANONICAL_CONFIG_ROOT\"" in entrypoint
    assert "/home/dev/workflow-publisher.py" in session
    assert '"/home/dev/.djinn-canonical"' in session
