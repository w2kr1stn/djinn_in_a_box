from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import djinn_in_a_box.core.seeding as seeding_mod
from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core import contained_paths
from djinn_in_a_box.core.seeding import (
    CLAUDE_BASELINE_SEEDS,
    SEED_MANIFEST,
    SHARED_SEEDS,
    WORKFLOW_ROOT_NAMES,
    SeedEntry,
    SeedingError,
    pristine_workflow_seed_digest,
    seed_config,
)


def _write_seed_fixture(project_root: Path) -> None:
    seed_root = project_root / "templates" / "seed"
    for entry in SEED_MANIFEST:
        source = seed_root / entry.source
        source.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "file":
            source.write_text(f"seed for {entry.source}\n", encoding="utf-8")
        else:
            source.mkdir(parents=True, exist_ok=True)
            (source / ".gitkeep").write_text("", encoding="utf-8")


def _target(project_root: Path, entry: SeedEntry) -> Path:
    return project_root / entry.target


def _plant_after_gate(
    monkeypatch: pytest.MonkeyPatch, plant: Callable[[Path], None]
) -> None:
    real_gate = seeding_mod.workflow_root_is_uninitialized

    def gate(root: Path) -> bool:
        admitted = real_gate(root)
        assert admitted
        root.mkdir(parents=True, exist_ok=True)
        plant(root)
        return admitted

    monkeypatch.setattr(seeding_mod, "workflow_root_is_uninitialized", gate)


def test_baseline_ignores_predictable_temp_link_planted_after_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside bytes")
    outside.chmod(0o640)
    before = outside.stat().st_mode
    _plant_after_gate(monkeypatch, lambda root: (root / ".AGENTS.md.seed-tmp").symlink_to(outside))
    created = seed_config(tmp_path)
    target = tmp_path / "config/claude/AGENTS.md"
    assert target in created
    assert target.read_bytes() == (tmp_path / "templates/seed/config/claude/AGENTS.md").read_bytes()
    assert not target.is_symlink()
    assert outside.read_bytes() == b"outside bytes"
    assert outside.stat().st_mode == before


@pytest.mark.parametrize("entry", CLAUDE_BASELINE_SEEDS, ids=lambda entry: entry.target.name)
@pytest.mark.parametrize("kind", ["link", "dangling", "wrong-type"])
def test_baseline_replaces_wrong_types_planted_after_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: SeedEntry, kind: str
) -> None:
    _write_seed_fixture(tmp_path)
    source = tmp_path / "templates/seed" / entry.source
    if entry.kind == "directory":
        (source / "content").write_bytes(b"shipped bytes")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "keep"
    sentinel.write_bytes(b"outside bytes")
    sentinel.chmod(0o640)
    before = (outside.stat().st_mode, sentinel.stat().st_mode)

    def plant(root: Path) -> None:
        target = root / entry.target.name
        if kind == "link":
            target.symlink_to(sentinel if entry.kind == "file" else outside)
        elif kind == "dangling":
            target.symlink_to(outside / "missing")
        elif entry.kind == "file":
            target.mkdir()
            (target / "stale").write_bytes(b"wrong type")
        else:
            target.write_bytes(b"wrong type")

    _plant_after_gate(monkeypatch, plant)
    created = seed_config(tmp_path)
    target = _target(tmp_path, entry)
    assert target in created
    assert not target.is_symlink()
    if entry.kind == "file":
        assert target.read_bytes() == source.read_bytes()
    else:
        assert (target / "content").read_bytes() == b"shipped bytes"
    assert sentinel.read_bytes() == b"outside bytes"
    assert before == (outside.stat().st_mode, sentinel.stat().st_mode)
    assert sorted(p.name for p in outside.iterdir()) == ["keep"]


def test_baseline_temp_directory_swap_keeps_writes_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_fixture(tmp_path)
    source = tmp_path / "templates/seed/config/claude/skills/nested"
    source.mkdir()
    (source / "content").write_bytes(b"shipped bytes")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (outside / "keep").write_bytes(b"outside bytes")
    before = (outside.stat().st_mode, (outside / "keep").stat().st_mode)
    real_mkdir = os.mkdir
    swapped = False
    saved = tmp_path / "config/claude/saved"

    def swap(name: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal swapped
        real_mkdir(name, *args, **kwargs)
        if isinstance(name, str) and name.startswith(".djinn-seed-") and not swapped:
            swapped = True
            parent = kwargs["dir_fd"]
            os.rename(name, "saved", src_dir_fd=parent, dst_dir_fd=parent)
            os.symlink(outside, name, dir_fd=parent)

    _plant_after_gate(
        monkeypatch, lambda root: monkeypatch.setattr(contained_paths.os, "mkdir", swap)
    )
    with pytest.raises(SeedingError, match="then rerun"):
        seed_config(tmp_path)
    assert swapped
    assert saved.is_dir()
    assert not list(saved.iterdir())
    assert not (tmp_path / "config/claude/skills").exists()
    assert not list((tmp_path / "config/claude").glob(".djinn-seed-*"))
    assert (outside / "keep").read_bytes() == b"outside bytes"
    assert sorted(p.name for p in outside.iterdir()) == ["keep"]
    assert before == (outside.stat().st_mode, (outside / "keep").stat().st_mode)


def test_baseline_pre_gate_entry_keeps_root_untouched(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    root = tmp_path / "config/claude"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside bytes")
    link = root / ".AGENTS.md.seed-tmp"
    link.symlink_to(outside)
    before = root.stat().st_mode
    created = seed_config(tmp_path)
    assert not any(_target(tmp_path, entry) in created for entry in CLAUDE_BASELINE_SEEDS)
    assert list(root.iterdir()) == [link]
    assert link.is_symlink()
    assert root.stat().st_mode == before
    assert outside.read_bytes() == b"outside bytes"


def test_baseline_root_swap_after_open_keeps_writes_at_opened_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_fixture(tmp_path)
    source = tmp_path / "templates/seed/config/claude/skills/content"
    source.write_bytes(b"shipped bytes")
    root = tmp_path / "config/claude"
    saved = tmp_path / "config/saved"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (outside / "keep").write_bytes(b"outside bytes")
    (outside / "AGENTS.md").write_bytes(b"outside baseline sentinel")
    before = (outside.stat().st_mode, (outside / "keep").stat().st_mode,
              (outside / "AGENTS.md").stat().st_mode)
    real_open = seeding_mod.open_root
    opened = False

    def swap(path: Path) -> int:
        nonlocal opened
        assert path == root
        descriptor = real_open(path)
        opened = True
        root.rename(saved)
        root.symlink_to(outside)
        return descriptor

    _plant_after_gate(monkeypatch, lambda path: monkeypatch.setattr(seeding_mod, "open_root", swap))
    created = seed_config(tmp_path)
    assert opened
    assert root / "AGENTS.md" in created
    assert (saved / "AGENTS.md").read_bytes() == (
        tmp_path / "templates/seed/config/claude/AGENTS.md"
    ).read_bytes()
    assert (saved / "skills/content").read_bytes() == b"shipped bytes"
    assert (outside / "keep").read_bytes() == b"outside bytes"
    assert (outside / "AGENTS.md").read_bytes() == b"outside baseline sentinel"
    assert sorted(p.name for p in outside.iterdir()) == ["AGENTS.md", "keep"]
    assert before == (outside.stat().st_mode, (outside / "keep").stat().st_mode,
                      (outside / "AGENTS.md").stat().st_mode)


@pytest.mark.parametrize("mode", [0o644, 0o744, 0o654])
def test_baseline_installed_file_keeps_source_owner_executable_bit(
    tmp_path: Path, mode: int
) -> None:
    _write_seed_fixture(tmp_path)
    source = tmp_path / "templates/seed/config/claude/AGENTS.md"
    source.chmod(mode)
    created = seed_config(tmp_path)
    target = tmp_path / "config/claude/AGENTS.md"
    assert target in created
    assert stat.S_IMODE(target.stat().st_mode) == (0o755 if mode & stat.S_IXUSR else 0o644)
    assert target.read_bytes() == source.read_bytes()
    assert seed_config(tmp_path) == []


@pytest.mark.parametrize("mode", [0o644, 0o744, 0o654])
def test_baseline_nested_file_keeps_source_owner_executable_bit(
    tmp_path: Path, mode: int
) -> None:
    _write_seed_fixture(tmp_path)
    source = tmp_path / "templates/seed/config/claude/skills/nested"
    source.mkdir()
    (source / "script").write_bytes(b"shipped bytes")
    (source / "script").chmod(mode)
    created = seed_config(tmp_path)
    target = tmp_path / "config/claude/skills/nested/script"
    assert target.parent.parent in created
    assert target.read_bytes() == b"shipped bytes"
    assert stat.S_IMODE(target.stat().st_mode) == (0o755 if mode & stat.S_IXUSR else 0o644)


def test_baseline_handles_wrong_type_tree_beyond_recursion_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_fixture(tmp_path)
    directories: list[Path] = []

    def plant(root: Path) -> None:
        directories.append(root / "AGENTS.md")
        directories[0].mkdir()
        for _ in range(sys.getrecursionlimit() + 11):
            directories.append(directories[-1] / "d")
            directories[-1].mkdir()
        (directories[-1] / "file").write_bytes(b"delete")

    _plant_after_gate(monkeypatch, plant)
    fds = len(os.listdir("/proc/self/fd"))
    try:
        try:
            created = seed_config(tmp_path)
        except SeedingError as exc:
            assert isinstance(exc.__cause__, OSError)
            assert exc.__cause__.errno in {errno.EMFILE, errno.ENFILE}
        else:
            target = tmp_path / "config/claude/AGENTS.md"
            assert target in created
            source = tmp_path / "templates/seed/config/claude/AGENTS.md"
            assert target.read_bytes() == source.read_bytes()
        assert len(os.listdir("/proc/self/fd")) == fds
    finally:
        if directories:
            leaf = directories[-1] / "file"
            if leaf.exists():
                leaf.unlink()
            for directory in reversed(directories):
                if directory.is_dir():
                    directory.rmdir()


@pytest.mark.parametrize("kind", ["content", "executable"])
@pytest.mark.parametrize("executable", [False, True])
def test_baseline_file_swap_before_commit_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, executable: bool
) -> None:
    _write_seed_fixture(tmp_path)
    source = tmp_path / "templates/seed/config/claude/AGENTS.md"
    source.chmod(0o755 if executable else 0o644)
    shipped = source.read_bytes()
    root = tmp_path / "config/claude"
    target = root / "AGENTS.md"
    saved = root / "saved-file"
    real_replace = os.replace

    def swap(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if dst == "AGENTS.md":
            assert str(src).startswith(".djinn-publisher-")
            assert (root / src).read_bytes() == shipped
            os.rename(src, "saved-file", *args, **kwargs)
            (root / src).write_bytes(b"substituted bytes" if kind == "content" else shipped)
            substitute_executable = not executable if kind == "executable" else executable
            (root / src).chmod(0o755 if substitute_executable else 0o644)
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(contained_paths.os, "replace", swap)
    created: list[Path] = []
    fds = len(os.listdir("/proc/self/fd"))
    with pytest.raises(SeedingError, match="Cannot install baseline seed target") as failed:
        created.extend(seed_config(tmp_path))
    assert target not in created
    assert isinstance(failed.value.__cause__, contained_paths.PathContainmentError)
    assert failed.value.__cause__.path == target
    assert failed.value.__cause__.reason == "substituted"
    assert saved.read_bytes() == shipped
    assert bool(saved.stat().st_mode & stat.S_IXUSR) == executable
    assert target.read_bytes() == (b"substituted bytes" if kind == "content" else shipped)
    assert bool(target.stat().st_mode & stat.S_IXUSR) == (
        not executable if kind == "executable" else executable
    )
    assert not list(root.glob(".djinn-publisher-*"))
    assert len(os.listdir("/proc/self/fd")) == fds


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file read permissions")
def test_baseline_unreadable_committed_file_translates_to_seeding_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_fixture(tmp_path)
    target = tmp_path / "config/claude/AGENTS.md"
    real_replace = os.replace
    unreadable = False

    def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal unreadable
        real_replace(src, dst, *args, **kwargs)
        if dst == "AGENTS.md":
            parent = kwargs["dst_dir_fd"]
            assert os.readlink(f"/proc/self/fd/{parent}") == str(target.parent)
            os.chmod(dst, 0o000, dir_fd=parent, follow_symlinks=False)
            unreadable = True

    monkeypatch.setattr(contained_paths.os, "replace", replace)
    fds = len(os.listdir("/proc/self/fd"))
    try:
        with pytest.raises(SeedingError, match="Cannot install baseline seed target") as failed:
            seed_config(tmp_path)
        assert unreadable
        assert str(target) in str(failed.value)
        assert "then rerun" in str(failed.value)
        assert isinstance(failed.value.__cause__, PermissionError)
        assert failed.value.__cause__.errno == errno.EACCES
        assert stat.S_IMODE(target.stat().st_mode) == 0o000
        assert not list(target.parent.glob(".djinn-publisher-*"))
        assert len(os.listdir("/proc/self/fd")) == fds
    finally:
        if unreadable:
            target.chmod(0o600)


@pytest.mark.parametrize("kind", ["link", "populated-link", "file", "directory"])
def test_baseline_tree_swap_after_population_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _write_seed_fixture(tmp_path)
    source = tmp_path / "templates/seed/config/claude/skills/nested"
    source.mkdir()
    (source / "content").write_bytes(b"shipped bytes")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "keep"
    sentinel.write_bytes(b"outside bytes")
    sentinel.chmod(0o640)
    before = (outside.stat().st_mode, sentinel.stat().st_mode, sentinel.read_bytes())
    root = tmp_path / "config/claude"
    saved = root / "saved"
    real_replace = os.replace
    populated_fd = None

    def swap(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal populated_fd
        if dst == "skills":
            assert (root / src / "nested/content").read_bytes() == b"shipped bytes"
            populated_fd = next(
                int(fd) for fd in os.listdir("/proc/self/fd")
                if Path(f"/proc/self/fd/{fd}").exists()
                and Path(os.readlink(f"/proc/self/fd/{fd}")) == root / src
            )
            os.rename(src, "saved", *args, **kwargs)
            if kind in {"link", "populated-link"}:
                os.symlink(saved if kind == "populated-link" else outside, src,
                           dir_fd=kwargs["src_dir_fd"])
            elif kind == "file":
                (root / src).write_bytes(b"substitute")
            else:
                os.mkdir(src, dir_fd=kwargs["src_dir_fd"])
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(contained_paths.os, "replace", swap)
    created: list[Path] = []
    fds = len(os.listdir("/proc/self/fd"))
    with pytest.raises(SeedingError, match="Cannot install baseline seed target") as failed:
        created.extend(seed_config(tmp_path))
    assert created == []
    assert populated_fd is not None
    assert isinstance(failed.value.__cause__, contained_paths.PathContainmentError)
    assert failed.value.__cause__.path == root / "skills"
    assert failed.value.__cause__.reason == (
        "symlink" if "link" in kind else "substituted" if kind == "directory" else "not a directory"
    )
    assert (saved / "nested/content").read_bytes() == b"shipped bytes"
    assert not list(root.glob(".djinn-seed-*"))
    assert len(os.listdir("/proc/self/fd")) == fds
    assert (outside.stat().st_mode, sentinel.stat().st_mode, sentinel.read_bytes()) == before
    assert sorted(p.name for p in outside.iterdir()) == ["keep"]


@pytest.mark.parametrize("operation", ["open", "stat", "remove", "file", "tree"])
@pytest.mark.parametrize("failure", ["containment", "oserror"])
def test_baseline_failures_translate_to_seeding_error_with_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, failure: str
) -> None:
    _write_seed_fixture(tmp_path)
    root = tmp_path / "config/claude"
    refused = (root if operation == "open"
               else root / ("skills" if operation == "tree" else "AGENTS.md"))
    exc = (contained_paths.PathContainmentError(refused, "symlink") if failure == "containment"
           else PermissionError(errno.EACCES, "denied"))

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise exc

    if operation == "remove":
        _plant_after_gate(monkeypatch, lambda path: (path / "AGENTS.md").mkdir())
    if operation == "stat":
        real_stat = os.stat

        def fail_stat(name: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if name == "AGENTS.md" and kwargs.get("dir_fd") is not None:
                raise exc
            return real_stat(name, *args, **kwargs)

        monkeypatch.setattr(seeding_mod.os, "stat", fail_stat)
    else:
        name = {"open": "open_root", "remove": "remove_entry", "file": "install_file",
                "tree": "install_tree"}[operation]
        monkeypatch.setattr(seeding_mod, name, fail)
    before = len(list(Path("/proc/self/fd").iterdir()))
    with pytest.raises(SeedingError, match="then rerun") as raised:
        seed_config(tmp_path)
    assert str(refused) in str(raised.value)
    assert raised.value.__cause__ is exc
    assert len(list(Path("/proc/self/fd").iterdir())) == before
    if operation == "remove" and failure == "oserror":
        assert f"Remove it with `sudo rm -rf {refused}`, then rerun." in str(raised.value)


def test_baseline_missing_source_keeps_installation_remedy(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    (tmp_path / "templates/seed/config/claude/AGENTS.md").unlink()
    with pytest.raises(SeedingError, match="Reinstall.*then rerun"):
        seed_config(tmp_path)


def test_baseline_reads_sources_before_opening_target_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_fixture(tmp_path)
    source = tmp_path / "templates/seed/config/claude/AGENTS.md"
    tree_source = tmp_path / "templates/seed/config/claude/skills/nested.md"
    tree_source.write_text("nested seed\n", encoding="utf-8")
    root = tmp_path / "config/claude"
    events: list[tuple[str, Path]] = []
    real_read_bytes = Path.read_bytes
    real_open_root = seeding_mod.open_root

    def read_bytes(path: Path, *args: Any, **kwargs: Any) -> bytes:
        if path in {source, tree_source}:
            events.append(("source-read", path))
        return real_read_bytes(path, *args, **kwargs)

    def open_root(path: Path) -> int:
        if path == root:
            events.append(("target-open", path))
        return real_open_root(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(seeding_mod, "open_root", open_root)

    seed_config(tmp_path)

    target_open = events.index(("target-open", root))
    assert events.index(("source-read", source)) < target_open
    assert events.index(("source-read", tree_source)) < target_open


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file read permissions")
@pytest.mark.parametrize(
    ("relative_source", "source_root"),
    [
        ("config/claude/AGENTS.md", "AGENTS.md"),
        ("config/claude/skills/unreadable.md", "skills"),
    ],
    ids=["file", "tree"],
)
def test_unreadable_baseline_source_is_an_installation_error(
    tmp_path: Path, relative_source: str, source_root: str
) -> None:
    _write_seed_fixture(tmp_path)
    source = tmp_path / "templates/seed" / relative_source
    if source.name == "unreadable.md":
        source.write_text("unreadable source\n", encoding="utf-8")
    source.chmod(0o000)
    try:
        with pytest.raises(SeedingError) as failed:
            seed_config(tmp_path)
        message = str(failed.value)
        source_entry = tmp_path / "templates/seed/config/claude" / source_root
        assert f"Cannot read baseline seed source {source_entry}:" in message
        assert str(source) in message
        assert "Reinstall djinn_in_a_box" in message
        assert "chown" not in message
        assert "Cannot install baseline seed target" not in message
    finally:
        source.chmod(0o600)


def test_baseline_error_reports_filename_when_distinct_from_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_fixture(tmp_path)
    target = tmp_path / "config/claude/AGENTS.md"
    other = tmp_path / "different-failure-path"
    failure = OSError(errno.EIO, "synthetic I/O error", str(other))

    def fail(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(seeding_mod, "install_file", fail)
    with pytest.raises(SeedingError) as failed:
        seed_config(tmp_path)

    assert (
        f"Cannot install baseline seed target {target}: synthetic I/O error." in str(failed.value)
    )
    assert f"Failing path: {other}." in str(failed.value)
    assert failed.value.__cause__ is failure


@pytest.mark.parametrize("entry", CLAUDE_BASELINE_SEEDS, ids=lambda entry: entry.target.name)
def test_baseline_expected_type_planted_after_gate_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: SeedEntry
) -> None:
    _write_seed_fixture(tmp_path)

    def plant(root: Path) -> None:
        target = root / entry.target.name
        if entry.kind == "file":
            target.write_bytes(b"operator bytes")
        else:
            target.mkdir()
            (target / "keep").write_bytes(b"operator bytes")

    _plant_after_gate(monkeypatch, plant)
    target = _target(tmp_path, entry)
    assert target not in seed_config(tmp_path)
    content = target if entry.kind == "file" else target / "keep"
    assert content.read_bytes() == b"operator bytes"


def test_empty_target_creates_all_manifest_targets(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)

    created = seed_config(tmp_path)

    expected = [tmp_path / "config" / name for name in WORKFLOW_ROOT_NAMES]
    expected.extend(_target(tmp_path, entry) for entry in SEED_MANIFEST)
    assert created == expected
    assert tmp_path / "config/claude/AGENTS.md" in created
    assert not (tmp_path / "config/claude/CLAUDE.md").exists()
    for entry in SEED_MANIFEST:
        target = _target(tmp_path, entry)
        if entry.kind == "file":
            assert target.is_file()
            assert target.read_text(encoding="utf-8") == f"seed for {entry.source}\n"
        else:
            assert target.is_dir()
            assert not (target / ".gitkeep").exists()


def test_user_modified_file_remains_byte_identical(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    seed_config(tmp_path)
    target = tmp_path / "config" / "claude" / "settings.json"
    modified = b'{"local": true}\n'
    target.write_bytes(modified)

    assert seed_config(tmp_path) == []
    assert target.read_bytes() == modified


def test_run_twice_second_run_returns_empty(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)

    assert seed_config(tmp_path)
    assert seed_config(tmp_path) == []


def test_initialized_claude_root_is_preserved_without_filling_baseline(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    claude_root = tmp_path / "config" / "claude"
    claude_root.mkdir(parents=True)
    custom = claude_root / "custom.txt"
    custom.write_bytes(b"operator bytes\n")

    created = seed_config(tmp_path)

    expected = [tmp_path / "config" / "codex", tmp_path / "config" / "opencode"]
    expected.extend(_target(tmp_path, entry) for entry in SHARED_SEEDS)
    assert created == expected
    assert custom.read_bytes() == b"operator bytes\n"
    for entry in CLAUDE_BASELINE_SEEDS:
        assert not _target(tmp_path, entry).exists()


@pytest.mark.parametrize("source", ["codex", "opencode"])
def test_non_claude_first_use_creates_empty_workflow_roots(
    tmp_path: Path, source: ConfigSyncSource
) -> None:
    _write_seed_fixture(tmp_path)

    seed_config(tmp_path, source=source)

    for name in WORKFLOW_ROOT_NAMES:
        root = tmp_path / "config" / name
        assert root.is_dir()
        assert list(root.iterdir()) == []
    for entry in SHARED_SEEDS:
        assert _target(tmp_path, entry).exists()


def test_wrong_type_targets_are_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory_entry = SeedEntry(Path("config/sample-dir"), Path("config/sample-dir"), "directory")
    monkeypatch.setattr(seeding_mod, "SHARED_SEEDS", (*SHARED_SEEDS, directory_entry))
    _write_seed_fixture(tmp_path)
    (tmp_path / "templates" / "seed" / directory_entry.source).mkdir()
    seed_config(tmp_path)
    file_entry = next(entry for entry in SHARED_SEEDS if entry.kind == "file")
    file_target = _target(tmp_path, file_entry)
    directory_target = _target(tmp_path, directory_entry)

    file_target.unlink()
    file_target.mkdir()
    directory_target.rmdir()
    directory_target.write_text("wrong type\n", encoding="utf-8")

    created = seed_config(tmp_path)

    assert created == [file_target, directory_target]
    assert file_target.is_file()
    assert file_target.read_text(encoding="utf-8") == f"seed for {file_entry.source}\n"
    assert directory_target.is_dir()


@pytest.mark.parametrize("collision_kind", ["file", "symlink"])
def test_workflow_root_collision_fails_closed_before_any_mutation(
    tmp_path: Path, collision_kind: str
) -> None:
    _write_seed_fixture(tmp_path)
    config_root = tmp_path / "config"
    config_root.mkdir()
    collision = config_root / "codex"
    if collision_kind == "file":
        collision.write_bytes(b"operator sentinel\n")
    else:
        target = tmp_path / "operator-codex"
        target.mkdir()
        (target / "sentinel").write_bytes(b"operator sentinel\n")
        collision.symlink_to(target, target_is_directory=True)

    with pytest.raises(SeedingError, match="must be a real directory"):
        seed_config(tmp_path)

    assert not (config_root / "claude").exists()
    assert not (config_root / "opencode").exists()
    assert collision.is_symlink() if collision_kind == "symlink" else collision.is_file()
    if collision_kind == "file":
        assert collision.read_bytes() == b"operator sentinel\n"
    else:
        assert (collision / "sentinel").read_bytes() == b"operator sentinel\n"


def test_non_removable_wrong_type_raises_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed_fixture(tmp_path)
    target = tmp_path / "config" / "mcp-servers.json"
    target.mkdir(parents=True)

    def raise_permission_error(_path: Path) -> None:
        raise PermissionError("root-owned")

    monkeypatch.setattr(seeding_mod, "_remove_path", raise_permission_error)

    with pytest.raises(SeedingError, match="sudo rm"):
        seed_config(tmp_path)


def test_missing_templates_seed_raises_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(SeedingError, match="templates/seed"):
        seed_config(tmp_path)


def test_pristine_workflow_seed_digest_matches_shipped_bytes(tmp_path: Path) -> None:
    _write_seed_fixture(tmp_path)
    relative = Path("claude/AGENTS.md")
    template = tmp_path / "templates" / "seed" / "config" / relative

    digest = pristine_workflow_seed_digest(tmp_path, relative)

    assert digest == hashlib.sha256(template.read_bytes()).hexdigest()
    assert pristine_workflow_seed_digest(tmp_path, Path("claude/custom.md")) is None
    assert pristine_workflow_seed_digest(tmp_path, Path("../outside")) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory write permissions")
def test_baseline_permission_failure_has_target_and_ownership_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_seed_fixture(tmp_path)
    root = tmp_path / "config/claude"
    _plant_after_gate(monkeypatch, lambda path: path.chmod(0o500))
    try:
        with pytest.raises(SeedingError) as failed:
            seed_config(tmp_path)
        message = str(failed.value)
        assert isinstance(failed.value.__cause__, PermissionError)
        assert f"Cannot install baseline seed target {root / 'AGENTS.md'}:" in message
        assert 'Fix ownership with `sudo chown -R "$(id -u):$(id -g)" ' + str(root) in message
        assert "Move or remove" not in message
        assert "writable" not in message
        assert ".djinn-publisher-" not in message
    finally:
        root.chmod(0o700)


def test_baseline_refusal_has_conflict_remedy_without_writability_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_seed_fixture(tmp_path)
    root = tmp_path / "config/claude"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"outside")
    before = (outside.stat().st_mode, (outside / "keep").read_bytes())

    def swap(path: Path) -> None:
        path.rmdir()
        path.symlink_to(outside, target_is_directory=True)

    real_open = seeding_mod.open_root

    def open_root(path: Path) -> int:
        assert path == root
        swap(path)
        return real_open(path)

    monkeypatch.setattr(seeding_mod, "open_root", open_root)
    with pytest.raises(SeedingError) as failed:
        seed_config(tmp_path)
    message = str(failed.value)
    assert isinstance(failed.value.__cause__, contained_paths.PathContainmentError)
    assert f"Cannot install baseline seed target {root}: symlink." in message
    assert "Move or remove the conflicting path, then rerun." in message
    assert "writable" not in message
    assert "chown" not in message
    assert (outside.stat().st_mode, (outside / "keep").read_bytes()) == before


def test_baseline_io_failure_has_target_and_writability_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_seed_fixture(tmp_path)
    root = tmp_path / "config/claude"
    failure = OSError(errno.EIO, "Input/output error", ".djinn-publisher-internal")
    real_write = os.write
    denied = False

    def write(descriptor: int, *args: Any, **kwargs: Any) -> int:
        nonlocal denied
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if path.parent == root and path.name.startswith(".djinn-publisher-"):
            denied = True
            raise failure
        return real_write(descriptor, *args, **kwargs)

    monkeypatch.setattr(contained_paths.os, "write", write)
    with pytest.raises(SeedingError) as failed:
        seed_config(tmp_path)
    message = str(failed.value)
    assert denied
    assert isinstance(failed.value.__cause__, OSError)
    assert failed.value.__cause__.errno == errno.EIO
    target = root / "AGENTS.md"
    assert f"Cannot install baseline seed target {target}: Input/output error." in message
    assert f"Check that {root} is writable, then rerun." in message
    assert "chown" not in message
    assert "Move or remove" not in message
    assert ".djinn-publisher-" not in message
    assert list(root.iterdir()) == []
