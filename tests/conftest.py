"""Pytest configuration and fixtures for Djinn in a Box tests."""

from pathlib import Path

import pytest

from djinn_in_a_box.config.models import AppConfig, ResourceLimits, ShellConfig


@pytest.fixture
def mock_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mock the home directory for testing XDG paths."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def mock_app_config(tmp_path: Path) -> AppConfig:
    """Provide mock app configuration for tests."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    return AppConfig(
        code_dir=projects_dir,
        resources=ResourceLimits(),
        shell=ShellConfig(),
    )
