"""Tests for MCP Gateway commands."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

from ai_dev_base.commands import mcp
from ai_dev_base.commands.mcp import AI_DEV_NETWORK, GATEWAY_CONTAINER


class TestRequireMcpCli:
    """Tests for _require_mcp_cli helper."""

    def test_exits_when_docker_mcp_not_installed(self) -> None:
        with (
            patch("subprocess.run", return_value=MagicMock(returncode=1)),
            pytest.raises(typer.Exit) as exc_info,
        ):
            mcp._require_mcp_cli()
        assert exc_info.value.exit_code == 1

    def test_passes_when_docker_mcp_installed(self) -> None:
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            mcp._require_mcp_cli()


class TestRequireRunning:
    """Tests for _require_running helper."""

    def test_exits_when_gateway_not_running(self) -> None:
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=False),
            pytest.raises(typer.Exit) as exc_info,
        ):
            mcp._require_running()
        assert exc_info.value.exit_code == 1

    def test_passes_when_gateway_running(self) -> None:
        with patch("ai_dev_base.commands.mcp.is_container_running", return_value=True):
            mcp._require_running()


class TestStartCommand:
    """Tests for the start command."""

    def test_start_ensures_network_and_runs_compose(self) -> None:
        with (
            patch("ai_dev_base.commands.mcp._require_mcp_cli"),
            patch("ai_dev_base.commands.mcp.ensure_network") as mock_network,
            patch("subprocess.run") as mock_run,
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("time.sleep"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            mcp.start()
            mock_network.assert_called_once_with(AI_DEV_NETWORK)
            assert mock_run.call_args_list[0][0][0] == ["docker", "compose", "up", "-d"]

    def test_start_exits_on_compose_failure(self) -> None:
        with (
            patch("ai_dev_base.commands.mcp._require_mcp_cli"),
            patch("ai_dev_base.commands.mcp.ensure_network"),
            patch("subprocess.run", return_value=MagicMock(returncode=1)),
            patch("time.sleep"),
            pytest.raises(typer.Exit),
        ):
            mcp.start()


class TestStopCommand:
    def test_stop_runs_compose_down(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            mcp.stop()
            assert mock_run.call_args_list[0][0][0] == ["docker", "compose", "down"]


class TestRestartCommand:
    def test_restart_runs_compose_restart(self) -> None:
        with patch("subprocess.run") as mock_run, patch("time.sleep"):
            mock_run.return_value = MagicMock(returncode=0)
            mcp.restart()
            assert mock_run.call_args_list[0][0][0] == ["docker", "compose", "restart"]


class TestLogsCommand:
    def test_logs_requires_running_gateway(self) -> None:
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=False),
            pytest.raises(typer.Exit),
        ):
            mcp.logs()

    def test_logs_runs_docker_logs(self) -> None:
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
            pytest.raises(typer.Exit) as exc_info,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            mcp.logs()
        assert exc_info.value.exit_code == 0
        cmd = mock_run.call_args_list[0][0][0]
        assert cmd[0:2] == ["docker", "logs"]
        assert GATEWAY_CONTAINER in cmd

    def test_logs_with_follow_flag(self) -> None:
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
            pytest.raises(typer.Exit),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            mcp.logs(follow=True)
        assert "-f" in mock_run.call_args_list[0][0][0]

    def test_logs_with_tail_option(self) -> None:
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
            pytest.raises(typer.Exit),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            mcp.logs(tail=50)
        cmd = mock_run.call_args_list[0][0][0]
        assert "--tail" in cmd
        assert "50" in cmd


@pytest.mark.parametrize(
    ("command", "server", "expected_action"),
    [
        (mcp.enable, "duckduckgo", "enable"),
        (mcp.disable, "memory", "disable"),
    ],
)
class TestEnableDisableCommands:
    """Tests for enable and disable commands (parametrized)."""

    def test_requires_running_gateway(
        self, command: Callable[..., None], server: str, expected_action: str
    ) -> None:
        with (
            patch("ai_dev_base.commands.mcp._require_mcp_cli"),
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=False),
            pytest.raises(typer.Exit),
        ):
            command(server)

    def test_runs_docker_mcp_command(
        self, command: Callable[..., None], server: str, expected_action: str
    ) -> None:
        with (
            patch("ai_dev_base.commands.mcp._require_mcp_cli"),
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            command(server)
            assert mock_run.call_args_list[0][0][0] == [
                "docker",
                "mcp",
                "server",
                expected_action,
                server,
            ]


class TestServersCommand:
    def test_servers_runs_docker_mcp_ls(self) -> None:
        with (
            patch("ai_dev_base.commands.mcp._require_mcp_cli"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="duckduckgo\nmemory")
            mcp.servers()
            assert mock_run.call_args_list[0][0][0] == ["docker", "mcp", "server", "ls"]


class TestCatalogCommand:
    def test_catalog_runs_docker_mcp_catalog(self) -> None:
        with (
            patch("ai_dev_base.commands.mcp._require_mcp_cli"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="catalog data")
            mcp.catalog()
            assert "catalog" in mock_run.call_args_list[0][0][0]


class TestTestCommand:
    def test_test_checks_container_status(self) -> None:
        with (
            patch(
                "ai_dev_base.commands.mcp.is_container_running", return_value=False
            ) as mock_running,
            patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
            pytest.raises(typer.Exit),
        ):
            mcp.test()

        mock_running.assert_called_once_with(GATEWAY_CONTAINER)

    def test_test_checks_localhost_endpoint(self) -> None:
        with (
            patch("ai_dev_base.commands.mcp.is_container_running", return_value=True),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="200")),
        ):
            mcp.test()


class TestCleanCommand:
    def test_clean_requires_confirmation(self) -> None:
        with patch("typer.confirm", return_value=False), pytest.raises(typer.Abort):
            mcp.clean()

    def test_clean_stops_gateway_and_removes_network(self) -> None:
        with (
            patch("typer.confirm", return_value=True),
            patch("subprocess.run") as mock_run,
            patch("shutil.rmtree"),
            patch("pathlib.Path.exists", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            mcp.clean()
            assert mock_run.call_args_list[0][0][0] == ["docker", "compose", "down"]
            assert mock_run.call_args_list[1][0][0] == [
                "docker",
                "network",
                "rm",
                AI_DEV_NETWORK,
            ]

    def test_clean_removes_mcp_config_dir(self) -> None:
        with (
            patch("typer.confirm", return_value=True),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch("shutil.rmtree") as mock_rmtree,
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.home", return_value=Path("/fake/home")),
        ):
            mcp.clean()
            mock_rmtree.assert_called_once()
