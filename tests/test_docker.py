"""Tests for ai_dev_base.core.docker module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_dev_base.config.models import AppConfig, ShellConfig
from ai_dev_base.core.docker import (
    ContainerOptions,
    cleanup_docker_proxy,
    compose_build,
    compose_down,
    compose_run,
    compose_up,
    delete_network,
    delete_volume,
    delete_volumes,
    ensure_network,
    get_compose_files,
    get_running_containers,
    get_shell_mount_args,
    is_container_running,
    network_exists,
    volume_exists,
)


class TestNetworkExists:
    """Tests for network_exists function."""

    @pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_returns_expected_for_returncode(
        self, mock_run: MagicMock, returncode: int, expected: bool
    ) -> None:
        """Test returns correct boolean based on Docker inspect returncode."""
        mock_run.return_value = MagicMock(returncode=returncode)
        assert network_exists("ai-dev-network") is expected


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


class TestVolumeExists:
    """Tests for volume_exists function."""

    @pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_returns_expected_for_returncode(
        self, mock_run: MagicMock, returncode: int, expected: bool
    ) -> None:
        """Test returns correct boolean based on Docker inspect returncode."""
        mock_run.return_value = MagicMock(returncode=returncode)
        assert volume_exists("ai-dev-test") is expected


class TestDeleteNetwork:
    """Tests for delete_network function."""

    @pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_returns_expected_for_returncode(
        self, mock_run: MagicMock, returncode: int, expected: bool
    ) -> None:
        """Test returns correct boolean based on docker network rm returncode."""
        mock_run.return_value = MagicMock(returncode=returncode, stderr="")
        assert delete_network("ai-dev-network") is expected


class TestDeleteVolume:
    """Tests for delete_volume function."""

    @pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
    @patch("ai_dev_base.core.docker.subprocess.run")
    def test_returns_expected_for_returncode(
        self, mock_run: MagicMock, returncode: int, expected: bool
    ) -> None:
        """Test returns correct boolean based on docker volume rm returncode."""
        mock_run.return_value = MagicMock(returncode=returncode, stderr="")
        assert delete_volume("ai-dev-test") is expected


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


@pytest.fixture
def mock_app_config(tmp_path: Path) -> AppConfig:
    """Provide mock app configuration for compose tests."""
    from ai_dev_base.config.models import ResourceLimits

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    return AppConfig(
        code_dir=projects_dir,
        resources=ResourceLimits(),
        shell=ShellConfig(),
    )


class TestComposeRun:
    """Tests for compose_run function."""

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


class TestComposeRunErrorHandling:
    """Tests for subprocess error handling in compose_run."""

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
