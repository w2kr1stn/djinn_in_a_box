from __future__ import annotations

import errno
import fcntl
import multiprocessing
import os
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from djinn_in_a_box.core import config_lock
from djinn_in_a_box.core.config_lock import ConfigDirectoryLockError, config_directory_lock


def _probe_nonblocking_lock(
    config_dir: Path, *, exclusive: bool, result: Connection
) -> None:
    """Report whether this process can acquire the directory lock immediately."""
    descriptor = os.open(config_dir, os.O_RDONLY | os.O_DIRECTORY)
    acquired = False
    try:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            result.send(False)
        else:
            acquired = True
            result.send(True)
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        result.close()


def _can_acquire_nonblocking_lock(config_dir: Path, *, exclusive: bool) -> bool:
    context = multiprocessing.get_context("spawn")
    received, sent = context.Pipe(duplex=False)
    probe = context.Process(
        target=_probe_nonblocking_lock,
        kwargs={"config_dir": config_dir, "exclusive": exclusive, "result": sent},
    )
    probe.start()
    sent.close()
    try:
        probe.join()
        assert probe.exitcode == 0
        return received.recv()
    finally:
        received.close()
        if probe.is_alive():
            probe.terminate()
        probe.join()
        probe.close()


def test_config_directory_lock_creates_no_artifact(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with config_directory_lock(config_dir, exclusive=False) as descriptor:
        assert descriptor >= 0
    assert list(config_dir.iterdir()) == []
    with config_directory_lock(config_dir, exclusive=True) as descriptor:
        assert descriptor >= 0
        assert list(config_dir.iterdir()) == []


def test_config_directory_lock_excludes_second_exclusive_holder(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with config_directory_lock(config_dir, exclusive=True):
        assert not _can_acquire_nonblocking_lock(config_dir, exclusive=True)
        assert not _can_acquire_nonblocking_lock(config_dir, exclusive=False)


def test_config_directory_lock_allows_second_shared_holder(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with config_directory_lock(config_dir, exclusive=False):
        assert _can_acquire_nonblocking_lock(config_dir, exclusive=False)


def test_config_directory_lock_wraps_acquisition_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    def fail_acquisition(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(config_lock.fcntl, "flock", fail_acquisition)

    with pytest.raises(ConfigDirectoryLockError) as exc_info, config_directory_lock(
        config_dir, exclusive=True
    ):
        pass

    assert str(config_dir) in str(exc_info.value)
    assert "No locks available" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.__cause__.errno == errno.ENOLCK


def test_config_directory_lock_wraps_unlock_failure_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    original_open = config_lock.os.open
    original_flock = config_lock.fcntl.flock
    descriptor: int | None = None

    def record_descriptor(path: Path, flags: int) -> int:
        nonlocal descriptor
        descriptor = original_open(path, flags)
        return descriptor

    def fail_unlock(candidate: int, operation: int) -> None:
        if candidate == descriptor and operation == config_lock.fcntl.LOCK_UN:
            raise OSError(errno.EINTR, "Interrupted system call")
        original_flock(candidate, operation)

    monkeypatch.setattr(config_lock.os, "open", record_descriptor)
    monkeypatch.setattr(config_lock.fcntl, "flock", fail_unlock)

    with pytest.raises(ConfigDirectoryLockError) as exc_info, config_directory_lock(
        config_dir, exclusive=True
    ):
        pass

    assert descriptor is not None
    with pytest.raises(OSError) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF
    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.__cause__.errno == errno.EINTR
