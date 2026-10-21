"""ID-bound closure and journal contention without a Docker daemon."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from djinn_in_a_box.commands.doctor import Status, hostctl_checks
from djinn_in_a_box.core import host_runtime, hostctl

REAL_INSPECT_OBJECT = host_runtime.inspect_object
REAL_POPEN = subprocess.Popen
REAL_SLEEP = time.sleep
REAL_MONOTONIC = time.monotonic
HOLDER = """import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print('held', flush=True)
time.sleep(float(sys.argv[2]))
"""


def hold(path, seconds):
    child = REAL_POPEN(
        [sys.executable, "-c", HOLDER, str(path), str(seconds)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout.readline().strip() == "held"
    return child


HOLDER_UNTIL_EOF = """import fcntl, os, sys, threading
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
timer = threading.Timer(float(sys.argv[2]), lambda: os._exit(0))
timer.daemon = True
timer.start()
print('held', flush=True)
sys.stdin.read()
"""


def hold_until_eof(path, seconds):
    child = REAL_POPEN(
        [sys.executable, "-c", HOLDER_UNTIL_EOF, str(path), str(seconds)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout.readline().strip() == "held"
    return child


def release_holder(holder):
    holder.stdin.close()
    holder.wait(timeout=3)


def assert_control_lock_held(path):
    descriptor = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


@pytest.fixture
def window(tmp_path, monkeypatch):
    value = {
        "Id": "helper-a", "State": {"Running": True},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "generation-a"}},
    }
    root = hostctl.state_root()
    hostctl.save_private("opening.json", {"generation": "generation-a", "dev_id": None})
    state = SimpleNamespace(
        helper=value, root=root, stops=[], probes=[], signals={}, admitted=False, commands=[],
        clock=0.0, sleeps=[], strict=0, inspections=0, journal_waits=[],
    )

    def inspect(*, strict=False):
        state.inspections += 1
        state.strict += strict
        state.probes.append(strict)
        return state.helper

    def command(*args, **kwargs):
        state.commands.append(args)
        if args[0] == "logs":
            assert args == ("logs", "--tail", "100", "helper-a") and not kwargs
            return ""
        assert args == ("stop", "-t", "3", "helper-a")
        assert kwargs == {"timeout": 8}
        state.stops.append(args[-1])
        state.helper["State"]["Running"] = False
        return ""

    def sleep(seconds):
        state.clock += seconds
        state.sleeps.append(seconds)
        if state.clock > 180:
            pytest.fail("closure exceeded test's safety bound")

    def admit(*args, **kwargs):
        state.admitted = True

    journal = hostctl.journal

    def journaled(event, **kwargs):
        state.journal_waits.append(kwargs.get("wait"))
        return journal(event, **kwargs)

    monkeypatch.setattr(hostctl, "inspect_helper", inspect)
    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(hostctl, "node_status", lambda *a: {"BackendState": "Running"})
    monkeypatch.setattr(hostctl, "prepare_trust", lambda *a: {})
    monkeypatch.setattr(hostctl, "admission_assessment", lambda *a: {
        "creator": None, "dev_id": None, "sealing": "sealed",
    })
    monkeypatch.setattr(hostctl, "commit_assessment", lambda *a: None)
    monkeypatch.setattr(hostctl, "admit_locked", admit)
    monkeypatch.setattr(hostctl, "verify_agent", lambda *a: None)
    monkeypatch.setattr(hostctl, "journal", journaled)
    monkeypatch.setattr(host_runtime.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(
        poll=lambda: 0, returncode=0,
    ))
    monkeypatch.setattr(host_runtime.signal, "signal", lambda sig, handler:
                        state.signals.__setitem__(sig, handler))
    monkeypatch.setattr(host_runtime.time, "monotonic", lambda: state.clock)
    monkeypatch.setattr(host_runtime.time, "sleep", sleep)

    def gap(*args):
        raise host_runtime.GitSSHError("inspection unknown")

    monkeypatch.setattr(host_runtime, "inspect_dev", gap)
    state.inspect = inspect
    state.command = command
    state.journal = journaled
    state.sleep = sleep
    state.run = lambda: host_runtime.observe_hostctl(
        "helper-a", "generation-a", "/isolated/docker", tmp_path / "records",
    )
    state.rows = lambda: [json.loads(line) for line in
                          (root / "journal.jsonl").read_text().splitlines()]
    return state


@pytest.mark.parametrize("path", [
    "gap", "external", "refused", "unexpected", "pre-admission", "partial-admission",
    "SIGTERM", "SIGINT", "SIGHUP", "two-signals",
])
def test_every_exit_closes_under_long_control_hold(window, monkeypatch, path):
    """H2: prove closure while the 31 s holder still owns the lock, then release it."""
    holder = None
    closed_at = None
    started = REAL_MONOTONIC()

    def start_holder():
        nonlocal holder
        holder = hold_until_eof(window.root / "control.lock", 31)
        # Actual lock lifetime is real; closure's bound/backoff uses a controlled clock.
        window.clock = 0

    def tick(*args):
        if holder is None:
            return ("dev-a", True, "") if path == "external" else None
        if path in ("SIGTERM", "SIGINT", "SIGHUP", "two-signals"):
            return None
        if path == "external":
            return ("foreign-dev", True, "foreign-generation")
        if path in ("unexpected", "pre-admission"):
            raise KeyError("defect")
        raise host_runtime.GitSSHError("inspection unknown")

    def next_round(seconds):
        window.sleep(seconds)
        if holder is None:
            start_holder()

    def assess(*args):
        if holder is not None and path == "refused":
            raise hostctl.HostctlError("refused")
        return {"creator": None, "dev_id": None, "sealing": "sealed"}

    def verify(*args):
        if holder is not None and path in ("SIGTERM", "SIGINT", "SIGHUP", "two-signals"):
            handler = window.signals[getattr(signal, path) if path != "two-signals"
                                     else signal.SIGTERM]
            try:
                handler(signal.SIGTERM, None)
            finally:
                assert all(window.signals[s] == signal.SIG_IGN for s in
                           (signal.SIGTERM, signal.SIGINT, signal.SIGHUP))
                # A second delivery is ignored before KeyboardInterrupt reaches closure.
                if path == "two-signals":
                    assert window.signals[signal.SIGHUP] == signal.SIG_IGN

    def inspect(*, strict=False):
        if strict:
            assert all(window.signals[s] == signal.SIG_IGN for s in
                       (signal.SIGTERM, signal.SIGINT, signal.SIGHUP))
        return window.inspect(strict=strict)

    monkeypatch.setattr(host_runtime, "inspect_dev", tick)
    monkeypatch.setattr(host_runtime.time, "sleep", next_round)
    monkeypatch.setattr(hostctl, "verify_agent", verify)
    monkeypatch.setattr(hostctl, "inspect_helper", inspect)
    if path == "external":
        monkeypatch.setattr(hostctl, "admission_assessment", lambda *a: {
            "creator": None, "dev_id": "dev-a", "sealing": "sealed",
        })
    if path == "refused":
        # Remain in opening until the holder has started; refusal happens before another guard.
        monkeypatch.setattr(hostctl, "admission_assessment", lambda *a: None if holder is None
                            else assess())
    if path in ("pre-admission", "partial-admission"):
        guard = hostctl.control_guard

        @contextlib.contextmanager
        def released(*, timeout=15):
            try:
                with guard(timeout=timeout):
                    yield
            finally:
                if holder is None:
                    start_holder()

        if path == "partial-admission":
            monkeypatch.setattr(hostctl, "control_guard", released)

        def partial(*args, **kwargs):
            if path == "partial-admission":
                raise KeyboardInterrupt
            start_holder()
            raise KeyError("before admission")
        monkeypatch.setattr(hostctl, "prepare_trust" if path == "pre-admission"
                            else "admit_locked", partial)
    try:
        if path in ("unexpected", "pre-admission"):
            with pytest.raises(KeyError):
                window.run()
        else:
            window.run()
        closed_at = REAL_MONOTONIC()
        assert_control_lock_held(window.root / "control.lock")
        assert window.stops == ["helper-a"]
        assert_control_lock_held(window.root / "control.lock")
        assert not window.helper["State"]["Running"]
        rows = window.rows()
        assert_control_lock_held(window.root / "control.lock")
        assert rows[-1]["event"] == "relay-closed-unlocked"
        assert_control_lock_held(window.root / "control.lock")
        assert rows[-1]["helper_id"] == "helper-a"
        assert_control_lock_held(window.root / "control.lock")
        assert window.strict >= 2
        assert_control_lock_held(window.root / "control.lock")
        assert 2 <= window.clock < 3
        assert_control_lock_held(window.root / "control.lock")
        assert set(window.journal_waits) == {0.5}
        assert_control_lock_held(window.root / "control.lock")
        assert closed_at is not None and closed_at - started < 5
    finally:
        # Popen.wait itself uses time.sleep; restore wall time for holder reaping.
        monkeypatch.setattr(host_runtime.time, "sleep", REAL_SLEEP)
        if holder is not None:
            release_holder(holder)


def test_publication_skips_keep_checks_and_catch_up(window, monkeypatch):
    """H1: real >30 s contention on both locks never interrupts checks."""
    holders = []
    checks = []
    companions = []
    publications = []
    deadline = REAL_MONOTONIC() + 40
    monkeypatch.setattr(host_runtime.time, "monotonic", REAL_MONOTONIC)

    def tick(*args):
        checks.append(REAL_MONOTONIC())
        return None

    def sleep(seconds):
        if not holders:
            publications.append((window.root / "observation.json").read_text())
            holders.extend(hold(window.root / name, 31) for name in
                           ("control.lock", "journal.lock"))
        elif all(p.poll() is not None for p in holders):
            # One complete round after release must publish the fresh observation.
            current = (window.root / "observation.json").read_text()
            if current != publications[0]:
                publications.append(current)
                raise KeyboardInterrupt
        assert REAL_MONOTONIC() < deadline
        REAL_SLEEP(seconds)

    monkeypatch.setattr(host_runtime, "inspect_dev", tick)
    monkeypatch.setattr(hostctl, "verify_agent", lambda *a: companions.append(REAL_MONOTONIC()))
    monkeypatch.setattr(host_runtime.time, "sleep", sleep)
    try:
        window.run()
    finally:
        for holder in holders:
            holder.wait(timeout=35)
    assert len(checks) >= 25 and len(companions) == len(checks)
    assert len(publications) == 2
    assert window.stops == ["helper-a"]  # Only cancellation closes it after catch-up.
    assert [r["event"] for r in window.rows()] == ["observation-skipped", "relay-closed"]


@pytest.mark.parametrize("foreign", ["id", "generation", "absent", "stopped"])
def test_final_probe_never_stops_a_foreign_helper(window, monkeypatch, foreign):
    def inspect(*, strict=False):
        if strict:
            if foreign == "absent":
                return None
            if foreign == "stopped":
                window.helper["State"]["Running"] = False
            elif foreign == "id":
                window.helper["Id"] = "helper-b"
            else:
                window.helper["Config"]["Labels"][hostctl.GENERATION_LABEL] = "generation-b"
        return window.inspect(strict=strict)
    monkeypatch.setattr(hostctl, "inspect_helper", inspect)
    window.run()
    assert not window.stops
    assert window.rows()[-1]["event"] == "relay-closed"


def test_stop_retry_backoff_journal_failures_and_throttle(window, monkeypatch, capsys):
    calls = []
    failures = 18

    def command(*args, **kwargs):
        if args[0] == "logs":
            return window.command(*args, **kwargs)
        calls.append(window.clock)
        if len(calls) <= failures:
            raise hostctl.HostctlError("Docker stop failed (exit 1)")
        return window.command(*args, **kwargs)

    writes = []

    def journal(event, **kwargs):
        writes.append(window.clock)
        assert kwargs["wait"] == 0.5
        if window.clock < 2:
            raise OSError("full")
        return window.journal(event, **kwargs)

    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(hostctl, "journal", journal)
    window.run()
    rows = window.rows()
    assert len(calls) == failures + 1
    assert window.sleeps[:5] == [0.5, 1, 2, 4, 5]
    assert max(window.sleeps) == 5
    assert [r["attempt"] for r in rows[:-1]] == list(range(1, failures + 1))
    assert all(r["reason"] == "stop-unverified" for r in rows[:-1])
    assert rows[-1]["event"] == "relay-closed"
    assert len(writes) > failures
    assert capsys.readouterr().err.count("closure pending;") == 2


@pytest.mark.parametrize("probe", ["initial", "after-stop", "failed-stop"])
def test_positive_absence_only_and_all_closure_probes_strict(window, monkeypatch, probe):
    strict_calls = 0
    commands = 0

    def inspect(*, strict=False):
        nonlocal strict_calls
        if strict:
            strict_calls += 1
        # Broad absence would return None for a missing daemon socket.
        if ((probe == "initial" and strict_calls == 1)
                or (probe != "initial" and commands == 1)):
            if not strict:
                return None
            if probe == "initial" or strict_calls == 2:
                raise host_runtime.GitSSHError(
                    "Cannot connect to the Docker daemon: connect: no such file or directory"
                )
        return window.inspect(strict=strict)

    def command(*args, **kwargs):
        nonlocal commands
        commands += 1
        if probe == "failed-stop" and commands == 1:
            raise hostctl.HostctlError("Docker stop failed (exit 1)")
        return window.command(*args, **kwargs)

    monkeypatch.setattr(hostctl, "inspect_helper", inspect)
    monkeypatch.setattr(hostctl, "command", command)
    window.run()
    assert window.rows()[0]["event"] == "relay-close-failed"
    assert window.rows()[0]["reason"] == (
        "inspection-unknown" if probe == "initial" else "stop-unverified"
    )
    assert strict_calls >= 3
    assert not window.helper["State"]["Running"]


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("after", [None, "foreign", "foreign-generation", "running", "stopped"])
def test_shared_stop_verification_contract(window, monkeypatch, strict, after):
    before = copy.deepcopy(window.helper)
    after_value = copy.deepcopy(before)
    if after == "foreign":
        after_value["Id"] = "helper-b"
    elif after == "foreign-generation":
        after_value["Config"]["Labels"][hostctl.GENERATION_LABEL] = "generation-b"
    elif after == "stopped":
        after_value["State"]["Running"] = False
    inspected = []
    stopped = False

    def inspect(**kwargs):
        inspected.append(kwargs)
        return (None if after is None else after_value) if stopped else before

    def command(*args, **kwargs):
        nonlocal stopped
        assert args[-1] == "helper-a" and kwargs["timeout"] == 8
        stopped = True
        return ""

    monkeypatch.setattr(hostctl, "inspect_helper", inspect)
    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(hostctl, "reconcile", lambda *a: (_ for _ in ()).throw(
        hostctl.HostctlError("Docker logs failed (exit 1)"),
    ))
    if strict:
        if after == "running":
            with pytest.raises(hostctl.HostctlError, match="termination"):
                hostctl.stop_and_verify("helper-a", "generation-a", strict=True)
        else:
            outcome = hostctl.stop_and_verify("helper-a", "generation-a", strict=True)
            assert outcome is (after_value if after == "stopped" else None)
        assert inspected == [{"strict": True}]
    else:
        with pytest.raises(hostctl.HostctlError, match=(
            "changed during teardown" if after in (None, "foreign", "foreign-generation") else
            "termination" if after == "running" else "logs failed"
        )):
            hostctl.stop_helper_locked()
        assert inspected == [{}, {}]


def test_guarded_closure_ignores_post_stop_diagnostic_failure(window, monkeypatch):
    monkeypatch.setattr(hostctl, "reconcile", lambda *a: (_ for _ in ()).throw(
        hostctl.HostctlError("Docker logs failed (exit 1)"),
    ))
    window.run()
    assert window.stops == ["helper-a"]
    assert [r["event"] for r in window.rows()] == ["relay-closed"]


def test_publication_only_skips_busy(window, monkeypatch):
    guard = hostctl.control_guard
    calls = 0
    monkeypatch.setattr(host_runtime, "inspect_dev", lambda *a: None)

    @contextlib.contextmanager
    def permission_error(*, timeout=15):
        nonlocal calls
        calls += 1
        if calls == 2:  # Admission succeeded; publication fails for another reason.
            assert timeout == 0
            raise hostctl.HostctlError("state ownership invalid")
        with guard(timeout=timeout):
            yield

    monkeypatch.setattr(hostctl, "control_guard", permission_error)
    window.run()
    assert window.stops == ["helper-a"]
    assert [r["event"] for r in window.rows()] == ["relay-closed"]
    assert calls == 3


def test_bounded_closure_publication_deadline(window, monkeypatch, capsys):
    attempts = []

    def journal(*a, **kwargs):
        attempts.append(window.clock)
        assert kwargs["wait"] <= 0.5
        return False

    monkeypatch.setattr(hostctl, "journal", journal)
    window.run()
    assert not window.helper["State"]["Running"]
    assert window.clock == 60
    assert len(attempts) < 20
    assert "closure journal publication timed out" in capsys.readouterr().err


def test_unlocked_retry_reacquires_guard(window, monkeypatch):
    guard = hostctl.control_guard
    first = True

    @contextlib.contextmanager
    def busy(*, timeout=15):
        if window.admitted and first:
            raise hostctl.HostctlBusy("busy")
        with guard(timeout=timeout):
            yield

    def command(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            assert window.clock == pytest.approx(2)
            raise hostctl.HostctlError("Docker stop failed (exit 1)")
        return window.command(*args, **kwargs)

    monkeypatch.setattr(hostctl, "control_guard", busy)
    monkeypatch.setattr(hostctl, "command", command)
    window.run()
    assert [r["event"] for r in window.rows()] == ["relay-close-failed", "relay-closed"]
    assert window.sleeps[-1] == 0.5


def test_journal_bounded_and_blocking_default(tmp_path, monkeypatch):
    """H6: an acknowledged real holder controls both acquisition modes."""
    root = hostctl.state_root()
    holder = hold(root / "journal.lock", 0.7)
    started = REAL_MONOTONIC()
    try:
        assert hostctl.journal("bounded", wait=0.1) is False
        assert REAL_MONOTONIC() - started < 0.4
        assert not (root / "journal.jsonl").exists()
        assert hostctl.journal("default") is True
        assert REAL_MONOTONIC() - started >= 0.5
    finally:
        holder.wait(timeout=3)
    rows = [json.loads(line) for line in (root / "journal.jsonl").read_text().splitlines()]
    assert [r["event"] for r in rows] == ["default"]
    assert hostctl.journal("bounded-free", wait=0) is True
    assert hostctl.journal("default-free") is True
    assert "wait" not in json.loads((root / "journal.jsonl").read_text().splitlines()[-2])


@pytest.mark.parametrize("daemon,snapshot_error", [(False, False), (True, True), (True, False)])
def test_doctor_pending_uses_produced_journal(window, monkeypatch, daemon, snapshot_error):
    # Interrupt only this test after one failed stop, leaving its actual produced row.
    def command(*a, **kw):
        raise hostctl.HostctlError("Docker stop failed (exit 1)")

    def abort(seconds):
        raise SystemExit

    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(host_runtime.time, "sleep", abort)
    with pytest.raises(SystemExit):
        window.run()
    monkeypatch.setattr(hostctl, "snapshot", lambda: (_ for _ in ()).throw(
        hostctl.HostctlError("snapshot failed"),
    ) if snapshot_error else {"helper": "helper-a", "state": "open"})
    monkeypatch.setattr(host_runtime, "inspect_object", lambda *a, **kw: window.helper)
    rows = hostctl_checks(None, daemon=daemon)
    row = next(r for r in rows if r.name == "Hostctl journal")
    assert row.status is Status.FAIL
    assert "hostctl closure pending: helper stop failed 1×, last " in row.detail
    assert ("unverifiable" in row.detail) is (not daemon)


@pytest.mark.parametrize("outcome", [
    "relay-closed", "relay-closed-unlocked", "off", "expiry", "new-generation",
    "relay-refused", "absent", "stopped", "foreign", "daemon-socket",
])
@pytest.mark.parametrize("daemon", [False, True])
def test_doctor_clear_precedence(window, monkeypatch, outcome, daemon):
    # Produce a failed and then verified outcome through the observer itself.
    attempts = 0

    def command(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise hostctl.HostctlError("Docker stop failed (exit 1)")
        return window.command(*args, **kwargs)

    journal = hostctl.journal

    def write(event, **kwargs):
        if event == "relay-closed":
            # Keep the genuine failure; select the next event through the same journal API.
            return True
        return journal(event, **kwargs)

    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(hostctl, "journal", write)
    window.run()
    window.helper["State"]["Running"] = True
    if outcome in ("relay-closed", "relay-closed-unlocked", "off", "expiry", "relay-refused"):
        journal(outcome, generation="generation-a")
    elif outcome == "new-generation":
        journal("on-attempt", generation="generation-b")
        journal("relay-close-failed", generation="generation-a")  # Old retry is superseded.

    def inspect(*args, **kwargs):
        assert kwargs["missing"] == "No such container"
        if outcome == "absent":
            return None
        if outcome == "daemon-socket":
            raise host_runtime.GitSSHError("connect: no such file or directory")
        if outcome == "stopped":
            window.helper["State"]["Running"] = False
        if outcome == "foreign":
            window.helper["Config"]["Labels"][hostctl.GENERATION_LABEL] = "generation-b"
        return window.helper

    monkeypatch.setattr(host_runtime, "inspect_object", inspect)
    monkeypatch.setattr(hostctl, "snapshot", lambda: (_ for _ in ()).throw(
        hostctl.HostctlError("snapshot failed"),
    ))
    row = next(r for r in hostctl_checks(None, daemon=daemon) if r.name == "Hostctl journal")
    pending = ("relay-refused", "daemon-socket") if daemon else (
        "relay-refused", "daemon-socket", "absent", "stopped", "foreign",
    )
    assert (row.status is Status.FAIL) is (outcome in pending)


def test_stop_retries_while_real_journal_lock_stays_held(window, monkeypatch):
    holder = hold(window.root / "journal.lock", 3)
    attempts = []
    monkeypatch.setattr(host_runtime.time, "monotonic", REAL_MONOTONIC)
    monkeypatch.setattr(host_runtime.time, "sleep", REAL_SLEEP)

    def command(*args, **kwargs):
        if args[0] == "logs":
            return window.command(*args, **kwargs)
        if holder.poll() is not None:
            pytest.fail("journal contention delayed a stop attempt until after lock release")
        attempts.append(REAL_MONOTONIC())
        if len(attempts) == 1:
            raise hostctl.HostctlError("Docker stop failed (exit 1)")
        return window.command(*args, **kwargs)

    monkeypatch.setattr(hostctl, "command", command)
    try:
        window.run()
    finally:
        holder.wait(timeout=5)
    assert len(attempts) == 2
    assert attempts[1] - attempts[0] < 2
    assert [r["event"] for r in window.rows()] == ["relay-close-failed", "relay-closed"]


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_handler_ignores_all_signals_before_interrupt(window, monkeypatch, signum):
    def tick(*args):
        assert window.admitted
        assert set(window.signals) == {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
        handler = window.signals[signum]
        try:
            handler(signum, None)
        except KeyboardInterrupt:
            assert all(h == signal.SIG_IGN for h in window.signals.values())
            raise
        pytest.fail("signal was not routed through the closure interrupt")

    monkeypatch.setattr(host_runtime, "inspect_dev", tick)
    try:
        window.run()
    except BaseException as exc:
        pytest.fail(f"signal escaped the observer: {type(exc).__name__}")
    assert window.stops == ["helper-a"]
    assert window.rows()[-1]["event"] == "relay-closed"


@pytest.mark.parametrize("vanish", [True, False])
def test_stop_command_error_uses_verified_outcome(window, monkeypatch, vanish):
    def command(*args, **kwargs):
        window.command(*args, **kwargs)
        if vanish:
            window.helper = None
        raise hostctl.HostctlError("Docker stop failed (exit 124)")

    monkeypatch.setattr(hostctl, "command", command)
    window.run()
    assert [r["event"] for r in window.rows()] == ["relay-closed"]


def test_none_journal_fake_means_success(window, monkeypatch):
    calls = []

    def journal(event, **kwargs):
        calls.append(event)
        return None

    monkeypatch.setattr(hostctl, "journal", journal)
    window.run()
    assert calls == ["relay-closed"]
    assert not window.sleeps


def test_strict_absence_uses_exact_engine_wording(monkeypatch):
    # The generic legacy fixture replaces inspect_object; use its collected original.
    monkeypatch.setattr(host_runtime, "inspect_object", REAL_INSPECT_OBJECT)
    monkeypatch.setattr(hostctl, "HELPER_NAME", "helper-a")
    message = "connect: no such file or directory"

    def run(*args, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 5
        return subprocess.CompletedProcess(args[0], 1, stdout="", stderr=message)

    monkeypatch.setattr(host_runtime.subprocess, "run", run)
    with pytest.raises(host_runtime.GitSSHError):
        hostctl.inspect_helper(strict=True)
    message = "Error response from daemon: No such container: helper-a"
    assert hostctl.inspect_helper(strict=True) is None


def test_doctor_tail_is_bounded(window, monkeypatch):
    journal = window.root / "journal.jsonl"
    # An old failure outside the tail cannot determine the current closure state.
    hostctl.journal("relay-close-failed", generation="generation-a")
    with journal.open("a") as stream:
        stream.write("x" * 100000 + "\n")
    hostctl.journal("relay-close-failed", generation="generation-b")
    monkeypatch.setattr(hostctl, "snapshot", lambda: (_ for _ in ()).throw(
        hostctl.HostctlError("snapshot failed"),
    ))
    reads = []
    original = type(journal).open

    class Tracked:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def seek(self, *args):
            return self.stream.seek(*args)

        def readline(self):
            return self.stream.readline()

        def read(self, size=-1):
            reads.append(size)
            assert 0 < size <= 65536
            return self.stream.read(size)

    def opened(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return Tracked(stream) if path == journal else stream

    monkeypatch.setattr(type(journal), "open", opened)
    row = next(r for r in hostctl_checks(None, daemon=False) if r.name == "Hostctl journal")
    assert row.status is Status.FAIL and "failed 1×" in row.detail
    assert reads == [65536]


@pytest.mark.parametrize("failure", [KeyError, KeyboardInterrupt])
def test_closure_is_independent_of_admitted(window, monkeypatch, failure):
    def partial(*args, **kwargs):
        raise failure("partially applied")

    monkeypatch.setattr(hostctl, "admit_locked", partial)
    if failure is KeyError:
        with pytest.raises(KeyError):
            window.run()
    else:
        window.run()
    assert not window.admitted
    assert window.stops == ["helper-a"]
    assert window.rows()[-1]["event"] == "relay-closed"


def test_unlocked_success_and_outcome_journal_order(window, monkeypatch):
    guard = hostctl.control_guard
    events = []
    window.helper["State"]["StartedAt"] = "2026-10-21T12:00:00Z"

    def marker(*args):
        pytest.fail("unlocked closure wrote a guarded provenance marker")

    monkeypatch.setattr(hostctl, "_helper_marker", marker)

    @contextlib.contextmanager
    def busy(*, timeout=15):
        if window.admitted:
            assert timeout == 0
            raise hostctl.HostctlBusy("busy")
        with guard(timeout=timeout):
            yield

    def command(*args, **kwargs):
        assert not any(row["event"].startswith("relay-closed") for row in events)
        return window.command(*args, **kwargs)

    def journal(event, **kwargs):
        if event.startswith("relay-closed"):
            assert window.helper["State"]["Running"] is False
        events.append({"event": event})
        return window.journal(event, **kwargs)

    monkeypatch.setattr(hostctl, "control_guard", busy)
    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(hostctl, "journal", journal)
    window.run()
    assert window.clock == pytest.approx(2)
    assert window.stops == ["helper-a"]
    assert window.rows()[-1]["event"] == "relay-closed-unlocked"


def test_short_publication_skip_streaks_and_reset(window, monkeypatch):
    guard = hostctl.control_guard
    rounds = 0
    companion = []
    published = []
    skip_rounds = []
    # Two busy streaks separated by a successful fresh publication.
    busy_rounds = {1, 2, 3, 4, 6, 7, 8}
    monkeypatch.setattr(hostctl, "verify_agent", lambda *a: companion.append(rounds))

    def tick(*args):
        nonlocal rounds
        rounds += 1
        return None

    @contextlib.contextmanager
    def busy(*, timeout=15):
        if window.admitted and rounds > 0:
            assert timeout == 0
            if rounds in busy_rounds:
                raise hostctl.HostctlBusy("busy")
        with guard(timeout=timeout):
            yield

    def sleep(seconds):
        window.sleep(seconds)
        if rounds in (5, 9):
            published.append(json.loads((window.root / "observation.json").read_text()))
        if rounds == 9:
            raise KeyboardInterrupt

    def journal(event, **kwargs):
        if event == "observation-skipped":
            skip_rounds.append(rounds)
        return window.journal(event, **kwargs)

    monkeypatch.setattr(host_runtime, "inspect_dev", tick)
    monkeypatch.setattr(hostctl, "control_guard", busy)
    monkeypatch.setattr(hostctl, "journal", journal)
    monkeypatch.setattr(host_runtime.time, "sleep", sleep)
    window.run()
    assert companion == list(range(1, 10))
    assert skip_rounds == [3, 8]
    assert len(published) == 2 and published[0]["time"] < published[1]["time"]
    assert window.stops == ["helper-a"]
    assert not list(window.root.glob("observation-*.tmp"))


def test_final_publication_budget_includes_lock_wait(window, monkeypatch):
    waits = []

    def journal(*args, **kwargs):
        wait = kwargs["wait"]
        assert 0 < wait <= 0.5
        waits.append(wait)
        window.clock += wait
        return False

    monkeypatch.setattr(hostctl, "journal", journal)
    window.run()
    assert waits and window.clock == 60
    assert max(window.sleeps) <= 5


def test_control_guard_zero_wait_and_default(window, monkeypatch):
    import inspect

    assert inspect.signature(hostctl.control_guard).parameters["timeout"].default == 15
    holder = hold(window.root / "control.lock", 0.3)
    descriptions = []
    monkeypatch.setattr(hostctl, "describe_holders", lambda *a: descriptions.append(1))
    try:
        with pytest.raises(hostctl.HostctlBusy), hostctl.control_guard(timeout=0):
            pytest.fail("entered a held lock")
        assert not descriptions and window.clock == 0
        with pytest.raises(hostctl.HostctlBusy), hostctl.control_guard():
            pytest.fail("entered a held lock")
        assert descriptions == [1] and window.clock == pytest.approx(15)
    finally:
        monkeypatch.setattr(host_runtime.time, "sleep", REAL_SLEEP)
        holder.wait(timeout=2)
    with hostctl.control_guard(timeout=0), hostctl.control_guard(timeout=0):
        assert hostctl.held_control_descriptor() >= 0


def test_doctor_counts_all_produced_failures(window, monkeypatch):
    calls = 0

    def command(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise hostctl.HostctlError("Docker stop failed (exit 1)")
        return window.command(*args, **kwargs)

    def journal(event, **kwargs):
        return True if event == "relay-closed" else window.journal(event, **kwargs)

    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(hostctl, "journal", journal)
    window.run()
    window.helper["State"]["Running"] = True
    monkeypatch.setattr(host_runtime, "inspect_object", lambda *a, **k: window.helper)
    monkeypatch.setattr(hostctl, "snapshot", lambda: (_ for _ in ()).throw(
        hostctl.HostctlError("snapshot failed"),
    ))
    row = next(r for r in hostctl_checks(None, daemon=True) if r.name == "Hostctl journal")
    assert row.status is Status.FAIL and "failed 3×" in row.detail
    assert all(r["generation"] == "generation-a" and r["helper_id"] == "helper-a"
               for r in window.rows())


def test_post_stop_marker_failure_is_not_stop_failure(window, monkeypatch):
    window.helper["State"]["StartedAt"] = "2026-10-21T12:00:00Z"

    def failed_marker(*args):
        raise OSError("diagnostic unavailable")

    monkeypatch.setattr(hostctl, "_helper_marker", failed_marker)
    window.run()
    assert window.stops == ["helper-a"]
    assert [r["event"] for r in window.rows()] == ["relay-closed"]


def test_retry_log_carries_utc_time(window, monkeypatch, capsys):
    calls = 0

    def command(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise hostctl.HostctlError("Docker stop failed (exit 1)")
        return window.command(*args, **kwargs)

    monkeypatch.setattr(hostctl, "command", command)
    window.run()
    from datetime import datetime

    lines = capsys.readouterr().err.splitlines()
    retries = [line for line in lines if "closure pending;" in line]
    assert len(retries) == 1
    stamp = retries[0].split("Warning: ")[1].split()[0]
    assert stamp.endswith("Z")
    assert datetime.fromisoformat(stamp).utcoffset().total_seconds() == 0


@pytest.mark.parametrize("opening", ["missing", "invalid"])
def test_startup_read_failure_also_closes_pinned_helper(window, opening):
    path = window.root / "opening.json"
    if opening == "missing":
        path.unlink()
    else:
        path.write_text("invalid json")
    window.run()
    assert not window.admitted and window.stops == ["helper-a"]
    assert window.rows()[-1]["event"] == "relay-closed"


def test_control_journal_preserves_blocking_default(window, monkeypatch):
    calls = []

    def journal(event, **kwargs):
        assert "wait" not in kwargs
        calls.append(event)
        return True

    monkeypatch.setattr(hostctl, "journal", journal)
    hostctl.control_journal("off-attempt", generation="generation-a")
    assert calls == ["off-attempt"]


@pytest.mark.parametrize("failure", [KeyError, OSError, host_runtime.GitSSHError])
def test_unknown_closure_inspection_retries(window, monkeypatch, failure):
    called = False

    def inspect(*, strict=False):
        nonlocal called
        if strict and not called:
            called = True
            raise failure("unknown")
        return window.inspect(strict=strict)

    monkeypatch.setattr(hostctl, "inspect_helper", inspect)
    window.run()
    assert [r["event"] for r in window.rows()] == ["relay-close-failed", "relay-closed"]
    assert window.stops == ["helper-a"]


def test_unavailable_guard_also_falls_back(window, monkeypatch):
    guard = hostctl.control_guard

    @contextlib.contextmanager
    def invalid_state(*, timeout=15):
        if window.admitted:
            raise hostctl.HostctlError("state ownership invalid")
        with guard(timeout=timeout):
            yield

    monkeypatch.setattr(hostctl, "control_guard", invalid_state)
    window.run()
    assert window.clock == pytest.approx(2)
    assert window.stops == ["helper-a"]
    assert window.rows()[-1]["event"] == "relay-closed-unlocked"


def test_exception_closure_ignores_later_signals(window):
    window.run()
    assert window.stops == ["helper-a"]
    assert all(handler == signal.SIG_IGN for handler in window.signals.values())


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP, "pair"])
def test_real_signals_during_long_control_hold(tmp_path, signum):
    """Actual signal delivery to an admitted observer while another PID owns the lock."""
    root = hostctl.state_root()
    store = tmp_path / "helper.json"
    store.write_text(json.dumps({
        "Id": "helper-a", "State": {"Running": True},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "generation-a"}},
    }))
    hostctl.save_private("opening.json", {"generation": "generation-a", "dev_id": None})
    script = """import json, pathlib, sys, types
from djinn_in_a_box.core import host_runtime, hostctl
store = pathlib.Path(sys.argv[1])
def inspect(*, strict=False):
    return json.loads(store.read_text())
def command(*args, **kwargs):
    assert args == ('stop', '-t', '3', 'helper-a')
    assert kwargs['timeout'] == 8
    value = json.loads(store.read_text())
    value['State']['Running'] = False
    temporary = store.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(store)
    return ''
hostctl.inspect_helper = inspect
hostctl.command = command
hostctl.node_status = lambda *a: {'BackendState': 'Running'}
hostctl.prepare_trust = lambda *a: {}
hostctl.admission_assessment = lambda *a: {
    'creator': None, 'dev_id': None, 'sealing': 'sealed'}
hostctl.commit_assessment = lambda *a: None
hostctl.admit_locked = lambda *a, **k: None
hostctl.verify_agent = lambda *a: None
host_runtime.inspect_dev = lambda *a: None
host_runtime.subprocess.Popen = lambda *a, **k: types.SimpleNamespace(
    poll=lambda: 0, returncode=0)
host_runtime.observe_hostctl('helper-a', 'generation-a', '/isolated/docker')
"""
    log = tmp_path / "observer.log"
    holder = None

    def until(predicate):
        deadline = REAL_MONOTONIC() + 10
        while not predicate():
            assert REAL_MONOTONIC() < deadline, log.read_text()
            REAL_SLEEP(0.02)

    started = REAL_MONOTONIC()
    with log.open("w") as output:
        observer = REAL_POPEN(
            [sys.executable, "-c", script, str(store)], stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            until(lambda: (root / "observation.json").exists())
            holder = hold_until_eof(root / "control.lock", 31)
            os.kill(observer.pid, signal.SIGTERM if signum == "pair" else signum)
            if signum == "pair":
                until(lambda: "Hostctl observation gap:" in log.read_text())
                os.kill(observer.pid, signal.SIGINT)
                os.kill(observer.pid, signal.SIGHUP)
            returncode = observer.wait(timeout=5)
            closed_at = REAL_MONOTONIC()
            assert_control_lock_held(root / "control.lock")
            assert returncode == 0, log.read_text()
            assert_control_lock_held(root / "control.lock")
            assert not json.loads(store.read_text())["State"]["Running"]
            rows = [json.loads(line) for line in
                    (root / "journal.jsonl").read_text().splitlines()]
            assert_control_lock_held(root / "control.lock")
            assert rows[-1]["event"] == "relay-closed-unlocked"
            assert_control_lock_held(root / "control.lock")
            assert closed_at - started < 5
        finally:
            if holder is not None:
                release_holder(holder)
            if observer.poll() is None:
                try:
                    observer.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    observer.kill()
                    observer.wait(timeout=3)


def test_doctor_malformed_live_inspection_is_unknown(window, monkeypatch):
    hostctl.journal("relay-close-failed", generation="generation-a")
    monkeypatch.setattr(host_runtime, "inspect_object", lambda *a, **kw: {"Config": None})
    monkeypatch.setattr(hostctl, "snapshot", lambda: (_ for _ in ()).throw(
        hostctl.HostctlError("snapshot failed"),
    ))
    row = next(r for r in hostctl_checks(None, daemon=True) if r.name == "Hostctl journal")
    assert row.status is Status.FAIL and "failed 1×" in row.detail


@pytest.mark.parametrize("foreign", ["id", "generation", "absent", "stopped"])
def test_unlocked_probe_already_closed_uses_closed_event(window, monkeypatch, foreign):
    guard = hostctl.control_guard

    @contextlib.contextmanager
    def busy(*, timeout=15):
        if window.admitted:
            raise hostctl.HostctlBusy("busy")
        with guard(timeout=timeout):
            yield

    def inspect(*, strict=False):
        if strict:
            if foreign == "absent":
                return None
            if foreign == "id":
                window.helper["Id"] = "helper-b"
            elif foreign == "generation":
                window.helper["Config"]["Labels"][hostctl.GENERATION_LABEL] = "generation-b"
            else:
                window.helper["State"]["Running"] = False
        return window.inspect(strict=strict)

    monkeypatch.setattr(hostctl, "control_guard", busy)
    monkeypatch.setattr(hostctl, "inspect_helper", inspect)
    window.run()
    assert window.clock == pytest.approx(2)
    assert not window.stops
    assert window.rows()[-1]["event"] == "relay-closed"


def test_reconcile_error_after_guarded_stop_does_not_report_failed_stop(window, monkeypatch):
    reconciled = []

    def reconcile(helper):
        assert not helper["State"]["Running"]
        reconciled.append(helper["Id"])
        raise hostctl.HostctlError("Docker logs failed (exit 1)")

    def admit(*args, **kwargs):
        # A guarded caller's stop succeeded before its post-stop reconciliation failed.
        assert getattr(hostctl._held, "active", False)
        hostctl.stop_helper_locked()

    monkeypatch.setattr(hostctl, "reconcile", reconcile)
    monkeypatch.setattr(hostctl, "admit_locked", admit)
    window.run()
    # The original guarded caller failed; observer closure reconciles once more, without a stop.
    assert reconciled == ["helper-a", "helper-a"]
    assert window.stops == ["helper-a"]
    assert [r["event"] for r in window.rows()] == ["relay-refused", "relay-closed"]


def test_guarded_stop_attempt_keeps_marker_after_unknown_verification(window, monkeypatch):
    window.helper["State"]["StartedAt"] = "2026-10-21T12:00:00Z"
    strict_calls = 0
    markers = []

    def inspect(*, strict=False):
        nonlocal strict_calls
        strict_calls += strict
        if strict_calls == 2:
            raise host_runtime.GitSSHError("Docker inspection failed")
        return window.inspect(strict=strict)

    def command(*args, **kwargs):
        window.command(*args, **kwargs)
        raise hostctl.HostctlError("Docker stop failed (exit 124)")

    def mark(*args, **kwargs):
        assert not getattr(hostctl._held, "active", False)
        assert window.rows()[-1]["event"] == "relay-closed"
        markers.append(args)

    monkeypatch.setattr(hostctl, "inspect_helper", inspect)
    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(hostctl.post_mortem, "mark_terminated", mark)
    window.run()
    assert len(markers) == 1
    assert [r["event"] for r in window.rows()] == ["relay-close-failed", "relay-closed"]


def test_cancellation_during_signal_registration_also_closes(window, monkeypatch):
    delivered = False

    def register(signum, handler):
        nonlocal delivered
        window.signals[signum] = handler
        if not delivered and handler != signal.SIG_IGN:
            delivered = True
            handler(signum, None)

    monkeypatch.setattr(host_runtime.signal, "signal", register)
    try:
        window.run()
    except BaseException as exc:
        pytest.fail(f"registration cancellation escaped closure: {type(exc).__name__}")
    assert delivered and not window.admitted
    assert window.stops == ["helper-a"]
    assert window.rows()[-1]["event"] == "relay-closed"


@pytest.mark.parametrize("running", [False, True], ids=["found-stopped", "guarded-stop"])
def test_guarded_closure_reconciles_real_expiry_after_stop(window, monkeypatch, running):
    window.helper["State"]["Running"] = running
    logs = []
    reconciled = []
    real_reconcile = hostctl.reconcile

    def reconcile(helper):
        assert getattr(hostctl._held, "active", False)
        assert not helper["State"]["Running"]
        reconciled.append(helper)
        real_reconcile(helper)
    expiry = {
        "event": "expiry", "generation": "generation-a",
        "deadline": "2026-10-21T12:00:00Z", "time": "2026-10-21T12:00:00Z",
    }

    def command(*args, **kwargs):
        if args[0] == "logs":
            assert getattr(hostctl._held, "active", False)
            assert not window.helper["State"]["Running"]
            assert window.stops == (["helper-a"] if running else [])
            logs.append(args)
            return json.dumps(expiry)
        return window.command(*args, **kwargs)

    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(hostctl, "inspect_helper", lambda **kw: copy.deepcopy(window.helper))
    monkeypatch.setattr(hostctl, "reconcile", reconcile)
    window.run()
    assert len(reconciled) == 1
    assert logs == [("logs", "--tail", "100", "helper-a")]
    assert window.stops == (["helper-a"] if running else [])
    rows = window.rows()
    assert [row["event"] for row in rows] == ["expiry", "relay-closed"]
    assert rows[0]["generation"] == "generation-a"
    assert rows[0]["helper_time"] == expiry["time"]
    assert rows[0]["deadline"] == expiry["deadline"]
    assert json.loads((window.root / "observed-expiry.json").read_text()) == ["generation-a"]


def test_unlocked_closure_does_not_reconcile(window, monkeypatch):
    guard = hostctl.control_guard
    reconciled = []

    @contextlib.contextmanager
    def busy(*, timeout=15):
        if window.admitted:
            raise hostctl.HostctlBusy("busy")
        with guard(timeout=timeout):
            yield

    def reconcile(helper):
        reconciled.append(helper["Id"])

    monkeypatch.setattr(hostctl, "control_guard", busy)
    monkeypatch.setattr(hostctl, "reconcile", reconcile)
    window.run()
    assert window.stops == ["helper-a"]
    assert not reconciled
    assert window.rows()[-1]["event"] == "relay-closed-unlocked"


@pytest.mark.parametrize(
    "failure", [OSError, KeyError, RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_guarded_reconcile_failure_is_logged_once_without_retry(
    window, monkeypatch, capsys, failure,
):
    reconciled = []

    def reconcile(helper):
        assert getattr(hostctl._held, "active", False)
        assert not helper["State"]["Running"]
        assert window.stops == ["helper-a"]
        reconciled.append(helper)
        raise failure("reconciliation failed")

    monkeypatch.setattr(hostctl, "reconcile", reconcile)
    try:
        window.run()
    except BaseException as exc:
        pytest.fail(f"reconciliation escaped verified closure: {type(exc).__name__}")
    assert len(reconciled) == 1
    assert window.stops == ["helper-a"]
    assert [row["event"] for row in window.rows()] == ["relay-closed"]
    assert not window.sleeps
    assert capsys.readouterr().err.count("expiry reconciliation failed after verified closure") == 1


@pytest.mark.parametrize("foreign", ["id", "generation", "absent", "removed-after-stop"])
def test_guarded_reconcile_requires_pinned_stopped_helper(window, monkeypatch, foreign):
    reconciled = []

    def inspect(*, strict=False):
        if strict and foreign != "removed-after-stop":
            if foreign == "absent":
                return None
            window.helper["State"]["Running"] = False
            if foreign == "id":
                window.helper["Id"] = "helper-b"
            else:
                window.helper["Config"]["Labels"][hostctl.GENERATION_LABEL] = "generation-b"
        return window.inspect(strict=strict)

    def command(*args, **kwargs):
        result = window.command(*args, **kwargs)
        window.helper = None
        return result

    monkeypatch.setattr(hostctl, "inspect_helper", inspect)
    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(hostctl, "reconcile", lambda helper: reconciled.append(helper))
    window.run()
    assert not reconciled
    assert window.stops == (["helper-a"] if foreign == "removed-after-stop" else [])
    assert [row["event"] for row in window.rows()] == ["relay-closed"]


def test_control_probe_rejects_holder_released_by_eof(tmp_path):
    lock = tmp_path / "control.lock"
    holder = hold_until_eof(lock, 4)
    try:
        assert_control_lock_held(lock)
        release_holder(holder)
        assert holder.returncode == 0
        with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
            assert_control_lock_held(lock)
    finally:
        holder.stdin.close()
        holder.wait(timeout=5)


def test_control_probe_closes_descriptor(tmp_path, monkeypatch):
    lock = tmp_path / "control.lock"
    holder = hold_until_eof(lock, 0.3)
    opened = []
    real_open = os.open

    def open_probe(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    try:
        monkeypatch.setattr(os, "open", open_probe)
        assert_control_lock_held(lock)
        assert len(opened) == 1
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(opened[0])
    finally:
        release_holder(holder)


def test_eof_holder_deadline_releases_lock_with_pipe_open(tmp_path):
    lock = tmp_path / "control.lock"
    holder = hold_until_eof(lock, 0.3)
    try:
        assert_control_lock_held(lock)
        assert not holder.stdin.closed
        assert holder.wait(timeout=2) == 0
        with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
            assert_control_lock_held(lock)
    finally:
        release_holder(holder)
