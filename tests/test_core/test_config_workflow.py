from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import NoReturn

import pytest

import djinn_in_a_box.core.config_workflow as workflow_module
from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig, ConfigSyncSource
from djinn_in_a_box.core import workflow_publisher
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

    result = prepare_config_workflow(project, targets, config_path=config_path)

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
    assert prepare_config_workflow(project, targets, config_path=config_path).success
    assert manifest_path.read_bytes() == before


def test_preflight_reports_one_class_and_remedy_without_workflow_body(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    assert prepare_config_workflow(project, (target,), config_path=config_path).success
    sentinel = "PRIVATE-WORKFLOW-BODY"
    (project / "config/codex/AGENTS.md").write_text(sentinel)

    result = prepare_config_workflow(project, (target,), config_path=config_path)

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == DriftClass.TARGET_DRIFT.value
    assert problem.remedy
    assert sentinel not in repr(result)


def test_preflight_does_not_repair_a_missing_managed_canonical_file(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    assert prepare_config_workflow(project, config_path=config_path).success
    managed = project / "config/codex/AGENTS.md"
    managed.unlink()

    result = prepare_config_workflow(project, config_path=config_path)

    assert not result.success
    assert result.problems[0].identifier == DriftClass.TARGET_DRIFT.value
    assert not managed.exists()


def test_target_symlink_reports_the_destination_not_workflow_drift(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    managed_by_dotfiles = tmp_path / "dotfiles-claude"
    managed_by_dotfiles.mkdir()
    target = WorkflowDeliveryTarget("claude", tmp_path / ".claude", provision=True)
    target.destination_root.symlink_to(managed_by_dotfiles, target_is_directory=True)

    result = prepare_config_workflow(project, (target,), config_path=config_path)

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

    result = prepare_config_workflow(project, (target,), config_path=config_path)

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

    result = prepare_config_workflow(project, (target,), config_path=config_path)

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "target-provisioning-failed"
    assert str(target.destination_root) in problem.message
    assert "Permission denied" in problem.message
    assert "portable" not in problem.remedy


def test_missing_target_reports_the_destination_not_workflow_drift(tmp_path: Path) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "missing-codex")

    result = prepare_config_workflow(project, (target,), config_path=config_path)

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
        result = prepare_config_workflow(project, (target,), config_path=config_path)
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
    assert prepare_config_workflow(project, (target,), config_path=config_path).success

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

    result = prepare_config_workflow(project, (target,), config_path=config_path)

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
        result = prepare_config_workflow(project, (target,), config_path=config_path)
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

    result = prepare_config_workflow(project, (target,), config_path=config_path)

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

    result = prepare_config_workflow(project, (target,), config_path=config_path)

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
    assert prepare_config_workflow(project, config_path=config_path).success
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
        result = prepare_config_workflow(project, (target,), config_path=config_path)
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
    assert prepare_config_workflow(project, config_path=config_path).success
    audit = audit_config_sync(project, config_path=config_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)

    def fixed_audit(*_args: object, **_kwargs: object) -> ConfigSyncAudit:
        return audit

    def fail_acquisition(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(workflow_module, "audit_config_sync", fixed_audit)
    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_acquisition)
    result = prepare_config_workflow(project, (target,), config_path=config_path)

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
    result = prepare_config_workflow(project, (target,), config_path=config_path)

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
    assert prepare_config_workflow(project, config_path=config_path).success
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
    result = prepare_config_workflow(project, (target,), config_path=config_path)

    assert not result.success
    problem = result.problems[0]
    assert problem.identifier == "canonical-lock-failed"
    assert "Interrupted system call" in problem.message


def test_canonical_config_reload_os_error_is_not_reported_as_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    assert prepare_config_workflow(project, config_path=config_path).success
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
        result = prepare_config_workflow(project, (target,), config_path=config_path)
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
        assert prepare_config_workflow(project, config_path=config_path).success
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
    )

    assert not result.success
    assert result.problems[0].remedy == "Rebuild/recreate required."
    assert not (runtime / "codex").exists()


def test_runtime_publish_rechecks_source_after_delivery_view_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path, _runtime = _workspace(tmp_path)
    target = WorkflowDeliveryTarget("codex", tmp_path / "host-codex", provision=True)
    assert prepare_config_workflow(project, (target,), config_path=config_path).success
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
    result = prepare_config_workflow(project, (target,), config_path=config_path)

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
    assert prepare_config_workflow(project, (target,), config_path=config_path).success
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
    result = prepare_config_workflow(project, (target,), config_path=config_path)

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

    first = prepare_config_workflow(project, (target,), config_path=config_path)

    assert first.success, first.problems
    assert (target.destination_root / "AGENTS.md").read_text() == "shared workflow\n"

    # A second start after Claude re-synced its account skills and a new cache
    # appeared: still clean, and idempotent.
    _runtime_state(source_root, generation=1)
    second = prepare_config_workflow(project, (target,), config_path=config_path)

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
