"""WS-0 correctness tests — env-bridge, config_root, ensure_host_env, doctor.

Covers the keystone surface: the config->compose env bridge (every compose
subprocess receives the rendered host env), the single-choke-point structural
invariant, get_config_root precedence, host-env provisioning, and the doctor
diagnostic + preflight.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

import djinn_in_a_box.core.docker as docker_mod
from djinn_in_a_box.commands import doctor as doctor_mod
from djinn_in_a_box.config.defaults import SYNC_PATHS
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig, BuildConfig
from djinn_in_a_box.core.docker import (
    ContainerOptions,
    DockerMode,
    build_compose_env,
    cleanup_docker_proxy,
    compose_build,
    compose_down,
    compose_run,
    ensure_host_env,
    get_config_root,
)
from djinn_in_a_box.core.seeding import SeedingError

_GUARDED = ("CODE_DIR", "DJINN_CONFIG_ROOT")


class TestBuildComposeEnv:
    """build_compose_env renders the compose interpolation variables."""

    def test_full_render_from_config(self, mock_app_config: AppConfig) -> None:
        env = build_compose_env(mock_app_config)
        assert env["CODE_DIR"] == str(mock_app_config.code_dir)
        assert env["DJINN_CONFIG_ROOT"] == str(get_config_root(mock_app_config))
        assert env["TZ"] == mock_app_config.timezone
        assert env["CPU_LIMIT"] == str(mock_app_config.resources.cpu_limit)
        assert env["MEMORY_LIMIT"] == mock_app_config.resources.memory_limit
        assert env["CPU_RESERVATION"] == str(mock_app_config.resources.cpu_reservation)
        assert env["MEMORY_RESERVATION"] == mock_app_config.resources.memory_reservation

    def test_none_renders_both_guarded_vars(self) -> None:
        env = build_compose_env(None)
        # The two ${...:?}-guarded vars must always be present so compose never aborts.
        for key in _GUARDED:
            assert key in env and env[key]

    def test_build_network_reaches_compose(self, mock_app_config: AppConfig) -> None:
        """`build.network` is interpolated from this var, so the bridge must carry it."""
        assert build_compose_env(mock_app_config)["DJINN_BUILD_NETWORK"] == "default"

    def test_configured_build_network_wins(self, tmp_path: Path) -> None:
        config = AppConfig(code_dir=tmp_path, build=BuildConfig(network="host"))
        assert build_compose_env(config)["DJINN_BUILD_NETWORK"] == "host"

    def test_compose_consumes_the_variable_this_bridge_produces(self) -> None:
        """Both halves of the contract, pinned together.

        Rendering the variable is worthless if the compose file spells it differently
        or reads it in the wrong place — and neither half fails on its own. This
        checks the name, the default, and that it lands on `build.network`.
        """
        compose = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
        assert "network: ${DJINN_BUILD_NETWORK:-default}" in compose
        build_section = compose[compose.find("build:") : compose.find("image: djinn-in-a-box")]
        assert "DJINN_BUILD_NETWORK" in build_section, "variable is not under build:"


class TestComposeEnvBridge:
    """Every compose subprocess must receive env= with the guarded vars."""

    def _env_of(self, mock_run: MagicMock) -> dict[str, str]:
        env = mock_run.call_args.kwargs.get("env")
        assert env is not None, "compose subprocess.run was called without env="
        return env

    def _assert_guarded(self, env: dict[str, str], config: AppConfig) -> None:
        assert env["CODE_DIR"] == str(config.code_dir)
        assert env["DJINN_CONFIG_ROOT"] == str(get_config_root(config))

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_compose_run_interactive(
        self, mock_run: MagicMock, _root: MagicMock, mock_app_config: AppConfig
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        compose_run(mock_app_config, ContainerOptions(), interactive=True)
        self._assert_guarded(self._env_of(mock_run), mock_app_config)
        assert "stdin" not in mock_run.call_args.kwargs

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_compose_run_headless(
        self, mock_run: MagicMock, _root: MagicMock, mock_app_config: AppConfig
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_run(mock_app_config, ContainerOptions(), command="echo", interactive=False)
        self._assert_guarded(self._env_of(mock_run), mock_app_config)
        # Headless agent CLIs (codex exec) hang on an inherited open terminal stdin.
        assert mock_run.call_args.kwargs["stdin"] == subprocess.DEVNULL

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_compose_build(
        self, mock_run: MagicMock, _root: MagicMock, mock_app_config: AppConfig
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_build(mock_app_config)
        self._assert_guarded(self._env_of(mock_run), mock_app_config)

    # Teardown from the host: the self-teardown guard is not what this pins, and
    # the suite may well be running inside the container it would refuse to reap.
    @patch("djinn_in_a_box.core.docker.is_own_container", return_value=False)
    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_compose_down(
        self, mock_run: MagicMock, _root: MagicMock, _own: MagicMock, mock_app_config: AppConfig
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_down(mock_app_config)
        self._assert_guarded(self._env_of(mock_run), mock_app_config)

    # Teardown from the host: the self-teardown guard is not what this pins, and
    # the suite may well be running inside the container it would refuse to reap.
    @patch("djinn_in_a_box.core.docker.is_own_container", return_value=False)
    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_compose_down_removes_orphans(
        self, mock_run: MagicMock, _root: MagicMock, _own: MagicMock, mock_app_config: AppConfig
    ) -> None:
        """Teardown must reap one-off containers and undeclared project containers.

        Compose skips ``compose run`` containers on a plain ``down`` — which is
        how ``start``/``run`` create the dev container — so without this flag
        ``djinn clean`` reports success while the live session survives and
        ``djinn backup`` keeps refusing. It also reaps a proxy left by
        ``--docker`` and services dropped in an upgrade.
        """
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_down(mock_app_config)
        argv = mock_run.call_args.args[0]
        assert "down" in argv
        assert "--remove-orphans" in argv

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_cleanup_docker_proxy_both_calls(
        self, mock_run: MagicMock, _root: MagicMock, mock_app_config: AppConfig
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        cleanup_docker_proxy(DockerMode.PROXY, mock_app_config)
        assert mock_run.call_count == 2  # stop + rm
        for call in mock_run.call_args_list:
            env = call.kwargs.get("env")
            assert env is not None
            self._assert_guarded(env, mock_app_config)

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_rendered_env_precedes_os_environ(
        self,
        mock_run: MagicMock,
        _root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CODE_DIR", "/stale/from/shell")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        compose_build(mock_app_config)
        env = self._env_of(mock_run)
        # build_compose_env(config) must win over the inherited shell value.
        assert env["CODE_DIR"] == str(mock_app_config.code_dir)

    @patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
    @patch("djinn_in_a_box.core.docker.subprocess.run")
    def test_compose_run_env_precedes_os_environ(
        self,
        mock_run: MagicMock,
        _root: MagicMock,
        mock_app_config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The same precedence guarantee on the interactive compose_run path.
        monkeypatch.setenv("CODE_DIR", "/stale/from/shell")
        mock_run.return_value = MagicMock(returncode=0)
        compose_run(mock_app_config, ContainerOptions(), interactive=True)
        assert self._env_of(mock_run)["CODE_DIR"] == str(mock_app_config.code_dir)


class TestComposeChokePointInvariant:
    """The literal "compose" command token may only appear in sanctioned helpers."""

    def test_compose_token_only_in_sanctioned_helpers(self) -> None:
        sanctioned = {"_run_compose", "compose_run"}
        tree = ast.parse(Path(docker_mod.__file__).read_text())
        offenders: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name not in sanctioned:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and sub.value == "compose":
                        offenders.add(node.name)
        assert not offenders, (
            f"`docker compose` command built outside the choke-point: {offenders}"
        )


class TestGetConfigRootPrecedence:
    """env DJINN_CONFIG_ROOT -> config.config_root -> default ~/.djinn/config."""

    def test_env_wins_over_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DJINN_CONFIG_ROOT", "/from/env")
        config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "from_config")
        assert get_config_root(config) == Path("/from/env")

    def test_config_wins_without_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        target = tmp_path / "from_config"
        config = AppConfig(code_dir=tmp_path, config_root=target)
        assert get_config_root(config) == target

    def test_default_without_env_or_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        assert get_config_root() == Path.home() / ".djinn" / "config"


class TestEnsureHostEnv:
    """ensure_host_env provisions the unconditional host bind-mount sources."""

    def test_creates_sources_idempotently(
        self, mock_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        # ensure_host_env touches get_project_root()/config/claude. Without this
        # patch that is the real checkout — this test created the repo's own
        # config/claude/AGENTS.md. Tests must not write into the working copy.
        monkeypatch.setattr(docker_mod, "get_project_root", lambda: mock_home / "project")
        config = AppConfig(code_dir=mock_home, config_root=mock_home / ".djinn" / "config")

        ensure_host_env(config)

        root = get_config_root(config)
        for name in SYNC_PATHS["credentials"]:  # claude, gemini, codex, opencode, gh, age
            assert (root / name).is_dir()
            # Credential stores hold secrets: no group/other access.
            assert (root / name).stat().st_mode & 0o077 == 0
        assert (mock_home / ".djinn" / "sessions").is_dir()
        assert (mock_home / ".djinn" / "backups").is_dir()
        assert (mock_home / ".ssh").is_dir()
        assert (mock_home / ".gitconfig").is_file()

        # Idempotent: a second run must not raise.
        ensure_host_env(config)
        assert (mock_home / ".gitconfig").is_file()

    def test_preserves_existing_gitconfig(
        self, mock_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        monkeypatch.setattr(docker_mod, "get_project_root", lambda: mock_home / "project")
        gitconfig = mock_home / ".gitconfig"
        gitconfig.write_text("[user]\n  name = existing\n")
        config = AppConfig(code_dir=mock_home, config_root=mock_home / ".djinn" / "config")

        ensure_host_env(config)

        assert "existing" in gitconfig.read_text()  # never clobbered

    def test_initialized_claude_root_gets_empty_companion_mount_source(
        self, mock_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = mock_home / "project"
        claude_root = project / "config" / "claude"
        claude_root.mkdir(parents=True)
        (claude_root / "CLAUDE.md").write_text("seed\n")
        monkeypatch.setattr(docker_mod, "get_project_root", lambda: project)
        config = AppConfig(code_dir=mock_home, config_root=mock_home / ".djinn" / "config")

        ensure_host_env(config)

        companion = claude_root / "AGENTS.md"
        assert companion.is_file()
        assert companion.read_bytes() == b""
        companion.write_text("projected\n")
        ensure_host_env(config)
        assert companion.read_text() == "projected\n"

    def test_uninitialized_claude_root_never_gets_companion(
        self, mock_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = mock_home / "project"
        claude_root = project / "config" / "claude"
        claude_root.mkdir(parents=True)
        monkeypatch.setattr(docker_mod, "get_project_root", lambda: project)
        config = AppConfig(code_dir=mock_home, config_root=mock_home / ".djinn" / "config")

        ensure_host_env(config)

        assert not (claude_root / "AGENTS.md").exists()


class TestDoctor:
    """doctor diagnostics + preflight."""

    def test_run_checks_reports_docker_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: False)
        checks = doctor_mod.run_checks(None)
        installed = next(c for c in checks if c.name == "Docker installed")
        assert installed.status is doctor_mod.Status.FAIL
        assert installed.remedy  # actionable remedy present

    def test_run_checks_includes_socket_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: True)
        monkeypatch.setattr(doctor_mod, "_docker_socket_ok", lambda: False)
        checks = doctor_mod.run_checks(None)
        sock = next(c for c in checks if c.name == "Docker socket")
        assert sock.status is doctor_mod.Status.FAIL
        assert "docker" in sock.remedy.lower()

    def test_run_checks_invalid_config_reports_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: True)
        checks = doctor_mod.run_checks(None, config_error="bad value at code_dir")
        cfg = next(c for c in checks if c.name == "Configuration")
        assert cfg.status is doctor_mod.Status.FAIL
        assert "invalid" in cfg.detail

    def test_preflight_exits_when_docker_missing(
        self, monkeypatch: pytest.MonkeyPatch, mock_app_config: AppConfig
    ) -> None:
        monkeypatch.setattr(doctor_mod, "ensure_host_env", MagicMock())
        monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: False)
        with pytest.raises(typer.Exit):
            doctor_mod.preflight(mock_app_config)

    def test_preflight_exits_when_daemon_down(
        self, monkeypatch: pytest.MonkeyPatch, mock_app_config: AppConfig
    ) -> None:
        monkeypatch.setattr(doctor_mod, "ensure_host_env", MagicMock())
        monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: True)
        monkeypatch.setattr(doctor_mod, "docker_daemon_ok", lambda: False)
        with pytest.raises(typer.Exit):
            doctor_mod.preflight(mock_app_config)

    def test_preflight_ok(
        self, monkeypatch: pytest.MonkeyPatch, mock_app_config: AppConfig
    ) -> None:
        mock_ensure = MagicMock()
        mock_seed = MagicMock(return_value=[])
        monkeypatch.setattr(doctor_mod, "ensure_host_env", mock_ensure)
        monkeypatch.setattr(doctor_mod, "seed_config", mock_seed)
        monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: True)
        monkeypatch.setattr(doctor_mod, "docker_daemon_ok", lambda: True)
        assert doctor_mod.preflight(mock_app_config) is None
        mock_ensure.assert_called_once_with(mock_app_config)  # provisioning ran
        mock_seed.assert_not_called()

    def test_preflight_never_seeds_even_when_seed_would_fail(
        self, monkeypatch: pytest.MonkeyPatch, mock_app_config: AppConfig
    ) -> None:
        monkeypatch.setattr(doctor_mod, "ensure_host_env", MagicMock())
        monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: True)
        monkeypatch.setattr(doctor_mod, "docker_daemon_ok", lambda: True)
        monkeypatch.setattr(
            doctor_mod, "seed_config", MagicMock(side_effect=SeedingError("sudo rm -rf /x"))
        )
        assert doctor_mod.preflight(mock_app_config) is None

    def test_preflight_never_seeds_even_when_seed_would_raise_oserror(
        self, monkeypatch: pytest.MonkeyPatch, mock_app_config: AppConfig
    ) -> None:
        monkeypatch.setattr(doctor_mod, "ensure_host_env", MagicMock())
        monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: True)
        monkeypatch.setattr(doctor_mod, "docker_daemon_ok", lambda: True)
        monkeypatch.setattr(
            doctor_mod, "seed_config", MagicMock(side_effect=PermissionError("denied"))
        )
        assert doctor_mod.preflight(mock_app_config) is None

    def test_doctor_command_exits_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failing = [doctor_mod.Check("X", doctor_mod.Status.FAIL, "broken", "fix it")]
        monkeypatch.setattr(doctor_mod, "run_checks", MagicMock(return_value=failing))
        with pytest.raises(typer.Exit) as exc_info:
            doctor_mod.doctor()
        assert exc_info.value.exit_code == 1


class TestConfigRootRoundTrip:
    """save_config writes config_root under [general]; load_config reconstructs it."""

    def test_save_load_preserves_config_root(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.toml"
        custom = tmp_path / "custom_root"
        config = AppConfig(code_dir=tmp_path, config_root=custom)
        save_config(config, cfg_path)
        loaded = load_config(cfg_path)
        # Round-trip preserves the stored (validated/resolved) value.
        assert loaded.config_root == config.config_root
