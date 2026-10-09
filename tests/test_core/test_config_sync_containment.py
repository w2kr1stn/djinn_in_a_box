"""Config sync never follows a symlink in the container-writable config trees.

Each test links part of `config/` to a sibling `outside` tree of regular-file
sentinels and fails as soon as anything below `outside` is opened.
"""

from __future__ import annotations

import builtins
import os
import shutil
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

import djinn_in_a_box.core.config_sync as sync_module
from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig
from djinn_in_a_box.core.config_sync import (
    MANIFEST_NAME,
    DriftClass,
    audit_config_sync,
    load_canonical_delivery_view,
    sync_config,
)
from djinn_in_a_box.core.config_sync_adapters import read_native_only_workflow
from djinn_in_a_box.core.workflow_publisher import WorkflowView

_AGENT = "---\nname: reviewer\ndescription: Reviews.\n---\nReview.\n"


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
    (project / "config/claude/AGENTS.md").write_text("Instructions.\n")
    (project / "config/claude/agents").mkdir()
    (project / "config/claude/agents/reviewer.md").write_text(_AGENT)
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    config_path = tmp_path / "operator.toml"
    save_config(
        AppConfig(
            code_dir=code_dir,
            config_root=tmp_path / "runtime",
            config_sync=ConfigSyncConfig(source="claude"),
        ),
        config_path,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    return project, config_path, outside


def _identities(root: Path) -> set[tuple[int, int]]:
    paths = (root, *root.rglob("*"))
    return {(info.st_dev, info.st_ino) for info in (path.lstat() for path in paths)}


@pytest.fixture
def forbid_outside_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Path]]:
    """Fail on any open below a registered root, by inode, however it is reached."""
    roots: list[Path] = []
    original_os_open = os.open
    original_open: Any = builtins.open

    def check(descriptor: int, label: object) -> None:
        info = os.fstat(descriptor)
        if any((info.st_dev, info.st_ino) in _identities(root) for root in roots):
            pytest.fail(f"opened {label!r} below a forbidden root")

    def spy_os_open(
        path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        descriptor = original_os_open(path, flags, mode, dir_fd=dir_fd)
        if not flags & os.O_PATH:
            check(descriptor, path)
        return descriptor

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        handle: Any = original_open(file, *args, **kwargs)
        check(int(handle.fileno()), file)
        return handle

    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(builtins, "open", spy_open)
    yield roots


def _mirror_into_outside(project: Path, relative: str, outside: Path) -> None:
    """Move a config subtree to `outside` and leave a link in its place."""
    source = project / "config" / relative
    shutil.move(source, outside / source.name)
    source.symlink_to(outside / source.name, target_is_directory=(outside / source.name).is_dir())


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_linked_source_directory_is_refused_unread(
    tmp_path: Path, forbid_outside_reads: list[Path]
) -> None:
    project, config_path, outside = _workspace(tmp_path)
    _mirror_into_outside(project, "claude/agents", outside)
    before = _snapshot(outside)
    forbid_outside_reads.append(outside)

    result = sync_config(project, config_path=config_path)

    assert not result.success
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert not (project / "config" / MANIFEST_NAME).exists()
    assert _snapshot(outside) == before


def test_source_relinked_before_the_snapshot_is_never_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forbid_outside_reads: list[Path]
) -> None:
    project, config_path, outside = _workspace(tmp_path)
    original = sync_module.fingerprint_source_inputs
    calls = 0

    def fingerprint_then_relink(source_root: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal calls
        fingerprint = original(source_root, *args, **kwargs)
        calls += 1
        if calls == 3:  # the last input fingerprint before the private snapshot
            _mirror_into_outside(project, "claude/agents", outside)
            forbid_outside_reads.append(outside)
        return fingerprint

    monkeypatch.setattr(sync_module, "fingerprint_source_inputs", fingerprint_then_relink)
    result = sync_config(project, config_path=config_path)

    assert calls >= 3
    assert not result.success
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_source_edit_after_the_read_is_source_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _config_path, _outside = _workspace(tmp_path)
    original = sync_module.snapshot_file_view
    edited = False

    def read_then_edit(root: Path, **kwargs: Any) -> WorkflowView:
        nonlocal edited
        view = original(root, **kwargs)
        if not edited:
            edited = True
            (project / "config/claude/AGENTS.md").write_text("Edited.\n")
        return view

    monkeypatch.setattr(sync_module, "snapshot_file_view", read_then_edit)
    with pytest.raises(sync_module._BuildError) as changed:  # pyright: ignore[reportPrivateUsage]
        sync_module._snapshot_build(project, "claude")  # pyright: ignore[reportPrivateUsage]

    assert changed.value.drift is DriftClass.SOURCE_CHANGED


def test_audit_and_sync_never_follow_a_linked_output_directory(
    tmp_path: Path, forbid_outside_reads: list[Path]
) -> None:
    project, config_path, outside = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    _mirror_into_outside(project, "opencode/agents", outside)
    before = _snapshot(outside)
    forbid_outside_reads.append(outside)

    audit = audit_config_sync(project, config_path=config_path)
    synced = sync_config(project, config_path=config_path)

    assert not audit.clean
    assert not synced.success
    assert _snapshot(outside) == before


def test_linked_canonical_manifest_is_invalid_and_unread(
    tmp_path: Path, forbid_outside_reads: list[Path]
) -> None:
    project, config_path, outside = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    _mirror_into_outside(project, MANIFEST_NAME, outside)
    before = _snapshot(outside)
    forbid_outside_reads.append(outside)

    audit = audit_config_sync(project, config_path=config_path)
    synced = sync_config(project, config_path=config_path)
    loaded = load_canonical_delivery_view(project, "codex", config_path=config_path)

    assert audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert not synced.success
    assert not loaded.success
    assert _snapshot(outside) == before


def test_linked_output_carrier_is_a_collision(
    tmp_path: Path, forbid_outside_reads: list[Path]
) -> None:
    project, config_path, outside = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    _mirror_into_outside(project, "codex/config.toml", outside)
    forbid_outside_reads.append(outside)

    audit = audit_config_sync(project, config_path=config_path)

    assert audit.drift_classes == (DriftClass.COLLISION,)


def test_native_only_reads_refuse_links_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    (root / "plugins-real").mkdir(parents=True)
    (root / "plugins-real/ready-notify.js").write_text("export const Plugin = () => ({});\n")
    (root / "plugins").symlink_to(root / "plugins-real", target_is_directory=True)

    result = read_native_only_workflow(root, "opencode")

    assert result.artifacts == ()
    assert any(
        issue.identifier.startswith("external-path:") for issue in result.validation_issues
    )


def test_missing_native_only_root_reports_nothing(tmp_path: Path) -> None:
    result = read_native_only_workflow(tmp_path / "absent", "codex")

    assert result.artifacts == ()
    assert result.validation_issues == ()


def test_native_only_reads_of_a_regular_tree_are_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "opencode"
    (root / "plugins").mkdir(parents=True)
    plugin = root / "plugins/ready-notify.js"
    plugin.write_text("export const Plugin = () => ({});\n")

    result = read_native_only_workflow(root, "opencode")

    assert result.validation_issues == ()
    paths = [item.source_path for item in result.artifacts]
    assert paths == [PurePosixPath("plugins/ready-notify.js")]
