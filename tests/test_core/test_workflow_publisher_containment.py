"""The publisher never follows a symlink at or below the roots it reads and writes.

The container can write the target and source trees, so every test plants a link
(or a swapped name) that points into a sibling `outside` tree and proves that the
publish refuses it and leaves `outside` byte-identical.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import errno
import json
import os
import resource
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from djinn_in_a_box.core import workflow_publisher
from djinn_in_a_box.core.workflow_publisher import (
    CANONICAL_MANIFEST_NAME,
    EXIT_CODES,
    RUNTIME_MANIFEST_NAME,
    CarrierFragment,
    DriftClass,
    PublishedFile,
    PublishError,
    PublishResult,
    WorkflowView,
    canonical_lock,
    fingerprint_source_inputs,
    publish_workflow_view,
    read_regular_file,
    runtime_residue_prefixes,
    snapshot_file_view,
)

_p = PurePosixPath
_SCRIPT = Path(__file__).resolve().parents[2] / "src/djinn_in_a_box/core/workflow_publisher.py"
_DEEP = "skills/demo/refs/guide.md"
_REAL_OPEN = os.open
_SECRET = b"outside secret\n"


def _view(*paths: str, marker: bytes = b"one\n") -> WorkflowView:
    files = tuple(PublishedFile(_p(path), f"{path}\n".encode()) for path in paths)
    return WorkflowView(
        "claude", (PublishedFile(_p("AGENTS.md"), marker), *files), target_tool="claude"
    )


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    canonical, target, outside = (tmp_path / name for name in ("canonical", "target", "outside"))
    for root in (canonical, target, outside):
        root.mkdir()
    (outside / "keep.md").write_bytes(_SECRET)
    return canonical, target, outside


def _publish(canonical: Path, target: Path, view: WorkflowView) -> PublishResult:
    return publish_workflow_view(view, canonical, target, target / RUNTIME_MANIFEST_NAME)


def _snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    """Every entry with its type and content (links by target, never followed)."""
    result: dict[str, tuple[str, bytes | str]] = {}
    for directory, names, files in os.walk(root):
        for name in (*names, *files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                result[relative] = ("dir", b"")
            elif path.is_file():
                result[relative] = ("file", path.read_bytes())
            else:
                result[relative] = ("special", b"")
    return result


def _link_into_outside(target: Path, component: str, outside: Path) -> None:
    """Replace the directory `component` below `target` with a link to `outside`."""
    path = target / component
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir() and not path.is_symlink():
        for child in sorted(path.rglob("*"), reverse=True):
            child.unlink() if not child.is_dir() else child.rmdir()
        path.rmdir()
    path.symlink_to(outside, target_is_directory=True)


def _no_hook(_count: int) -> None:
    return None


@pytest.fixture(autouse=True)
def fail_before_a_fifo_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FIFO opened for I/O would block forever; fail the test instead."""
    original_open = os.open

    def guarded(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if not flags & os.O_PATH:
            try:
                info = os.stat(path, dir_fd=dir_fd)
            except OSError:
                info = None
            if info is not None and stat.S_ISFIFO(info.st_mode):
                pytest.fail(f"FIFO {path!r} opened for I/O")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workflow_publisher.os, "open", guarded)


# --- the issue probe and every level of a nested write -----------------------


def test_issue_probe_refuses_a_linked_agents_directory(tmp_path: Path) -> None:
    canonical, target, outside = _roots(tmp_path)
    (target / "agents").symlink_to(outside, target_is_directory=True)
    before = _snapshot(outside)
    view = WorkflowView(
        "claude", (PublishedFile(_p("agents/probe.md"), b"probe"),), target_tool="opencode"
    )

    result = _publish(canonical, target, view)

    assert result.drift_class is DriftClass.COLLISION
    assert result.changed_paths == ()
    assert _snapshot(outside) == before
    assert not (target / RUNTIME_MANIFEST_NAME).exists()


@pytest.mark.parametrize("component", ("skills", "skills/demo", "skills/demo/refs"))
def test_new_file_below_a_linked_component_is_a_collision(
    tmp_path: Path, component: str
) -> None:
    canonical, target, outside = _roots(tmp_path)
    _link_into_outside(target, component, outside)
    before_outside = _snapshot(outside)
    before_target = _snapshot(target)

    result = _publish(canonical, target, _view(_DEEP))

    assert result.drift_class is DriftClass.COLLISION
    assert _snapshot(outside) == before_outside
    assert _snapshot(target) == before_target


@pytest.mark.parametrize("component", ("skills", "skills/demo", "skills/demo/refs"))
def test_non_directory_component_is_a_collision(tmp_path: Path, component: str) -> None:
    canonical, target, _outside = _roots(tmp_path)
    blocker = target / component
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_bytes(b"operator file\n")
    before = _snapshot(target)

    result = _publish(canonical, target, _view(_DEEP))

    assert result.drift_class is DriftClass.COLLISION
    assert _snapshot(target) == before


@pytest.mark.parametrize("component", ("skills", "skills/demo", "skills/demo/refs"))
def test_stale_removal_never_unlinks_through_a_linked_component(
    tmp_path: Path, component: str
) -> None:
    canonical, target, outside = _roots(tmp_path)
    assert _publish(canonical, target, _view(_DEEP)).success
    # The outside tree mirrors the managed file byte for byte, so only the
    # link refusal (not a hash mismatch) can keep it alive.
    mirror = outside / Path(_DEEP).relative_to(component)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_bytes(f"{_DEEP}\n".encode())
    _link_into_outside(target, component, outside)
    before_outside = _snapshot(outside)

    result = _publish(canonical, target, _view(marker=b"two\n"))

    assert result.drift_class is DriftClass.COLLISION
    assert result.removed_paths == ()
    assert _snapshot(outside) == before_outside


def test_canonical_carrier_below_a_linked_tool_root_is_a_collision(tmp_path: Path) -> None:
    canonical, _target, outside = _roots(tmp_path)
    (canonical / "opencode").symlink_to(outside, target_is_directory=True)
    before = _snapshot(outside)
    view = WorkflowView(
        "claude",
        (),
        (CarrierFragment(_p("opencode/opencode.json"), ("x",), b"true"),),
    )

    result = publish_workflow_view(view, canonical, canonical, canonical / CANONICAL_MANIFEST_NAME)

    assert result.drift_class is DriftClass.COLLISION
    assert _snapshot(outside) == before


@pytest.mark.parametrize("kind", ("link", "fifo", "directory"))
def test_special_leaf_at_a_managed_path_is_a_collision(tmp_path: Path, kind: str) -> None:
    canonical, target, outside = _roots(tmp_path)
    leaf = target / "AGENTS.md"
    if kind == "link":
        leaf.symlink_to(outside / "keep.md")
    elif kind == "fifo":
        os.mkfifo(leaf)
    else:
        leaf.mkdir()
    before = _snapshot(outside)

    result = _publish(canonical, target, _view())

    assert result.drift_class is DriftClass.COLLISION
    assert _snapshot(outside) == before


@pytest.mark.parametrize("kind", ("link", "fifo"))
def test_special_runtime_manifest_is_a_collision(tmp_path: Path, kind: str) -> None:
    canonical, target, outside = _roots(tmp_path)
    manifest = target / RUNTIME_MANIFEST_NAME
    if kind == "link":
        manifest.symlink_to(outside / "keep.md")
    else:
        os.mkfifo(manifest)
    before = _snapshot(outside)

    result = _publish(canonical, target, _view())

    assert result.drift_class is DriftClass.COLLISION
    assert _snapshot(outside) == before
    assert not (target / "AGENTS.md").exists()


def test_preflight_reads_never_create_missing_parents(tmp_path: Path) -> None:
    canonical, target, _outside = _roots(tmp_path)
    (target / "AGENTS.md").write_bytes(b"unmanaged operator file\n")

    result = _publish(canonical, target, _view(_DEEP))

    assert result.drift_class is DriftClass.COLLISION
    assert not (target / "skills").exists()


# --- swaps after the preflight -------------------------------------------------


def test_parent_swapped_before_the_first_write_is_a_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target, outside = _roots(tmp_path)
    (target / "skills/demo").mkdir(parents=True)
    before = _snapshot(outside)

    def swap() -> None:
        _link_into_outside(target, "skills/demo", outside)

    monkeypatch.setattr(workflow_publisher, "_before_target_commit", swap)
    result = _publish(canonical, target, _view(_DEEP))

    assert result.drift_class is DriftClass.COLLISION
    assert _snapshot(outside) == before


def test_parent_swapped_mid_commit_is_refused_and_a_repaired_retry_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target, outside = _roots(tmp_path)
    (target / "skills/demo/refs").mkdir(parents=True)
    before = _snapshot(outside)

    def swap_after_first(count: int) -> None:
        if count == 1:
            _link_into_outside(target, "skills/demo", outside)

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", swap_after_first)
    refused = _publish(canonical, target, _view(_DEEP))
    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", _no_hook)
    (target / "skills/demo").unlink()
    retried = _publish(canonical, target, _view(_DEEP))

    assert refused.drift_class is DriftClass.COLLISION
    assert _snapshot(outside) == before
    assert retried.success
    assert (target / _DEEP).read_bytes() == f"{_DEEP}\n".encode()


def test_stale_parent_swapped_mid_commit_keeps_the_outside_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target, outside = _roots(tmp_path)
    assert _publish(canonical, target, _view(_DEEP)).success
    mirror = outside / "guide.md"
    mirror.write_bytes(f"{_DEEP}\n".encode())
    before = _snapshot(outside)

    def swap_after_first(count: int) -> None:
        if count == 1:
            _link_into_outside(target, "skills/demo/refs", outside)

    monkeypatch.setattr(workflow_publisher, "_after_target_mutation", swap_after_first)
    result = _publish(canonical, target, _view(marker=b"two\n"))

    assert result.drift_class is DriftClass.COLLISION
    assert _snapshot(outside) == before


def test_mkdir_race_lost_to_a_link_is_a_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target, outside = _roots(tmp_path)
    before = _snapshot(outside)
    original_mkdir = os.mkdir

    def plant_link_first(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if dir_fd is not None and path == "skills":
            os.symlink(str(outside), path, dir_fd=dir_fd)
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workflow_publisher.os, "mkdir", plant_link_first)
    result = _publish(canonical, target, _view(_DEEP))

    assert result.drift_class is DriftClass.COLLISION
    assert _snapshot(outside) == before


# --- leaf reads: pinned, never a name reopen ----------------------------------


def _swap_after_pin(
    monkeypatch: pytest.MonkeyPatch, name: str, replace: Callable[[int], None]
) -> list[tuple[object, int]]:
    """Replace `name` right after its O_PATH pin; record every later open."""
    original_open = os.open
    opened: list[tuple[object, int]] = []

    def spy(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == name and not flags & os.O_PATH:
            # Checked before the open: a regressed read would block on the FIFO.
            pytest.fail(f"{name} was opened by name for I/O: flags={flags:#o}")
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.append((path, flags))
        if path == name and dir_fd is not None:
            os.rename(name, f"{name}.pinned", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            replace(dir_fd)
        return descriptor

    monkeypatch.setattr(workflow_publisher.os, "open", spy)
    return opened


@pytest.mark.parametrize("replacement", ("fifo", "regular", "link"))
def test_name_swapped_after_the_pin_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    _canonical, target, outside = _roots(tmp_path)
    (target / "a.md").write_bytes(b"pinned\n")

    def replace(directory: int) -> None:
        if replacement == "fifo":
            os.mkfifo("a.md", dir_fd=directory)
        elif replacement == "regular":
            descriptor = _REAL_OPEN("a.md", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=directory)
            os.write(descriptor, b"replacement\n")
            os.close(descriptor)
        else:
            os.symlink(str(outside / "keep.md"), "a.md", dir_fd=directory)

    opened = _swap_after_pin(monkeypatch, "a.md", replace)

    assert read_regular_file(target, _p("a.md")) == (b"pinned\n", False)
    assert any(str(path).startswith("/proc/self/fd/") for path, _flags in opened)


def test_read_failure_of_a_target_file_is_invalid_without_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target, _outside = _roots(tmp_path)
    (target / "AGENTS.md").write_bytes(b"one\n")

    def fail_read(_pin: int) -> bytes:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(workflow_publisher, "_read_pinned", fail_read)
    result = _publish(canonical, target, _view())

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert result.write_error is None


def test_failed_install_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target, _outside = _roots(tmp_path)

    def fail_fchmod(_descriptor: int, _mode: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(workflow_publisher.os, "fchmod", fail_fchmod)
    result = _publish(canonical, target, _view())

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert result.write_error is not None
    assert os.listdir(target) == []


def test_stale_removal_syncs_its_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target, _outside = _roots(tmp_path)
    assert _publish(canonical, target, _view(_DEEP)).success
    events: list[tuple[str, object, int | None]] = []
    original_unlink = os.unlink
    original_fsync = os.fsync
    original_close = os.close

    def record_unlink(path: Any, *, dir_fd: int | None = None) -> None:
        events.append(("unlink", path, dir_fd))
        original_unlink(path, dir_fd=dir_fd)

    def record_fsync(descriptor: int) -> None:
        events.append(("fsync", None, descriptor))
        original_fsync(descriptor)

    def record_close(descriptor: int) -> None:
        events.append(("close", None, descriptor))
        original_close(descriptor)

    monkeypatch.setattr(workflow_publisher.os, "unlink", record_unlink)
    monkeypatch.setattr(workflow_publisher.os, "fsync", record_fsync)
    monkeypatch.setattr(workflow_publisher.os, "close", record_close)
    result = _publish(canonical, target, _view())

    assert result.removed_paths == (_p(_DEEP),)
    removal = next(
        index for index, event in enumerate(events) if event[:2] == ("unlink", "guide.md")
    )
    parent = events[removal][2]
    closed = events.index(("close", None, parent), removal)
    assert ("fsync", None, parent) in events[removal:closed]


# --- roots -----------------------------------------------------------------------


@pytest.mark.parametrize("manifest_name", (CANONICAL_MANIFEST_NAME, RUNTIME_MANIFEST_NAME))
def test_target_link_to_the_canonical_root_is_never_canonical(
    tmp_path: Path, manifest_name: str
) -> None:
    canonical, _target, _outside = _roots(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(canonical, target_is_directory=True)

    result = publish_workflow_view(_view(), canonical, alias, alias / manifest_name)

    assert not result.success
    if manifest_name == RUNTIME_MANIFEST_NAME:
        assert result.lock_error is not None
    assert os.listdir(canonical) == []


def test_inherited_lease_does_not_make_a_linked_target_canonical(tmp_path: Path) -> None:
    canonical, _target, _outside = _roots(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(canonical, target_is_directory=True)

    with canonical_lock(canonical, exclusive=True) as lease:
        result = publish_workflow_view(
            _view(), canonical, alias, alias / CANONICAL_MANIFEST_NAME, canonical_lease=lease
        )

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert os.listdir(canonical) == []


@pytest.mark.parametrize("canonical_target", (True, False))
def test_lease_for_another_directory_is_rejected(tmp_path: Path, canonical_target: bool) -> None:
    canonical, target, outside = _roots(tmp_path)
    destination = canonical if canonical_target else target
    manifest = CANONICAL_MANIFEST_NAME if canonical_target else RUNTIME_MANIFEST_NAME
    before = _snapshot(outside)

    with canonical_lock(outside, exclusive=True) as foreign:
        result = publish_workflow_view(
            _view(), canonical, destination, destination / manifest, canonical_lease=foreign
        )

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert os.listdir(destination) == []
    assert _snapshot(outside) == before


def test_canonical_lock_on_a_swapped_root_never_anchors_the_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, _target, outside = _roots(tmp_path)
    before = _snapshot(outside)
    original_open = workflow_publisher._open_directory

    def open_swapped(path: Path) -> int:
        # The canonical root is replaced between the identity check and the lock.
        return original_open(outside if path == canonical else path)

    monkeypatch.setattr(workflow_publisher, "_open_directory", open_swapped)
    result = publish_workflow_view(
        _view(), canonical, canonical, canonical / CANONICAL_MANIFEST_NAME
    )

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert os.listdir(canonical) == []
    assert _snapshot(outside) == before


@pytest.mark.parametrize("spelling", ("../escape/x.md", "/abs/x.md", "a/../../x.md"))
def test_parent_walk_refuses_unsafe_paths_without_creating_anything(
    tmp_path: Path, spelling: str
) -> None:
    _canonical, target, _outside = _roots(tmp_path)
    descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(PublishError) as refused:
            workflow_publisher._open_parent(descriptor, _p(spelling), create=True)
    finally:
        os.close(descriptor)

    assert refused.value.drift_class is DriftClass.COLLISION
    assert not (tmp_path / "escape").exists()
    assert not Path("/abs").exists()
    assert os.listdir(target) == []


def test_linked_target_root_is_a_lock_error(tmp_path: Path) -> None:
    canonical, target, _outside = _roots(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    result = publish_workflow_view(_view(), canonical, alias, alias / RUNTIME_MANIFEST_NAME)

    assert result.lock_error is not None
    assert os.listdir(target) == []


def test_linked_canonical_root_is_a_lock_error(tmp_path: Path) -> None:
    canonical, target, _outside = _roots(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(canonical, target_is_directory=True)

    result = publish_workflow_view(_view(), alias, target, target / RUNTIME_MANIFEST_NAME)

    assert result.lock_error is not None
    assert os.listdir(target) == []


def test_standalone_cli_refuses_a_linked_target_parent(tmp_path: Path) -> None:
    canonical, target, outside = _roots(tmp_path)
    view = tmp_path / "view"
    (view / "agents").mkdir(parents=True)
    (view / "agents/probe.md").write_bytes(b"probe\n")
    (canonical / CANONICAL_MANIFEST_NAME).write_text(
        json.dumps({"source": "opencode", "items": []})
    )
    (target / "agents").symlink_to(outside, target_is_directory=True)
    before = _snapshot(outside)

    result = subprocess.run(
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
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == EXIT_CODES[DriftClass.COLLISION]
    assert _snapshot(outside) == before


# --- source reads ------------------------------------------------------------------

_GOLDEN_TREE = {
    "AGENTS.md": (b"Instructions.\n", False),
    "a.txt": (b"a\n", False),
    "a/z.md": (b"z\n", False),
    "a.b/c.md": (b"c\n", False),
    "skills/demo/SKILL.md": (b"skill\n", False),
    "scripts/run.py": (b"#!/usr/bin/env python3\n", True),
    "__pycache__/x.pyc": (b"\x00", False),
    "skills/synced/b/s/SKILL.md": (b"synced\n", False),
}
# Digest of _GOLDEN_TREE computed by the path-based reader this module replaced.
_GOLDEN_FINGERPRINT = "3afd9d0631a433d66325b9de40b37544701ac19017750da64354f8e34ca33449"


def _golden_source(root: Path) -> Path:
    for relative, (content, executable) in _GOLDEN_TREE.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o755 if executable else 0o644)
    return root


def test_source_fingerprint_matches_the_previous_reader(tmp_path: Path) -> None:
    source = _golden_source(tmp_path / "source")

    residue = runtime_residue_prefixes("claude")
    view = snapshot_file_view(source, source="claude", residue_prefixes=residue)

    assert view.source_fingerprint == _GOLDEN_FINGERPRINT
    assert [item.relative_path.as_posix() for item in view.files] == [
        "AGENTS.md",
        "a/z.md",
        "a.b/c.md",
        "a.txt",
        "scripts/run.py",
        "skills/demo/SKILL.md",
    ]


@pytest.mark.parametrize("kind", ("directory-link", "file-link", "fifo"))
def test_source_tree_refuses_links_and_special_files(tmp_path: Path, kind: str) -> None:
    _canonical, _target, outside = _roots(tmp_path)
    source = tmp_path / "source"
    (source / "agents").mkdir(parents=True)
    (source / "AGENTS.md").write_bytes(b"x\n")
    if kind == "directory-link":
        (source / "skills").symlink_to(outside, target_is_directory=True)
    elif kind == "file-link":
        (source / "agents/x.md").symlink_to(outside / "keep.md")
    else:
        os.mkfifo(source / "agents/x.md")

    with pytest.raises(PublishError) as refused:
        snapshot_file_view(source, source="claude")
    with pytest.raises(PublishError) as changed:
        fingerprint_source_inputs(source)

    assert refused.value.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert changed.value.drift_class is DriftClass.SOURCE_CHANGED


@pytest.mark.parametrize(
    "residue", ("skills/synced", "skills/synced/bucket", "scripts/__pycache__")
)
def test_links_at_or_inside_runtime_residue_stay_ignored(tmp_path: Path, residue: str) -> None:
    _canonical, _target, outside = _roots(tmp_path)
    source = tmp_path / "source"
    (source / residue).parent.mkdir(parents=True)
    (source / "AGENTS.md").write_bytes(b"x\n")
    (source / residue).symlink_to(outside, target_is_directory=True)

    residue_prefixes = runtime_residue_prefixes("claude")
    view = snapshot_file_view(source, source="claude", residue_prefixes=residue_prefixes)

    assert [item.relative_path for item in view.files] == [_p("AGENTS.md")]


def test_linked_source_root_is_refused(tmp_path: Path) -> None:
    _canonical, _target, outside = _roots(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PublishError) as refused:
        snapshot_file_view(alias, source="claude")

    assert refused.value.drift_class is DriftClass.INVALID_OR_SEMANTIC


def test_source_read_failure_keeps_each_wrapper_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "AGENTS.md").write_bytes(b"x\n")

    def fail_read(_pin: int) -> bytes:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(workflow_publisher, "_read_pinned", fail_read)
    with pytest.raises(PublishError) as snapshot:
        snapshot_file_view(source, source="claude")
    with pytest.raises(PublishError) as fingerprint:
        fingerprint_source_inputs(source)

    assert snapshot.value.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert fingerprint.value.drift_class is DriftClass.SOURCE_CHANGED


def _native_input_case(tmp_path: Path) -> tuple[Path, Path, Path]:
    config = tmp_path / "config"
    source = config / "claude"
    source.mkdir(parents=True)
    (source / "AGENTS.md").write_bytes(b"x\n")
    outside = tmp_path / "outside"
    (outside / "hooks").mkdir(parents=True)
    (outside / "hooks/security_guard.py").write_bytes(_SECRET)
    (outside / "security_guard.py").write_bytes(_SECRET)
    return config, source, outside


def test_native_input_behind_a_linked_component_is_source_changed(tmp_path: Path) -> None:
    config, source, outside = _native_input_case(tmp_path)
    (config / "codex").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PublishError) as changed:
        fingerprint_source_inputs(source, (config / "codex/hooks/security_guard.py",))

    assert changed.value.drift_class is DriftClass.SOURCE_CHANGED


@pytest.mark.parametrize(
    "spelling", ("claude/../../outside/security_guard.py", "../outside/security_guard.py")
)
def test_native_input_escaping_the_canonical_root_is_source_changed(
    tmp_path: Path, spelling: str
) -> None:
    config, source, _outside = _native_input_case(tmp_path)

    with pytest.raises(PublishError) as changed:
        fingerprint_source_inputs(source, (config / spelling,))

    assert changed.value.drift_class is DriftClass.SOURCE_CHANGED


def test_missing_native_input_keeps_its_digest_record(tmp_path: Path) -> None:
    config, source, _outside = _native_input_case(tmp_path)

    first = fingerprint_source_inputs(source, (config / "codex/hooks/security_guard.py",))
    (config / "codex/hooks").mkdir(parents=True)
    (config / "codex/hooks/security_guard.py").write_bytes(b"guard\n")
    second = fingerprint_source_inputs(source, (config / "codex/hooks/security_guard.py",))

    assert first != second


# --- the shared reader ------------------------------------------------------------


@pytest.mark.parametrize("spelling", ("../outside/keep.md", "/etc/hostname", ""))
def test_read_regular_file_refuses_unsafe_paths_before_any_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    _canonical, target, _outside = _roots(tmp_path)

    def no_open(*_args: object, **_kwargs: object) -> int:
        pytest.fail("an unsafe path reached a system call")

    monkeypatch.setattr(workflow_publisher.os, "open", no_open)
    with pytest.raises(PublishError) as refused:
        read_regular_file(target, _p(spelling))

    assert refused.value.drift_class is DriftClass.COLLISION


@pytest.mark.parametrize("component", ("a", "a/b"))
def test_read_regular_file_refuses_linked_components(tmp_path: Path, component: str) -> None:
    _canonical, target, outside = _roots(tmp_path)
    (outside / "b").mkdir()
    (outside / "b/c.md").write_bytes(_SECRET)
    (outside / "c.md").write_bytes(_SECRET)
    _link_into_outside(target, component, outside)

    with pytest.raises(PublishError) as refused:
        read_regular_file(target, _p("a/b/c.md"))

    assert refused.value.drift_class is DriftClass.COLLISION


def test_read_regular_file_reports_absence_and_mode(tmp_path: Path) -> None:
    _canonical, target, _outside = _roots(tmp_path)
    script = target / "bin/run.py"
    script.parent.mkdir()
    script.write_bytes(b"#!\n")
    script.chmod(0o755)

    assert read_regular_file(target, _p("bin/run.py")) == (b"#!\n", True)
    assert read_regular_file(target, _p("bin/missing.py")) is None
    assert read_regular_file(target, _p("missing/run.py")) is None
    assert read_regular_file(tmp_path / "no-root", _p("run.py")) is None


def test_read_regular_file_refuses_a_fifo_leaf(tmp_path: Path) -> None:
    _canonical, target, _outside = _roots(tmp_path)
    os.mkfifo(target / "pipe.md")

    with pytest.raises(PublishError) as refused:
        read_regular_file(target, _p("pipe.md"))

    assert refused.value.drift_class is DriftClass.COLLISION
    assert stat.S_ISFIFO((target / "pipe.md").lstat().st_mode)


# --- failure modes ---------------------------------------------------------------


def test_lease_on_a_renamed_root_is_rejected(tmp_path: Path) -> None:
    canonical, _target, _outside = _roots(tmp_path)

    with canonical_lock(canonical, exclusive=True) as lease:
        canonical.rename(tmp_path / "canonical.old")
        canonical.mkdir()
        result = publish_workflow_view(
            _view(),
            canonical,
            canonical,
            canonical / CANONICAL_MANIFEST_NAME,
            canonical_lease=lease,
        )

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert os.listdir(canonical) == []
    assert os.listdir(tmp_path / "canonical.old") == []


@pytest.mark.parametrize("name", ("a/b", "", ".", ".."))
def test_entry_primitives_take_one_component_only(tmp_path: Path, name: str) -> None:
    _canonical, target, outside = _roots(tmp_path)
    (target / "a").symlink_to(outside, target_is_directory=True)
    (outside / "b").write_bytes(_SECRET)
    descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(PublishError) as read:
            workflow_publisher._read_at(descriptor, name)
        with pytest.raises(PublishError) as written:
            workflow_publisher._replace_at(descriptor, name, b"x", False)
    finally:
        os.close(descriptor)

    assert read.value.drift_class is DriftClass.COLLISION
    assert written.value.drift_class is DriftClass.COLLISION
    assert (outside / "b").read_bytes() == _SECRET


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_unreadable_parent_stays_a_write_error(tmp_path: Path) -> None:
    canonical, target, _outside = _roots(tmp_path)
    (target / "skills").mkdir(mode=0o000)
    try:
        result = _publish(canonical, target, _view(_DEEP))
    finally:
        (target / "skills").chmod(0o755)

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert result.write_error is not None
    assert result.write_error.errno == errno.EACCES


def test_missing_proc_fails_closed_instead_of_reading_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target, _outside = _roots(tmp_path)
    (target / "AGENTS.md").write_bytes(b"unmanaged operator file\n")
    original_open = os.open

    def no_proc(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if str(path).startswith("/proc/self/fd/"):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workflow_publisher.os, "open", no_proc)
    result = _publish(canonical, target, _view())
    with pytest.raises(PublishError) as read:
        read_regular_file(target, _p("AGENTS.md"))

    assert result.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert (target / "AGENTS.md").read_bytes() == b"unmanaged operator file\n"
    assert read.value.drift_class is DriftClass.INVALID_OR_SEMANTIC


@pytest.fixture
def descriptor_limit() -> Iterator[Callable[[int], None]]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)

    def set_soft(limit: int) -> None:
        if hard != resource.RLIM_INFINITY and limit > hard:
            pytest.skip("hard descriptor limit too low")
        resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))

    yield set_soft
    resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def _chain(source: Path, depth: int) -> None:
    source.mkdir()
    (source / "AGENTS.md").write_bytes(b"x\n")
    descriptor = _REAL_OPEN(source, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _level in range(depth):
            os.mkdir("d", dir_fd=descriptor)
            child = _REAL_OPEN("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        leaf = _REAL_OPEN("leaf.md", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=descriptor)
        os.write(leaf, b"deep\n")
        os.close(leaf)
    finally:
        os.close(descriptor)


def test_deep_source_chain_is_walked_without_recursion(
    tmp_path: Path, descriptor_limit: Callable[[int], None]
) -> None:
    descriptor_limit(4096)
    _chain(tmp_path / "source", 1100)

    view = snapshot_file_view(tmp_path / "source", source="claude")

    assert len(view.files) == 2
    assert view.files[-1].relative_path.name == "leaf.md"
    assert len(view.files[-1].relative_path.parts) == 1101


def test_wide_source_tree_holds_no_descriptor_per_sibling(
    tmp_path: Path, descriptor_limit: Callable[[int], None]
) -> None:
    source = tmp_path / "source"
    for index in range(300):
        (source / f"skills/s{index:03d}").mkdir(parents=True)
        (source / f"skills/s{index:03d}/SKILL.md").write_bytes(b"s\n")
    descriptor_limit(128)

    view = snapshot_file_view(source, source="claude")

    assert len(view.files) == 300


def test_exhausted_descriptors_fail_closed(
    tmp_path: Path, descriptor_limit: Callable[[int], None]
) -> None:
    _chain(tmp_path / "source", 200)
    descriptor_limit(64)
    before = len(os.listdir("/proc/self/fd"))

    with pytest.raises(PublishError) as refused:
        snapshot_file_view(tmp_path / "source", source="claude")

    assert refused.value.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert len(os.listdir("/proc/self/fd")) == before


def test_directory_relinked_between_stat_and_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _canonical, _target, outside = _roots(tmp_path)
    (outside / "secret.md").write_bytes(_SECRET)
    source = tmp_path / "source"
    (source / "agents").mkdir(parents=True)
    (source / "AGENTS.md").write_bytes(b"x\n")
    (source / "agents/reviewer.md").write_bytes(b"r\n")
    original_stat = os.stat
    relinked: list[bool] = []

    def stat_then_relink(
        path: Any, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> Any:
        info = original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == "agents" and dir_fd is not None and not relinked:
            relinked.append(True)
            os.rename("agents", "agents.real", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.symlink(str(outside), "agents", dir_fd=dir_fd)
        return info

    monkeypatch.setattr(workflow_publisher.os, "stat", stat_then_relink)
    with pytest.raises(PublishError) as refused:
        snapshot_file_view(source, source="claude")
    (source / "agents").unlink()
    (source / "agents.real").rename(source / "agents")
    relinked.clear()
    with pytest.raises(PublishError) as changed:
        fingerprint_source_inputs(source)

    assert refused.value.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert changed.value.drift_class is DriftClass.SOURCE_CHANGED


def _open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.parametrize("kind", ("directory-link", "file-link", "fifo"))
def test_refused_walk_closes_every_descriptor(tmp_path: Path, kind: str) -> None:
    _canonical, _target, outside = _roots(tmp_path)
    source = tmp_path / "source"
    deep = source / "a/b/c"
    deep.mkdir(parents=True)
    (source / "AGENTS.md").write_bytes(b"x\n")
    if kind == "directory-link":
        (deep / "skills").symlink_to(outside, target_is_directory=True)
    elif kind == "file-link":
        (deep / "x.md").symlink_to(outside / "keep.md")
    else:
        os.mkfifo(deep / "x.md")
    before = _open_descriptors()

    with pytest.raises(PublishError):
        snapshot_file_view(source, source="claude")

    assert _open_descriptors() == before
