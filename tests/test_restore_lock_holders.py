"""Real inherited flocks retain exclusion and identify their surviving fd owners."""

from __future__ import annotations

import ctypes
import fcntl
import itertools
import os
import re
import select
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from djinn_in_a_box.commands import doctor
from djinn_in_a_box.core import assistant, docker, host_runtime, hostctl
from djinn_in_a_box.core import opencode_credentials as oc
from djinn_in_a_box.core.lock_holders import describe_holders
from djinn_in_a_box.core.ssh_delivery import GitSSHError


def fast_clock(monkeypatch, module):
    ticks = itertools.count(step=30)
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(
            monotonic=lambda: float(next(ticks)),
            sleep=lambda _: None,
        ),
    )


@contextmanager
def flock_holder(path, *, shared=False):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import ctypes, fcntl, os, sys",
                    "ctypes.CDLL(None).prctl(15, b'restore-writer', 0, 0, 0)",
                    "fd = os.open(sys.argv[1], os.O_RDONLY)",
                    "fcntl.flock(fd, int(sys.argv[2]))",
                    "print('ready', flush=True)",
                    "sys.stdin.read()",
                ]
            ),
            str(path),
            str(fcntl.LOCK_SH if shared else fcntl.LOCK_EX),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([process.stdout], [], [], 10)[0], "holder did not start"
        assert process.stdout.readline().strip() == "ready"
        yield process
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()


@pytest.mark.parametrize("kind", ["control", "creation"])
def test_held_descriptor_is_guarded_and_thread_local(kind, monkeypatch):
    if kind == "control":
        guard, getter, error = (
            hostctl.control_guard,
            hostctl.held_control_descriptor,
            hostctl.HostctlError,
        )
        path = hostctl.state_root() / "control.lock"
    else:
        guard, getter, error = (
            host_runtime.creation_guard,
            host_runtime.held_creation_descriptor,
            GitSSHError,
        )
        path = host_runtime.runtime_root(create=True) / "creator.lock"

    def outside():
        with monkeypatch.context() as patch:

            def forbidden(*args, **kwargs):
                pytest.fail("descriptor reads must never open or lock a file")

            patch.setattr(os, "open", forbidden)
            patch.setattr(fcntl, "flock", forbidden)
            with pytest.raises(error, match="no .*lock is held"):
                getter()

    outside()
    with guard():
        descriptor = getter()
        assert os.path.samestat(os.fstat(descriptor), path.stat())
        if kind == "control":
            with guard():
                assert getter() == descriptor
            assert getter() == descriptor
        errors = []

        def other_thread():
            try:
                getter()
            except error:
                errors.append(True)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(timeout=5)
        assert errors == [True]
    outside()
    with pytest.raises(RuntimeError, match="guard body"), guard():
        raise RuntimeError("guard body")
    outside()


@pytest.mark.parametrize("shared", [False, True])
def test_describe_real_holders(tmp_path, shared):
    path = tmp_path / "lock"
    path.touch()
    assert describe_holders(path) is None
    with flock_holder(path, shared=shared) as child:
        text = describe_holders(path)
        assert text is not None
        match = re.search(
            rf"held by PID {child.pid} \(restore-writer\) since "
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
            text,
        )
        assert match, text
        assert (
            abs(
                (
                    datetime.now()
                    - datetime.strptime(
                        match[1],
                        "%Y-%m-%d %H:%M:%S",
                    )
                ).total_seconds()
            )
            < 30
        )
        assert f"stop exactly that process (kill {child.pid}), then retry" in text
        assert (describe_holders(path, exclusive_only=True) is None) == shared
    assert describe_holders(path) is None


def test_describe_ignores_unlocked_fds_and_sanitizes_comm(tmp_path, monkeypatch):
    path = tmp_path / "lock"
    path.touch()
    with path.open():
        assert describe_holders(path) is None
    original = Path.read_text

    def read(path, *args, **kwargs):
        assert path.name != "cmdline"
        if path.name == "comm":
            return "writer\x1b\nname\n"
        return original(path, *args, **kwargs)

    with flock_holder(path):
        monkeypatch.setattr(Path, "read_text", read)
        assert "(writer??name)" in describe_holders(path)
    assert describe_holders(tmp_path / "missing") is None


@pytest.mark.parametrize("kind", ["control", "creation"])
def test_busy_refusal_without_visible_holder_preserves_text(monkeypatch, kind):
    module = hostctl if kind == "control" else host_runtime
    monkeypatch.setattr(module, "describe_holders", lambda *_: None)

    def busy(*args):
        raise BlockingIOError

    monkeypatch.setattr(fcntl, "flock", busy)
    fast_clock(monkeypatch, module)
    if kind == "control":
        with pytest.raises(hostctl.HostctlError) as caught, hostctl.control_guard():
            pytest.fail("busy guard entered")
        assert str(caught.value) == ("host control is busy; retry after the transition completes")
    else:
        with pytest.raises(GitSSHError) as caught:
            host_runtime.acquire_creation_lock(host_runtime.runtime_root(create=True))
        assert str(caught.value) == (
            "another dev container creation is pending; retry after it completes"
        )


def test_orphan_writer_holds_all_restore_locks(git_inputs, monkeypatch):
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # Adopt the writer so this test can wait for it.
    with oc.create_audit_session() as session:
        identity = session.id
    writer_code = (
        "import ctypes, sys; "
        "ctypes.CDLL(None).prctl(15, b'restore-writer', 0, 0, 0); "
        "print('ready', flush=True); sys.stdin.read()"
    )
    parent_code = "\n".join(
        [
            "import os, pwd, signal, subprocess, sys",
            "from types import SimpleNamespace",
            "from djinn_in_a_box.core import hostctl as hc, host_runtime as hr",
            "from djinn_in_a_box.core import opencode_credentials as oc",
            "home = os.environ['DJINN_TEST_PASSWD_HOME']",
            "pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=home)",
            "with hc.control_guard(), hr.creation_guard(), oc.restore_gate() as gate:",
            " fds = (hc.held_control_descriptor(), hr.held_creation_descriptor(), gate)",
            " child = subprocess.Popen([sys.executable, '-c', sys.argv[1]],",
            "     pass_fds=fds, stdin=sys.stdin, stdout=subprocess.PIPE, text=True)",
            " assert child.stdout.readline().strip() == 'ready'",
            " print(child.pid, flush=True)",
            " signal.pause()",
        ]
    )
    parent = None
    writer_pid = None
    try:
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code, writer_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert select.select([parent.stdout], [], [], 15)[0], "parent did not start"
        writer_pid = int(parent.stdout.readline())
        parent.kill()
        parent.wait(timeout=10)
        assert parent.returncode == -signal.SIGKILL
        runtime = (
            Path(os.environ["DJINN_TEST_PASSWD_HOME"]) / ".local/state/djinn/runtime/git-agent"
        )
        for path in (
            hostctl.state_root(create=False) / "control.lock",
            runtime / "creator.lock",
            Path(oc.default_audit_root()),
        ):
            text = describe_holders(path, exclusive_only=True)
            assert f"PID {writer_pid} (restore-writer)" in text
            assert f"PID {parent.pid} " not in text
        fast_clock(monkeypatch, hostctl)
        with (
            pytest.raises(hostctl.HostctlError, match="host control is busy") as caught,
            hostctl.control_guard(),
        ):
            pytest.fail("orphan lost the control lock")
        assert f"PID {writer_pid} (restore-writer)" in str(caught.value)
        with pytest.raises(GitSSHError) as caught:
            host_runtime.acquire_creation_lock(runtime)
        assert f"PID {writer_pid} (restore-writer)" in str(caught.value)
        report = oc.recover_audit_sessions(
            None,
            git_inputs.config_root / "opencode",
            lambda _: b"[]",
            lambda _: False,
        )
        assert report.kept == [(identity, "restore in progress")]
        parent.stdin.close()
        with os.fdopen(os.pidfd_open(writer_pid)) as pidfd:
            assert select.select([pidfd], [], [], 10)[0], "writer did not finish"
        assert os.waitpid(writer_pid, 0)[1] == 0
        writer_pid = None
        with hostctl.control_guard(), host_runtime.creation_guard(runtime), oc.restore_gate():
            pass
    finally:
        if parent is not None:
            parent.stdin.close()
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=10)
            parent.stdout.close()
        if writer_pid is not None:
            os.waitpid(writer_pid, 0)
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


@pytest.mark.parametrize("name", ["control", "creation", "gate"])
def test_doctor_reports_lifecycle_holder(monkeypatch, mock_home, name):
    paths = {
        "control": hostctl.state_root() / "control.lock",
        "creation": host_runtime.runtime_root(create=True) / "creator.lock",
        "gate": mock_home / "audit",
    }
    paths["gate"].mkdir()
    monkeypatch.setattr(oc, "default_audit_root", lambda: str(paths["gate"]))
    paths["control"].touch()
    paths["creation"].touch()
    with flock_holder(paths[name]) as child:
        monkeypatch.setattr(doctor, "_docker_installed", lambda: False)
        monkeypatch.setattr(doctor, "_command_ok", lambda _: False)
        monkeypatch.setattr(
            doctor.docker_core,
            "inspect_running_desktop",
            lambda: SimpleNamespace(
                channels=(),
                raw_sources=(),
                raw_verified=False,
            ),
        )
        rows = doctor.run_checks(None)
        row = next(row for row in rows if row.name == "Lifecycle locks")
        assert row.status is doctor.Status.WARN
        assert f"PID {child.pid} (restore-writer) since" in row.detail
        assert f"kill {child.pid}" in row.detail


@pytest.mark.parametrize("missing", [False, True])
def test_doctor_no_lifecycle_holders_creates_nothing(tmp_path, monkeypatch, missing):
    root = tmp_path / "diagnostic-only"
    monkeypatch.setattr(hostctl, "state_root", lambda **kwargs: root / "control")
    monkeypatch.setattr(host_runtime, "runtime_root", lambda **kwargs: root / "creation")
    monkeypatch.setattr(oc, "default_audit_root", lambda: str(root / "audit"))
    if not missing:
        (root / "control").mkdir(parents=True)
        (root / "control/control.lock").touch()
        (root / "creation").mkdir()
        (root / "creation/creator.lock").touch()
        (root / "audit").mkdir()
    before = sorted(root.rglob("*"))
    row = doctor.lifecycle_locks_check()
    assert row.status is doctor.Status.PASS
    assert sorted(root.rglob("*")) == before
    assert root.exists() == (not missing)


def test_doctor_lifecycle_check_contains_an_unusable_state_root(monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    row = doctor.lifecycle_locks_check()
    assert row.status is doctor.Status.WARN
    assert "XDG_STATE_HOME must be an absolute host path" in row.detail


def test_assistant_reports_gate_holder_once(mock_home, mock_app_config, monkeypatch):
    monkeypatch.setattr(oc, "default_audit_root", lambda: str(mock_home / "audit"))
    for _ in range(2):
        with oc.create_audit_session():
            pass
    warnings = []
    monkeypatch.setattr(assistant, "warning", warnings.append)
    with flock_holder(Path(oc.default_audit_root())) as child:
        assistant.recover_leftover_sessions(mock_home, mock_app_config)
    assert len([line for line in warnings if "kept: restore in progress" in line]) == 2
    holders = [line for line in warnings if f"PID {child.pid} (restore-writer) since" in line]
    assert len(holders) == 1
    assert f"kill {child.pid}" in holders[0]


def test_restore_volume_passes_held_descriptors(tmp_path, monkeypatch):
    (tmp_path / "djinn-example.tar.gz").touch()
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs)
        return docker.RunResult(0)

    monkeypatch.setattr(docker, "_run_captured", run)
    with hostctl.control_guard(), host_runtime.creation_guard():
        inherited = (hostctl.held_control_descriptor(), host_runtime.held_creation_descriptor())
        assert docker.restore_volume("djinn-example", tmp_path, inherit_fds=inherited).success
        assert calls == [{"pass_fds": inherited}]
