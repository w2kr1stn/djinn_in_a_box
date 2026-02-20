"""Tests for ai_dev_base.core.docker module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_dev_base.config.models import AppConfig, ShellConfig
from ai_dev_base.core.docker import (
    ContainerOptions,
    RunResult,
    cleanup_docker_proxy,
    compose_build,
    compose_down,
    compose_run,
    compose_up,
    delete_volume,
    delete_volumes,
    ensure_network,
    get_compose_files,
    get_existing_volumes_by_category,
    get_running_containers,
    get_shell_mount_args,
    is_container_running,
    list_volumes,
    network_exists,
    volume_exists,
)

# =============================================================================
# Data Classes Tests
# =============================================================================


class TestContainerOptions:
    """Tests for ContainerOptions dataclass."""

    def test_default_values(self) -> None:
        """Test default values are correct."""
        options = ContainerOptions()
        assert options.docker_enabled is False
        assert options.docker_direct is False
        assert options.firewall_enabled is False
        assert options.mount_path is None
        assert options.shell_mounts is True

    def test_custom_values(self) -> None:
        """Test custom values are set correctly."""
        options = ContainerOptions(
            docker_enabled=True,
            firewall_enabled=True,
            mount_path=Path("/workspace"),
            shell_mounts=False,
        )
        assert options.docker_enabled is True
        assert options.firewall_enabled is True
        assert options.mount_path == Path("/workspace")
        assert options.shell_mounts is False

    def test_docker_direct_enabled(self) -> None:
        """Test docker_direct can be set to True."""
        options = ContainerOptions(docker_direct=True)
        assert options.docker_direct is True
        assert options.docker_enabled is False


class TestRunResult:
    """Tests for RunResult dataclass."""

    def test_success_property_true(self) -> None:
        """Test success property returns True for returncode 0."""
        result = RunResult(returncode=0)
        assert result.success is True

    def test_success_property_false(self) -> None:
        """Test success property returns False for non-zero returncode."""
        result = RunResult(returncode=1)
        assert result.success is False
        result = RunResult(returncode=127)
        assert result.success is False

    def test_default_values(self) -> None:
        """Test default values for stdout, stderr."""
        result = RunResult(returncode=0)
        assert result.stdout == ""
        assert result.stderr == ""

    def test_full_result(self) -> None:
        """Test result with all fields populated."""
        result = RunResult(
            returncode=0,
            stdout="output",
            stderr="error",
        )
        assert result.returncode == 0
        assert result.stdout == "output"
        assert result.stderr == "error"


# =============================================================================
# Network Management Tests
# =============================================================================


class TestNetworkExists:
    """Tests for network_exists function."""

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_network_exists_true(self, mock_run: MagicMock) -> None:
        """Test returns True when network exists."""
        mock_run.return_value = MagicMock(returncode=0)
        assert network_exists("ai-dev-network") is True
        mock_run.assert_called_once()

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_network_exists_false(self, mock_run: MagicMock) -> None:
        """Test returns False when network does not exist."""
        mock_run.return_value = MagicMock(returncode=1)
        assert network_exists("nonexistent") is False

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_network_exists_default_name(self, mock_run: MagicMock) -> None:
        """Test uses default network name."""
        mock_run.return_value = MagicMock(returncode=0)
        network_exists()
        call_args = mock_run.call_args[0][0]
        assert "ai-dev-network" in call_args


class TestEnsureNetwork:
    """Tests for ensure_network function."""

    @patch("ai_dev_base.core.docker._docker_inspect")
    def test_network_already_exists(self, mock_inspect: MagicMock) -> None:
        """Test returns True when network already exists."""
        mock_inspect.return_value = True
        result = ensure_network()
        assert result is True

    @patch("ai_dev_base.core.docker.subprocess.run")
    @patch("ai_dev_base.core.docker._docker_inspect")
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

    @patch("ai_dev_base.core.docker.subprocess.run")
    @patch("ai_dev_base.core.docker._docker_inspect")
    def test_create_network_fails(self, mock_inspect: MagicMock, mock_run: MagicMock) -> None:
        """Test returns False when network creation fails."""
        mock_inspect.return_value = False
        mock_run.return_value = MagicMock(returncode=1)
        result = ensure_network()
        assert result is False


# =============================================================================
# Compose File Management Tests
# =============================================================================


class TestGetComposeFiles:
    """Tests for get_compose_files function."""

    @patch("ai_dev_base.core.docker.get_project_root")
    def test_without_docker(self, mock_root: MagicMock) -> None:
        """Test returns only base compose file when docker_enabled=False."""
        mock_root.return_value = Path("/project")
        files = get_compose_files(docker_enabled=False)
        assert len(files) == 2  # ["-f", "path"]
        assert files[0] == "-f"
        assert "docker-compose.yml" in files[1]
        assert "docker-compose.docker.yml" not in str(files)

    @patch("ai_dev_base.core.docker.get_project_root")
    def test_with_docker(self, mock_root: MagicMock) -> None:
        """Test returns both compose files when docker_enabled=True."""
        mock_root.return_value = Path("/project")
        files = get_compose_files(docker_enabled=True)
        assert len(files) == 4  # ["-f", "path1", "-f", "path2"]
        assert files.count("-f") == 2
        # Check both files are present
        file_paths = [f for f in files if f != "-f"]
        assert any("docker-compose.yml" in f for f in file_paths)
        assert any("docker-compose.docker.yml" in f for f in file_paths)

    @patch("ai_dev_base.core.docker.get_project_root")
    def test_default_is_without_docker(self, mock_root: MagicMock) -> None:
        """Test default is docker_enabled=False."""
        mock_root.return_value = Path("/project")
        files = get_compose_files()
        assert len(files) == 2

    @patch("ai_dev_base.core.docker.get_project_root")
    def test_with_docker_direct(self, mock_root: MagicMock) -> None:
        """Test returns docker-direct compose file when docker_direct=True."""
        mock_root.return_value = Path("/project")
        files = get_compose_files(docker_direct=True)
        assert len(files) == 4
        file_paths = [f for f in files if f != "-f"]
        assert any("docker-compose.yml" in f for f in file_paths)
        assert any("docker-compose.docker-direct.yml" in f for f in file_paths)

    @patch("ai_dev_base.core.docker.get_project_root")
    def test_docker_direct_excludes_proxy(self, mock_root: MagicMock) -> None:
        """Test docker_direct does not include proxy compose file."""
        mock_root.return_value = Path("/project")
        files = get_compose_files(docker_direct=True)
        file_paths = [f for f in files if f != "-f"]
        assert not any("docker-compose.docker.yml" in f and "direct" not in f for f in file_paths)

    @patch("ai_dev_base.core.docker.get_project_root")
    def test_docker_enabled_takes_precedence(self, mock_root: MagicMock) -> None:
        """Test docker_enabled=True uses proxy even if docker_direct=True."""
        mock_root.return_value = Path("/project")
        files = get_compose_files(docker_enabled=True, docker_direct=True)
        file_paths = [f for f in files if f != "-f"]
        assert any("docker-compose.docker.yml" in f and "direct" not in f for f in file_paths)
        assert not any("docker-compose.docker-direct.yml" in f for f in file_paths)


# =============================================================================
# Shell Mount Configuration Tests
# =============================================================================


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


# =============================================================================
# Container Status Tests
# =============================================================================


class TestIsContainerRunning:
    """Tests for is_container_running function."""

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_container_running(self, mock_run: MagicMock) -> None:
        """Test returns True when container is running."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="ai-dev-docker-proxy\n",
        )
        assert is_container_running("ai-dev-docker-proxy") is True

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_container_not_running(self, mock_run: MagicMock) -> None:
        """Test returns False when container is not running."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
        )
        assert is_container_running("ai-dev-docker-proxy") is False

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_partial_match_rejected(self, mock_run: MagicMock) -> None:
        """Test partial name matches are rejected."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="ai-dev-docker-proxy-2\n",
        )
        assert is_container_running("ai-dev-docker-proxy") is False


class TestGetRunningContainers:
    """Tests for get_running_containers function."""

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_returns_container_list(self, mock_run: MagicMock) -> None:
        """Test returns list of running containers."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="ai-dev\nai-dev-docker-proxy\n",
        )
        containers = get_running_containers()
        assert "ai-dev" in containers
        assert "ai-dev-docker-proxy" in containers

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_returns_empty_list_on_error(self, mock_run: MagicMock) -> None:
        """Test returns empty list on command failure."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        containers = get_running_containers()
        assert containers == []


# =============================================================================
# Volume Management Tests
# =============================================================================


class TestListVolumes:
    """Tests for list_volumes function."""

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_returns_volume_list(self, mock_run: MagicMock) -> None:
        """Test returns list of volumes."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="ai-dev-claude-config\nai-dev-uv-cache\n",
        )
        volumes = list_volumes()
        assert "ai-dev-claude-config" in volumes
        assert "ai-dev-uv-cache" in volumes

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_returns_empty_list_on_error(self, mock_run: MagicMock) -> None:
        """Test returns empty list on command failure."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        volumes = list_volumes()
        assert volumes == []


class TestVolumeExists:
    """Tests for volume_exists function."""

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_volume_exists_true(self, mock_run: MagicMock) -> None:
        """Test returns True when volume exists."""
        mock_run.return_value = MagicMock(returncode=0)
        assert volume_exists("ai-dev-claude-config") is True

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_volume_exists_false(self, mock_run: MagicMock) -> None:
        """Test returns False when volume does not exist."""
        mock_run.return_value = MagicMock(returncode=1)
        assert volume_exists("nonexistent") is False


class TestDeleteVolume:
    """Tests for delete_volume function."""

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_delete_success(self, mock_run: MagicMock) -> None:
        """Test returns True on successful deletion."""
        mock_run.return_value = MagicMock(returncode=0)
        assert delete_volume("ai-dev-uv-cache") is True

    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_delete_failure(self, mock_run: MagicMock) -> None:
        """Test returns False on deletion failure."""
        mock_run.return_value = MagicMock(returncode=1)
        assert delete_volume("volume-in-use") is False


class TestDeleteVolumes:
    """Tests for delete_volumes function."""

    @patch("ai_dev_base.core.docker.delete_volume")
    def test_deletes_multiple_volumes(self, mock_delete: MagicMock) -> None:
        """Test deletes multiple volumes and returns status dict."""
        mock_delete.side_effect = [True, False, True]
        volumes = ["vol1", "vol2", "vol3"]
        results = delete_volumes(volumes)
        assert results == {"vol1": True, "vol2": False, "vol3": True}
        assert mock_delete.call_count == 3


# =============================================================================
# Compose Operations Tests
# =============================================================================


class TestComposeBuild:
    """Tests for compose_build function."""

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
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

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_build_no_cache(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Test includes --no-cache flag when requested."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_build(no_cache=True)
        cmd = mock_run.call_args[0][0]
        assert "--no-cache" in cmd


class TestComposeRun:
    """Tests for compose_run function."""

    @pytest.fixture
    def mock_app_config(self, tmp_path: Path) -> AppConfig:
        """Provide mock app configuration."""
        from ai_dev_base.config.models import ResourceLimits

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        return AppConfig(
            code_dir=projects_dir,
            resources=ResourceLimits(),
            shell=ShellConfig(),
        )

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
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

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
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

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
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


class TestComposeUp:
    """Tests for compose_up function."""

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_up_detached(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Test runs with -d flag by default."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_up()
        cmd = mock_run.call_args[0][0]
        assert "-d" in cmd

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_up_with_services(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Test includes service names when specified."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_up(services=["docker-proxy"])
        cmd = mock_run.call_args[0][0]
        assert "docker-proxy" in cmd


class TestComposeDown:
    """Tests for compose_down function."""

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_down_basic(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Test basic down command."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_down()
        cmd = mock_run.call_args[0][0]
        assert "down" in cmd
        assert "-v" not in cmd

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_down_with_volumes(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Test includes -v flag when remove_volumes=True."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_down(remove_volumes=True)
        cmd = mock_run.call_args[0][0]
        assert "-v" in cmd


class TestCleanupDockerProxy:
    """Tests for cleanup_docker_proxy function."""

    @patch("ai_dev_base.core.docker.subprocess.run")
    @patch("ai_dev_base.core.docker.get_project_root")
    def test_skips_when_docker_disabled(self, mock_root: MagicMock, mock_run: MagicMock) -> None:
        """Test does nothing when docker_enabled=False."""
        cleanup_docker_proxy(docker_enabled=False)
        mock_run.assert_not_called()

    @patch("ai_dev_base.core.docker.subprocess.run")
    @patch("ai_dev_base.core.docker.get_project_root")
    def test_stops_and_removes_proxy(self, mock_root: MagicMock, mock_run: MagicMock) -> None:
        """Test stops and removes docker-proxy when docker_enabled=True."""
        mock_root.return_value = Path("/project")
        mock_run.return_value = MagicMock(returncode=0)
        cleanup_docker_proxy(docker_enabled=True)
        assert mock_run.call_count == 2
        # First call should be stop
        first_call = mock_run.call_args_list[0][0][0]
        assert "stop" in first_call
        assert "docker-proxy" in first_call
        # Second call should be rm
        second_call = mock_run.call_args_list[1][0][0]
        assert "rm" in second_call
        assert "docker-proxy" in second_call


# =============================================================================
# Volume Category Tests
# =============================================================================


class TestGetExistingVolumesByCategory:
    """Tests for get_existing_volumes_by_category function."""

    @patch("ai_dev_base.core.docker.volume_exists")
    def test_filters_existing_volumes(self, mock_exists: MagicMock) -> None:
        """Test returns only volumes that exist."""

        # Mock volume_exists to return True for some volumes
        def exists_side_effect(name: str) -> bool:
            return name in ["ai-dev-claude-config", "ai-dev-gemini-config"]

        mock_exists.side_effect = exists_side_effect

        volumes = get_existing_volumes_by_category("credentials")
        assert "ai-dev-claude-config" in volumes
        assert "ai-dev-gemini-config" in volumes

    @patch("ai_dev_base.core.docker.volume_exists")
    def test_returns_empty_for_unknown_category(self, mock_exists: MagicMock) -> None:
        """Test returns empty list for unknown category."""
        mock_exists.return_value = True
        volumes = get_existing_volumes_by_category("unknown")
        assert volumes == []


# =============================================================================
# Subprocess Error Handling Tests
# =============================================================================


class TestComposeRunErrorHandling:
    """Tests for subprocess error handling in compose_run."""

    @pytest.fixture
    def mock_app_config(self, tmp_path: Path) -> AppConfig:
        """Provide mock app configuration."""
        from ai_dev_base.config.models import ResourceLimits

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        return AppConfig(
            code_dir=projects_dir,
            resources=ResourceLimits(),
            shell=ShellConfig(),
        )

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
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

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
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

    @patch("ai_dev_base.core.docker.get_project_root")
    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_handles_docker_not_found_interactive(
        self,
        mock_run: MagicMock,
        mock_root: MagicMock,
        mock_app_config: AppConfig,
    ) -> None:
        """Test graceful handling when docker command is not found in interactive mode."""
        mock_root.return_value = Path("/project")
        mock_run.side_effect = FileNotFoundError("[Errno 2] No such file or directory: 'docker'")

        options = ContainerOptions()
        result = compose_run(mock_app_config, options, interactive=True)

        # Should return error result, not crash
        assert result.returncode == 127
        assert "docker" in result.stderr.lower() or "not found" in result.stderr.lower()
