from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


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
    assert "/home/dev/workflow-publisher.py" in entrypoint
    assert "--canonical-root \"$CANONICAL_CONFIG_ROOT\"" in entrypoint
    assert "/home/dev/workflow-publisher.py" in session
    assert '"/home/dev/.djinn-canonical"' in session
