"""Pytest configuration and fixtures for Djinn in a Box tests."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# Rich reads color-forcing variables from the live process environment at
# print time; an inherited FORCE_COLOR injects ANSI codes mid-string and
# breaks substring assertions. Scrub before any CLI module is imported so
# the suite behaves identically regardless of the caller's shell.
os.environ.pop("FORCE_COLOR", None)

from djinn_in_a_box.config.models import AppConfig, ResourceLimits, ShellConfig
from djinn_in_a_box.core.paths import get_project_root


@pytest.fixture(autouse=True)
def _clear_project_root_cache() -> Iterator[None]:
    """Keep the ``get_project_root`` cache from carrying between tests.

    ``get_project_root`` is ``@functools.cache``d and walks upward looking for
    ``docker-compose.yml``. A test that stubs ``Path.exists`` therefore poisons
    the cache process-wide: with ``True`` it caches the first candidate it hits,
    and every later test reaching the real function is served that wrong value.
    The inverse is just as bad — a test can pass only because an earlier test
    warmed the cache, which makes it fail under ``-k``, ``--lf`` or xdist while
    the full sequential run stays green.
    """
    get_project_root.cache_clear()
    yield
    get_project_root.cache_clear()


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
        config_root=tmp_path / "config",
        resources=ResourceLimits(),
        shell=ShellConfig(),
    )
