from __future__ import annotations

from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import djinn_in_a_box.core.config_sync as sync_module
from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import doctor as doctor_module
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.config_sync import (
    ConfigSyncAudit,
    ConfigSyncResult,
    DriftClass,
    DriftItem,
    SyncProblem,
)
from djinn_in_a_box.core.config_sync_agent import SemanticFailure

runner = CliRunner()
_SENTINEL = "PRIVATE-WORKFLOW-BODY-SENTINEL"


def _audit(*, clean: bool) -> ConfigSyncAudit:
    if clean:
        return ConfigSyncAudit("claude", "claude")
    return ConfigSyncAudit(
        "claude",
        "claude",
        (
            DriftItem(
                DriftClass.SEMANTIC_REQUIRED,
                _SENTINEL,
                "codex",
                PurePosixPath("agents/reviewer.toml"),
            ),
        ),
        (
            SyncProblem(
                "agent:reviewer",
                _SENTINEL,
                "codex",
                PurePosixPath("agents/reviewer.toml"),
            ),
        ),
    )


def _no_legacy_root(_config: AppConfig | None) -> bool:
    return False


def _no_mount_args() -> list[str]:
    return []


def _mock_command_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "config-root")
    monkeypatch.setattr("djinn_in_a_box.commands.config.load_config", lambda: config)


def test_config_status_is_read_only_and_never_discloses_source_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    source_root = project / "config" / "claude"
    source_root.mkdir(parents=True)
    (source_root / "CLAUDE.md").write_text(_SENTINEL)
    config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "config-root")
    before = sorted(path.relative_to(project) for path in project.rglob("*"))

    def load_test_config(_path: Path | None = None) -> AppConfig:
        return config

    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: project)
    monkeypatch.setattr(sync_module, "load_config", load_test_config)

    result = runner.invoke(app, ["config", "status"])

    assert result.exit_code == 0, result.output
    assert "Source: claude" in result.output
    assert "source-only" in result.output
    assert _SENTINEL not in result.output
    assert sorted(path.relative_to(project) for path in project.rglob("*")) == before
    assert not (project / "config" / sync_module.MANIFEST_NAME).exists()


def test_config_status_prints_sanitized_identifiers_not_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = MagicMock(return_value=_audit(clean=False))
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.audit_workflow_config", audit)

    result = runner.invoke(app, ["config", "status"])

    assert result.exit_code == 0
    assert "semantic-agent-required" in result.output
    assert "agent:reviewer" in result.output
    assert "djinn config sync" in result.output
    assert "Semantic failure:" not in result.output
    assert _SENTINEL not in result.output
    audit.assert_called_once_with(tmp_path)


def test_config_status_prints_quarantine_preservation_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = ConfigSyncAudit(
        "claude",
        "claude",
        (DriftItem(DriftClass.SOURCE_CHANGED, "private"),),
        (SyncProblem("quarantine-preserved", "private"),),
    )
    def _fake_audit(_root: Path) -> ConfigSyncAudit:
        return audit

    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.audit_workflow_config", _fake_audit)

    result = runner.invoke(app, ["config", "status"])

    assert result.exit_code == 0
    assert ".djinn-*-stage-*" in result.output
    assert "before deleting anything or retrying" in result.output
    assert "Retry after the source stops changing" not in result.output
    assert "private" not in result.output


def test_config_sync_requests_explicit_agent_fallback_and_reports_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock(return_value=ConfigSyncResult(True, _audit(clean=True), (), ()))
    _mock_command_config(monkeypatch, tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.synchronize_workflow_config", service)

    result = runner.invoke(app, ["config", "sync"])
    normalized = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "Configuration synchronized" in result.output
    assert "0 changed, 0 removed" in result.output
    assert "sends each unresolved workflow artifact" in normalized
    assert "selected claude provider" in normalized
    assert "read-only-agent mounts and network access" in normalized
    assert "120s per item, 300s total" in normalized
    assert "workflow bodies and provider output are not printed" in normalized
    assert _SENTINEL not in result.output
    service.assert_called_once_with(tmp_path, allow_agent=True)


def test_config_sync_blocked_is_exit_one_and_does_not_disclose_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock(return_value=ConfigSyncResult(False, _audit(clean=False)))
    _mock_command_config(monkeypatch, tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.synchronize_workflow_config", service)

    result = runner.invoke(app, ["config", "sync"])

    assert result.exit_code == 1
    assert "Configuration synchronization is blocked" in result.output
    assert "semantic-agent-required" in result.output
    assert _SENTINEL not in result.output
    service.assert_called_once_with(tmp_path, allow_agent=True)


def test_config_sync_reports_allowlisted_semantic_timeout_and_concrete_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = SemanticFailure.TIMED_OUT
    audit = ConfigSyncAudit(
        "claude",
        "claude",
        (
            DriftItem(
                DriftClass.SEMANTIC_REQUIRED,
                _SENTINEL,
                "codex",
                PurePosixPath("skills/convergence-loop/SKILL.md"),
            ),
        ),
        (
            SyncProblem(
                "skill:convergence-loop:skills/convergence-loop/SKILL.md",
                f"Semantic adaptation required ({failure.value}).",
                "codex",
                PurePosixPath("skills/convergence-loop/SKILL.md"),
            ),
        ),
    )
    service = MagicMock(return_value=ConfigSyncResult(False, audit))
    _mock_command_config(monkeypatch, tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.synchronize_workflow_config", service)

    result = runner.invoke(app, ["config", "sync"])

    assert result.exit_code == 1
    assert "Semantic failure: timed-out" in result.output
    assert "semantic" in result.output
    assert "adaptation timed out" in result.output
    assert "Run `djinn config sync` to perform explicit adaptation" not in result.output
    assert _SENTINEL not in result.output


def test_config_sync_sanitizes_service_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock(side_effect=ValueError(_SENTINEL))
    _mock_command_config(monkeypatch, tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: tmp_path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.synchronize_workflow_config", service)

    result = runner.invoke(app, ["config", "sync"])

    assert result.exit_code == 1
    assert "ValueError" in result.output
    assert _SENTINEL not in result.output
    assert "Traceback" not in result.output


def test_doctor_config_workflow_check_is_read_only_and_never_syncs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code_dir = tmp_path / "projects"
    config_root = tmp_path / "config-root"
    code_dir.mkdir()
    config_root.mkdir()
    config = AppConfig(code_dir=code_dir, config_root=config_root)
    audit = MagicMock(return_value=_audit(clean=False))
    sync = MagicMock()
    monkeypatch.setattr(doctor_module, "audit_workflow_config", audit)
    monkeypatch.setattr(sync_module, "sync_config", sync)
    monkeypatch.setattr(doctor_module, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(doctor_module, "_docker_installed", lambda: False)
    monkeypatch.setattr(doctor_module, "_docker_socket_ok", lambda: True)
    monkeypatch.setattr(doctor_module, "_old_sync_root_present", _no_legacy_root)
    monkeypatch.setattr(doctor_module, "get_dbus_mount_args", _no_mount_args)

    checks = doctor_module.run_checks(config)

    workflow = next(check for check in checks if check.name == "Config workflow")
    assert workflow.status is doctor_module.Status.WARN
    assert workflow.detail == "source=claude; drift=semantic-agent-required"
    assert _SENTINEL not in workflow.detail
    audit.assert_called_once_with(tmp_path)
    sync.assert_not_called()


def test_doctor_config_workflow_clean_is_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(code_dir=tmp_path, config_root=tmp_path)

    def clean_audit(_root: Path) -> ConfigSyncAudit:
        return _audit(clean=True)

    monkeypatch.setattr(doctor_module, "audit_workflow_config", clean_audit)
    monkeypatch.setattr(doctor_module, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(doctor_module, "_docker_installed", lambda: False)
    monkeypatch.setattr(doctor_module, "_docker_socket_ok", lambda: True)
    monkeypatch.setattr(doctor_module, "_old_sync_root_present", _no_legacy_root)
    monkeypatch.setattr(doctor_module, "get_dbus_mount_args", _no_mount_args)

    checks = doctor_module.run_checks(config)

    workflow = next(check for check in checks if check.name == "Config workflow")
    assert workflow.status is doctor_module.Status.PASS
    assert workflow.detail == "source=claude; clean"
