"""Tests for djinn_in_a_box.core.docker module."""

import json
import os
import re
import socket
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import djinn_in_a_box.config.zones as zones_mod
import djinn_in_a_box.core.docker as docker_mod
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig, BuildConfig, ShellConfig
from djinn_in_a_box.config.zones import ZoneAssignments, ZoneName
from djinn_in_a_box.core.docker import (
    ContainerMount,
    ContainerOptions,
    DockerMode,
    MountCollisionError,
    MountSpecificationError,
    RuntimeMountSpecificationError,
    WorkflowImageCompatibility,
    backup_sync_path,
    backup_volume,
    build_compose_env,
    clear_sync_path,
    compose_build,
    compose_down,
    compose_run,
    compose_up_detached,
    delete_volumes,
    ensure_network,
    extract_sync_path_name,
    get_compose_files,
    get_config_root,
    get_existing_sync_paths_by_category,
    get_existing_volumes_by_category,
    get_running_containers,
    get_shell_mount_args,
    get_zone_overlay_mount_args,
    is_background_process_group,
    is_container_running,
    is_sync_archive,
    parse_mount_spec,
    resolve_container_mounts,
    restore_sync_path,
    restore_volume,
    validate_container_mounts,
    workflow_image_compatible,
)
from djinn_in_a_box.core.exceptions import ZoneConfigurationError


def _empty_mount_args(_config: AppConfig | None = None) -> list[str]:
    return []


def _deny_empty_placeholder_removal(
    monkeypatch: pytest.MonkeyPatch, placeholder: Path
) -> None:
    original_rmdir = docker_mod.os.rmdir

    def deny_placeholder_removal(path: str, *, dir_fd: int | None = None) -> None:
        if Path(path).name == placeholder.name:
            raise PermissionError("Docker-owned mount placeholder")
        original_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(docker_mod.os, "rmdir", deny_placeholder_removal)


def test_clear_sync_path_preserves_an_empty_nonremovable_mount_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_path = tmp_path / "claude"
    placeholder = sync_path / "plugins" / "marketplaces"
    placeholder.mkdir(parents=True)
    removable = sync_path / "credentials.json"
    removable.write_text("remove me")
    _deny_empty_placeholder_removal(monkeypatch, placeholder)

    assert clear_sync_path(sync_path) is True
    assert placeholder.is_dir()
    assert not removable.exists()


def test_restore_sync_path_preserves_an_empty_nonremovable_mount_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = tmp_path / "config"
    sync_path = config_root / "claude"
    placeholder = sync_path / "plugins" / "marketplaces"
    placeholder.mkdir(parents=True)
    stale = sync_path / "stale.json"
    stale.write_text("remove me")
    staging = tmp_path / "staging"
    staging.mkdir()
    restored = tmp_path / "restored.json"
    restored.write_text("restore me")
    archive = staging / "djinn-sync-claude.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(restored, arcname=restored.name)
    projects = tmp_path / "projects"
    projects.mkdir()
    config = AppConfig(code_dir=projects, config_root=config_root)
    _deny_empty_placeholder_removal(monkeypatch, placeholder)

    result = restore_sync_path("claude", staging, config)

    assert result.success
    assert placeholder.is_dir()
    assert not stale.exists()
    assert (sync_path / restored.name).read_text() == "restore me"


def _parse_dockerfile_symlink_line(line: str) -> list[tuple[str, str]]:
    """Return source and alias tokens from every ``ln`` symlink command on one line."""
    parsed: list[tuple[str, str]] = []
    for match in re.finditer(r"\bln\b([^;&|\n]*)", line):
        tokens = match.group(1).split()
        flags: list[str] = []
        while tokens and tokens[0].startswith("-") and tokens[0] != "\\":
            flags.append(tokens.pop(0))

        is_symlink = any(
            flag == "--symbolic"
            or flag.startswith("--symbolic-")
            or (flag.startswith("-") and not flag.startswith("--") and "s" in flag[1:])
            for flag in flags
        )
        if not is_symlink:
            continue

        path_tokens = [token for token in tokens if token != "\\"]
        if len(path_tokens) >= 2:
            parsed.append((path_tokens[0], path_tokens[1]))
    return parsed


def _assert_dockerfile_aliases_are_reserved(
    dockerfile: str, reserved: set[Path]
) -> int:
    """Check every Dockerfile symlink alias against exact/ancestor reservations."""
    matched = 0
    home = Path("/home/dev")

    for line in dockerfile.splitlines():
        for source_token, alias_token in _parse_dockerfile_symlink_line(line):
            source = (
                home / source_token[2:] if source_token.startswith("~/") else Path(source_token)
            )
            alias = home / alias_token[2:] if alias_token.startswith("~/") else Path(alias_token)
            canonical_alias = docker_mod._resolve_image_aliases(alias)
            matched += 1
            source_contains_reserved_target = any(
                source == target or target.is_relative_to(source) for target in reserved
            )
            if source_contains_reserved_target:
                assert any(
                    canonical_alias == target or canonical_alias.is_relative_to(target)
                    for target in reserved
                ), (
                    f"Dockerfile alias {alias} points to reserved source/ancestor {source} "
                    "but is not itself reserved"
                )

    assert matched, "Dockerfile alias drift guard matched no symlink lines"
    return matched


def _assert_compose_anchor_uses_short_form(lines: list[str]) -> None:
    assert not any(
        re.match(r"\s*(?:-\s+)?type\s*:", line) for line in lines
    )


class TestParseMountSpec:
    """Tests for the public ``SRC[:DST[:ro|rw]]`` mount grammar."""

    @pytest.mark.parametrize(
        ("specification", "expected"),
        [
            ("/host/src", ("/host/src", None, False)),
            ("/host/src:/container/dst", ("/host/src", Path("/container/dst"), False)),
            ("/host/src:/container/dst:ro", ("/host/src", Path("/container/dst"), True)),
            ("/host/src:/container/dst:rw", ("/host/src", Path("/container/dst"), False)),
            ("/host/src:ro", ("/host/src", None, True)),
            ("/host/src:rw", ("/host/src", None, False)),
        ],
    )
    def test_parses_each_supported_field_count(
        self, specification: str, expected: tuple[str, Path | None, bool]
    ) -> None:
        assert parse_mount_spec(specification) == expected

    @pytest.mark.parametrize(
        "specification",
        ["", "/host/src:/container/dst:ro:extra"],
    )
    def test_rejects_invalid_field_counts(self, specification: str) -> None:
        with pytest.raises(MountSpecificationError, match="mount specification"):
            parse_mount_spec(specification)

    def test_rejects_relative_target(self) -> None:
        with pytest.raises(MountSpecificationError, match="absolute container path"):
            parse_mount_spec("/host/src:relative/path")

    def test_rejects_nul_in_target(self) -> None:
        with pytest.raises(MountSpecificationError, match="NUL"):
            parse_mount_spec("/host/src:/container/\x00dst")

    def test_rejects_unknown_mode(self) -> None:
        with pytest.raises(MountSpecificationError, match="expected 'ro' or 'rw'"):
            parse_mount_spec("/host/src:/container/dst:read-only")

    def test_collapses_leading_slashes_in_absolute_target(self) -> None:
        _, target, _ = parse_mount_spec("/host/src://home/dev/.claude")

        assert target == Path("/home/dev/.claude")

    def test_normalizes_parent_segments_in_absolute_target(self) -> None:
        _, target, _ = parse_mount_spec("/host/src:/home/dev/mount/../.claude")

        assert target == Path("/home/dev/.claude")

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("/home/dev/.config/claude/skills", Path("/home/dev/.claude/skills")),
            (
                "/var/run/user/1000/pulse/native",
                Path("/run/user/1000/pulse/native"),
            ),
            ("/var/run/user/1000/bus", Path("/run/user/1000/bus")),
            ("/var/run", Path("/run")),
        ],
    )
    def test_resolves_image_alias_subtrees(self, target: str, expected: Path) -> None:
        _, resolved, _ = parse_mount_spec(f"/host/src:{target}")

        assert resolved == expected


class TestResolveContainerMounts:
    """Tests for source resolution and deterministic automatic targets."""

    def test_derives_a_nonempty_target_for_root_source(self) -> None:
        mounts = resolve_container_mounts(("/",))

        assert mounts == (
            ContainerMount(source=Path("/"), target=Path("/home/dev/mount/root")),
        )

    def test_distinguishes_duplicate_basenames_with_one_parent(self, tmp_path: Path) -> None:
        first = tmp_path / "one" / "src"
        second = tmp_path / "two" / "src"
        first.mkdir(parents=True)
        second.mkdir(parents=True)

        mounts = resolve_container_mounts((str(first), str(second)))

        assert [mount.target for mount in mounts] == [
            Path("/home/dev/mount/src"),
            Path("/home/dev/mount/two-src"),
        ]

    def test_explicit_target_is_reserved_before_derived_target(self, tmp_path: Path) -> None:
        automatic = tmp_path / "customer" / "src"
        explicit = tmp_path / "other"
        automatic.mkdir(parents=True)
        explicit.mkdir()

        mounts = resolve_container_mounts(
            (str(automatic), f"{explicit}:/home/dev/mount/src")
        )

        assert [mount.target for mount in mounts] == [
            Path("/home/dev/mount/customer-src"),
            Path("/home/dev/mount/src"),
        ]

    def test_numbers_the_third_duplicate_basename(self, tmp_path: Path) -> None:
        sources = [tmp_path / parent / "x" / "src" for parent in ("a", "b", "c")]
        for source in sources:
            source.mkdir(parents=True)

        mounts = resolve_container_mounts(tuple(str(source) for source in sources))

        assert [mount.target for mount in mounts] == [
            Path("/home/dev/mount/src"),
            Path("/home/dev/mount/x-src"),
            Path("/home/dev/mount/x-src-2"),
        ]

    def test_rejects_workspace_as_an_explicit_target(self, tmp_path: Path) -> None:
        with pytest.raises(MountSpecificationError, match="reserved for --here"):
            resolve_container_mounts((f"{tmp_path}:/home/dev/workspace",))

    @pytest.mark.parametrize("target", ["/proc", "/proc/1", "/sys", "/sys/kernel", "/dev"])
    def test_rejects_kernel_mount_targets(self, target: str) -> None:
        with pytest.raises(MountSpecificationError, match="not allowed"):
            parse_mount_spec(f"/host/source:{target}")


class TestMountTargetCollisions:
    """User mount targets may not hide a mount of this ``dev`` invocation."""

    @staticmethod
    def _without_runtime_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", _empty_mount_args)

    def test_mount_targets_from_args_accepts_long_volume_flag(self) -> None:
        assert docker_mod._mount_targets_from_args(
            ["--volume", "/host/source:/container/target:ro"]
        ) == [Path("/container/target")]

    def test_mount_targets_from_args_rejects_unknown_volume_flag(self) -> None:
        with pytest.raises(RuntimeMountSpecificationError, match="Unknown volume flag"):
            docker_mod._mount_targets_from_args(["--volumes-from", "other-container"])

    def test_mount_targets_from_args_rejects_missing_volume_specification(self) -> None:
        with pytest.raises(RuntimeMountSpecificationError, match="requires a specification"):
            docker_mod._mount_targets_from_args(["-v"])

    @pytest.mark.parametrize(
        "specification",
        ["/target", "/host:relative", "/host:/proc/../dev", "/host:/target:bad"],
    )
    def test_mount_targets_from_args_rejects_invalid_internal_specification(
        self, specification: str
    ) -> None:
        with pytest.raises(RuntimeMountSpecificationError):
            docker_mod._mount_targets_from_args(["-v", specification])

    def test_validator_normalizes_direct_container_mount_target(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)

        with pytest.raises(MountCollisionError, match=r"conflict path: /home/dev/\.claude"):
            validate_container_mounts(
                (ContainerMount(tmp_path, Path("/home/dev/tmp/../.claude")),),
                mock_app_config,
                DockerMode.NONE,
            )

    def test_validator_rejects_runtime_mount_over_compose_target(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._without_runtime_mounts(monkeypatch)

        with pytest.raises(RuntimeMountSpecificationError, match="conflicts"):
            validate_container_mounts(
                (),
                mock_app_config,
                DockerMode.NONE,
                shell_args=["-v", "/host:/home/dev/.claude"],
            )

    @pytest.mark.parametrize("specification", ["/host:relative", "/host:/proc/../dev"])
    def test_validator_rejects_invalid_runtime_mount_target(
        self,
        specification: str,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)

        with pytest.raises(RuntimeMountSpecificationError):
            validate_container_mounts(
                (),
                mock_app_config,
                DockerMode.NONE,
                shell_args=["-v", specification],
            )

    @pytest.mark.parametrize("target", ["/proc", "/sys/kernel", "/dev"])
    def test_validator_rejects_kernel_target_from_internal_mount(
        self,
        target: str,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)

        with pytest.raises(MountSpecificationError, match="not allowed"):
            validate_container_mounts(
                (ContainerMount(tmp_path, Path(target)),),
                mock_app_config,
                DockerMode.NONE,
            )

    def test_rejects_exact_compose_target(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mount = ContainerMount(tmp_path, Path("/home/dev/.claude"))

        with pytest.raises(MountCollisionError) as exc_info:
            validate_container_mounts((mount,), mock_app_config, DockerMode.NONE)

        assert str(tmp_path) in str(exc_info.value)
        assert "/home/dev/.claude" in str(exc_info.value)
        assert "conflict path: /home/dev/.claude" in str(exc_info.value)

    def test_rejects_parent_of_compose_target(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mount = ContainerMount(tmp_path, Path("/home/dev"))

        with pytest.raises(MountCollisionError, match=r"conflict path: /home/dev/\.claude"):
            validate_container_mounts((mount,), mock_app_config, DockerMode.NONE)

    def test_allows_child_of_compose_target(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mount = ContainerMount(tmp_path, Path("/home/dev/projects/scratch"))

        validate_container_mounts((mount,), mock_app_config, DockerMode.NONE)

    @pytest.mark.parametrize("workspace", ["projects", "aios"])
    @pytest.mark.parametrize("docker_mode", list(DockerMode))
    def test_workspace_mount_collisions_follow_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        workspace: str,
        docker_mode: DockerMode,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        config = AppConfig.model_validate({"code_dir": tmp_path, "workspace": workspace})
        active = Path("/home/dev") / workspace
        inactive = Path("/home/dev/aios" if workspace == "projects" else "/home/dev/projects")
        for target in (str(active), f"{active}/../{workspace}", str(active.parent)):
            mounts = resolve_container_mounts((f"{tmp_path}:{target}",))
            with pytest.raises(MountCollisionError, match="conflicts with reserved mount"):
                validate_container_mounts(mounts, config, docker_mode)

        for target in (active / "child", inactive, inactive / "child"):
            mounts = resolve_container_mounts((f"{tmp_path}:{target}",))
            validate_container_mounts(mounts, config, docker_mode)
        monkeypatch.chdir(tmp_path)
        mounts = resolve_container_mounts((str(tmp_path),), here=True)
        assert mounts[0].target == Path("/home/dev/workspace")
        assert mounts[1].target.is_relative_to(Path("/home/dev/mount"))
        validate_container_mounts(mounts, config, docker_mode)
        reserved = docker_mod._reserved_mount_targets(config, docker_mode)
        assert active in reserved
        assert inactive not in reserved

    def test_rejects_reserved_automatic_mount_root(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mount = ContainerMount(tmp_path, Path("/home/dev/mount"))

        with pytest.raises(MountCollisionError, match=r"conflict path: /home/dev/mount"):
            validate_container_mounts((mount,), mock_app_config, DockerMode.NONE)

    def test_rejects_duplicate_user_target(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        first = ContainerMount(tmp_path / "first", Path("/container/shared"))
        second = ContainerMount(tmp_path / "second", Path("/container/shared"))
        first.source.mkdir()
        second.source.mkdir()

        with pytest.raises(MountCollisionError) as exc_info:
            validate_container_mounts((first, second), mock_app_config, DockerMode.NONE)

        assert str(first.source) in str(exc_info.value)
        assert str(second.source) in str(exc_info.value)
        assert "conflict path: /container/shared" in str(exc_info.value)

    def test_rejects_active_shell_mount_target(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            docker_mod,
            "get_shell_mount_args",
            MagicMock(return_value=["-v", "/host/.zshrc:/home/dev/.zshrc.local:ro"]),
        )

        with pytest.raises(MountCollisionError, match=r"conflict path: /home/dev/\.zshrc\.local"):
            validate_container_mounts(
                (ContainerMount(tmp_path, Path("/home/dev/.zshrc.local")),),
                mock_app_config,
                DockerMode.NONE,
            )

    def test_rejects_image_claude_symlink_target(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)

        with pytest.raises(
            MountCollisionError, match=r"conflict path: /home/dev/\.claude"
        ):
            validate_container_mounts(
                (ContainerMount(tmp_path, Path("/home/dev/.config/claude")),),
                mock_app_config,
                DockerMode.NONE,
            )

    def test_rejects_image_claude_alias_subtree(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mounts = resolve_container_mounts((f"{tmp_path}:/home/dev/.config/claude/skills",))

        with pytest.raises(
            MountCollisionError, match=r"conflict path: /home/dev/\.claude/skills"
        ):
            validate_container_mounts(mounts, mock_app_config, DockerMode.NONE)

    def test_allows_unoccupied_child_of_image_claude_alias(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mounts = resolve_container_mounts((f"{tmp_path}:/home/dev/.config/claude/custom",))

        assert mounts[0].target == Path("/home/dev/.claude/custom")
        validate_container_mounts(mounts, mock_app_config, DockerMode.NONE)

    @pytest.mark.parametrize(
        "target",
        [
            "/run/djinn/audio/native",
            "/var/run/djinn/audio/native",
        ],
    )
    def test_rejects_active_audio_socket_target_and_alias(
        self,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        target: str,
    ) -> None:
        pulse_dir = tmp_path / "pulse"
        pulse_dir.mkdir()
        pulse_socket = pulse_dir / "native"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(pulse_socket))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", MagicMock(return_value=[]))

        try:
            with pytest.raises(
                MountCollisionError,
                match=re.escape(
                    "conflict path: /run/djinn/audio"
                ),
            ):
                validate_container_mounts(
                    resolve_container_mounts((f"{tmp_path}:{target}",)),
                    mock_app_config,
                    DockerMode.NONE,
                )
        finally:
            server.close()

    @pytest.mark.parametrize(
        "target",
        [
            "/run/djinn/dbus/bus",
            "/var/run/djinn/dbus/bus",
        ],
    )
    def test_rejects_active_dbus_socket_target_and_alias(
        self,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        target: str,
    ) -> None:
        bus_socket = tmp_path / "bus"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(bus_socket))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", MagicMock(return_value=[]))

        try:
            with pytest.raises(
                MountCollisionError,
                match=re.escape(
                    "conflict path: /run/djinn/dbus"
                ),
            ):
                validate_container_mounts(
                    resolve_container_mounts((f"{tmp_path}:{target}",)),
                    mock_app_config,
                    DockerMode.NONE,
                )
        finally:
            server.close()

    def test_rejects_parent_of_active_audio_socket_through_var_run_alias(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pulse_dir = tmp_path / "pulse"
        pulse_dir.mkdir()
        pulse_socket = pulse_dir / "native"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(pulse_socket))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", MagicMock(return_value=[]))

        try:
            with pytest.raises(
                MountCollisionError,
                match=r"conflict path: /run/djinn-git-agent",
            ):
                validate_container_mounts(
                    resolve_container_mounts((f"{tmp_path}:/var/run",)),
                    mock_app_config,
                    DockerMode.NONE,
                )
        finally:
            server.close()

    def test_allows_audio_target_when_audio_socket_is_absent(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", MagicMock(return_value=[]))

        validate_container_mounts(
            (ContainerMount(tmp_path, Path("/run/user/1000/pulse/native")),),
            mock_app_config,
            DockerMode.NONE,
        )

    @pytest.mark.parametrize(
        ("docker_mode", "target"),
        [
            (DockerMode.NONE, Path("/var/run/docker.sock")),
            (DockerMode.NONE, Path("/run/docker.sock")),
            (DockerMode.AGENT, Path("/var/run/docker.sock")),
            (DockerMode.AGENT, Path("/run/docker.sock")),
        ],
    )
    def test_allows_docker_socket_target_without_direct_socket(
        self,
        docker_mode: DockerMode,
        target: Path,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)

        validate_container_mounts(
            (ContainerMount(tmp_path, target),),
            mock_app_config,
            docker_mode,
        )

    def test_rejects_docker_socket_in_direct_mode(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)

        with pytest.raises(MountCollisionError, match=r"conflict path: /run/docker\.sock"):
            validate_container_mounts(
                (ContainerMount(tmp_path, Path("/var/run/docker.sock")),),
                mock_app_config,
                DockerMode.DIRECT,
            )

    def test_rejects_docker_socket_symlink_alias_in_direct_mode(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._without_runtime_mounts(monkeypatch)

        with pytest.raises(MountCollisionError, match=r"conflict path: /run/docker\.sock"):
            validate_container_mounts(
                (ContainerMount(tmp_path, Path("/run/docker.sock")),),
                mock_app_config,
                DockerMode.DIRECT,
            )

    def test_rejects_ancestor_of_direct_socket_alias(
        self,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)

        with pytest.raises(MountCollisionError, match=r"conflict path: /run/djinn-git-agent"):
            validate_container_mounts(
                (ContainerMount(tmp_path, Path("/var")),),
                mock_app_config,
                DockerMode.DIRECT,
                shell_args=[],
            )

    @pytest.mark.parametrize("workspace", ["projects", "aios"])
    def test_static_targets_match_the_dev_compose_volume_anchor(self, workspace: str) -> None:
        compose_lines = (Path(__file__).parents[1] / "docker-compose.yml").read_text().splitlines()
        anchor_start = compose_lines.index("x-common-volumes: &common-volumes")
        anchor_end = compose_lines.index("x-common-environment: &common-environment")
        dev_start = compose_lines.index("  dev:")
        networks_start = compose_lines.index("networks:")
        targets: list[Path] = []
        workspace_target = Path("/home/dev") / workspace
        for line in compose_lines[anchor_start + 1 : anchor_end]:
            if not line.strip().startswith("- "):
                continue
            line = line.replace(
                "${DJINN_WORKSPACE_TARGET:-/home/dev/projects}", str(workspace_target)
            )
            match = re.fullmatch(r"\s*-\s+.*:(/[^:\s]+)(?::(?:ro|rw))?", line)
            assert match is not None, f"Unexpected Compose mount: {line}"
            assert "$" not in match.group(1), f"Unresolved Compose target: {line}"
            targets.append(Path(match.group(1)))

        anchor_lines = compose_lines[anchor_start + 1 : anchor_end]
        _assert_compose_anchor_uses_short_form(anchor_lines)
        assert "    volumes: *common-volumes" in compose_lines[dev_start:networks_start]
        assert targets.count(workspace_target) == 1
        assert tuple(t for t in targets if t != workspace_target) == tuple(
            docker_mod._COMPOSE_DEV_MOUNT_TARGETS
        )

    def test_compose_anchor_watcher_rejects_reordered_long_form(self) -> None:
        with pytest.raises(AssertionError):
            _assert_compose_anchor_uses_short_form(
                ["    - source: /tmp", "      type: bind", "      target: /mnt"]
            )

    @pytest.mark.parametrize("flags", ["-s", "-sfn", "-snf", "-sf", "-sfT", "--symbolic"])
    def test_dockerfile_symlink_parser_accepts_all_symlink_flag_forms(
        self, flags: str
    ) -> None:
        assert _parse_dockerfile_symlink_line(f"RUN ln {flags} ~/.claude ~/.config/claude") == [
            ("~/.claude", "~/.config/claude")
        ]

    def test_dockerfile_symlink_parser_checks_every_ln_on_a_line(self) -> None:
        assert _parse_dockerfile_symlink_line(
            "RUN ln --force --symbolic ~/.claude ~/.config/claude "
            "&& ln --relative -s /home/dev/.sample-agent /home/dev/.sample-agent"
        ) == [
            ("~/.claude", "~/.config/claude"),
            ("/home/dev/.sample-agent", "/home/dev/.sample-agent"),
        ]
        assert _parse_dockerfile_symlink_line(
            "RUN ln -s ~/.sample-agent /tmp/g || ln -s ~/.claude /srv/claude"
        ) == [("~/.sample-agent", "/tmp/g"), ("~/.claude", "/srv/claude")]

    def test_dockerfile_aliases_of_reserved_paths_are_reserved(
        self, mock_app_config: AppConfig
    ) -> None:
        dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
        reserved = set(docker_mod._reserved_mount_targets(mock_app_config, DockerMode.NONE))

        assert _assert_dockerfile_aliases_are_reserved(dockerfile, reserved) == 1

    def test_dockerfile_alias_guard_rejects_alias_of_reserved_descendant(
        self, mock_app_config: AppConfig
    ) -> None:
        reserved = set(docker_mod._reserved_mount_targets(mock_app_config, DockerMode.NONE))

        with pytest.raises(AssertionError, match="source/ancestor"):
            _assert_dockerfile_aliases_are_reserved(
                "RUN ln -sfn ~/.config ~/cfg\n",
                reserved,
            )

    def test_dockerfile_alias_guard_checks_aliases_outside_home(
        self, mock_app_config: AppConfig
    ) -> None:
        reserved = set(docker_mod._reserved_mount_targets(mock_app_config, DockerMode.NONE))

        with pytest.raises(AssertionError, match="alias /srv/claude"):
            _assert_dockerfile_aliases_are_reserved(
                "RUN ln -sfn ~/.claude /srv/claude\n",
                reserved,
            )

    def test_dockerfile_alias_guard_uses_prefix_reservations(
        self, mock_app_config: AppConfig
    ) -> None:
        reserved = set(docker_mod._reserved_mount_targets(mock_app_config, DockerMode.NONE))

        assert (
            _assert_dockerfile_aliases_are_reserved(
                "RUN ln -sfn ~/.claude ~/.config/claude/plugins\n",
                reserved,
            )
            == 1
        )

    def test_dockerfile_alias_guard_rejects_empty_match(self, mock_app_config: AppConfig) -> None:
        reserved = set(docker_mod._reserved_mount_targets(mock_app_config, DockerMode.NONE))

        with pytest.raises(AssertionError, match="matched no symlink lines"):
            _assert_dockerfile_aliases_are_reserved("# no links\n", reserved)

    @pytest.mark.parametrize("workspace", ["projects", "aios"])
    def test_dev_service_keeps_its_compose_working_dir(self, workspace: str) -> None:
        """Without a mount no ``--workdir`` is passed, so this line decides where
        a plain ``djinn start`` lands. Deleting it would silently move every
        mount-less start from the selected workspace to the image default /home/dev.
        """
        compose_lines = (Path(__file__).parents[1] / "docker-compose.yml").read_text().splitlines()
        dev_start = compose_lines.index("  dev:")
        networks_start = compose_lines.index("networks:")
        dev_block = "\n".join(compose_lines[dev_start:networks_start])

        expression = "${DJINN_WORKSPACE_TARGET:-/home/dev/projects}"
        assert f"working_dir: {expression}" in dev_block
        compose = yaml.safe_load("\n".join(compose_lines))
        dev = compose["services"]["dev"]
        workspace_binds = [v for v in dev["volumes"] if v.startswith("${CODE_DIR:")]
        assert len(workspace_binds) == 1
        target = workspace_binds[0].split("}:", 1)[1]
        assert target == dev["working_dir"] == expression
        assert target.replace(expression, f"/home/dev/{workspace}") == f"/home/dev/{workspace}"


class TestEnsureNetwork:
    """Tests for ensure_network function."""

    @patch("djinn_in_a_box.core.docker._docker_inspect")
    def test_network_already_exists(self, mock_inspect: MagicMock) -> None:
        """Test returns True when network already exists."""
        mock_inspect.return_value = True
        result = ensure_network()
        assert result is True

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    @patch("djinn_in_a_box.core.docker._docker_inspect")
    def test_creates_network(self, mock_inspect: MagicMock, mock_run: MagicMock) -> None:
        """Test creates network and returns True."""
        mock_inspect.return_value = False
        mock_run.return_value = MagicMock(returncode=0)
        result = ensure_network()
        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert docker_mod.DOCKER_EXECUTABLE in call_args
        assert "network" in call_args
        assert "create" in call_args

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    @patch("djinn_in_a_box.core.docker._docker_inspect")
    def test_create_network_fails(self, mock_inspect: MagicMock, mock_run: MagicMock) -> None:
        """Test returns False when network creation fails."""
        mock_inspect.return_value = False
        mock_run.return_value = MagicMock(returncode=1)
        result = ensure_network()
        assert result is False


class TestWorkflowImageCompatibility:
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_accepts_publisher_label(self, run: MagicMock) -> None:
        run.return_value = MagicMock(returncode=0, stdout="1\n")

        assert workflow_image_compatible() is WorkflowImageCompatibility.COMPATIBLE
        assert run.call_args.args[0] == [
            docker_mod.DOCKER_EXECUTABLE,
            "image",
            "inspect",
            "djinn-in-a-box:latest",
            "--format",
            '{{ index .Config.Labels "djinn.workflow.publisher" }}',
        ]

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_rejects_unlabelled_image_content_free(self, run: MagicMock) -> None:
        run.return_value = MagicMock(returncode=0, stdout="")

        assert workflow_image_compatible() is WorkflowImageCompatibility.INCOMPATIBLE

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_missing_image_is_distinguished_from_an_unreachable_daemon(
        self, run: MagicMock
    ) -> None:
        run.side_effect = (
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=0, stdout=""),
        )

        assert workflow_image_compatible() is WorkflowImageCompatibility.MISSING
        assert run.call_args_list[1].args[0] == [docker_mod.DOCKER_EXECUTABLE, "info"]

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_inspect_failure_is_unknown_when_daemon_is_unreachable(
        self, run: MagicMock
    ) -> None:
        run.side_effect = (
            MagicMock(returncode=1, stdout=""),
            MagicMock(returncode=1, stdout=""),
        )

        assert workflow_image_compatible() is WorkflowImageCompatibility.UNKNOWN

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_inspect_timeout_is_unknown_and_bounded(self, run: MagicMock) -> None:
        run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)

        assert workflow_image_compatible() is WorkflowImageCompatibility.UNKNOWN
        assert run.call_args.kwargs["timeout"] == 10.0


class TestGetComposeFiles:
    """Tests for get_compose_files function."""

    @patch("djinn_in_a_box.core.docker.get_project_root")
    def test_without_docker(self, mock_root: MagicMock) -> None:
        """Test returns only base compose file when docker_mode=NONE."""
        mock_root.return_value = Path("/project")
        files = get_compose_files(DockerMode.NONE)
        assert len(files) == 6
        assert files[:2] == ["-p", "djinn-in-a-box"]
        assert files[2] == "-f"
        assert "docker-compose.yml" in files[3]
        assert "docker-compose.agent-docker.yml" not in str(files)

    @patch("djinn_in_a_box.core.docker.get_project_root")
    def test_with_docker(self, mock_root: MagicMock) -> None:
        """Test returns both compose files when docker_mode=PROXY."""
        mock_root.return_value = Path("/project")
        files = get_compose_files(DockerMode.AGENT)
        assert len(files) == 8
        assert files[:2] == ["-p", "djinn-in-a-box"]
        assert files.count("-f") == 3
        # Check both files are present
        file_paths = [f for f in files if f != "-f"]
        assert any("docker-compose.yml" in f for f in file_paths)
        assert any("docker-compose.agent-docker.yml" in f for f in file_paths)

    @patch("djinn_in_a_box.core.docker.get_project_root")
    def test_with_docker_direct(self, mock_root: MagicMock) -> None:
        """Test returns docker-direct compose file when docker_mode=DIRECT."""
        mock_root.return_value = Path("/project")
        files = get_compose_files(DockerMode.DIRECT)
        assert len(files) == 8
        assert files[:2] == ["-p", "djinn-in-a-box"]
        file_paths = [f for f in files if f != "-f"]
        assert any("docker-compose.yml" in f for f in file_paths)
        assert any("docker-compose.docker-direct.yml" in f for f in file_paths)

    @patch("djinn_in_a_box.core.docker.get_project_root")
    def test_explicit_project_keeps_identity_substitution(
        self, mock_root: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_root.return_value = Path("/project")
        monkeypatch.setattr(docker_mod, "COMPOSE_PROJECT", "djinn-test")

        files = get_compose_files()

        assert files[:2] == ["-p", "djinn-test"]
        compose = yaml.safe_load(
            (Path(__file__).parents[1] / "docker-compose.yml").read_text()
        )
        assert compose["name"] == "djinn-in-a-box"


class TestBuildComposeEnv:
    """Tests for docker compose host interpolation environment."""

    @pytest.mark.parametrize("workspace", ["projects", "aios", None])
    def test_workspace_env_selects_compose_mount_and_cwd(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        workspace: str | None,
    ) -> None:
        config = (
            AppConfig.model_validate({"code_dir": tmp_path, "workspace": workspace})
            if workspace is not None
            else None
        )
        monkeypatch.setenv("CODE_DIR", "/stale/source")
        monkeypatch.setenv("DJINN_WORKSPACE_TARGET", "/stale/target")
        env = build_compose_env(config)
        host_env = docker_mod._compose_host_env(config)
        expected_target = f"/home/dev/{workspace or 'projects'}"
        expected_source = str(tmp_path if config is not None else Path.home())
        assert env["CODE_DIR"] == host_env["CODE_DIR"] == expected_source
        assert (
            env["DJINN_WORKSPACE_TARGET"] == host_env["DJINN_WORKSPACE_TARGET"] == expected_target
        )
        compose = yaml.safe_load((Path(__file__).parents[1] / "docker-compose.yml").read_text())
        dev = compose["services"]["dev"]
        workspace_binds = [v for v in dev["volumes"] if v.startswith("${CODE_DIR:")]
        assert len(workspace_binds) == 1
        expression = "${DJINN_WORKSPACE_TARGET:-/home/dev/projects}"
        assert workspace_binds[0].endswith(f":{expression}")
        assert dev["working_dir"] == expression
        assert workspace_binds[0].replace(
            "${CODE_DIR:?Run 'djinn init' to set CODE_DIR}", host_env["CODE_DIR"]
        ).replace(expression, host_env["DJINN_WORKSPACE_TARGET"]) == (
            f"{expected_source}:{expected_target}"
        )
        assert (
            dev["working_dir"].replace(expression, host_env["DJINN_WORKSPACE_TARGET"])
            == expected_target
        )

    def test_sets_terminal_width_when_output_is_tty(self, tmp_path: Path) -> None:
        config = AppConfig(code_dir=tmp_path)
        stdout = MagicMock()
        stdout.isatty.return_value = True
        stderr = MagicMock()
        stderr.isatty.return_value = False

        with (
            patch("djinn_in_a_box.core.docker.sys.stdout", stdout),
            patch("djinn_in_a_box.core.docker.sys.stderr", stderr),
            patch(
                "djinn_in_a_box.core.docker.shutil.get_terminal_size",
                return_value=os.terminal_size((123, 40)),
            ),
        ):
            env = build_compose_env(config)

        assert env["DJINN_TERM_WIDTH"] == "123"

    def test_does_not_set_terminal_width_without_tty(self, tmp_path: Path) -> None:
        config = AppConfig(code_dir=tmp_path)
        stdout = MagicMock()
        stdout.isatty.return_value = False
        stderr = MagicMock()
        stderr.isatty.return_value = False

        with (
            patch("djinn_in_a_box.core.docker.sys.stdout", stdout),
            patch("djinn_in_a_box.core.docker.sys.stderr", stderr),
            patch(
                "djinn_in_a_box.core.docker.shutil.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ) as get_terminal_size,
        ):
            env = build_compose_env(config)

        assert "DJINN_TERM_WIDTH" not in env
        get_terminal_size.assert_not_called()


class TestGetShellMountArgs:
    """Tests for get_shell_mount_args function."""

    def test_skip_mounts_true(self, tmp_path: Path) -> None:
        """Test returns empty list when skip_mounts=True."""
        config = AppConfig(
            code_dir=tmp_path,
            shell=ShellConfig(skip_mounts=True),
        )
        args = get_shell_mount_args(config)
        assert args == []

    def test_no_files_exist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test returns empty list when no shell files exist."""
        # Use a fake home that has no shell files
        fake_home = tmp_path / "empty_home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        config = AppConfig(code_dir=tmp_path)
        args = get_shell_mount_args(config)
        assert args == []

    def test_zshrc_mount(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test mounts .zshrc when it exists."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        zshrc = fake_home / ".zshrc"
        zshrc.write_text("# zshrc")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        config = AppConfig(code_dir=tmp_path)
        args = get_shell_mount_args(config)
        assert "-v" in args
        assert any(".zshrc:/home/dev/.zshrc.local:ro" in arg for arg in args)

    def test_missing_configured_omp_theme_warns_and_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicitly configured but missing theme must warn, not vanish silently."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        config = AppConfig(
            code_dir=tmp_path,
            shell=ShellConfig(omp_theme_path=tmp_path / "missing-theme.json"),
        )
        with patch("djinn_in_a_box.core.docker.warning") as mock_warn:
            args = get_shell_mount_args(config)
        assert not any("omp" in a for a in args)
        mock_warn.assert_called_once()
        assert "missing-theme.json" in mock_warn.call_args[0][0]

    def test_custom_omp_theme_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test mounts custom OMP theme when specified and exists."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        theme_file = fake_home / "custom-theme.json"
        theme_file.write_text("{}")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        config = AppConfig(
            code_dir=tmp_path,
            shell=ShellConfig(omp_theme_path=theme_file),
        )
        args = get_shell_mount_args(config)
        assert "-v" in args
        assert any(".zsh-theme.omp.json:ro" in arg for arg in args)


class TestIsContainerRunning:
    """Tests for is_container_running function."""

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_container_running(self, mock_run: MagicMock) -> None:
        """Test returns True when container is running."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="djinn-agent-docker\n",
        )
        assert is_container_running("djinn-agent-docker") is True

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_container_not_running(self, mock_run: MagicMock) -> None:
        """Test returns False when container is not running."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
        )
        assert is_container_running("djinn-agent-docker") is False

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_partial_match_rejected(self, mock_run: MagicMock) -> None:
        """Test partial name matches are rejected."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="djinn-agent-docker-2\n",
        )
        assert is_container_running("djinn-agent-docker") is False


class TestGetRunningContainers:
    """Tests for get_running_containers function."""

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_returns_container_list(self, mock_run: MagicMock) -> None:
        """Test returns list of running containers."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="djinn\ndjinn-agent-docker\n",
        )
        containers = get_running_containers()
        assert containers is not None
        assert "djinn" in containers
        assert "djinn-agent-docker" in containers

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_returns_unknown_on_error(self, mock_run: MagicMock) -> None:
        """A failed probe must stay distinct from an empty container list."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        containers = get_running_containers()
        assert containers is None


class TestDeleteVolumes:
    """Tests for delete_volumes function."""

    @patch("djinn_in_a_box.core.docker.delete_volume")
    def test_deletes_multiple_volumes(self, mock_delete: MagicMock) -> None:
        """Test deletes multiple volumes and returns status dict."""
        mock_delete.side_effect = [True, False, True]
        volumes = ["vol1", "vol2", "vol3"]
        results = delete_volumes(volumes)
        assert results == {"vol1": True, "vol2": False, "vol3": True}
        assert mock_delete.call_count == 3


class TestComposeBuild:
    """Tests for compose_build function."""

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_build_success(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Test returns successful RunResult."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Building...",
            stderr="",
        )
        result = compose_build()
        assert result.success is True
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == [docker_mod.DOCKER_EXECUTABLE, "buildx", "bake"]

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_build_no_cache(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Test includes --no-cache flag when requested."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_build(no_cache=True)
        cmd = mock_run.call_args[0][0]
        assert "--no-cache" in cmd


class TestStreamedBuildPath:
    """`djinn build` streams: the log must appear while the build runs.

    A captured build says nothing until the process exits — which is precisely the
    moment a stalled build has nothing left to tell you. These pin the streaming
    contract and the result contract that comes with it.
    """

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_build_inherits_stdio_and_returns_no_output(
        self, mock_run: MagicMock, _root: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        result = compose_build()
        kwargs = mock_run.call_args.kwargs
        # Nothing may be piped: any redirection puts the log back into a buffer.
        assert "capture_output" not in kwargs
        assert "stdout" not in kwargs
        assert "stderr" not in kwargs
        # An inherited terminal stdin lets a prompt masquerade as a hang.
        assert kwargs["stdin"] == subprocess.DEVNULL
        # The log already went to the terminal, so the result carries none of it.
        assert result.stdout == ""
        assert result.stderr == ""

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_missing_docker_binary_keeps_exit_code_127(
        self, mock_run: MagicMock, _root: MagicMock
    ) -> None:
        """Streaming must not cost the spawn-failure contract of the captured path.

        Here `stderr` is load-bearing rather than empty: no process ever ran, so the
        terminal holds nothing and this is the only place the diagnosis exists.
        """
        mock_run.side_effect = FileNotFoundError("docker")
        result = compose_build()
        assert result.returncode == 127
        assert "Command not found" in result.stderr

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_unexecutable_docker_binary_keeps_exit_code_126(
        self, mock_run: MagicMock, _root: MagicMock
    ) -> None:
        mock_run.side_effect = PermissionError("docker")
        result = compose_build()
        assert result.returncode == 126
        assert "Permission denied" in result.stderr

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_bake_reads_the_compose_file_and_loads_the_image(
        self, mock_run: MagicMock, _root: MagicMock
    ) -> None:
        """The compose file stays the one build definition, named explicitly.

        Bake's own file discovery would also pick up bake files and overrides lying
        in the project root, which no other djinn call honors. `--load` is what
        compose asked for implicitly: without it a `docker-container` builder keeps
        the image in its cache and the local store never sees it.

        Naming the file does not anchor the build, though: bake resolves `context`
        against its working directory. Run elsewhere, it would build that
        directory's Dockerfile under djinn's tag.
        """
        mock_run.return_value = MagicMock(returncode=0)
        compose_build()
        argv = mock_run.call_args.args[0]
        assert argv == [
            docker_mod.DOCKER_EXECUTABLE, "buildx", "bake", "-f", "/project/docker-compose.yml",
            "-f", "/project/docker-compose.desktop.yml",
            "--progress", "plain", "--load", "dev", "dbus-helper",
        ]
        assert mock_run.call_args.kwargs["cwd"] == Path("/project")

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_streamed_build_still_bridges_the_host_env(
        self,
        mock_run: MagicMock,
        _root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The env bridge must survive the change of execution helper."""
        monkeypatch.setenv("CODE_DIR", "/stale/from/shell")
        mock_run.return_value = MagicMock(returncode=0)
        compose_build(mock_app_config)
        env = mock_run.call_args.kwargs["env"]
        assert env["CODE_DIR"] == str(mock_app_config.code_dir)

    @patch("djinn_in_a_box.core.docker.is_own_container", return_value=False)
    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_only_the_build_streams(
        self,
        mock_run: MagicMock,
        _root: MagicMock,
        _own: MagicMock,
        mock_app_config: AppConfig,
    ) -> None:
        """Only the build streams, and it is not a compose call.

        Every compose subcommand has callers that read its output, so compose
        calls must stay captured even though the build next to them streams.
        """
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_down(mock_app_config)
        assert mock_run.call_args.kwargs["capture_output"] is True


class TestBuildProgressOverride:
    """`plain` is the default, but it must not be a cage."""

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_override_is_honored(
        self, mock_run: MagicMock, _root: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DJINN_BUILD_PROGRESS", "tty")
        mock_run.return_value = MagicMock(returncode=0)
        compose_build()
        assert _progress_of(mock_run.call_args.args[0]) == "tty"

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_unusable_value_falls_back_instead_of_failing_the_build(
        self, mock_run: MagicMock, _root: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typo must not cost the whole build: buildx would reject the flag."""
        monkeypatch.setenv("DJINN_BUILD_PROGRESS", "plian")
        mock_run.return_value = MagicMock(returncode=0)
        with patch("djinn_in_a_box.core.docker.warning") as mock_warning:
            compose_build()
        assert _progress_of(mock_run.call_args.args[0]) == "plain"
        # A silent fallback would leave the user believing the override took effect.
        mock_warning.assert_called_once()
        assert "plian" in mock_warning.call_args.args[0]

    @pytest.mark.parametrize(
        ("requested", "passed"), [("rawjson", "rawjson"), ("json", "plain"), ("none", "plain")]
    )
    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_modes_are_the_ones_bake_accepts(
        self,
        mock_run: MagicMock,
        _root: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        requested: str,
        passed: str,
    ) -> None:
        """`json` was compose's renderer; bake calls it `rawjson` and rejects `json`.

        `none` appears in bake's help text, but only `buildx build` maps it to
        `quiet` — bake fails with `invalid progress mode none`.
        """
        monkeypatch.setenv("DJINN_BUILD_PROGRESS", requested)
        mock_run.return_value = MagicMock(returncode=0)
        with patch("djinn_in_a_box.core.docker.warning"):
            compose_build()
        assert _progress_of(mock_run.call_args.args[0]) == passed


def _progress_of(argv: list[str]) -> str:
    return argv[argv.index("--progress") + 1]


def _allow_grants(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--allow"]


class TestBuildNetworkGrant:
    """A `host` build network needs buildx's consent, and gets exactly that much.

    Since buildx 0.37.2 bake rejects an ungranted `network.host` entitlement, and
    `docker compose build` has no way to grant it — which is why the build calls
    bake directly. The grant is consent to share the host's network namespace, so
    it must follow the requested network exactly: missing, the build fails before
    its first step; given on every build, it would sign away a protection nobody
    asked to lose.
    """

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_host_network_grants_network_host_only(
        self, mock_run: MagicMock, _root: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        compose_build(AppConfig(code_dir=tmp_path, build=BuildConfig(network="host")))
        assert _allow_grants(mock_run.call_args.args[0]) == ["network.host"]

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_default_network_grants_nothing(
        self, mock_run: MagicMock, _root: MagicMock, mock_app_config: AppConfig
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        compose_build(mock_app_config)
        assert _allow_grants(mock_run.call_args.args[0]) == []

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_grant_follows_the_network_the_compose_file_receives(
        self,
        mock_run: MagicMock,
        _root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Request and grant come from one value, so they cannot disagree.

        A stale shell export loses to the configured network on both sides; without
        a config the exported value is what compose interpolates, so it is also
        what gets granted.
        """
        monkeypatch.setenv("DJINN_BUILD_NETWORK", "host")
        mock_run.return_value = MagicMock(returncode=0)

        compose_build(mock_app_config)
        assert mock_run.call_args.kwargs["env"]["DJINN_BUILD_NETWORK"] == "default"
        assert _allow_grants(mock_run.call_args.args[0]) == []

        compose_build(None)
        assert mock_run.call_args.kwargs["env"]["DJINN_BUILD_NETWORK"] == "host"
        assert _allow_grants(mock_run.call_args.args[0]) == ["network.host"]


class TestDockerfileDnsGuard:
    """The build must refuse a network it cannot resolve names on.

    Without this the agent installs discover the failure one timeout at a time —
    measured once at 70 minutes before npm gave up.
    """

    @staticmethod
    def _dockerfile() -> str:
        return (Path(__file__).parents[1] / "Dockerfile").read_text()

    @staticmethod
    def _run_instructions() -> list[str]:
        """The Dockerfile's RUN instructions, each with its continuations joined.

        Layer boundaries are what matter here, so line continuations must be folded
        back into one string — a guard on the next physical line is in the same
        instruction, a guard in the next RUN is not.
        """
        text = (Path(__file__).parents[1] / "Dockerfile").read_text()
        joined = text.replace("\\\n", " ")
        # Indented or lower-case RUN is still a RUN; matching the literal prefix only
        # would let a renamed-but-real layer slip past this whole check.
        return [line for line in joined.splitlines() if re.match(r"^\s*RUN\s+", line, re.I)]

    def test_guard_shares_the_run_of_every_layer_it_protects(self) -> None:
        """The guard must sit *inside* the download's instruction, not before it.

        A guard in its own layer can stay cached while the download below re-runs —
        only sharing a RUN shares the cache decision. `npm install -g` is the layer
        that matters most: a `djinn update` bump invalidates it while everything
        above stays cached, and its failure mode cost 70 minutes.
        """
        instructions = self._run_instructions()
        assert instructions, "no RUN instructions parsed — has the Dockerfile moved?"
        for needle in ("apt-get update", "npm install -g"):
            owners = [i for i in instructions if needle in i]
            assert owners, f"{needle} vanished from the Dockerfile"
            for owner in owners:
                assert "check-build-dns.sh" in owner, (
                    f"the RUN containing {needle!r} does not call the DNS guard, "
                    "so it can be cached away from it"
                )

    def test_guard_runs_before_the_download_it_shares_a_layer_with(self) -> None:
        # `guard && download` fails first; `download && guard` would be decoration.
        for instruction in self._run_instructions():
            if "check-build-dns.sh" not in instruction:
                continue
            for needle in ("apt-get update", "npm install -g"):
                if needle in instruction:
                    assert instruction.index("check-build-dns.sh") < instruction.index(needle)

    def test_guard_names_the_escape_hatch_and_its_cost(self) -> None:
        # A message that does not say what to do costs another debugging session —
        # and one that hides the trade-off invites an uninformed `host`.
        script = (Path(__file__).parents[1] / "scripts" / "check-build-dns.sh").read_text()
        assert "djinn config set build.network host" in script
        assert "host's network namespace" in script


class TestComposeRun:
    """Tests for compose_run function."""

    @staticmethod
    def _without_runtime_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", _empty_mount_args)

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_serializes_all_mounts_and_uses_the_first_target_as_workdir(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        options = ContainerOptions(
            mounts=(
                ContainerMount(Path("/host/readonly"), Path("/work/one"), read_only=True),
                ContainerMount(Path("/host/readwrite"), Path("/work/two")),
            )
        )

        compose_run(mock_app_config, options, command="echo", interactive=False)

        cmd = mock_run.call_args.args[0]
        assert cmd[cmd.index("-v") : cmd.index("-v") + 2] == [
            "-v",
            "/host/readonly:/work/one:ro",
        ]
        second_mount = cmd.index("-v", cmd.index("-v") + 1)
        assert cmd[second_mount : second_mount + 2] == ["-v", "/host/readwrite:/work/two"]
        assert cmd[cmd.index("--workdir") + 1] == "/work/one"

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_emits_empty_zone_sources_without_using_them_as_workdir(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty assigned source mounts without changing workdir."""
        self._without_runtime_mounts(monkeypatch)
        zones_file = mock_app_config.config_root.parent / "zones.toml"
        monkeypatch.setattr(zones_mod, "ZONES_FILE", zones_file)
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        empty_source = Path(f"{mock_app_config.config_root}.shared") / "claude" / "projects"
        empty_source.mkdir(parents=True)

        overlay_args = get_zone_overlay_mount_args(mock_app_config)
        compose_run(mock_app_config, ContainerOptions(), command="echo", interactive=False)

        expected = f"{empty_source}:/home/dev/.claude/projects"
        assert expected in overlay_args
        cmd = mock_run.call_args.args[0]
        assert expected in cmd
        assert "--workdir" not in cmd

    def test_skips_zone_overlay_when_its_source_is_missing(
        self, mock_app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(zones_mod, "ZONES_FILE", tmp_path / "zones.toml")

        assert get_zone_overlay_mount_args(mock_app_config) == []

    @pytest.mark.parametrize("source_kind", ("regular", "symlink"))
    def test_rejects_non_directory_zone_source_after_assignment_resolution(
        self,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        source_kind: str,
    ) -> None:
        source = Path(f"{mock_app_config.config_root}.local") / "claude" / "jobs"
        source.parent.mkdir(parents=True)
        if source_kind == "regular":
            source.write_text("not a directory")
        else:
            outside = source.parent / "outside"
            outside.mkdir()
            source.symlink_to(outside, target_is_directory=True)
        by_agent: dict[str, dict[ZoneName, tuple[Path, ...]]] = {
            agent: {"local": (), "shared": ()} for agent in zones_mod.ZONE_CONTAINER_TARGETS
        }
        by_agent["claude"]["local"] = (Path("jobs"),)
        assignments = ZoneAssignments(by_agent, ())

        def load_assignments(_config: AppConfig | None = None) -> ZoneAssignments:
            return assignments

        monkeypatch.setattr(zones_mod, "load_zone_assignments", load_assignments)

        with pytest.raises(ZoneConfigurationError, match="not a directory"):
            get_zone_overlay_mount_args(mock_app_config)

    def test_rejects_mount_at_a_reserved_zone_overlay_target(
        self,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        zones_file = tmp_path / "zones.toml"
        monkeypatch.setattr(zones_mod, "ZONES_FILE", zones_file)

        with pytest.raises(
            MountCollisionError, match=r"reserved mount at /home/dev/\.claude/projects"
        ):
            validate_container_mounts(
                (ContainerMount(tmp_path, Path("/home/dev/.claude/projects")),),
                mock_app_config,
                DockerMode.NONE,
                shell_args=[],
            )

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_emits_canonical_alias_target(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        options = ContainerOptions(
            mounts=(ContainerMount(tmp_path, Path("/home/dev/.config/claude/custom")),)
        )

        compose_run(mock_app_config, options, command="echo", interactive=False)

        cmd = mock_run.call_args.args[0]
        assert f"{tmp_path}:/home/dev/.claude/custom" in cmd
        assert cmd[cmd.index("--workdir") + 1] == "/home/dev/.claude/custom"

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_emits_normalized_direct_mount_target(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        options = ContainerOptions(
            mounts=(ContainerMount(tmp_path, Path("/home/dev/tmp/../work")),)
        )

        compose_run(mock_app_config, options, command="echo", interactive=False)

        cmd = mock_run.call_args.args[0]
        assert f"{tmp_path}:/home/dev/work" in cmd
        assert cmd[cmd.index("--workdir") + 1] == "/home/dev/work"

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_reuses_dynamic_mount_args_for_validation_and_command(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        shell_args = ["-v", "/host/.zshrc:/home/dev/.zshrc.local:ro"]
        shell = MagicMock(return_value=shell_args)
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", shell)

        compose_run(mock_app_config, ContainerOptions(), command="echo", interactive=False)

        shell.assert_called_once_with(mock_app_config)
        assert shell_args[1] in mock_run.call_args.args[0]

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_emits_canonical_runtime_mount_targets(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
    ) -> None:
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        shell_args = ["-v", "/host:/home/dev/tmp/../runtime:ro"]

        compose_run(
            mock_app_config,
            ContainerOptions(),
            command="echo",
            interactive=False,
            shell_mount_args=shell_args,
        )

        cmd = mock_run.call_args.args[0]
        assert "/host:/home/dev/runtime:ro" in cmd
        assert "/host:/home/dev/tmp/../runtime:ro" not in cmd

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_omits_workdir_without_mounts(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        compose_run(mock_app_config, ContainerOptions(), command="echo", interactive=False)

        assert "--workdir" not in mock_run.call_args.args[0]

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_rejects_mount_collisions_before_starting_compose(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        options = ContainerOptions(
            mounts=(ContainerMount(Path("/host/source"), Path("/home/dev/.claude")),)
        )

        with pytest.raises(MountCollisionError):
            compose_run(mock_app_config, options, command="echo", interactive=False)

        mock_run.assert_not_called()

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_rejects_normalized_alias_of_compose_target_before_starting_compose(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        source = tmp_path / "source"
        source.mkdir()
        mounts = resolve_container_mounts((f"{source}:/home/dev/mount/../.claude",))

        with pytest.raises(MountCollisionError, match=r"conflict path: /home/dev/\.claude"):
            compose_run(
                mock_app_config,
                ContainerOptions(mounts=mounts),
                command="echo",
                interactive=False,
            )

        mock_run.assert_not_called()

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_run_headless_with_timeout(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
    ) -> None:
        """Test headless run passes timeout to subprocess."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="output", stderr="")

        options = ContainerOptions()
        result = compose_run(
            mock_app_config,
            options,
            command="echo test",
            interactive=False,
            timeout=300,
        )

        assert result.success is True
        assert result.stdout == "output"
        # Verify timeout was passed
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 300

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_run_handles_timeout_expiration(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
    ) -> None:
        """Test returns exit code 124 when timeout expires."""
        import subprocess

        mock_root.return_value = Path("/project")
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=10)

        options = ContainerOptions()
        result = compose_run(
            mock_app_config,
            options,
            command="long_running_command",
            interactive=False,
            timeout=10,
        )

        # Return code 124 is conventional for timeout (like GNU timeout)
        assert result.returncode == 124
        assert "Timeout" in result.stderr

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_run_headless_mode(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
    ) -> None:
        """Test headless mode adds -T flag and captures output."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="captured", stderr="")

        options = ContainerOptions()
        result = compose_run(
            mock_app_config,
            options,
            command="echo hello",
            interactive=False,
        )

        cmd = mock_run.call_args[0][0]
        assert "-T" in cmd
        assert result.stdout == "captured"




class TestComposeRunErrorHandling:
    """Tests for subprocess error handling in compose_run."""

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_handles_docker_not_found(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
    ) -> None:
        """Test graceful handling when docker command is not found."""
        mock_root.return_value = Path("/project")
        mock_run.side_effect = FileNotFoundError("[Errno 2] No such file or directory: 'docker'")

        options = ContainerOptions()
        result = compose_run(mock_app_config, options, command="test", interactive=False)

        # Should return error result, not crash
        assert result.returncode == 127  # Command not found convention
        assert "docker" in result.stderr.lower() or "not found" in result.stderr.lower()

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_handles_permission_error(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
    ) -> None:
        """Test graceful handling when docker socket is inaccessible."""
        mock_root.return_value = Path("/project")
        mock_run.side_effect = PermissionError("Permission denied: '/var/run/docker.sock'")

        options = ContainerOptions()
        result = compose_run(mock_app_config, options, command="test", interactive=False)

        assert result.returncode == 126  # Permission denied convention
        assert "permission" in result.stderr.lower()


class TestGetExistingVolumesByCategory:
    """Tests for get_existing_volumes_by_category."""

    def test_returns_existing_volumes(self) -> None:
        def _exists(name: str) -> bool:
            return name == "djinn-uv-cache"

        cat_patch = patch.dict(
            "djinn_in_a_box.core.docker.VOLUME_CATEGORIES",
            {"cache": ["djinn-uv-cache", "djinn-tools-cache"]},
            clear=True,
        )
        with (
            cat_patch,
            patch("djinn_in_a_box.core.docker.volume_exists") as mock_exists,
        ):
            mock_exists.side_effect = _exists
            result = get_existing_volumes_by_category("cache")
            assert result == ["djinn-uv-cache"]

    def test_returns_empty_for_unknown_category(self) -> None:
        result = get_existing_volumes_by_category("nonexistent")
        assert result == []

    def test_returns_empty_when_no_volumes_exist(self) -> None:
        cat_patch = patch.dict(
            "djinn_in_a_box.core.docker.VOLUME_CATEGORIES",
            {"cache": ["djinn-uv-cache"]},
            clear=True,
        )
        with (
            cat_patch,
            patch("djinn_in_a_box.core.docker.volume_exists", return_value=False),
        ):
            result = get_existing_volumes_by_category("cache")
            assert result == []


class TestGetConfigRoot:
    """Tests for get_config_root."""

    def test_uses_env_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DJINN_CONFIG_ROOT", "/custom/config")
        assert get_config_root() == Path("/custom/config")

    def test_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        assert get_config_root() == Path.home() / ".djinn" / "config"

    def test_expands_user_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DJINN_CONFIG_ROOT", "~/myconfig")
        assert get_config_root() == Path.home() / "myconfig"


class TestGetExistingSyncPathsByCategory:
    """Tests for get_existing_sync_paths_by_category."""

    def test_returns_only_existing_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(tmp_path))
        (tmp_path / "claude").mkdir()
        # "unused-agent" dir intentionally missing

        cat_patch = patch.dict(
            "djinn_in_a_box.core.docker.SYNC_PATHS",
            {"credentials": ["claude", "unused-agent"]},
            clear=True,
        )
        with cat_patch:
            result = get_existing_sync_paths_by_category("credentials")

        assert result == [tmp_path / "claude"]

    def test_uses_config_root_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        code_dir = tmp_path / "projects"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        (configured_root / "claude").mkdir(parents=True)
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)
        config = load_config(config_file)

        cat_patch = patch.dict(
            "djinn_in_a_box.core.docker.SYNC_PATHS",
            {"credentials": ["claude"]},
            clear=True,
        )
        with cat_patch:
            result = get_existing_sync_paths_by_category("credentials", config)

        assert result == [configured_root / "claude"]

    def test_env_root_wins_over_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code_dir = tmp_path / "projects"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        env_root = tmp_path / "env-root"
        (configured_root / "unused-agent").mkdir(parents=True)
        (env_root / "claude").mkdir(parents=True)
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(env_root))
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)
        config = load_config(config_file)

        cat_patch = patch.dict(
            "djinn_in_a_box.core.docker.SYNC_PATHS",
            {"credentials": ["claude", "unused-agent"]},
            clear=True,
        )
        with cat_patch:
            result = get_existing_sync_paths_by_category("credentials", config)

        assert result == [env_root / "claude"]

    def test_returns_empty_for_unknown_category(self) -> None:
        assert get_existing_sync_paths_by_category("nonexistent") == []


class TestBackupSyncPath:
    """Tests for backup_sync_path."""

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_command_structure(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        source = tmp_path / "claude"
        source.mkdir()
        dest = tmp_path / "staging"
        dest.mkdir()

        result = backup_sync_path(source, dest)

        assert result.success
        args = mock_run.call_args[0][0]
        assert args[:2] == ["tar", "czf"]
        assert str(dest / "djinn-sync-claude.tar.gz") in args
        assert str(source) in args


class TestRestoreSyncPath:
    """Tests for restore_sync_path."""

    def test_missing_archive_returns_error(self, tmp_path: Path) -> None:
        result = restore_sync_path("claude", tmp_path)
        assert not result.success
        assert "Archive not found" in result.stderr

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_creates_target_dir_and_extracts(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_root = tmp_path / "config"
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(config_root))
        source = tmp_path / "staging"
        source.mkdir()
        archive = source / "djinn-sync-claude.tar.gz"
        archive.touch()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = restore_sync_path("claude", source)

        assert result.success
        assert (config_root / "claude").is_dir()
        args = mock_run.call_args[0][0]
        assert args[:2] == ["tar", "xzf"]

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_uses_config_root_from_config(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        code_dir = tmp_path / "projects"
        code_dir.mkdir()
        configured_root = tmp_path / "configured-root"
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)
        config = load_config(config_file)
        source = tmp_path / "staging"
        source.mkdir()
        (source / "djinn-sync-claude.tar.gz").touch()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = restore_sync_path("claude", source, config)

        assert result.success
        assert (configured_root / "claude").is_dir()


class TestClearSyncPath:
    """Tests for clear_sync_path."""

    def test_clears_contents_preserves_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "claude"
        target.mkdir()
        (target / "file1.txt").write_text("content")
        (target / "subdir").mkdir()
        (target / "subdir" / "file2.txt").write_text("nested")

        assert clear_sync_path(target)
        assert target.is_dir()
        assert list(target.iterdir()) == []

    def test_returns_false_for_missing_path(self, tmp_path: Path) -> None:
        assert not clear_sync_path(tmp_path / "missing")


class TestSyncArchiveHelpers:
    """Tests for is_sync_archive and extract_sync_path_name."""

    def test_is_sync_archive_detects_prefix(self) -> None:
        assert is_sync_archive("djinn-sync-claude.tar.gz")
        assert not is_sync_archive("djinn-opencode-data.tar.gz")
        assert not is_sync_archive("random.tar.gz")

    def test_extract_sync_path_name(self) -> None:
        assert extract_sync_path_name("djinn-sync-claude.tar.gz") == "claude"
        assert extract_sync_path_name("djinn-sync-repo-dotfiles.tar.gz") == "repo-dotfiles"


class TestBackupVolume:
    """Tests for backup_volume."""

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_successful_backup(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = backup_volume("djinn-opencode-data", Path("/tmp/staging"))
        assert result.success
        assert result.returncode == 0

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_backup_command_structure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        backup_volume("djinn-opencode-data", Path("/tmp/staging"))
        args = mock_run.call_args[0][0]
        assert args[0:3] == [docker_mod.DOCKER_EXECUTABLE, "run", "--rm"]
        assert "djinn-opencode-data:/source:ro" in args[4]
        assert "/tmp/staging:/backup" in args[6]
        assert "alpine" in args
        assert "tar" in args

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_failed_backup(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
        result = backup_volume("djinn-test", Path("/tmp/staging"))
        assert not result.success
        assert result.stderr == "error msg"


class TestRestoreVolume:
    """Tests for restore_volume."""

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_successful_restore(self, mock_run: MagicMock, tmp_path: Path) -> None:
        (tmp_path / "djinn-opencode-data.tar.gz").touch()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = restore_volume("djinn-opencode-data", tmp_path)
        assert result.success
        assert result.returncode == 0

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_restore_command_clears_data(self, mock_run: MagicMock, tmp_path: Path) -> None:
        (tmp_path / "djinn-opencode-data.tar.gz").touch()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        restore_volume("djinn-opencode-data", tmp_path)
        args = mock_run.call_args[0][0]
        # Should use sh -c with rm -rf before tar extract
        assert "sh" in args
        assert "-c" in args
        shell_cmd = args[args.index("-c") + 1]
        assert "rm -rf" in shell_cmd
        assert "tar xzf" in shell_cmd

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_failed_restore(self, mock_run: MagicMock, tmp_path: Path) -> None:
        (tmp_path / "djinn-test.tar.gz").touch()
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="restore error")
        result = restore_volume("djinn-test", tmp_path)
        assert not result.success
        assert result.stderr == "restore error"

    def test_returns_error_when_archive_missing(self, tmp_path: Path) -> None:
        result = restore_volume("nonexistent", tmp_path)
        assert not result.success
        assert "Archive not found" in result.stderr


class TestBackgroundProcessGroupGuard:
    """``djinn start ... &`` must fail fast instead of storming the container.

    A backgrounded TTY-attached compose client turns every terminal-attribute
    call into SIGTTOU. Container PID 1 survives those — namespace init discards a
    signal it has no handler for — but the storm costs host load and overflows
    Docker's event ring buffer, erasing the diagnostic record. Whether it also ends
    the container was never established. The guard refuses that shape.
    """

    @staticmethod
    def _without_runtime_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", _empty_mount_args)

    @staticmethod
    def _stdin_state(
        monkeypatch: pytest.MonkeyPatch,
        *,
        isatty: bool,
        foreground: bool = True,
        no_controlling_terminal: bool = False,
        tty_fds: frozenset[int] | None = None,
    ) -> None:
        """Drive all three standard streams.

        ``tty_fds`` overrides ``isatty`` per descriptor (0=stdin, 1=stdout,
        2=stderr) so the streams can disagree — which is the whole point, because
        Compose picks its TTY from stdout while the storm needs any terminal.
        """

        class _Stream:
            def __init__(self, fd: int) -> None:
                self._fd = fd

            def fileno(self) -> int:
                return self._fd

            def isatty(self) -> bool:
                # `_host_terminal_width()` asks the stream directly, not os.isatty.
                return _isatty(self._fd)

        def _tcgetpgrp(_fd: int) -> int:
            if no_controlling_terminal:
                raise OSError("no controlling terminal")
            return 4242 if foreground else 9999

        def _isatty(fd: int) -> bool:
            return isatty if tty_fds is None else fd in tty_fds

        def _getpgrp() -> int:
            return 4242

        monkeypatch.setattr(docker_mod.sys, "stdin", _Stream(0))
        monkeypatch.setattr(docker_mod.sys, "stdout", _Stream(1))
        monkeypatch.setattr(docker_mod.sys, "stderr", _Stream(2))
        monkeypatch.setattr(docker_mod.os, "isatty", _isatty)
        monkeypatch.setattr(docker_mod.os, "getpgrp", _getpgrp)
        monkeypatch.setattr(docker_mod.os, "tcgetpgrp", _tcgetpgrp)

    def test_detects_background_process_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stdin_state(monkeypatch, isatty=True, foreground=False)
        assert is_background_process_group()

    def test_accepts_foreground_process_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stdin_state(monkeypatch, isatty=True, foreground=True)
        assert not is_background_process_group()

    def test_detects_background_when_only_stdout_is_a_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`djinn start < /dev/null &` — the shape a stdin-only guard waves through.

        Compose selects TTY allocation from the *client's stdout*, so redirecting
        only stdin still yields a terminal, a background process group, and the
        storm. The entrypoint's no-TTY fallback does not help here: a TTY exists.
        """
        self._stdin_state(monkeypatch, isatty=True, foreground=False, tty_fds=frozenset({1, 2}))
        assert is_background_process_group()

    def test_allows_a_background_start_with_stdout_redirected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`djinn start > log &` is safe and must not be refused.

        Compose derives `noTty` from stdout alone, so a redirected stdout means no
        TTY is allocated and nothing calls tcsetattr — whatever stdin and stderr
        are attached to. Refusing this would block a working shape, and the
        entrypoint's no-TTY branch already covers the container side of it.
        """
        self._stdin_state(monkeypatch, isatty=True, foreground=False, tty_fds=frozenset({0, 2}))
        assert not is_background_process_group()

    def test_ignores_non_tty_streams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`< /dev/null > log 2>&1` leaves no terminal at all, so nothing can storm."""
        self._stdin_state(monkeypatch, isatty=False, foreground=False)
        assert not is_background_process_group()

    def test_ignores_missing_controlling_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`setsid` leaves no controlling terminal, so the kernel raises no SIGTTOU."""
        self._stdin_state(monkeypatch, isatty=True, no_controlling_terminal=True)
        assert not is_background_process_group()

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_interactive_run_refuses_from_background(
        self,
        mock_run: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._stdin_state(monkeypatch, isatty=True, foreground=False)

        result = compose_run(mock_app_config, ContainerOptions(), interactive=True)

        assert result.returncode == 1
        assert "SIGTTOU" in result.stderr
        assert "--detach" in result.stderr
        mock_run.assert_not_called()

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_headless_run_is_unaffected(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Headless runs pass ``-T``, allocate no TTY, and need no guard."""
        self._without_runtime_mounts(monkeypatch)
        self._stdin_state(monkeypatch, isatty=True, foreground=False)
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        compose_run(mock_app_config, ContainerOptions(), command="echo", interactive=False)

        mock_run.assert_called_once()


class TestSelfTeardownGuard:
    """`compose down` must never reap the container it is running inside.

    The docker socket is mounted, so any process in the container can do this to
    itself. It is not hypothetical: a mutation test that disabled an unrelated
    guard let `clean_all` through to a real teardown, and `compose down` selects
    by the project name pinned in docker-compose.yml — so it killed the live
    session from a throwaway copy of the repo.
    """

    @staticmethod
    def _inside(monkeypatch: pytest.MonkeyPatch, *, dockerenv: bool, hostname: str) -> None:
        monkeypatch.setattr(docker_mod.Path, "exists", lambda self: dockerenv)
        monkeypatch.setattr(docker_mod.socket, "gethostname", lambda: hostname)

    def test_detects_its_own_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._inside(monkeypatch, dockerenv=True, hostname="djinn")
        assert docker_mod.is_own_container("djinn")

    def test_ignores_a_different_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._inside(monkeypatch, dockerenv=True, hostname="some-other-box")
        assert not docker_mod.is_own_container("djinn")

    def test_ignores_the_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No /.dockerenv means we are on the host, where teardown is the point."""
        self._inside(monkeypatch, dockerenv=False, hostname="djinn")
        assert not docker_mod.is_own_container("djinn")

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_compose_down_refuses_from_inside(
        self, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(docker_mod, "is_own_container", lambda _name: True)

        result = compose_down()

        assert result.returncode == 1
        assert "Refusing to tear down" in result.stderr
        assert "djinn clean" in result.stderr
        mock_run.assert_not_called()


class TestVolumeSpecsFromMountArgs:
    """``up`` takes no ``-v`` flags, so specs are lifted out of the run-style args."""

    def test_extracts_specs_after_volume_flags(self) -> None:
        assert docker_mod._volume_specs_from_mount_args(["-v", "/a:/b", "-v", "/c:/d:ro"]) == [
            "/a:/b",
            "/c:/d:ro",
        ]

    def test_ignores_unrelated_arguments(self) -> None:
        assert docker_mod._volume_specs_from_mount_args(["--rm", "-e", "X=1"]) == []

    def test_ignores_volume_flag_without_specification(self) -> None:
        assert docker_mod._volume_specs_from_mount_args(["-v"]) == []

    def test_extracts_env_pairs(self) -> None:
        assert docker_mod._env_pairs_from_mount_args(
            ["-v", "/a:/b", "-e", "PULSE_SERVER=unix:/run/pulse", "-e", "EMPTY="]
        ) == {"PULSE_SERVER": "unix:/run/pulse", "EMPTY": ""}

    def test_env_pairs_ignore_unrelated_arguments(self) -> None:
        assert docker_mod._env_pairs_from_mount_args(["-v", "/a:/b", "--rm"]) == {}


class TestComposeUpDetached:
    """Detached start uses ``up -d``, leaving no TTY client to be backgrounded."""

    @staticmethod
    def _without_runtime_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", _empty_mount_args)

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_issues_up_detached_for_the_service(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        compose_up_detached(mock_app_config, ContainerOptions())

        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == [docker_mod.DOCKER_EXECUTABLE, "compose"]
        assert cmd[-3:] == ["up", "-d", "dev"]

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_hands_dynamic_mounts_over_as_an_override_file(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        generated_overrides: Callable[..., list[Path]],
        djinn_named_project_root: Path,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = djinn_named_project_root
        payload: dict[str, object] = {}
        seen_paths: list[Path] = []

        def _read_override(cmd: list[str], **_kwargs: object) -> MagicMock:
            (override,) = generated_overrides(cmd, "djinn-detach-")
            seen_paths.append(override)
            payload.update(json.loads(override.read_text()))
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _read_override
        options = ContainerOptions(
            mounts=(ContainerMount(tmp_path, Path("/work"), read_only=True),)
        )

        compose_up_detached(mock_app_config, options)

        service = payload["services"]["dev"]  # type: ignore[index]
        assert service["volumes"] == [f"{tmp_path}:/work:ro"]
        assert service["working_dir"] == "/work"
        # The override is Compose's only view of these mounts; it must not outlive the call.
        assert not seen_paths[0].exists()

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_empty_declarations_still_use_a_scoped_override(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        generated_overrides: Callable[..., list[Path]],
        djinn_named_project_root: Path,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = djinn_named_project_root
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        compose_up_detached(mock_app_config, ContainerOptions())

        cmd = mock_run.call_args.args[0]
        (override,) = generated_overrides(cmd, "djinn-detach-")
        assert not override.exists()

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_rejects_a_colliding_mount_like_the_foreground_path(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both paths must refuse the same mounts — a divergence is silent damage.

        Without this the detached path would happily bind over `/home/dev/.claude`
        (or `/proc`) while the identical foreground invocation refuses.
        """
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        options = ContainerOptions(
            mounts=(ContainerMount(Path("/host/data"), Path("/home/dev/.claude")),)
        )

        with pytest.raises(MountCollisionError):
            compose_up_detached(mock_app_config, options)

        mock_run.assert_not_called()

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_carries_the_zone_overlays_like_the_foreground_path(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        generated_overrides: Callable[..., list[Path]],
        djinn_named_project_root: Path,
    ) -> None:
        """Detached startup mounts assigned overlays just like foreground startup."""
        self._without_runtime_mounts(monkeypatch)
        zones_file = mock_app_config.config_root.parent / "zones.toml"
        monkeypatch.setattr(zones_mod, "ZONES_FILE", zones_file)
        mock_root.return_value = djinn_named_project_root
        shared_source = Path(f"{mock_app_config.config_root}.shared") / "claude" / "projects"
        shared_source.mkdir(parents=True)
        local_source = Path(f"{mock_app_config.config_root}.local") / "claude" / "jobs"
        local_source.mkdir(parents=True)
        payload: dict[str, object] = {}

        def _read_override(cmd: list[str], **_kwargs: object) -> MagicMock:
            (override,) = generated_overrides(cmd, "djinn-detach-")
            payload.update(json.loads(override.read_text()))
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _read_override

        compose_up_detached(mock_app_config, ContainerOptions())

        service = payload["services"]["dev"]  # type: ignore[index]
        assert f"{shared_source}:/home/dev/.claude/projects" in service["volumes"]
        assert f"{local_source}:/home/dev/.claude/jobs" in service["volumes"]

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_carries_the_firewall_flag_through_compose_interpolation(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``up`` has no ``-e``, so ENABLE_FIREWALL must ride the host environment."""
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        compose_up_detached(mock_app_config, ContainerOptions(firewall_enabled=True))

        assert mock_run.call_args.kwargs["env"]["ENABLE_FIREWALL"] == "true"

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_marks_the_container_as_detached(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The entrypoint needs to know nobody will use PID 1's shell.

        Rides the same compose interpolation as ENABLE_FIREWALL rather than the
        mount override, so the override stays purely about mounts.
        """
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        compose_up_detached(mock_app_config, ContainerOptions())

        assert mock_run.call_args.kwargs["env"]["DJINN_DETACHED"] == "true"
class TestRunningContainerProbeFailure:
    """A failed probe must stay distinguishable from 'no containers running'.

    `_guard_no_containers_running` refuses on ``None`` and proceeds on ``[]``, so
    collapsing the two would allow backup/restore while a live container may
    still be using the managed data.
    """

    def test_a_failed_docker_call_yields_none_not_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr(docker_mod.subprocess, "run", failing_run)

        assert docker_mod.get_running_containers() is None

    def test_a_missing_docker_binary_yields_none_not_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing_binary(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError

        monkeypatch.setattr(docker_mod.subprocess, "run", missing_binary)

        assert docker_mod.get_running_containers() is None

    def test_a_successful_call_with_no_output_yields_an_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def empty_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        monkeypatch.setattr(docker_mod.subprocess, "run", empty_run)

        assert docker_mod.get_running_containers() == []


@pytest.fixture
def declared_creator(tmp_path, monkeypatch, djinn_named_project_root):
    from djinn_in_a_box.config.models import AppConfig
    from djinn_in_a_box.core import docker

    monkeypatch.setattr(docker, "get_project_root", lambda: djinn_named_project_root)
    source = tmp_path / "archive$disk"
    source.mkdir()
    (source / ".ready").touch()
    monkeypatch.delenv("CDP_HOST", raising=False)
    config = AppConfig(
        code_dir=tmp_path,
        mounts={
            "archive.disk": {"source": str(source), "target": "/archive$disk", "marker": ".ready"},
            "worker": {"volume": True, "target": "/worker\n space$cash", "backup": "none"},
        },
        environment={"CDP_HOST": "$HOST ${HOST:-fallback}\n[brackets]", "EMPTY": ""},
    )
    for name in (
        "get_shell_mount_args",


        "get_sops_age_key_mount_args",
    ):
        monkeypatch.setattr(docker, name, lambda *args: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *args: ([], ()))
    monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
    return config


def _call_declared_creator(kind, config, options=None, **kwargs):
    from djinn_in_a_box.core import docker

    options = options or ContainerOptions()
    if kind == "detached":
        return docker.compose_up_detached(config, options, **kwargs)
    return docker.compose_run(config, options, interactive=kind == "interactive", **kwargs)


@pytest.mark.parametrize("kind", ["interactive", "headless", "detached"])
@pytest.mark.parametrize("mode", [DockerMode.NONE, DockerMode.DIRECT])
@pytest.mark.parametrize("read_only", [True, False, None], ids=["ro", "rw", "default"])
def test_declared_entries_on_every_creator(
    tmp_path, monkeypatch, declared_creator, generated_overrides, kind, mode, read_only,
):
    from djinn_in_a_box.core import docker

    if read_only is not None:
        mounts = declared_creator.model_dump()["mounts"]
        mounts["archive.disk"]["read_only"] = read_only
        declared_creator = AppConfig.model_validate({
            **declared_creator.model_dump(), "mounts": mounts,
        })
    payloads, commands = [], []

    def capture(cmd, **kwargs):
        if kwargs.get("capture_output"):
            assert kwargs["stdin"] is subprocess.DEVNULL
        (override,) = generated_overrides(cmd)
        last_compose_file = max(i for i, arg in enumerate(cmd[:-1]) if arg == "-f")
        assert cmd[last_compose_file + 1] == str(override)
        payloads.append(json.loads(override.read_text()))
        commands.append((cmd, kwargs))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docker.subprocess, "run", capture)
    invocation = ContainerMount(tmp_path, Path("/invocation"))
    _call_declared_creator(
        kind,
        declared_creator,
        ContainerOptions(docker_mode=mode, mounts=(invocation,)),
        env={"AGENT_PROMPT": "prompt"},
    )
    fragment = payloads[0]
    service = fragment["services"]["dev"]
    binds = [m for m in service["volumes"] if isinstance(m, dict) and m["type"] == "bind"]
    volumes = [m for m in service["volumes"] if isinstance(m, dict) and m["type"] == "volume"]
    assert binds == [
        {
            "type": "bind",
            "source": str(tmp_path / "archive$$disk"),
            "target": "/archive$$disk",
            "bind": {"create_host_path": False},
            **({"read_only": True} if read_only is True else {}),
        }
    ]
    assert volumes == [
        {"type": "volume", "source": "djinn-worker", "target": "/worker\n space$$cash"}
    ]
    assert fragment["volumes"] == {"djinn-worker": {"name": "djinn-worker"}}
    assert service["environment"]["CDP_HOST"] == "$$HOST $${HOST:-fallback}\n[brackets]"
    assert service["environment"]["EMPTY"] == ""
    assert service["environment"]["DJINN_DECLARED_VOLUME_TARGETS"] == json.dumps(
        ["/worker\n space$cash"]
    ).replace("$", "$$")
    assert declared_creator.environment["CDP_HOST"] == "$HOST ${HOST:-fallback}\n[brackets]"
    cmd, kwargs = commands[0]
    assert "CDP_HOST" not in kwargs["env"]
    if kind == "detached":
        assert service["working_dir"] == "/invocation"
        assert service["environment"]["AGENT_PROMPT"] == "prompt"
        assert kwargs["env"]["DJINN_DETACHED"] == "true"
    else:
        assert cmd[cmd.index("--workdir") + 1] == "/invocation"
        assert "AGENT_PROMPT=prompt" in cmd
        assert not any(arg.startswith("CDP_HOST=") for arg in cmd)
        assert cmd.index("run") > max(i for i, arg in enumerate(cmd) if arg == "-f")
    for cmd, _ in commands:
        overrides = generated_overrides(cmd)
        assert overrides
        assert all(not override.exists() for override in overrides)


@pytest.mark.parametrize("kind", ["interactive", "headless", "detached"])
@pytest.mark.parametrize(
    "cause", [
        "missing", "marker", "target", "volume", "environment", "schema", "caller", "socket",
        "mode-string", "mode-integer", "volume-ro", "volume-rw",
    ]
)
def test_declaration_refusal_precedes_creation(
    tmp_path, monkeypatch, declared_creator, kind, cause
):
    from djinn_in_a_box.core import docker
    from djinn_in_a_box.core.exceptions import DeclarationSpecificationError

    entry = {"source": str(tmp_path), "target": "/archive"}
    env, caller = {}, {}
    if cause == "missing":
        entry["source"] = str(tmp_path / "missing")
    elif cause == "marker":
        entry["marker"] = ".missing"
    elif cause in ("target", "socket"):
        entry["target"] = "/home/dev/.codex" if cause == "target" else "/var/run/docker.sock"
    elif cause in ("mode-string", "mode-integer"):
        entry["read_only"] = "true" if cause == "mode-string" else 1
    elif cause in ("volume-ro", "volume-rw"):
        entry = {
            "volume": True, "target": "/extra", "backup": "none",
            "read_only": cause == "volume-ro",
        }
    elif cause == "schema":
        entry["backup"] = "data"
    elif cause == "volume":
        entry = {"volume": True, "target": "/extra", "backup": "none"}
    elif cause == "environment":
        env = {"DOCKER_HOST": "do not echo"}
    else:
        env = {"CDP_HOST": "do not echo"}
        caller = {"CDP_HOST": "caller"}
    name = "uv-cache" if cause == "volume" else "archive"
    config = declared_creator.model_copy(update={"mounts": {name: entry}, "environment": env})
    run = MagicMock()
    allocation = MagicMock()
    monkeypatch.setattr(docker.subprocess, "run", run)
    monkeypatch.setattr(docker.tempfile, "mkstemp", allocation)
    with pytest.raises(DeclarationSpecificationError) as exc:
        _call_declared_creator(kind, config, env=caller)
    text = str(exc.value)
    assert (
        "DOCKER_HOST" if cause == "environment" else "CDP_HOST" if cause == "caller" else name
    ) in text
    assert {
        "missing": "does not exist",
        "marker": "marker",
        "target": "conflict",
        "socket": "conflict",
        "volume": "built-in volume",
        "environment": "reserved",
        "schema": "backup",
        "mode-string": "invalid read_only: value must be a boolean",
        "mode-integer": "invalid read_only: value must be a boolean",
        "volume-ro": "invalid read_only: field is not supported for this mount kind",
        "volume-rw": "invalid read_only: field is not supported for this mount kind",
        "caller": "caller",
    }[cause] in text
    assert "do not echo" not in text
    run.assert_not_called()
    allocation.assert_not_called()
    assert not (tmp_path / "missing").exists() and not (tmp_path / ".missing").exists()


@pytest.mark.parametrize("kind", ["interactive", "headless", "detached"])
@pytest.mark.parametrize("outcome", ["success", "exception", "timeout"])
def test_declaration_override_cleanup(
    monkeypatch, declared_creator, generated_overrides, kind, outcome
):
    from djinn_in_a_box.core import docker

    paths = []

    def run(cmd, **kwargs):
        paths.extend(generated_overrides(cmd))
        assert paths and paths[-1].exists()
        if outcome == "exception":
            raise RuntimeError("Docker failed")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, 1)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docker.subprocess, "run", run)
    if outcome == "exception":
        with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
            _call_declared_creator(
                kind, declared_creator, **({"timeout": 1} if kind != "detached" else {})
            )
    else:
        _call_declared_creator(
            kind, declared_creator, **({"timeout": 1} if kind != "detached" else {})
        )
    assert paths and all(not path.exists() for path in paths)


def test_generated_overrides_ignore_other_djinn_paths(tmp_path, generated_overrides):
    temp = Path(tempfile.gettempdir())
    cmd = [
        "-p",
        "djinn-in-a-box",
        "-f",
        str(tmp_path / "djinn-detach-export" / "docker-compose.yml"),
        "-f",
        str(tmp_path / "djinn-run-stray.yml"),
        "-f",
        str(temp / "compose-extra.yml"),
        "-f",
        str(temp / "djinn-run-generated.txt"),
        "--env-file",
        str(temp / "djinn-run-flagged.yml"),
        "-f",
        str(temp / "djinn-run-generated.yml"),
        "-f",
        str(temp / "djinn-detach-generated.yml"),
        "run",
    ]

    assert generated_overrides(cmd) == [
        temp / "djinn-run-generated.yml",
        temp / "djinn-detach-generated.yml",
    ]
    assert generated_overrides(cmd, "djinn-detach-") == [temp / "djinn-detach-generated.yml"]


@pytest.mark.parametrize("prefix", ["djinn-detach-", "djinn-run-", "djinn-probe-"])
def test_generated_overrides_match_real_creation(
    tmp_path, monkeypatch, generated_overrides, prefix
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "temp").mkdir()
    # mkstemp normalizes its relative cached temp directory to an absolute parent.
    monkeypatch.setattr(tempfile, "tempdir", "temp")

    with docker_mod._compose_override({}, prefix=prefix) as path:
        assert generated_overrides(["-f", str(path)]) == [path]
        assert generated_overrides(["-f", str(path)], prefix) == [path]


def test_compose_reservations_match(tmp_path, monkeypatch):
    import socket

    import yaml

    from djinn_in_a_box.config.declarations import COMPOSE_ENV_KEYS, RESERVED_ENVIRONMENT
    from djinn_in_a_box.config.defaults import VOLUME_CATEGORIES
    from djinn_in_a_box.core import desktop, docker, session

    root = Path(__file__).resolve().parents[1]
    keys, names = set(), set()
    for path in root.glob("docker-compose*.yml"):
        data = yaml.safe_load(path.read_text())
        for service in data["services"].values():
            environment = service.get("environment", {})
            keys.update(
                environment
                if isinstance(environment, dict)
                else (item.split("=", 1)[0] for item in environment)
            )
        names.update(volume["name"] for volume in data.get("volumes", {}).values())
    assert keys == COMPOSE_ENV_KEYS
    assert names == {v for values in VOLUME_CATEGORIES.values() for v in values}
    assert set(docker.build_compose_env(None)) <= RESERVED_ENVIRONMENT.keys()
    pulse = tmp_path / "pulse" / "native"
    pulse.parent.mkdir()
    pulse.touch()
    key = tmp_path / "age-key"
    key.touch()
    key.chmod(0o600)
    config = AppConfig(code_dir=tmp_path, sops_age_key_file=key)
    assert set(docker.build_compose_env(config)) <= RESERVED_ENVIRONMENT.keys()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bus:
        bus.bind(str(tmp_path / "bus"))
        runtime_args = [
            *docker.get_sops_age_key_mount_args(config),
        ]
    emitted = docker._env_pairs_from_mount_args(runtime_args)
    assert emitted.keys() == {"SOPS_AGE_KEY_FILE"}
    assert emitted.keys() <= RESERVED_ENVIRONMENT.keys()
    assert set(session._SESSION_ENV) <= RESERVED_ENVIRONMENT.keys()
    for channel in ("dbus", "audio"):
        endpoint = desktop.DesktopEndpoint(channel, tmp_path / channel, True)
        fragment = desktop.helper_fragment(endpoint, "sha256:local", "generation", "generation")
        fragment["services"]["dev"] = {"environment": dict.fromkeys(desktop.MANAGED_ENV)}
        desktop.add_delivery(fragment, endpoint)
        delivery = fragment["services"]["dev"]
        assert set(delivery["environment"]) <= RESERVED_ENVIRONMENT.keys()
        assert endpoint.volume in VOLUME_CATEGORIES["none"]
        assert delivery["volumes"] == [
            {
                "type": "volume",
                "source": f"desktop-{channel}",
                "target": endpoint.target,
                "read_only": True,
            }
        ]
        assert {
            key: delivery["environment"][key] for key in endpoint.environment
        } == endpoint.environment
    assert {
        "AGENT_PROMPT",
        "PULSE_SERVER",
        "DBUS_SESSION_BUS_ADDRESS",
        "SOPS_AGE_KEY_FILE",
        "DJINN_DECLARED_VOLUME_TARGETS",
    } <= RESERVED_ENVIRONMENT.keys()


def test_startup_environment_class_guard():
    import re

    from djinn_in_a_box.config.declarations import RESERVED_ENVIRONMENT

    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "Dockerfile",
        *(root / "scripts").rglob("*"),
        *(root / "tools").rglob("*"),
        *(root / "helpers").rglob("*"),
    ]
    assigned = set()
    pattern = (
        r"(?<![A-Za-z0-9_])([A-Z_][A-Z0-9_]*)\s*=|\bexport\s+([A-Z_][A-Z0-9_]*)|"
        r"\$\{([A-Z_][A-Z0-9_]*):-|^(?:ENV|ARG)\s+([A-Z_][A-Z0-9_]*)"
    )
    for path in paths:
        if path.is_file() and "__pycache__" not in path.parts:
            # Comments are not executable assignments; heredoc content is included.
            text = "\n".join(
                line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
            )
            assigned.update(
                next(part for part in match if part) for match in re.findall(pattern, text, re.M)
            )
    # These names are local script/Python constants, never caller environment.
    exceptions = {
        "_OUTPUT_LIB_DEFAULT",
        "_MCP_REGISTER_DIR",
        "_OUTPUT_LIB",
        "_DJINN_STATE_PERSISTED",
        "_DJINN_OUTPUT_LIB_LOADED",
        "ALL",  # sudoers ALL=(ALL) grammar, not an environment assignment.
        "POLICY",
        "DROP",
        "FLOOR",  # Image-owned Python constants, never environment.
    }
    assert assigned - RESERVED_ENVIRONMENT.keys() - exceptions == set()


def test_captured_runs_never_read_the_terminal(monkeypatch):
    # A Compose prompt must not wait on the caller's terminal until the timeout.
    seen = {}

    def run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(docker_mod.subprocess, "run", run)
    assert docker_mod._run_captured(["docker", "version"]).success
    assert seen["stdin"] is subprocess.DEVNULL
