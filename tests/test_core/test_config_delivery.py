from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import multiprocessing
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from djinn_in_a_box.core import config_delivery
from djinn_in_a_box.core.config_delivery import (
    DELIVERY_MANIFEST_NAME,
    DeliveryResult,
    DeliveryView,
    deliver_config_view,
)
from djinn_in_a_box.core.config_lock import config_directory_lock
from djinn_in_a_box.core.config_sync_adapters import RenderedFile, SettingsFragment

_p = PurePosixPath


def _file(path: str, content: bytes, *, executable: bool = False) -> RenderedFile:
    return RenderedFile(_p(path), content, f"artifact:{path}", executable)


def _codex_view(
    marker: bytes = b"v1\n",
    *,
    extra_files: tuple[RenderedFile, ...] = (),
    fallback: bytes | None = b'["CLAUDE.md"]',
) -> DeliveryView:
    fragments = (
        ()
        if fallback is None
        else (
            SettingsFragment(
                _p("config.toml"),
                ("project_doc_fallback_filenames",),
                fallback,
                "bridge",
            ),
        )
    )
    return DeliveryView(
        "codex",
        (
            _file("AGENTS.md", b"agents-" + marker),
            _file("CLAUDE.md", b"claude-" + marker),
            *extra_files,
        ),
        fragments,
    )


def _manifest(root: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads((root / DELIVERY_MANIFEST_NAME).read_bytes()))


def _assert_clean_failure(result: DeliveryResult, root: Path) -> None:
    assert not result.success
    assert result.problems
    assert not any(path.name.startswith(".djinn-delivery-stage-") for path in root.iterdir())


def _hold_directory_lock(root: str, ready: ProcessEvent, release: ProcessEvent) -> None:
    with config_directory_lock(Path(root), exclusive=True):
        ready.set()
        assert release.wait(10)


def test_first_delivery_creates_missing_outputs_and_strict_manifest(tmp_path: Path) -> None:
    result = deliver_config_view(_codex_view(), tmp_path)

    assert result == DeliveryResult(
        success=True,
        changed_paths=(_p("AGENTS.md"), _p("CLAUDE.md"), _p("config.toml")),
    )
    assert (tmp_path / "AGENTS.md").read_bytes() == b"agents-v1\n"
    assert (tmp_path / "CLAUDE.md").read_bytes() == b"claude-v1\n"
    assert "CLAUDE.md" in (tmp_path / "config.toml").read_text()
    manifest = _manifest(tmp_path)
    assert set(manifest) == {"schema_version", "tool", "files", "fragments"}
    assert manifest["schema_version"] == 1
    assert manifest["tool"] == "codex"


def test_first_delivery_adopts_only_exact_outputs(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_bytes(b"agents-v1\n")
    (tmp_path / "CLAUDE.md").write_bytes(b"claude-v1\n")
    carrier = b'project_doc_fallback_filenames=["CLAUDE.md"]\nneighbor="kept"\n'
    (tmp_path / "config.toml").write_bytes(carrier)

    result = deliver_config_view(_codex_view(), tmp_path)

    assert result.success
    assert result.changed_paths == ()
    assert (tmp_path / "config.toml").read_bytes() == carrier
    assert (tmp_path / DELIVERY_MANIFEST_NAME).is_file()


@pytest.mark.parametrize("collision", ["file", "fragment"])
def test_first_delivery_collision_has_zero_mutation(tmp_path: Path, collision: str) -> None:
    if collision == "file":
        (tmp_path / "AGENTS.md").write_bytes(b"locally-edited")
    else:
        (tmp_path / "config.toml").write_text(
            'project_doc_fallback_filenames=["OTHER.md"]\nneighbor="kept"\n'
        )
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    result = deliver_config_view(_codex_view(), tmp_path)

    _assert_clean_failure(result, tmp_path)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert not (tmp_path / DELIVERY_MANIFEST_NAME).exists()


def test_clean_redelivery_is_a_noop(tmp_path: Path) -> None:
    assert deliver_config_view(_codex_view(), tmp_path).success
    mtimes = {path.name: path.stat().st_mtime_ns for path in tmp_path.iterdir() if path.is_file()}

    result = deliver_config_view(_codex_view(), tmp_path)

    assert result == DeliveryResult(success=True)
    assert {
        path.name: path.stat().st_mtime_ns for path in tmp_path.iterdir() if path.is_file()
    } == mtimes


def test_version_update_and_missing_owned_file_repair(tmp_path: Path) -> None:
    assert deliver_config_view(_codex_view(), tmp_path).success
    (tmp_path / "CLAUDE.md").unlink()

    repaired = deliver_config_view(_codex_view(), tmp_path)
    updated = deliver_config_view(_codex_view(b"v2\n"), tmp_path)

    assert repaired.success
    assert repaired.changed_paths == (_p("CLAUDE.md"),)
    assert updated.success
    assert updated.changed_paths == (_p("AGENTS.md"), _p("CLAUDE.md"))
    assert (tmp_path / "AGENTS.md").read_bytes() == b"agents-v2\n"


def test_managed_file_edit_blocks_update_without_mutation(tmp_path: Path) -> None:
    assert deliver_config_view(_codex_view(), tmp_path).success
    (tmp_path / "AGENTS.md").write_bytes(b"private-user-content")
    manifest_before = (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes()

    result = deliver_config_view(_codex_view(b"v2\n"), tmp_path)

    _assert_clean_failure(result, tmp_path)
    assert {problem.identifier for problem in result.problems} == {"managed-file-drift"}
    assert (tmp_path / "AGENTS.md").read_bytes() == b"private-user-content"
    assert (tmp_path / "CLAUDE.md").read_bytes() == b"claude-v1\n"
    assert (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes() == manifest_before


def test_stale_owned_file_is_removed_but_edited_stale_file_blocks(tmp_path: Path) -> None:
    agent = b'name = "worker"\ndescription = "Works"\ndeveloper_instructions = "Work carefully."\n'
    extra = _file("agents/worker.toml", agent)
    assert deliver_config_view(_codex_view(extra_files=(extra,)), tmp_path).success

    blocked_path = tmp_path / "agents" / "worker.toml"
    blocked_path.write_bytes(b'user="edit"\n')
    blocked = deliver_config_view(_codex_view(), tmp_path)
    assert not blocked.success
    assert {problem.identifier for problem in blocked.problems} == {"stale-file-drift"}
    assert blocked_path.exists()

    blocked_path.write_bytes(agent)
    removed = deliver_config_view(_codex_view(), tmp_path)
    assert removed.success
    assert removed.removed_paths == (_p("agents/worker.toml"),)
    assert not blocked_path.exists()


def test_carrier_merge_preserves_unmanaged_neighbors_and_removes_owned_stale_key(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.toml").write_text('neighbor="kept"\n')
    assert deliver_config_view(_codex_view(), tmp_path).success

    result = deliver_config_view(_codex_view(fallback=None), tmp_path)

    assert result.success
    assert result.changed_paths == (_p("config.toml"),)
    carrier = (tmp_path / "config.toml").read_text()
    assert 'neighbor = "kept"' in carrier
    assert "project_doc_fallback_filenames" not in carrier


def test_json_carrier_merge_is_narrow_and_preserves_neighbors(tmp_path: Path) -> None:
    view = DeliveryView(
        "claude",
        (_file("CLAUDE.md", b"claude\n"), _file("AGENTS.md", b"agents\n")),
        (
            SettingsFragment(
                _p("settings.json"),
                ("hooks", "SessionStart"),
                b'[{"hooks":[{"type":"command","command":"run"}]}]',
                "startup",
            ),
        ),
    )
    (tmp_path / "settings.json").write_text(
        json.dumps({"neighbor": {"retained": True}, "hooks": {"Other": [1]}})
    )

    result = deliver_config_view(view, tmp_path)

    assert result.success
    carrier = cast(dict[str, object], json.loads((tmp_path / "settings.json").read_bytes()))
    assert carrier["neighbor"] == {"retained": True}
    assert cast(dict[str, object], carrier["hooks"])["Other"] == [1]
    assert "SessionStart" in cast(dict[str, object], carrier["hooks"])


def test_unlisted_files_and_directories_are_never_claimed_or_removed(tmp_path: Path) -> None:
    neighbor = tmp_path / "private" / "notes.txt"
    neighbor.parent.mkdir()
    neighbor.write_bytes(b"keep-me")

    assert deliver_config_view(_codex_view(), tmp_path).success
    assert deliver_config_view(_codex_view(b"v2\n"), tmp_path).success

    assert neighbor.read_bytes() == b"keep-me"


def test_edited_stale_fragment_blocks_removal(tmp_path: Path) -> None:
    assert deliver_config_view(_codex_view(), tmp_path).success
    (tmp_path / "config.toml").write_text(
        'project_doc_fallback_filenames=["PRIVATE.md"]\nneighbor="kept"\n'
    )

    result = deliver_config_view(_codex_view(fallback=None), tmp_path)

    _assert_clean_failure(result, tmp_path)
    assert {problem.identifier for problem in result.problems} == {"stale-fragment-drift"}
    assert "PRIVATE.md" in (tmp_path / "config.toml").read_text()


def test_carrier_neighbor_race_is_remerged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.toml").write_text('neighbor="before"\n')
    original = config_delivery._read_snapshot
    reads = 0

    def racing_read(root_fd: int, path: PurePosixPath) -> config_delivery._Snapshot | None:
        nonlocal reads
        if path == _p("config.toml"):
            reads += 1
            if reads == 2:
                (tmp_path / "config.toml").write_text('neighbor="after"\n')
        return original(root_fd, path)

    monkeypatch.setattr(config_delivery, "_read_snapshot", racing_read)
    result = deliver_config_view(_codex_view(), tmp_path)

    assert result.success
    carrier = (tmp_path / "config.toml").read_text()
    assert 'neighbor = "after"' in carrier
    assert "project_doc_fallback_filenames" in carrier


def test_stale_fragment_race_is_retryable_and_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert deliver_config_view(_codex_view(), tmp_path).success
    original = config_delivery._read_snapshot
    reads = 0

    def racing_read(root_fd: int, path: PurePosixPath) -> config_delivery._Snapshot | None:
        nonlocal reads
        if path == _p("config.toml"):
            reads += 1
            if reads == 2:
                (tmp_path / "config.toml").write_text(
                    'project_doc_fallback_filenames=["PRIVATE.md"]\n'
                )
        return original(root_fd, path)

    monkeypatch.setattr(config_delivery, "_read_snapshot", racing_read)
    result = deliver_config_view(_codex_view(fallback=None), tmp_path)

    assert not result.success
    assert result.retryable
    assert "PRIVATE.md" in (tmp_path / "config.toml").read_text()


def test_new_unmanaged_file_appearing_with_target_parent_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _p("skills/demo/SKILL.md")
    skill = b"---\nname: demo\ndescription: Demo skill\n---\n\nManaged.\n"
    view = _codex_view(extra_files=(_file(target.as_posix(), skill),))
    original = config_delivery._parent_fd
    attacked = False

    @contextmanager
    def racing_parent(root_fd: int, path: PurePosixPath, *, create: bool) -> Iterator[int]:
        nonlocal attacked
        if path == target and create and not attacked:
            sentinel = tmp_path / Path(*target.parts)
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_bytes(b"UNMANAGED-SENTINEL\n")
            attacked = True
        with original(root_fd, path, create=create) as descriptor:
            yield descriptor

    monkeypatch.setattr(config_delivery, "_parent_fd", racing_parent)

    result = deliver_config_view(view, tmp_path)

    assert attacked is True
    assert result.success is False
    assert result.retryable is True
    assert (tmp_path / Path(*target.parts)).read_bytes() == b"UNMANAGED-SENTINEL\n"


def test_stale_parent_swap_never_unlinks_replacement_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _p("skills/demo/SKILL.md")
    skill = b"---\nname: demo\ndescription: Demo skill\n---\n\nManaged.\n"
    with_skill = _codex_view(extra_files=(_file(target.as_posix(), skill),))
    assert deliver_config_view(with_skill, tmp_path).success
    original = config_delivery._unlink_at
    attacked = False

    def racing_unlink(root_fd: int, path: PurePosixPath, expected: object) -> None:
        nonlocal attacked
        if path == target and not attacked:
            live_parent = tmp_path / "skills" / "demo"
            live_parent.rename(tmp_path / "skills" / "demo-parked")
            live_parent.mkdir()
            (live_parent / "SKILL.md").write_bytes(b"UNMANAGED-SENTINEL\n")
            attacked = True
        original(root_fd, path, expected)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(config_delivery, "_unlink_at", racing_unlink)

    result = deliver_config_view(_codex_view(), tmp_path)

    assert attacked is True
    assert result.success is False
    assert result.retryable is True
    assert (tmp_path / Path(*target.parts)).read_bytes() == b"UNMANAGED-SENTINEL\n"


def test_existing_target_changed_after_final_read_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert deliver_config_view(_codex_view(), tmp_path).success
    manifest_before = (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes()
    original = config_delivery._read_snapshot_at
    reads = 0

    def racing_read(parent_fd: int, name: str) -> config_delivery._Snapshot | None:
        nonlocal reads
        snapshot = original(parent_fd, name)
        if name == "AGENTS.md":
            reads += 1
            if reads == 3:
                (tmp_path / name).write_bytes(b"UNMANAGED-SENTINEL\n")
        return snapshot

    monkeypatch.setattr(config_delivery, "_read_snapshot_at", racing_read)
    result = deliver_config_view(_codex_view(b"v2\n"), tmp_path)

    assert not result.success and result.retryable
    assert (tmp_path / "AGENTS.md").read_bytes() == b"UNMANAGED-SENTINEL\n"
    assert (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes() == manifest_before


def test_restore_collision_preserves_quarantine_and_never_claims_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert deliver_config_view(_codex_view(), tmp_path).success
    manifest_before = (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes()
    original_read = config_delivery._read_snapshot_at
    original_rename = config_delivery.rename_noreplace
    reads = 0
    collided = False

    def racing_read(parent_fd: int, name: str) -> config_delivery._Snapshot | None:
        nonlocal reads
        snapshot = original_read(parent_fd, name)
        if name == "AGENTS.md":
            reads += 1
            if reads == 3:
                (tmp_path / name).write_bytes(b"FIRST-SENTINEL\n")
        return snapshot

    def colliding_restore(source_fd: int, source: str, target_fd: int, target: str) -> None:
        nonlocal collided
        if source.startswith(".quarantine-") and target == "AGENTS.md" and not collided:
            (tmp_path / target).write_bytes(b"SECOND-SENTINEL\n")
            collided = True
        original_rename(source_fd, source, target_fd, target)

    monkeypatch.setattr(config_delivery, "_read_snapshot_at", racing_read)
    monkeypatch.setattr(config_delivery, "rename_noreplace", colliding_restore)
    result = deliver_config_view(_codex_view(b"v2\n"), tmp_path)

    assert collided
    assert not result.success and result.retryable
    assert {problem.identifier for problem in result.problems} == {"quarantine-preserved"}
    assert (tmp_path / "AGENTS.md").read_bytes() == b"SECOND-SENTINEL\n"
    assert (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes() == manifest_before
    quarantines = list(tmp_path.glob(".djinn-delivery-stage-*/.quarantine-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b"FIRST-SENTINEL\n"

    monkeypatch.undo()
    retry = deliver_config_view(_codex_view(b"v2\n"), tmp_path)
    assert not retry.success
    assert (tmp_path / "AGENTS.md").read_bytes() == b"SECOND-SENTINEL\n"
    assert quarantines[0].read_bytes() == b"FIRST-SENTINEL\n"


def test_quarantine_bookkeeping_failure_requires_recovery_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert deliver_config_view(_codex_view(), tmp_path).success
    manifest_before = (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes()
    original_read = config_delivery._read_snapshot_at
    original_stat = config_delivery.os.stat
    reads = 0
    attacked = False
    failed = False

    def racing_read(parent_fd: int, name: str) -> config_delivery._Snapshot | None:
        nonlocal reads, attacked
        snapshot = original_read(parent_fd, name)
        if name == "AGENTS.md":
            reads += 1
            if reads == 3:
                (tmp_path / name).write_bytes(b"UNIQUE-OPERATOR-EDIT\n")
                attacked = True
        return snapshot

    def failing_stat(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal failed
        if attacked and isinstance(path, str) and path.startswith(".quarantine-") and not failed:
            failed = True
            raise OSError("injected quarantine bookkeeping failure")
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(config_delivery, "_read_snapshot_at", racing_read)
    monkeypatch.setattr(config_delivery.os, "stat", failing_stat)
    result = deliver_config_view(_codex_view(b"v2\n"), tmp_path)
    monkeypatch.undo()

    assert attacked and failed
    assert not result.success and result.retryable
    assert {problem.identifier for problem in result.problems} == {"quarantine-preserved"}
    assert not (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes() == manifest_before
    quarantines = list(tmp_path.glob(".djinn-delivery-stage-*/.quarantine-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b"UNIQUE-OPERATOR-EDIT\n"


def test_stage_name_exhaustion_is_exact_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fixed_token(_length: int) -> str:
        return "fixed"

    monkeypatch.setattr(config_delivery.secrets, "token_hex", _fixed_token)
    (tmp_path / ".djinn-delivery-stage-fixed").mkdir()

    result = deliver_config_view(_codex_view(), tmp_path)

    assert not result.success and result.retryable
    assert {problem.identifier for problem in result.problems} == {"stage-create-failed"}


def test_absent_target_appearing_at_atomic_publish_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config_delivery.rename_noreplace
    attacked = False

    def racing_rename(source_fd: int, source: str, target_fd: int, target: str) -> None:
        nonlocal attacked
        if target == "AGENTS.md" and not attacked:
            (tmp_path / target).write_bytes(b"UNMANAGED-SENTINEL\n")
            attacked = True
        original(source_fd, source, target_fd, target)

    monkeypatch.setattr(config_delivery, "rename_noreplace", racing_rename)
    result = deliver_config_view(_codex_view(), tmp_path)

    assert attacked
    assert not result.success and result.retryable
    assert (tmp_path / "AGENTS.md").read_bytes() == b"UNMANAGED-SENTINEL\n"
    assert not (tmp_path / DELIVERY_MANIFEST_NAME).exists()


def test_stale_target_changed_after_final_read_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _p("skills/demo/SKILL.md")
    skill = b"---\nname: demo\ndescription: Demo skill\n---\n\nManaged.\n"
    assert deliver_config_view(
        _codex_view(extra_files=(_file(target.as_posix(), skill),)), tmp_path
    ).success
    manifest_before = (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes()
    original = config_delivery._read_snapshot_at
    reads = 0

    def racing_read(parent_fd: int, name: str) -> config_delivery._Snapshot | None:
        nonlocal reads
        snapshot = original(parent_fd, name)
        if name == target.name:
            reads += 1
            if reads == 3:
                (tmp_path / Path(*target.parts)).write_bytes(b"UNMANAGED-SENTINEL\n")
        return snapshot

    monkeypatch.setattr(config_delivery, "_read_snapshot_at", racing_read)
    result = deliver_config_view(_codex_view(), tmp_path)

    assert not result.success and result.retryable
    assert (tmp_path / Path(*target.parts)).read_bytes() == b"UNMANAGED-SENTINEL\n"
    assert (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes() == manifest_before


def test_parent_detach_after_open_aborts_before_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _p("skills/demo/SKILL.md")
    v1 = b"---\nname: demo\ndescription: Demo skill\n---\n\nManaged v1.\n"
    v2 = v1.replace(b"v1", b"v2")
    assert deliver_config_view(
        _codex_view(extra_files=(_file(target.as_posix(), v1),)), tmp_path
    ).success
    manifest_before = (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes()
    original = config_delivery._read_snapshot_at
    reads = 0

    def detaching_read(parent_fd: int, name: str) -> config_delivery._Snapshot | None:
        nonlocal reads
        snapshot = original(parent_fd, name)
        if name == target.name:
            reads += 1
            if reads == 3:
                parent = tmp_path / "skills/demo"
                parent.rename(tmp_path / "skills/demo-parked")
                parent.mkdir()
                (parent / target.name).write_bytes(b"UNMANAGED-SENTINEL\n")
        return snapshot

    monkeypatch.setattr(config_delivery, "_read_snapshot_at", detaching_read)
    result = deliver_config_view(_codex_view(extra_files=(_file(target.as_posix(), v2),)), tmp_path)

    assert not result.success and result.retryable
    assert (tmp_path / Path(*target.parts)).read_bytes() == b"UNMANAGED-SENTINEL\n"
    assert (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes() == manifest_before


def test_root_detach_immediately_before_manifest_suppresses_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    assert deliver_config_view(_codex_view(), root).success
    original = config_delivery._verify_final_snapshots
    attacked = False

    def detaching_verify(root_fd: int, expected: object) -> None:
        nonlocal attacked
        original(root_fd, expected)  # pyright: ignore[reportArgumentType]
        if not attacked:
            root.rename(tmp_path / "runtime-parked")
            root.mkdir()
            (root / "UNMANAGED").write_text("retain")
            attacked = True

    monkeypatch.setattr(config_delivery, "_verify_final_snapshots", detaching_verify)
    result = deliver_config_view(_codex_view(b"v2\n"), root)

    assert attacked
    assert not result.success and result.retryable
    assert (root / "UNMANAGED").read_text() == "retain"
    assert not (root / DELIVERY_MANIFEST_NAME).exists()


def test_executable_state_is_owned_and_drift_is_blocked(tmp_path: Path) -> None:
    script = _file("scripts/run.py", b"print('ok')\n", executable=True)
    view = _codex_view(extra_files=(script,))
    assert deliver_config_view(view, tmp_path).success
    target = tmp_path / "scripts" / "run.py"
    assert target.stat().st_mode & stat.S_IXUSR

    target.chmod(target.stat().st_mode & ~stat.S_IXUSR)
    result = deliver_config_view(view, tmp_path)

    assert not result.success
    assert {problem.identifier for problem in result.problems} == {"managed-file-drift"}


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_destination_root_must_be_an_existing_real_directory(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "destination"
    if kind == "file":
        root.write_text("not a directory")
    elif kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)

    result = deliver_config_view(_codex_view(), root)

    assert not result.success
    assert {problem.identifier for problem in result.problems} == {"destination-root-unsafe"}


@pytest.mark.parametrize("kind", ["target-symlink", "wrong-type", "parent-symlink"])
def test_symlinks_wrong_types_and_external_parents_are_blocked_before_mutation(
    tmp_path: Path, kind: str
) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    view = _codex_view()
    if kind == "target-symlink":
        (tmp_path / "AGENTS.md").symlink_to(outside / "escaped")
    elif kind == "wrong-type":
        (tmp_path / "AGENTS.md").mkdir()
    else:
        (tmp_path / "agents").symlink_to(outside, target_is_directory=True)
        view = _codex_view(extra_files=(_file("agents/worker.toml", b'name="worker"\n'),))

    result = deliver_config_view(view, tmp_path)

    _assert_clean_failure(result, tmp_path)
    assert not (outside / "escaped").exists()
    assert not (tmp_path / DELIVERY_MANIFEST_NAME).exists()


def test_unsafe_and_unowned_view_paths_are_rejected_without_content_echo(tmp_path: Path) -> None:
    secret = b"SUPER-SECRET-PROMPT"
    view = DeliveryView(
        "codex",
        (
            _file("AGENTS.md", b"agents"),
            _file("CLAUDE.md", b"claude"),
            _file("../escape", secret),
            _file("config.toml", secret),
        ),
        (),
    )

    result = deliver_config_view(view, tmp_path)

    _assert_clean_failure(result, tmp_path)
    assert secret.decode() not in repr(result)
    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.parametrize(
    "manifest",
    [
        b"not-json",
        b'{"schema_version":2,"tool":"codex","files":{},"fragments":[]}',
        b'{"schema_version":true,"tool":"codex","files":{},"fragments":[]}',
        b'{"schema_version":1,"tool":"codex","files":{},"fragments":[],"extra":1}',
        b'{"schema_version":1,"schema_version":1,"tool":"codex","files":{},"fragments":[]}',
        b'{"schema_version":1,"tool":"codex","files":{"settings.json":{"content_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","executable":false}},"fragments":[]}',
        b'{"schema_version":1,"tool":"claude","files":{},"fragments":[]}',
    ],
)
def test_foreign_or_malformed_manifest_never_adopts(tmp_path: Path, manifest: bytes) -> None:
    (tmp_path / DELIVERY_MANIFEST_NAME).write_bytes(manifest)
    before = manifest

    result = deliver_config_view(_codex_view(), tmp_path)

    _assert_clean_failure(result, tmp_path)
    assert (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes() == before
    assert not (tmp_path / "AGENTS.md").exists()


def test_manifest_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "foreign.json"
    target.write_text("{}")
    (tmp_path / DELIVERY_MANIFEST_NAME).symlink_to(target)

    result = deliver_config_view(_codex_view(), tmp_path)

    assert not result.success
    assert {problem.identifier for problem in result.problems} == {"manifest-unsafe"}
    assert target.read_text() == "{}"


@pytest.mark.parametrize("value", [b"null", b'{"nested":null}'])
def test_toml_fragment_must_be_serializable_before_mutation(tmp_path: Path, value: bytes) -> None:
    result = deliver_config_view(_codex_view(fallback=value), tmp_path)

    _assert_clean_failure(result, tmp_path)
    assert {problem.identifier for problem in result.problems} == {"invalid-fragment-value"}
    assert not (tmp_path / DELIVERY_MANIFEST_NAME).exists()


def test_recursive_untrusted_manifest_fails_closed(tmp_path: Path) -> None:
    (tmp_path / DELIVERY_MANIFEST_NAME).write_bytes(b"[" * 2000 + b"]" * 2000)

    result = deliver_config_view(_codex_view(), tmp_path)

    _assert_clean_failure(result, tmp_path)
    assert {problem.identifier for problem in result.problems} == {"manifest-malformed"}


def test_failure_after_partial_publication_is_retry_convergent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = config_delivery._replace_staged
    calls = 0

    def fail_second(
        root_fd: int,
        stage: config_delivery._Stage,
        staged: config_delivery._StagedFile,
        destination: PurePosixPath,
        expected: config_delivery._Snapshot | None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError
        original(root_fd, stage, staged, destination, expected)

    monkeypatch.setattr(config_delivery, "_replace_staged", fail_second)
    interrupted = deliver_config_view(_codex_view(), tmp_path)
    assert not interrupted.success
    assert interrupted.retryable
    assert not (tmp_path / DELIVERY_MANIFEST_NAME).exists()

    monkeypatch.setattr(config_delivery, "_replace_staged", original)
    retried = deliver_config_view(_codex_view(), tmp_path)
    assert retried.success
    assert deliver_config_view(_codex_view(), tmp_path) == DeliveryResult(success=True)


def test_manifest_is_published_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = config_delivery._replace_staged
    published: list[PurePosixPath] = []

    def recording_replace(
        root_fd: int,
        stage: config_delivery._Stage,
        staged: config_delivery._StagedFile,
        destination: PurePosixPath,
        expected: config_delivery._Snapshot | None,
    ) -> None:
        published.append(destination)
        original(root_fd, stage, staged, destination, expected)

    monkeypatch.setattr(config_delivery, "_replace_staged", recording_replace)
    result = deliver_config_view(_codex_view(), tmp_path)

    assert result.success
    assert published[-1] == _p(DELIVERY_MANIFEST_NAME)


@pytest.mark.parametrize(
    ("category", "tamper_mode"),
    [("files", False), ("carriers", False), ("metadata", False), ("files", True)],
)
def test_staged_output_is_verified_before_replace_and_clean_retry_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    tamper_mode: bool,
) -> None:
    original = config_delivery._stage_bytes
    tampered = False

    def tampering_stage(
        root_fd: int,
        stage: config_delivery._Stage,
        staged_category: str,
        path: PurePosixPath,
        content: bytes,
        executable: bool,
    ) -> config_delivery._StagedFile:
        nonlocal tampered
        staged = original(root_fd, stage, staged_category, path, content, executable)
        if staged_category == category and not tampered:
            with config_delivery._stage_parent_fd(
                stage, staged.relative_path, create=False
            ) as parent_fd:
                descriptor = os.open(
                    staged.relative_path.name,
                    os.O_WRONLY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                try:
                    if tamper_mode:
                        os.fchmod(descriptor, os.fstat(descriptor).st_mode ^ stat.S_IXUSR)
                    else:
                        os.ftruncate(descriptor, 0)
                        os.write(descriptor, b"adversarial-stage-content")
                finally:
                    os.close(descriptor)
            tampered = True
        return staged

    monkeypatch.setattr(config_delivery, "_stage_bytes", tampering_stage)
    interrupted = deliver_config_view(_codex_view(), tmp_path)

    assert tampered
    assert not interrupted.success
    assert interrupted.retryable
    assert not (tmp_path / DELIVERY_MANIFEST_NAME).exists()
    assert all(
        path.read_bytes() != b"adversarial-stage-content"
        for path in tmp_path.rglob("*")
        if path.is_file()
    )

    monkeypatch.setattr(config_delivery, "_stage_bytes", original)
    retried = deliver_config_view(_codex_view(), tmp_path)
    assert retried.success
    assert (tmp_path / "AGENTS.md").read_bytes() == b"agents-v1\n"
    assert (tmp_path / "CLAUDE.md").read_bytes() == b"claude-v1\n"
    assert "CLAUDE.md" in (tmp_path / "config.toml").read_text()
    assert deliver_config_view(_codex_view(), tmp_path) == DeliveryResult(success=True)


@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_stage_root_replacement_cannot_escape_or_delete_unmanaged_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
    timing: str,
) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    outside_marker = outside / "keep.txt"
    outside_marker.write_text("unchanged")
    original = config_delivery._stage_bytes
    attacked = False
    parked: Path | None = None
    replacement_path: Path | None = None
    replacement_root: Path | None = None

    def attack(stage: config_delivery._Stage) -> None:
        nonlocal attacked, parked, replacement_path, replacement_root
        stage_path = tmp_path / stage.name
        parked = tmp_path / f"{stage.name}-parked"
        stage_path.rename(parked)
        replacement_path = stage_path
        if replacement == "symlink":
            stage_path.symlink_to(outside, target_is_directory=True)
        else:
            stage_path.mkdir()
            replacement_root = stage_path
            (stage_path / "unmanaged.txt").write_text("retain")
        attacked = True

    def replacing_stage(
        root_fd: int,
        stage: config_delivery._Stage,
        category: str,
        path: PurePosixPath,
        content: bytes,
        executable: bool,
    ) -> config_delivery._StagedFile:
        if timing == "before" and not attacked:
            attack(stage)
        staged = original(root_fd, stage, category, path, content, executable)
        if timing == "after" and not attacked:
            attack(stage)
        return staged

    monkeypatch.setattr(config_delivery, "_stage_bytes", replacing_stage)

    result = deliver_config_view(_codex_view(), tmp_path)

    assert attacked is True
    assert result.success is False
    assert result.retryable is True
    assert outside_marker.read_text() == "unchanged"
    assert list(outside.iterdir()) == [outside_marker]
    assert parked is not None and parked.is_dir() and list(parked.iterdir()) == []
    assert replacement_path is not None
    if replacement == "symlink":
        assert replacement_path.is_symlink()
        assert replacement_path.resolve() == outside
    else:
        assert replacement_root is not None
        assert replacement_root.is_dir()
        assert (replacement_root / "unmanaged.txt").read_text() == "retain"
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / DELIVERY_MANIFEST_NAME).exists()


def test_result_and_manifest_never_contain_rendered_bodies(tmp_path: Path) -> None:
    secret = b"BODY-SENTINEL-DO-NOT-PERSIST"
    view = _codex_view(marker=secret)

    result = deliver_config_view(view, tmp_path)
    manifest = (tmp_path / DELIVERY_MANIFEST_NAME).read_bytes()

    assert result.success
    assert secret not in manifest
    assert secret.decode() not in repr(result)
    assert str(tmp_path).encode() not in manifest


def test_directory_lock_serializes_delivery_without_lock_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_directory_lock, args=(str(tmp_path), ready, release))
    holder.start()
    assert ready.wait(10)

    attempted = threading.Event()
    completed = threading.Event()
    result: list[DeliveryResult] = []
    original_lock = config_delivery.config_directory_lock

    @contextmanager
    def observed_lock(config_dir: Path, *, exclusive: bool) -> Iterator[int]:
        attempted.set()
        with original_lock(config_dir, exclusive=exclusive) as descriptor:
            yield descriptor

    monkeypatch.setattr(config_delivery, "config_directory_lock", observed_lock)

    def run_delivery() -> None:
        result.append(deliver_config_view(_codex_view(), tmp_path))
        completed.set()

    worker = threading.Thread(target=run_delivery)
    worker.start()
    assert attempted.wait(10)
    assert not completed.is_set()

    release.set()
    worker.join(10)
    holder.join(10)
    assert not worker.is_alive()
    assert holder.exitcode == 0
    assert result[0].success
    assert not any(path.name.endswith(".lock") for path in tmp_path.iterdir())
