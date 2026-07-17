from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import multiprocessing
import stat
import subprocess
import sys
import tomllib
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path, PurePosixPath
from typing import NoReturn

import pytest

from djinn_in_a_box.core import workflow_publisher
from djinn_in_a_box.core.workflow_publisher import (
    CANONICAL_MANIFEST_NAME,
    EXIT_CODES,
    RUNTIME_MANIFEST_NAME,
    CarrierFragment,
    DriftClass,
    PublishedFile,
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
) -> WorkflowView:
    return WorkflowView("claude", (_file("AGENTS.md", marker), *files), fragments)


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


def test_stale_file_and_owned_json_key_are_removed_without_touching_neighbor(
    tmp_path: Path,
) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(
        files=(_file("agents/obsolete.md", b"obsolete\n"),),
        fragments=(
            _fragment("settings.json", ("hooks", "owned"), ["old"]),
            _fragment("settings.json", ("hooks", "retired"), ["remove"]),
        ),
    )
    assert _publish(canonical, target, first).success
    carrier = json.loads((target / "settings.json").read_text())
    carrier["operator"] = {"keep": True}
    (target / "settings.json").write_text(json.dumps(carrier))

    result = _publish(
        canonical,
        target,
        _view(fragments=(_fragment("settings.json", ("hooks", "owned"), ["new"]),)),
    )
    updated = json.loads((target / "settings.json").read_text())

    assert result.success
    assert result.removed_paths == (_p("agents/obsolete.md"),)
    assert not (target / "agents/obsolete.md").exists()
    assert updated["hooks"] == {"owned": ["new"]}
    assert updated["operator"] == {"keep": True}


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
        b"inline = { enabled = true, labels = [\"private\"] }\n"
        b"\n[tool.operator]\n"
        b"comment = \"keep every byte\"\n"
    )
    _write(target / "config.toml", original)
    first = _view(fragments=(_fragment("config.toml", ("fallback",), ["CLAUDE.md"]),))

    assert _publish(canonical, target, first).success
    inserted = (target / "config.toml").read_bytes()
    assignment = b'fallback = ["CLAUDE.md"]\n'
    parsed = tomllib.loads(inserted.decode())

    assert parsed["fallback"] == ["CLAUDE.md"]
    assert inserted.replace(assignment, b"") == original
    assert inserted.index(assignment) < inserted.index(b"[tool.operator]")
    second = _view(fragments=(_fragment("config.toml", ("fallback",), ["AGENTS.md"]),))
    assert _publish(canonical, target, second).success
    replaced = (target / "config.toml").read_bytes()

    assert tomllib.loads(replaced.decode())["fallback"] == ["AGENTS.md"]
    assert replaced.replace(b'fallback = ["AGENTS.md"]\n', b"") == original


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
        files=(_file("obsolete.md", b"old\n"),),
        fragments=(
            _fragment("settings.json", ("hooks", "owned"), ["old"]),
            _fragment("settings.json", ("hooks", "retired"), ["remove"]),
        ),
    )
    assert _publish(canonical, target, first).success
    manifest_before = (target / RUNTIME_MANIFEST_NAME).read_bytes()
    second = _view(
        b"two\n",
        fragments=(_fragment("settings.json", ("hooks", "owned"), ["new"]),),
    )

    def abort_after_second_mutation(count: int) -> None:
        if count == 2:
            raise RuntimeError("injected abort")

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", abort_after_second_mutation)
    with pytest.raises(RuntimeError, match="injected abort"):
        _publish(canonical, target, second)
    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", _no_target_mutation)

    assert (target / "AGENTS.md").read_bytes() == b"two\n"
    assert json.loads((target / "settings.json").read_text())["hooks"] == {"owned": ["new"]}
    assert (target / "obsolete.md").exists()
    assert (target / RUNTIME_MANIFEST_NAME).read_bytes() == manifest_before
    recovered = _publish(canonical, target, second)

    assert recovered.success
    assert not (target / "obsolete.md").exists()
    assert json.loads((target / "settings.json").read_text())["hooks"] == {"owned": ["new"]}


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


def _canonical_identity(root: Path, source: str = "opencode") -> None:
    (root / CANONICAL_MANIFEST_NAME).write_text(json.dumps({"source": source, "items": []}))


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
