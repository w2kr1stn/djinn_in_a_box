from __future__ import annotations

from pathlib import Path

from djinn_in_a_box.config.defaults import KNOWN_CONFIG_ROOT_ENTRIES
from djinn_in_a_box.core.docker import repo_owned_submount_targets

_ROOT = Path(__file__).resolve().parents[1]


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
