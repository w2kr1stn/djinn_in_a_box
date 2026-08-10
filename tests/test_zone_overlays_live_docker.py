from __future__ import annotations

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
    not _docker_daemon_reachable(), reason="Docker daemon is not reachable"
)


def test_container_view_manifest_preserves_base_overlay_and_nested_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live container proves the mounted paths retain the agent's expected layout."""
    project = tmp_path / "project"
    project.mkdir()
    compose_file = project / "compose.yml"
    project_name = f"djinn-zone-test-{uuid4().hex}"
    service = f"zone-manifest-{uuid4().hex}"
    volume_names = {
        "config": f"djinn-zone-config-{uuid4().hex}",
        "shared": f"djinn-zone-shared-{uuid4().hex}",
        "local": f"djinn-zone-local-{uuid4().hex}",
    }
    created_volumes: list[str] = []
    try:
        mountpoints: dict[str, Path] = {}
        for zone, name in volume_names.items():
            created = subprocess.run(
                ["docker", "volume", "create", name],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            assert created.returncode == 0, created.stderr
            created_volumes.append(name)
            inspected = subprocess.run(
                ["docker", "volume", "inspect", name, "--format", "{{.Mountpoint}}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            assert inspected.returncode == 0, inspected.stderr
            mountpoints[zone] = Path(inspected.stdout.strip())

        helpers = {
            "config": (
                "mkdir -p /data/claude/base /data/claude/projects /data/claude/plugins/cache; "
                "printf 'base\\n' > /data/claude/base/marker; "
                "printf 'shared\\n' > /data/claude/projects/marker; "
                "printf 'local\\n' > /data/claude/plugins/cache/marker"
            ),
            "shared": "mkdir -p /data/claude/projects; "
            "printf 'shared\\n' > /data/claude/projects/marker",
            "local": "mkdir -p /data/claude/plugins/cache; "
            "printf 'local\\n' > /data/claude/plugins/cache/marker",
        }
        for zone, script in helpers.items():
            populated = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{volume_names[zone]}:/data",
                    "alpine:3.20",
                    "/bin/sh",
                    "-c",
                    script,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            assert populated.returncode == 0, populated.stderr

        config = AppConfig(
            code_dir=project,
            config_root=mountpoints["config"],
            shared_root=mountpoints["shared"],
            local_root=mountpoints["local"],
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
        migrated = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume_names['config']}:/data",
                "alpine:3.20",
                "/bin/sh",
                "-c",
                "rm -f /data/claude/projects/marker /data/claude/plugins/cache/marker",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert migrated.returncode == 0, migrated.stderr
        source_directories = {
            roots.shared_root / "claude" / "projects",
            roots.local_root / "claude" / "plugins" / "cache",
        }
        original_is_dir = Path.is_dir

        def docker_host_directory(path: Path) -> bool:
            return path in source_directories or original_is_dir(path)

        monkeypatch.setattr(Path, "is_dir", docker_host_directory)
        monkeypatch.setattr(zones_mod, "ZONES_FILE", tmp_path / "zones.toml")

        def project_root() -> Path:
            return project

        def compose_files(_mode: DockerMode) -> list[str]:
            return ["--project-name", project_name, "-f", str(compose_file)]

        monkeypatch.setattr(docker_mod, "get_project_root", project_root)
        monkeypatch.setattr(docker_mod, "get_compose_files", compose_files)
        result = compose_run(
            config,
            ContainerOptions(),
            command=command,
            interactive=False,
            shell_mount_args=[],
            audio_mount_args=[],
            dbus_mount_args=[],
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
        for name in created_volumes:
            subprocess.run(
                ["docker", "volume", "rm", name],
                capture_output=True,
                check=False,
                timeout=30,
            )

    expected = "base=base\noverlay=shared\nnested-overlay=local\n"
    assert before.stdout == expected
    assert result.success, result.stderr
    assert result.stdout == before.stdout
