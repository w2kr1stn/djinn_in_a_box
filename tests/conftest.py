"""Pytest configuration and fixtures for Djinn in a Box tests."""

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Rich reads color-forcing variables from the live process environment at
# print time; an inherited FORCE_COLOR injects ANSI codes mid-string and
# breaks substring assertions. Scrub before any CLI module is imported so
# the suite behaves identically regardless of the caller's shell.
os.environ.pop("FORCE_COLOR", None)

from djinn_in_a_box.config.models import AppConfig, ResourceLimits, ShellConfig
from djinn_in_a_box.core.paths import get_project_root

_MUTATING_DOCKER_VERBS = frozenset({
    "attach", "build", "bake", "commit", "connect", "create", "disconnect", "down", "exec", "kill",
    "load", "pause", "prune", "pull", "push", "rename", "restart", "rm", "rmi", "run", "start",
    "stop", "tag", "unpause", "up", "update",
})


@pytest.fixture(autouse=True)
def _forbid_real_docker(request, monkeypatch):
    """Unit tests never change the host daemon; opt-in live modules are exempt."""
    if request.module.__name__.endswith("_live_docker"):
        return
    from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE

    original = subprocess.Popen.__init__

    def guarded(self, args, *rest, **kwargs):
        argv = [str(arg) for arg in args] if isinstance(args, list | tuple) else [str(args)]
        if (
            argv[0] in {DOCKER_EXECUTABLE, "docker"}
            and "--help" not in argv
            and _MUTATING_DOCKER_VERBS.intersection(argv[1:])
        ):
            raise AssertionError(f"unit test would change the real Docker host: {argv!r}")
        original(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)


@pytest.fixture(autouse=True)
def _isolate_hostctl_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "hostctl-state-home"))


@pytest.fixture(autouse=True)
def _isolate_legacy_compose_subprocess_tests(request, monkeypatch, tmp_path):
    """Legacy Compose tests mock Docker. Dedicated Git tests exercise the real lifecycle."""
    dedicated = {"test_git_runtime", "test_desktop", "test_desktop_runtime", "test_desktop_live",
                 "test_hostctl", "test_hostctl_observer", "test_hostctl_doctor",
                 "test_agent_docker", "test_agent_docker_p2"}
    if request.module.__name__.split(".")[-1] in dedicated:
        return
    from djinn_in_a_box.core import docker

    @contextmanager
    def isolated(*args):
        owner = MagicMock()
        owner.observer.poll.return_value = None
        yield owner

    monkeypatch.setattr(docker, "git_runtime", isolated)
    monkeypatch.setattr(docker, "_prepare_companions", lambda *args: None)
    runtime = tmp_path / "runtime-owner"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(docker.host_runtime, "runtime_root", lambda **kwargs: runtime)
    monkeypatch.setattr(docker.host_runtime, "inspect_object", lambda *args, **kwargs: None)


@pytest.fixture
def git_inputs(tmp_path, monkeypatch, request):
    """Two disposable identities and public trust; never touches the user's keys."""
    from types import SimpleNamespace

    from djinn_in_a_box.config.ssh import GitConfig, GitIdentity
    from djinn_in_a_box.core import host_runtime

    home = tmp_path / "home"
    ssh = home / ".ssh"
    ssh.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    passwd_home = tempfile.TemporaryDirectory(prefix="djinn-h-")
    request.addfinalizer(passwd_home.cleanup)
    monkeypatch.setenv("DJINN_TEST_PASSWD_HOME", passwd_home.name)
    monkeypatch.setattr(
        host_runtime.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=passwd_home.name),
    )
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    identities = {}
    for alias, name in (("git-work", "work_git"), ("git-personal", "personal_git")):
        key = ssh / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        identities[alias] = GitIdentity(
            hostname="git.example.com",
            user="git",
            key_file=key,
            public_key_file=Path(str(key) + ".pub"),
        )
    public = identities["git-work"].public_key_file.read_text().split()[:2]
    (ssh / "known_hosts").write_text(
        "git.example.com "
        + " ".join(public)
        + "\n"
        + "other.example.com "
        + " ".join(public)
        + "\n"
    )
    signers = ssh / "allowed_signers"
    signers.write_text("signer@example.com " + " ".join(public) + "\n")
    projects = tmp_path / "projects"
    projects.mkdir()
    config = AppConfig(
        code_dir=projects,
        config_root=tmp_path / "config",
        git=GitConfig(
            identities=identities, signing_identity="git-work", allowed_signers_file=signers
        ),
    )
    with tempfile.TemporaryDirectory(prefix="djinn-git-") as temporary:
        monkeypatch.setenv("XDG_RUNTIME_DIR", temporary)
        yield config


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


@pytest.fixture
def generated_overrides() -> Callable[..., list[Path]]:
    """Select generated overrides; project files may live under paths with Djinn prefixes.

    ``_compose_override`` creates ``<prefix>*.yml`` directly in the temp dir.
    """

    def select(cmd: Sequence[str], prefix: str = "djinn-") -> list[Path]:
        temp_dir = Path(os.path.abspath(tempfile.gettempdir()))
        overrides = []
        for index, arg in enumerate(cmd[:-1]):
            if arg != "-f":
                continue
            path = Path(cmd[index + 1])
            if (
                path.parent == temp_dir
                and path.name.startswith(prefix)
                and path.suffix == ".yml"
            ):
                overrides.append(path)
        return overrides

    return select


@pytest.fixture
def djinn_named_project_root(tmp_path: Path) -> Path:
    """Create a decoy root so loose path predicates also match project compose files."""
    project_root = tmp_path / "djinn-detach-export"
    project_root.mkdir()
    repository_root = Path(__file__).resolve().parents[1]
    for compose_file in repository_root.glob("docker-compose*.yml"):
        shutil.copy(compose_file, project_root / compose_file.name)
    return project_root
