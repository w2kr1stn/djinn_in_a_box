"""Proxy logs moved from the former audit command."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
import typer

from djinn_in_a_box.commands import logs


class TestProxyCommand:
    """Tests for the proxy command."""

    def test_proxy_requires_proxy_running(self) -> None:
        """Test proxy requires docker proxy to be running."""
        with (
            patch("djinn_in_a_box.commands.logs.is_container_running", return_value=False),
            patch("subprocess.run", side_effect=AssertionError("proxy gate bypassed")),
        ):
            with pytest.raises(typer.Exit) as exc_info:
                logs.proxy()

            assert exc_info.value.exit_code == 1

    def test_proxy_shows_logs(self) -> None:
        """Test proxy shows proxy logs."""
        with (
            patch("djinn_in_a_box.commands.logs.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            # Successful proxy returns normally (no exit)
            logs.proxy()

            # Should have called docker logs
            call_args = mock_run.call_args[0][0]
            assert call_args == [
                logs.DOCKER_EXECUTABLE,
                "logs",
                "--tail",
                "50",
                "djinn-docker-proxy",
            ]
            assert mock_run.call_args.kwargs["stdin"] is subprocess.DEVNULL

    def test_proxy_with_tail_option(self) -> None:
        """Test proxy -n option sets tail count."""
        with (
            patch("djinn_in_a_box.commands.logs.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            # Successful proxy returns normally (no exit)
            logs.proxy(tail=100)

            call_args = mock_run.call_args[0][0]
            assert "--tail" in call_args
            assert "100" in call_args

    def test_proxy_propagates_error_exit_code(self) -> None:
        """Test proxy propagates error exit code from docker logs."""
        with (
            patch("djinn_in_a_box.commands.logs.is_container_running", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1)

            with pytest.raises(typer.Exit) as exc_info:
                logs.proxy()

            assert exc_info.value.exit_code == 1
