from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import NoReturn

import pytest

import djinn_in_a_box.core.config_workflow as workflow_module
from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig, ConfigSyncSource
from djinn_in_a_box.core import workflow_publisher
from djinn_in_a_box.core.config_lock import ConfigDirectoryLockError
from djinn_in_a_box.core.config_sync import (
    CANONICAL_REMEDY,
    LOCK_REMEDY,
    CanonicalDeliveryViewResult,
    ConfigSyncAudit,
    DriftClass,
    audit_config_sync,
)
from djinn_in_a_box.core.config_workflow import WorkflowDeliveryTarget, prepare_config_workflow
from djinn_in_a_box.core.docker import WorkflowImageCompatibility
from djinn_in_a_box.core.workflow_publisher import (
    RUNTIME_MANIFEST_NAME,
    CanonicalLockLease,
    CarrierFragment,
    WorkflowView,
)

_DEFAULT_HOST_WORKFLOW_TRUST_FILE = workflow_module.HOST_WORKFLOW_TRUST_FILE


def _workspace(tmp_path: Path, source: ConfigSyncSource = "claude") -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
    (project / "config" / source / "AGENTS.md").write_text("shared workflow\n")
    config_path = tmp_path / "djinn.toml"
    runtime = tmp_path / "runtime"
    save_config(
        AppConfig(
            code_dir=tmp_path,
            config_root=runtime,
            config_sync=ConfigSyncConfig(source=source),
        ),
        config_path,
    )
    return project, config_path, runtime


def _ensure_host_env(_config: AppConfig) -> None:
    return None


_IMAGE_GATE_FAILURES = {
    WorkflowImageCompatibility.UNKNOWN: (
        "image-unreachable",
        "Docker daemon/container not reachable.",
        "Retry.",
    ),
    WorkflowImageCompatibility.MISSING: (
        "image-not-built",
        "Workflow image is not built.",
        "Run `djinn build`, then retry.",
    ),
    WorkflowImageCompatibility.INCOMPATIBLE: (
        "image-incompatible",
        "Workflow image is incompatible.",
        "Rebuild/recreate required.",
    ),
}


def _audit_must_not_run(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("audit")


@pytest.mark.parametrize("tool", ("claude", "codex", "opencode"))
def test_host_runtime_publisher_syncs_selected_view_and_state_manifest(
    tmp_path: Path, tool: ConfigSyncSource
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    host_root = tmp_path / f"host-{tool}"
    targets = (WorkflowDeliveryTarget(tool, host_root, provision=True),)

    result = prepare_config_workflow(
        project, targets, config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert result.success, result.problems
    assert (host_root / "AGENTS.md").read_bytes() == b"shared workflow\n"
    manifest_path = host_root / RUNTIME_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    files = {item["path"]: item for item in manifest["items"] if "key_path" not in item}
    expected = {"AGENTS.md": b"shared workflow\n"}
    if tool == "claude":
        expected["CLAUDE.md"] = b"@AGENTS.md\n"
    assert set(files) == set(expected)
    for name, content in expected.items():
        assert (host_root / name).read_bytes() == content
        assert files[name]["content_hash"] == hashlib.sha256(content).hexdigest()
        assert files[name]["executable"] is False
    before = manifest_path.read_bytes()
    assert prepare_config_workflow(
        project, targets, config_path=config_path, confirm_host_workflow=lambda review: True
    ).success
    assert manifest_path.read_bytes() == before


def test_preflight_reports_one_class_and_remedy_without_workflow_body(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    assert prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    ).success
    sentinel = "PRIVATE-WORKFLOW-BODY"
    (project / "config/codex/AGENTS.md").write_text(sentinel)

    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == DriftClass.TARGET_DRIFT.value
    assert problem.remedy
    assert sentinel not in repr(result)


def test_preflight_does_not_repair_a_missing_managed_canonical_file(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    assert prepare_config_workflow(
        project, config_path=config_path, confirm_host_workflow=lambda review: True
    ).success
    managed = project / "config/codex/AGENTS.md"
    managed.unlink()

    result = prepare_config_workflow(
        project, config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    assert result.problems[0].identifier == DriftClass.TARGET_DRIFT.value
    assert not managed.exists()


def test_target_symlink_reports_the_destination_not_workflow_drift(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    managed_by_dotfiles = tmp_path / "dotfiles-claude"
    managed_by_dotfiles.mkdir()
    target = WorkflowDeliveryTarget("claude", tmp_path / ".claude", provision=True)
    target.destination_root.symlink_to(managed_by_dotfiles, target_is_directory=True)

    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "target-symlink"
    assert str(target.destination_root) in problem.message
    assert "symlink" in problem.message
    assert "portable" not in problem.remedy


def test_target_file_reports_not_a_directory_not_workflow_drift(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    target.destination_root.write_text("not a directory")

    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "target-not-directory"
    assert str(target.destination_root) in problem.message
    assert "not a directory" in problem.message
    assert "portable" not in problem.remedy


def test_target_provisioning_os_error_reports_the_path_not_workflow_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "unwritable" / "codex", provision=True)
    original_mkdir = Path.mkdir

    def refuse_target_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path == target.destination_root:
            raise PermissionError(13, "Permission denied", path)
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", refuse_target_mkdir)

    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "target-provisioning-failed"
    assert str(target.destination_root) in problem.message
    assert "Permission denied" in problem.message
    assert "portable" not in problem.remedy


def test_missing_target_reports_the_destination_not_workflow_drift(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "missing-codex")

    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "target-missing"
    assert str(target.destination_root) in problem.message
    assert "does not exist" in problem.message
    assert "portable" not in problem.remedy


def test_non_traversable_target_parent_reports_preparation_failure(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    parent = tmp_path / "blocked-parent"
    target = WorkflowDeliveryTarget("codex", parent / "existing-target")
    target.destination_root.mkdir(parents=True)
    original_mode = stat.S_IMODE(parent.stat().st_mode)
    parent.chmod(0o000)
    try:
        result = prepare_config_workflow(
            project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
        )
    finally:
        parent.chmod(original_mode)

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "target-provisioning-failed"
    assert str(target.destination_root) in problem.message
    assert "Permission denied" in problem.message
    assert "portable" not in problem.remedy


def test_target_lock_failure_is_not_reported_as_a_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lease failure must not borrow the publish wording.

    Nothing was published, so "Failed to publish ... check the directory is
    writable with available space" names the wrong operation and offers a remedy
    that does not apply to a missing lock. Mock-based: ENOLCK cannot be provoked
    reliably on an ordinary filesystem.
    """
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    assert prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    ).success

    real_flock = fcntl.flock
    seen = {"count": 0}

    def fail_target_acquisition(descriptor: int, operation: int) -> None:
        seen["count"] += 1
        # The canonical shared lock comes first; fail only the later exclusive
        # acquisition, which is the target lock.
        if seen["count"] > 2 and operation == fcntl.LOCK_EX:
            raise OSError(37, "No locks available")
        real_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", fail_target_acquisition)

    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "workflow-lock-failed"
    assert "No locks available" in problem.message
    assert "lock" in problem.message
    assert "publish" not in problem.message
    assert "writable" not in problem.remedy


def test_publish_write_error_reports_the_path_not_workflow_drift(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    target.destination_root.mkdir()
    original_mode = stat.S_IMODE(target.destination_root.stat().st_mode)
    target.destination_root.chmod(0o555)
    try:
        result = prepare_config_workflow(
            project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
        )
    finally:
        target.destination_root.chmod(original_mode)

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "workflow-publish-failed"
    assert str(target.destination_root) in problem.message
    assert "Permission denied" in problem.message
    assert "portable" not in problem.remedy


def test_target_lock_acquisition_reports_a_lock_failure_not_a_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    original_open = workflow_publisher._open_directory
    original_flock = workflow_publisher.fcntl.flock
    descriptor: int | None = None

    def record_target_descriptor(path: Path) -> int:
        nonlocal descriptor
        candidate = original_open(path)
        if path == target.destination_root:
            descriptor = candidate
        return candidate

    def fail_target_acquisition(candidate: int, operation: int) -> None:
        if candidate == descriptor and operation == workflow_publisher.fcntl.LOCK_EX:
            raise OSError(errno.ENOLCK, "No locks available")
        original_flock(candidate, operation)

    monkeypatch.setattr(workflow_publisher, "_open_directory", record_target_descriptor)
    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_target_acquisition)

    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "workflow-lock-failed"
    assert str(target.destination_root) in problem.message
    assert "No locks available" in problem.message
    assert "portable" not in problem.remedy
    assert "writable" not in problem.remedy


def test_target_lock_release_reports_a_lock_failure_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    original_open = workflow_publisher._open_directory
    original_flock = workflow_publisher.fcntl.flock
    descriptor: int | None = None

    def record_target_descriptor(path: Path) -> int:
        nonlocal descriptor
        candidate = original_open(path)
        if path == target.destination_root:
            descriptor = candidate
        return candidate

    def fail_target_unlock(candidate: int, operation: int) -> None:
        if candidate == descriptor and operation == workflow_publisher.fcntl.LOCK_UN:
            raise OSError(errno.EINTR, "Interrupted system call")
        original_flock(candidate, operation)

    monkeypatch.setattr(workflow_publisher, "_open_directory", record_target_descriptor)
    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_target_unlock)

    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert descriptor is not None
    with pytest.raises(OSError) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF
    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "workflow-lock-failed"
    assert str(target.destination_root) in problem.message
    assert "Interrupted system call" in problem.message
    assert "portable" not in problem.remedy
    assert "writable" not in problem.remedy


def test_unreadable_canonical_root_reports_lock_failure_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    assert prepare_config_workflow(
        project, config_path=config_path, confirm_host_workflow=lambda review: True
    ).success
    canonical_root = project / "config"
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    original_audit = workflow_module.audit_config_sync
    original_mode = stat.S_IMODE(canonical_root.stat().st_mode)

    def make_canonical_root_unreadable_after_audit(
        project_root: Path, *, config_path: Path | None = None
    ) -> ConfigSyncAudit:
        audit = original_audit(project_root, config_path=config_path)
        canonical_root.chmod(0o000)
        return audit

    monkeypatch.setattr(
        workflow_module, "audit_config_sync", make_canonical_root_unreadable_after_audit
    )
    try:
        result = prepare_config_workflow(
            project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
        )
    finally:
        canonical_root.chmod(original_mode)

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "canonical-lock-failed"
    assert str(canonical_root) in problem.message
    assert "Permission denied" in problem.message
    assert "Traceback" not in repr(result)
    assert "portable" not in problem.remedy
    assert "writable" not in problem.remedy


def test_flock_acquisition_failure_reports_canonical_lock_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    assert prepare_config_workflow(
        project, config_path=config_path, confirm_host_workflow=lambda review: True
    ).success
    audit = audit_config_sync(project, config_path=config_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)

    def fixed_audit(*_args: object, **_kwargs: object) -> ConfigSyncAudit:
        return audit

    def fail_acquisition(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(workflow_module, "audit_config_sync", fixed_audit)
    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_acquisition)
    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "canonical-lock-failed"
    assert problem.identifier != DriftClass.INVALID_OR_SEMANTIC.value
    assert "No locks available" in problem.message


def test_initial_audit_lock_failure_reports_the_structured_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)

    def fail_acquisition(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_acquisition)
    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "canonical-lock-failed"
    assert str(project / "config") in problem.message
    assert "No locks available" in problem.message
    assert problem.remedy == LOCK_REMEDY
    assert "portable" not in problem.remedy
    assert "writable" not in problem.remedy


def test_canonical_unlock_failure_reports_preparation_lock_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    assert prepare_config_workflow(
        project, config_path=config_path, confirm_host_workflow=lambda review: True
    ).success
    audit = audit_config_sync(project, config_path=config_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    original_flock = workflow_publisher.fcntl.flock

    def fixed_audit(*_args: object, **_kwargs: object) -> ConfigSyncAudit:
        return audit

    def failed_view(*_args: object, **_kwargs: object) -> CanonicalDeliveryViewResult:
        return CanonicalDeliveryViewResult(False, audit)

    def fail_unlock(descriptor: int, operation: int) -> None:
        if operation == workflow_publisher.fcntl.LOCK_UN:
            raise OSError(errno.EINTR, "Interrupted system call")
        original_flock(descriptor, operation)

    monkeypatch.setattr(workflow_module, "audit_config_sync", fixed_audit)
    monkeypatch.setattr(workflow_module, "load_canonical_delivery_view", failed_view)
    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_unlock)
    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "canonical-lock-failed"
    assert "Interrupted system call" in problem.message


def test_canonical_config_reload_os_error_is_not_reported_as_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    assert prepare_config_workflow(
        project, config_path=config_path, confirm_host_workflow=lambda review: True
    ).success
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex")
    target.destination_root.mkdir()
    original_audit = workflow_module.audit_config_sync
    original_mode = stat.S_IMODE(config_path.stat().st_mode)
    config_file = config_path

    def make_config_unreadable_after_audit(
        project_root: Path, *, config_path: Path | None = None
    ) -> ConfigSyncAudit:
        audit = original_audit(project_root, config_path=config_path)
        config_file.chmod(0o000)
        return audit

    monkeypatch.setattr(workflow_module, "audit_config_sync", make_config_unreadable_after_audit)
    try:
        result = prepare_config_workflow(
            project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
        )
    finally:
        config_path.chmod(original_mode)

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == DriftClass.INVALID_OR_SEMANTIC.value
    assert "publish" not in problem.message
    assert str(target.destination_root) not in problem.message


@pytest.mark.parametrize("source", ("claude", "codex", "opencode"))
def test_compose_claude_uses_direct_mounts_and_codex_uses_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: ConfigSyncSource
) -> None:
    project, config_path, runtime = _workspace(tmp_path, source=source)
    claude_root = runtime / "claude"
    codex_root = runtime / "codex"
    claude_root.mkdir(parents=True)
    codex_root.mkdir(parents=True)
    monkeypatch.setattr(
        workflow_module,
        "workflow_image_compatible",
        lambda: WorkflowImageCompatibility.COMPATIBLE,
    )
    monkeypatch.setattr(workflow_module, "ensure_host_env", _ensure_host_env)

    result = prepare_config_workflow(
        project,
        (
            WorkflowDeliveryTarget("claude", claude_root),
            WorkflowDeliveryTarget("codex", codex_root),
        ),
        config_path=config_path,
        require_compose_host_env=True,
        confirm_host_workflow=lambda review: True,
    )

    assert result.success
    instructions = project / "config/claude/AGENTS.md"
    assert instructions.is_file()
    assert stat.S_ISREG(instructions.lstat().st_mode)
    assert instructions.read_text() == "shared workflow\n"
    assert not (claude_root / RUNTIME_MANIFEST_NAME).exists()
    assert (codex_root / RUNTIME_MANIFEST_NAME).is_file()


@pytest.mark.parametrize("source", ("claude", "codex", "opencode"))
@pytest.mark.parametrize("invalid_entry", ("missing", "directory"))
def test_compose_preparation_refuses_unusable_instruction_mount_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: ConfigSyncSource,
    invalid_entry: str,
) -> None:
    project, config_path, runtime = _workspace(tmp_path, source=source)
    monkeypatch.setattr(workflow_module, "ensure_host_env", _ensure_host_env)
    if source != "claude":
        assert prepare_config_workflow(
            project, config_path=config_path, confirm_host_workflow=lambda review: True
        ).success
    instructions = project / "config/claude/AGENTS.md"
    instructions.unlink()
    if invalid_entry == "directory":
        instructions.mkdir()

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", runtime / "claude"),),
        config_path=config_path,
        require_compose_host_env=True,
        container_image_compatibility=WorkflowImageCompatibility.COMPATIBLE,
        confirm_host_workflow=lambda review: True,
    )

    assert not result.success
    assert result.problems[0].identifier == (
        "invalid-or-semantic" if source == "claude" else "target-drift"
    )
    assert not instructions.is_file()
    assert not (runtime / "claude" / RUNTIME_MANIFEST_NAME).exists()


def test_compose_image_gate_blocks_before_audit_or_runtime_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, runtime = _workspace(tmp_path)
    sentinel = "PRIVATE-WORKFLOW-BODY"
    (project / "config/claude/AGENTS.md").write_text(sentinel)
    monkeypatch.setattr(
        workflow_module,
        "workflow_image_compatible",
        lambda: WorkflowImageCompatibility.INCOMPATIBLE,
    )
    monkeypatch.setattr(
        workflow_module,
        "audit_config_sync",
        _audit_must_not_run,
    )

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("codex", runtime / "codex", provision=True),),
        config_path=config_path,
        require_compose_host_env=True,
        confirm_host_workflow=lambda review: True,
    )

    assert not result.success
    assert result.problems[0].remedy == "Rebuild/recreate required."
    assert sentinel not in repr(result)
    assert not (runtime / "codex").exists()


def test_compose_image_gate_handles_every_noncompatible_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert set(_IMAGE_GATE_FAILURES) == {
        compatibility
        for compatibility in WorkflowImageCompatibility
        if compatibility is not WorkflowImageCompatibility.COMPATIBLE
    }

    for compatibility, expected in _IMAGE_GATE_FAILURES.items():
        project, config_path, runtime = _workspace(tmp_path / compatibility.value)
        monkeypatch.setattr(
            workflow_module,
            "workflow_image_compatible",
            lambda compatibility=compatibility: compatibility,
        )
        monkeypatch.setattr(workflow_module, "audit_config_sync", _audit_must_not_run)

        result = prepare_config_workflow(
            project,
            (WorkflowDeliveryTarget("codex", runtime / "codex", provision=True),),
            config_path=config_path,
            require_compose_host_env=True,
            confirm_host_workflow=lambda review: True,
        )

        assert not result.success
        problem = result.problems[0]
        assert (problem.identifier, problem.message, problem.remedy) == expected
        assert not (runtime / "codex").exists()


def test_host_provisioning_failure_reports_the_path_not_workflow_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable config root must not be reported as a workflow-artifact problem.

    Routing the OSError through the canonical drift remedy told users to make a
    workflow artifact portable when the actual cause was a permission denial on
    a host path — a diagnosable failure turned into a misleading one.
    """
    project, config_path, runtime = _workspace(tmp_path)

    def _refuse(_config: AppConfig) -> None:
        raise PermissionError(13, "Permission denied", "/root-owned/unused-agent")

    monkeypatch.setattr(
        workflow_module,
        "workflow_image_compatible",
        lambda: WorkflowImageCompatibility.COMPATIBLE,
    )
    monkeypatch.setattr(workflow_module, "ensure_host_env", _refuse)
    monkeypatch.setattr(workflow_module, "audit_config_sync", _audit_must_not_run)

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("codex", runtime / "codex", provision=True),),
        config_path=config_path,
        require_compose_host_env=True,
        confirm_host_workflow=lambda review: True,
    )

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "host-provisioning-failed"
    assert "/root-owned/unused-agent" in problem.message
    assert "writable" in problem.remedy
    assert "portable" not in problem.remedy


def test_compose_uses_the_running_container_image_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, runtime = _workspace(tmp_path)
    monkeypatch.setattr(workflow_module, "workflow_image_compatible", _audit_must_not_run)

    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("codex", runtime / "codex", provision=True),),
        config_path=config_path,
        require_compose_host_env=True,
        container_image_compatibility=WorkflowImageCompatibility.INCOMPATIBLE,
        confirm_host_workflow=lambda review: True,
    )

    assert not result.success
    assert result.problems[0].remedy == "Rebuild/recreate required."
    assert not (runtime / "codex").exists()


def test_runtime_publish_rechecks_source_after_delivery_view_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    assert prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    ).success
    destination = target.destination_root
    before_agents = (destination / "AGENTS.md").read_bytes()
    before_manifest = (destination / RUNTIME_MANIFEST_NAME).read_bytes()
    original = workflow_module.load_canonical_delivery_view

    def edit_source_after_view_load(
        project_root: Path,
        tool: ConfigSyncSource,
        *,
        config_path: Path | None = None,
        canonical_lease: CanonicalLockLease | None = None,
    ) -> CanonicalDeliveryViewResult:
        loaded = original(
            project_root,
            tool,
            config_path=config_path,
            canonical_lease=canonical_lease,
        )
        (project / "config/claude/AGENTS.md").write_text("operator edit\n")
        return loaded

    monkeypatch.setattr(
        workflow_module, "load_canonical_delivery_view", edit_source_after_view_load
    )
    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    assert result.problems[0].identifier == DriftClass.SOURCE_CHANGED.value
    assert (destination / "AGENTS.md").read_bytes() == before_agents
    assert (destination / RUNTIME_MANIFEST_NAME).read_bytes() == before_manifest


def test_runtime_publish_rechecks_native_only_input_after_delivery_view_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    plugin = project / "config/opencode/plugins/ready-notify.js"
    plugin.parent.mkdir()
    plugin.write_bytes(b"export const Plugin = () => ({});\n")
    target = WorkflowDeliveryTarget("opencode", tmp_path / "host-opencode", provision=True)
    assert prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    ).success
    before_plugin = (target.destination_root / "plugins/ready-notify.js").read_bytes()
    before_manifest = (target.destination_root / RUNTIME_MANIFEST_NAME).read_bytes()
    original = workflow_module.load_canonical_delivery_view

    def edit_native_input_after_view_load(
        project_root: Path,
        tool: ConfigSyncSource,
        *,
        config_path: Path | None = None,
        canonical_lease: CanonicalLockLease | None = None,
    ) -> CanonicalDeliveryViewResult:
        loaded = original(
            project_root,
            tool,
            config_path=config_path,
            canonical_lease=canonical_lease,
        )
        plugin.write_bytes(b"export const Plugin = () => ({ changed: true });\n")
        return loaded

    monkeypatch.setattr(
        workflow_module, "load_canonical_delivery_view", edit_native_input_after_view_load
    )
    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert not result.success
    assert result.problems[0].identifier == DriftClass.SOURCE_CHANGED.value
    assert (target.destination_root / "plugins/ready-notify.js").read_bytes() == before_plugin
    assert (target.destination_root / RUNTIME_MANIFEST_NAME).read_bytes() == before_manifest


def test_host_claude_profile_rewrites_only_managed_hook_paths(tmp_path: Path) -> None:
    _project, config_path, _runtime = _workspace(tmp_path)
    config = workflow_module.load_config(config_path)
    seed_value = json.dumps(
        [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 ~/.claude_seed/ready_notify_hook.py",
                    }
                ],
            }
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    view = WorkflowView(
        "claude",
        (),
        (
            CarrierFragment(PurePosixPath("settings.json"), ("hooks", "Stop"), seed_value),
            CarrierFragment(
                PurePosixPath("settings.json"),
                ("operator", "keep"),
                b'"~/.claude_seed/keep"',
            ),
        ),
    )

    bridged = workflow_module._host_claude_view(  # pyright: ignore[reportPrivateUsage]
        config,
        WorkflowDeliveryTarget("claude", tmp_path / "host-claude"),
        view,
    )

    assert bridged is not None
    assert b"~/.claude/ready_notify_hook.py" in bridged.fragments[0].value_json
    assert bridged.fragments[1] == view.fragments[1]
    assert CANONICAL_REMEDY == (
        "Author or edit the artifact natively in the target tool's view, "
        "or make the source form portable."
    )


_BUCKET = "8838fb9b-ff03-4afc-9dca-4826659fd5b1_cc3e3300-e0c1-4cbe-9603-b1aca8eac330"
_BINARY = b"\x00\x01\xff\xfe binary, not UTF-8 \xc3\x28\n"


def _runtime_state(source_root: Path, *, generation: int = 0) -> None:
    """Reproduce what Claude Code and the interpreter write into ./config/claude."""
    synced = source_root / "skills" / "synced" / _BUCKET / "morning" / "assets" / "fonts"
    synced.mkdir(parents=True, exist_ok=True)
    (synced / "fraunces-latin-600-normal.woff2").write_bytes(_BINARY + bytes([generation]))
    cache = source_root / "scripts" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"status-line.cpython-31{generation}.pyc").write_bytes(_BINARY)


def test_runtime_state_does_not_block_repeated_workflow_preparation(tmp_path: Path) -> None:
    """The start path that failed: prepare must stay clean across appearances.

    Fehlerbild A and B both surfaced here, on `djinn start` — once per blocker,
    each only after the previous one had been moved away.
    """
    project, config_path, _runtime = _workspace(tmp_path)
    source_root = project / "config" / "claude"
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    _runtime_state(source_root)

    first = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert first.success, first.problems
    assert (target.destination_root / "AGENTS.md").read_text() == "shared workflow\n"

    # A second start after Claude re-synced its account skills and a new cache
    # appeared: still clean, and idempotent.
    _runtime_state(source_root, generation=1)
    second = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=lambda review: True
    )

    assert second.success, second.problems
    assert second.problems == ()
    assert not (target.destination_root / "skills" / "synced").exists()
    assert not (target.destination_root / "scripts" / "__pycache__").exists()
    # Nothing of the tool's own data was removed to get there.
    assert (source_root / "skills" / "synced" / _BUCKET).is_dir()
    assert list((source_root / "scripts" / "__pycache__").iterdir())


def test_managed_hook_commands_never_sync_the_working_directory_project() -> None:
    # `uv run` would create a venv and build any Python project in the agent's cwd.
    from djinn_in_a_box.core import config_workflow

    rewrites = config_workflow._CLAUDE_HOOK_REWRITES  # pyright: ignore[reportPrivateUsage]
    assert rewrites
    for container_form, host_form in rewrites.values():
        for raw in (container_form, host_form):
            for entry in json.loads(raw):
                for hook in entry["hooks"]:
                    assert hook["command"].startswith("python3 "), hook["command"]


@pytest.fixture
def host_case(tmp_path):
    project, config_path, _runtime = _workspace(tmp_path)
    source = project / "config/claude"
    (source / "context").mkdir()
    (source / "context/guide.md").write_text("guide v1\n")
    hook = source / "security_reminder_hook.py"
    hook.write_text("print('harmless')\n")
    hook.chmod(0o700)
    (source / "settings.json").write_bytes(
        workflow_module._canonical_json(
            {
                "hooks": {
                    "PreToolUse": json.loads(
                        workflow_module._CLAUDE_HOOK_REWRITES[("hooks", "PreToolUse")][0]
                    )
                },
            }
        )
    )
    target = WorkflowDeliveryTarget("claude", tmp_path / "absent-parent/host-claude", True)
    return project, config_path, target


def _host_prepare(case, confirm=None):
    project, config_path, target = case
    return prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=confirm
    )


def _host_state(case):
    root = case[2].destination_root
    record = workflow_module.HOST_WORKFLOW_TRUST_FILE
    return (
        root.exists(),
        root.parent.exists(),
        {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()},
        record.read_bytes() if record.exists() else None,
    )


def test_host_trust_default_path():
    assert (
        _DEFAULT_HOST_WORKFLOW_TRUST_FILE == workflow_module.CONFIG_DIR / "host-workflow-trust.json"
    )


def test_host_first_review_and_cached_confirmation(host_case, monkeypatch):
    project, _config, target = host_case
    reviews = []
    leases = []
    original_lock = workflow_module.canonical_lock
    original_save = workflow_module._save_host_trust

    @contextmanager
    def lock(root, *, exclusive):
        assert exclusive is False
        with original_lock(root, exclusive=exclusive) as lease:
            leases.append("enter")
            yield lease
        leases.append("exit")

    def save(*args):
        assert leases == ["enter", "exit", "enter", "exit"]
        assert (target.destination_root / RUNTIME_MANIFEST_NAME).exists()
        return original_save(*args)

    monkeypatch.setattr(workflow_module, "canonical_lock", lock)
    monkeypatch.setattr(workflow_module, "_save_host_trust", save)

    def confirm(review):
        # No shared lease is held over human input.
        descriptor = os.open(project / "config", os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        assert leases == ["enter", "exit"]
        assert not target.destination_root.parent.exists()
        assert not workflow_module.HOST_WORKFLOW_TRUST_FILE.exists()
        reviews.append(review)
        return True

    assert _host_prepare(host_case, confirm).success
    (review,) = reviews
    bridge = workflow_module._claude_bridge_path()
    assert review.tool == "claude"
    assert review.destination_root == target.destination_root
    assert review.sources == (project / "config/claude", bridge)
    assert [(c.status, c.label) for c in review.changes] == [
        ("new", "AGENTS.md"),
        ("new", "CLAUDE.md"),
        ("new", "context/guide.md"),
        ("new", "security_reminder_hook.py (executable)"),
        ("new", "settings.json: hooks.PreToolUse"),
    ]
    manifest = json.loads((target.destination_root / RUNTIME_MANIFEST_NAME).read_bytes())
    actual = frozenset(
        workflow_publisher.ManifestItem(
            PurePosixPath(item["path"]),
            item["content_hash"],
            item["executable"],
            tuple(item["key_path"]) if "key_path" in item else None,
        )
        for item in manifest["items"]
    )
    assert review.items == actual
    assert (
        next(i for i in review.items if i.path.as_posix() == "CLAUDE.md").content_hash
        == hashlib.sha256(bridge.read_bytes()).hexdigest()
    )
    (fragment,) = [i for i in review.items if i.key_path is not None]
    assert (
        fragment.content_hash
        == hashlib.sha256(
            workflow_module._CLAUDE_HOOK_REWRITES[("hooks", "PreToolUse")][1]
        ).hexdigest()
    )
    record = workflow_module.HOST_WORKFLOW_TRUST_FILE
    before = record.read_bytes()
    entries = json.loads(before)[str(target.destination_root)]
    assert entries == [
        {
            "path": item.path.as_posix(),
            "key_path": list(item.key_path) if item.key_path else None,
            "content_hash": item.content_hash,
            "executable": item.executable,
        }
        for item in sorted(
            review.items, key=lambda item: (item.path.as_posix(), item.key_path or ())
        )
    ]
    monkeypatch.setattr(
        workflow_module, "_save_host_trust", lambda *args: pytest.fail("cached trust rewritten")
    )
    assert _host_prepare(host_case, lambda review: pytest.fail("cached trust prompted")).success
    assert record.read_bytes() == before


@pytest.mark.parametrize("tool", ["claude", "codex", "opencode"])
def test_host_missing_record_refuses_without_creating_directories(tmp_path, tool):
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget(tool, tmp_path / "absent-parent" / tool, provision=True)
    assert not target.destination_root.parent.exists()
    assert not workflow_module.HOST_WORKFLOW_TRUST_FILE.exists()
    result = prepare_config_workflow(project, (target,), config_path=config_path)
    assert result.problems[0].identifier == "host-workflow-unconfirmed"
    assert not target.destination_root.exists()
    assert not target.destination_root.parent.exists()
    assert not workflow_module.HOST_WORKFLOW_TRUST_FILE.exists()


def test_host_refusal_creates_nothing(host_case):
    before = _host_state(host_case)
    result = _host_prepare(host_case, lambda review: False)
    assert not result.success
    assert result.problems[0].identifier == "host-workflow-unconfirmed"
    assert (
        result.problems[0].message
        == "Host workflow for claude was not confirmed; nothing was published."
    )
    assert (
        result.problems[0].remedy == "Run `djinn session --agent claude` in a terminal, "
        "review the listed changes and confirm."
    )
    assert _host_state(host_case) == before


def test_host_cached_confirmation_still_rejects_target_drift(host_case):
    assert _host_prepare(host_case, lambda review: True).success
    (host_case[2].destination_root / "AGENTS.md").write_text("host-owned edit\n")
    before = _host_state(host_case)
    result = _host_prepare(host_case, lambda review: pytest.fail("cached trust prompted"))
    assert result.problems[0].identifier == "target-drift"
    assert _host_state(host_case) == before


@pytest.mark.parametrize(
    "change,label,status",
    [
        ("content", "context/guide.md", "changed"),
        ("executable", "context/guide.md (executable)", "changed"),
        ("fragment", "settings.json: hooks.SessionStart", "new"),
        ("fragment-value", "settings.json: hooks.SessionStart", "changed"),
        ("removed", "context/guide.md", "removed"),
        ("added", "context/new.md", "new"),
    ],
    ids=["content", "executable", "fragment", "fragment-value", "removed", "added"],
)
def test_host_item_change_requires_confirmation(host_case, change, label, status):
    source = host_case[0] / "config/claude"
    settings = source / "settings.json"
    if change == "fragment-value":
        (source / "scripts").mkdir()
        (source / "scripts/session-start-status.py").write_text("print('startup')\n")
        data = json.loads(settings.read_bytes())
        data["hooks"]["SessionStart"] = [{"hooks": [{"type": "command", "command": "echo v1"}]}]
        settings.write_text(json.dumps(data))
    assert _host_prepare(host_case, lambda review: True).success
    guide = source / "context/guide.md"
    if change == "content":
        guide.write_text("guide v2\n")
    elif change == "executable":
        guide.chmod(0o700)
    elif change in {"fragment", "fragment-value"}:
        if change == "fragment":
            (source / "scripts").mkdir()
            (source / "scripts/session-start-status.py").write_text("print('startup')\n")
        data = json.loads(settings.read_bytes())
        data["hooks"]["SessionStart"] = [{"hooks": [{"type": "command", "command": "echo v2"}]}]
        settings.write_text(json.dumps(data))
    elif change == "removed":
        guide.unlink()
    else:
        (source / "context/new.md").write_text("new\n")
    before = _host_state(host_case)
    reviews = []
    result = _host_prepare(host_case, lambda review: reviews.append(review) or False)
    assert result.problems[0].identifier == "host-workflow-unconfirmed"
    expected = [(status, label)]
    if change == "fragment":
        expected.insert(0, ("new", "scripts/session-start-status.py"))
    assert [(c.status, c.label) for c in reviews[0].changes] == expected
    assert _host_state(host_case) == before


def test_host_personal_settings_are_not_items(host_case):
    assert _host_prepare(host_case, lambda review: True).success
    before = _host_state(host_case)
    (host_case[0] / "config/claude/settings.local.json").write_text('{"personal": true}')
    assert _host_prepare(
        host_case, lambda review: pytest.fail("personal settings prompted")
    ).success
    assert _host_state(host_case) == before
    assert not (host_case[2].destination_root / "settings.local.json").exists()


@pytest.mark.parametrize(
    "race",
    ["source", "unchanged-item", "delete-instructions", "bad-hook", "bridge", "bridge-missing"],
    ids=["A", "B", "D-delete", "D-hook", "E", "D-bridge-missing"],
)
def test_host_confirmation_race(host_case, monkeypatch, tmp_path, race):
    source = host_case[0] / "config/claude"
    bridge = tmp_path / "bridge/CLAUDE.md"
    bridge.parent.mkdir()
    bridge.write_text("@AGENTS.md\n")
    monkeypatch.setattr(workflow_module, "_claude_bridge_path", lambda: bridge)
    if race == "unchanged-item":
        assert _host_prepare(host_case, lambda review: True).success
        (source / "context/guide.md").write_text("pending update\n")
    before = _host_state(host_case)
    calls = []

    def confirm(review):
        calls.append(review)
        monkeypatch.setattr(
            workflow_module,
            "sync_config",
            lambda *args, **kwargs: pytest.fail("prompt-time repair"),
        )
        if race in {"source", "unchanged-item"}:
            (source / "AGENTS.md").write_text("changed during prompt\n")
        elif race == "delete-instructions":
            (source / "AGENTS.md").unlink()
        elif race == "bad-hook":
            (source / "settings.json").write_text('{"hooks":{"PreToolUse":[]}}')
        elif race == "bridge-missing":
            bridge.unlink()
        else:
            bridge.write_text("new unapproved bridge\n")
        return True

    result = _host_prepare(host_case, confirm)
    assert len(calls) == 1
    assert not result.success
    assert result.problems[0].identifier == "host-workflow-changed"
    assert (
        result.problems[0].message
        == "Workflow source changed during confirmation; nothing was published."
    )
    assert result.problems[0].remedy == "Retry."
    assert _host_state(host_case) == before


def test_host_confirmation_late_source_race(host_case, monkeypatch):
    assert _host_prepare(host_case, lambda review: True).success
    source = host_case[0] / "config/claude"
    (source / "context/guide.md").write_text("pending update\n")
    before = _host_state(host_case)
    calls = []

    def late_edit():
        calls.append("commit")
        monkeypatch.setattr(workflow_publisher, "_before_target_commit", lambda: None)
        (source / "AGENTS.md").write_text("late unapproved edit\n")

    def confirm(review):
        calls.append("confirm")
        monkeypatch.setattr(workflow_publisher, "_before_target_commit", late_edit)
        return True

    result = _host_prepare(host_case, confirm)
    assert calls == ["confirm", "commit"]
    assert result.problems[0].identifier == "host-workflow-changed"
    assert _host_state(host_case) == before


@pytest.mark.parametrize("existing", ["none", "parent", "root"])
def test_host_confirmation_late_race_leaves_at_most_an_empty_root(host_case, monkeypatch, existing):
    root = host_case[2].destination_root
    if existing == "root":
        root.mkdir(parents=True)
    elif existing == "parent":
        root.parent.mkdir()

    def late_edit():
        monkeypatch.setattr(workflow_publisher, "_before_target_commit", lambda: None)
        (host_case[0] / "config/claude/AGENTS.md").write_text("late unapproved edit\n")

    def confirm(review):
        monkeypatch.setattr(workflow_publisher, "_before_target_commit", late_edit)
        return True

    result = _host_prepare(host_case, confirm)
    assert result.problems[0].identifier == "host-workflow-changed"
    # Nothing is published or recorded, and nothing is removed: the destination prepared for
    # the attempt stays as an empty directory.
    assert root.is_dir()
    assert list(root.iterdir()) == []
    assert not workflow_module.HOST_WORKFLOW_TRUST_FILE.exists()


@pytest.mark.parametrize("failure", ["provision", "publish"])
def test_host_confirmation_os_errors_keep_mappings(host_case, monkeypatch, failure):
    root = host_case[2].destination_root
    if failure == "provision":
        original_mkdir = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path == root:
                raise PermissionError("root provisioning failed")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", mkdir)
    else:

        def publish(*args, **kwargs):
            raise PermissionError("publication failed")

        monkeypatch.setattr(workflow_module, "publish_workflow_view", publish)
    result = _host_prepare(host_case, lambda review: True)
    assert result.problems[0].identifier == {
        "provision": "target-provisioning-failed",
        "publish": "host-workflow-changed",
    }[failure]
    assert not (root / RUNTIME_MANIFEST_NAME).exists()
    assert not workflow_module.HOST_WORKFLOW_TRUST_FILE.exists()


def test_host_review_keeps_newline_filename_raw(host_case):
    name = "context/line\nbreak.md"
    (host_case[0] / "config/claude" / name).write_text("review this file\n")
    reviews = []
    result = _host_prepare(host_case, lambda review: reviews.append(review) or False)
    assert result.problems[0].identifier == "host-workflow-unconfirmed"
    assert len(reviews) == 1
    assert name in {change.label for change in reviews[0].changes}
    assert PurePosixPath(name) in {item.path for item in reviews[0].items}
    assert not host_case[2].destination_root.exists()


def test_host_review_uses_actual_bridge_path(host_case, monkeypatch, tmp_path):
    installation = tmp_path / "installation"
    bridge = installation / "templates/claude/CLAUDE.md"
    bridge.parent.mkdir(parents=True)
    bridge.write_bytes(b"installed bridge\n")
    monkeypatch.setattr(workflow_module, "get_project_root", lambda: installation)
    reviews = []
    _host_prepare(host_case, lambda review: reviews.append(review) or False)
    assert reviews[0].sources == (host_case[0] / "config/claude", bridge)
    (item,) = [i for i in reviews[0].items if i.path.as_posix() == "CLAUDE.md"]
    assert item.content_hash == hashlib.sha256(bridge.read_bytes()).hexdigest()


def test_host_cross_tool_review_sources(tmp_path):
    project, config_path, _runtime = _workspace(tmp_path)
    hook = project / "config/codex/hooks/security_guard.py"
    hook.parent.mkdir()
    hook.write_text("print('native')\n")
    (project / "config/codex/hooks.json").write_text(
        '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"echo native"}]}]}}'
    )
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", True)
    reviews = []
    result = prepare_config_workflow(
        project,
        (target,),
        config_path=config_path,
        confirm_host_workflow=lambda review: reviews.append(review) or False,
    )
    assert result.problems[0].identifier == "host-workflow-unconfirmed"
    assert reviews[0].sources == (project / "config/claude", project / "config/codex")
    assert any(i.path.as_posix() == "hooks/security_guard.py" for i in reviews[0].items)


def _codex_native_case(tmp_path):
    project, config_path, _runtime = _workspace(tmp_path)
    hook = project / "config/codex/hooks/security_guard.py"
    hook.parent.mkdir()
    hook.write_text("print('native')\n")
    registration = project / "config/codex/hooks.json"
    registration.write_text(
        '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"echo native"}]}]}}'
    )
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", True)
    return project, config_path, target, hook, registration


@pytest.mark.parametrize("race", ["fragment-value", "executable-only"])
def test_host_confirmation_race_on_fragment_and_executable_bit(tmp_path, race):
    # Both edits keep the payload loadable, so only the comparison with the approved set
    # (fragments and executable flags included) can refuse them.
    project, config_path, target, hook, registration = _codex_native_case(tmp_path)

    def confirm(review):
        if race == "fragment-value":
            registration.write_text(
                '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"echo swapped"}]}]}}'
            )
        else:
            hook.chmod(0o755)
        return True

    result = prepare_config_workflow(
        project, (target,), config_path=config_path, confirm_host_workflow=confirm
    )
    assert result.problems[0].identifier == "host-workflow-changed"
    assert not target.destination_root.exists()
    assert not workflow_module.HOST_WORKFLOW_TRUST_FILE.exists()


def test_host_claude_review_sources_with_other_selected_source(tmp_path):
    project, config_path, _runtime = _workspace(tmp_path, "codex")
    target = WorkflowDeliveryTarget("claude", tmp_path / "host-claude", True)
    reviews = []
    result = prepare_config_workflow(
        project,
        (target,),
        config_path=config_path,
        confirm_host_workflow=lambda review: reviews.append(review) or False,
    )
    assert result.problems[0].identifier == "host-workflow-unconfirmed"
    assert reviews[0].sources == (
        project / "config/codex",
        project / "config/claude",
        workflow_module._claude_bridge_path(),
    )


@pytest.mark.parametrize("destination", ["other-tool", "nested"])
def test_host_gate_exempts_only_the_exact_config_root_tool_directory(tmp_path, destination):
    project, config_path, runtime = _workspace(tmp_path)
    root = runtime / "codex" if destination == "other-tool" else runtime / "claude/nested"
    reviews = []
    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("claude", root, True),),
        config_path=config_path,
        confirm_host_workflow=lambda review: reviews.append(review) or False,
    )
    assert result.problems[0].identifier == "host-workflow-unconfirmed"
    assert len(reviews) == 1
    assert not root.exists()


def test_host_confirmation_is_bound_to_its_own_root(host_case, tmp_path):
    reviews = []
    assert not _host_prepare(host_case, lambda review: reviews.append(review) or False).success
    workflow_module._save_host_trust(
        workflow_module.HOST_WORKFLOW_TRUST_FILE, str(tmp_path / "other-root"), reviews[0].items
    )
    before = _host_state(host_case)
    result = _host_prepare(host_case, lambda review: reviews.append(review) or False)
    assert result.problems[0].identifier == "host-workflow-unconfirmed"
    assert len(reviews) == 2
    assert _host_state(host_case) == before


@pytest.mark.parametrize("tool", ["claude", "codex", "opencode"])
def test_container_root_delivery_never_requires_host_trust(tmp_path, tool):
    project, config_path, runtime = _workspace(tmp_path)
    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget(tool, runtime / tool, True),),
        config_path=config_path,
        confirm_host_workflow=lambda review: pytest.fail("container prompted"),
    )
    assert result.success, result.problems
    assert not workflow_module.HOST_WORKFLOW_TRUST_FILE.exists()
    assert not (runtime / tool / "CLAUDE.md").exists()


def test_host_trust_overlapping_saves_preserve_both_roots(tmp_path, monkeypatch):
    record = tmp_path / "trust/record.json"
    first_loaded = threading.Event()
    second_started = threading.Event()
    second_finished = threading.Event()
    release_first = threading.Event()
    errors = []
    original_load = workflow_module._load_host_trust

    def load(path):
        records = original_load(path)
        if threading.current_thread().name == "trust-first":
            first_loaded.set()
            if not release_first.wait(5):
                raise TimeoutError("first save was not released")
        return records

    monkeypatch.setattr(workflow_module, "_load_host_trust", load)

    def save(root, second=False):
        try:
            if second:
                second_started.set()
            workflow_module._save_host_trust(record, root, frozenset())
        except Exception as error:
            errors.append(error)
        finally:
            if second:
                second_finished.set()

    first = threading.Thread(target=save, args=("/first",), name="trust-first", daemon=True)
    second = threading.Thread(target=save, args=("/second", True), daemon=True)
    first.start()
    try:
        assert first_loaded.wait(3), "first save never loaded"
        second.start()
        assert second_started.wait(3), "second save never started"
        # Without the transaction lock, the second save finishes with an old
        # snapshot before the first is released; the first then overwrites
        # the second root's entry.
        second_finished.wait(0.5)
    finally:
        release_first.set()
        first.join(timeout=3)
        if second.ident is not None:
            second.join(timeout=3)
    assert not first.is_alive() and not second.is_alive(), "trust saves deadlocked"
    assert errors == []
    assert json.loads(record.read_bytes()) == {"/first": [], "/second": []}


def test_host_trust_lock_encloses_load_update_and_replace(tmp_path, monkeypatch):
    record = tmp_path / "trust/record.json"
    original_lock = workflow_module.config_directory_lock
    original_load = workflow_module._load_host_trust
    original_json = workflow_module._canonical_json
    original_replace = os.replace
    events = []
    locked = False

    @contextmanager
    def lock(path, *, exclusive):
        nonlocal locked
        assert path == record.parent
        assert exclusive is True
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        with original_lock(path, exclusive=exclusive):
            locked = True
            events.append("lock")
            try:
                yield
            finally:
                locked = False
                events.append("unlock")

    def load(path):
        assert locked, "record load escaped lock"
        events.append("load")
        return original_load(path)

    def canonical_json(payload):
        assert locked, "record update escaped lock"
        assert payload == {"/host": []}
        events.append("update")
        return original_json(payload)

    def replace(src, dst):
        assert locked, "record replace escaped lock"
        events.append("replace")
        return original_replace(src, dst)

    monkeypatch.setattr(workflow_module, "config_directory_lock", lock)
    monkeypatch.setattr(workflow_module, "_load_host_trust", load)
    monkeypatch.setattr(workflow_module, "_canonical_json", canonical_json)
    monkeypatch.setattr(os, "replace", replace)
    workflow_module._save_host_trust(record, "/host", frozenset())
    assert events == ["lock", "load", "update", "replace", "unlock"]
    assert json.loads(record.read_bytes()) == {"/host": []}


@pytest.mark.parametrize("phase", ["acquire", "release"])
def test_host_trust_lock_failure_maps_to_trust_failure(host_case, monkeypatch, phase):
    original_lock = workflow_module.config_directory_lock
    record = workflow_module.HOST_WORKFLOW_TRUST_FILE

    @contextmanager
    def lock(path, *, exclusive):
        assert path == record.parent and exclusive is True
        if phase == "acquire":
            raise ConfigDirectoryLockError("trust lock failed")
        with original_lock(path, exclusive=exclusive):
            yield
        raise ConfigDirectoryLockError("trust unlock failed")

    monkeypatch.setattr(workflow_module, "config_directory_lock", lock)
    result = _host_prepare(host_case, lambda review: True)
    assert result.problems[0].identifier == "host-workflow-trust-failed"
    assert str(record) in result.problems[0].message
    assert f"trust {'un' if phase == 'release' else ''}lock failed" in result.problems[0].message
    assert result.problems[0].remedy == f"Make {record.parent} writable, then retry."
    assert record.exists() is (phase == "release")
    assert (host_case[2].destination_root / "AGENTS.md").is_file()


def test_host_trust_redirected_atomic_save_and_permissions(host_case, monkeypatch):
    record = workflow_module.HOST_WORKFLOW_TRUST_FILE
    original_mkdir, original_temp = Path.mkdir, workflow_module.tempfile.mkstemp
    original_replace, original_sync = os.replace, os.fsync
    events = []

    def mkdir(path, mode=0o777, parents=False, exist_ok=False):
        assert path != workflow_module.CONFIG_DIR
        if path == record.parent:
            assert mode == 0o700 and parents and exist_ok
        return original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    def mkstemp(*args, **kwargs):
        assert kwargs["dir"] == record.parent
        events.append("temp")
        return original_temp(*args, **kwargs)

    def fsync(fd):
        events.append("sync")
        return original_sync(fd)

    def replace(src, dst, **kwargs):
        if Path(dst) == record:
            assert Path(src).parent == record.parent
            assert stat.S_IMODE(Path(src).stat().st_mode) == 0o600
            events.append("replace")
        return original_replace(src, dst, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(workflow_module.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync)
    assert _host_prepare(host_case, lambda review: True).success
    assert events.index("temp") < len(events) - 1
    assert events[-2:] == ["sync", "replace"]
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert stat.S_IMODE(record.parent.stat().st_mode) == 0o700
    assert list(record.parent.iterdir()) == [record]


@pytest.mark.parametrize(
    "failure", ["mkdir", "replace"], ids=["read-only-parent", "atomic-replace"]
)
def test_host_trust_save_failure(host_case, monkeypatch, failure):
    assert _host_prepare(host_case, lambda review: True).success
    record = workflow_module.HOST_WORKFLOW_TRUST_FILE
    before = record.read_bytes()
    (host_case[0] / "config/claude/context/guide.md").write_text("approved update\n")
    if failure == "mkdir":
        original = Path.mkdir

        def refuse(path, *args, **kwargs):
            if path == record.parent:
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", refuse)
    else:
        original = os.replace

        def refuse(src, dst, **kwargs):
            if Path(dst) == record:
                raise PermissionError(errno.EACCES, "Permission denied", dst)
            return original(src, dst, **kwargs)

        monkeypatch.setattr(os, "replace", refuse)
    result = _host_prepare(host_case, lambda review: True)
    assert result.problems[0].identifier == "host-workflow-trust-failed"
    assert str(record) in result.problems[0].message
    assert "Permission denied" in result.problems[0].message
    assert result.problems[0].remedy == f"Make {record.parent} writable, then retry."
    assert record.read_bytes() == before
    assert (host_case[2].destination_root / "context/guide.md").read_text() == "approved update\n"
    assert list(record.parent.iterdir()) == [record]


@pytest.mark.parametrize(
    "failure",
    ["collision", "lock", "write", "canonical-lock"],
    ids=["collision", "lock", "write", "canonical-lock"],
)
def test_host_publish_failure_does_not_record(host_case, monkeypatch, failure):
    before = _host_state(host_case)
    calls = []

    def confirm(review):
        calls.append("confirm")
        if failure == "collision":
            root = host_case[2].destination_root
            root.mkdir(parents=True)
            (root / "AGENTS.md").write_text("host-owned\n")
        elif failure == "canonical-lock":

            def fail_lock(*args, **kwargs):
                raise workflow_publisher.PublishError(
                    DriftClass.INVALID_OR_SEMANTIC
                ) from PermissionError("lease")

            monkeypatch.setattr(workflow_module, "canonical_lock", fail_lock)
        else:
            kwargs = {f"{failure}_error": PermissionError("host destination")}
            monkeypatch.setattr(
                workflow_module,
                "publish_workflow_view",
                lambda *args, **kw: workflow_publisher.PublishResult(
                    DriftClass.INVALID_OR_SEMANTIC, **kwargs
                ),
            )
        return True

    result = _host_prepare(host_case, confirm)
    assert calls == ["confirm"]
    assert (
        result.problems[0].identifier
        == {
            "collision": "collision",
            "lock": "workflow-lock-failed",
            "write": "workflow-publish-failed",
            "canonical-lock": "canonical-lock-failed",
        }[failure]
    )
    assert not workflow_module.HOST_WORKFLOW_TRUST_FILE.exists()
    if failure == "canonical-lock":
        assert _host_state(host_case) == before


_BAD_RECORDS = [
    ("bad-json", None, None),
    ("duplicate-json-key", None, None),
    ("duplicate-item", None, None),
    ("top-level", None, None),
    ("non-list", None, None),
    ("relative-root", None, None),
    ("empty-root", None, None),
    ("non-object-item", None, None),
    ("missing-field", None, None),
    ("extra-field", None, None),
    ("path-type", "path", 1),
    ("empty-path", "path", ""),
    ("absolute-path", "path", "/escape"),
    ("parent-path", "path", "../escape"),
    ("key-type", "key_path", "hooks"),
    ("empty-key-list", "key_path", []),
    ("key-element-type", "key_path", [1]),
    ("empty-key", "key_path", [""]),
    ("hash-type", "content_hash", 1),
    ("hash-length", "content_hash", "a"),
    ("hash-overlong", "content_hash", "a" * 65),
    ("hash-uppercase", "content_hash", "A" * 64),
    ("hash-nonhex", "content_hash", "g" * 64),
    ("executable-type", "executable", 1),
    ("executable-fragment", "key_path", ["hooks"]),
    ("deep-nesting", None, None),
]


@pytest.mark.parametrize("kind,field,value", _BAD_RECORDS, ids=[row[0] for row in _BAD_RECORDS])
def test_host_malformed_trust_fails_closed_and_repairs(host_case, kind, field, value):
    assert _host_prepare(host_case, lambda review: True).success
    record = workflow_module.HOST_WORKFLOW_TRUST_FILE
    good = json.loads(record.read_bytes())
    item = {"path": "other.md", "key_path": None, "content_hash": "a" * 64, "executable": True}
    bad = {**good, "/other": [item]}
    if field:
        item[field] = value
        if field == "key_path" and kind != "executable-fragment":
            item["executable"] = False
    elif kind == "duplicate-item":
        bad["/other"].append(dict(item))
    elif kind == "top-level":
        bad = []
    elif kind == "non-list":
        bad["/other"] = {}
    elif kind in {"relative-root", "empty-root"}:
        bad["relative" if kind == "relative-root" else ""] = bad.pop("/other")
    elif kind == "non-object-item":
        bad["/other"] = [1]
    elif kind == "missing-field":
        del item["key_path"]
    elif kind == "extra-field":
        item["extra"] = 1
    raw = json.dumps(bad).encode()
    if kind == "bad-json":
        raw = b"{broken"
    elif kind == "deep-nesting":
        raw = b"[" * 200_000 + b"]" * 200_000
    elif kind == "duplicate-json-key":
        raw = raw[:-1] + b',"/other": []}'
    record.write_bytes(raw)
    before = _host_state(host_case)
    reviews = []
    result = _host_prepare(host_case, lambda review: reviews.append(review) or False)
    assert result.problems[0].identifier == "host-workflow-unconfirmed"
    assert len(reviews) == 1
    assert all(change.status == "new" for change in reviews[0].changes)
    assert _host_state(host_case) == before
    assert _host_prepare(host_case, lambda review: True).success
    repaired = json.loads(record.read_bytes())
    assert repaired == good


def test_host_trust_preserves_other_roots(host_case, tmp_path):
    assert _host_prepare(host_case, lambda review: True).success
    record = workflow_module.HOST_WORKFLOW_TRUST_FILE
    before = json.loads(record.read_bytes())
    other = str(tmp_path / "other-root")
    before[other] = []
    record.write_text(json.dumps(before))
    (host_case[0] / "config/claude/context/guide.md").write_text("new guide\n")
    assert _host_prepare(host_case, lambda review: True).success
    assert json.loads(record.read_bytes())[other] == []


def test_host_unreadable_trust_is_unconfirmed(host_case, monkeypatch):
    assert _host_prepare(host_case, lambda review: True).success
    record = workflow_module.HOST_WORKFLOW_TRUST_FILE
    original = Path.read_bytes

    def refuse(path):
        if path == record:
            raise PermissionError("unreadable trust")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", refuse)
    result = _host_prepare(host_case)
    assert result.problems[0].identifier == "host-workflow-unconfirmed"


def test_host_valid_empty_set_is_confirmed(tmp_path, monkeypatch):
    project, config_path, _runtime = _workspace(tmp_path, "codex")
    root = tmp_path / "host-codex"
    record = workflow_module.HOST_WORKFLOW_TRUST_FILE
    record.parent.mkdir()
    record.write_text(json.dumps({str(root): []}))
    before = record.read_bytes()
    monkeypatch.setattr(
        workflow_module,
        "load_canonical_delivery_view",
        lambda *args, **kw: CanonicalDeliveryViewResult(
            True, ConfigSyncAudit("codex", "codex"), WorkflowView("codex", ())
        ),
    )
    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("codex", root, True),),
        config_path=config_path,
        confirm_host_workflow=lambda review: pytest.fail("empty set prompted"),
    )
    assert result.success, result.problems
    assert record.read_bytes() == before
    record.unlink()
    result = prepare_config_workflow(
        project, (WorkflowDeliveryTarget("codex", root),), config_path=config_path
    )
    assert result.problems[0].identifier == "host-workflow-unconfirmed"


def test_host_confirmation_reload_lock_failure_keeps_mapping(host_case, monkeypatch):
    def confirm(review):
        monkeypatch.setattr(
            workflow_module,
            "load_canonical_delivery_view",
            lambda *args, **kwargs: CanonicalDeliveryViewResult(
                False,
                ConfigSyncAudit(
                    "claude",
                    "claude",
                    problems=(
                        workflow_module.SyncProblem(
                            "canonical-lock-failed", "lease failed", remedy="Retry lock."
                        ),
                    ),
                ),
            ),
        )
        return True

    before = _host_state(host_case)
    result = _host_prepare(host_case, confirm)
    assert result.problems[0].identifier == "canonical-lock-failed"
    assert result.problems[0].message == "lease failed"
    assert result.problems[0].remedy == "Retry lock."
    assert _host_state(host_case) == before


def test_host_change_identity_labels_and_sorting():
    item = workflow_publisher.ManifestItem
    path = PurePosixPath
    old = frozenset(
        {
            item(path("z.py"), "a" * 64, True),
            item(path("settings.json"), "a" * 64, False, ("hooks", "Stop")),
            item(path("settings.json"), "a" * 64, False, ("hooks", "PreToolUse")),
            item(path("keep.md"), "a" * 64, False),
        }
    )
    new = frozenset(
        {
            item(path("a.py"), "b" * 64, True),
            item(path("settings.json"), "b" * 64, False, ("hooks", "Stop")),
            item(path("settings.json"), "a" * 64, False, ("hooks", "SessionStart")),
            item(path("keep.md"), "a" * 64, False),
        }
    )
    assert [(c.status, c.label) for c in workflow_module._host_workflow_changes(new, old)] == [
        ("new", "a.py (executable)"),
        ("removed", "settings.json: hooks.PreToolUse"),
        ("new", "settings.json: hooks.SessionStart"),
        ("changed", "settings.json: hooks.Stop"),
        ("removed", "z.py (executable)"),
    ]


def test_host_missing_target_is_rejected_before_review(tmp_path):
    project, config_path, _runtime = _workspace(tmp_path)
    root = tmp_path / "missing-parent/host-codex"
    result = prepare_config_workflow(
        project,
        (WorkflowDeliveryTarget("codex", root),),
        config_path=config_path,
        confirm_host_workflow=lambda review: pytest.fail("missing target prompted"),
    )
    assert result.problems[0].identifier == "target-missing"
    assert not root.parent.exists()
    assert not workflow_module.HOST_WORKFLOW_TRUST_FILE.exists()
