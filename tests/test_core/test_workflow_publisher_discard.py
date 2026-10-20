from __future__ import annotations

# pyright: reportPrivateUsage=false
import errno
import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

from djinn_in_a_box.core import workflow_publisher
from djinn_in_a_box.core.workflow_publisher import (
    CANONICAL_MANIFEST_NAME,
    RUNTIME_MANIFEST_NAME,
    BackedUpEntry,
    BackedUpState,
    CarrierFragment,
    DriftClass,
    DriftEntry,
    PublishedFile,
    PublishError,
    WorkflowView,
    backup_target_drift,
    publish_workflow_view,
)

_p = PurePosixPath
Accept = Mapping[tuple[PurePosixPath, tuple[str, ...] | None], BackedUpState]


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
        "claude", (_file("AGENTS.md", marker), *files), fragments, target_tool=target_tool
    )


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    canonical.mkdir()
    target.mkdir()
    return canonical, target


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not path.stat().st_mode & stat.S_IWUSR:
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    path.write_bytes(content)


def _state(root: Path) -> list[tuple[str, int, bytes, int]]:
    entries: list[tuple[str, int, bytes, int]] = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = path.read_bytes() if stat.S_ISREG(info.st_mode) else b""
        entries.append(
            (
                path.relative_to(root).as_posix(),
                stat.S_IMODE(info.st_mode),
                content,
                info.st_mtime_ns,
            )
        )
    return entries


def _publish(
    canonical: Path, target: Path, view: WorkflowView, accept: Accept | None = None
) -> workflow_publisher.PublishResult:
    return publish_workflow_view(
        view, canonical, target, target / RUNTIME_MANIFEST_NAME, accept_drift=accept
    )


@contextmanager
def _backup_dir(tmp_path: Path, name: str = "backup") -> Iterator[tuple[Path, int]]:
    path = tmp_path / name
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield path, descriptor
    finally:
        os.close(descriptor)


def _backup(
    canonical: Path, target: Path, view: WorkflowView, descriptor: int
) -> tuple[BackedUpEntry, ...]:
    return backup_target_drift(view, canonical, target, target / RUNTIME_MANIFEST_NAME, descriptor)


def _accept(entries: tuple[BackedUpEntry, ...]) -> Accept:
    return {(item.entry.path, item.entry.key_path): item.state for item in entries}


def _digest(content: bytes) -> str:
    return workflow_publisher._digest(content)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- backup authorization ---


def test_backup_then_publish_with_the_authorization_overwrites_and_reports(
    tmp_path: Path,
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success
    _write(target / "AGENTS.md", b"operator edit\n")
    before = _state(target)
    with _backup_dir(tmp_path) as (backup, descriptor):
        entries = _backup(canonical, target, _view(b"two\n"), descriptor)

        assert entries == (
            BackedUpEntry(
                DriftEntry(DriftClass.TARGET_DRIFT, _p("AGENTS.md")),
                BackedUpState(_digest(b"operator edit\n"), False),
                _p("AGENTS.md"),
            ),
        )
        assert _state(target) == before
        assert (backup / "AGENTS.md").read_bytes() == b"operator edit\n"
        assert stat.S_IMODE((backup / "AGENTS.md").stat().st_mode) == 0o600

    result = _publish(canonical, target, _view(b"two\n"), _accept(entries))

    assert result.success
    assert result.discarded == (entries[0].entry,)
    assert (target / "AGENTS.md").read_bytes() == b"two\n"
    assert stat.S_IMODE((target / "AGENTS.md").stat().st_mode) == 0o444


def test_publish_without_or_with_a_foreign_authorization_still_refuses(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success
    _write(target / "AGENTS.md", b"operator edit\n")
    state = BackedUpState(_digest(b"operator edit\n"))
    before = _state(target)

    omitted = _publish(canonical, target, _view(b"two\n"), {})
    none = _publish(canonical, target, _view(b"two\n"), None)
    other_path = _publish(canonical, target, _view(b"two\n"), {(_p("other.md"), None): state})
    as_key = _publish(canonical, target, _view(b"two\n"), {(_p("AGENTS.md"), ("k",)): state})

    for result in (omitted, none, other_path, as_key):
        assert result.drift_class is DriftClass.TARGET_DRIFT
        assert result.drift_items == (DriftEntry(DriftClass.TARGET_DRIFT, _p("AGENTS.md")),)
        assert result.discarded == ()
    assert _state(target) == before


@pytest.mark.parametrize(
    "backed_up",
    (
        BackedUpState(_digest(b"something else\n")),
        BackedUpState(_digest(b"operator edit\n"), True),
        BackedUpState(None),
    ),
    ids=("other-bytes", "other-exec-bit", "missing"),
)
def test_a_mismatched_authorization_state_refuses(tmp_path: Path, backed_up: BackedUpState) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success
    _write(target / "AGENTS.md", b"operator edit\n")

    result = _publish(canonical, target, _view(b"two\n"), {(_p("AGENTS.md"), None): backed_up})

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert (target / "AGENTS.md").read_bytes() == b"operator edit\n"


def test_a_file_changed_after_its_backup_is_refused_and_the_newer_bytes_survive(
    tmp_path: Path,
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success
    _write(target / "AGENTS.md", b"first edit\n")
    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        entries = _backup(canonical, target, _view(b"two\n"), descriptor)
    _write(target / "AGENTS.md", b"newer edit\n")

    result = _publish(canonical, target, _view(b"two\n"), _accept(entries))

    assert result.drift_class is DriftClass.TARGET_DRIFT
    assert result.drift_items == (DriftEntry(DriftClass.TARGET_DRIFT, _p("AGENTS.md")),)
    assert (target / "AGENTS.md").read_bytes() == b"newer edit\n"


def test_a_carrier_key_changed_after_its_backup_is_refused_with_personal_bytes_kept(
    tmp_path: Path,
) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(fragments=(_fragment("settings.json", ("hooks", "Stop"), ["wanted"]),))
    assert _publish(canonical, target, first).success
    carrier = json.loads((target / "settings.json").read_text())
    carrier["hooks"]["Stop"] = ["edited"]
    carrier["personal"] = {"keep": 1}
    (target / "settings.json").write_text(json.dumps(carrier))
    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        entries = _backup(canonical, target, first, descriptor)
    carrier["hooks"]["Stop"] = ["edited again"]
    carrier["personal"] = {"keep": 2}
    newer = json.dumps(carrier).encode()
    (target / "settings.json").write_bytes(newer)

    result = _publish(canonical, target, first, _accept(entries))

    assert result.drift_items == (
        DriftEntry(DriftClass.TARGET_DRIFT, _p("settings.json"), ("hooks", "Stop")),
    )
    assert (target / "settings.json").read_bytes() == newer


def test_a_path_that_changes_class_after_its_backup_is_refused(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success
    _write(target / "AGENTS.md", b"operator edit\n")
    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        entries = _backup(canonical, target, _view(b"two\n"), descriptor)
    (target / "AGENTS.md").unlink()
    (target / "AGENTS.md").mkdir()

    collision = _publish(canonical, target, _view(b"two\n"), _accept(entries))

    assert collision.drift_class is DriftClass.COLLISION
    (target / "AGENTS.md").rmdir()
    vanished = _publish(canonical, target, _view(b"two\n"), _accept(entries))
    assert vanished.drift_class is DriftClass.TARGET_DRIFT
    assert not (target / "AGENTS.md").exists()


def test_a_backed_up_entry_that_is_clean_again_is_not_a_problem(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success
    _write(target / "AGENTS.md", b"operator edit\n")
    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        entries = _backup(canonical, target, _view(b"two\n"), descriptor)
    _write(target / "AGENTS.md", b"one\n")

    result = _publish(canonical, target, _view(b"two\n"), _accept(entries))

    assert result.success and result.discarded == ()


def test_the_authorization_does_not_cover_a_second_drifted_item(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(b"one\n", files=(_file("agents/b.md", b"b\n"),))
    assert _publish(canonical, target, first).success
    _write(target / "AGENTS.md", b"edit a\n")
    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        entries = _backup(canonical, target, first, descriptor)
    _write(target / "agents/b.md", b"edit b\n")

    result = _publish(canonical, target, first, _accept(entries))

    assert result.drift_items == (DriftEntry(DriftClass.TARGET_DRIFT, _p("agents/b.md")),)
    assert (target / "AGENTS.md").read_bytes() == b"edit a\n"


# --- discard semantics ---


def test_a_drifted_retired_file_is_unlinked_only_with_the_authorization(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(b"one\n", files=(_file("agents/old.md", b"old\n"),))
    assert _publish(canonical, target, first).success
    _write(target / "agents/old.md", b"edited retired\n")
    second = _view(b"one\n")

    refused = _publish(canonical, target, second)
    with _backup_dir(tmp_path) as (backup, descriptor):
        entries = _backup(canonical, target, second, descriptor)
        assert (backup / "agents/old.md").read_bytes() == b"edited retired\n"
        assert stat.S_IMODE((backup / "agents").stat().st_mode) == 0o700
    result = _publish(canonical, target, second, _accept(entries))

    assert refused.drift_class is DriftClass.TARGET_DRIFT
    assert result.success
    assert result.removed_paths == (_p("agents/old.md"),)
    assert result.discarded == (DriftEntry(DriftClass.TARGET_DRIFT, _p("agents/old.md")),)
    assert not (target / "agents/old.md").exists()


def test_a_missing_file_is_recorded_without_a_copy_and_recreated(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view(b"one\n")).success
    (target / "AGENTS.md").unlink()
    with _backup_dir(tmp_path) as (backup, descriptor):
        entries = _backup(canonical, target, _view(b"one\n"), descriptor)

        assert entries == (
            BackedUpEntry(
                DriftEntry(DriftClass.TARGET_DRIFT, _p("AGENTS.md")), BackedUpState(None), None
            ),
        )
        assert list(backup.iterdir()) == []

    result = _publish(canonical, target, _view(b"one\n"), _accept(entries))

    assert result.success
    assert (target / "AGENTS.md").read_bytes() == b"one\n"


def test_carrier_key_drift_restores_the_key_and_keeps_personal_keys(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(
        fragments=(
            _fragment("settings.json", ("hooks", "Stop"), ["wanted"]),
            _fragment("settings.json", ("hooks", "PreToolUse"), ["wanted2"]),
        )
    )
    assert _publish(canonical, target, first).success
    carrier = json.loads((target / "settings.json").read_text())
    carrier["hooks"]["Stop"] = ["edited"]
    del carrier["hooks"]["PreToolUse"]
    carrier["personal"] = {"keep": True}
    original = json.dumps(carrier).encode()
    (target / "settings.json").write_bytes(original)
    with _backup_dir(tmp_path) as (backup, descriptor):
        entries = _backup(canonical, target, first, descriptor)

        assert [item.entry for item in entries] == [
            DriftEntry(DriftClass.TARGET_DRIFT, _p("settings.json"), ("hooks", "PreToolUse")),
            DriftEntry(DriftClass.TARGET_DRIFT, _p("settings.json"), ("hooks", "Stop")),
        ]
        assert entries[0].state == BackedUpState(None)
        assert entries[1].state == BackedUpState(workflow_publisher._value_digest(["edited"]))
        # The whole carrier file is copied once for both keys.
        assert [path.name for path in backup.iterdir()] == ["settings.json"]
        assert (backup / "settings.json").read_bytes() == original
        assert {item.backup_path for item in entries} == {_p("settings.json")}

    result = _publish(canonical, target, first, _accept(entries))

    assert result.success
    assert len(result.discarded) == 2
    merged = json.loads((target / "settings.json").read_text())
    assert merged["hooks"] == {"Stop": ["wanted"], "PreToolUse": ["wanted2"]}
    assert merged["personal"] == {"keep": True}


def test_a_retired_carrier_key_with_drift_is_removed_on_discard(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(
        fragments=(
            _fragment("settings.json", ("hooks", "Stop"), ["wanted"]),
            _fragment("settings.json", ("hooks", "PreToolUse"), ["retired"]),
        )
    )
    second = _view(fragments=(_fragment("settings.json", ("hooks", "Stop"), ["wanted"]),))
    assert _publish(canonical, target, first).success
    carrier = json.loads((target / "settings.json").read_text())
    carrier["hooks"]["PreToolUse"] = ["edited retired"]
    carrier["personal"] = 1
    (target / "settings.json").write_text(json.dumps(carrier))
    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        entries = _backup(canonical, target, second, descriptor)

    result = _publish(canonical, target, second, _accept(entries))

    assert result.success
    merged = json.loads((target / "settings.json").read_text())
    assert merged == {"hooks": {"Stop": ["wanted"]}, "personal": 1}


def test_a_multi_line_toml_key_cannot_be_discarded_and_is_named(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    key = ("project_doc_fallback_filenames",)
    view = _view(target_tool="codex", fragments=(_fragment("config.toml", key, ["CLAUDE.md"]),))
    assert _publish(canonical, target, view).success
    _write(
        target / "config.toml",
        b'project_doc_fallback_filenames = [\n  "EDITED.md",\n  "X.md",\n]\n',
    )
    before = _state(target)
    item = DriftEntry(DriftClass.INVALID_OR_SEMANTIC, _p("config.toml"), key)

    publish = _publish(canonical, target, view)
    with _backup_dir(tmp_path) as (backup, descriptor), pytest.raises(PublishError) as error:
        _backup(canonical, target, view, descriptor)

    assert publish.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert publish.drift_items == (item,)
    assert error.value.drift_class is DriftClass.INVALID_OR_SEMANTIC
    assert error.value.items == (item,)
    assert list(backup.iterdir()) == []
    assert _state(target) == before


def test_a_single_line_toml_key_is_discarded(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    key = ("project_doc_fallback_filenames",)
    view = _view(target_tool="codex", fragments=(_fragment("config.toml", key, ["CLAUDE.md"]),))
    assert _publish(canonical, target, view).success
    _write(
        target / "config.toml", b'personal = 1\nproject_doc_fallback_filenames = ["EDITED.md"]\n'
    )
    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        entries = _backup(canonical, target, view, descriptor)

    assert _publish(canonical, target, view, _accept(entries)).success
    assert (
        b'project_doc_fallback_filenames = ["CLAUDE.md"]' in (target / "config.toml").read_bytes()
    )
    assert b"personal = 1" in (target / "config.toml").read_bytes()


def test_backup_refuses_when_anything_but_target_drift_is_present(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(b"one\n", files=(_file("agents/a.md", b"a\n"),))
    assert _publish(canonical, target, first).success
    _write(target / "AGENTS.md", b"operator edit\n")
    _write(target / "agents/new.md", b"unmanaged\n")
    second = _view(b"two\n", files=(_file("agents/a.md", b"a\n"), _file("agents/new.md", b"n\n")))
    before = _state(target)

    with _backup_dir(tmp_path) as (backup, descriptor), pytest.raises(PublishError) as error:
        _backup(canonical, target, second, descriptor)

    assert error.value.drift_class is DriftClass.COLLISION
    assert error.value.items == (DriftEntry(DriftClass.COLLISION, _p("agents/new.md")),)
    assert list(backup.iterdir()) == []
    assert _state(target) == before


def test_backup_of_a_clean_or_missing_target_is_empty(tmp_path: Path) -> None:
    canonical, target = _roots(tmp_path)
    with _backup_dir(tmp_path) as (backup, descriptor):
        assert _backup(canonical, target, _view(), descriptor) == ()
        assert _publish(canonical, target, _view()).success
        assert _backup(canonical, target, _view(), descriptor) == ()
        gone = tmp_path / "a" / "b"
        assert (
            backup_target_drift(_view(), canonical, gone, gone / RUNTIME_MANIFEST_NAME, descriptor)
            == ()
        )
        assert list(backup.iterdir()) == []
    assert not (tmp_path / "a").exists()


def test_backup_takes_shared_locks_for_a_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target = _roots(tmp_path)
    assert _publish(canonical, target, _view()).success
    real_flock = workflow_publisher.fcntl.flock
    operations: list[tuple[str, int]] = []

    def spy(descriptor: int, operation: int) -> None:
        operations.append((os.readlink(f"/proc/self/fd/{descriptor}"), operation))
        real_flock(descriptor, operation)

    monkeypatch.setattr(workflow_publisher.fcntl, "flock", spy)
    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        _backup(canonical, target, _view(), descriptor)

    fcntl = workflow_publisher.fcntl
    locks = [item for item in operations if item[1] != fcntl.LOCK_UN]
    assert sorted(locks) == sorted([(str(canonical), fcntl.LOCK_SH), (str(target), fcntl.LOCK_SH)])


# --- canonical tree ---


def _canonical_view(source: str, *files: tuple[str, bytes]) -> WorkflowView:
    return WorkflowView(source, tuple(_file(path, content) for path, content in files))


def _release(canonical: Path, source: str) -> bytes:
    manifest = json.loads((canonical / CANONICAL_MANIFEST_NAME).read_text())
    manifest["items"] = [
        item for item in manifest["items"] if PurePosixPath(item["path"]).parts[0] != source
    ]
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def test_canonical_discard_after_a_source_switch_uses_the_released_manifest(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    manifest = canonical / CANONICAL_MANIFEST_NAME
    first = _canonical_view(
        "claude", ("codex/AGENTS.md", b"codex projection\n"), ("opencode/AGENTS.md", b"oc\n")
    )
    assert publish_workflow_view(first, canonical, canonical, manifest).success
    # The owner switches the source to codex: config/codex now holds their own text,
    # and an agent edited the opencode projection.
    _write(canonical / "codex/AGENTS.md", b"the new source's own text\n")
    _write(canonical / "opencode/AGENTS.md", b"agent edit\n")
    second = _canonical_view(
        "codex", ("claude/AGENTS.md", b"cl\n"), ("opencode/AGENTS.md", b"oc\n")
    )
    released = _release(canonical, "codex")

    with _backup_dir(tmp_path) as (backup, descriptor):
        without = backup_target_drift(second, canonical, canonical, manifest, descriptor)
        # Without the release, the new source's own file looks like retired drift.
        assert {item.entry.path for item in without} == {
            _p("codex/AGENTS.md"),
            _p("opencode/AGENTS.md"),
        }
        leftovers = [path for path in backup.rglob("*") if path.is_file()]
        for path in leftovers:
            path.unlink()
        entries = backup_target_drift(
            second, canonical, canonical, manifest, descriptor, preflight_manifest=released
        )

        assert [item.entry.path for item in entries] == [_p("opencode/AGENTS.md")]
        assert (backup / "opencode/AGENTS.md").read_bytes() == b"agent edit\n"

    refused = publish_workflow_view(
        second, canonical, canonical, manifest, preflight_manifest=released
    )
    result = publish_workflow_view(
        second,
        canonical,
        canonical,
        manifest,
        preflight_manifest=released,
        accept_drift=_accept(entries),
    )

    assert refused.drift_items == (DriftEntry(DriftClass.TARGET_DRIFT, _p("opencode/AGENTS.md")),)
    assert result.success
    assert (canonical / "codex/AGENTS.md").read_bytes() == b"the new source's own text\n"
    assert (canonical / "opencode/AGENTS.md").read_bytes() == b"oc\n"
    assert stat.S_IMODE((canonical / "opencode/AGENTS.md").stat().st_mode) == 0o644


def test_canonical_backup_needs_the_exclusive_lease(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    manifest = canonical / CANONICAL_MANIFEST_NAME
    view = _canonical_view("claude", ("codex/AGENTS.md", b"x\n"))
    assert publish_workflow_view(view, canonical, canonical, manifest).success
    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        with workflow_publisher.canonical_lock(canonical, exclusive=False) as shared:
            with pytest.raises(PublishError) as error:
                backup_target_drift(
                    view, canonical, canonical, manifest, descriptor, canonical_lease=shared
                )
            assert error.value.drift_class is DriftClass.INVALID_OR_SEMANTIC
        with workflow_publisher.canonical_lock(canonical, exclusive=True) as exclusive:
            assert (
                backup_target_drift(
                    view, canonical, canonical, manifest, descriptor, canonical_lease=exclusive
                )
                == ()
            )


# --- the backup writer ---


def _drifted_nested(tmp_path: Path) -> tuple[Path, Path, WorkflowView]:
    canonical, target = _roots(tmp_path)
    view = _view(files=(_file("skills/demo/refs/guide.md", b"guide\n"),))
    assert _publish(canonical, target, view).success
    _write(target / "skills/demo/refs/guide.md", b"edited guide\n")
    return canonical, target, view


def test_backup_writer_modes_under_a_permissive_umask_and_exclusive_create(
    tmp_path: Path,
) -> None:
    canonical, target, view = _drifted_nested(tmp_path)
    previous = os.umask(0o022)
    try:
        with _backup_dir(tmp_path) as (backup, descriptor):
            entries = _backup(canonical, target, view, descriptor)
            assert entries[0].backup_path == _p("skills/demo/refs/guide.md")
            assert stat.S_IMODE((backup / "skills/demo/refs/guide.md").stat().st_mode) == 0o600
            for directory in ("skills", "skills/demo", "skills/demo/refs"):
                assert stat.S_IMODE((backup / directory).stat().st_mode) == 0o700
            # A second attempt into the same backup directory must not overwrite.
            with pytest.raises(FileExistsError):
                _backup(canonical, target, view, descriptor)
            assert (backup / "skills/demo/refs/guide.md").read_bytes() == b"edited guide\n"
    finally:
        os.umask(previous)


def test_backup_writer_fsyncs_every_created_entry_bottom_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target, view = _drifted_nested(tmp_path)
    real_fsync = os.fsync
    traced: list[str] = []

    def spy(descriptor: int) -> None:
        traced.append(os.readlink(f"/proc/self/fd/{descriptor}"))
        real_fsync(descriptor)

    with _backup_dir(tmp_path) as (backup, descriptor):
        monkeypatch.setattr(os, "fsync", spy)
        _backup(canonical, target, view, descriptor)
        monkeypatch.setattr(os, "fsync", real_fsync)

        root = str(backup)
        assert traced == [
            root,  # parent of the created "skills"
            f"{root}/skills",  # parent of the created "demo"
            f"{root}/skills/demo",  # parent of the created "refs"
            f"{root}/skills/demo/refs/guide.md",  # the file itself
            f"{root}/skills/demo/refs",  # bottom-up chain ...
            f"{root}/skills/demo",
            f"{root}/skills",
            root,  # ... up to and including the admitted descriptor
        ]


@pytest.mark.parametrize("failing_call", range(8))
def test_an_fsync_fault_aborts_before_any_target_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_call: int
) -> None:
    canonical, target, view = _drifted_nested(tmp_path)
    before = _state(target)
    real_fsync = os.fsync
    calls = 0

    def faulty(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failing_call + 1:
            raise OSError(errno.EIO, "Input/output error")
        real_fsync(descriptor)

    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        monkeypatch.setattr(os, "fsync", faulty)
        with pytest.raises(OSError, match="Input/output error"):
            _backup(canonical, target, view, descriptor)
        monkeypatch.setattr(os, "fsync", real_fsync)

    assert _state(target) == before


def test_backup_writer_refuses_an_unsafe_backup_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical, target, view = _drifted_nested(tmp_path)
    before = _state(target)
    with _backup_dir(tmp_path) as (backup, descriptor):
        backup.chmod(0o770)
        with pytest.raises(PermissionError):
            _backup(canonical, target, view, descriptor)
        backup.chmod(0o707)
        with pytest.raises(PermissionError):
            _backup(canonical, target, view, descriptor)
        backup.chmod(0o700)
        monkeypatch.setattr(os, "geteuid", lambda: os.stat(backup).st_uid + 1)
        with pytest.raises(PermissionError):
            _backup(canonical, target, view, descriptor)
        monkeypatch.undo()
        assert list(backup.iterdir()) == []
    file_descriptor = os.open(tmp_path / "plain", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        with pytest.raises(PermissionError):
            _backup(canonical, target, view, file_descriptor)
    finally:
        os.close(file_descriptor)
    assert _state(target) == before


def test_backup_writer_refuses_a_group_writable_existing_subdirectory(tmp_path: Path) -> None:
    canonical, target, view = _drifted_nested(tmp_path)
    with _backup_dir(tmp_path) as (backup, descriptor):
        (backup / "skills").mkdir(mode=0o770)
        (backup / "skills").chmod(0o770)
        with pytest.raises(PermissionError):
            _backup(canonical, target, view, descriptor)
        assert list((backup / "skills").iterdir()) == []


def test_backup_writer_does_not_follow_a_planted_symlink(tmp_path: Path) -> None:
    canonical, target, view = _drifted_nested(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)  # private, so only the no-follow open can refuse the link
    with _backup_dir(tmp_path) as (backup, descriptor):
        (backup / "skills").symlink_to(outside)
        with pytest.raises(OSError):  # noqa: PT011 - ELOOP or ENOTDIR, both are refusals
            _backup(canonical, target, view, descriptor)
    assert list(outside.iterdir()) == []


def test_an_unmanaged_collision_is_never_accepted_even_with_a_matching_state(
    tmp_path: Path,
) -> None:
    canonical, target = _roots(tmp_path)
    _write(target / "AGENTS.md", b"operator file\n")
    state = BackedUpState(_digest(b"operator file\n"))

    result = _publish(canonical, target, _view(b"one\n"), {(_p("AGENTS.md"), None): state})

    assert result.drift_class is DriftClass.COLLISION
    assert result.discarded == ()
    assert (target / "AGENTS.md").read_bytes() == b"operator file\n"


def test_a_retired_key_below_a_replaced_parent_is_accepted_without_touching_it(
    tmp_path: Path,
) -> None:
    canonical, target = _roots(tmp_path)
    first = _view(fragments=(_fragment("settings.json", ("hooks", "Stop"), ["wanted"]),))
    assert _publish(canonical, target, first).success
    _write(target / "settings.json", b'{"hooks": "operator string"}')
    second = _view()
    with _backup_dir(tmp_path) as (_backup_path, descriptor):
        entries = _backup(canonical, target, second, descriptor)

    refused = _publish(canonical, target, second)
    result = _publish(canonical, target, second, _accept(entries))

    assert entries[0].state == BackedUpState("blocked")
    assert refused.drift_class is DriftClass.TARGET_DRIFT
    assert result.success
    assert (target / "settings.json").read_bytes() == b'{"hooks": "operator string"}'
