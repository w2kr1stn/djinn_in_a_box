from __future__ import annotations

# pyright: reportPrivateUsage=false
import errno
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path, PurePosixPath
from typing import NoReturn

import pytest

from djinn_in_a_box.core import workflow_publisher
from djinn_in_a_box.core.workflow_publisher import (
    CANONICAL_MANIFEST_NAME,
    EXIT_CODES,
    PUBLISHER_LOCK_ERROR_PREFIX,
    PUBLISHER_WRITE_ERROR_PREFIX,
    RUNTIME_MANIFEST_NAME,
    CarrierFragment,
    DriftClass,
    PublishedFile,
    PublishError,
    WorkflowView,
    canonical_lock,
    publish_workflow_view,
    snapshot_file_view,
)

_p = PurePosixPath
_SCRIPT = Path(__file__).resolve().parents[2] / "src/djinn_in_a_box/core/workflow_publisher.py"


def _file(path: str, content: bytes, *, executable: bool = False) -> PublishedFile:
    return PublishedFile(_p(path), content, executable)


def _fragment(path: str, key: tuple[str, ...], value: object) -> CarrierFragment:
    return CarrierFragment(_p(path), key, json.dumps(value).encode())


def _view(
    marker: bytes = b"one\n",
    *,
    files: tuple[PublishedFile, ...] = (),
    fragments: tuple[CarrierFragment, ...] = (),
    target_tool: str | None = None,
) -> WorkflowView:
    return WorkflowView(
        "claude",
        (_file("AGENTS.md", marker), *files),
        fragments,
        target_tool=target_tool,
    )


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    canonical.mkdir()
    target.mkdir()
    return canonical, target


def _publish(canonical: Path, target: Path, view: WorkflowView) -> workflow_publisher.PublishResult:
    return publish_workflow_view(view, canonical, target, target / RUNTIME_MANIFEST_NAME)


def _write(path: Path, content: bytes, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not path.stat().st_mode & stat.S_IWUSR:
        path.chmod(path.stat().st_mode | stat.S_IWUSR)  # a sealed runtime file
    path.write_bytes(content)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _no_target_mutation(_count: int) -> None:
    return None


def _lease_reacquired(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("lease was acquired twice")


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _hold_canonical_lock(root: str, ready: ProcessEvent, release: ProcessEvent) -> None:
    with canonical_lock(Path(root), exclusive=True):
        ready.set()
        assert release.wait(10)


def _publish_worker(canonical: str, target: str, marker: bytes, manifest_name: str) -> None:
    result = publish_workflow_view(
        _view(marker),
        Path(canonical),
        Path(target),
        Path(target) / manifest_name,
    )
    if not result.success:
        raise RuntimeError(result.drift_class.value)


def test_publish_writes_strict_manifest_and_is_idempotent(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    executable = _file("scripts/check.py", b"#!/usr/bin/env python3\n", executable=True)

    first = _publish(canonical, target, _view(files=(executable,)))
    manifest = json.loads((target / RUNTIME_MANIFEST_NAME).read_text())
    before = _tree(target)
    second = _publish(canonical, target, _view(files=(executable,)))

    assert first.success
    assert first.changed_paths == (_p("AGENTS.md"), _p("scripts/check.py"))
    assert set(manifest) == {"source", "items"}
    assert manifest["source"] == "claude"
    assert all(set(item) == {"path", "content_hash", "executable"} for item in manifest["items"])
    assert second.success
    assert second.changed_paths == ()
    assert _tree(target) == before


def test_unmanaged_collision_and_managed_edit_fail_closed(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    _write(target / "AGENTS.md", b"operator-owned\n")
    before_collision = _tree(target)

    collision = _publish(canonical, target, _view())

    assert collision.drift_class is DriftClass.COLLISION
    assert _tree(target) == before_collision
    (target / "AGENTS.md").unlink()
    assert _publish(canonical, target, _view()).success
    _write(target / "AGENTS.md", b"operator edit\n")
    before_edit = _tree(target)

    drift = _publish(canonical, target, _view(b"two\n"))

    assert drift.drift_class is DriftClass.TARGET_DRIFT
    assert _tree(target) == before_edit


@pytest.mark.parametrize("target_tool", ("claude", "codex", "opencode"))
def test_zero_byte_native_agents_file_is_a_collision(
    tmp_path: Path, target_tool: str
) -> None:
    canonical, target = _roots(tmp_path)
    _write(target / "AGENTS.md", b"")
    before = _tree(target)

    result = _publish(canonical, target, _view(target_tool=target_tool))

    assert result.drift_class is DriftClass.COLLISION
    assert _tree(target) == before


def test_claude_root_hook_paths_round_trip(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    security = b"security\n"
    ready = b"ready\n"
    view = _view(
        files=(
            _file("security_reminder_hook.py", security),
            _file("ready_notify_hook.py", ready),
        ),
        target_tool="claude",
    )

    assert _publish(canonical, target, view).success
    assert _publish(canonical, target, view).success


@pytest.mark.parametrize("foreign_kind", ("file", "fragment"))
def test_runtime_state_rejects_unowned_manifest_items_without_mutation(
    tmp_path: Path, foreign_kind: str
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view()).success
    manifest_path = target / RUNTIME_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    if foreign_kind == "file":
        operator_file = target / "operator-private.txt"
        operator_file.write_bytes(b"operator-owned\n")
        manifest["items"].append(
            {
                "path": "operator-private.txt",
                "content_hash": workflow_publisher._digest(operator_file.read_bytes()),  # pyright: ignore[reportPrivateUsage]
                "executable": False,
            }
        )
    else:
        carrier = target / "settings.json"
        carrier.write_bytes(b'{"operator":{"keep":true}}\n')
        manifest["items"].append(
            {
                "path": "settings.json",
                "key_path": ["operator", "keep"],
                "content_hash": workflow_publisher._value_digest(True),  # pyright: ignore[reportPrivateUsage]
                "executable": False,
            }
        )
    _write(manifest_path, json.dumps(manifest).encode())
    before = _tree(target)

    result = _publish(canonical, target, _view())

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert _tree(target) == before


def test_stale_file_and_owned_json_key_are_removed_without_touching_neighbor(
    tmp_path: Path,
) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(
        files=(_file("agents/obsolete.md", b"obsolete\n"),),
        fragments=(
            _fragment("settings.json", ("hooks", "Stop"), ["old"]),
            _fragment("settings.json", ("hooks", "PreToolUse"), ["remove"]),
        ),
    )
    assert _publish(canonical, target, first).success
    carrier = json.loads((target / "settings.json").read_text())
    carrier["operator"] = {"keep": True}
    (target / "settings.json").write_text(json.dumps(carrier))

    result = _publish(
        canonical,
        target,
        _view(fragments=(_fragment("settings.json", ("hooks", "Stop"), ["new"]),)),
    )
    updated = json.loads((target / "settings.json").read_text())

    assert result.success
    assert result.removed_paths == (_p("agents/obsolete.md"),)
    assert not (target / "agents/obsolete.md").exists()
    assert updated["hooks"] == {"Stop": ["new"]}
    assert updated["operator"] == {"keep": True}


def test_missing_managed_file_blocks_without_reconstruction(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    view = _view(files=(_file("agents/reviewer.md", b"review\n"),))
    assert _publish(canonical, target, view).success
    managed = target / "agents/reviewer.md"
    managed.unlink()
    before = _tree(target)

    result = _publish(canonical, target, view)

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert _tree(target) == before


def test_missing_managed_carrier_key_blocks_without_reconstruction(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    view = _view(fragments=(_fragment("settings.json", ("hooks", "Stop"), ["ready"]),))
    assert _publish(canonical, target, view).success
    carrier = json.loads((target / "settings.json").read_text())
    del carrier["hooks"]["Stop"]
    (target / "settings.json").write_text(json.dumps(carrier))
    before = _tree(target)

    result = _publish(canonical, target, view)

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert _tree(target) == before


def test_json_neighbors_are_preserved_semantically(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    original = {"operator": {"values": [1, {"keep": True}]}, "enabled": False}
    (target / "settings.json").write_text(json.dumps(original, indent=1))

    result = _publish(
        canonical,
        target,
        _view(fragments=(_fragment("settings.json", ("hooks", "Stop"), {"command": "ready"}),)),
    )
    updated = json.loads((target / "settings.json").read_text())

    assert result.success
    assert updated["operator"] == original["operator"]
    assert updated["enabled"] is False
    assert updated["hooks"] == {"Stop": {"command": "ready"}}


def test_toml_splice_keeps_neighbors_byte_identical_and_inserts_before_tables(
    tmp_path: Path,
) -> None:
    canonical, target = _roots(tmp_path)
    original = (
        b"# operator comment\n"
        b'title = "Djinn"\n'
        b"release = 2026-07-17\n"
        b'inline = { enabled = true, labels = ["private"] }\n'
        b"\n[tool.operator]\n"
        b'comment = "keep every byte"\n'
    )
    _write(target / "config.toml", original)
    first = _view(
        fragments=(_fragment("config.toml", ("project_doc_fallback_filenames",), ["CLAUDE.md"]),),
        target_tool="codex",
    )

    assert _publish(canonical, target, first).success
    inserted = (target / "config.toml").read_bytes()
    assignment = b'project_doc_fallback_filenames = ["CLAUDE.md"]\n'
    parsed = tomllib.loads(inserted.decode())

    assert parsed["project_doc_fallback_filenames"] == ["CLAUDE.md"]
    assert inserted.replace(assignment, b"") == original
    assert inserted.index(assignment) < inserted.index(b"[tool.operator]")
    second = _view(
        fragments=(_fragment("config.toml", ("project_doc_fallback_filenames",), ["AGENTS.md"]),),
        target_tool="codex",
    )
    assert _publish(canonical, target, second).success
    replaced = (target / "config.toml").read_bytes()

    assert tomllib.loads(replaced.decode())["project_doc_fallback_filenames"] == ["AGENTS.md"]
    assert replaced.replace(b'project_doc_fallback_filenames = ["AGENTS.md"]\n', b"") == original


def test_executable_mode_is_published_and_mode_only_drift_blocks(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    view = _view(files=(_file("scripts/run", b"#!/bin/sh\n", executable=True),))

    assert _publish(canonical, target, view).success
    script = target / "scripts/run"
    assert script.stat().st_mode & stat.S_IXUSR
    script.chmod(script.stat().st_mode & ~stat.S_IXUSR)

    result = _publish(canonical, target, view)

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert not script.stat().st_mode & stat.S_IXUSR


def test_source_change_before_commit_blocks_without_target_mutation(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    source = tmp_path / "view"
    source.mkdir()
    _write(source / "AGENTS.md", b"snapshot\n")
    view = snapshot_file_view(source, source="claude")
    _write(source / "AGENTS.md", b"edited after snapshot\n")

    result = publish_workflow_view(
        view,
        canonical,
        target,
        target / RUNTIME_MANIFEST_NAME,
        source_root=source,
    )

    assert result.drift_class is DriftClass.SOURCE_CHANGED
    assert _tree(target) == {}


def test_source_change_at_commit_point_blocks_without_target_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    source = tmp_path / "view"
    source.mkdir()
    _write(source / "AGENTS.md", b"snapshot\n")
    view = snapshot_file_view(source, source="claude")
    monkeypatch.setattr(
        workflow_publisher,
        "_before_target_commit",
        lambda: _write(source / "AGENTS.md", b"edited at commit point\n"),
    )

    result = publish_workflow_view(
        view,
        canonical,
        target,
        target / RUNTIME_MANIFEST_NAME,
        source_root=source,
    )

    assert result.drift_class is DriftClass.SOURCE_CHANGED
    assert _tree(target) == {}


def test_source_change_after_commit_point_finishes_frozen_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    source = tmp_path / "view"
    source.mkdir()
    _write(source / "AGENTS.md", b"frozen generation\n")
    view = snapshot_file_view(source, source="claude")

    def edit_source_after_first_target_mutation(count: int) -> None:
        if count == 1:
            _write(source / "AGENTS.md", b"next generation\n")

    monkeypatch.setattr(
        workflow_publisher,
        "_after_target_mutation",
        edit_source_after_first_target_mutation,
    )
    committed = publish_workflow_view(
        view,
        canonical,
        target,
        target / RUNTIME_MANIFEST_NAME,
        source_root=source,
    )
    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", _no_target_mutation)

    next_audit = publish_workflow_view(
        view,
        canonical,
        target,
        target / RUNTIME_MANIFEST_NAME,
        source_root=source,
    )

    assert committed.success
    assert (target / "AGENTS.md").read_bytes() == b"frozen generation\n"
    assert (target / RUNTIME_MANIFEST_NAME).is_file()
    assert next_audit.drift_class is DriftClass.SOURCE_CHANGED


def test_recovery_accepts_old_or_new_residue_and_manifest_is_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(
        files=(_file("agents/obsolete.md", b"old\n"),),
        fragments=(
            _fragment("settings.json", ("hooks", "Stop"), ["old"]),
            _fragment("settings.json", ("hooks", "PreToolUse"), ["remove"]),
        ),
    )
    assert _publish(canonical, target, first).success
    manifest_before = (target / RUNTIME_MANIFEST_NAME).read_bytes()
    second = _view(
        b"two\n",
        fragments=(_fragment("settings.json", ("hooks", "Stop"), ["new"]),),
    )

    def abort_after_second_mutation(count: int) -> None:
        if count == 2:
            raise RuntimeError("injected abort")

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", abort_after_second_mutation)
    with pytest.raises(RuntimeError, match="injected abort"):
        _publish(canonical, target, second)
    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", _no_target_mutation)

    assert (target / "AGENTS.md").read_bytes() == b"two\n"
    assert json.loads((target / "settings.json").read_text())["hooks"] == {"Stop": ["new"]}
    assert (target / "agents/obsolete.md").exists()
    assert (target / RUNTIME_MANIFEST_NAME).read_bytes() == manifest_before
    recovered = _publish(canonical, target, second)

    assert recovered.success
    assert not (target / "agents/obsolete.md").exists()
    assert json.loads((target / "settings.json").read_text())["hooks"] == {"Stop": ["new"]}


def test_source_edit_between_crash_and_retry_blocks_without_more_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view()).success
    source = tmp_path / "view"
    source.mkdir()
    _write(source / "AGENTS.md", b"new generation\n")
    view = snapshot_file_view(source, source="claude")

    def abort_after_first_mutation(count: int) -> None:
        if count == 1:
            raise RuntimeError("injected abort")

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", abort_after_first_mutation)
    with pytest.raises(RuntimeError, match="injected abort"):
        publish_workflow_view(
            view,
            canonical,
            target,
            target / RUNTIME_MANIFEST_NAME,
            source_root=source,
        )
    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", _no_target_mutation)
    partial = _tree(target)
    _write(source / "AGENTS.md", b"operator edit\n")

    blocked = publish_workflow_view(
        view,
        canonical,
        target,
        target / RUNTIME_MANIFEST_NAME,
        source_root=source,
    )

    assert blocked.drift_class is DriftClass.SOURCE_CHANGED
    assert _tree(target) == partial


def test_lock_contention_blocks_until_canonical_lock_releases(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_canonical_lock,
        args=(str(canonical), ready, release),
    )
    holder.start()
    assert ready.wait(10)
    publisher = multiprocessing.Process(
        target=_publish_worker,
        args=(str(canonical), str(target), b"blocked then published\n", RUNTIME_MANIFEST_NAME),
    )
    publisher.start()
    publisher.join(0.25)

    assert publisher.is_alive()
    release.set()
    holder.join(10)
    publisher.join(10)
    assert holder.exitcode == 0
    assert publisher.exitcode == 0
    assert (target / "AGENTS.md").read_bytes() == b"blocked then published\n"


def test_canonical_lock_wraps_unlock_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, _target = _roots(tmp_path)
    original_flock = workflow_publisher.fcntl.flock

    def fail_unlock(descriptor: int, operation: int) -> None:
        if operation == workflow_publisher.fcntl.LOCK_UN:
            raise OSError(errno.EINTR, "Interrupted system call")
        original_flock(descriptor, operation)

    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_unlock)

    with pytest.raises(PublishError) as exc_info, canonical_lock(canonical, exclusive=False):
        pass

    assert exc_info.value.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.__cause__.errno == errno.EINTR


def test_canonical_lock_wraps_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, _target = _roots(tmp_path)
    original_open = workflow_publisher._open_directory
    original_close = workflow_publisher.os.close
    descriptor: int | None = None

    def record_descriptor(path: Path) -> int:
        nonlocal descriptor
        descriptor = original_open(path)
        return descriptor

    def fail_close(candidate: int) -> None:
        if candidate == descriptor:
            raise OSError(errno.EIO, "Input/output error")
        original_close(candidate)

    monkeypatch.setattr(workflow_publisher, "_open_directory", record_descriptor)
    monkeypatch.setattr(workflow_publisher.os, "close", fail_close)
    try:
        with pytest.raises(PublishError) as exc_info, canonical_lock(canonical, exclusive=False):
            pass
    finally:
        assert descriptor is not None
        original_close(descriptor)

    assert exc_info.value.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.__cause__.errno == errno.EIO


def test_target_lock_wraps_acquisition_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _canonical, target = _roots(tmp_path)

    def fail_acquisition(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_acquisition)

    with pytest.raises(PublishError) as exc_info, workflow_publisher._target_lock(target):
        pass

    assert exc_info.value.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert exc_info.value.lock_error is exc_info.value.__cause__
    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.__cause__.errno == errno.ENOLCK


def test_target_lock_wraps_unlock_failure_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _canonical, target = _roots(tmp_path)
    original_open = workflow_publisher._open_directory
    original_flock = workflow_publisher.fcntl.flock
    descriptor: int | None = None

    def record_descriptor(path: Path) -> int:
        nonlocal descriptor
        descriptor = original_open(path)
        return descriptor

    def fail_unlock(candidate: int, operation: int) -> None:
        if candidate == descriptor and operation == workflow_publisher.fcntl.LOCK_UN:
            raise OSError(errno.EINTR, "Interrupted system call")
        original_flock(candidate, operation)

    monkeypatch.setattr(workflow_publisher, "_open_directory", record_descriptor)
    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_unlock)

    with pytest.raises(PublishError) as exc_info, workflow_publisher._target_lock(target):
        pass

    assert descriptor is not None
    with pytest.raises(OSError) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF
    assert exc_info.value.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert exc_info.value.lock_error is exc_info.value.__cause__
    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.__cause__.errno == errno.EINTR


def test_parallel_canonical_and_runtime_publish_complete_without_deadlock(tmp_path: Path) -> None:
    canonical, runtime = _roots(tmp_path)
    canonical_publisher = multiprocessing.Process(
        target=_publish_worker,
        args=(str(canonical), str(canonical), b"new generation\n", CANONICAL_MANIFEST_NAME),
    )
    runtime_publisher = multiprocessing.Process(
        target=_publish_worker,
        args=(str(canonical), str(runtime), b"new generation\n", RUNTIME_MANIFEST_NAME),
    )
    canonical_publisher.start()
    runtime_publisher.start()
    canonical_publisher.join(10)
    runtime_publisher.join(10)

    assert canonical_publisher.exitcode == 0
    assert runtime_publisher.exitcode == 0
    assert (canonical / "AGENTS.md").read_bytes() == b"new generation\n"
    assert (runtime / "AGENTS.md").read_bytes() == b"new generation\n"


def test_canonical_target_requires_exclusive_mode_and_inherits_held_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, _target = _roots(tmp_path)
    with canonical_lock(canonical, exclusive=False) as shared_lease:
        blocked = publish_workflow_view(
            _view(),
            canonical,
            canonical,
            canonical / CANONICAL_MANIFEST_NAME,
            canonical_lease=shared_lease,
        )
    with canonical_lock(canonical, exclusive=True) as exclusive_lease:
        monkeypatch.setattr(
            workflow_publisher,
            "canonical_lock",
            _lease_reacquired,
        )
        inherited = publish_workflow_view(
            _view(),
            canonical,
            canonical,
            canonical / CANONICAL_MANIFEST_NAME,
            canonical_lease=exclusive_lease,
        )

    assert blocked.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert inherited.success
    assert (canonical / CANONICAL_MANIFEST_NAME).is_file()


def _canonical_identity(
    root: Path,
    source: str = "opencode",
    *,
    files: Mapping[str, tuple[bytes, bool]] | None = None,
) -> None:
    items = [
        {
            "path": f"opencode/{path}",
            "content_hash": workflow_publisher._digest(content),
            "executable": executable,
        }
        for path, (content, executable) in sorted((files or {}).items())
    ]
    (root / CANONICAL_MANIFEST_NAME).write_text(json.dumps({"source": source, "items": items}))


def _run_cli(
    view: Path, canonical: Path, target: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--view",
            str(view),
            "--canonical-root",
            str(canonical),
            "--target",
            str(target),
            "--manifest",
            str(target / RUNTIME_MANIFEST_NAME),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_standalone_cli_reports_flock_acquisition_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canonical, target = _roots(tmp_path)
    view = tmp_path / "view"
    view.mkdir()
    sentinel = "PRIVATE-WORKFLOW-BODY-SENTINEL"
    _write(view / "AGENTS.md", sentinel.encode())

    def fail_acquisition(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_acquisition)
    code = workflow_publisher.main(
        (
            "--view",
            str(view),
            "--canonical-root",
            str(canonical),
            "--target",
            str(target),
            "--manifest",
            str(target / RUNTIME_MANIFEST_NAME),
        )
    )
    captured = capsys.readouterr()

    assert code == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert captured.err.splitlines()[0] == "workflow publisher: invalid-or-semantic"
    diagnostic = json.loads(captured.err.splitlines()[1].removeprefix(PUBLISHER_LOCK_ERROR_PREFIX))
    assert diagnostic == {"root": str(canonical), "error": "No locks available"}
    assert sentinel not in captured.err
    assert "Traceback" not in captured.err


def test_standalone_cli_reports_canonical_unlock_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canonical, target = _roots(tmp_path)
    view = tmp_path / "view"
    view.mkdir()
    _canonical_identity(canonical)
    sentinel = "PRIVATE-WORKFLOW-BODY-SENTINEL"
    _write(view / "AGENTS.md", sentinel.encode())
    original_open = workflow_publisher._open_directory
    original_flock = workflow_publisher.fcntl.flock
    descriptor: int | None = None

    def record_canonical_descriptor(path: Path) -> int:
        nonlocal descriptor
        candidate = original_open(path)
        if path == canonical:
            descriptor = candidate
        return candidate

    def fail_canonical_unlock(candidate: int, operation: int) -> None:
        if candidate == descriptor and operation == workflow_publisher.fcntl.LOCK_UN:
            raise OSError(errno.EINTR, "Interrupted system call")
        original_flock(candidate, operation)

    monkeypatch.setattr(workflow_publisher, "_open_directory", record_canonical_descriptor)
    monkeypatch.setattr(workflow_publisher.fcntl, "flock", fail_canonical_unlock)
    code = workflow_publisher.main(
        (
            "--view",
            str(view),
            "--canonical-root",
            str(canonical),
            "--target",
            str(target),
            "--manifest",
            str(target / RUNTIME_MANIFEST_NAME),
        )
    )
    captured = capsys.readouterr()

    assert descriptor is not None
    with pytest.raises(OSError) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF
    assert code == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert captured.err.splitlines()[0] == "workflow publisher: invalid-or-semantic"
    diagnostic = json.loads(captured.err.splitlines()[1].removeprefix(PUBLISHER_LOCK_ERROR_PREFIX))
    assert diagnostic == {"root": str(canonical), "error": "Interrupted system call"}
    assert sentinel not in captured.err
    assert "Traceback" not in captured.err


def test_standalone_file_only_publish_exit_codes_fragment_refusal_and_missing_canonical(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    view = tmp_path / "view"
    canonical.mkdir()
    target.mkdir()
    view.mkdir()
    _canonical_identity(canonical)
    _write(view / "AGENTS.md", b"cli view\n")

    published = _run_cli(view, canonical, target)
    _write(target / "AGENTS.md", b"operator edit\n")
    drift = _run_cli(view, canonical, target)
    collision_target = tmp_path / "collision"
    collision_target.mkdir()
    _write(collision_target / "AGENTS.md", b"unmanaged\n")
    collision = _run_cli(view, canonical, collision_target)
    refused = _run_cli(view, canonical, target, "--fragment", "settings.json:hooks")
    missing_target = tmp_path / "missing-target"
    missing_target.mkdir()
    missing_before = _tree(missing_target)
    missing = _run_cli(view, tmp_path / "missing-canonical", missing_target)

    assert EXIT_CODES == {
        DriftClass.CLEAN: 0,
        DriftClass.SOURCE_CHANGED: 10,
        DriftClass.TARGET_DRIFT: 11,
        DriftClass.COLLISION: 12,
        DriftClass.INVALID_OR_SEMANTIC: 13,
    }
    assert published.returncode == EXIT_CODES[DriftClass.CLEAN]
    assert (target / "AGENTS.md").read_bytes() == b"operator edit\n"
    assert drift.returncode == EXIT_CODES[DriftClass.TARGET_DRIFT]
    assert collision.returncode == EXIT_CODES[DriftClass.COLLISION]
    assert refused.returncode == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert missing.returncode == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert _tree(missing_target) == missing_before
    sentinel = "operator edit"
    assert sentinel not in drift.stderr
    assert sentinel not in collision.stderr


def test_standalone_write_failure_reports_destination_io_without_workflow_content(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    view = tmp_path / "view"
    canonical.mkdir()
    target.mkdir()
    view.mkdir()
    sentinel = "PRIVATE-WORKFLOW-BODY-SENTINEL"
    _canonical_identity(canonical)
    _write(view / "AGENTS.md", sentinel.encode())
    original_mode = stat.S_IMODE(target.stat().st_mode)
    target.chmod(0o555)
    try:
        result = _run_cli(view, canonical, target)
    finally:
        target.chmod(original_mode)

    assert result.returncode == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert result.stderr.splitlines()[0] == "workflow publisher: invalid-or-semantic"
    diagnostic_line = result.stderr.splitlines()[1]
    diagnostic = json.loads(diagnostic_line.removeprefix(PUBLISHER_WRITE_ERROR_PREFIX))
    assert diagnostic == {"destination": str(target), "error": "Permission denied"}
    assert sentinel not in result.stderr


@pytest.mark.parametrize("foreign_path", ("operator-private.txt", "commands/codex-review.md"))
def test_opencode_cli_profile_rejects_foreign_seed_files(tmp_path: Path, foreign_path: str) -> None:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    view = tmp_path / "view"
    canonical.mkdir()
    target.mkdir()
    view.mkdir()
    instructions = b"OpenCode instructions\n"
    agent = b"---\nname: reviewer\n---\nReview\n"
    _write(view / "AGENTS.md", instructions)
    _write(view / "agents/reviewer.md", agent)
    plugins = {
        "plugins/session-start-status.js": b"const x = {}; export { x as default };\n",
        "plugins/security-reminder.js": b"export default { id: 'fixture', setup() {} };\n",
        "plugins/ready-notify.js": b"export default { id: 'fixture', setup() {} };\n",
    }
    for path, content in plugins.items():
        _write(view / path, content)
    _canonical_identity(
        canonical,
        files={
            "AGENTS.md": (instructions, False),
            "agents/reviewer.md": (agent, False),
            **{path: (content, False) for path, content in plugins.items()},
        },
    )
    _write(view / foreign_path, b"foreign\n")
    _write(target / "personal.json", b'{"keep":true}\n')
    before = _tree(target)

    result = _run_cli(view, canonical, target, "--profile", "opencode")

    assert result.returncode == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert _tree(target) == before


def test_opencode_cli_blocks_seed_content_that_differs_from_canonical_manifest(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    view = tmp_path / "view"
    canonical.mkdir()
    target.mkdir()
    view.mkdir()
    _canonical_identity(canonical, files={"AGENTS.md": (b"canonical\n", False)})
    _write(view / "AGENTS.md", b"manipulated seed\n")
    before = _tree(target)

    result = _run_cli(view, canonical, target, "--profile", "opencode")

    assert result.returncode == EXIT_CODES[DriftClass.SOURCE_CHANGED]
    assert _tree(target) == before


@pytest.mark.parametrize(
    "export_clause",
    (b"export {p as default}", b"export{p as default}", b"export\n{p as default}"),
    ids=("spaced", "compact", "newline"),
)
def test_opencode_cli_delivers_native_plugins_not_managed_by_canonical_manifest(
    tmp_path: Path,
    export_clause: bytes,
) -> None:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    seed = tmp_path / "seed"
    canonical.mkdir()
    target.mkdir()
    seed.mkdir()
    instructions = b"OpenCode instructions\n"
    plugins = {
        "plugins/session-start-status.js": b"const p = {}; " + export_clause + b";\n",
        "plugins/security-reminder.js": b"export default { id: 'fixture', setup() {} };\n",
        "plugins/ready-notify.js": b"export default { id: 'fixture', setup() {} };\n",
    }
    _canonical_identity(canonical, files={"AGENTS.md": (instructions, False)})
    _write(seed / "AGENTS.md", instructions)
    for path, content in plugins.items():
        _write(seed / path, content)

    first = _run_cli(seed, canonical, target, "--profile", "opencode")
    after_first = _tree(target)
    second = _run_cli(seed, canonical, target, "--profile", "opencode")
    state = json.loads((target / RUNTIME_MANIFEST_NAME).read_text())

    assert first.returncode == EXIT_CODES[DriftClass.CLEAN]
    assert second.returncode == EXIT_CODES[DriftClass.CLEAN]
    assert _tree(target) == after_first
    assert {
        item["path"] for item in state["items"]
    } >= {PurePosixPath(path).as_posix() for path in plugins}
    for path, content in plugins.items():
        assert (target / path).read_bytes() == content

    tracked_plugin = target / "plugins/ready-notify.js"
    _write(
        tracked_plugin, b"export default { id: 'fixture', setup() { return () => {}; } };\n"
    )
    before_drift = _tree(target)
    drift = _run_cli(seed, canonical, target, "--profile", "opencode")

    assert drift.returncode == EXIT_CODES[DriftClass.TARGET_DRIFT]
    assert _tree(target) == before_drift


def test_opencode_cli_rejects_untracked_non_native_seed_file(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    seed = tmp_path / "seed"
    canonical.mkdir()
    target.mkdir()
    seed.mkdir()
    instructions = b"OpenCode instructions\n"
    _canonical_identity(canonical, files={"AGENTS.md": (instructions, False)})
    _write(seed / "AGENTS.md", instructions)
    _write(seed / "plugins/ready-notify.js", b"export default { id: 'fixture', setup() {} };\n")
    _write(seed / "commands/untracked.md", b"---\nname: untracked\n---\nUntracked\n")
    _write(target / "personal.json", b'{"keep":true}\n')
    before = _tree(target)

    result = _run_cli(seed, canonical, target, "--profile", "opencode")

    assert result.returncode == EXIT_CODES[DriftClass.SOURCE_CHANGED]
    assert _tree(target) == before


@pytest.mark.parametrize(
    "content",
    [
        b"const plugin = () => ({});",
        b"export const Plugin = () => ({});",
        b"exportdefault { id: 'fixture', setup() {} };",
    ],
)
def test_opencode_cli_rejects_invalid_native_plugin_before_runtime_mutation(
    tmp_path: Path, content: bytes
) -> None:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    seed = tmp_path / "seed"
    canonical.mkdir()
    target.mkdir()
    seed.mkdir()
    _canonical_identity(canonical, files={"AGENTS.md": (b"OpenCode instructions\n", False)})
    _write(seed / "AGENTS.md", b"OpenCode instructions\n")
    _write(seed / "plugins/ready-notify.js", content)
    _write(target / "personal.json", b'{"keep":true}\n')
    before = _tree(target)

    result = _run_cli(seed, canonical, target, "--profile", "opencode")

    assert result.returncode == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert _tree(target) == before


# --- #166: named items, sealing, check-only, mutation-boundary recheck, CLI items ---

_ITEMS_PREFIX = "workflow publisher drift-items: "
_ITEM_PREFIX = "workflow publisher item: "
_CAP = 200


def _entry(
    kind: DriftClass,
    path: str,
    key: tuple[str, ...] | None = None,
    origin: str = "target",
    reason: str | None = None,
) -> workflow_publisher.DriftEntry:
    return workflow_publisher.DriftEntry(kind, _p(path), key, origin, reason)


def _inspect(canonical: Path, target: Path, view: WorkflowView) -> workflow_publisher.PublishResult:
    return workflow_publisher.inspect_workflow_view(
        view, canonical, target, target / RUNTIME_MANIFEST_NAME
    )


def _run(
    mode: str, canonical: Path, target: Path, view: WorkflowView
) -> workflow_publisher.PublishResult:
    return (_publish if mode == "publish" else _inspect)(canonical, target, view)


def _state(root: Path) -> list[tuple[str, str, int, bytes]]:
    """Every entry below `root`: path, kind, mode and bytes (directory entries included)."""
    entries: list[tuple[str, str, int, bytes]] = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        kind = "dir" if stat.S_ISDIR(info.st_mode) else "link" if path.is_symlink() else "file"
        content = path.read_bytes() if kind == "file" else b""
        entries.append(
            (path.relative_to(root).as_posix(), kind, stat.S_IMODE(info.st_mode), content)
        )
    return entries


MODES = ("publish", "inspect")


@pytest.mark.parametrize("mode", MODES)
def test_whole_file_drift_names_the_file(tmp_path: Path, mode: str) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success
    _write(target / "AGENTS.md", b"operator edit\n")

    result = _run(mode, canonical, target, _view(b"two\n"))

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert result.drift_items == (_entry(DriftClass.TARGET_DRIFT, "AGENTS.md"),)


@pytest.mark.parametrize("mode", MODES)
def test_carrier_key_drift_names_carrier_and_key_only(tmp_path: Path, mode: str) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(
        fragments=(
            _fragment("settings.json", ("hooks", "Stop"), ["old"]),
            _fragment("settings.json", ("hooks", "PreToolUse"), ["keep"]),
        )
    )
    assert _publish(canonical, target, first).success
    carrier = json.loads((target / "settings.json").read_text())
    carrier["hooks"]["Stop"] = ["edited"]
    carrier["operator"] = True
    (target / "settings.json").write_text(json.dumps(carrier))

    result = _run(mode, canonical, target, first)

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert result.drift_items == (
        _entry(DriftClass.TARGET_DRIFT, "settings.json", ("hooks", "Stop")),
    )


@pytest.mark.parametrize("mode", MODES)
def test_missing_and_retired_files_are_named(tmp_path: Path, mode: str) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(files=(_file("agents/gone.md", b"gone\n"), _file("agents/kept.md", b"kept\n")))
    assert _publish(canonical, target, first).success
    (target / "agents/kept.md").unlink()
    _write(target / "agents/gone.md", b"edited retired\n")
    second = _view(files=(_file("agents/kept.md", b"kept\n"),))

    result = _run(mode, canonical, target, second)

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert set(result.drift_items) == {
        _entry(DriftClass.TARGET_DRIFT, "agents/kept.md"),
        _entry(DriftClass.TARGET_DRIFT, "agents/gone.md"),
    }


@pytest.mark.parametrize("mode", MODES)
def test_non_regular_entry_and_unmanaged_collision_are_named(tmp_path: Path, mode: str) -> None:
    canonical, target = _roots(tmp_path)
    (target / "AGENTS.md").mkdir()
    _write(target / "agents/other.md", b"unmanaged\n")
    view = _view(files=(_file("agents/other.md", b"wanted\n"),))

    result = _run(mode, canonical, target, view)

    assert result.drift_class is DriftClass.COLLISION
    assert result.drift_items == (_entry(DriftClass.COLLISION, "AGENTS.md"),)
    (target / "AGENTS.md").rmdir()
    again = _run(mode, canonical, target, view)
    assert again.drift_items == (_entry(DriftClass.COLLISION, "agents/other.md"),)


def test_only_items_of_the_worst_class_are_listed(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(files=(_file("agents/a.md", b"a\n"),))
    assert _publish(canonical, target, first).success
    _write(target / "agents/a.md", b"drifted\n")
    _write(target / "agents/b.md", b"unmanaged\n")

    result = _publish(
        canonical,
        target,
        _view(files=(_file("agents/a.md", b"a2\n"), _file("agents/b.md", b"b\n"))),
    )

    assert result.drift_class is DriftClass.COLLISION
    assert result.drift_items == (_entry(DriftClass.COLLISION, "agents/b.md"),)


@pytest.mark.parametrize("mode", MODES)
def test_malformed_and_linked_manifests_name_the_manifest(tmp_path: Path, mode: str) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view()).success
    manifest = target / RUNTIME_MANIFEST_NAME
    _write(manifest, b"{not json")

    malformed = _run(mode, canonical, target, _view())

    manifest.unlink()
    manifest.symlink_to(tmp_path / "elsewhere")
    linked = _run(mode, canonical, target, _view())
    assert malformed.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert malformed.drift_items == (
        _entry(DriftClass.INVALID_OR_SEMANTIC, RUNTIME_MANIFEST_NAME, origin="target-manifest"),
    )
    assert linked.drift_class is DriftClass.COLLISION
    assert linked.drift_items == (
        _entry(DriftClass.COLLISION, RUNTIME_MANIFEST_NAME, origin="target-manifest"),
    )


def test_carrier_parse_failure_names_the_carrier(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    view = _view(fragments=(_fragment("settings.json", ("hooks", "Stop"), ["x"]),))
    _write(target / "settings.json", b"{broken")

    result = _publish(canonical, target, view)

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert result.drift_items == (_entry(DriftClass.INVALID_OR_SEMANTIC, "settings.json"),)


def test_publish_error_keeps_its_items_and_defaults_to_none() -> None:
    item = _entry(DriftClass.COLLISION, "x.md")

    assert PublishError(DriftClass.COLLISION, items=[item]).items == (item,)
    assert PublishError(DriftClass.COLLISION).items == ()
    assert workflow_publisher.PublishResult(DriftClass.CLEAN).drift_items == ()


# --- sealing ---


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_runtime_files_and_manifest_are_sealed_and_carriers_keep_their_mode(
    tmp_path: Path,
) -> None:
    canonical, target = _roots(tmp_path)
    view = _view(
        files=(_file("scripts/run.sh", b"#!/bin/sh\n", executable=True),),
        fragments=(_fragment("settings.json", ("hooks", "Stop"), ["x"]),),
    )

    assert _publish(canonical, target, view).success

    assert _mode(target / "AGENTS.md") == 0o444
    assert _mode(target / "scripts/run.sh") == 0o555
    assert _mode(target / RUNTIME_MANIFEST_NAME) == 0o444
    assert _mode(target / "settings.json") == 0o644


def test_canonical_target_keeps_the_default_modes(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    view = WorkflowView(
        "claude",
        (
            _file("codex/AGENTS.md", b"one\n"),
            _file("codex/scripts/x.sh", b"#!/bin/sh\n", executable=True),
        ),
    )

    result = publish_workflow_view(view, canonical, canonical, canonical / CANONICAL_MANIFEST_NAME)

    assert result.success
    assert _mode(canonical / "codex/AGENTS.md") == 0o644
    assert _mode(canonical / "codex/scripts/x.sh") == 0o755
    assert _mode(canonical / CANONICAL_MANIFEST_NAME) == 0o644
    again = publish_workflow_view(view, canonical, canonical, canonical / CANONICAL_MANIFEST_NAME)
    assert again.success and again.changed_paths == ()


def test_replace_at_default_modes_are_unchanged(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        workflow_publisher._replace_at(descriptor, "plain", b"x", False)
        workflow_publisher._replace_at(descriptor, "exec", b"x", True)
        workflow_publisher._replace_at(descriptor, "private", b"x", False, mode=0o600)
    finally:
        os.close(descriptor)

    assert (_mode(tmp_path / "plain"), _mode(tmp_path / "exec")) == (0o644, 0o755)
    assert _mode(tmp_path / "private") == 0o600


def test_update_goes_through_a_sealed_inode_and_stays_sealed(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success

    result = _publish(canonical, target, _view(b"two\n"))

    assert result.changed_paths == (_p("AGENTS.md"),)
    assert (target / "AGENTS.md").read_bytes() == b"two\n"
    assert _mode(target / "AGENTS.md") == 0o444


@pytest.mark.parametrize("write_bits", (0o644, 0o600, 0o666, 0o464, 0o446))
def test_clean_writable_file_and_manifest_are_resealed_and_mode_is_not_drift(
    tmp_path: Path, write_bits: int
) -> None:
    canonical, target = _roots(tmp_path)
    view = _view(files=(_file("scripts/run.sh", b"#!/bin/sh\n", executable=True),))
    assert _publish(canonical, target, view).success
    (target / "AGENTS.md").chmod(write_bits)
    (target / RUNTIME_MANIFEST_NAME).chmod(write_bits)
    inode = (target / "AGENTS.md").stat().st_ino

    inspected = _inspect(canonical, target, view)
    result = _publish(canonical, target, view)

    assert inspected.success
    assert result.success
    assert result.changed_paths == (_p("AGENTS.md"),)
    assert _mode(target / "AGENTS.md") == 0o444
    assert _mode(target / RUNTIME_MANIFEST_NAME) == 0o444
    assert _mode(target / "scripts/run.sh") == 0o555
    assert (target / "AGENTS.md").stat().st_ino != inode


def test_sealed_clean_files_are_not_rewritten(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view()).success
    before = [(path.stat().st_ino, path.stat().st_mtime_ns) for path in sorted(target.iterdir())]

    result = _publish(canonical, target, _view())

    assert result.success and result.changed_paths == ()
    assert [
        (path.stat().st_ino, path.stat().st_mtime_ns) for path in sorted(target.iterdir())
    ] == before


def test_a_write_bit_on_an_executable_only_changes_the_seal_not_the_state(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    view = _view(files=(_file("scripts/run.sh", b"#!/bin/sh\n", executable=True),))
    assert _publish(canonical, target, view).success
    (target / "scripts/run.sh").chmod(0o755)

    assert _publish(canonical, target, view).changed_paths == (_p("scripts/run.sh"),)
    assert _mode(target / "scripts/run.sh") == 0o555


# --- check-only ---


def test_inspect_does_not_write_create_or_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success
    _write(target / "AGENTS.md", b"drifted\n")
    before = _state(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("inspection must not commit, fingerprint or write")

    monkeypatch.setattr(workflow_publisher, "_commit", forbidden)
    monkeypatch.setattr(workflow_publisher, "fingerprint_source_inputs", forbidden)
    monkeypatch.setattr(workflow_publisher, "_replace_at", forbidden)

    result = workflow_publisher.inspect_workflow_view(
        _view(b"two\n"), canonical, target, target / RUNTIME_MANIFEST_NAME
    )

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert _state(tmp_path) == before


@pytest.mark.parametrize("depth", ("missing-root", "missing-parents"))
def test_inspect_of_a_missing_root_is_clean_and_creates_nothing(tmp_path: Path, depth: str) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    target = tmp_path / "target" if depth == "missing-root" else tmp_path / "a" / "b" / "target"
    before = _state(tmp_path)

    result = workflow_publisher.inspect_workflow_view(
        _view(), canonical, target, target / RUNTIME_MANIFEST_NAME
    )

    assert result.success and result.drift_items == ()
    assert _state(tmp_path) == before
    assert not target.parent.exists() or depth == "missing-root"


def test_inspect_refuses_a_canonical_alias(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    before = _state(tmp_path)

    result = workflow_publisher.inspect_workflow_view(
        _view(), canonical, canonical, canonical / CANONICAL_MANIFEST_NAME
    )

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert result.drift_items == ()
    assert _state(tmp_path) == before


def test_inspect_takes_shared_locks_and_publish_an_exclusive_target_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    real_flock = workflow_publisher.fcntl.flock
    operations: list[tuple[str, int]] = []

    def spy(descriptor: int, operation: int) -> None:
        operations.append((os.readlink(f"/proc/self/fd/{descriptor}"), operation))
        real_flock(descriptor, operation)

    monkeypatch.setattr(workflow_publisher.fcntl, "flock", spy)
    _inspect(canonical, target, _view())
    inspection = list(operations)
    operations.clear()
    _publish(canonical, target, _view())

    fcntl = workflow_publisher.fcntl
    assert (str(canonical), fcntl.LOCK_SH) in inspection
    assert (str(target), fcntl.LOCK_SH) in inspection
    assert all(op in {fcntl.LOCK_SH, fcntl.LOCK_UN} for _path, op in inspection)
    assert (str(target), fcntl.LOCK_EX) in operations


def test_inspect_reuses_a_given_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view()).success
    with canonical_lock(canonical, exclusive=False) as lease:
        monkeypatch.setattr(workflow_publisher, "canonical_lock", _lease_reacquired)
        result = workflow_publisher.inspect_workflow_view(
            _view(), canonical, target, target / RUNTIME_MANIFEST_NAME, canonical_lease=lease
        )

    assert result.success


# --- mutation-boundary recheck ---


def test_initially_clean_managed_file_changed_during_publish_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    kept = _file("agents/zz.md", b"kept\n")
    assert _publish(canonical, target, _view(b"one\n", files=(kept,))).success
    manifest = (target / RUNTIME_MANIFEST_NAME).read_bytes()

    def concurrent_edit(count: int) -> None:
        if count == 1:
            _write(target / "agents/zz.md", b"concurrent edit\n")

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", concurrent_edit)
    result = _publish(canonical, target, _view(b"two\n", files=(kept,)))

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert result.drift_items == (
        _entry(DriftClass.TARGET_DRIFT, "agents/zz.md", reason="changed-during-publish"),
    )
    assert (target / "agents/zz.md").read_bytes() == b"concurrent edit\n"
    assert (target / RUNTIME_MANIFEST_NAME).read_bytes() == manifest


def test_initially_clean_carrier_with_a_personal_key_changed_during_publish_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    fragment = _fragment("settings.json", ("hooks", "Stop"), ["x"])
    assert _publish(canonical, target, _view(b"one\n", fragments=(fragment,))).success
    carrier = json.loads((target / "settings.json").read_text())
    carrier["personal"] = 1
    (target / "settings.json").write_text(json.dumps(carrier))

    def concurrent_edit(count: int) -> None:
        if count == 1:
            carrier["personal"] = 2
            carrier["added-later"] = True
            (target / "settings.json").write_text(json.dumps(carrier))

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", concurrent_edit)
    result = _publish(canonical, target, _view(b"two\n", fragments=(fragment,)))

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert result.drift_items == (
        _entry(DriftClass.TARGET_DRIFT, "settings.json", reason="changed-during-publish"),
    )
    assert json.loads((target / "settings.json").read_text())["added-later"] is True


def test_a_path_absent_at_preflight_must_still_be_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success

    def concurrent_create(count: int) -> None:
        if count == 1:
            _write(target / "agents/new.md", b"operator file\n")

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", concurrent_create)
    result = _publish(
        canonical, target, _view(b"two\n", files=(_file("agents/new.md", b"wanted\n"),))
    )

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert result.drift_items == (
        _entry(DriftClass.TARGET_DRIFT, "agents/new.md", reason="changed-during-publish"),
    )
    assert (target / "agents/new.md").read_bytes() == b"operator file\n"


def test_a_retired_file_changed_before_its_unlink_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(
        canonical, target, _view(b"one\n", files=(_file("agents/old.md", b"old\n"),))
    ).success

    def concurrent_edit(count: int) -> None:
        if count == 1:
            _write(target / "agents/old.md", b"concurrent edit\n")

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", concurrent_edit)
    result = _publish(canonical, target, _view(b"two\n"))

    assert result.drift_items == (
        _entry(DriftClass.TARGET_DRIFT, "agents/old.md", reason="changed-during-publish"),
    )
    assert (target / "agents/old.md").read_bytes() == b"concurrent edit\n"


def test_the_manifest_changed_before_it_is_written_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success

    def concurrent_edit(count: int) -> None:
        if count == 1:
            _write(target / RUNTIME_MANIFEST_NAME, b"concurrent manifest\n")

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", concurrent_edit)
    result = _publish(canonical, target, _view(b"two\n"))

    assert result.drift_items == (
        _entry(
            DriftClass.TARGET_DRIFT,
            RUNTIME_MANIFEST_NAME,
            origin="target-manifest",
            reason="changed-during-publish",
        ),
    )
    assert (target / RUNTIME_MANIFEST_NAME).read_bytes() == b"concurrent manifest\n"


def test_a_change_made_by_the_pre_commit_hook_is_caught_at_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success

    def concurrent_edit() -> None:
        _write(target / "AGENTS.md", b"late edit\n")

    monkeypatch.setattr(workflow_publisher, "_before_target_commit", concurrent_edit)
    result = _publish(canonical, target, _view(b"two\n"))

    assert result.drift_items == (
        _entry(DriftClass.TARGET_DRIFT, "AGENTS.md", reason="changed-during-publish"),
    )
    assert (target / "AGENTS.md").read_bytes() == b"late edit\n"


def test_a_change_after_the_final_read_is_the_documented_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success
    real_replace = workflow_publisher._replace_at

    def late_writer(
        directory: int, name: str, content: bytes, executable: bool, *, mode: int | None = None
    ) -> None:
        if name == "AGENTS.md":
            _write(target / "AGENTS.md", b"after the final read\n")
        real_replace(directory, name, content, executable, mode=mode)

    monkeypatch.setattr(workflow_publisher, "_replace_at", late_writer)
    result = _publish(canonical, target, _view(b"two\n"))

    # Documented: the last read-to-rename window stays open against a writer that
    # ignores Djinn's locks; that is why discard requires a stopped container.
    assert result.success
    assert (target / "AGENTS.md").read_bytes() == b"two\n"


# --- publisher CLI: drift-items line, item lines, --check ---


def _cli_setup(tmp_path: Path, *, source: str = "opencode") -> tuple[Path, Path, Path]:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    view = tmp_path / "view"
    for directory in (canonical, target, view):
        directory.mkdir()
    _canonical_identity(canonical, source)
    _write(view / "AGENTS.md", b"cli view\n")
    return canonical, target, view


def _drift_payload(stderr: str) -> dict[str, object]:
    lines = [line for line in stderr.splitlines() if line.startswith(_ITEMS_PREFIX)]
    assert len(lines) == 1
    payload = json.loads(lines[0][len(_ITEMS_PREFIX) :])
    assert isinstance(payload, dict)
    return payload


def _record(
    cls: str,
    origin: str,
    root: Path,
    path: str,
    key: list[str] | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    return {
        "class": cls,
        "origin": origin,
        "root": str(root),
        "path": path,
        "key": key,
        "reason": reason,
    }


@pytest.mark.parametrize("check", (False, True))
def test_cli_target_drift_line_has_the_documented_schema(tmp_path: Path, check: bool) -> None:
    canonical, target, view = _cli_setup(tmp_path)
    assert _run_cli(view, canonical, target).returncode == 0
    _write(target / "AGENTS.md", b"operator edit\n")
    _write(view / "AGENTS.md", b"cli view two\n")

    result = _run_cli(view, canonical, target, *(("--check",) if check else ()))

    assert result.returncode == EXIT_CODES[DriftClass.TARGET_DRIFT]
    lines = result.stderr.splitlines()
    assert lines[0] == "workflow publisher: target-drift"
    assert _drift_payload(result.stderr) == {
        "items": [_record("target-drift", "target", target, "AGENTS.md")],
        "total": 1,
        "truncated": False,
    }
    assert (
        lines[2] == f'{_ITEM_PREFIX}target-drift "{target}/AGENTS.md"'
    )
    assert "operator edit" not in result.stderr
    assert (target / "AGENTS.md").read_bytes() == b"operator edit\n"


def test_cli_item_with_a_key_and_a_reason_renders_both(tmp_path: Path) -> None:
    # A carrier key item as the CLI renders it; the publisher CLI itself only
    # publishes whole files, so the lines are produced from a result directly.
    arguments = type("Arguments", (), {"view": "/v", "canonical_root": "/c", "target": "/t"})()
    items = (
        _entry(
            DriftClass.TARGET_DRIFT,
            "settings.json",
            ("hooks", "Stop"),
            reason="changed-during-publish",
        ),
        _entry(DriftClass.SOURCE_CHANGED, "AGENTS.md", origin="seed"),
        _entry(
            DriftClass.INVALID_OR_SEMANTIC, CANONICAL_MANIFEST_NAME, origin="canonical-manifest"
        ),
    )

    lines = workflow_publisher._drift_item_lines(items, arguments)  # pyright: ignore[reportArgumentType]

    payload = json.loads(lines[0][len(_ITEMS_PREFIX) :])
    assert payload["items"][0] == {
        "class": "target-drift",
        "origin": "target",
        "root": "/t",
        "path": "settings.json",
        "key": ["hooks", "Stop"],
        "reason": "changed-during-publish",
    }
    assert [(item["origin"], item["root"]) for item in payload["items"]] == [
        ("target", "/t"),
        ("seed", "/v"),
        ("canonical-manifest", "/c"),
    ]
    assert (
        lines[1]
        == f'{_ITEM_PREFIX}target-drift "/t/settings.json[hooks.Stop]"'
    )


@pytest.mark.parametrize("check", (False, True))
def test_cli_escapes_control_characters_in_item_names(tmp_path: Path, check: bool) -> None:
    canonical, target, view = _cli_setup(tmp_path)
    hostile = "we\nird\x1b[31m.md"
    _write(view / hostile, b"wanted\n")
    _write(target / hostile, b"unmanaged\n")

    result = _run_cli(view, canonical, target, *(("--check",) if check else ()))

    assert result.returncode == EXIT_CODES[DriftClass.COLLISION]
    for line in result.stderr.splitlines():
        assert line.startswith("workflow publisher")
        assert "\x1b" not in line
    payload = _drift_payload(result.stderr)
    assert payload["items"] == [_record("collision", "target", target, hostile)]
    assert json.dumps(f"{target}/{hostile}") in result.stderr


@pytest.mark.parametrize("check", (False, True))
def test_cli_caps_the_listed_items_and_reports_total_and_truncated(
    tmp_path: Path, check: bool
) -> None:
    canonical, target, view = _cli_setup(tmp_path)
    total = _CAP + 5
    for index in range(total):
        _write(view / f"agents/n{index:03}.md", b"wanted\n")
        _write(target / f"agents/n{index:03}.md", b"unmanaged\n")

    result = _run_cli(view, canonical, target, *(("--check",) if check else ()))

    payload = _drift_payload(result.stderr)
    assert len(payload["items"]) == _CAP  # type: ignore[arg-type]
    assert (payload["total"], payload["truncated"]) == (total, True)
    item_lines = [
        line
        for line in result.stderr.splitlines()
        if line.startswith(_ITEM_PREFIX)
    ]
    assert len(item_lines) == _CAP


def test_cli_exactly_the_cap_is_not_truncated(tmp_path: Path) -> None:
    canonical, target, view = _cli_setup(tmp_path)
    for index in range(_CAP):
        _write(view / f"agents/n{index:03}.md", b"wanted\n")
        _write(target / f"agents/n{index:03}.md", b"unmanaged\n")

    payload = _drift_payload(_run_cli(view, canonical, target).stderr)

    assert (payload["total"], payload["truncated"]) == (
        _CAP,
        False,
    )


@pytest.mark.parametrize("check", (False, True))
def test_cli_seed_mismatch_names_the_differing_seed_paths(tmp_path: Path, check: bool) -> None:
    canonical, target, view = _cli_setup(tmp_path)
    _canonical_identity(
        canonical, files={"AGENTS.md": (b"canonical\n", False), "agents/gone.md": (b"x\n", False)}
    )
    _write(view / "AGENTS.md", b"manipulated seed\n")
    before = _state(target)

    result = _run_cli(
        view, canonical, target, "--profile", "opencode", *(("--check",) if check else ())
    )

    assert result.returncode == EXIT_CODES[DriftClass.SOURCE_CHANGED]
    assert result.stderr.splitlines()[0] == "workflow publisher: source-changed"
    assert _drift_payload(result.stderr)["items"] == [
        _record("source-changed", "seed", view, "AGENTS.md"),
        _record("source-changed", "seed", view, "agents/gone.md"),
    ]
    assert _state(target) == before


@pytest.mark.parametrize("check", (False, True))
def test_cli_names_a_malformed_or_linked_canonical_manifest(tmp_path: Path, check: bool) -> None:
    canonical, target, view = _cli_setup(tmp_path)
    manifest = canonical / CANONICAL_MANIFEST_NAME
    manifest.write_text("{not json")

    malformed = _run_cli(view, canonical, target, *(("--check",) if check else ()))

    manifest.unlink()
    manifest.symlink_to(tmp_path / "elsewhere")
    linked = _run_cli(view, canonical, target, *(("--check",) if check else ()))
    assert malformed.returncode == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert _drift_payload(malformed.stderr)["items"] == [
        _record("invalid-or-semantic", "canonical-manifest", canonical, CANONICAL_MANIFEST_NAME)
    ]
    assert linked.returncode == EXIT_CODES[DriftClass.COLLISION]
    assert _drift_payload(linked.stderr)["items"] == [
        _record("collision", "canonical-manifest", canonical, CANONICAL_MANIFEST_NAME)
    ]


def test_cli_names_a_non_regular_seed_entry(tmp_path: Path) -> None:
    canonical, target, view = _cli_setup(tmp_path)
    os.mkfifo(view / "agents-fifo.md")

    result = _run_cli(view, canonical, target, "--profile", "opencode")

    assert result.returncode == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert _drift_payload(result.stderr)["items"] == [
        _record("invalid-or-semantic", "seed", view, "agents-fifo.md")
    ]


def test_cli_names_a_malformed_runtime_manifest_in_the_target(tmp_path: Path) -> None:
    canonical, target, view = _cli_setup(tmp_path)
    assert _run_cli(view, canonical, target).returncode == 0
    _write(target / RUNTIME_MANIFEST_NAME, b"{broken")

    for extra in ((), ("--check",)):
        result = _run_cli(view, canonical, target, *extra)

        assert result.returncode == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
        assert _drift_payload(result.stderr)["items"] == [
            _record("invalid-or-semantic", "target-manifest", target, RUNTIME_MANIFEST_NAME)
        ]


def test_cli_check_exit_codes_and_no_writes(tmp_path: Path) -> None:
    canonical, target, view = _cli_setup(tmp_path)
    missing_target = tmp_path / "not" / "there"
    before = _state(tmp_path)

    missing = _run_cli(view, canonical, missing_target, "--check")
    unpublished = _run_cli(view, canonical, target, "--check")

    assert missing.returncode == 0 and missing.stderr == ""
    assert unpublished.returncode == 0 and unpublished.stderr == ""
    assert _state(tmp_path) == before
    assert _run_cli(view, canonical, target).returncode == 0
    clean = _run_cli(view, canonical, target, "--check")
    _write(target / "AGENTS.md", b"edit\n")
    drift = _run_cli(view, canonical, target, "--check")
    (target / "AGENTS.md").unlink()
    (target / "AGENTS.md").mkdir()
    collision = _run_cli(view, canonical, target, "--check")
    assert [clean.returncode, drift.returncode, collision.returncode] == [0, 11, 12]
    assert clean.stderr == ""


def test_cli_check_refuses_a_canonical_alias_without_writing(tmp_path: Path) -> None:
    canonical, _target, view = _cli_setup(tmp_path)
    before = _state(canonical)

    result = _run_cli(view, canonical, canonical, "--check")

    assert result.returncode == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert _state(canonical) == before


def test_cli_keeps_lock_diagnostics_and_still_prints_an_empty_items_line(tmp_path: Path) -> None:
    canonical, target, view = _cli_setup(tmp_path)
    result = _run_cli(view, tmp_path / "missing-canonical", target)

    lines = result.stderr.splitlines()
    assert lines[0] == "workflow publisher: invalid-or-semantic"
    assert lines[1].startswith(PUBLISHER_LOCK_ERROR_PREFIX)
    assert _drift_payload(result.stderr) == {"items": [], "total": 0, "truncated": False}
    assert not any(line.startswith(_ITEM_PREFIX) for line in lines)


def test_a_claude_instruction_position_in_a_foreign_target_names_the_file(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    view = _view(files=(_file("CLAUDE.md", b"bridge\n"),), target_tool="codex")

    for mode in MODES:
        result = _run(mode, canonical, target, view)

        assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
        assert result.drift_items == (_entry(DriftClass.INVALID_OR_SEMANTIC, "CLAUDE.md"),)


def test_a_seed_with_a_foreign_source_names_the_canonical_manifest() -> None:
    manifest = workflow_publisher._Manifest("claude", {}, {})
    view = WorkflowView("codex", ())

    with pytest.raises(PublishError) as error:
        workflow_publisher._verify_seed_against_canonical_manifest(view, manifest, "opencode")

    assert error.value.drift_class is DriftClass.SOURCE_CHANGED
    assert error.value.items == (
        _entry(DriftClass.SOURCE_CHANGED, CANONICAL_MANIFEST_NAME, origin="canonical-manifest"),
    )
