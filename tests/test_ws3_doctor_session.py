"""WS-3 doctor --fix, doctor rows, and session --create acceptance tests."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import config as config_mod
from djinn_in_a_box.commands import doctor as doctor_mod
from djinn_in_a_box.commands import session as session_mod
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import contained_paths, docker
from djinn_in_a_box.core.config_workflow import WorkflowPreparationResult
from djinn_in_a_box.core.exceptions import ConfigNotFoundError, ConfigValidationError
from djinn_in_a_box.core.seeding import seed_config
from djinn_in_a_box.core.session import SessionResult, SessionTarget

runner = CliRunner()


def _returns[T](value: T) -> Callable[..., T]:
    def _stub(*_args: object, **_kwargs: object) -> T:
        return value

    return _stub


@pytest.fixture(autouse=True)
def _ready_session_workflow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(session_mod, "load_config", lambda: object())
    monkeypatch.setattr(
        session_mod,
        "prepare_config_workflow",
        _returns(WorkflowPreparationResult(True)),
    )
    monkeypatch.setattr(session_mod, "get_project_root", lambda: tmp_path / "project")
    monkeypatch.setattr(session_mod, "get_config_root", _returns(tmp_path / "runtime"))


def _quiet_doctor_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    def network_exists(_name: str) -> bool:
        return True

    monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: True)
    monkeypatch.setattr(doctor_mod, "docker_daemon_ok", lambda: True)
    monkeypatch.setattr(doctor_mod, "_docker_socket_ok", lambda: True)
    monkeypatch.setattr(doctor_mod, "compose_v2_ok", lambda: True)
    monkeypatch.setattr(doctor_mod, "buildx_ok", lambda: True)
    monkeypatch.setattr(doctor_mod, "_image_built", lambda: True)
    monkeypatch.setattr(doctor_mod, "network_exists", network_exists)


def _check_named(checks: list[doctor_mod.Check], name: str) -> doctor_mod.Check:
    return next(check for check in checks if check.name == name)


def _no_dbus_mount_args() -> list[str]:
    return []


def _write_seed_targets(project_root: Path, *, skip: set[Path] | None = None) -> None:
    skipped = skip or set()
    for entry in doctor_mod.SEED_MANIFEST:
        if entry.target in skipped:
            continue
        target = project_root / entry.target
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "file":
            target.write_text("")
        else:
            target.mkdir(parents=True, exist_ok=True)


def test_doctor_fix_calls_repairs_and_reports(
    monkeypatch: pytest.MonkeyPatch,
    mock_app_config: AppConfig,
    tmp_path: Path,
) -> None:
    mock_checks = MagicMock(return_value=[])
    mock_host_env = MagicMock()
    mock_seed = MagicMock(return_value=[])
    mock_network = MagicMock(return_value=True)

    monkeypatch.setattr("djinn_in_a_box.config.loader.load_config", lambda: mock_app_config)
    monkeypatch.setattr(doctor_mod, "run_checks", mock_checks)
    monkeypatch.setattr(doctor_mod, "ensure_host_env", mock_host_env)
    monkeypatch.setattr(doctor_mod, "seed_config", mock_seed)
    monkeypatch.setattr(doctor_mod, "ensure_network", mock_network)
    monkeypatch.setattr(doctor_mod, "get_project_root", lambda: tmp_path)

    result = runner.invoke(app, ["doctor", "--fix"])

    assert result.exit_code == 0, result.output
    mock_checks.assert_called_once_with(mock_app_config, None)
    mock_host_env.assert_called_once_with(mock_app_config)
    mock_seed.assert_called_once_with(tmp_path, source="claude")
    mock_network.assert_called_once_with()
    assert "Fixed: host environment" in result.output
    assert "Fixed: seed configuration" in result.output
    assert "Fixed: Docker network" in result.output


def test_doctor_fix_without_config_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"
    config_check = doctor_mod.Check("Configuration", doctor_mod.Status.FAIL, "missing")

    def missing_config() -> AppConfig:
        raise ConfigNotFoundError(config_path)

    monkeypatch.setattr("djinn_in_a_box.config.loader.load_config", missing_config)
    monkeypatch.setattr(doctor_mod, "run_checks", MagicMock(return_value=[config_check]))

    result = runner.invoke(app, ["doctor", "--fix"])

    assert result.exit_code == 1
    assert "Run `djinn init` first" in result.output


@pytest.mark.parametrize("layout", ["equal", "nested"])
@pytest.mark.parametrize("provision", [True, False], ids=["real-provision", "mode-setup-only"])
def test_doctor_fix_invalid_zone_roots_fails_cleanly_and_continues_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str, provision: bool
) -> None:
    code = tmp_path / "code"
    code.mkdir()
    root = tmp_path / "config"
    config = AppConfig(code_dir=code, config_root=root,
                       shared_root=root if layout == "equal" else root / "shared")
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    monkeypatch.setattr("djinn_in_a_box.config.loader.load_config", lambda: config)
    monkeypatch.setattr(doctor_mod, "run_checks", MagicMock(return_value=[]))
    monkeypatch.setattr(doctor_mod, "seed_config", MagicMock(return_value=[]))
    monkeypatch.setattr(doctor_mod, "get_project_root", lambda: tmp_path)
    if not provision:
        monkeypatch.setattr(doctor_mod, "ensure_host_env", MagicMock())
        monkeypatch.setattr(doctor_mod, "load_zone_assignments", MagicMock(return_value=None))
    with (
        patch.object(doctor_mod, "loose_credential_dirs") as loose,
        patch.object(doctor_mod, "ensure_network", return_value=True) as network,
    ):
        result = runner.invoke(app, ["doctor", "--fix"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert "Could not fix: zone directory modes" in result.output
    assert "Zone roots must be distinct and not nested" in result.output
    assert "Fixed: Docker network" in result.output
    loose.assert_not_called()
    network.assert_called_once()


def test_doctor_fix_with_invalid_config_points_to_config_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_check = doctor_mod.Check("Configuration", doctor_mod.Status.FAIL, "present but invalid")

    def invalid_config() -> AppConfig:
        raise ConfigValidationError("bad config")

    monkeypatch.setattr("djinn_in_a_box.config.loader.load_config", invalid_config)
    monkeypatch.setattr(doctor_mod, "run_checks", MagicMock(return_value=[config_check]))

    result = runner.invoke(app, ["doctor", "--fix"])

    assert result.exit_code == 1
    assert "Fix config.toml first" in result.output
    assert "Configuration check above" in result.output
    assert "Run `djinn init` first" not in result.output


def test_doctor_fix_seed_permission_error_prints_chown_remedy(
    monkeypatch: pytest.MonkeyPatch,
    mock_app_config: AppConfig,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("djinn_in_a_box.config.loader.load_config", lambda: mock_app_config)
    monkeypatch.setattr(doctor_mod, "run_checks", MagicMock(return_value=[]))
    monkeypatch.setattr(doctor_mod, "ensure_host_env", MagicMock())
    monkeypatch.setattr(doctor_mod, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        doctor_mod,
        "seed_config",
        MagicMock(side_effect=PermissionError("root-owned")),
    )
    monkeypatch.setattr(doctor_mod, "ensure_network", MagicMock(return_value=True))

    result = runner.invoke(app, ["doctor", "--fix"])

    assert result.exit_code == 1
    assert "Could not fix: seed configuration" in result.output
    assert "root-owned" in result.output
    assert "sudo chown -R" in result.output
    assert str(tmp_path / "config") in result.output


def test_run_checks_reports_buildx_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """`djinn build` runs `docker buildx bake`, so a missing plugin needs a remedy.

    It warns rather than fails: only the build needs buildx, and an existing image
    still starts and runs without it.
    """
    _quiet_doctor_probes(monkeypatch)
    monkeypatch.setattr(doctor_mod, "get_project_root", MagicMock(side_effect=FileNotFoundError))

    buildx = _check_named(doctor_mod.run_checks(None), "Buildx")
    assert buildx.status is doctor_mod.Status.PASS

    monkeypatch.setattr(doctor_mod, "buildx_ok", lambda: False)
    buildx = _check_named(doctor_mod.run_checks(None), "Buildx")
    assert buildx.status is doctor_mod.Status.WARN
    assert "djinn build" in buildx.remedy


def test_run_checks_reports_seed_completeness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _quiet_doctor_probes(monkeypatch)
    monkeypatch.setattr(doctor_mod, "get_project_root", lambda: tmp_path)
    _write_seed_targets(tmp_path)

    checks = doctor_mod.run_checks(None)
    seed = _check_named(checks, "Seed config")
    assert seed.status is doctor_mod.Status.PASS
    assert seed.detail == "all seed targets present"
    assert seed.remedy == ""

    missing_entry = doctor_mod.SEED_MANIFEST[0]
    (tmp_path / missing_entry.target).unlink()
    checks = doctor_mod.run_checks(None)
    seed = _check_named(checks, "Seed config")
    assert seed.status is doctor_mod.Status.WARN
    assert missing_entry.target.as_posix() in seed.detail
    assert seed.remedy == "run `djinn init` (or `djinn doctor --fix`)."


def test_run_checks_warns_seed_row_outside_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_doctor_probes(monkeypatch)
    monkeypatch.setattr(doctor_mod, "get_project_root", MagicMock(side_effect=FileNotFoundError))

    checks = doctor_mod.run_checks(None)

    seed = _check_named(checks, "Seed config")
    assert seed.status is doctor_mod.Status.WARN
    assert "Djinn repo could not be located" in seed.detail
    assert "seed status unknown" in seed.detail
    assert seed.remedy == "Run from a clone of the Djinn repo."


def test_session_create_absent_workspace_creates_and_proceeds(tmp_path: Path) -> None:
    workspace = tmp_path / ".djinn" / "sessions" / "testproj"
    mock_result = SessionResult(returncode=0)

    with (
        patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
        patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
    ):
        mock_instance = mock_mgr.return_value
        mock_instance.resolve_target.return_value = SessionTarget()
        mock_instance.preflight_check.return_value = None
        mock_instance.run_headless.return_value = mock_result

        result = runner.invoke(
            app,
            ["session", "--project", "testproj", "--prompt", "hello", "--create"],
        )

        assert result.exit_code == 0, result.output
        assert workspace.is_dir()
        mock_mgr.assert_called_once_with("testproj")
        mock_instance.run_headless.assert_called_once()
        assert mock_instance.run_headless.call_args.kwargs["workspace_dir"] == workspace


def test_session_without_create_absent_workspace_errors(tmp_path: Path) -> None:
    with (
        patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
        patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
    ):
        mock_mgr.return_value.resolve_target.return_value = SessionTarget()
        result = runner.invoke(app, ["session", "--project", "missing", "--prompt", "hello"])

        assert result.exit_code == 1
        assert "Session workspace not found" in result.output
        mock_mgr.assert_called_once_with("missing")


def test_session_create_existing_file_errors_cleanly(tmp_path: Path) -> None:
    workspace = tmp_path / ".djinn" / "sessions" / "testproj"
    workspace.parent.mkdir(parents=True)
    workspace.write_text("not a directory")

    with (
        patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
        patch("djinn_in_a_box.commands.session.SessionManager") as mock_mgr,
    ):
        mock_mgr.return_value.resolve_target.return_value = SessionTarget()
        result = runner.invoke(
            app,
            ["session", "--project", "testproj", "--prompt", "hello", "--create"],
        )

        assert result.exit_code == 1
        assert "exists but is not a directory" in result.output
        assert ".djinn" in result.output
        assert "sessions" in result.output
        assert "Remove or rename" in result.output
        mock_mgr.assert_called_once_with("testproj")
        assert not mock_mgr.return_value.preflight_check.called


def test_session_interactive_failure_prints_stderr(tmp_path: Path) -> None:
    workspace = tmp_path / ".djinn" / "sessions" / "testproj"
    workspace.mkdir(parents=True)
    mock_result = SessionResult(returncode=127, stderr="Agent binary not found: claude\n")

    with (
        patch.object(session_mod.Path, "home", return_value=tmp_path),
        patch.object(session_mod.sys, "stdin", SimpleNamespace(isatty=lambda: True)),
        patch.object(session_mod, "SessionManager") as mock_mgr,
        patch.object(session_mod.err_console, "print") as mock_err_print,
    ):
        mock_instance = mock_mgr.return_value
        mock_instance.resolve_target.return_value = SessionTarget()
        mock_instance.preflight_check.return_value = None
        mock_instance.run_interactive.return_value = mock_result

        with pytest.raises(typer.Exit) as exc_info:
            session_mod.session(project="testproj")

        assert exc_info.value.exit_code == 127
        # Asserts the text reaches stderr, not how print_captured spells its kwargs.
        assert any(
            call.args and "Agent binary not found: claude" in str(call.args[0])
            for call in mock_err_print.call_args_list
        )
        mock_instance.run_interactive.assert_called_once()


def test_session_create_traversal_project_exits_without_creating_outside(
    tmp_path: Path,
) -> None:
    outside = tmp_path / ".djinn" / "outside"

    with (
        patch("djinn_in_a_box.commands.session.Path.home", return_value=tmp_path),
    ):
        result = runner.invoke(
            app,
            ["session", "--project", "../outside", "--prompt", "hello", "--create"],
        )

        assert result.exit_code == 1
        assert "Invalid project name" in result.output
        assert not outside.exists()


def _credential_root(tmp_path: Path) -> tuple[Path, AppConfig]:
    """A config root with one loose, one tight, and two decoys."""
    root = tmp_path / "config-root"
    root.mkdir()
    root.chmod(0o700)
    # chmod after mkdir: mkdir's mode is masked by the umask, so 0o777 would
    # silently become 0o755 and the decoy would not be the wide-open case.
    (root / "claude").mkdir()
    (root / "claude").chmod(0o755)
    (root / "gh").mkdir()
    (root / "gh").chmod(0o700)
    (root / "not-a-credential-dir").mkdir()
    (root / "not-a-credential-dir").chmod(0o777)
    return root, AppConfig(code_dir=tmp_path, config_root=root)


def test_loose_credential_dirs_finds_only_group_or_other_accessible(tmp_path: Path) -> None:
    """Would fail if the mode test dropped the 0o077 mask and flagged every dir."""
    root, config = _credential_root(tmp_path)

    loose = doctor_mod.loose_credential_dirs(config)

    assert [path.name for path in loose] == ["claude"]
    assert (root / "gh").stat().st_mode & 0o077 == 0


def test_loose_credential_dirs_leaves_a_stricter_mode_alone(tmp_path: Path) -> None:
    """The test is "group or other bits set", not "differs from 0700". A
    directory at 0500 is stricter than required; treating it as drift would
    *loosen* it. Would fail if the 0o077 mask became a `!= 0o700` comparison.
    """
    root, config = _credential_root(tmp_path)
    (root / "codex").mkdir()
    (root / "codex").chmod(0o500)

    loose = doctor_mod.loose_credential_dirs(config)

    assert "codex" not in [path.name for path in loose]


def test_loose_credential_dirs_skips_a_symlinked_name(tmp_path: Path) -> None:
    """lstat, not stat: a redirected credential name is someone's deliberate
    arrangement. Would fail if the check followed the link and chmod'd its target.
    """
    root, config = _credential_root(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    (root / "codex").symlink_to(target)

    loose = doctor_mod.loose_credential_dirs(config)

    assert "codex" not in [path.name for path in loose]
    assert target.stat().st_mode & 0o777 == 0o755


def test_loose_credential_dirs_ignores_files_and_unlisted_names(tmp_path: Path) -> None:
    """Only SYNC_PATHS["credentials"] names, and only directories."""
    root, config = _credential_root(tmp_path)
    (root / "unused-agent").write_text("a file, not a directory")
    (root / "codex").write_text("a listed file, not a directory")

    loose = doctor_mod.loose_credential_dirs(config)

    names = [path.name for path in loose]
    assert "unused-agent" not in names
    assert "codex" not in names
    assert "not-a-credential-dir" not in names


def test_doctor_warns_about_loose_credential_dirs_and_names_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, config = _credential_root(tmp_path)
    monkeypatch.setattr("djinn_in_a_box.config.loader.load_config", lambda: config)

    def fixed_root(_config: AppConfig | None = None) -> Path:
        return root

    monkeypatch.setattr(doctor_mod, "get_config_root", fixed_root)

    checks = doctor_mod.run_checks(config, None)

    row = next(check for check in checks if check.name == "Credential and zone dir modes")
    assert row.status is doctor_mod.Status.WARN
    assert "claude" in row.detail
    assert "--fix" in row.remedy


def test_doctor_fix_tightens_loose_credential_dirs_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The acceptance criterion in full: tighten, report, and change nothing on
    a second run -- while leaving an unlisted directory alone.
    """
    root, config = _credential_root(tmp_path)
    monkeypatch.setattr("djinn_in_a_box.config.loader.load_config", lambda: config)

    def fixed_root(_config: AppConfig | None = None) -> Path:
        return root

    monkeypatch.setattr(doctor_mod, "get_config_root", fixed_root)
    monkeypatch.setattr(doctor_mod, "run_checks", MagicMock(return_value=[]))
    monkeypatch.setattr(doctor_mod, "ensure_host_env", MagicMock())
    monkeypatch.setattr(doctor_mod, "seed_config", MagicMock(return_value=[]))
    monkeypatch.setattr(doctor_mod, "ensure_network", MagicMock(return_value=True))
    monkeypatch.setattr(doctor_mod, "get_project_root", lambda: tmp_path)

    first = runner.invoke(app, ["doctor", "--fix"])

    # Rich wraps to the terminal width, and a long tmp_path splits the directory
    # name across lines -- 80 columns in CI, wider locally. Compare against the
    # unwrapped text so the assertion does not depend on the environment.
    unwrapped = first.output.replace("\n", "")
    assert first.exit_code == 0, first.output
    assert "tightened" in unwrapped
    assert "claude" in unwrapped
    assert (root / "claude").stat().st_mode & 0o777 == 0o700
    assert (root / "not-a-credential-dir").stat().st_mode & 0o777 == 0o777

    second = runner.invoke(app, ["doctor", "--fix"])

    assert second.exit_code == 0, second.output
    assert "tightened" not in second.output.replace("\n", "")


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    (outside / "keep").write_bytes(b"outside content")
    (outside / "keep").chmod(0o640)
    return outside


def _outside_state(outside: Path) -> tuple[int, int, bytes]:
    return (
        outside.stat().st_mode,
        (outside / "keep").stat().st_mode,
        (outside / "keep").read_bytes(),
    )


@pytest.mark.parametrize(
    "kind", ["file", "dangling", "link-file", "link-directory", "project-link"]
)
def test_session_create_refuses_roots_and_existing_project_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    outside = _outside(tmp_path)
    base = tmp_path / ".djinn/sessions"
    base.parent.mkdir()
    if kind == "file":
        base.write_bytes(b"regular file")
    elif kind == "dangling":
        base.symlink_to(outside / "missing")
    elif kind == "link-file":
        base.symlink_to(outside / "keep")
    elif kind == "link-directory":
        (outside / "testproj").mkdir()
        base.symlink_to(outside)
    else:
        base.mkdir()
        (base / "real").mkdir()
        # An in-root link used to pass the resolve/relative_to check.
        (base / "testproj").symlink_to(base / "real")
    before = _outside_state(outside)
    monkeypatch.setattr(session_mod.Path, "home", lambda: tmp_path)
    manager = MagicMock()
    manager.resolve_target.return_value = SessionTarget()
    monkeypatch.setattr(session_mod, "SessionManager", _returns(manager))
    result = runner.invoke(
        app, ["session", "--project", "testproj", "--prompt", "hello", "--create"]
    )
    assert result.exit_code == 1, result.output
    refused = base / "testproj" if kind == "project-link" else base
    subject = "Session workspace" if kind == "project-link" else "Sessions root"
    reason = "is not a directory" if kind == "file" else "must not be a symlink"
    assert f"{subject} {reason}: {refused}" in result.output.replace("\n", "")
    remedy = f"Replace {refused} with a real directory, then retry."
    assert remedy in result.output.replace("\n", "")
    assert isinstance(result.exception, SystemExit)
    assert not manager.preflight_check.called
    assert not manager.run_headless.called
    assert not manager.run_interactive.called
    assert _outside_state(outside) == before
    assert sorted(p.name for p in outside.iterdir()) == (
        ["keep", "testproj"] if kind == "link-directory" else ["keep"]
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory read permissions")
@pytest.mark.parametrize("command", ["init", "doctor", "doctor-finding"])
@pytest.mark.parametrize("kind", ["link", "file"])
@pytest.mark.parametrize("timing", ["before-pin", "after-pin"])
def test_permission_repair_substitution_fails_cleanly_and_doctor_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, kind: str, timing: str,
) -> None:
    config = _repair_setup(tmp_path, monkeypatch)
    victim = docker.resolve_zone_roots(config).shared_root / "codex/sessions"
    victim.mkdir(parents=True)
    saved = victim.with_name("sessions-saved")
    outside = _outside(tmp_path)
    before = _outside_state(outside)
    network = MagicMock(return_value=True)
    monkeypatch.setattr(doctor_mod, "ensure_network", network)
    monkeypatch.setattr(doctor_mod, "loose_credential_dirs", _returns([]))
    monkeypatch.setattr(docker, "get_project_root", lambda: tmp_path / "project")
    if command == "doctor-finding":
        monkeypatch.setattr(doctor_mod, "ensure_host_env", _returns(None))
        monkeypatch.setattr(doctor_mod, "loose_credential_dirs", _returns([victim]))
    elif command == "init":
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config-files")
        monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config-files/config.toml")
        monkeypatch.setattr(config_mod, "AppConfig", _returns(config))
        monkeypatch.setattr(config_mod, "save_config", _returns(None))
        monkeypatch.setattr(config_mod, "get_project_root", lambda: tmp_path)
        monkeypatch.setattr(config_mod, "seed_config", _returns([]))
        answers = iter(["projects", str(config.code_dir), "UTC"])
        monkeypatch.setattr(config_mod.typer, "prompt", lambda *_args, **_kwargs: next(answers))
        monkeypatch.setattr(config_mod.typer, "confirm", _returns(False))
    victim.chmod(0o000)
    real_open = os.open
    denied = False
    swapped = False

    def swap() -> None:
        nonlocal swapped
        assert denied and not swapped
        swapped = True
        victim.rename(saved)
        if kind == "link":
            victim.symlink_to(outside)
        else:
            victim.write_bytes(b"substitute")
            victim.chmod(0o640)

    def open_entry(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal denied
        parent = kwargs.get("dir_fd")
        candidate = (
            Path(name) if parent is None
            else Path(os.readlink(f"/proc/self/fd/{parent}")) / name
        )
        is_pin = candidate == victim and flags & os.O_PATH
        if is_pin and timing == "before-pin":
            swap()
        try:
            descriptor = real_open(name, flags, *args, **kwargs)
        except PermissionError:
            if candidate == victim:
                denied = True
            raise
        if is_pin and timing == "after-pin":
            swap()
        return descriptor

    monkeypatch.setattr(contained_paths.os, "open", open_entry)
    fds = len(os.listdir("/proc/self/fd"))
    try:
        args = ["init"] if command == "init" else ["doctor", "--fix"]
        result = runner.invoke(app, args)
        assert denied and swapped
        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output
        output = result.output.replace("\n", "")
        assert str(victim) in output
        assert ("symlink" if kind == "link" else "not a directory") in output
        assert "writable" not in output
        if command != "init":
            network.assert_called_once_with()
            assert "Fixed: Docker network" in output
            assert "Could not fix:" in output
        if command == "doctor-finding":
            assert f"Could not fix: {victim}" in output
            assert "Fixed: tightened" not in output
        assert saved.stat().st_mode & 0o777 == (0o000 if timing == "before-pin" else 0o700)
        if kind == "file":
            assert victim.stat().st_mode & 0o777 == 0o640
            assert victim.read_bytes() == b"substitute"
        else:
            assert victim.is_symlink()
        assert _outside_state(outside) == before
        assert len(os.listdir("/proc/self/fd")) == fds
    finally:
        if saved.exists():
            saved.chmod(0o700)
        elif victim.is_dir() and not victim.is_symlink():
            victim.chmod(0o700)


def test_session_create_leaf_swap_before_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _outside(tmp_path)
    base = tmp_path / ".djinn/sessions"
    leaf = base / "testproj"
    leaf.mkdir(parents=True)
    before = _outside_state(outside)
    monkeypatch.setattr(session_mod.Path, "home", lambda: tmp_path)
    manager = MagicMock()
    manager.resolve_target.return_value = SessionTarget()
    monkeypatch.setattr(session_mod, "SessionManager", _returns(manager))
    real_open = os.open
    swapped = False

    def swap(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if name == "testproj" and "dir_fd" in kwargs and not swapped:
            swapped = True
            leaf.rename(base / "saved")
            leaf.symlink_to(outside)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(contained_paths.os, "open", swap)
    result = runner.invoke(
        app, ["session", "--project", "testproj", "--prompt", "hello", "--create"]
    )
    assert result.exit_code == 1
    assert swapped
    assert f"Session workspace must not be a symlink: {leaf}" in result.output.replace("\n", "")
    assert isinstance(result.exception, SystemExit)
    assert not manager.preflight_check.called
    assert not manager.run_headless.called
    assert _outside_state(outside) == before
    assert sorted(p.name for p in outside.iterdir()) == ["keep"]


@pytest.mark.parametrize("existing", ["none", "root", "workspace"])
def test_session_create_modes_and_root_creation_only_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str
) -> None:
    base = tmp_path / ".djinn/sessions"
    workspace = base / "testproj"
    if existing != "none":
        base.mkdir(parents=True)
    if existing == "workspace":
        workspace.mkdir()
        workspace.chmod(0o770)
    real_mkdir = Path.mkdir
    root_creates = []

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == base:
            root_creates.append(path)
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(session_mod.Path, "home", lambda: tmp_path)
    manager = MagicMock()
    manager.resolve_target.return_value = SessionTarget()
    manager.run_headless.return_value = SessionResult(returncode=0)
    monkeypatch.setattr(session_mod, "SessionManager", _returns(manager))
    previous_umask = os.umask(0o022)
    try:
        result = runner.invoke(
            app, ["session", "--project", "testproj", "--prompt", "hello", "--create"]
        )
    finally:
        os.umask(previous_umask)
    assert result.exit_code == 0, result.output
    assert bool(root_creates) is (existing == "none")
    assert stat.S_IMODE(workspace.stat().st_mode) == (0o770 if existing == "workspace" else 0o755)
    manager.run_headless.assert_called_once()


def test_session_create_missing_root_mkdir_failure_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _outside(tmp_path)
    before = _outside_state(outside)
    base = tmp_path / ".djinn/sessions"
    real_mkdir = Path.mkdir

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == base:
            raise PermissionError("test denied")
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(session_mod.Path, "home", lambda: tmp_path)
    manager = MagicMock()
    manager.resolve_target.return_value = SessionTarget()
    monkeypatch.setattr(session_mod, "SessionManager", _returns(manager))
    result = runner.invoke(app, ["session", "--project", "testproj", "--create"])
    assert result.exit_code == 1
    assert "Failed to create session workspace" in result.output
    assert isinstance(result.exception, SystemExit)
    assert not manager.preflight_check.called
    assert not manager.run_interactive.called
    assert _outside_state(outside) == before


def _repair_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    projects = tmp_path / "projects"
    projects.mkdir()
    config = AppConfig(code_dir=projects, config_root=tmp_path / "config")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    monkeypatch.setattr(doctor_mod, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(doctor_mod, "seed_config", _returns([]))
    monkeypatch.setattr(doctor_mod, "ensure_network", _returns(True))
    monkeypatch.setattr(doctor_mod, "run_checks", _returns([]))
    monkeypatch.setattr("djinn_in_a_box.config.loader.load_config", lambda: config)
    monkeypatch.setattr("djinn_in_a_box.config.zones.ZONES_FILE", tmp_path / "zones.toml")
    return config


@pytest.mark.parametrize("command", ["init", "doctor"])
def test_provisioning_refusal_routes_path_without_writability_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    config = _repair_setup(tmp_path, monkeypatch)
    victim = config.config_root / "claude/skills"
    victim.parent.mkdir(parents=True)
    outside = _outside(tmp_path)
    victim.symlink_to(outside)
    before = _outside_state(outside)
    monkeypatch.setattr(docker, "get_project_root", lambda: tmp_path / "project")
    if command == "init":
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config-files")
        monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config-files/config.toml")
        monkeypatch.setattr(config_mod, "AppConfig", _returns(config))
        monkeypatch.setattr(config_mod, "save_config", _returns(None))
        monkeypatch.setattr(config_mod, "get_project_root", lambda: tmp_path)
        monkeypatch.setattr(config_mod, "seed_config", _returns([]))
        answers = iter(["projects", str(config.code_dir), "UTC"])
        monkeypatch.setattr(config_mod.typer, "prompt", lambda *_args, **_kwargs: next(answers))
        monkeypatch.setattr(config_mod.typer, "confirm", _returns(False))
        args = ["init"]
    else:
        args = ["doctor", "--fix"]
    result = runner.invoke(app, args)
    assert result.exit_code == 1, result.output
    assert str(victim) in result.output.replace("\n", "")
    assert "symlink" in result.output
    assert "writable" not in result.output
    assert isinstance(result.exception, SystemExit)
    assert _outside_state(outside) == before
    assert sorted(p.name for p in outside.iterdir()) == ["keep"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file read permissions")
@pytest.mark.parametrize("command", ["init", "doctor"])
def test_unreadable_committed_seed_file_fails_cleanly_and_doctor_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    config = _repair_setup(tmp_path, monkeypatch)
    shutil.copytree(Path(__file__).parent.parent / "templates/seed", tmp_path / "templates/seed")
    target = tmp_path / "config/claude/AGENTS.md"
    provisioning = MagicMock()
    network = MagicMock(return_value=True)
    monkeypatch.setattr(doctor_mod, "seed_config", seed_config)
    monkeypatch.setattr(doctor_mod, "ensure_host_env", provisioning)
    monkeypatch.setattr(doctor_mod, "ensure_network", network)
    monkeypatch.setattr(doctor_mod, "loose_credential_dirs", _returns([]))
    real_replace = os.replace
    unreadable = False

    def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal unreadable
        real_replace(src, dst, *args, **kwargs)
        if dst == "AGENTS.md":
            parent = kwargs["dst_dir_fd"]
            assert os.readlink(f"/proc/self/fd/{parent}") == str(target.parent)
            os.chmod(dst, 0o000, dir_fd=parent, follow_symlinks=False)
            unreadable = True

    monkeypatch.setattr(contained_paths.os, "replace", replace)
    if command == "init":
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config-files")
        monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config-files/config.toml")
        monkeypatch.setattr(config_mod, "AppConfig", _returns(config))
        monkeypatch.setattr(config_mod, "save_config", _returns(None))
        monkeypatch.setattr(config_mod, "get_project_root", lambda: tmp_path)
        monkeypatch.setattr(config_mod, "seed_config", seed_config)
        monkeypatch.setattr(config_mod, "ensure_host_env", provisioning)
        answers = iter(["projects", str(config.code_dir), "UTC"])
        monkeypatch.setattr(config_mod.typer, "prompt", lambda *_args, **_kwargs: next(answers))
        monkeypatch.setattr(config_mod.typer, "confirm", _returns(False))
        args = ["init"]
    else:
        args = ["doctor", "--fix"]
    fds = len(os.listdir("/proc/self/fd"))
    try:
        result = runner.invoke(app, args)
        assert unreadable
        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output
        assert str(target) in result.output.replace("\n", "")
        assert "Cannot install baseline seed target" in result.output
        if command == "doctor":
            provisioning.assert_called_once_with(config)
            network.assert_called_once_with()
            assert "Fixed: host environment" in result.output
            assert "Fixed: Docker network" in result.output
        assert len(os.listdir("/proc/self/fd")) == fds
    finally:
        if unreadable:
            target.chmod(0o600)


@pytest.mark.parametrize("zone", ["config_root", "shared_root", "local_root"])
@pytest.mark.parametrize("kind", ["symlink", "file", "missing"])
def test_doctor_repair_finding_swapped_before_open_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, zone: str, kind: str
) -> None:
    config = _repair_setup(tmp_path, monkeypatch)
    roots = docker.resolve_zone_roots(config)
    root = getattr(roots, zone)
    victim = root / "codex/sessions"
    victim.mkdir(parents=True)
    outside = _outside(tmp_path)
    before = _outside_state(outside)
    monkeypatch.setattr(doctor_mod, "ensure_host_env", _returns(None))
    monkeypatch.setattr(doctor_mod, "loose_credential_dirs", _returns([victim]))
    real_open = os.open
    swapped = False

    def swap(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if not swapped and name == "sessions" and "dir_fd" in kwargs:
            swapped = True
            victim.rmdir()
            if kind == "symlink":
                victim.symlink_to(outside)
            elif kind == "file":
                victim.write_bytes(b"wrong type")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(contained_paths.os, "open", swap)
    result = runner.invoke(app, ["doctor", "--fix"])
    assert result.exit_code == 1, result.output
    assert swapped
    unwrapped = result.output.replace("\n", "")
    assert f"Could not fix: {victim}" in unwrapped
    assert "Fixed: tightened" not in unwrapped
    assert isinstance(result.exception, SystemExit)
    assert _outside_state(outside) == before


@pytest.mark.parametrize("command", ["init", "doctor"])
@pytest.mark.parametrize("position", ["zone", "credential"])
def test_provisioning_fchmod_failure_reports_full_path_and_writability_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, position: str,
) -> None:
    config = _repair_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(docker, "get_project_root", lambda: tmp_path / "project")
    victim = (Path(f"{config.config_root}.local") / "claude" if position == "zone"
              else config.config_root / "codex")
    real_fchmod = os.fchmod
    failure = PermissionError(errno.EPERM, "Operation not permitted")
    denied = False

    def fchmod(descriptor: int, *args: Any, **kwargs: Any) -> None:
        nonlocal denied
        if os.readlink(f"/proc/self/fd/{descriptor}") == str(victim):
            denied = True
            raise failure
        real_fchmod(descriptor, *args, **kwargs)

    monkeypatch.setattr(contained_paths.os, "fchmod", fchmod)
    if command == "init":
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config-files")
        monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config-files/config.toml")
        monkeypatch.setattr(config_mod, "AppConfig", _returns(config))
        monkeypatch.setattr(config_mod, "save_config", _returns(None))
        monkeypatch.setattr(config_mod, "get_project_root", lambda: tmp_path)
        monkeypatch.setattr(config_mod, "seed_config", _returns([]))
        answers = iter(["projects", str(config.code_dir), "UTC"])
        monkeypatch.setattr(config_mod.typer, "prompt", lambda *_args, **_kwargs: next(answers))
        monkeypatch.setattr(config_mod.typer, "confirm", _returns(False))
        args = ["init"]
    else:
        monkeypatch.setattr(doctor_mod, "loose_credential_dirs", _returns([]))
        args = ["doctor", "--fix"]
    fds = len(os.listdir("/proc/self/fd"))
    result = runner.invoke(app, args)
    assert denied
    assert result.exit_code == 1, result.output
    assert str(victim) in result.output.replace("\n", "")
    assert "writable" in result.output
    assert "Operation not permitted" in " ".join(result.output.split())
    assert len(os.listdir("/proc/self/fd")) == fds


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory read permissions")
def test_session_unreadable_workspace_error_names_full_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / ".djinn/sessions/testproj"
    workspace.mkdir(parents=True)
    workspace.chmod(0o000)
    monkeypatch.setattr(session_mod.Path, "home", lambda: tmp_path)
    manager = MagicMock()
    manager.resolve_target.return_value = SessionTarget()
    monkeypatch.setattr(session_mod, "SessionManager", _returns(manager))
    try:
        result = runner.invoke(
            app, ["session", "--project", "testproj", "--prompt", "hello", "--create"]
        )
        assert result.exit_code == 1, result.output
        assert "Failed to create session workspace" in result.output
        assert str(workspace) in result.output.replace("\n", "")
        manager.preflight_check.assert_not_called()
    finally:
        workspace.chmod(0o700)
