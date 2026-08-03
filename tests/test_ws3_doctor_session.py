"""WS-3 doctor --fix, doctor rows, and session --create acceptance tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import doctor as doctor_mod
from djinn_in_a_box.commands import session as session_mod
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.config_workflow import WorkflowPreparationResult
from djinn_in_a_box.core.exceptions import ConfigNotFoundError, ConfigValidationError
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

    def old_sync_root_present(_config: AppConfig | None) -> bool:
        return False

    monkeypatch.setattr(doctor_mod, "_docker_installed", lambda: True)
    monkeypatch.setattr(doctor_mod, "docker_daemon_ok", lambda: True)
    monkeypatch.setattr(doctor_mod, "_docker_socket_ok", lambda: True)
    monkeypatch.setattr(doctor_mod, "compose_v2_ok", lambda: True)
    monkeypatch.setattr(doctor_mod, "_image_built", lambda: True)
    monkeypatch.setattr(doctor_mod, "network_exists", network_exists)
    monkeypatch.setattr(doctor_mod, "_docker_mcp_ok", lambda: True)
    monkeypatch.setattr(doctor_mod, "_old_sync_root_present", old_sync_root_present)


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


def test_run_checks_reports_dbus_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_doctor_probes(monkeypatch)
    monkeypatch.setattr(doctor_mod, "get_project_root", MagicMock(side_effect=FileNotFoundError))

    monkeypatch.setattr(doctor_mod, "get_dbus_mount_args", lambda: ["-v", "/run/user/1000/bus"])
    checks = doctor_mod.run_checks(None)
    dbus = _check_named(checks, "D-Bus session")
    assert dbus.status is doctor_mod.Status.PASS
    assert dbus.detail == "desktop notifications available"

    monkeypatch.setattr(doctor_mod, "get_dbus_mount_args", _no_dbus_mount_args)
    checks = doctor_mod.run_checks(None)
    dbus = _check_named(checks, "D-Bus session")
    assert dbus.status is doctor_mod.Status.PASS
    assert dbus.detail == "not detected — desktop notifications off"
    assert dbus.remedy == ""


def test_run_checks_reports_seed_completeness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _quiet_doctor_probes(monkeypatch)
    monkeypatch.setattr(doctor_mod, "get_dbus_mount_args", _no_dbus_mount_args)
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
    monkeypatch.setattr(doctor_mod, "get_dbus_mount_args", _no_dbus_mount_args)
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
        mock_err_print.assert_any_call("Agent binary not found: claude\n", end="")
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
    (root / "gemini").write_text("a file, not a directory")

    loose = doctor_mod.loose_credential_dirs(config)

    names = [path.name for path in loose]
    assert "gemini" not in names
    assert "not-a-credential-dir" not in names


def test_doctor_warns_about_loose_credential_dirs_and_names_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, config = _credential_root(tmp_path)
    monkeypatch.setattr("djinn_in_a_box.config.loader.load_config", lambda: config)
    monkeypatch.setattr(doctor_mod, "get_config_root", lambda _config=None: root)

    checks = doctor_mod.run_checks(config, None)

    row = next(check for check in checks if check.name == "Credential dir modes")
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
    monkeypatch.setattr(doctor_mod, "get_config_root", lambda _config=None: root)
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
