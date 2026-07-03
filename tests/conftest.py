"""Pytest configuration and fixtures for Djinn in a Box tests."""

import os
from pathlib import Path

import pytest

# Rich reads color-forcing variables from the live process environment at
# print time; an inherited FORCE_COLOR injects ANSI codes mid-string and
# breaks substring assertions. Scrub before any CLI module is imported so
# the suite behaves identically regardless of the caller's shell.
os.environ.pop("FORCE_COLOR", None)

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
