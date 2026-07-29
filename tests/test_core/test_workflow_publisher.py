from __future__ import annotations

# pyright: reportPrivateUsage=false
import errno
import json
import multiprocessing
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
    PUBLISHER_WRITE_ERROR_PREFIX,
    RUNTIME_MANIFEST_NAME,
    CarrierFragment,
    DriftClass,
    PublishedFile,
    PublishError,
    WorkflowView,
    canonical_lock,
    publish_workflow_view,
    retire_legacy_delivery_manifest,
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
    provisioning_placeholder_paths: tuple[PurePosixPath, ...] = (),
) -> WorkflowView:
    return WorkflowView(
        "claude",
        (_file("AGENTS.md", marker), *files),
        fragments,
        target_tool=target_tool,
        provisioning_placeholder_paths=provisioning_placeholder_paths,
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
    path.write_bytes(content)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _legacy_manifest(
    files: dict[str, tuple[bytes, bool]],
    fragments: list[Mapping[str, object]] | None = None,
    *,
    tool: str = "claude",
) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "tool": tool,
            "files": {
                path: {
                    "content_hash": workflow_publisher._digest(content),  # pyright: ignore[reportPrivateUsage]
                    "executable": executable,
                }
                for path, (content, executable) in files.items()
            },
            "fragments": [] if fragments is None else fragments,
        }
    ).encode()


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
    (target / "AGENTS.md").write_bytes(b"operator edit\n")
    before_edit = _tree(target)

    drift = _publish(canonical, target, _view(b"two\n"))

    assert drift.drift_class is DriftClass.TARGET_DRIFT
    assert _tree(target) == before_edit


def test_zero_byte_claude_companion_adopts_but_nonempty_file_blocks(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    _write(target / "AGENTS.md", b"")

    adopted = _publish(
        canonical,
        target,
        _view(
            target_tool="claude",
            provisioning_placeholder_paths=(_p("AGENTS.md"),),
        ),
    )

    assert adopted.success
    assert (target / "AGENTS.md").read_bytes() == b"one\n"
    (target / RUNTIME_MANIFEST_NAME).unlink()
    (target / "AGENTS.md").write_bytes(b"operator content\n")
    result = _publish(
        canonical,
        target,
        _view(
            target_tool="claude",
            provisioning_placeholder_paths=(_p("AGENTS.md"),),
        ),
    )
    assert result.drift_class is DriftClass.COLLISION


@pytest.mark.parametrize("target_tool", ("codex", "opencode"))
def test_zero_byte_native_agents_file_is_a_collision(
    tmp_path: Path, target_tool: str
) -> None:
    canonical, target = _roots(tmp_path)
    _write(target / "AGENTS.md", b"")
    before = _tree(target)

    result = _publish(canonical, target, _view(target_tool=target_tool))

    assert result.drift_class is DriftClass.COLLISION
    assert _tree(target) == before


def test_claude_root_hook_paths_round_trip_and_adopt_legacy_writer_state(tmp_path: Path) -> None:
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
    (target / RUNTIME_MANIFEST_NAME).unlink()
    legacy = target / workflow_publisher.LEGACY_DELIVERY_MANIFEST_NAME
    legacy.write_bytes(
        _legacy_manifest(
            {
                "AGENTS.md": (b"one\n", False),
                "security_reminder_hook.py": (security, False),
                "ready_notify_hook.py": (ready, False),
            }
        )
    )

    adopted = _publish(canonical, target, view)

    assert adopted.success
    assert (target / RUNTIME_MANIFEST_NAME).is_file()
    assert not legacy.exists()


def test_runtime_legacy_manifest_adoption_covers_files_and_fragments(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    _write(target / "AGENTS.md", b"one\n")
    _write(target / "settings.json", b'{"hooks":{"Stop":["ready"]},"operator":true}\n')
    value = ["ready"]
    fragment = {
        "carrier_path": "settings.json",
        "key_path": ["hooks", "Stop"],
        "value_hash": workflow_publisher._legacy_value_digest(value),  # pyright: ignore[reportPrivateUsage]
    }
    legacy = target / workflow_publisher.LEGACY_DELIVERY_MANIFEST_NAME
    legacy.write_bytes(_legacy_manifest({"AGENTS.md": (b"one\n", False)}, [fragment]))

    result = _publish(
        canonical,
        target,
        _view(fragments=(_fragment("settings.json", ("hooks", "Stop"), value),)),
    )

    assert result.success
    assert not legacy.exists()
    assert (target / RUNTIME_MANIFEST_NAME).is_file()
    assert json.loads((target / "settings.json").read_text())["operator"] is True


def test_runtime_legacy_adoption_is_retry_safe_after_state_write_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    _write(target / "AGENTS.md", b"one\n")
    legacy = target / workflow_publisher.LEGACY_DELIVERY_MANIFEST_NAME
    legacy.write_bytes(_legacy_manifest({"AGENTS.md": (b"one\n", False)}))

    def abort_after_state_write() -> None:
        raise RuntimeError("injected adoption crash")

    monkeypatch.setattr(
        workflow_publisher,
        "_after_runtime_manifest_write",
        abort_after_state_write,
    )
    with pytest.raises(RuntimeError, match="injected adoption crash"):
        _publish(canonical, target, _view())
    monkeypatch.setattr(
        workflow_publisher,
        "_after_runtime_manifest_write",
        lambda: None,
    )

    assert (target / RUNTIME_MANIFEST_NAME).is_file()
    assert legacy.is_file()
    assert _publish(canonical, target, _view()).success
    assert not legacy.exists()


def test_runtime_adoption_retry_removes_legacy_before_reporting_new_state_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    _write(target / "AGENTS.md", b"one\n")
    legacy = target / workflow_publisher.LEGACY_DELIVERY_MANIFEST_NAME
    legacy.write_bytes(_legacy_manifest({"AGENTS.md": (b"one\n", False)}))

    monkeypatch.setattr(
        workflow_publisher,
        "_after_runtime_manifest_write",
        lambda: (_ for _ in ()).throw(RuntimeError("injected adoption crash")),
    )
    with pytest.raises(RuntimeError, match="injected adoption crash"):
        _publish(canonical, target, _view())
    monkeypatch.setattr(workflow_publisher, "_after_runtime_manifest_write", lambda: None)
    (target / "AGENTS.md").write_bytes(b"operator edit\n")

    retried = _publish(canonical, target, _view())

    assert retried.drift_class is DriftClass.TARGET_DRIFT
    assert not legacy.exists()


@pytest.mark.parametrize("payload", [b"not-json", b'{"schema_version":1}'])
def test_runtime_legacy_manifest_malformed_or_edited_fails_closed(
    tmp_path: Path, payload: bytes
) -> None:
    canonical, target = _roots(tmp_path)
    _write(target / "AGENTS.md", b"one\n")
    legacy = target / workflow_publisher.LEGACY_DELIVERY_MANIFEST_NAME
    if payload == b"not-json":
        legacy.write_bytes(payload)
    else:
        legacy.write_bytes(_legacy_manifest({"AGENTS.md": (b"recorded\n", False)}))
    before = _tree(target)

    result = _publish(canonical, target, _view())

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert _tree(target) == before


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
    manifest_path.write_text(json.dumps(manifest))
    before = _tree(target)

    result = _publish(canonical, target, _view())

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert _tree(target) == before


@pytest.mark.parametrize("foreign_kind", ("file", "fragment"))
def test_runtime_legacy_rejects_unowned_manifest_items_without_mutation(
    tmp_path: Path, foreign_kind: str
) -> None:
    canonical, target = _roots(tmp_path)
    _write(target / "AGENTS.md", b"one\n")
    if foreign_kind == "file":
        operator_file = target / "operator-private.txt"
        operator_file.write_bytes(b"operator-owned\n")
        legacy = _legacy_manifest(
            {
                "AGENTS.md": (b"one\n", False),
                "operator-private.txt": (operator_file.read_bytes(), False),
            }
        )
    else:
        carrier = target / "settings.json"
        carrier.write_bytes(b'{"operator":{"keep":true}}\n')
        legacy = _legacy_manifest(
            {"AGENTS.md": (b"one\n", False)},
            [
                {
                    "carrier_path": "settings.json",
                    "key_path": ["operator", "keep"],
                    "value_hash": workflow_publisher._legacy_value_digest(True),  # pyright: ignore[reportPrivateUsage]
                }
            ],
        )
    (target / workflow_publisher.LEGACY_DELIVERY_MANIFEST_NAME).write_bytes(legacy)
    before = _tree(target)

    result = _publish(canonical, target, _view())

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert _tree(target) == before


def test_compose_retirement_retries_after_verify_before_remove_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "claude"
    root.mkdir()
    legacy = root / workflow_publisher.LEGACY_DELIVERY_MANIFEST_NAME
    legacy.write_bytes(_legacy_manifest({}))

    def abort_before_remove() -> None:
        raise RuntimeError("injected retirement crash")

    monkeypatch.setattr(
        workflow_publisher,
        "_after_legacy_delivery_verified",
        abort_before_remove,
    )
    with pytest.raises(RuntimeError, match="injected retirement crash"):
        retire_legacy_delivery_manifest(root)
    monkeypatch.setattr(workflow_publisher, "_after_legacy_delivery_verified", lambda: None)

    assert legacy.exists()
    assert retire_legacy_delivery_manifest(root).success
    assert not legacy.exists()


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
    assert captured.err.splitlines() == ["workflow publisher: invalid-or-semantic"]
    assert "Traceback" not in captured.err


def test_standalone_cli_reports_canonical_unlock_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canonical, target = _roots(tmp_path)
    view = tmp_path / "view"
    view.mkdir()
    _canonical_identity(canonical)
    _write(view / "AGENTS.md", b"cli view\n")
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
    assert code == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert captured.err.splitlines() == ["workflow publisher: invalid-or-semantic"]
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
    (target / "AGENTS.md").write_bytes(b"operator edit\n")
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
        "plugins/session-start-status.js": b"export const Plugin = () => ({});\n",
        "plugins/security-reminder.js": b"export const Plugin = () => ({});\n",
        "plugins/ready-notify.js": b"export const Plugin = () => ({});\n",
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


def test_opencode_cli_delivers_native_plugins_not_managed_by_canonical_manifest(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    seed = tmp_path / "seed"
    canonical.mkdir()
    target.mkdir()
    seed.mkdir()
    instructions = b"OpenCode instructions\n"
    plugins = {
        "plugins/session-start-status.js": b"export const Plugin = () => ({});\n",
        "plugins/security-reminder.js": b"export const Plugin = () => ({});\n",
        "plugins/ready-notify.js": b"export const Plugin = () => ({});\n",
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
    tracked_plugin.write_bytes(b"export const Plugin = () => ({ edited: true });\n")
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
    _write(seed / "plugins/ready-notify.js", b"export const Plugin = () => ({});\n")
    _write(seed / "commands/untracked.md", b"---\nname: untracked\n---\nUntracked\n")
    _write(target / "personal.json", b'{"keep":true}\n')
    before = _tree(target)

    result = _run_cli(seed, canonical, target, "--profile", "opencode")

    assert result.returncode == EXIT_CODES[DriftClass.SOURCE_CHANGED]
    assert _tree(target) == before


def test_opencode_cli_rejects_invalid_native_plugin_before_runtime_mutation(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    seed = tmp_path / "seed"
    canonical.mkdir()
    target.mkdir()
    seed.mkdir()
    _canonical_identity(canonical, files={"AGENTS.md": (b"OpenCode instructions\n", False)})
    _write(seed / "AGENTS.md", b"OpenCode instructions\n")
    _write(seed / "plugins/ready-notify.js", b"const plugin = () => ({});\n")
    _write(target / "personal.json", b'{"keep":true}\n')
    before = _tree(target)

    result = _run_cli(seed, canonical, target, "--profile", "opencode")

    assert result.returncode == EXIT_CODES[DriftClass.INVALID_OR_SEMANTIC]
    assert _tree(target) == before
