"""Pytest configuration and fixtures for AI Dev Base tests."""

import os
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture
def mock_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mock the home directory for testing XDG paths."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def change_dir(tmp_path: Path) -> Generator[Path]:
    """Temporarily change working directory to tmp_path."""
    original_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(original_cwd)
