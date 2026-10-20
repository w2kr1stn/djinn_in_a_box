from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

import djinn_in_a_box.config.zones as zones_mod
import djinn_in_a_box.core.docker as docker_mod
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.docker import (
    ContainerOptions,
    DockerMode,
    compose_run,
    resolve_zone_roots,
)


def _docker_daemon_reachable() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    os.environ.get("DJINN_LIVE_DOCKER") != "1" or not _docker_daemon_reachable(),
    reason="Requires DJINN_LIVE_DOCKER=1 and a reachable Docker daemon",
)


def test_container_view_manifest_preserves_base_overlay_and_nested_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live container proves the mounted paths retain the agent's expected layout.

    The zone roots are directories the test process can inspect, as the managed-bind
    check requires; the Docker daemon must therefore share this filesystem (as in CI).
    """
    project = tmp_path / "project"
    project.mkdir()
    compose_file = project / "compose.yml"
    project_name = f"djinn-zone-test-{uuid4().hex}"
    service = f"zone-manifest-{uuid4().hex}"
    markers = {
        "config": {
            "claude/base/marker": "base",
            "claude/projects/marker": "shared",
            "claude/plugins/cache/marker": "local",
        },
        "shared": {"claude/projects/marker": "shared"},
        "local": {"claude/plugins/cache/marker": "local"},
    }
    for zone, files in markers.items():
        for relative, content in files.items():
            marker = tmp_path / zone / relative
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"{content}\n")
    try:
        config = AppConfig(
            code_dir=project,
            config_root=tmp_path / "config",
            shared_root=tmp_path / "shared",
            local_root=tmp_path / "local",
        )
        roots = resolve_zone_roots(config)
        compose_file.write_text(
            f"""services:
  {service}:
    image: alpine:3.20
    entrypoint: [\"/bin/sh\"]
    volumes:
      - {roots.config_root}/claude:/home/dev/.claude
"""
        )
        command = "".join(
            f"printf '{label}='; cat {path}; "
            for label, path in (
                ("base", "/home/dev/.claude/base/marker"),
                ("overlay", "/home/dev/.claude/projects/marker"),
                ("nested-overlay", "/home/dev/.claude/plugins/cache/marker"),
            )
        )
        before = subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                project_name,
                "-f",
                str(compose_file),
                "run",
                "--rm",
                "-T",
                service,
                "-c",
                command,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert before.returncode == 0, before.stderr
        # Only the zone overlays can supply these markers from now on.
        (roots.config_root / "claude/projects/marker").unlink()
        (roots.config_root / "claude/plugins/cache/marker").unlink()
        monkeypatch.setattr(zones_mod, "ZONES_FILE", tmp_path / "zones.toml")

        def project_root() -> Path:
            return project

        def compose_files(_mode: DockerMode, *, guest: bool = False) -> list[str]:
            assert not guest  # This fixture deliberately exercises a non-dev service.
            return ["--project-name", project_name, "-f", str(compose_file)]

        monkeypatch.setattr(docker_mod, "get_project_root", project_root)
        monkeypatch.setattr(docker_mod, "get_compose_files", compose_files)
        result = compose_run(
            config,
            ContainerOptions(),
            command=command,
            interactive=False,
            shell_mount_args=[],
            service=service,
            timeout=30,
        )
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                project_name,
                "-f",
                str(compose_file),
                "down",
                "--remove-orphans",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )

    expected = "base=base\noverlay=shared\nnested-overlay=local\n"
    assert before.stdout == expected
    assert result.success, result.stderr
    assert result.stdout == before.stdout
