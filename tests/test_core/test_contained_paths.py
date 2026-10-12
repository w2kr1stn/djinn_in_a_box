"""Containment, preservation and failure semantics of the shared host helpers."""

from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from djinn_in_a_box.core import contained_paths as paths
from djinn_in_a_box.core import workflow_publisher as publisher
from djinn_in_a_box.core.workflow_publisher import DriftClass, PublishError


@pytest.fixture(autouse=True)
def _umask() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)



def _assert_os_error(error: OSError, original: OSError, path: Path) -> None:
    assert type(error) is type(original)
    assert error.errno == original.errno
    assert error.strerror == original.strerror
    assert error.filename == str(path)
    cause: BaseException | None = error.__cause__
    while cause is not None and cause is not original:
        cause = cause.__cause__
    assert cause is original

def _snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    return {
        str(path.relative_to(root)): (
            stat.S_IMODE(path.lstat().st_mode),
            path.read_bytes() if path.is_file() else None,
        )
        for path in [root, *sorted(root.rglob("*"))]
    }


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    outside.chmod(0o755)
    (outside / "keep").write_bytes(b"outside\x00")
    (outside / "keep").chmod(0o640)
    return root, outside


def _invoke(operation: str, root: Path, relative: PurePosixPath) -> None:
    if operation == "ensure":
        paths.ensure_directory(root, relative, mode=0o700, secure=True)
    elif operation == "file":
        paths.ensure_empty_file(root, relative / "file", directory_mode=0o700, file_mode=0o600)
    elif operation == "tighten":
        paths.tighten_directory(root, relative, mode=0o700)
    elif operation == "secure":
        paths.secure_root(root, mode=0o700)
    elif operation == "clear":
        paths.clear_directory(root)
    else:
        descriptor = paths.open_root(root)
        os.close(descriptor)


@pytest.mark.parametrize("kind", ["link", "dangling", "file", "fifo"])
@pytest.mark.parametrize("operation", ["open", "ensure", "file", "tighten", "secure", "clear"])
def test_root_refusals(tmp_path: Path, kind: str, operation: str) -> None:
    root, outside = _roots(tmp_path)
    root.rmdir()
    if kind in {"link", "dangling"}:
        root.symlink_to(outside if kind == "link" else outside / "missing")
    elif kind == "fifo":
        os.mkfifo(root)
    else:
        root.write_bytes(b"root file")
    before = _snapshot(outside)
    fds = len(os.listdir("/proc/self/fd"))
    with pytest.raises(paths.PathContainmentError) as refusal:
        _invoke(operation, root, PurePosixPath("one/two/three"))
    assert refusal.value.path == root
    assert refusal.value.reason == (
        "symlink" if kind in {"link", "dangling"} else "not a directory"
    )
    assert str(root) in str(refusal.value)
    assert _snapshot(outside) == before
    assert len(os.listdir("/proc/self/fd")) == fds


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("kind", ["link", "file"])
@pytest.mark.parametrize("operation", ["ensure", "file", "tighten"])
def test_component_refusals(tmp_path: Path, depth: int, kind: str, operation: str) -> None:
    root, outside = _roots(tmp_path)
    relative = PurePosixPath("one/two/three")
    refused = root.joinpath(*relative.parts[:depth])
    refused.parent.mkdir(parents=True, exist_ok=True)
    if kind == "link":
        refused.symlink_to(outside)
    else:
        refused.write_bytes(b"in-root file")
    before = _snapshot(outside)
    fds = len(os.listdir("/proc/self/fd"))
    with pytest.raises(paths.PathContainmentError) as failure:
        _invoke(operation, root, relative)
    assert failure.value.path == refused
    assert failure.value.reason == ("symlink" if kind == "link" else "not a directory")
    assert _snapshot(outside) == before
    assert len(os.listdir("/proc/self/fd")) == fds


@pytest.mark.parametrize("kind", ["link", "dangling", "fifo", "directory"])
def test_empty_file_refuses_special_leaf(tmp_path: Path, kind: str) -> None:
    root, outside = _roots(tmp_path)
    leaf = root / "file"
    if kind in {"link", "dangling"}:
        leaf.symlink_to(outside / ("keep" if kind == "link" else "missing"))
    elif kind == "fifo":
        os.mkfifo(leaf)
    else:
        leaf.mkdir()
    before = _snapshot(outside)
    # O_EXCL checks existence before any FIFO can be opened.
    with pytest.raises(paths.PathContainmentError) as refusal:
        paths.ensure_empty_file(root, PurePosixPath("file"), directory_mode=0o700, file_mode=0o600)
    assert refusal.value.path == leaf
    assert refusal.value.reason == (
        "symlink" if kind in {"link", "dangling"} else "not a regular file"
    )
    assert _snapshot(outside) == before


def test_modes_creation_and_preservation(tmp_path: Path) -> None:
    root, _outside = _roots(tmp_path)
    paths.secure_root(root, mode=0o700)
    paths.ensure_directory(root, PurePosixPath("one/two/three"), mode=0o700, secure=True)
    for directory in (root, root / "one", root / "one/two", root / "one/two/three"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        directory.chmod(0o755)
    paths.ensure_directory(root, PurePosixPath("one/two/three"), mode=0o700, secure=False)
    assert stat.S_IMODE((root / "one/two/three").stat().st_mode) == 0o755
    paths.ensure_directory(root, PurePosixPath("one/two/three"), mode=0o700, secure=True)
    assert all(
        stat.S_IMODE(p.stat().st_mode) == 0o700
        for p in (root / "one", root / "one/two", root / "one/two/three")
    )
    paths.ensure_directory(root, PurePosixPath("workspace"), mode=0o777, secure=False)
    assert stat.S_IMODE((root / "workspace").stat().st_mode) == 0o755
    paths.ensure_empty_file(
        root, PurePosixPath("placeholder/empty"), directory_mode=0o700, file_mode=0o600
    )
    assert stat.S_IMODE((root / "placeholder").stat().st_mode) == 0o700
    leaf = root / "placeholder/empty"
    assert leaf.read_bytes() == b""
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o600
    leaf.write_bytes(b"existing bytes")
    leaf.chmod(0o640)
    paths.ensure_empty_file(
        root, PurePosixPath("placeholder/empty"), directory_mode=0o700, file_mode=0o600
    )
    assert leaf.read_bytes() == b"existing bytes"
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o640
    paths.tighten_directory(root, PurePosixPath(), mode=0o700)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    paths.tighten_directory(root, PurePosixPath("workspace"), mode=0o700)
    assert stat.S_IMODE((root / "workspace").stat().st_mode) == 0o700
    new_root = tmp_path / "trusted/new/root"
    paths.secure_root(new_root, mode=0o700)
    assert stat.S_IMODE(new_root.stat().st_mode) == 0o700


def test_missing_paths_and_other_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _outside = _roots(tmp_path)
    with pytest.raises(FileNotFoundError):
        paths.open_root(root / "missing")
    with pytest.raises(FileNotFoundError):
        paths.tighten_directory(root, PurePosixPath("missing"), mode=0o700)
    (root / "child").mkdir()
    for exception in (
        PermissionError(errno.EACCES, "denied"),
        PublishError(DriftClass.INVALID_OR_SEMANTIC),
    ):

        def fail(*_args: Any, caught: BaseException = exception, **_kwargs: Any) -> Any:
            raise caught

        with monkeypatch.context() as patcher:
            patcher.setattr(paths, "_open_child_directory", fail)
            with pytest.raises(type(exception)) as failure:
                paths.ensure_directory(root, PurePosixPath("child"), mode=0o700, secure=True)
            if isinstance(exception, OSError):
                assert isinstance(failure.value, OSError)
                _assert_os_error(failure.value, exception, root / "child")
            else:
                assert failure.value is exception
        with monkeypatch.context() as patcher:
            patcher.setattr(paths, "_open_root", fail)
            with pytest.raises(type(exception)) as failure:
                paths.open_root(root)
            if isinstance(exception, OSError):
                assert isinstance(failure.value, OSError)
                _assert_os_error(failure.value, exception, root)
            else:
                assert failure.value is exception


@pytest.mark.parametrize("relative", ["../outside", "/absolute", "one/../../outside"])
def test_unsafe_names_are_translated(tmp_path: Path, relative: str) -> None:
    root, outside = _roots(tmp_path)
    before = _snapshot(outside)
    with pytest.raises(paths.PathContainmentError) as refusal:
        paths.ensure_directory(root, PurePosixPath(relative), mode=0o700, secure=False)
    if PurePosixPath(relative).is_absolute():
        assert refusal.value.path == Path(relative)
    descriptor = paths.open_root(root)
    try:
        for operation in (
            lambda: paths.remove_entry(descriptor, ".."),
            lambda: paths.install_file(descriptor, "../keep", b"bad", False),
            lambda: paths.install_tree(descriptor, "..", outside),
        ):
            with pytest.raises(paths.PathContainmentError):
                operation()
    finally:
        os.close(descriptor)
    assert _snapshot(outside) == before


@pytest.mark.parametrize("operation", ["ensure", "file", "tighten", "secure", "clear"])
def test_swap_before_directory_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    root, outside = _roots(tmp_path)
    (root / "one/two/three").mkdir(parents=True)
    victim = root if operation == "secure" else root / "one/two/three"
    before = _snapshot(outside)
    real_open = os.open
    swapped = False

    def swap(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal swapped
        if not swapped and (name == victim if operation == "secure" else name == "three"):
            swapped = True
            victim.rename(victim.with_name(victim.name + "-saved"))
            victim.symlink_to(outside)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(paths.os, "open", swap)
    with pytest.raises(paths.PathContainmentError) as failure:
        _invoke(operation, root, PurePosixPath("one/two/three"))
    assert swapped
    assert failure.value.path == victim
    assert _snapshot(outside) == before


def test_mkdir_race_and_no_follow_file_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _roots(tmp_path)
    before = _snapshot(outside)
    real_mkdir, real_open = os.mkdir, os.open

    def mkdir(name: Any, *args: Any, **kwargs: Any) -> None:
        if name == "child" and kwargs.get("dir_fd") is not None:
            os.symlink(outside, name, dir_fd=kwargs["dir_fd"])
        real_mkdir(name, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(paths.os, "mkdir", mkdir)
        with pytest.raises(paths.PathContainmentError):
            paths.ensure_directory(root, PurePosixPath("child"), mode=0o700, secure=True)

    def check_flags(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert flags & os.O_NOFOLLOW
        assert flags & os.O_CLOEXEC
        if name != "file":
            assert flags & os.O_DIRECTORY
        if name == "file":
            assert flags & os.O_WRONLY
            assert flags & os.O_CREAT
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_CLOEXEC
            assert flags & os.O_EXCL
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(paths.os, "open", check_flags)
    paths.ensure_empty_file(root, PurePosixPath("file"), directory_mode=0o700, file_mode=0o600)
    assert _snapshot(outside) == before


@pytest.mark.parametrize("preserved_errno", [errno.EACCES, errno.EBUSY, errno.EPERM])
def test_remove_visits_later_sibling_and_preserves_only_placeholders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preserved_errno: int
) -> None:
    root, outside = _roots(tmp_path)
    (root / "parent/aa-placeholder").mkdir(parents=True)
    (root / "parent/zz-link").symlink_to(outside)
    (root / "later-file").write_bytes(b"delete")
    before = _snapshot(outside)
    real_rmdir = os.rmdir

    def rmdir(name: Any, *args: Any, **kwargs: Any) -> None:
        if name == "aa-placeholder":
            raise OSError(preserved_errno, "busy placeholder")
        assert name != "parent", "preserved ancestors must not be removed"
        real_rmdir(name, *args, **kwargs)

    monkeypatch.setattr(paths.os, "rmdir", rmdir)
    paths.clear_directory(root)
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == [
        "parent",
        "parent/aa-placeholder",
    ]
    assert _snapshot(outside) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses filesystem EACCES")
def test_real_eacces_on_later_link_fails_clear(tmp_path: Path) -> None:
    root, outside = _roots(tmp_path)
    parent = root / "parent"
    (parent / "aa-placeholder").mkdir(parents=True)
    (parent / "zz-link").symlink_to(outside)
    before = _snapshot(outside)
    parent.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            paths.clear_directory(root)
        assert (parent / "zz-link").is_symlink()
        assert _snapshot(outside) == before
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize("failure", ["other-errno", "nonempty", "unlink"])
def test_removal_failures_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root, outside = _roots(tmp_path)
    (root / "entry").mkdir()
    before = _snapshot(outside)

    def fail(name: Any, *args: Any, **kwargs: Any) -> None:
        if failure == "nonempty":
            (root / "entry/new").write_bytes(b"arrived")
        raise OSError(errno.EIO if failure == "other-errno" else errno.EACCES, "failure")

    if failure == "unlink":
        (root / "entry").rmdir()
        (root / "entry").symlink_to(outside)
        monkeypatch.setattr(paths.os, "unlink", fail)
    else:
        monkeypatch.setattr(paths.os, "rmdir", fail)
    with pytest.raises(OSError):
        paths.clear_directory(root)
    assert _snapshot(outside) == before


def test_clear_normal_and_install_file(tmp_path: Path) -> None:
    root, outside = _roots(tmp_path)
    (root / "one/two").mkdir(parents=True)
    (root / "one/two/file").write_bytes(b"delete")
    (root / "link").symlink_to(outside)
    os.mkfifo(root / "fifo")
    before = _snapshot(outside)
    paths.clear_directory(root)
    assert list(root.iterdir()) == []
    descriptor = paths.open_root(root)
    try:
        for executable in (False, True):
            paths.install_file(descriptor, "installed", b"shipped", executable)
            assert (root / "installed").read_bytes() == b"shipped"
            assert stat.S_IMODE((root / "installed").stat().st_mode) == (
                0o755 if executable else 0o644
            )
        (root / "installed").unlink()
        (root / "installed").symlink_to(outside / "keep")
        paths.install_file(descriptor, "installed", b"safe", False)
        assert not (root / "installed").is_symlink()
    finally:
        os.close(descriptor)
    assert _snapshot(outside) == before


def test_install_tree_and_exclusive_temp_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _roots(tmp_path)
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / ".gitkeep").write_bytes(b"")
    (source / "nested/script").write_bytes(b"#!/bin/sh\n")
    (source / "nested/script").chmod(0o755)
    (source / "plain").write_bytes(b"plain")
    before = _snapshot(outside)
    occupied = ".djinn-seed-" + (b"a" * 8).hex()
    (root / occupied).symlink_to(outside)
    random_values = iter([b"a" * 8, b"b" * 8, b"c" * 8, b"d" * 8])
    real_random = os.urandom

    def random(size: int, *args: Any, **kwargs: Any) -> bytes:
        if size == 8:
            return next(random_values)
        return real_random(size, *args, **kwargs)

    monkeypatch.setattr(paths.os, "urandom", random)
    descriptor = paths.open_root(root)
    try:
        paths.install_tree(descriptor, "installed", source)
    finally:
        os.close(descriptor)
    assert (root / "installed/nested/script").read_bytes() == b"#!/bin/sh\n"
    assert stat.S_IMODE((root / "installed/nested/script").stat().st_mode) == 0o755
    assert (root / "installed/plain").read_bytes() == b"plain"
    assert stat.S_IMODE((root / "installed/plain").stat().st_mode) == 0o644
    assert not (root / "installed/.gitkeep").exists()
    assert sorted(p.name for p in root.iterdir()) == [occupied, "installed"]
    assert _snapshot(outside) == before


def test_install_tree_temp_swap_refused_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _roots(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"shipped")
    before = _snapshot(outside)
    real_open = os.open

    def swap(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if isinstance(name, str) and name.startswith(".djinn-seed-"):
            os.rmdir(name, dir_fd=kwargs["dir_fd"])
            os.symlink(outside, name, dir_fd=kwargs["dir_fd"])
        return real_open(name, flags, *args, **kwargs)

    descriptor = paths.open_root(root)
    monkeypatch.setattr(paths.os, "open", swap)
    try:
        with pytest.raises(paths.PathContainmentError):
            paths.install_tree(descriptor, "installed", source)
    finally:
        os.close(descriptor)
    assert list(root.iterdir()) == []
    assert _snapshot(outside) == before


def test_install_failures_cleanup_and_leave_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _roots(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"new")
    (root / "installed").write_bytes(b"old")
    before = _snapshot(outside)

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.EIO, "write failed")

    descriptor = paths.open_root(root)
    monkeypatch.setattr(publisher.os, "write", fail)
    try:
        with pytest.raises(OSError):
            paths.install_tree(descriptor, "tree", source)
        with pytest.raises(OSError):
            paths.install_file(descriptor, "installed", b"new", False)
    finally:
        os.close(descriptor)
    assert sorted(p.name for p in root.iterdir()) == ["installed"]
    assert (root / "installed").read_bytes() == b"old"
    assert _snapshot(outside) == before


def test_remove_entry_return_values_and_fd_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _roots(tmp_path)
    (root / "empty").mkdir()
    (root / "preserved").mkdir()
    (root / "link").symlink_to(outside)
    before = _snapshot(outside)
    real_rmdir = os.rmdir

    def rmdir(name: Any, *args: Any, **kwargs: Any) -> None:
        if name == "preserved":
            raise OSError(errno.EPERM, "placeholder")
        real_rmdir(name, *args, **kwargs)

    monkeypatch.setattr(paths.os, "rmdir", rmdir)
    descriptor = paths.open_root(root)
    try:
        fds = len(os.listdir("/proc/self/fd"))
        assert paths.remove_entry(descriptor, "empty") is True
        assert paths.remove_entry(descriptor, "link") is True
        assert paths.remove_entry(descriptor, "preserved") is False
        assert len(os.listdir("/proc/self/fd")) == fds
    finally:
        os.close(descriptor)
    assert _snapshot(outside) == before


def test_fchmod_failure_closes_all_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _roots(tmp_path)
    before = _snapshot(outside)
    exception = PermissionError(errno.EPERM, "test fchmod")
    real_fchmod = os.fchmod

    def fail(descriptor: int, *args: Any, **kwargs: Any) -> None:
        if Path(os.readlink(f"/proc/self/fd/{descriptor}")).is_relative_to(root):
            raise exception
        real_fchmod(descriptor, *args, **kwargs)

    monkeypatch.setattr(paths.os, "fchmod", fail)
    for target, operation in (
        (root, lambda: paths.secure_root(root, mode=0o700)),
        (root / "child", lambda: paths.ensure_directory(
            root, PurePosixPath("child"), mode=0o700, secure=True
        )),
        (root, lambda: paths.tighten_directory(root, PurePosixPath(), mode=0o700)),
    ):
        fds = len(os.listdir("/proc/self/fd"))
        with pytest.raises(PermissionError) as failed:
            operation()
        _assert_os_error(failed.value, exception, target)
        assert len(os.listdir("/proc/self/fd")) == fds
    assert _snapshot(outside) == before


def test_failed_rmdir_reopen_refuses_a_swapped_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _roots(tmp_path)
    victim = root / "empty"
    victim.mkdir()
    before = _snapshot(outside)
    real_rmdir = os.rmdir

    def rmdir(name: Any, *args: Any, **kwargs: Any) -> None:
        if name == "empty" and kwargs.get("dir_fd") is not None:
            real_rmdir(name, *args, **kwargs)
            os.symlink(outside, name, dir_fd=kwargs["dir_fd"])
            raise OSError(errno.EACCES, "swapped placeholder")
        real_rmdir(name, *args, **kwargs)

    monkeypatch.setattr(paths.os, "rmdir", rmdir)
    fds = len(os.listdir("/proc/self/fd"))
    with pytest.raises(paths.PathContainmentError) as refusal:
        paths.clear_directory(root)
    assert refusal.value.path == victim
    assert refusal.value.reason == "symlink"
    assert len(os.listdir("/proc/self/fd")) == fds
    assert _snapshot(outside) == before


def test_clear_and_remove_visit_sorted_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _roots(tmp_path)
    before = _snapshot(outside)
    for name in ("zz", "aa", "mm"):
        directory = root / name
        directory.mkdir()
        for child in ("zz", "aa", "mm"):
            (directory / child).write_bytes(b"delete")
    visited = []
    real_stat = os.stat

    def entry_stat(name: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if kwargs.get("dir_fd") is not None:
            assert kwargs["follow_symlinks"] is False
            visited.append(name)
        return real_stat(name, *args, **kwargs)

    monkeypatch.setattr(paths.os, "stat", entry_stat)
    paths.clear_directory(root)
    assert visited == ["aa", "aa", "mm", "zz", "mm", "aa", "mm", "zz", "zz", "aa", "mm", "zz"]
    assert _snapshot(outside) == before


def test_remove_tree_beyond_recursion_limit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    directories = [root / "tree"]
    directories[0].mkdir()
    for _ in range(sys.getrecursionlimit() + 11):
        directories.append(directories[-1] / "d")
        directories[-1].mkdir()
    (directories[-1] / "file").write_bytes(b"delete")
    descriptor = paths.open_root(root)
    before = len(os.listdir("/proc/self/fd"))
    try:
        try:
            assert paths.remove_entry(descriptor, "tree") is True
            assert list(root.iterdir()) == []
        except OSError as exc:
            assert exc.errno in {errno.EMFILE, errno.ENFILE}
        assert len(os.listdir("/proc/self/fd")) == before
    finally:
        os.close(descriptor)
        (directories[-1] / "file").unlink(missing_ok=True)
        for directory in reversed(directories):
            if directory.exists():
                directory.rmdir()


@pytest.mark.parametrize("failure", ["open", "listdir", "unlink", "rmdir"])
def test_remove_nested_failure_closes_active_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root, _ = _roots(tmp_path)
    (root / "tree/child/leaf").mkdir(parents=True)
    (root / "tree/child/leaf/file").write_bytes(b"delete")
    real_call = getattr(os, failure)

    def fail(name: Any, *args: Any, **kwargs: Any) -> Any:
        candidate = (Path(os.readlink(f"/proc/self/fd/{name}")).name
                     if failure == "listdir" else name)
        if candidate == ("file" if failure == "unlink" else "leaf"):
            raise OSError(errno.EIO, "nested failure")
        return real_call(name, *args, **kwargs)

    before = len(os.listdir("/proc/self/fd"))
    with monkeypatch.context() as patcher:
        patcher.setattr(paths.os, failure, fail)
        with pytest.raises(OSError, match="nested failure"):
            paths.clear_directory(root)
    assert len(os.listdir("/proc/self/fd")) == before


def test_remove_directory_swapped_before_open_refuses_without_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _roots(tmp_path)
    (root / "tree/victim").mkdir(parents=True)
    before = _snapshot(outside)
    real_open = os.open

    def swap(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if name == "victim" and kwargs.get("dir_fd") is not None:
            os.rmdir(name, dir_fd=kwargs["dir_fd"])
            os.symlink(outside, name, dir_fd=kwargs["dir_fd"])
        return real_open(name, flags, *args, **kwargs)

    fds = len(os.listdir("/proc/self/fd"))
    with monkeypatch.context() as patcher:
        patcher.setattr(paths.os, "open", swap)
        with pytest.raises(paths.PathContainmentError) as refusal:
            paths.clear_directory(root)
        assert refusal.value.path == root / "tree/victim"
        assert refusal.value.reason == "symlink"
    assert len(os.listdir("/proc/self/fd")) == fds
    assert _snapshot(outside) == before


@pytest.mark.parametrize("kind", ["link", "directory", "fifo", "missing"])
def test_install_file_post_commit_substitution_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root, outside = _roots(tmp_path)
    before = _snapshot(outside)
    real_replace = paths._replace_at  # pyright: ignore[reportPrivateUsage]

    def substitute(parent: int, name: str, content: bytes, executable: bool) -> None:
        real_replace(parent, name, content, executable)
        os.unlink(name, dir_fd=parent)
        if kind == "link":
            os.symlink(outside / "keep", name, dir_fd=parent)
        elif kind == "directory":
            os.mkdir(name, dir_fd=parent)
        elif kind == "fifo":
            os.mkfifo(name, dir_fd=parent)

    monkeypatch.setattr(paths, "_replace_at", substitute)
    descriptor = paths.open_root(root)
    fds = len(os.listdir("/proc/self/fd"))
    try:
        with pytest.raises(paths.PathContainmentError) as refusal:
            paths.install_file(descriptor, "installed", b"shipped", False)
        assert refusal.value.path == root / "installed"
        assert refusal.value.reason == ("symlink" if kind == "link" else "not a regular file")
        assert len(os.listdir("/proc/self/fd")) == fds
    finally:
        os.close(descriptor)
    assert _snapshot(outside) == before


@pytest.mark.parametrize("cause", [None, OSError(errno.EIO, "read failed"), ValueError("invalid")])
def test_install_file_read_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: BaseException | None
) -> None:
    root, _outside = _roots(tmp_path)
    failure = PublishError(DriftClass.INVALID_OR_SEMANTIC)

    def fail(_parent: int, _name: str) -> Any:
        raise failure from cause

    monkeypatch.setattr(paths, "_read_at", fail)
    descriptor = paths.open_root(root)
    try:
        expected = OSError if isinstance(cause, OSError) else paths.PathContainmentError
        with pytest.raises(expected) as failed:
            paths.install_file(descriptor, "installed", b"shipped", False)
        if isinstance(cause, OSError):
            assert isinstance(failed.value, OSError)
            _assert_os_error(failed.value, cause, root / "installed")
        else:
            assert isinstance(failed.value, paths.PathContainmentError)
            assert failed.value.path == root / "installed"
            assert failed.value.reason == "substituted"
            assert failed.value.__cause__ is failure
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("field", ["device", "inode"])
def test_install_tree_rejects_committed_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    root, outside = _roots(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"shipped")
    before = _snapshot(outside)
    real_stat = os.stat

    def changed_stat(name: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = real_stat(name, *args, **kwargs)
        if name == "installed" and kwargs.get("dir_fd") is not None:
            assert kwargs["follow_symlinks"] is False
            values = list(info)
            values[2 if field == "device" else 1] += 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(paths.os, "stat", changed_stat)
    descriptor = paths.open_root(root)
    fds = len(os.listdir("/proc/self/fd"))
    try:
        with pytest.raises(paths.PathContainmentError) as refusal:
            paths.install_tree(descriptor, "installed", source)
        assert refusal.value.path == root / "installed"
        assert refusal.value.reason == "substituted"
        assert len(os.listdir("/proc/self/fd")) == fds
    finally:
        os.close(descriptor)
    assert not list(root.glob(".djinn-seed-*"))
    assert _snapshot(outside) == before


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFREG])
def test_install_tree_refuses_non_directory_diagnostic_with_matching_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    root, _ = _roots(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    real_stat = os.stat

    def changed_stat(name: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        info = real_stat(name, *args, **kwargs)
        if name == "installed" and kwargs.get("dir_fd") is not None:
            values = list(info)
            values[0] = mode | 0o644
            return os.stat_result(values)
        return info

    monkeypatch.setattr(paths.os, "stat", changed_stat)
    descriptor = paths.open_root(root)
    try:
        with pytest.raises(paths.PathContainmentError) as refusal:
            paths.install_tree(descriptor, "installed", source)
        assert refusal.value.path == root / "installed"
        assert refusal.value.reason == ("symlink" if mode == stat.S_IFLNK else "not a directory")
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("failure", ["replace", "stat", "fstat"])
def test_install_tree_commit_error_closes_descriptor_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root, outside = _roots(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_bytes(b"shipped")
    before = _snapshot(outside)
    real_call = getattr(os, failure)

    def fail(name: Any, *args: Any, **kwargs: Any) -> Any:
        if ((failure == "fstat" and os.readlink(f"/proc/self/fd/{name}") == str(root / "installed"))
                or name == "installed"
                or (failure == "replace" and args[0] == "installed")):
            raise OSError(errno.EIO, "commit failure")
        return real_call(name, *args, **kwargs)

    descriptor = paths.open_root(root)
    fds = len(os.listdir("/proc/self/fd"))
    with monkeypatch.context() as patcher:
        patcher.setattr(paths.os, failure, fail)
        try:
            with pytest.raises(OSError, match="commit failure"):
                paths.install_tree(descriptor, "installed", source)
            assert len(os.listdir("/proc/self/fd")) == fds
        finally:
            os.close(descriptor)
    assert not list(root.glob(".djinn-seed-*"))
    assert (root / "installed").exists() is (failure != "replace")
    assert _snapshot(outside) == before


def test_secure_root_creates_only_missing_roots_with_requested_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _roots(tmp_path)
    before = _snapshot(outside)
    created = []
    missing = tmp_path / "trusted/new"
    missing.parent.mkdir()
    real_mkdir = Path.mkdir

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path in (root, missing):
            assert kwargs["mode"] == 0o700
            created.append(path)
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    paths.secure_root(root, mode=0o700)
    assert created == []
    paths.secure_root(missing, mode=0o700)
    assert created == [missing]
    assert _snapshot(outside) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory write permissions")
def test_secure_root_creation_error_keeps_failing_ancestor(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    root = blocked / "ancestor" / "root"
    blocked.chmod(0o500)
    try:
        with pytest.raises(PermissionError) as failed:
            paths.secure_root(root, mode=0o700)
        assert failed.value.filename == str(blocked / "ancestor")
    finally:
        blocked.chmod(0o700)


def test_open_root_follows_trusted_ancestors(tmp_path: Path) -> None:
    root, outside = _roots(tmp_path)
    before = _snapshot(outside)
    trusted_link = tmp_path / "trusted-link"
    trusted_link.symlink_to(root)
    (root / "actual").mkdir()
    descriptor = paths.open_root(trusted_link / "actual")
    try:
        assert os.fstat(descriptor).st_ino == (root / "actual").stat().st_ino
    finally:
        os.close(descriptor)
    assert _snapshot(outside) == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory read permissions")
@pytest.mark.parametrize("operation", ["secure", "ensure", "tighten"])
@pytest.mark.parametrize("position", ["root", "intermediate", "final"])
@pytest.mark.parametrize("blocked_mode", [0o000, 0o300])
@pytest.mark.parametrize("mode", [0o700, 0o750])
def test_securing_repairs_owned_unreadable_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, position: str,
    blocked_mode: int, mode: int,
) -> None:
    root, _outside = _roots(tmp_path)
    intermediate = root / "one"
    final = intermediate / "two"
    final.mkdir(parents=True)
    victim = {"root": root, "intermediate": intermediate, "final": final}[position]
    victim.chmod(blocked_mode)
    real_chmod = os.chmod
    real_open = os.open
    repaired: list[Path] = []
    directory_opens = 0

    def open_entry(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal directory_opens
        parent = kwargs.get("dir_fd")
        candidate = (
            Path(name) if parent is None
            else Path(os.readlink(f"/proc/self/fd/{parent}")) / name
        )
        if flags & os.O_PATH:
            assert flags == os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
            if operation == "secure" or position == "root":
                assert name == victim and parent is None
            else:
                assert name == victim.name and parent is not None
        elif candidate == victim:
            directory_opens += 1
            assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
        return real_open(name, flags, *args, **kwargs)

    def chmod(name: Any, *args: Any, **kwargs: Any) -> None:
        assert args == (mode,)
        assert kwargs == {}
        assert str(name).startswith("/proc/self/fd/")
        repaired.append(Path(os.readlink(name)))
        real_chmod(name, *args, **kwargs)

    monkeypatch.setattr(paths.os, "open", open_entry)
    monkeypatch.setattr(paths.os, "chmod", chmod)
    fds = len(os.listdir("/proc/self/fd"))
    try:
        if operation == "secure":
            paths.secure_root(victim, mode=mode)
        elif operation == "ensure":
            paths.ensure_directory(root, PurePosixPath("one/two"), mode=mode, secure=True)
        else:
            paths.tighten_directory(root, PurePosixPath(victim.relative_to(root)), mode=mode)
        assert stat.S_IMODE(victim.stat().st_mode) == mode
        assert repaired == [victim]
        assert directory_opens == 2
        assert len(os.listdir("/proc/self/fd")) == fds
    finally:
        for directory in (root, intermediate, final):
            real_chmod(directory, 0o700)


@pytest.mark.parametrize("operation", ["secure", "ensure", "tighten"])
@pytest.mark.parametrize("position", ["root", "child"])
@pytest.mark.parametrize("kind", ["link", "file", "fifo", "missing", "pin-error"])
def test_permission_repair_refuses_non_directories_and_propagates_io_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, position: str, kind: str,
) -> None:
    root, outside = _roots(tmp_path)
    victim = root if position == "root" else root / "child"
    if position == "root":
        root.rmdir()
    if kind == "link":
        victim.symlink_to(outside, target_is_directory=True)
    elif kind == "file":
        victim.write_bytes(b"keep")
    elif kind == "fifo":
        os.mkfifo(victim)
    elif kind == "pin-error":
        victim.mkdir()
    if kind in {"file", "fifo"}:
        victim.chmod(0o640)
    before = _snapshot(outside)
    failure = PermissionError(errno.EACCES, "original open denied")
    real_open_root = paths.open_root
    real_child = paths._open_child_directory  # pyright: ignore[reportPrivateUsage]
    real_open = os.open
    real_chmod = os.chmod
    pin_failure = OSError(errno.EIO, "pin failed")
    chmod_calls: list[Any] = []

    def open_root(path: Path) -> int:
        if path == victim:
            if kind == "missing" and path.is_dir():
                path.rmdir()
            raise failure
        return real_open_root(path)

    def open_child(parent: int, name: str, *args: Any, **kwargs: Any) -> int | None:
        if name == victim.name:
            raise failure
        return real_child(parent, name, *args, **kwargs)

    def open_entry(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if name in (victim, victim.name):
            assert flags & os.O_PATH  # Never risk a blocking FIFO open.
            if kind == "pin-error":
                raise pin_failure
        return real_open(name, flags, *args, **kwargs)

    def chmod(name: Any, *args: Any, **kwargs: Any) -> None:
        chmod_calls.append(name)
        real_chmod(name, *args, **kwargs)

    monkeypatch.setattr(paths, "open_root", open_root)
    monkeypatch.setattr(paths, "_open_child_directory", open_child)
    monkeypatch.setattr(paths.os, "open", open_entry)
    monkeypatch.setattr(paths.os, "chmod", chmod)
    relative = PurePosixPath() if position == "root" else PurePosixPath("child")
    fds = len(os.listdir("/proc/self/fd"))
    exception = paths.PathContainmentError if kind in {"link", "file", "fifo"} else OSError
    with pytest.raises(exception) as failed:
        if operation == "secure":
            paths.secure_root(victim, mode=0o700)
        elif operation == "ensure":
            paths.ensure_directory(root, relative, mode=0o700, secure=True)
        else:
            paths.tighten_directory(root, relative, mode=0o700)
    if isinstance(failed.value, paths.PathContainmentError):
        assert failed.value.path == victim
        assert failed.value.reason == ("symlink" if kind == "link" else "not a directory")
        if kind in {"file", "fifo"}:
            assert stat.S_IMODE(victim.lstat().st_mode) == 0o640
        if kind == "file":
            assert victim.read_bytes() == b"keep"
    elif kind == "pin-error":
        assert isinstance(failed.value, OSError)
        _assert_os_error(failed.value, pin_failure, victim)
    else:
        assert isinstance(failed.value, FileNotFoundError)
    assert chmod_calls == []
    assert _snapshot(outside) == before
    assert len(os.listdir("/proc/self/fd")) == fds


@pytest.mark.parametrize("operation", ["secure", "ensure", "tighten"])
@pytest.mark.parametrize("position", ["root", "child"])
@pytest.mark.parametrize("errno_value", [errno.EACCES, errno.EIO])
def test_permission_repair_retries_open_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, position: str,
    errno_value: int,
) -> None:
    root, _outside = _roots(tmp_path)
    victim = root if position == "root" else root / "child"
    if position == "child":
        victim.mkdir()
    failure = OSError(errno_value, "open still denied")
    real_open_root = paths.open_root
    real_child = paths._open_child_directory  # pyright: ignore[reportPrivateUsage]
    attempts = 0

    def deny() -> None:
        nonlocal attempts
        attempts += 1
        assert attempts <= 2
        raise failure

    def open_root(path: Path) -> int:
        if path == victim:
            deny()
        return real_open_root(path)

    def open_child(parent: int, name: str, *args: Any, **kwargs: Any) -> int | None:
        if name == victim.name:
            deny()
        return real_child(parent, name, *args, **kwargs)

    monkeypatch.setattr(paths, "open_root", open_root)
    monkeypatch.setattr(paths, "_open_child_directory", open_child)
    relative = PurePosixPath() if position == "root" else PurePosixPath("child")
    fds = len(os.listdir("/proc/self/fd"))
    with pytest.raises(OSError) as failed:
        if operation == "secure":
            paths.secure_root(victim, mode=0o700)
        elif operation == "ensure":
            paths.ensure_directory(root, relative, mode=0o700, secure=True)
        else:
            paths.tighten_directory(root, relative, mode=0o700)
    _assert_os_error(failed.value, failure, victim)
    assert attempts == (2 if errno_value == errno.EACCES else 1)
    assert victim.stat().st_mode & 0o777 == (0o700 if errno_value == errno.EACCES else 0o755)
    assert len(os.listdir("/proc/self/fd")) == fds


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory read permissions")
@pytest.mark.parametrize("operation", ["secure", "ensure", "tighten"])
@pytest.mark.parametrize("position", ["root", "intermediate", "final"])
@pytest.mark.parametrize("kind", ["file", "fifo", "link", "directory", "missing"])
def test_permission_repair_pins_original_before_name_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, position: str, kind: str,
) -> None:
    root, outside = _roots(tmp_path)
    (root / "one/two").mkdir(parents=True)
    victim = {"root": root, "intermediate": root / "one", "final": root / "one/two"}[position]
    (victim / "keep").write_bytes(b"original")
    saved = victim.with_name(victim.name + "-saved")
    before = _snapshot(outside)
    victim.chmod(0o300)
    real_open = os.open
    denied = False
    swapped = False

    def open_entry(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal denied, swapped
        parent = kwargs.get("dir_fd")
        candidate = (
            Path(name) if parent is None
            else Path(os.readlink(f"/proc/self/fd/{parent}")) / name
        )
        if candidate == victim:
            assert flags & (os.O_PATH | os.O_DIRECTORY)  # FIFO substitutes must never block.
        try:
            descriptor = real_open(name, flags, *args, **kwargs)
        except PermissionError:
            if candidate == victim:
                denied = True
            raise
        if candidate == victim and flags & os.O_PATH:
            assert denied
            assert not swapped
            swapped = True
            victim.rename(saved)
            if kind == "link":
                victim.symlink_to(outside)
            elif kind == "file":
                victim.write_bytes(b"substitute")
                victim.chmod(0o640)
            elif kind == "fifo":
                os.mkfifo(victim, 0o640)
            elif kind == "directory":
                victim.mkdir(mode=0o750)
                (victim / "keep").write_bytes(b"substitute")
        return descriptor

    monkeypatch.setattr(paths.os, "open", open_entry)
    fds = len(os.listdir("/proc/self/fd"))
    exception = FileNotFoundError if kind == "missing" else paths.PathContainmentError
    try:
        with pytest.raises(exception) as failed:
            if operation == "secure":
                paths.secure_root(victim, mode=0o700)
            elif operation == "ensure":
                paths.ensure_directory(root, PurePosixPath("one/two"), mode=0o700, secure=True)
            else:
                paths.tighten_directory(root, PurePosixPath(victim.relative_to(root)), mode=0o700)
        assert denied and swapped
        if isinstance(failed.value, paths.PathContainmentError):
            assert failed.value.path == victim
            assert failed.value.reason == (
                "symlink" if kind == "link" else "substituted" if kind == "directory"
                else "not a directory"
            )
        assert saved.stat().st_mode & 0o777 == 0o700
        assert (saved / "keep").read_bytes() == b"original"
        if kind in {"file", "fifo"}:
            assert victim.lstat().st_mode & 0o777 == 0o640
        if kind == "file":
            assert victim.read_bytes() == b"substitute"
        if kind == "directory":
            assert victim.stat().st_mode & 0o777 == 0o750
            assert (victim / "keep").read_bytes() == b"substitute"
        if kind == "link":
            assert victim.is_symlink()
        assert _snapshot(outside) == before
        assert len(os.listdir("/proc/self/fd")) == fds
    finally:
        if saved.exists():
            saved.chmod(0o700)
        elif victim.is_dir() and not victim.is_symlink():
            victim.chmod(0o700)


@pytest.mark.parametrize("operation", ["secure", "ensure", "tighten"])
@pytest.mark.parametrize("position", ["root", "child"])
@pytest.mark.parametrize("phase", ["pin", "pin-stat", "chmod", "missing-proc", "retry-stat"])
def test_permission_repair_io_failures_propagate_and_close_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, position: str, phase: str,
) -> None:
    root, _outside = _roots(tmp_path)
    victim = root if position == "root" else root / "child"
    if position == "child":
        victim.mkdir()
    victim.chmod(0o300)
    failure = OSError(errno.ENOENT if phase == "missing-proc" else errno.EIO, phase)
    real_root = paths.open_root
    real_child = paths._open_child_directory  # pyright: ignore[reportPrivateUsage]
    real_open, real_fstat, real_chmod = os.open, os.fstat, os.chmod
    attempts = 0
    pin: int | None = None

    def open_root(path: Path) -> int:
        nonlocal attempts
        if path == victim:
            attempts += 1
            if attempts == 1:
                raise PermissionError(errno.EACCES, "initial open")
        return real_root(path)

    def open_child(parent: int, name: str, *args: Any, **kwargs: Any) -> int | None:
        nonlocal attempts
        if name == victim.name:
            attempts += 1
            if attempts == 1:
                raise PermissionError(errno.EACCES, "initial open")
        return real_child(parent, name, *args, **kwargs)

    def open_entry(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal pin
        if flags & os.O_PATH and phase == "pin":
            raise failure
        descriptor = real_open(name, flags, *args, **kwargs)
        if flags & os.O_PATH:
            pin = descriptor
        return descriptor

    def fstat(descriptor: int, *args: Any, **kwargs: Any) -> os.stat_result:
        if (phase == "pin-stat" and descriptor == pin) or (
            phase == "retry-stat" and pin is not None and descriptor != pin
        ):
            raise failure
        return real_fstat(descriptor, *args, **kwargs)

    def chmod(name: Any, *args: Any, **kwargs: Any) -> None:
        assert str(name).startswith("/proc/self/fd/")
        if phase in {"chmod", "missing-proc"}:
            raise failure
        real_chmod(name, *args, **kwargs)

    monkeypatch.setattr(paths, "open_root", open_root)
    monkeypatch.setattr(paths, "_open_child_directory", open_child)
    monkeypatch.setattr(paths.os, "open", open_entry)
    monkeypatch.setattr(paths.os, "fstat", fstat)
    monkeypatch.setattr(paths.os, "chmod", chmod)
    relative = PurePosixPath() if position == "root" else PurePosixPath("child")
    fds = len(os.listdir("/proc/self/fd"))
    try:
        with pytest.raises(OSError) as failed:
            if operation == "secure":
                paths.secure_root(victim, mode=0o700)
            elif operation == "ensure":
                paths.ensure_directory(root, relative, mode=0o700, secure=True)
            else:
                paths.tighten_directory(root, relative, mode=0o700)
        _assert_os_error(failed.value, failure, victim)
        assert attempts == (2 if phase == "retry-stat" else 1)
        assert victim.stat().st_mode & 0o777 == (0o700 if phase == "retry-stat" else 0o300)
        assert len(os.listdir("/proc/self/fd")) == fds
    finally:
        real_chmod(victim, 0o700)


@pytest.mark.parametrize("field", ["device", "inode"])
def test_permission_repair_checks_both_identity_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    root, _outside = _roots(tmp_path)
    real_root, real_open, real_fstat = paths.open_root, os.open, os.fstat
    attempts = 0
    pin: int | None = None

    def open_root(path: Path) -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(errno.EACCES, "initial open")
        return real_root(path)

    def open_entry(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal pin
        descriptor = real_open(name, flags, *args, **kwargs)
        if flags & os.O_PATH:
            pin = descriptor
        return descriptor

    def fstat(descriptor: int, *args: Any, **kwargs: Any) -> os.stat_result:
        info = real_fstat(descriptor, *args, **kwargs)
        if pin is not None and descriptor != pin:
            fields = list(info)
            fields[2 if field == "device" else 1] += 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(paths, "open_root", open_root)
    monkeypatch.setattr(paths.os, "open", open_entry)
    monkeypatch.setattr(paths.os, "fstat", fstat)
    fds = len(os.listdir("/proc/self/fd"))
    with pytest.raises(paths.PathContainmentError) as failed:
        paths.secure_root(root, mode=0o700)
    assert failed.value.path == root
    assert failed.value.reason == "substituted"
    assert attempts == 2
    assert len(os.listdir("/proc/self/fd")) == fds


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory read permissions")
@pytest.mark.parametrize("operation", ["ensure", "tighten", "file"])
@pytest.mark.parametrize("position", ["root", "intermediate"])
def test_non_securing_walk_does_not_repair_unreadable_ancestors(
    tmp_path: Path, operation: str, position: str,
) -> None:
    root, _outside = _roots(tmp_path)
    (root / "one/two").mkdir(parents=True)
    victim = root if position == "root" else root / "one"
    victim.chmod(0o300)
    try:
        with pytest.raises(PermissionError):
            if operation == "ensure":
                paths.ensure_directory(root, PurePosixPath("one/two"), mode=0o700, secure=False)
            elif operation == "tighten":
                paths.tighten_directory(root, PurePosixPath("one/two"), mode=0o700)
            else:
                paths.ensure_empty_file(root, PurePosixPath("one/two/file"),
                                        directory_mode=0o700, file_mode=0o600)
        assert victim.stat().st_mode & 0o777 == 0o300
    finally:
        victim.chmod(0o700)


@pytest.mark.parametrize("phase", [
    "component-open", "mkdir", "placeholder-open", "placeholder-stat", "remove-stat",
    "unlink", "rmdir", "remove-list", "clear-list", "tree-mkdir", "tree-rename", "tree-stat",
])
def test_os_errors_name_full_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    root, _outside = _roots(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    target = root / "child"
    if phase in {"placeholder-stat", "remove-stat", "unlink"}:
        target.write_bytes(b"keep")
    elif phase not in {"mkdir", "placeholder-open", "tree-mkdir", "tree-rename", "tree-stat"}:
        target.mkdir()
    function = {
        "component-open": "open", "mkdir": "mkdir", "placeholder-open": "open",
        "placeholder-stat": "stat", "remove-stat": "stat", "unlink": "unlink",
        "rmdir": "rmdir", "remove-list": "listdir", "clear-list": "listdir",
        "tree-mkdir": "mkdir", "tree-rename": "replace", "tree-stat": "stat",
    }[phase]
    real_call = getattr(os, function)
    failure = OSError(errno.EIO, "injected I/O failure", "relative-or-temporary-name")
    expected = target
    calls = 0

    def fail(name: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal expected, calls
        parent = kwargs.get("dir_fd")
        candidate = (
            Path(os.readlink(f"/proc/self/fd/{name}")) if isinstance(name, int)
            else Path(os.readlink(f"/proc/self/fd/{parent}")) / name if parent is not None
            else Path(name)
        )
        if phase == "tree-rename":
            candidate = root / args[0]
        if candidate == target or (phase == "clear-list" and candidate == root) or (
            phase == "tree-mkdir" and candidate.name.startswith(".djinn-seed-")
        ):
            expected = candidate
            calls += 1
            raise failure
        return real_call(name, *args, **kwargs)

    fds = len(os.listdir("/proc/self/fd"))
    with monkeypatch.context() as patcher:
        patcher.setattr(paths.os, function, fail)
        with pytest.raises(OSError) as failed:
            if phase.startswith("placeholder"):
                paths.ensure_empty_file(root, PurePosixPath("child"), directory_mode=0o700,
                                        file_mode=0o600)
            elif phase.startswith("tree"):
                descriptor = paths.open_root(root)
                try:
                    paths.install_tree(descriptor, "child", source)
                finally:
                    os.close(descriptor)
            elif phase in {"remove-stat", "unlink", "rmdir", "remove-list", "clear-list"}:
                paths.clear_directory(root)
            else:
                paths.ensure_directory(root, PurePosixPath("child"), mode=0o700, secure=False)
        _assert_os_error(failed.value, failure, expected)
    assert calls >= 1
    assert len(os.listdir("/proc/self/fd")) == fds


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory read permissions")
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_root_refusal_under_search_only_parent(tmp_path: Path, kind: str) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"outside")
    root = parent / "root"
    if kind == "symlink":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.write_bytes(b"wrong type")
    before = _snapshot(outside)
    parent.chmod(0o300)
    try:
        for operation in (paths.open_root, paths.clear_directory):
            with pytest.raises(paths.PathContainmentError) as refused:
                operation(root)
            assert refused.value.path == root
            assert refused.value.reason == ("symlink" if kind == "symlink" else "not a directory")
        assert _snapshot(outside) == before
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize("phase", ["stat", "unlink", "open", "rmdir", "reopen"])
@pytest.mark.parametrize("nested", [False, True])
def test_vanished_entries_are_removed_and_later_siblings_visited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, nested: bool,
) -> None:
    root, _outside = _roots(tmp_path)
    parent = root / "a" if nested else root
    parent.mkdir(exist_ok=True)
    victim = parent / "b"
    if phase in {"open", "rmdir", "reopen"}:
        victim.mkdir()
    else:
        victim.write_bytes(b"gone soon")
    (parent / "c").write_bytes(b"later child")
    (root / "z").write_bytes(b"later sibling")
    real_call = getattr(os, "rmdir" if phase == "reopen" else phase)
    real_rmdir = os.rmdir
    real_unlink = os.unlink
    vanished = False

    def vanish(name: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal vanished
        parent_fd = kwargs.get("dir_fd")
        candidate = (Path(name) if parent_fd is None
                     else Path(os.readlink(f"/proc/self/fd/{parent_fd}")) / name)
        if candidate == victim and not vanished:
            vanished = True
            if phase in {"open", "rmdir", "reopen"}:
                real_rmdir(name, dir_fd=parent_fd)
            else:
                real_unlink(name, dir_fd=parent_fd)
            if phase == "reopen":
                raise PermissionError(errno.EACCES, "vanished after rmdir refusal")
        return real_call(name, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(paths.os, "rmdir" if phase == "reopen" else phase, vanish)
        paths.clear_directory(root)
    assert vanished
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("kind", ["refusal", "io", "interrupt"])
def test_tree_cleanup_failure_keeps_primary_exception_and_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    root, _outside = _roots(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    primary = (paths.PathContainmentError(root / "tree", "substituted") if kind == "refusal"
               else OSError(errno.EIO, "population failed") if kind == "io"
               else KeyboardInterrupt("cancelled"))
    cleanup = PermissionError(errno.EACCES, "cleanup denied")
    real_remove = paths.remove_entry

    def populate(*_args: Any, **_kwargs: Any) -> None:
        raise primary

    def remove(*_args: Any, **_kwargs: Any) -> bool:
        raise cleanup

    descriptor = paths.open_root(root)
    fds = len(os.listdir("/proc/self/fd"))
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(paths, "_populate", populate)
            patcher.setattr(paths, "remove_entry", remove)
            with pytest.raises(type(primary)) as failed:
                paths.install_tree(descriptor, "tree", source)
        assert failed.value is primary
        assert len(primary.__notes__) == 1
        assert "Could not remove temporary seed tree" in primary.__notes__[0]
        assert "cleanup denied" in primary.__notes__[0]
        assert len(os.listdir("/proc/self/fd")) == fds
        for name in os.listdir(descriptor):
            real_remove(descriptor, name)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory read permissions")
@pytest.mark.parametrize("position", ["root", "child"])
def test_mode_zero_repair_error_names_component_instead_of_proc_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: str,
) -> None:
    root, _outside = _roots(tmp_path)
    victim = root if position == "root" else root / "child"
    victim.mkdir(exist_ok=True)
    victim.chmod(0o000)
    real_chmod = os.chmod
    failure: PermissionError | None = None

    def chmod(name: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal failure
        if str(name).startswith("/proc/self/fd/"):
            assert os.readlink(name) == str(victim)
            failure = PermissionError(errno.EPERM, "Operation not permitted", str(name))
            raise failure
        real_chmod(name, *args, **kwargs)

    monkeypatch.setattr(paths.os, "chmod", chmod)
    try:
        with pytest.raises(PermissionError) as failed:
            if position == "root":
                paths.secure_root(root, mode=0o700)
            else:
                paths.ensure_directory(root, PurePosixPath("child"), mode=0o700, secure=True)
        assert failure is not None
        _assert_os_error(failed.value, failure, victim)
        assert "/proc/self/fd/" not in str(failed.value)
    finally:
        real_chmod(victim, 0o700)
