"""Tests for djinn_in_a_box.core.docker module."""

import json
import os
import re
import socket
import subprocess
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import djinn_in_a_box.core.docker as docker_mod
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig, ShellConfig
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
    cleanup_docker_proxy,
    clear_sync_path,
    compose_build,
    compose_run,
    compose_up_detached,
    delete_volumes,
    ensure_network,
    extract_sync_path_name,
    get_audio_mount_args,
    get_compose_files,
    get_config_root,
    get_dbus_mount_args,
    get_existing_sync_paths_by_category,
    get_existing_volumes_by_category,
    get_running_containers,
    get_shell_mount_args,
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


def _empty_mount_args(_config: AppConfig | None = None) -> list[str]:
    return []


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
        monkeypatch.setattr(docker_mod, "get_audio_mount_args", _empty_mount_args)
        monkeypatch.setattr(docker_mod, "get_dbus_mount_args", _empty_mount_args)

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
                audio_args=[],
                dbus_args=[],
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
                audio_args=[],
                dbus_args=[],
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
        monkeypatch.setattr(docker_mod, "get_audio_mount_args", _empty_mount_args)
        monkeypatch.setattr(docker_mod, "get_dbus_mount_args", _empty_mount_args)

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
        mounts = resolve_container_mounts((f"{tmp_path}:/home/dev/.config/claude/plugins",))

        assert mounts[0].target == Path("/home/dev/.claude/plugins")
        validate_container_mounts(mounts, mock_app_config, DockerMode.NONE)

    @pytest.mark.parametrize(
        "target",
        [
            "/run/user/1000/pulse/native",
            "/var/run/user/1000/pulse/native",
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
                match=r"conflict path: /run/user/1000/pulse/native",
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
        ["/run/user/1000/bus", "/var/run/user/1000/bus"],
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
                MountCollisionError, match=r"conflict path: /run/user/1000/bus"
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
                match=r"conflict path: /run/user/1000/pulse/native",
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
            (DockerMode.PROXY, Path("/var/run/docker.sock")),
            (DockerMode.PROXY, Path("/run/docker.sock")),
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

        with pytest.raises(MountCollisionError, match=r"conflict path: /run/docker\.sock"):
            validate_container_mounts(
                (ContainerMount(tmp_path, Path("/var")),),
                mock_app_config,
                DockerMode.DIRECT,
                shell_args=[],
                audio_args=[],
                dbus_args=[],
            )

    def test_static_targets_match_the_dev_compose_volume_anchor(self) -> None:
        compose_lines = (Path(__file__).parents[1] / "docker-compose.yml").read_text().splitlines()
        anchor_start = compose_lines.index("x-common-volumes: &common-volumes")
        anchor_end = compose_lines.index("x-common-environment: &common-environment")
        dev_start = compose_lines.index("  dev:")
        networks_start = compose_lines.index("networks:")
        targets: list[Path] = []
        for line in compose_lines[anchor_start + 1 : anchor_end]:
            match = re.fullmatch(r"\s*-\s+.*:(/[^:\s]+)(?::(?:ro|rw))?", line)
            if match:
                targets.append(Path(match.group(1)))

        anchor_lines = compose_lines[anchor_start + 1 : anchor_end]
        _assert_compose_anchor_uses_short_form(anchor_lines)
        assert "    volumes: *common-volumes" in compose_lines[dev_start:networks_start]
        assert tuple(targets) == docker_mod._COMPOSE_DEV_MOUNT_TARGETS

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
            "&& ln --relative -s /home/dev/.gemini /home/dev/.gemini"
        ) == [
            ("~/.claude", "~/.config/claude"),
            ("/home/dev/.gemini", "/home/dev/.gemini"),
        ]
        assert _parse_dockerfile_symlink_line(
            "RUN ln -s ~/.gemini /tmp/g || ln -s ~/.claude /srv/claude"
        ) == [("~/.gemini", "/tmp/g"), ("~/.claude", "/srv/claude")]

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

    def test_dev_service_keeps_its_compose_working_dir(self) -> None:
        """Without a mount no ``--workdir`` is passed, so this line decides where
        a plain ``djinn start`` lands. Deleting it would silently move every
        mount-less start from /home/dev/projects to the image default /home/dev.
        """
        compose_lines = (Path(__file__).parents[1] / "docker-compose.yml").read_text().splitlines()
        dev_start = compose_lines.index("  dev:")
        networks_start = compose_lines.index("networks:")
        dev_block = "\n".join(compose_lines[dev_start:networks_start])

        assert "working_dir: /home/dev/projects" in dev_block


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
        assert "docker" in call_args
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
            "docker",
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
        assert run.call_args_list[1].args[0] == ["docker", "info"]

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
        assert len(files) == 2  # ["-f", "path"]
        assert files[0] == "-f"
        assert "docker-compose.yml" in files[1]
        assert "docker-compose.docker.yml" not in str(files)

    @patch("djinn_in_a_box.core.docker.get_project_root")
    def test_with_docker(self, mock_root: MagicMock) -> None:
        """Test returns both compose files when docker_mode=PROXY."""
        mock_root.return_value = Path("/project")
        files = get_compose_files(DockerMode.PROXY)
        assert len(files) == 4  # ["-f", "path1", "-f", "path2"]
        assert files.count("-f") == 2
        # Check both files are present
        file_paths = [f for f in files if f != "-f"]
        assert any("docker-compose.yml" in f for f in file_paths)
        assert any("docker-compose.docker.yml" in f for f in file_paths)

    @patch("djinn_in_a_box.core.docker.get_project_root")
    def test_with_docker_direct(self, mock_root: MagicMock) -> None:
        """Test returns docker-direct compose file when docker_mode=DIRECT."""
        mock_root.return_value = Path("/project")
        files = get_compose_files(DockerMode.DIRECT)
        assert len(files) == 4
        file_paths = [f for f in files if f != "-f"]
        assert any("docker-compose.yml" in f for f in file_paths)
        assert any("docker-compose.docker-direct.yml" in f for f in file_paths)


class TestBuildComposeEnv:
    """Tests for docker compose host interpolation environment."""

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


class TestGetAudioMountArgs:
    """Tests for get_audio_mount_args function."""

    @pytest.fixture()
    def pulse_socket(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Create a fake PulseAudio socket and set XDG_RUNTIME_DIR."""
        pulse_dir = tmp_path / "pulse"
        pulse_dir.mkdir()
        socket = pulse_dir / "native"
        socket.touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        return socket

    def test_returns_mount_args_when_socket_exists(self, pulse_socket: Path) -> None:
        args = get_audio_mount_args()
        assert "-v" in args
        assert "-e" in args
        assert any("pulse/native" in a for a in args)
        assert any("PULSE_SERVER=" in a for a in args)

    def test_returns_empty_when_no_socket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert get_audio_mount_args() == []

    def test_returns_empty_when_xdg_runtime_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "nonexistent"))
        assert get_audio_mount_args() == []

    def test_fallback_when_xdg_runtime_dir_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr("os.getuid", lambda: 99999)
        assert get_audio_mount_args() == []

    def test_volume_mount_maps_to_container_path(self, pulse_socket: Path) -> None:
        args = get_audio_mount_args()
        v_idx = args.index("-v")
        mount_arg = args[v_idx + 1]
        assert mount_arg.endswith("/run/user/1000/pulse/native")
        assert ":ro" not in mount_arg

    def test_pulse_server_env_points_to_container_socket(self, pulse_socket: Path) -> None:
        args = get_audio_mount_args()
        e_idx = args.index("-e")
        env_arg = args[e_idx + 1]
        assert env_arg == "PULSE_SERVER=unix:/run/user/1000/pulse/native"


class TestGetDbusMountArgs:
    """Tests for get_dbus_mount_args function."""

    @pytest.fixture()
    def dbus_socket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[Path]:
        """Create a real Unix socket at the bus path and set XDG_RUNTIME_DIR."""
        import socket as socket_mod

        bus_path = tmp_path / "bus"
        server = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        server.bind(str(bus_path))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        yield bus_path
        server.close()

    def test_returns_mount_args_when_socket_exists(self, dbus_socket: Path) -> None:
        args = get_dbus_mount_args()
        assert "-v" in args
        assert "-e" in args
        assert f"{dbus_socket}:/run/user/1000/bus:ro" in args
        assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus" in args

    def test_returns_empty_when_no_socket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert get_dbus_mount_args() == []

    def test_returns_empty_for_stale_regular_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A stale non-socket at the bus path must not inject a broken mount.
        (tmp_path / "bus").touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert get_dbus_mount_args() == []

    def test_fallback_when_xdg_runtime_dir_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr("os.getuid", lambda: 99999)
        assert get_dbus_mount_args() == []


class TestIsContainerRunning:
    """Tests for is_container_running function."""

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_container_running(self, mock_run: MagicMock) -> None:
        """Test returns True when container is running."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="djinn-docker-proxy\n",
        )
        assert is_container_running("djinn-docker-proxy") is True

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_container_not_running(self, mock_run: MagicMock) -> None:
        """Test returns False when container is not running."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
        )
        assert is_container_running("djinn-docker-proxy") is False

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_partial_match_rejected(self, mock_run: MagicMock) -> None:
        """Test partial name matches are rejected."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="djinn-docker-proxy-2\n",
        )
        assert is_container_running("djinn-docker-proxy") is False


class TestGetRunningContainers:
    """Tests for get_running_containers function."""

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_returns_container_list(self, mock_run: MagicMock) -> None:
        """Test returns list of running containers."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="djinn\ndjinn-docker-proxy\n",
        )
        containers = get_running_containers()
        assert "djinn" in containers
        assert "djinn-docker-proxy" in containers

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_returns_empty_list_on_error(self, mock_run: MagicMock) -> None:
        """Test returns empty list on command failure."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        containers = get_running_containers()
        assert containers == []


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
        assert "docker" in cmd
        assert "compose" in cmd
        assert "build" in cmd

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_build_no_cache(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Test includes --no-cache flag when requested."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_build(no_cache=True)
        cmd = mock_run.call_args[0][0]
        assert "--no-cache" in cmd


class TestComposeRun:
    """Tests for compose_run function."""

    @staticmethod
    def _without_runtime_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", _empty_mount_args)
        monkeypatch.setattr(docker_mod, "get_audio_mount_args", _empty_mount_args)
        monkeypatch.setattr(docker_mod, "get_dbus_mount_args", _empty_mount_args)

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
            mounts=(
                ContainerMount(tmp_path, Path("/home/dev/.config/claude/plugins")),
            )
        )

        compose_run(mock_app_config, options, command="echo", interactive=False)

        cmd = mock_run.call_args.args[0]
        assert f"{tmp_path}:/home/dev/.claude/plugins" in cmd
        assert cmd[cmd.index("--workdir") + 1] == "/home/dev/.claude/plugins"

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
        audio = MagicMock(return_value=[])
        dbus = MagicMock(return_value=[])
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", shell)
        monkeypatch.setattr(docker_mod, "get_audio_mount_args", audio)
        monkeypatch.setattr(docker_mod, "get_dbus_mount_args", dbus)

        compose_run(mock_app_config, ContainerOptions(), command="echo", interactive=False)

        shell.assert_called_once_with(mock_app_config)
        audio.assert_called_once_with()
        dbus.assert_called_once_with()
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
            audio_mount_args=[],
            dbus_mount_args=[],
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


class TestCleanupDockerProxy:
    """Tests for cleanup_docker_proxy function."""

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    @patch("djinn_in_a_box.core.docker.get_project_root")
    def test_skips_when_docker_disabled(self, mock_root: MagicMock, mock_run: MagicMock) -> None:
        """Test does nothing when docker_mode=NONE."""
        cleanup_docker_proxy(DockerMode.NONE)
        mock_run.assert_not_called()

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    @patch("djinn_in_a_box.core.docker.get_project_root")
    def test_stops_and_removes_proxy(self, mock_root: MagicMock, mock_run: MagicMock) -> None:
        """Test stops and removes docker-proxy when docker_mode=PROXY."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0)
        cleanup_docker_proxy(DockerMode.PROXY)
        assert mock_run.call_count == 2
        # First call: stop docker-proxy
        first_call = mock_run.call_args_list[0][0][0]
        assert first_call[-2:] == ["stop", "docker-proxy"]
        # Second call: rm docker-proxy
        second_call = mock_run.call_args_list[1][0][0]
        assert second_call[-3:] == ["rm", "-f", "docker-proxy"]


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
    """Tests for get_config_root (renamed from get_sync_root)."""

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
        # "gemini" dir intentionally missing

        cat_patch = patch.dict(
            "djinn_in_a_box.core.docker.SYNC_PATHS",
            {"credentials": ["claude", "gemini"]},
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
        (configured_root / "gemini").mkdir(parents=True)
        (env_root / "claude").mkdir(parents=True)
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(env_root))
        config_file = tmp_path / "config.toml"
        save_config(AppConfig(code_dir=code_dir, config_root=configured_root), config_file)
        config = load_config(config_file)

        cat_patch = patch.dict(
            "djinn_in_a_box.core.docker.SYNC_PATHS",
            {"credentials": ["claude", "gemini"]},
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
        sync_root = tmp_path / "sync"
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(sync_root))
        source = tmp_path / "staging"
        source.mkdir()
        archive = source / "djinn-sync-claude.tar.gz"
        archive.touch()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = restore_sync_path("claude", source)

        assert result.success
        assert (sync_root / "claude").is_dir()
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
        assert not is_sync_archive("djinn-claude-config.tar.gz")
        assert not is_sync_archive("random.tar.gz")

    def test_extract_sync_path_name(self) -> None:
        assert extract_sync_path_name("djinn-sync-claude.tar.gz") == "claude"
        assert extract_sync_path_name("djinn-sync-repo-dotfiles.tar.gz") == "repo-dotfiles"


class TestBackupVolume:
    """Tests for backup_volume."""

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_successful_backup(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = backup_volume("djinn-claude-config", Path("/tmp/staging"))
        assert result.success
        assert result.returncode == 0

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_backup_command_structure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        backup_volume("djinn-claude-config", Path("/tmp/staging"))
        args = mock_run.call_args[0][0]
        assert args[0:3] == ["docker", "run", "--rm"]
        assert "djinn-claude-config:/source:ro" in args[4]
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
        (tmp_path / "djinn-claude-config.tar.gz").touch()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = restore_volume("djinn-claude-config", tmp_path)
        assert result.success
        assert result.returncode == 0

    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_restore_command_clears_data(self, mock_run: MagicMock, tmp_path: Path) -> None:
        (tmp_path / "djinn-claude-config.tar.gz").touch()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        restore_volume("djinn-claude-config", tmp_path)
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
    signal it has no handler for — but the storm floods Docker's event ring buffer
    and stops the host-side compose client, after which `--rm` reaps the
    container. The guard refuses that shape.
    """

    @staticmethod
    def _without_runtime_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(docker_mod, "get_shell_mount_args", _empty_mount_args)
        monkeypatch.setattr(docker_mod, "get_audio_mount_args", _empty_mount_args)
        monkeypatch.setattr(docker_mod, "get_dbus_mount_args", _empty_mount_args)

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


class TestVolumeSpecsFromMountArgs:
    """``up`` takes no ``-v`` flags, so specs are lifted out of the run-style args."""

    def test_extracts_specs_after_volume_flags(self) -> None:
        assert docker_mod._volume_specs_from_mount_args(
            ["-v", "/a:/b", "-v", "/c:/d:ro"]
        ) == ["/a:/b", "/c:/d:ro"]

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
        monkeypatch.setattr(docker_mod, "get_audio_mount_args", _empty_mount_args)
        monkeypatch.setattr(docker_mod, "get_dbus_mount_args", _empty_mount_args)

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
        assert cmd[:2] == ["docker", "compose"]
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
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        payload: dict[str, object] = {}
        seen_paths: list[Path] = []

        def _read_override(cmd: list[str], **_kwargs: object) -> MagicMock:
            override = Path(next(arg for arg in cmd if "djinn-detach-" in arg))
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
    def test_omits_the_override_when_nothing_is_dynamic(
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
        assert not any("djinn-detach-" in arg for arg in cmd)

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
    def test_carries_the_env_half_of_the_runtime_mount_pairs(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
    ) -> None:
        """A mounted socket is useless without the variable that points at it.

        Deliberately does not stub the runtime mount builders: passing real
        `-v`/`-e` pairs is the input class that exposes a dropped `-e` half.
        """
        mock_root.return_value = Path("/project")
        payload: dict[str, object] = {}

        def _read_override(cmd: list[str], **_kwargs: object) -> MagicMock:
            override = Path(next(arg for arg in cmd if "djinn-detach-" in arg))
            payload.update(json.loads(override.read_text()))
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _read_override

        compose_up_detached(
            mock_app_config,
            ContainerOptions(),
            shell_mount_args=[],
            audio_mount_args=[
                "-v", "/run/user/1000/pulse/native:/run/user/1000/pulse/native",
                "-e", "PULSE_SERVER=unix:/run/user/1000/pulse/native",
            ],
            dbus_mount_args=[
                "-v", "/run/user/1000/bus:/run/user/1000/bus:ro",
                "-e", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
            ],
        )

        service = payload["services"]["dev"]  # type: ignore[index]
        assert service["environment"] == {
            "PULSE_SERVER": "unix:/run/user/1000/pulse/native",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        }
        assert "/run/user/1000/pulse/native:/run/user/1000/pulse/native" in service["volumes"]

    @patch("djinn_in_a_box.core.docker.get_project_root")
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_explicit_env_overrides_the_derived_pairs(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._without_runtime_mounts(monkeypatch)
        mock_root.return_value = Path("/project")
        payload: dict[str, object] = {}

        def _read_override(cmd: list[str], **_kwargs: object) -> MagicMock:
            override = Path(next(arg for arg in cmd if "djinn-detach-" in arg))
            payload.update(json.loads(override.read_text()))
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = _read_override

        compose_up_detached(
            mock_app_config,
            ContainerOptions(),
            env={"PULSE_SERVER": "explicit"},
            audio_mount_args=["-v", "/host:/sock", "-e", "PULSE_SERVER=derived"],
            shell_mount_args=[],
            dbus_mount_args=[],
        )

        service = payload["services"]["dev"]  # type: ignore[index]
        assert service["environment"]["PULSE_SERVER"] == "explicit"

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
