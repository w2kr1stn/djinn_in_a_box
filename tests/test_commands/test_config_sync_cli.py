from __future__ import annotations

from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import doctor as doctor_module
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig
from djinn_in_a_box.core.config_sync import (
    CANONICAL_REMEDY,
    LOCK_REMEDY,
    ConfigSyncAudit,
    ConfigSyncResult,
    DriftClass,
    DriftItem,
    SyncProblem,
)

runner = CliRunner()
_SENTINEL = "PRIVATE-WORKFLOW-BODY-SENTINEL"


def _no_legacy_root(_config: AppConfig | None) -> bool:
    return False


def _no_mount_args() -> list[str]:
    return []


def _audit(kind: DriftClass | None = None) -> ConfigSyncAudit:
    if kind is None:
        return ConfigSyncAudit("claude", "claude")
    return ConfigSyncAudit(
        "claude",
        "claude",
        (DriftItem(kind, _SENTINEL, "codex", PurePosixPath("agents/reviewer.toml")),),
    )


@pytest.mark.parametrize(
    ("kind", "expected_exit", "remedy"),
    [
        (DriftClass.CLEAN, 0, None),
        (
            DriftClass.SOURCE_CHANGED,
            1,
            "Run `djinn config sync` or retry after source changes settle.",
        ),
        (DriftClass.TARGET_DRIFT, 1, "Revert or adopt the managed change via the sync flow."),
        (DriftClass.COLLISION, 1, "Move or remove the conflicting unmanaged file."),
        (DriftClass.INVALID_OR_SEMANTIC, 1, CANONICAL_REMEDY),
    ],
)
def test_config_status_has_five_class_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: DriftClass,
    expected_exit: int,
    remedy: str | None,
) -> None:
    audit = _audit(None if kind is DriftClass.CLEAN else kind)
    service = MagicMock(return_value=audit)
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.audit_workflow_config", service)

    result = runner.invoke(app, ["config", "status"])

    assert result.exit_code == expected_exit, result.output
    assert "Source: claude" in result.output
    assert _SENTINEL not in result.output
    assert result.output.count("Remedy:") == (0 if remedy is None else 1)
    if remedy is not None:
        assert f"Remedy: {remedy}" in " ".join(result.output.split())
    service.assert_called_once_with(tmp_path)


def test_config_sync_never_requests_provider_adaptation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock(return_value=ConfigSyncResult(True, _audit(), (), ()))
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.synchronize_workflow_config", service)

    result = runner.invoke(app, ["config", "sync"])

    assert result.exit_code == 0, result.output
    assert "provider" not in result.output.lower()
    assert "0 changed, 0 removed" in result.output
    service.assert_called_once_with(tmp_path)


def test_config_sync_reports_canonical_nonportable_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock(
        return_value=ConfigSyncResult(False, _audit(DriftClass.INVALID_OR_SEMANTIC))
    )
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.synchronize_workflow_config", service)

    result = runner.invoke(app, ["config", "sync"])

    assert result.exit_code == 1
    assert result.output.count("Remedy:") == 1
    assert CANONICAL_REMEDY in " ".join(result.output.split())
    assert _SENTINEL not in result.output


def test_config_status_reports_lock_failure_without_portability_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = tmp_path / "project" / "config"
    audit = ConfigSyncAudit(
        "claude",
        None,
        problems=(
            SyncProblem(
                "canonical-lock-failed",
                f"Canonical workflow lock failed at {config_root}: No locks available",
                remedy=LOCK_REMEDY,
            ),
        ),
    )
    service = MagicMock(return_value=audit)
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.audit_workflow_config", service)

    result = runner.invoke(app, ["config", "status"])

    assert result.exit_code == 1, result.output
    assert "canonical-lock-failed" in result.output
    assert str(config_root) in result.output
    normalized = " ".join(result.output.split())
    assert "No locks available" in normalized
    assert LOCK_REMEDY in normalized
    assert CANONICAL_REMEDY not in normalized
    assert _SENTINEL not in normalized


def test_config_sync_reports_lock_failure_without_portability_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = tmp_path / "project" / "config"
    audit = ConfigSyncAudit(
        "claude",
        None,
        problems=(
            SyncProblem(
                "canonical-lock-failed",
                f"Canonical workflow lock failed at {config_root}: Interrupted system call",
                remedy=LOCK_REMEDY,
            ),
        ),
    )
    service = MagicMock(return_value=ConfigSyncResult(False, audit))
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.synchronize_workflow_config", service)

    result = runner.invoke(app, ["config", "sync"])

    assert result.exit_code == 1, result.output
    assert "canonical-lock-failed" in result.output
    assert str(config_root) in result.output
    normalized = " ".join(result.output.split())
    assert "Interrupted system call" in normalized
    assert LOCK_REMEDY in normalized
    assert CANONICAL_REMEDY not in normalized
    assert "Traceback" not in normalized


def test_config_status_missing_canonical_root_is_content_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(
        code_dir=tmp_path,
        config_root=tmp_path / "runtime",
        config_sync=ConfigSyncConfig(source="claude"),
    )

    def load_test_config(_path: Path | None = None) -> AppConfig:
        return config

    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.core.config_sync.load_config", load_test_config)

    result = runner.invoke(app, ["config", "status"])

    assert result.exit_code == 1
    normalized = " ".join(result.output.split())
    assert "Traceback" not in normalized
    assert "canonical-lock-failed" in normalized
    assert str(tmp_path / "config") in normalized
    assert "No such file or directory" in normalized
    assert LOCK_REMEDY in normalized
    assert CANONICAL_REMEDY not in normalized


def test_config_sync_requires_a_clean_reaudit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock(return_value=ConfigSyncResult(True, _audit(DriftClass.SOURCE_CHANGED)))
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.synchronize_workflow_config", service)

    result = runner.invoke(app, ["config", "sync"])

    assert result.exit_code == 1
    assert "Configuration synchronization is blocked." in result.output
    assert "Configuration synchronized" not in result.output


def test_doctor_audits_once_without_sync_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "runtime")
    audit = MagicMock(return_value=_audit(DriftClass.INVALID_OR_SEMANTIC))
    sync = MagicMock()
    monkeypatch.setattr(doctor_module, "audit_workflow_config", audit)
    monkeypatch.setattr("djinn_in_a_box.core.config_sync.sync_config", sync)
    monkeypatch.setattr(doctor_module, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(doctor_module, "_docker_installed", lambda: False)
    monkeypatch.setattr(doctor_module, "_docker_socket_ok", lambda: True)
    monkeypatch.setattr(doctor_module, "_old_sync_root_present", _no_legacy_root)
    monkeypatch.setattr(doctor_module, "get_dbus_mount_args", _no_mount_args)

    checks = doctor_module.run_checks(config)

    workflow = next(check for check in checks if check.name == "Config workflow")
    assert workflow.status is doctor_module.Status.WARN
    assert workflow.detail == "source=claude; drift=invalid-or-semantic"
    assert _SENTINEL not in workflow.detail
    audit.assert_called_once_with(tmp_path)
    sync.assert_not_called()


def test_removed_semantic_module_has_no_consumer() -> None:
    root = Path(__file__).parents[2]
    removed_module = "config_sync" + "_agent"
    removed_symbol = "Semantic" + "Failure"
    hits = [
        path
        for path in root.rglob("*.py")
        if removed_module in path.read_text() or removed_symbol in path.read_text()
    ]

    assert hits == []
