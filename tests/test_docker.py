"""Tests for djinn_in_a_box.core.docker module."""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig, ShellConfig
from djinn_in_a_box.core.docker import (
    ContainerOptions,
    DockerMode,
    backup_sync_path,
    backup_volume,
    cleanup_docker_proxy,
    clear_sync_path,
    compose_build,
    compose_run,
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
    is_container_running,
    is_sync_archive,
    restore_sync_path,
    restore_volume,
)


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
