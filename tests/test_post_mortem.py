"""Private recorder behavior using executable Docker fakes and independent writers."""

from __future__ import annotations

import ast
import copy
import json
import os
import stat
import subprocess
import sys
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from djinn_in_a_box.core import host_runtime
from djinn_in_a_box.core import post_mortem as pm

STARTED = "2026-10-17T01:00:00.123456789Z"
FINISHED = "2026-10-17T02:00:00.123456789Z"


@pytest.fixture
def evidence():
    data = json.loads((Path(__file__).parent / "fixtures/agent_docker_inspect.json").read_text())
    data["State"].update(
        Status="exited",
        Running=False,
        ExitCode=137,
        OOMKilled=True,
        Error="",
        StartedAt=STARTED,
        FinishedAt=FINISHED,
    )
    data["RestartCount"] = 7
    data["State"]["RestartCount"] = 999
    data["Config"]["Labels"].update({"com.docker.compose.oneoff": "False"})
    data["HostConfig"]["AutoRemove"] = False
    return data


@pytest.fixture
def fake_docker(tmp_path):
    binary = tmp_path / "docker-fake"
    objects = tmp_path / "objects.json"
    behavior = tmp_path / "behavior.json"
    calls = tmp_path / "calls.jsonl"
    objects.write_text("{}")
    behavior.write_text("{}")
    binary.write_text(f"""#!{sys.executable}
import json, os, pathlib, signal, sys, time
objects = pathlib.Path({str(objects)!r})
behavior = json.loads(pathlib.Path({str(behavior)!r}).read_text())
args = sys.argv[1:]
with pathlib.Path({str(calls)!r}).open('a') as stream:
    stream.write(json.dumps({{'args': args, 'pid': os.getpid()}}) + '\\n')
if args[:2] == ['container', 'inspect']:
    time.sleep(behavior.get('inspect_delay', 0))
    if behavior.get('inspect_error'):
        print('daemon secret [red]do-not-print', file=sys.stderr)
        sys.exit(2)
    data = json.loads(objects.read_text())
    actual = data.get(args[2])
    if actual is None:
        actual = next((v for v in data.values() if v['Id'] == args[2]), None)
    if actual is None:
        print('Error response from daemon: No such container: ' + args[2], file=sys.stderr)
        sys.exit(1)
    print(json.dumps([actual]))
elif args[0] == 'logs':
    if behavior.get('ignore_term'):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(behavior.get('logs_delay', 0))
    if behavior.get('huge'):
        for _ in range(behavior['huge']):
            os.write(1, b'Q' * 8192)
        os.write(2, b'END\\xff')
    else:
        os.write(1, b'private tail [red]secret\\n')
    sys.exit(behavior.get('logs_exit', 0))
else:
    sys.exit(3)
""")
    binary.chmod(0o700)
    return SimpleNamespace(binary=str(binary), objects=objects, behavior=behavior, calls=calls)


def inventory(fake, **objects):
    fake.objects.write_text(json.dumps(objects))


def behavior(fake, **values):
    fake.behavior.write_text(json.dumps(values))


def capture(fake, candidates=None, **kwargs):
    if candidates is None:
        candidates = [pm.Candidate("dev", True)]
    return pm.record("clean", docker_path=fake.binary, candidates=candidates, **kwargs)


def document(result):
    assert result is not None and result.path is not None
    return json.loads(result.path.read_bytes())


def admitted():
    return [path for path, _ in pm._admitted(pm.records_root(create=True))]


def daemon_calls(fake):
    return [json.loads(line) for line in fake.calls.read_text().splitlines()]


def test_constants():
    assert (pm.RECORD_LIMIT, pm.RECORD_TOTAL_BYTES, pm.MAX_RECORD_BYTES) == (10, 8388608, 1048576)
    assert (pm.LOG_TAIL_LINES, pm.LOG_TAIL_BYTES, pm.RECORD_SECONDS) == (200, 65536, 15)
    assert (pm.STRING_BYTES, pm.MARKER_LIMIT) == (2048, 128)


def test_records_root_passwd_and_modes(tmp_path, monkeypatch, _isolate_post_mortem_records):
    home = tmp_path / "passwd"
    monkeypatch.setenv("HOME", str(tmp_path / "wrong-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "wrong-state"))
    monkeypatch.setattr(pm.pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(home)))
    resolver = _isolate_post_mortem_records
    root = resolver(create=False)
    assert root == home / ".local/state/djinn/post-mortem"
    assert not root.exists()
    assert resolver(create=True) == root
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(root.parent.stat().st_mode) == 0o700
    root.chmod(0o755)
    with pytest.raises(Exception, match="directory"):
        resolver(create=True)


def test_real_shape_fields_and_string_caps(fake_docker, evidence, monkeypatch):
    evidence["State"]["Error"] = "é" * 4096
    evidence["Config"]["Image"] = "界" * 4096
    evidence["Name"] = "n" * 4096
    evidence["Config"]["Labels"]["djinn.git-agent.generation"] = "g" * 4096
    inventory(fake_docker, dev=evidence)
    result = capture(fake_docker, runtime_generation="runtime", hostctl_generation="hostctl")
    data = document(result)
    subject = data["subjects"][0]
    assert data["runtime_generation"] == "runtime" and data["hostctl_generation"] == "hostctl"
    assert data["trigger"] == "clean" and data["recorded_at"].endswith("+00:00")
    assert subject["id"] == evidence["Id"]
    assert subject["Image"] == evidence["Image"]
    assert subject["RestartCount"] == 7
    assert subject["State"]["ExitCode"] == 137 and subject["State"]["OOMKilled"] is True
    assert subject["State"]["FinishedAt"] == FINISHED
    assert subject["service"] == evidence["Config"]["Labels"]["com.docker.compose.service"]
    assert set(subject["HostConfig"]) == {
        "Memory",
        "MemorySwap",
        "MemoryReservation",
        "NanoCpus",
        "CpuQuota",
        "CpuPeriod",
        "CpusetCpus",
        "PidsLimit",
    }
    assert subject["HostConfig"] == {k: evidence["HostConfig"].get(k) for k in pm._LIMITS}
    assert set(subject["labels"]) == {
        "djinn.git-agent.generation",
        "djinn.hostctl.generation",
        "com.docker.compose.project",
        "com.docker.compose.service",
    }
    for value in (
        subject["name"],
        subject["State"]["Error"],
        subject["Config"]["Image"],
        subject["labels"]["djinn.git-agent.generation"],
    ):
        assert len(value.encode()) <= 2048
    root = pm.records_root(create=False)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert stat.S_IMODE((root / ".lock").stat().st_mode) == 0o600
    assert pm._RECORD_NAME.fullmatch(result.path.name)
    assert not (root / "provenance.json").exists()
    calls = [call["args"] for call in daemon_calls(fake_docker)]
    assert calls == [["container", "inspect", "dev"], ["logs", "--tail", "200", evidence["Id"]]]


@pytest.mark.parametrize(
    "case", ["absent", "unowned", "running", "label", "autoremove", "firewall", "replacement"]
)
def test_subject_exclusions(fake_docker, evidence, case):
    candidate = pm.Candidate("dev", case != "unowned")
    if case == "running":
        evidence["State"]["Running"] = True
    elif case == "label":
        evidence["Config"]["Labels"]["com.docker.compose.oneoff"] = "True"
    elif case == "autoremove":
        evidence["HostConfig"]["AutoRemove"] = True
    elif case == "firewall":
        evidence["Config"]["Labels"]["com.docker.compose.service"] = "agent-docker-firewall"
    elif case == "replacement":
        candidate = pm.Candidate("dev", True, expected_id="a" * 64)
    inventory(fake_docker, **({} if case == "absent" else {"dev": evidence}))
    result = capture(fake_docker, [candidate])
    assert result is not None and result.path is None
    assert admitted() == []
    assert result.running == ({evidence["Id"]: STARTED} if case == "running" else {})
    assert not (pm.records_root(create=False) / "provenance.json").exists()
    if case == "unowned":
        assert not fake_docker.calls.exists()


def test_no_candidates_no_file(fake_docker):
    result = capture(fake_docker, [])
    assert result == pm.RecordResult(None, {})
    assert admitted() == []


def test_marker_matches_episode_not_container(fake_docker, evidence):
    inventory(fake_docker, dev=evidence)
    pm.mark_terminated(fake_docker.binary, evidence["Id"], STARTED)
    assert capture(fake_docker).path is None
    evidence["State"]["StartedAt"] = "2026-10-17T03:00:00Z"
    inventory(fake_docker, dev=evidence)
    assert document(capture(fake_docker))["subjects"][0]["State"]["StartedAt"] != STARTED


@pytest.mark.parametrize(
    "outcome", ["succeeded", "running", "stopped", "error", "absent", "different", "different-id"]
)
def test_mark_terminated_owner_matrix(fake_docker, evidence, capsys, outcome):
    identity = evidence["Id"]
    if outcome == "running":
        evidence["State"]["Running"] = True
    elif outcome == "different":
        evidence["State"]["StartedAt"] = "2026-10-17T04:00:00Z"
    elif outcome == "different-id":
        evidence["Id"] = "a" * 64
    inventory(fake_docker, **({} if outcome == "absent" else {identity: evidence}))
    behavior(fake_docker, inspect_error=outcome == "error")
    assert (
        pm.mark_terminated(fake_docker.binary, identity, STARTED)
        is None
    )
    root = pm.records_root(create=False)
    marked = (root / "provenance.json").exists()
    assert marked is (outcome in {"succeeded", "stopped"})
    if marked:
        assert json.loads((root / "provenance.json").read_text()) == {identity: STARTED}
        assert stat.S_IMODE((root / "provenance.json").stat().st_mode) == 0o600
    assert len(daemon_calls(fake_docker)) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ("Post-mortem record failed (inspect)\n" if outcome == "error" else "")
    if outcome == "running":
        evidence["State"]["Running"] = False
        inventory(fake_docker, dev=evidence)
        assert document(capture(fake_docker))["subjects"][0]["id"] == identity


def test_marker_cap_and_refresh_order(fake_docker, monkeypatch):
    monkeypatch.setattr(pm, "MARKER_LIMIT", 2)
    for identity in ("a", "b", "a", "c"):
        inventory(fake_docker, **{identity: {
            "Id": identity, "State": {"Running": False, "StartedAt": STARTED},
        }})
        pm.mark_terminated(fake_docker.binary, identity, STARTED)
    assert list(pm._markers(pm.records_root(create=False))) == ["a", "c"]


@contextmanager
def held_lock(root, tmp_path):
    ready, release = tmp_path / "ready", tmp_path / "release"
    code = """import fcntl, os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
pathlib.Path(sys.argv[2]).touch()
end = time.monotonic() + 8
while not pathlib.Path(sys.argv[3]).exists() and time.monotonic() < end:
    time.sleep(.01)
os.close(fd)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(root / ".lock"), str(ready), str(release)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        end = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < end:
            time.sleep(0.01)
        assert ready.exists()
        yield
    finally:
        release.touch()
        _, stderr = child.communicate(timeout=3)
        assert child.returncode == 0, stderr


def test_nonblocking_marker_one_attempt(fake_docker, evidence, monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "RECORD_SECONDS", 1.2)
    inventory(fake_docker, dev=evidence)
    root = pm.records_root(create=True)
    attempts = []
    original = pm.fcntl.flock

    def counted(fd, operation):
        attempts.append(operation)
        return original(fd, operation)

    monkeypatch.setattr(pm.fcntl, "flock", counted)
    with held_lock(root, tmp_path):
        started = time.monotonic()
        pm.mark_terminated(fake_docker.binary, evidence["Id"], STARTED, blocking=False)
        assert time.monotonic() - started < 0.35
    assert attempts == [pm.fcntl.LOCK_EX | pm.fcntl.LOCK_NB]
    assert not (root / "provenance.json").exists()


def test_lock_wait_bounded(fake_docker, evidence, monkeypatch, tmp_path, capsys):
    inventory(fake_docker, dev=evidence)
    monkeypatch.setattr(pm, "RECORD_SECONDS", 0.2)
    with held_lock(pm.records_root(create=True), tmp_path):
        started = time.monotonic()
        assert capture(fake_docker) is None
        assert time.monotonic() - started < 0.8
    assert "Post-mortem record failed (lock)" in capsys.readouterr().err
    assert admitted() == []


@pytest.mark.parametrize(
    "kind", ["symlink", "fifo", "directory", "foreign", "permissive", "bad-name"]
)
def test_admission_shared_by_dedup_latest_prune(fake_docker, evidence, monkeypatch, kind):
    inventory(fake_docker, dev=evidence)
    first = capture(fake_docker)
    path = first.path
    before = path.read_bytes()
    if kind == "bad-name":
        ignored = path.with_name("not-a-record.json")
        path.rename(ignored)
        path = ignored
    elif kind == "permissive":
        path.chmod(0o640)
    elif kind == "foreign":
        original_lstat = Path.lstat

        def foreign_lstat(candidate, *args, **kwargs):
            info = original_lstat(candidate, *args, **kwargs)
            if candidate == path:
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info

        monkeypatch.setattr(Path, "lstat", foreign_lstat)
    else:
        path.unlink()
        if kind == "symlink":
            target = path.parent / "untouched"
            target.write_bytes(before)
            target.chmod(0o600)
            path.symlink_to(target)
        elif kind == "fifo":
            os.mkfifo(path, 0o600)
        else:
            path.mkdir(mode=0o700)
    untouched = path.lstat()
    assert pm.latest() == (0, None, None)
    monkeypatch.setattr(pm, "RECORD_LIMIT", 1)
    monkeypatch.setattr(pm, "RECORD_TOTAL_BYTES", pm.MAX_RECORD_BYTES)
    second = capture(fake_docker)
    assert second.path != path
    assert pm.latest() == (1, second.path, None)
    assert path.lstat() == untouched
    assert path.exists() or path.is_symlink()


def test_latest_read_only_and_bad_directory(tmp_path):
    root = pm.records_root(create=False)
    assert pm.latest() == (0, None, None)
    assert not root.exists()
    root.mkdir(mode=0o755)
    assert pm.latest() == (0, None, "directory")
    root.rmdir()
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    root.symlink_to(target, target_is_directory=True)
    assert pm.latest() == (0, None, "directory")


def test_dedup_keys_across_triggers_generations_and_episodes(fake_docker, evidence):
    inventory(fake_docker, dev=evidence)
    first = capture(fake_docker, runtime_generation="old")
    second = pm.record(
        "start-reclaim",
        docker_path=fake_docker.binary,
        candidates=[pm.Candidate("dev", True)],
        hostctl_generation="new",
    )
    assert second.path == first.path
    evidence["State"]["FinishedAt"] = "2026-10-17T09:00:00Z"
    inventory(fake_docker, dev=evidence)
    assert capture(fake_docker).path != first.path


def test_subject_key_order_and_all_subjects(fake_docker, evidence):
    other = copy.deepcopy(evidence)
    other["Id"] = "a" * 64
    inventory(fake_docker, dev=evidence, helper=other)
    candidates = [pm.Candidate(name, True) for name in ("dev", "helper")]
    first = capture(fake_docker, candidates)
    assert len(document(first)["subjects"]) == 2
    assert capture(fake_docker, list(reversed(candidates))).path == first.path
    assert capture(fake_docker, candidates[:1]).path != first.path
    # An ID/name alias must not duplicate one subject within an incident.
    result = capture(fake_docker, [pm.Candidate("dev", True), pm.Candidate(evidence["Id"], True)])
    assert len(document(result)["subjects"]) == 1


def test_observation_key(fake_docker):
    observed = {"state": "absent", "expected_id": "expected", "actual_id": None}
    first = capture(fake_docker, [], observed=observed, hostctl_generation="one")
    assert document(first)["observed"] == observed
    assert document(first)["subjects"] == []
    assert capture(fake_docker, [], observed=observed, hostctl_generation="one").path == first.path
    assert capture(fake_docker, [], observed=observed, hostctl_generation="two").path != first.path
    for key in ("expected_id", "actual_id"):
        changed = dict(observed, **{key: "different"})
        assert (
            capture(fake_docker, [], observed=changed, hostctl_generation="one").path != first.path
        )


def test_two_process_writers_recheck_dedup_under_lock(fake_docker, evidence, tmp_path):
    inventory(fake_docker, dev=evidence)
    root = pm.records_root(create=True)
    code = """import contextlib, pathlib, sys, time
from djinn_in_a_box.core import post_mortem as pm
root, binary, barrier, number = sys.argv[1:]
barrier = pathlib.Path(barrier)
original = pm._lock
@contextlib.contextmanager
def synchronized(*args, **kwargs):
    (barrier / number).touch()
    end = time.monotonic() + 4
    while len(list(barrier.iterdir())) != 2:
        if time.monotonic() > end:
            raise RuntimeError('barrier timeout')
        time.sleep(.01)
    with original(*args, **kwargs):
        yield
pm._lock = synchronized
original_write = pm._atomic_write
def delayed_write(*args):
    time.sleep(.05)
    return original_write(*args)
pm._atomic_write = delayed_write
result = pm.record('clean', docker_path=binary,
    candidates=[pm.Candidate('dev', True)], root=pathlib.Path(root))
assert result is not None and result.path is not None
print(result.path)
"""
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(root), fake_docker.binary, str(barrier), str(i)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(2)
    ]
    outputs = []
    for child in children:
        stdout, stderr = child.communicate(timeout=8)
        assert child.returncode == 0, stderr
        outputs.append(stdout)
    assert outputs[0] == outputs[1]
    assert len(admitted()) == 1


def seed_record(root, number, size):
    path = root / f"20200101T000000{number:06d}Z-clean-{number:012x}.json"
    path.write_bytes(b" " * size)
    path.chmod(0o600)
    return path


def test_prune_count_and_oldest_order(fake_docker, evidence, monkeypatch):
    root = pm.records_root(create=True)
    paths = [seed_record(root, i, 1) for i in (3, 1, 2)]
    monkeypatch.setattr(pm, "RECORD_LIMIT", 3)
    inventory(fake_docker, dev=evidence)
    result = capture(fake_docker)
    assert [p.exists() for p in paths] == [True, False, True]
    assert pm.latest() == (3, result.path, None)


def test_prune_total_bytes_and_keep_newest(fake_docker, evidence, monkeypatch):
    root = pm.records_root(create=True)
    paths = [seed_record(root, i, 5000) for i in (1, 2)]
    monkeypatch.setattr(pm, "RECORD_TOTAL_BYTES", 6000)
    inventory(fake_docker, dev=evidence)
    result = capture(fake_docker)
    assert sum(p.stat().st_size for p in admitted()) <= 6000
    assert not any(p.exists() for p in paths)
    assert result.path.exists()
    # Even a clock reversal must not delete the newly published record.
    keep = seed_record(root, 0, 5)
    monkeypatch.setattr(pm, "RECORD_LIMIT", 1)
    pm._prune(root, keep)
    assert admitted() == [keep]


def test_oversized_record_drops_tail_with_marker(fake_docker, evidence, monkeypatch):
    inventory(fake_docker, dev=evidence)
    monkeypatch.setattr(pm, "MAX_RECORD_BYTES", 6000)
    monkeypatch.setattr(pm, "_logs", lambda *args: ("L" * 20000, None))
    result = capture(fake_docker)
    subject = document(result)["subjects"][0]
    assert result.path.stat().st_size <= 6000
    assert subject["log_tail"] is None and subject["log_tail_dropped"] == "record-size"


def test_metadata_overflow_fails_closed(fake_docker, evidence, monkeypatch, capsys):
    inventory(fake_docker, dev=evidence)
    monkeypatch.setattr(pm, "MAX_RECORD_BYTES", 10)
    assert capture(fake_docker) is None
    assert admitted() == []
    assert capsys.readouterr().err == "Post-mortem record failed (write)\n"


def test_huge_line_binary_ring_is_bounded(fake_docker, evidence, monkeypatch):
    inventory(fake_docker, dev=evidence)
    behavior(fake_docker, huge=4096)
    original_run = subprocess.run
    original_popen = subprocess.Popen
    children = []

    def run(args, **kwargs):
        assert args[1] != "logs", "logs must never use a fully buffering capture helper"
        assert kwargs["stdin"] is subprocess.DEVNULL
        return original_run(args, **kwargs)

    def popen(args, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        if args[1] == "logs":
            assert not kwargs.get("text") and kwargs.get("bufsize") == 0
            assert kwargs["stdout"] is subprocess.PIPE and kwargs["stderr"] is subprocess.STDOUT
        child = original_popen(args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", popen)
    tracemalloc.start()
    try:
        data = document(capture(fake_docker))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    tail = data["subjects"][0]["log_tail"]
    assert tail == "Q" * (65536 - 4) + "END�"
    assert peak < 2 * 1024 * 1024
    assert all(child.poll() is not None for child in children)


@pytest.mark.parametrize("ignore_term", [False, True])
def test_deadline_writes_incomplete_and_reaps_own_child(
    fake_docker, evidence, monkeypatch, ignore_term
):
    inventory(fake_docker, dev=evidence)
    behavior(fake_docker, logs_delay=2, ignore_term=ignore_term)
    monkeypatch.setattr(pm, "RECORD_SECONDS", 0.3)
    terminations = []
    original_terminate = subprocess.Popen.terminate

    def terminated(child):
        terminations.append(child.pid)
        return original_terminate(child)

    monkeypatch.setattr(subprocess.Popen, "terminate", terminated)
    started = time.monotonic()
    data = document(capture(fake_docker))
    assert time.monotonic() - started < 0.8
    assert data["incomplete"] == "deadline"
    assert data["subjects"][0]["errors"]["log_tail"] == "deadline"
    pid = next(c["pid"] for c in daemon_calls(fake_docker) if c["args"][0] == "logs")
    assert terminations == [pid]
    assert not Path(f"/proc/{pid}").exists()


def test_inspect_timeouts_and_partial_record(fake_docker, evidence, monkeypatch):
    inventory(fake_docker, dev=evidence)
    timeouts = []
    original = host_runtime.inspect_object

    def inspect(name, docker_path, *, timeout):
        timeouts.append(timeout)
        if name == "bad":
            raise ValueError("secret-token")
        return original(name, docker_path, timeout=timeout)

    monkeypatch.setattr(host_runtime, "inspect_object", inspect)
    data = document(capture(fake_docker, [pm.Candidate("bad", True), pm.Candidate("dev", True)]))
    assert data["errors"] == {"candidate:0": "inspect"}
    assert all(0 < timeout <= 5 for timeout in timeouts)
    assert len(data["subjects"]) == 1
    monkeypatch.setattr(pm, "RECORD_SECONDS", 0.02)
    behavior(fake_docker, inspect_delay=0.5)
    observed = {"state": "absent", "expected_id": "x", "actual_id": None}
    data = document(capture(fake_docker, observed=observed))
    assert data["incomplete"] == "deadline"
    assert 0 < timeouts[-1] <= 0.02


@pytest.mark.parametrize("layout", ["systemd", "cgroupfs", "missing", "invalid", "unreadable"])
def test_cgroup_best_effort(fake_docker, evidence, monkeypatch, tmp_path, layout):
    base = tmp_path / "cgroup"
    monkeypatch.setattr(pm, "CGROUP_BASE", base)
    if layout == "invalid":
        evidence["Id"] = "../outside"

        class NeverUsed:
            def __truediv__(self, value):
                pytest.fail("invalid container ID used in a cgroup path")

        monkeypatch.setattr(pm, "CGROUP_BASE", NeverUsed())
    elif layout in {"systemd", "cgroupfs", "unreadable"}:
        directory = (
            base / "system.slice" / f"docker-{evidence['Id']}.scope"
            if layout == "systemd"
            else base / "docker" / evidence["Id"]
        )
        directory.mkdir(parents=True)
        for name in ("memory.current", "memory.peak", "memory.max", "memory.events"):
            (directory / name).write_text("oom_kill 1\n" if name == "memory.events" else "42\n")
        if layout == "unreadable":
            (directory / "memory.current").unlink()
    inventory(fake_docker, dev=evidence)
    cgroup = document(capture(fake_docker))["subjects"][0]["cgroup"]
    if layout == "invalid":
        assert cgroup == {"unavailable": "invalid-id"}
    elif layout == "missing":
        assert cgroup == {"unavailable": "missing"}
    else:
        assert cgroup["memory.peak"] == "42\n" and cgroup["memory.events"] == "oom_kill 1\n"
        assert cgroup["memory.current"] == (
            {"unavailable": "read"} if layout == "unreadable" else "42\n"
        )


def test_literal_output_and_private_content(fake_docker, evidence, tmp_path, capsys):
    inventory(fake_docker, dev=evidence)
    root = tmp_path / "[red]literal[/red]\npath"
    result = capture(fake_docker, root=root)
    output = capsys.readouterr().err
    assert output == "Post-mortem record: " + str(result.path).replace("\n", "\\n") + "\n"
    assert "[red]literal[/red]" in output
    assert "private tail" not in output and "secret" not in output


@pytest.mark.parametrize(
    "stage,code",
    [
        ("directory", "directory"),
        ("write", "write"),
        ("prune", "prune"),
        ("internal", "internal:ValueError"),
        ("logs", "logs"),
        ("inspect", "inspect"),
    ],
)
def test_errors_never_disclose_exception_text(
    fake_docker, evidence, monkeypatch, capsys, stage, code
):
    inventory(fake_docker, dev=evidence)

    def fail(*args, **kwargs):
        raise ValueError("secret-token [red]private\x1b[31m")

    target = {
        "directory": "records_root",
        "write": "_atomic_write",
        "prune": "_prune",
        "internal": "_key",
        "logs": "_logs",
    }.get(stage)
    if target:
        monkeypatch.setattr(pm, target, fail)
    else:
        monkeypatch.setattr(host_runtime, "inspect_object", fail)
    result = capture(fake_docker)
    output = capsys.readouterr().err
    assert f"Post-mortem record failed ({code})\n" in output
    assert "secret-token" not in output and "private" not in output and "\x1b" not in output
    if stage in {"prune", "logs"}:
        assert result.path is not None


def test_failing_output_sink_never_raises(fake_docker, evidence, monkeypatch):
    inventory(fake_docker, dev=evidence)

    def fail(*args, **kwargs):
        raise RuntimeError("private output failure")

    monkeypatch.setattr(pm.console, "print_captured", fail)
    assert capture(fake_docker) is None
    assert pm.mark_terminated(fake_docker.binary, evidence["Id"], STARTED) is None


def test_marker_write_failure_does_not_prevent_record(fake_docker, evidence, monkeypatch, capsys):
    inventory(fake_docker, dev=evidence)
    original = pm._atomic_write

    def write(root, name, payload):
        if name == "provenance.json":
            raise OSError("private marker data")
        return original(root, name, payload)

    monkeypatch.setattr(pm, "_atomic_write", write)
    pm.mark_terminated(fake_docker.binary, evidence["Id"], STARTED)
    assert document(capture(fake_docker))["subjects"][0]["id"] == evidence["Id"]
    output = capsys.readouterr().err
    assert "Post-mortem record failed (marker)" in output
    assert "private marker data" not in output


def test_marker_read_failure_still_records(fake_docker, evidence, capsys):
    inventory(fake_docker, dev=evidence)
    path = pm.records_root(create=True) / "provenance.json"
    path.write_text("not-json secret")
    path.chmod(0o600)
    assert document(capture(fake_docker))["errors"]["provenance"] == "marker"
    output = capsys.readouterr().err
    assert "Post-mortem record failed (marker)" in output and "secret" not in output


def test_atomic_creation_and_fsync_before_publish(fake_docker, evidence, monkeypatch):
    inventory(fake_docker, dev=evidence)
    original_open, original_fsync, original_replace = os.open, os.fsync, os.replace
    events = []

    def opened(path, flags, mode=0o777):
        if Path(path).name.startswith(".record-"):
            assert flags & os.O_EXCL and flags & os.O_NOFOLLOW and mode == 0o600
            events.append("open")
        if Path(path).name == ".lock":
            assert flags & os.O_NOFOLLOW and flags & os.O_CREAT and mode == 0o600
        return original_open(path, flags, mode)

    def synced(fd):
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        events.append("fsync")
        return original_fsync(fd)

    def replaced(source, target):
        assert json.loads(Path(source).read_bytes())["subjects"]
        events.append("rename")
        return original_replace(source, target)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fsync", synced)
    monkeypatch.setattr(os, "replace", replaced)
    assert capture(fake_docker).path.exists()
    assert events == ["open", "fsync", "rename"]
    assert not list(pm.records_root(create=False).glob("*.tmp"))


@pytest.mark.parametrize("name", [".lock", "provenance.json"])
def test_no_follow_for_internal_files(fake_docker, evidence, tmp_path, name):
    inventory(fake_docker, dev=evidence)
    root = pm.records_root(create=True)
    target = tmp_path / "untouched"
    target.write_text("{}")
    target.chmod(0o600)
    (root / name).symlink_to(target)
    result = capture(fake_docker)
    assert target.read_text() == "{}"
    if name == ".lock":
        assert result is None and admitted() == []
    else:
        assert document(result)["errors"]["provenance"] == "marker"


def test_unrelated_marker_does_not_exclude_subject(fake_docker, evidence):
    inventory(fake_docker, dev=evidence)
    pm.mark_terminated(fake_docker.binary, "a" * 64, STARTED)
    assert document(capture(fake_docker))["subjects"][0]["id"] == evidence["Id"]


def test_marker_reinspect_budget(fake_docker, evidence, monkeypatch):
    inventory(fake_docker, dev=evidence)
    timeouts = []
    original = host_runtime.inspect_object

    def inspected(name, docker_path, *, timeout):
        timeouts.append(timeout)
        return original(name, docker_path, timeout=timeout)

    monkeypatch.setattr(host_runtime, "inspect_object", inspected)
    pm.mark_terminated(fake_docker.binary, evidence["Id"], STARTED)
    assert timeouts == [5]
    monkeypatch.setattr(pm, "RECORD_SECONDS", 0.1)
    behavior(fake_docker, inspect_delay=0.5)
    started = time.monotonic()
    pm.mark_terminated(fake_docker.binary, "a" * 64, STARTED)
    assert time.monotonic() - started < 0.5
    assert 0 < timeouts[-1] <= 0.1
    assert "a" * 64 not in pm._markers(pm.records_root(create=False))


def test_docker_logs_individual_five_second_budget(fake_docker, evidence):
    inventory(fake_docker, dev=evidence)
    behavior(fake_docker, logs_delay=6)
    started = time.monotonic()
    data = document(capture(fake_docker))
    assert time.monotonic() - started < 5.6
    assert data["subjects"][0]["errors"]["log_tail"] == "logs"


def test_nonzero_logs_is_field_error(fake_docker, evidence, capsys):
    inventory(fake_docker, dev=evidence)
    behavior(fake_docker, logs_exit=2)
    data = document(capture(fake_docker))
    assert data["subjects"][0]["errors"]["log_tail"] == "logs"
    assert "private tail" in data["subjects"][0]["log_tail"]
    output = capsys.readouterr().err
    assert "failed (logs)" in output and "private tail" not in output


def test_cgroup_directory_error_is_unavailable(fake_docker, evidence, monkeypatch):
    inventory(fake_docker, dev=evidence)
    original = Path.is_dir

    def denied(path):
        if str(path).startswith(str(pm.CGROUP_BASE)):
            raise PermissionError("secret-path")
        return original(path)

    monkeypatch.setattr(Path, "is_dir", denied)
    assert document(capture(fake_docker))["subjects"][0]["cgroup"] == {"unavailable": "read"}


def test_atomic_failure_cleans_only_own_temporary(fake_docker, evidence, monkeypatch, capsys):
    inventory(fake_docker, dev=evidence)
    root = pm.records_root(create=True)
    unrelated = root / ".record-unrelated.tmp"
    unrelated.write_text("untouched")

    def fail(fd):
        raise OSError("secret-fsync")

    monkeypatch.setattr(os, "fsync", fail)
    assert capture(fake_docker) is None
    assert list(root.glob("*.tmp")) == [unrelated]
    assert unrelated.read_text() == "untouched"
    assert capsys.readouterr().err == "Post-mortem record failed (write)\n"


def test_lock_retries_until_release(fake_docker, evidence, tmp_path):
    import threading

    inventory(fake_docker, dev=evidence)
    root = pm.records_root(create=True)
    with held_lock(root, tmp_path):
        release = threading.Timer(0.15, (tmp_path / "release").touch)
        release.start()
        try:
            assert capture(fake_docker).path.exists()
        finally:
            release.join()


def test_lock_rejects_permissive_file(fake_docker, evidence, capsys):
    inventory(fake_docker, dev=evidence)
    lock = pm.records_root(create=True) / ".lock"
    lock.touch(mode=0o666)
    lock.chmod(0o666)
    assert capture(fake_docker) is None
    assert "failed (lock)" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["mode", "list", "values", "cap", "fifo"])
def test_invalid_marker_store_fails_open_for_evidence(fake_docker, evidence, kind, capsys):
    inventory(fake_docker, dev=evidence)
    path = pm.records_root(create=True) / "provenance.json"
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        data = {evidence["Id"]: STARTED}
        if kind == "list":
            data = [data]
        elif kind == "values":
            data[evidence["Id"]] = 10
        elif kind == "cap":
            data.update({str(i): STARTED for i in range(128)})
        path.write_text(json.dumps(data))
        path.chmod(0o644 if kind == "mode" else 0o600)
    assert document(capture(fake_docker))["errors"]["provenance"] == "marker"
    assert "failed (marker)" in capsys.readouterr().err


def test_latest_internal_error_is_safe(monkeypatch, capsys):
    pm.records_root(create=True)

    def fail(root):
        raise RuntimeError("secret-directory")

    monkeypatch.setattr(pm, "_admitted", fail)
    assert pm.latest() == (0, None, "internal:RuntimeError")
    assert capsys.readouterr().err == ""


def test_invalid_trigger_cannot_publish(fake_docker, evidence):
    inventory(fake_docker, dev=evidence)
    assert (
        pm.record("Clean", docker_path=fake_docker.binary, candidates=[pm.Candidate("dev", True)])
        is None
    )
    assert not list(pm.records_root(create=False).glob("*.json"))


def test_fixture_isolates_root_and_guard_detects_changes(tmp_path, monkeypatch):
    import conftest

    root = pm.records_root(create=False)
    assert root.is_relative_to(tmp_path)
    assert not root.exists()
    # Exercise the guard itself against a disposable passwd home.
    monkeypatch.setattr(pm.pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(tmp_path)))
    fixture = conftest._post_mortem_real_directory_guard.__wrapped__
    unchanged = fixture()
    next(unchanged)
    real_shape = tmp_path / ".local/state/djinn/post-mortem"
    assert not real_shape.exists()
    with pytest.raises(StopIteration):
        next(unchanged)
    changed = fixture()
    next(changed)
    real_shape.mkdir(parents=True, mode=0o700)
    with pytest.raises(AssertionError, match="real passwd-home"):
        next(changed)


def test_distinct_ids_same_finish_are_distinct_incidents(fake_docker, evidence):
    inventory(fake_docker, dev=evidence)
    first = capture(fake_docker)
    evidence["Id"] = "a" * 64
    inventory(fake_docker, dev=evidence)
    assert capture(fake_docker).path != first.path


def test_timeout_rejects_expired_waits(monkeypatch):
    monkeypatch.setattr(pm.time, "monotonic", lambda: 1)
    assert pm._timeout(10) == 5
    assert pm._timeout(1.5) == 0.5
    with pytest.raises(pm._RecorderError, match="deadline"):
        pm._timeout(0)


def test_marker_payload_read_is_bounded(fake_docker, evidence, monkeypatch):
    inventory(fake_docker, dev=evidence)
    path = pm.records_root(create=True) / "provenance.json"
    path.write_text(json.dumps({evidence["Id"]: STARTED}) + " " * pm.MAX_RECORD_BYTES)
    path.chmod(0o600)
    sizes = []
    original_fdopen = os.fdopen

    def opened(fd, mode):
        stream = original_fdopen(fd, mode)
        if mode != "rb":
            return stream

        class Reader:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                stream.close()

            def fileno(self):
                return stream.fileno()

            def read(self, size=-1):
                sizes.append(size)
                return stream.read(size)

        return Reader()

    monkeypatch.setattr(os, "fdopen", opened)
    assert document(capture(fake_docker))["errors"]["provenance"] == "marker"
    assert sizes == [pm.MAX_RECORD_BYTES + 1]


def test_empty_id_is_not_a_subject(fake_docker, evidence):
    evidence["Id"] = ""
    inventory(fake_docker, dev=evidence)
    assert capture(fake_docker).path is None
    assert admitted() == []


@pytest.mark.parametrize("kind", ["corrupt", "mode", "symlink", "fifo"])
def test_mark_terminated_repairs_marker_store(fake_docker, evidence, kind, tmp_path, capsys):
    inventory(fake_docker, dev=evidence)
    root = pm.records_root(create=True)
    path = root / "provenance.json"
    foreign = tmp_path / "foreign"
    foreign.write_text("untouched")
    if kind == "symlink":
        path.symlink_to(foreign)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_text("invalid" if kind == "corrupt" else '{"old": "untrusted"}')
        path.chmod(0o600 if kind == "corrupt" else 0o644)
    inode = path.lstat().st_ino
    pm.mark_terminated(fake_docker.binary, evidence["Id"], STARTED)
    assert not path.is_symlink()
    assert path.stat().st_ino != inode
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == {evidence["Id"]: STARTED}
    assert foreign.read_text() == "untouched"
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Post-mortem record failed (marker)\n"
    inventory(fake_docker, dev=evidence)
    assert capture(fake_docker).path is None


def provenance_violations(sources):
    """Allow only the private filename declaration, read, and verified write."""
    violations = []
    for path, source in sources.items():
        tree = ast.parse(source)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def enclosing(node, parents=parents):
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return node.name
            return None

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "provenance.json":
                parent = parents[node]
                if not (
                    path == "core/post_mortem.py"
                    and isinstance(parent, ast.Assign)
                    and len(parent.targets) == 1
                    and isinstance(parent.targets[0], ast.Name)
                    and parent.targets[0].id == "_PROVENANCE_NAME"
                    and enclosing(node) is None
                ):
                    violations.append(f"{path}:{node.lineno}: filename outside its declaration")
            if not (
                isinstance(node, ast.Name) and node.id == "_PROVENANCE_NAME"
                or isinstance(node, ast.Attribute) and node.attr == "_PROVENANCE_NAME"
            ):
                continue
            parent = parents[node]
            if path == "core/post_mortem.py":
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    continue
                if (
                    isinstance(parent, ast.Call)
                    and isinstance(parent.func, ast.Name)
                    and parent.func.id == "_atomic_write"
                    and parent.args[1:2] == [node]
                    and enclosing(node) == "mark_terminated"
                ):
                    continue
                grandparent = parents.get(parent)
                if (
                    isinstance(parent, ast.BinOp)
                    and isinstance(parent.op, ast.Div)
                    and isinstance(grandparent, ast.Call)
                    and ast.unparse(grandparent.func) == "os.open"
                    and ast.unparse(grandparent.args[1]) == (
                        "os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK"
                    )
                    and enclosing(node) == "_markers"
                ):
                    continue
            violations.append(f"{path}:{node.lineno}: marker access outside the verified path")
    return violations


def provenance_sources():
    root = Path(pm.__file__).parents[1]
    return {str(path.relative_to(root)): path.read_text() for path in root.rglob("*.py")}


def test_provenance_has_one_write_path():
    assert provenance_violations(provenance_sources()) == []


@pytest.mark.parametrize(
    "writer", ["local-literal", "local-constant", "foreign-literal", "foreign-constant"]
)
def test_provenance_guard_rejects_second_writer(writer):
    sources = provenance_sources()
    path = "core/post_mortem.py" if writer.startswith("local") else "core/second_writer.py"
    name = '"provenance.json"' if writer.endswith("literal") else "_PROVENANCE_NAME"
    sources[path] = sources.get(path, "") + (
        f"\ndef second_writer(root):\n    _atomic_write(root, {name}, b'{{}}')\n"
    )
    assert provenance_violations(sources)


def test_marker_rejects_caller_outcome_flag(fake_docker, evidence):
    with pytest.raises(TypeError, match="succeeded"):
        pm.mark_terminated(fake_docker.binary, evidence["Id"], STARTED, succeeded=True)
