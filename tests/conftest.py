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


@pytest.fixture
def declared_app_config(mock_app_config: AppConfig, tmp_path: Path) -> AppConfig:
    """Declared storage alongside sync paths and host data that must survive cleanup."""
    config_root = mock_app_config.config_root
    for name in ("claude", "codex", "opencode", "gh", "age", "repo-dotfiles"):
        path = config_root / name
        path.mkdir(parents=True)
        (path / "sentinel").write_text(name)
    for root in (tmp_path / "shared", tmp_path / "local", tmp_path / "external"):
        root.mkdir()
        (root / "sentinel").write_text(root.name)
    (tmp_path / "external" / ".drive-ready").touch()
    return AppConfig.model_validate({
        **mock_app_config.model_dump(),
        "shared_root": tmp_path / "shared",
        "local_root": tmp_path / "local",
        "mounts": {
            "journal": {"volume": True, "target": "/home/dev/journal", "backup": "data"},
            "scratch": {"volume": True, "target": "/home/dev/scratch", "backup": "cache"},
            "worker": {"volume": True, "target": "/home/dev/worker", "backup": "none"},
            "archive": {
                "source": str(tmp_path / "external"),
                "target": "/home/dev/archive",
                "marker": ".drive-ready",
            },
        },
    })
