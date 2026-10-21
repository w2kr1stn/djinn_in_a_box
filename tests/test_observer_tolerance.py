"""Issue #199: ordinary lock conflicts and one-tick Docker failures in a running dev."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import resource
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from djinn_in_a_box.core import git_agent, host_runtime, hostctl, post_mortem
from djinn_in_a_box.core.git_agent import agent_keys
from djinn_in_a_box.core.host_runtime import (
    GENERATION_LABEL,
    git_runtime,
    runtime_root,
    stop_owned_process,
)
from djinn_in_a_box.core.ssh_delivery import GitSSHError, read_public_delivery

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")

HOLDER = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "time.sleep(float(sys.argv[2]))\n"
)


@pytest.fixture(autouse=True)
def container_uid_matches_host(monkeypatch):
    monkeypatch.setattr(host_runtime, "CONTAINER_USER_UID", os.getuid())


@pytest.fixture
def fake(git_inputs, monkeypatch):
    """Fake Docker that can fail exactly the observer's call made under the creation guard."""
    base = Path(os.environ["XDG_RUNTIME_DIR"])
    state = base / "fake-dev.json"
    arm = base / "arm"
    binary = base / "bin" / "docker"
    binary.parent.mkdir()
    creator = os.path.realpath(runtime_root(create=True) / "creator.lock")
    binary.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, subprocess, sys, time\n"
        f"state = pathlib.Path({str(state)!r})\n"
        f"arm = pathlib.Path({str(arm)!r})\n"
        f"binary = {str(binary)!r}\n"
        f"creator = {creator!r}\n"
        "def guarded():\n"
        "    directory = f'/proc/{os.getppid()}/fd'\n"
        "    for name in os.listdir(directory):\n"
        "        try:\n"
        "            if os.readlink(f'{directory}/{name}') == creator:\n"
        "                return True\n"
        "        except OSError:\n"
        "            pass\n"
        "    return False\n"
        "fields = arm.read_text().split() if arm.exists() else []\n"
        "mode = fields[0] if fields else ''\n"
        "scope = fields[1] if len(fields) > 1 else 'any'\n"
        "inside = guarded()\n"
        "hit = bool(mode) and scope in ('any', 'guarded' if inside else 'free')\n"
        "with open(binary + '.calls', 'a') as stream:\n"
        "    stream.write(f\"{time.time()} {'guarded' if inside else 'free'} {sys.argv[1]}\\n\")\n"
        "if hit and mode == 'spawn':\n"
        "    arm.unlink()\n"
        "    os.chmod(binary, 0o600)\n"
        "    subprocess.Popen([sys.executable, '-c',\n"
        "        'import os, sys, time; time.sleep(0.6); os.chmod(sys.argv[1], 0o700)', binary],\n"
        "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "        start_new_session=True)\n"
        "elif hit and mode == 'timeout':\n"
        "    arm.unlink()\n"
        "    time.sleep(6)\n"
        "elif hit and mode == 'daemon':\n"
        "    arm.unlink()\n"
        "    print('Cannot connect to the Docker daemon at unix:///var/run/docker.sock. '\n"
        "          'Is the docker daemon running?', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "if state.exists():\n"
        "    print(state.read_text())\n"
        "else:\n"
        "    print('Error response from daemon: No such container: djinn', file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    binary.chmod(0o700)
    monkeypatch.setattr(host_runtime, "DOCKER_EXECUTABLE", str(binary))
    try:
        yield SimpleNamespace(state=state, arm=arm, binary=binary)
    finally:
        binary.chmod(0o700)
        metadata = runtime_root() / "state.json"
        if metadata.exists():
            record = json.loads(metadata.read_text())
            if "observer_pid" in record:
                stop_owned_process(record["observer_pid"], record["observer_token"])
            stop_owned_process(record["agent_pid"], record["agent_token"])


def observed_dev(state, runtime, dev_id="dev-1", running=True):
    fd, temporary = tempfile.mkstemp(dir=state.parent)
    with os.fdopen(fd, "w") as stream:
        stream.write(
            json.dumps(
                [
                    {
                        "Id": dev_id,
                        "State": {"Running": running},
                        "Config": {"Labels": {GENERATION_LABEL: runtime.generation}},
                    }
                ]
            )
        )
    os.replace(temporary, state)


@pytest.fixture
def attached(git_inputs, fake, tmp_path):
    root = runtime_root()
    expected = read_public_delivery(git_inputs.git).blobs
    with git_runtime(git_inputs, "djinn") as runtime:
        observed_dev(fake.state, runtime)
        runtime.retain()
        observer = runtime.observer
    assert observer is not None and observer.poll() is None
    assert agent_keys(root / "export/auth.sock") == expected
    try:
        yield SimpleNamespace(
            root=root, observer=observer, expected=expected, fake=fake, runtime=runtime,
            public=git_inputs.git.identities["git-work"].public_key_file, tmp=tmp_path,
        )
    finally:
        if observer.poll() is None:
            observer.terminate()
        observer.wait(timeout=8)
        runtime.close()


def evidence(context) -> str:
    log = context.root / "observer.log"
    return (
        f"observer returncode={context.observer.poll()}; "
        f"socket present={(context.root / 'export/auth.sock').exists()}; "
        f"state present={(context.root / 'state.json').exists()}\n"
        + (log.read_text(errors="replace")[-3000:] if log.exists() else "<no observer.log>")
    )


def sign(context) -> None:
    """Git delivery is proven by a real signature through the exported socket."""
    payload = context.tmp / "payload"
    payload.write_text("signed\n")
    signature = context.tmp / "payload.sig"
    signature.unlink(missing_ok=True)
    result = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-n", "git", "-f", str(context.public), "-U", str(payload)],
        env={**os.environ, "SSH_AUTH_SOCK": str(context.root / "export/auth.sock")},
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (result.stderr, evidence(context))
    signature.unlink(missing_ok=True)


def assert_survived(context) -> None:
    assert context.observer.poll() is None, evidence(context)
    assert agent_keys(context.root / "export/auth.sock") == context.expected, evidence(context)
    sign(context)


def hold(path: Path, seconds: float) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path), str(seconds)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "held"
    return holder


def calls(context) -> list[tuple[float, str, str]]:
    path = Path(str(context.fake.binary) + ".calls")
    if not path.exists():
        return []
    rows = (line.split(" ", 2) for line in path.read_text().splitlines())
    return [(float(a), b, c) for a, b, c in rows]


def wait_for(predicate, seconds=10, message="condition"):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert predicate(), message


def fire(context, mode: str, scope: str = "free") -> None:
    """Arm one fault and wait until the fake consumed it (the hit counter)."""
    context.fake.arm.write_text(f"{mode} {scope}")
    wait_for(lambda: not context.fake.arm.exists(), 15, f"{mode} fault was never injected")


def test_creator_lock_held_across_ticks(attached):
    """L2: creator.lock held across several ticks."""
    holder = hold(attached.root / "creator.lock", 2)
    try:
        time.sleep(1.5)
        assert_survived(attached)
    finally:
        holder.wait(timeout=5)
    time.sleep(0.5)
    assert_survived(attached)


def test_control_lock_held_beyond_tolerance(attached):
    """L3: control.lock held past the former 15 s tolerance."""
    holder = hold(hostctl.state_root() / "control.lock", 17)
    try:
        time.sleep(16.5)
        assert_survived(attached)
    finally:
        holder.wait(timeout=5)
    time.sleep(0.5)
    assert_survived(attached)


def test_audit_admission_waiting_on_carrier_lock(attached, tmp_path):
    """T11: #162's admission holds control.lock while it waits for the carrier lock."""
    config_root = tmp_path / "audit-config"
    carrier = config_root / "opencode"
    carrier.mkdir(parents=True, mode=0o700)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\n"
         "time.sleep(20)\n", str(carrier)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
    admission = subprocess.Popen(
        [sys.executable, "-c",
         "import sys\n"
         "from djinn_in_a_box.config.models import AppConfig\n"
         "from djinn_in_a_box.core import assistant\n"
         "session = assistant.open_audit_session("
         "AppConfig(code_dir=sys.argv[1], config_root=sys.argv[2]))\n"
         "session.close()\n", str(tmp_path), str(config_root)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        # The admission must be inside its wait, i.e. holding control.lock, while we sign.
        wait_for(lambda: _lock_is_held(hostctl.state_root() / "control.lock"), 10, "no admission")
        time.sleep(1)
        assert_survived(attached)
        time.sleep(15)
        assert admission.poll() is None, "admission ended before the 16 s mark"
        assert _lock_is_held(hostctl.state_root() / "control.lock")
        assert_survived(attached)
    finally:
        holder.wait(timeout=30)
        admission.wait(timeout=40)
    assert_survived(attached)


def _lock_is_held(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


@pytest.mark.parametrize(
    ("mode", "word"),
    [
        ("spawn", "PermissionError"),
        ("timeout", "TimeoutExpired"),
        ("daemon", "runtime ownership is unknown"),
    ],
)
def test_lock_free_dev_inspection_failure_for_one_tick(attached, mode, word):
    """L4: a failed lock-free inspection is unknown; the hit counter proves it happened."""
    fire(attached, mode, "free")
    wait_for(lambda: word in (attached.root / "observer.log").read_text(), 15,
             f"observer never logged {word}: {evidence(attached)}")
    time.sleep(7 if mode == "timeout" else 1.5)
    assert_survived(attached)


@pytest.mark.parametrize("damage", ["invalid-json", "unreadable"])
def test_state_read_failure_for_one_tick(attached, damage):
    """L5: a failed state.json read is unknown; no resource action follows."""
    state = attached.root / "state.json"
    original = state.read_text()
    before = len(calls(attached))
    if damage == "invalid-json":
        state.write_text("{")
    else:
        state.chmod(0)
    time.sleep(1)
    state.chmod(0o600)
    state.write_text(original)
    time.sleep(1)
    assert_survived(attached)
    assert "retrying" in (attached.root / "observer.log").read_text()
    assert {call[2] for call in calls(attached)[before:]} <= {"container"}


@pytest.mark.parametrize(
    ("mode", "word"), [("daemon", "runtime ownership is unknown"), ("timeout", "TimeoutExpired")]
)
def test_guarded_inspection_failure_during_real_stop_transition(attached, mode, word):
    """G1: the guarded re-inspection fails once during a dev stop; the transition completes."""
    attached.fake.arm.write_text(f"{mode} guarded")
    observed_dev(attached.fake.state, attached.runtime, running=False)
    wait_for(lambda: not attached.fake.arm.exists(), 15, "guarded fault was never injected")
    assert attached.observer.poll() is None, evidence(attached)
    wait_for(lambda: attached.observer.poll() is not None, 20, evidence(attached))
    assert attached.observer.returncode == 0, evidence(attached)
    assert not (attached.root / "state.json").exists()
    assert word in (attached.root / "observer.log").read_text()


def test_busy_guard_during_pending_transition_is_retried(attached):
    """G2: a held control.lock delays a pending transition; it takes no action until release."""
    earlier = len(calls(attached))
    holder = hold(hostctl.state_root() / "control.lock", 3)
    released = time.time() + 3
    observed_dev(attached.fake.state, attached.runtime, running=False)
    try:
        time.sleep(2)
        assert attached.observer.poll() is None, evidence(attached)
        assert (attached.root / "state.json").exists()
    finally:
        holder.wait(timeout=5)
    wait_for(lambda: attached.observer.poll() is not None, 10, evidence(attached))
    assert not (attached.root / "state.json").exists()
    guarded = [call for call in calls(attached)[earlier:] if call[1] == "guarded"]
    assert guarded and all(call[0] >= released - 0.5 for call in guarded), guarded
    assert "HostctlBusy" in (attached.root / "observer.log").read_text()


def test_filter_death_is_rebound_and_published(attached):
    """F3: persistent accept failures end the thread; the observer rebinds in place."""
    pid = attached.observer.pid
    soft, hard = resource.prlimit(pid, resource.RLIMIT_NOFILE)
    resource.prlimit(pid, resource.RLIMIT_NOFILE, (len(os.listdir(f"/proc/{pid}/fd")) + 1, hard))
    clients = []
    try:
        # One descriptor of headroom: the first client takes it, every later accept() fails.
        for _ in range(3):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            clients.append(client)
            client.connect(str(attached.root / "export/auth.sock"))
        time.sleep(7)  # beyond 50 failures at 0.1 s
    finally:
        resource.prlimit(pid, resource.RLIMIT_NOFILE, (soft, hard))
        for client in clients:
            client.close()
    wait_for(
        lambda: json.loads((attached.root / "state.json").read_text()).get("filter_rebinds"),
        20, evidence(attached),
    )
    assert_survived(attached)
    published = json.loads((attached.root / "state.json").read_text())["filter_rebinds"]
    log = (attached.root / "observer.log").read_text()
    assert len(published) >= 1
    assert all(f"{stamp} observer: Git agent filter rebound" in log for stamp in published)


def descriptor_and_thread_counts(pid: int) -> tuple[int, int]:
    """Steady counts: a tick's in-flight `docker inspect` briefly holds two pipe descriptors."""
    samples = []
    for _ in range(20):
        threads = next(
            int(line.split()[1])
            for line in Path(f"/proc/{pid}/status").read_text().splitlines()
            if line.startswith("Threads:")
        )
        samples.append((len(os.listdir(f"/proc/{pid}/fd")), threads))
        time.sleep(0.05)
    return min(sample[0] for sample in samples), min(sample[1] for sample in samples)


def test_observer_does_not_leak_descriptors_or_threads(attached, git_inputs, tmp_path):
    pid = attached.observer.pid
    time.sleep(1)
    before = descriptor_and_thread_counts(pid)
    payload = tmp_path / "payload"
    payload.write_text("signed\n")
    public = git_inputs.git.identities["git-work"].public_key_file
    environment = {**os.environ, "SSH_AUTH_SOCK": str(attached.root / "export/auth.sock")}
    for index in range(150):
        result = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-n", "git", "-f", str(public), "-U", str(payload)],
            env=environment,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        (tmp_path / "payload.sig").unlink()
        if index % 10 == 0:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(attached.root / "export/auth.sock"))
            client.close()
            agent_keys(attached.root / "export/auth.sock")
    time.sleep(6)
    after = descriptor_and_thread_counts(pid)
    print(f"observer fds/threads before={before} after={after}")
    assert after[0] <= before[0] + 1 and after[1] <= before[1] + 1, (before, after)
    assert_survived(attached)


# ---------------------------------------------------------------------------------------------
# In-process observer: controlled clock, scripted dev inspections, counted guards.
# ---------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.ticks = 0
        self.on_tick = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Shim:
    """Overrides some attributes of a module and delegates the rest."""

    def __init__(self, real, **overrides) -> None:
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Harness:
    def __init__(self, root: Path, records: Path, docker: Path) -> None:
        self.root, self.records, self.docker = root, records, docker
        self.clock = Clock()
        self.generation = "g1"
        self.dev: tuple[str, bool, str] | None = ("dev-1", True, "g1")
        self.inspections: list[tuple[int, bool]] = []  # (tick, inside the guard)
        self.failures: list[tuple[str, BaseException]] = []  # (scope, error)
        self.hook = None
        self.guards: list[tuple[str, float, int]] = []
        self.servers: list[git_agent.AgentFilter] = []
        self.workers: list[threading.Thread] = []
        self.thread_failures = 0
        self.busy_after: int | None = None
        self.agent: subprocess.Popen[bytes] | None = None
        self.stop_at: int | None = None

    # -- fakes -------------------------------------------------------------------------------
    def inspect_dev(self, name, docker_path=None):
        inside = getattr(host_runtime._creation_held, "descriptor", None) is not None
        self.inspections.append((self.clock.ticks, inside))
        for index, (scope, error) in enumerate(self.failures):
            if scope == "any" or (scope == "guarded") == inside:
                del self.failures[index]
                raise error
        if self.hook is not None:
            self.hook(self, inside)
        return self.dev

    def tick(self) -> None:
        if self.stop_at is not None and self.clock.ticks >= self.stop_at:
            signal.raise_signal(signal.SIGTERM)
        if self.clock.on_tick is not None:
            self.clock.on_tick(self.clock.ticks)

    # -- state -------------------------------------------------------------------------------
    def prepare(self, *, git=False, resources=None, agent=False, creator=True) -> None:
        if agent:
            self.agent = subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL)
        state = {
            "generation": self.generation,
            "project": "djinn-in-a-box",
            "container_name": "djinn",
            "creator_pid": os.getpid(),
            "creator_token": host_runtime.process_token(os.getpid()) if creator else "gone",
            "observer_pid": -1,
            "observer_token": "",
            "agent_pid": self.agent.pid if self.agent else -1,
            "agent_token": host_runtime.process_token(self.agent.pid) if self.agent else "",
            "keys": [],
            "git_enabled": git,
            "hostctl_hosts": {},
            "docker_path": str(self.docker),
            "resources": resources or {},
            "volumes": [],
        }
        (self.root / "state.json").write_text(json.dumps(state))
        (self.root / "state.json").chmod(0o600)
        (self.root / "loaded").touch()
        if git:
            host_runtime.private_directory(self.root / "export")
            host_runtime.private_directory(self.root / "private")

    def state(self) -> dict | None:
        return host_runtime.read_state(self.root)

    def run(self) -> None:
        descriptor = host_runtime.acquire_creation_lock(self.root)
        high = fcntl.fcntl(descriptor, fcntl.F_DUPFD, 500)  # never reused by other opens
        os.close(descriptor)
        host_runtime.observe(self.root, high, str(self.docker), self.records)

    def log_lines(self, capsys) -> list[str]:
        return capsys.readouterr().err.splitlines()

    def exit_records(self) -> list[dict]:
        return [
            json.loads(path.read_text())
            for path in sorted(self.records.glob("*-observer-exit-*.json"))
        ]

    def export_file(self) -> Path:
        return self.root / "export/observer-exit"


@pytest.fixture
def inproc(git_inputs, monkeypatch):
    base = Path(os.environ["XDG_RUNTIME_DIR"])
    docker = base / "docker-absent"
    docker.write_text(
        "#!/bin/sh\necho 'Error response from daemon: No such container: x' >&2\nexit 1\n"
    )
    docker.chmod(0o700)
    records = base / "records"
    records.mkdir(mode=0o700)
    harness = Harness(runtime_root(create=True), records, docker)
    clock = harness.clock

    class TickEvent(threading.Event):
        def wait(self, timeout=None):
            if timeout is None or self.is_set():
                return super().wait(timeout)
            clock.now += timeout
            clock.ticks += 1
            harness.tick()
            return self.is_set()

    class FlakyThread(threading.Thread):
        def start(self):
            if harness.thread_failures:
                harness.thread_failures -= 1
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(
        host_runtime, "threading", Shim(threading, Event=TickEvent, Thread=FlakyThread)
    )
    monkeypatch.setattr(
        host_runtime, "time", Shim(time, monotonic=clock.monotonic, sleep=clock.sleep)
    )
    monkeypatch.setattr(host_runtime, "inspect_dev", harness.inspect_dev)
    monkeypatch.setattr(host_runtime, "agent_keys", lambda path: frozenset())

    real_creation = host_runtime.creation_guard
    real_control = hostctl.control_guard

    @contextlib.contextmanager
    def creation(root=None, *, timeout=15):
        harness.guards.append(("creation", timeout, clock.ticks))
        if harness.busy_after is not None and clock.ticks >= harness.busy_after:
            raise hostctl.HostctlBusy("host control is busy; retry after the transition completes")
        with real_creation(root, timeout=timeout) as entered:
            yield entered

    @contextlib.contextmanager
    def control(*, timeout=15):
        harness.guards.append(("control", timeout, clock.ticks))
        with real_control(timeout=timeout):
            yield

    monkeypatch.setattr(host_runtime, "creation_guard", creation)
    monkeypatch.setattr(hostctl, "control_guard", control)

    real_serve = host_runtime._serve_filter

    def serve(root, allowed, clients=None):
        pair = real_serve(root, allowed, clients)
        harness.servers.append(pair[0])
        harness.workers.append(pair[1])
        return pair

    monkeypatch.setattr(host_runtime, "_serve_filter", serve)
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield harness
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
        if harness.agent is not None:
            if harness.agent.poll() is None:
                harness.agent.kill()
            harness.agent.wait(timeout=5)
        for server in harness.servers:
            server.stop.set()
            with contextlib.suppress(OSError):
                server.listener.close()
            server.retire_path()


def test_idle_ticks_take_no_lock(inproc):
    """L1: a stable dev takes no lock; one change takes exactly the guarded entry."""
    inproc.prepare()
    inproc.hook = lambda h, inside: (
        setattr(h, "dev", ("dev-1", False, "g1")) if h.clock.ticks >= 12 else None
    )
    inproc.run()
    assert inproc.state() is None  # Git-only release-on-stop ended it after the one change
    idle = [g for g in inproc.guards if 1 < g[2] < 12]
    assert idle == [], idle
    change = [g for g in inproc.guards if g[2] >= 12]
    assert [g[:2] for g in change[:2]] == [("creation", 0), ("control", 0)]
    assert inproc.inspections[0:1] and all(
        not inside for tick, inside in inproc.inspections if 1 < tick < 12
    )


def test_startup_deadline_with_persistent_unknown_inspection(inproc):
    """L0: unknown inspections before attach cannot extend the startup deadline."""
    inproc.prepare()
    inproc.failures = [("any", GitSSHError("Docker unavailable"))] * 100000
    inproc.stop_at = 600
    inproc.run()
    assert inproc.clock.ticks < 600, "the startup deadline never ended the observer"
    assert inproc.clock.now - 1000 <= host_runtime.STARTUP_SECONDS + 1
    assert not inproc.export_file().exists()
    assert inproc.exit_records() == []
    assert inproc.state() is not None and "observer_exit" not in inproc.state()


def test_startup_creator_gone_ends_before_inspecting(inproc):
    inproc.prepare(creator=False)
    inproc.failures = [("any", GitSSHError("Docker unavailable"))] * 100000
    inproc.stop_at = 50
    inproc.run()
    assert inproc.clock.ticks < 5
    assert inproc.exit_records() == []


def test_attach_is_one_retried_step(inproc, monkeypatch):
    """L0: the attach publication is retried as a whole; dev_id and the fd follow the writes."""
    inproc.prepare()
    events: list[str] = []
    real_save, real_touch, real_close = host_runtime.save_state, Path.touch, os.close
    failing = {"save": 1, "touch": 1}

    def save(root, state):
        if "dev_id" in state:
            events.append("save")
            if failing["save"]:
                failing["save"] -= 1
                raise OSError("disk full")
        real_save(root, state)

    def touch(self, *args, **kwargs):
        if self.name == "attached":
            events.append("touch")
            if failing["touch"]:
                failing["touch"] -= 1
                raise OSError("read-only")
        return real_touch(self, *args, **kwargs)

    def close(descriptor):
        if descriptor >= 500:
            events.append("close")
        real_close(descriptor)

    monkeypatch.setattr(host_runtime, "save_state", save)
    monkeypatch.setattr(Path, "touch", touch)
    monkeypatch.setattr(os, "close", close)
    inproc.stop_at = 6
    inproc.run()
    assert events == ["save", "save", "touch", "save", "touch", "close"]
    assert (inproc.root / "attached").exists() or inproc.state() is None


def test_replacement_between_probe_and_guard_is_not_touched(inproc, steps):
    """G3: a dev replaced after the lock-free probe is E3b: no action on the replacement."""
    inproc.prepare(resources={"audio-helper": {"id": "helper-1", "service": "audio-helper"}})
    calls_made = steps.calls

    def hook(h, inside):
        if h.clock.ticks >= 4 and not inside:
            h.dev = ("dev-1", False, "g1")  # the probe sees dev-1 stopped ...
        elif inside:
            h.dev = ("dev-2", True, "g1")  # ... the guarded re-inspection a replacement

    inproc.hook = hook
    inproc.run()
    assert calls_made == []
    state = inproc.state()
    assert state is not None and state["observer_exit"]["code"] == "dev-replaced"
    assert not inproc.export_file().exists()
    assert [r["observed"]["state"] for r in inproc.exit_records()] == ["dev-replaced"]


def test_foreign_state_between_probe_and_guard_is_not_touched(inproc):
    """G3/E2: a state of another generation appearing before the guard is left untouched."""
    inproc.prepare()
    foreign = {"generation": "g2", "marker": "successor"}

    def hook(h, inside):
        if h.clock.ticks >= 4 and not inside:
            h.dev = ("dev-1", False, "g1")
        if inside:
            (h.root / "state.json").write_text(json.dumps(foreign))

    inproc.hook = hook
    inproc.run()
    assert json.loads((inproc.root / "state.json").read_text()) == foreign
    assert inproc.exit_records() == [] and not inproc.export_file().exists()


@pytest.fixture
def steps(inproc, monkeypatch):
    """Record the resource steps of a transition; failures are injected per step kind."""
    from djinn_in_a_box.core import docker as docker_module

    log = SimpleNamespace(calls=[], start=[], stop=[], resume=[], on_stop=None)

    def inspect_owned(record, generation, docker_path, **kwargs):
        return {"Id": record["id"], "State": {"Running": True}, "Config": {"Labels": {}}}

    def run(docker_path, *args, **kwargs):
        log.calls.append(("run", *args))
        if args[0] == "start" and log.start:
            raise log.start.pop(0)

    def teardown(docker_path, actual, *args, **kwargs):
        log.calls.append(("teardown", *args))
        if log.stop:
            error = log.stop.pop(0)
            if log.on_stop:
                log.on_stop()
            raise error

    def resume(*args):
        log.calls.append(("resume",))
        if log.resume:
            raise log.resume.pop(0)

    monkeypatch.setattr(host_runtime, "inspect_owned_resource", inspect_owned)
    monkeypatch.setattr(host_runtime, "run_runtime_command", run)
    monkeypatch.setattr(host_runtime, "_teardown_container", teardown)
    monkeypatch.setattr(docker_module, "resume_agent_docker", resume)
    return log


COMPANIONS = {
    "agent-docker": {"id": "agent-1", "service": "agent-docker"},
    "audio-helper": {"id": "helper-1", "service": "audio-helper"},
}


def flip(h, stopped_at, running_at=None):
    def hook(harness, inside):
        ticks = harness.clock.ticks
        if running_at is not None and ticks >= running_at:
            harness.dev = ("dev-1", True, "g1")
        elif ticks >= stopped_at:
            harness.dev = ("dev-1", False, "g1")

    h.hook = hook


def test_resumed_service_is_not_repeated_when_a_later_step_retries(inproc, steps):
    """S1: the agent-Docker resume succeeded; a helper start fails once and is retried."""
    inproc.prepare(resources=COMPANIONS)
    steps.start = [GitSSHError("operation refused")]
    flip(inproc, 4, 8)
    inproc.stop_at = 16
    inproc.run()
    assert steps.calls.count(("resume",)) == 1, steps.calls
    assert [c for c in steps.calls if c[:2] == ("run", "start")] == [
        ("run", "start", "helper-1")
    ] * 2


def test_stop_that_failed_is_retried_on_the_same_target(inproc, steps):
    """S2a: a stop step whose outcome is unclear is repeated while dev stays stopped."""
    inproc.prepare(resources=COMPANIONS)
    steps.stop = [subprocess.TimeoutExpired(["docker", "stop"], 5)]
    flip(inproc, 4)
    inproc.stop_at = 12
    inproc.run()
    first_helper_stop = steps.calls.index(("teardown", "stop", "helper-1"))
    stops = [c for c in steps.calls[:first_helper_stop] if c[-1] == "agent-1"]
    assert len(stops) == 2, steps.calls


def test_stop_that_took_effect_is_reconciled_when_dev_flips_back(inproc, steps):
    """S2b: the stop timed out after taking effect; dev runs again; the service is restarted."""
    inproc.prepare(resources=COMPANIONS)
    steps.stop = [subprocess.TimeoutExpired(["docker", "stop"], 5)]
    steps.on_stop = lambda: setattr(inproc, "dev", ("dev-1", True, "g1"))
    stopped = []

    def once(h, inside):
        if h.clock.ticks >= 4 and not stopped:
            stopped.append(True)
            h.dev = ("dev-1", False, "g1")

    inproc.hook = once
    inproc.stop_at = 12
    inproc.run()
    assert ("resume",) in steps.calls, steps.calls
    state = inproc.state()
    assert state is not None and "observer_exit" not in state


class Agentless:
    """A scripted ``agent_keys``: a list of results (frozenset or an exception) per call."""

    def __init__(self, results, default=frozenset()):
        self.results, self.default, self.count = list(results), default, 0

    def __call__(self, path):
        self.count += 1
        result = self.results.pop(0) if self.results else self.default
        if isinstance(result, BaseException):
            raise result
        return result


def assert_channels(h, code, *, export=True):
    state = h.state()
    assert state is not None
    cause = state["observer_exit"]
    assert set(cause) == {"code", "detail", "time"} and cause["code"] == code
    assert host_runtime.OBSERVER_EXIT_CAUSES[code] in cause["detail"]
    assert len(cause["detail"]) <= 512
    from datetime import datetime

    assert datetime.fromisoformat(cause["time"]).utcoffset().total_seconds() == 0
    records = h.exit_records()
    assert [r["observed"]["state"] for r in records] == [code]
    assert records[0]["trigger"] == "observer-exit" and records[0]["runtime_generation"] == "g1"
    target = h.export_file()
    if export:
        lines = target.read_text().splitlines()
        assert lines[1] == host_runtime.OBSERVER_EXIT_CAUSES[code]
        assert lines[2] == "Recover from the host: djinn clean, then djinn start."
        assert datetime.fromisoformat(lines[0]).utcoffset().total_seconds() == 0
        assert target.stat().st_mode & 0o777 == 0o600
        assert not target.with_name(target.name + ".tmp").exists()
        assert str(h.root) not in target.read_text()
    else:
        assert not target.exists()
    return cause


@pytest.fixture
def gitted(inproc, monkeypatch):
    inproc.prepare(git=True, agent=True)
    keys = Agentless([])
    monkeypatch.setattr(host_runtime, "agent_keys", keys)
    inproc.keys = keys
    return inproc


def test_transient_agent_read_failure_is_retried(gitted):
    """A1."""
    gitted.keys.results = [OSError("Connection reset"), EOFError(), GitSSHError("garbled")]
    gitted.stop_at = 8
    gitted.run()
    assert gitted.keys.count >= 6
    assert gitted.exit_records() == [] and not gitted.export_file().exists()
    state = gitted.state()
    assert state is not None and "observer_exit" not in state


def test_changed_key_set_fails_closed_through_every_channel(gitted):
    """A2: E5."""
    gitted.keys.results = [frozenset(), frozenset(), frozenset({b"unexpected"})]
    gitted.stop_at = 40
    gitted.run()
    assert gitted.clock.ticks < 40
    assert_channels(gitted, "key-set-changed")
    assert not (gitted.root / "export/auth.sock").exists()
    assert gitted.agent.poll() is not None  # the dedicated agent was stopped


@pytest.mark.parametrize("how", ["zombie", "absent"])
def test_agent_death_ends_loudly(gitted, how):
    """A3: E6."""
    gitted.agent.kill()
    if how == "absent":
        gitted.agent.wait(timeout=5)
    gitted.keys.default = OSError("No such file or directory")
    gitted.stop_at = 40
    gitted.run()
    assert gitted.clock.ticks < 40
    assert_channels(gitted, "agent-exited")


def test_agent_read_failure_with_other_birth_token_is_confirmed_death(gitted):
    """A3b: the process answers /proc but is not our agent (token differs)."""
    state = gitted.state()
    state["agent_token"] = "1"
    (gitted.root / "state.json").write_text(json.dumps(state))
    gitted.keys.default = OSError("Connection refused")
    gitted.stop_at = 40
    gitted.run()
    assert_channels(gitted, "agent-exited")


def test_agent_read_failure_with_matching_token_is_retried(gitted):
    gitted.keys.default = OSError("Connection refused")
    gitted.stop_at = 12
    gitted.run()
    assert gitted.clock.ticks >= 12 and gitted.exit_records() == []


def test_unreadable_proc_is_unknown_not_death(gitted, monkeypatch):
    """A4: an injected /proc read failure on a live agent is not E6."""
    target = f"/proc/{gitted.agent.pid}/stat"
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        if str(self) == target:
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    gitted.keys.default = OSError("Connection refused")
    gitted.stop_at = 12
    gitted.run()
    monkeypatch.undo()
    assert gitted.clock.ticks >= 12 and gitted.exit_records() == []


def test_process_alive_is_tri_state(tmp_path, monkeypatch):
    token = host_runtime.process_token(os.getpid())
    assert host_runtime.process_alive(os.getpid(), token) is True
    assert host_runtime.process_alive(os.getpid(), "other") is False
    assert host_runtime.process_alive(2**22 + 12345, token) is False
    real = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied"))
        if str(self) == f"/proc/{os.getpid()}/stat" else real(self, *a, **k),
    )
    assert host_runtime.process_alive(os.getpid(), token) is None


def kill_filters(h, *, after=0, jump=0.0, limit=None):
    """Hook: end the newest filter thread as soon as it runs (its listener closed under it)."""
    kills = []

    def hook(harness, inside):
        if harness.clock.ticks < after or not harness.workers or inside:
            return
        worker = harness.workers[-1]
        if worker.is_alive() and (limit is None or len(kills) < limit):
            kills.append(harness.clock.ticks)
            harness.servers[-1].listener.close()
            worker.join(timeout=5)
            assert not worker.is_alive()
            harness.clock.now += jump

    h.hook = hook
    return kills


@pytest.fixture
def quick_filter(monkeypatch):
    monkeypatch.setattr(git_agent, "SERVE_FAILURES", 2)
    monkeypatch.setattr(git_agent, "SERVE_BACKOFF", 0)


def rebind_stamps(capsys) -> list[str]:
    return [
        line.split(" ", 1)[0]
        for line in capsys.readouterr().err.splitlines()
        if line.endswith("observer: Git agent filter rebound")
    ]


def test_dead_filter_is_rebound_in_place_and_published(gitted, quick_filter, capsys):
    """F3/F5: rebound at the same path, old clients ended, semaphore shared, time published."""
    kills = kill_filters(gitted, after=3, limit=1)
    gitted.stop_at = 12
    gitted.run()
    assert len(kills) == 1 and len(gitted.servers) == 2
    old, new = gitted.servers
    assert old.stop.is_set() and new.clients is old.clients
    assert new.identity != old.identity
    stamps = rebind_stamps(capsys)
    state = gitted.state()
    assert state is not None and state["filter_rebinds"] == stamps and len(stamps) == 1
    assert "observer_exit" not in state and gitted.exit_records() == []


def test_sixth_death_within_the_window_ends_loudly(gitted, quick_filter, capsys):
    """F4: E7 with every channel; the real record carries the exact rebind times."""
    kills = kill_filters(gitted, after=3)
    gitted.stop_at = 200
    gitted.run()
    assert len(kills) == 6 and len(gitted.servers) == 6  # initial + 5 rebinds
    cause = assert_channels(gitted, "filter-failed")
    stamps = rebind_stamps(capsys)
    assert len(stamps) == 5
    assert gitted.exit_records()[0]["detail"] == {"rebinds": stamps}
    assert "; filter rebound at " + ", ".join(stamps) in cause["detail"]
    assert not (gitted.root / "export/auth.sock").exists()


def test_rebinds_outside_the_window_do_not_add_up(gitted, quick_filter, capsys):
    """F4: the rolling window evicts old rebinds; only the last five times are published."""
    kills = kill_filters(gitted, after=3, jump=301)
    gitted.stop_at = 60
    gitted.run()
    assert len(kills) >= 8 and gitted.exit_records() == []
    state = gitted.state()
    assert state is not None and len(state["filter_rebinds"]) == 5
    assert state["filter_rebinds"] == rebind_stamps(capsys)[-5:]


def test_failed_rebinds_count_toward_the_bound(gitted, quick_filter, monkeypatch):
    """F4: a failed rebind counts; five of them make the next tick E7."""
    attempts = []
    recording = host_runtime._serve_filter

    def failing(root, allowed, clients=None):
        attempts.append(1)
        if len(attempts) > 1:
            raise OSError(24, "Too many open files")
        return recording(root, allowed, clients)

    monkeypatch.setattr(host_runtime, "_serve_filter", failing)
    kill_filters(gitted, after=3, limit=1)
    gitted.stop_at = 100
    gitted.run()
    assert len(attempts) == 6  # the initial filter and five failed rebinds
    assert gitted.clock.ticks < 100
    assert_channels(gitted, "filter-failed")


def test_failed_thread_start_during_rebind_strands_no_socket(gitted, quick_filter):
    """F7: the rebind's Thread.start fails once; its listener and path are gone, the next works."""
    seen = []

    def hook(h, inside):
        if inside:
            return
        if h.clock.ticks == 3:
            h.thread_failures = 1
            h.servers[-1].listener.close()
            h.workers[-1].join(timeout=5)
        elif h.clock.ticks == 4:
            seen.append((h.root / "export/auth.sock").exists())  # failed attempt left nothing
        elif h.clock.ticks == 9:
            seen.append(agent_keys(h.root / "export/auth.sock") == frozenset())

    gitted.hook = hook
    gitted.stop_at = 12
    gitted.run()
    assert seen == [False, True]
    assert len(gitted.servers) == 2  # the failed attempt never returned a filter
    state = gitted.state()
    assert state is not None and len(state["filter_rebinds"]) == 1


def failing_save(monkeypatch, *, once_with):
    real = host_runtime.save_state
    failures = []

    def save(root, state):
        if once_with in state and not failures:
            failures.append(1)
            raise OSError(28, "No space left on device")
        real(root, state)

    monkeypatch.setattr(host_runtime, "save_state", save)
    return failures


def test_rebind_time_survives_one_failed_state_write(gitted, quick_filter, monkeypatch, capsys):
    """F9: the next guarded tick publishes the same single timestamp; no second rebind."""
    failures = failing_save(monkeypatch, once_with="filter_rebinds")
    kill_filters(gitted, after=3, limit=1)
    gitted.stop_at = 14
    gitted.run()
    assert failures == [1] and len(gitted.servers) == 2
    stamps = rebind_stamps(capsys)
    state = gitted.state()
    assert len(stamps) == 1 and state is not None and state["filter_rebinds"] == stamps


def test_pending_rebind_never_touches_a_successor_generation(gitted, quick_filter, monkeypatch):
    failing_save(monkeypatch, once_with="filter_rebinds")
    kill_filters(gitted, after=3, limit=1)
    foreign = {"generation": "g2", "marker": "successor"}
    swapped = []
    kill = gitted.hook

    def hook(h, inside):
        kill(h, inside)
        if len(h.servers) == 2 and not swapped and not inside and h.clock.ticks > 4:
            swapped.append(True)
            (h.root / "state.json").write_text(json.dumps(foreign))

    gitted.hook = hook
    gitted.stop_at = 14
    gitted.run()
    assert swapped
    assert json.loads((gitted.root / "state.json").read_text()) == foreign
    assert gitted.exit_records() == []


def test_exit_while_dev_runs_keeps_state_and_never_clears_it(gitted):
    """X3: a terminal exit enters the finally with dev running; state.json stays."""
    gitted.keys.results = [frozenset({b"unexpected"})]
    gitted.stop_at = 40
    gitted.run()
    state = gitted.state()
    assert state is not None and state["observer_exit"]["code"] == "key-set-changed"


@pytest.mark.parametrize("other", ["id", "generation"])
@pytest.mark.parametrize("running", [True, False])
def test_replacement_keeps_state_cause_and_resources(inproc, steps, other, running):
    """X1/E3b: a replaced or relabelled dev is foreign, running or stopped."""
    inproc.prepare(resources=COMPANIONS)
    replacement = ("dev-2", running, "g1") if other == "id" else ("dev-1", running, "g9")
    inproc.hook = lambda h, inside: (
        setattr(h, "dev", replacement) if h.clock.ticks >= 4 else None
    )
    inproc.run()
    state = inproc.state()
    assert state is not None and set(state["resources"]) == set(COMPANIONS)
    assert_channels(inproc, "dev-replaced", export=False)
    assert steps.calls == []  # neither stopped nor removed: ownership is unknown


@pytest.mark.parametrize("other", ["id", "generation"])
@pytest.mark.parametrize("running", [True, False])
def test_git_only_replacement_keeps_state_and_cause(inproc, other, running):
    """X1/E3b without resources: the Git-only fallback never clears a replacement's state."""
    inproc.prepare(resources={})
    replacement = ("dev-2", running, "g1") if other == "id" else ("dev-1", running, "g9")
    inproc.hook = lambda h, inside: (
        setattr(h, "dev", replacement) if h.clock.ticks >= 4 else None
    )
    inproc.run()
    state = inproc.state()
    assert state is not None and state["resources"] == {}
    assert_channels(inproc, "dev-replaced", export=False)


def test_dev_absent_and_released_dev_clear_state_without_cause(inproc):
    """X1/E3a: dev absent is owned cleanup; no cause, no export, no observer-exit record."""
    inproc.prepare()
    inproc.hook = lambda h, inside: setattr(h, "dev", None) if h.clock.ticks >= 4 else None
    inproc.run()
    assert inproc.state() is None
    assert inproc.exit_records() == [] and not inproc.export_file().exists()


def test_state_gone_ends_quietly(inproc):
    """X1/E2."""
    inproc.prepare()
    inproc.hook = lambda h, inside: (
        (h.root / "state.json").unlink(missing_ok=True) if h.clock.ticks >= 4 else None
    )
    inproc.run()
    assert inproc.exit_records() == [] and not inproc.export_file().exists()


def test_stop_signal_is_a_clean_exit_without_cause(inproc):
    """X1/E1."""
    inproc.prepare()
    inproc.stop_at = 5
    inproc.run()
    assert inproc.exit_records() == [] and not inproc.export_file().exists()
    state = inproc.state()
    assert state is not None and "observer_exit" not in state


def test_companion_resume_failure_stops_dev_and_ends_loudly(inproc, steps):
    """X1/E8: the deliberate fail-closed path, also when the defensive stop fails."""
    inproc.prepare(resources=COMPANIONS)
    steps.resume = [RuntimeError("Agent Docker restart readiness timeout")]
    steps.stop = [None, None]  # the two companion stops during dev's own stop succeed
    steps.stop = []
    flip(inproc, 4, 8)
    inproc.run()
    assert ("teardown", "stop", "-t", "3", "dev-1") in steps.calls
    defensive = ("teardown", "stop", "-t", "3", "dev-1")
    assert steps.calls.index(("resume",)) < steps.calls.index(defensive)
    assert_channels(inproc, "companion-resume-failed", export=False)


def test_companion_resume_failure_with_failing_defensive_stop_still_ends(inproc, steps):
    inproc.prepare(resources=COMPANIONS)
    steps.resume = [RuntimeError("resume refused")]
    flip(inproc, 4, 8)
    real = host_runtime._teardown_container

    def teardown(docker_path, actual, *args, **kwargs):
        if args[:2] == ("stop", "-t") and args[-1] == "dev-1":
            raise GitSSHError("Docker unavailable")
        return real(docker_path, actual, *args, **kwargs)

    inproc._teardown = teardown
    pytest.MonkeyPatch().setattr(host_runtime, "_teardown_container", teardown)
    inproc.run()
    assert_channels(inproc, "companion-resume-failed", export=False)


def test_unexpected_error_keeps_the_traceback_and_ends_loudly(inproc, capsys):
    """X1/E9: re-raised after the final section; the export file carries no exception text."""
    inproc.prepare()

    def explode(h, inside):
        if h.clock.ticks >= 4:
            raise KeyError("secret-looking-detail")

    inproc.hook = explode
    with pytest.raises(KeyError):
        inproc.run()
    cause = assert_channels(inproc, "observer-error", export=False)
    assert "KeyError" in cause["detail"]


def test_final_cleanup_bounds_and_survives_a_busy_guard(gitted, monkeypatch):
    """X3: self-initiated exit retries the guard up to 30 s, then preserves; no exception."""
    gitted.keys.results = [frozenset(), frozenset({b"unexpected"})]
    gitted.busy_after = 3
    started = []
    real_keys = gitted.keys

    def keys(path):
        result = real_keys(path)
        if real_keys.count == 2:
            started.append(gitted.clock.now)
        return result

    monkeypatch.setattr(host_runtime, "agent_keys", keys)
    gitted.stop_at = 100
    gitted.run()
    elapsed = gitted.clock.now - started[0]
    assert 30 <= elapsed < 31.5, elapsed
    assert gitted.state()["generation"] == "g1"
    assert [r["observed"]["state"] for r in gitted.exit_records()] == ["key-set-changed"]
    assert "observer_exit" not in gitted.state()  # no guard: state and export stay untouched
    assert not gitted.export_file().exists()


def test_final_cleanup_after_a_stop_signal_waits_at_most_two_seconds(inproc):
    """X3: E1 tries the guard without waiting, within 2 s, and leaves the rest to the caller."""
    inproc.prepare()
    inproc.busy_after = 3
    inproc.stop_at = 5
    inproc.run()
    assert inproc.clock.now - 1000 < 5 * 0.25 + 2.5
    assert inproc.state() is not None
    timeouts = {g[1] for g in inproc.guards if g[2] >= 5}
    assert timeouts == {0}


def test_export_file_removed_by_clear_state_and_start_preparation(fake, git_inputs):
    """X3: clear_state and the start preparation remove the cause file and its temporary."""
    root = runtime_root(create=True)
    export = host_runtime.private_directory(root / "export")
    # Start preparation: the file is present and no state exists.
    for name in ("observer-exit", "observer-exit.tmp"):
        (export / name).write_text("stale")
    with git_runtime(git_inputs, "djinn") as runtime:
        assert not (export / "observer-exit").exists()
        assert not (export / "observer-exit.tmp").exists()
        generation = runtime.generation
        for name in ("observer-exit", "observer-exit.tmp"):
            (export / name).write_text("stale")
        # clear_state is generation-checked: a foreign generation leaves it alone.
        host_runtime.clear_state(root, "someone-else")
        assert (export / "observer-exit").exists()
        record = json.loads((root / "state.json").read_text())
        observer = runtime.observer
        if observer is not None:
            stop_owned_process(record["observer_pid"], record["observer_token"])
        stop_owned_process(record["agent_pid"], record["agent_token"])
        host_runtime.clear_state(root, generation)
    assert not (export / "observer-exit").exists()
    assert not (export / "observer-exit.tmp").exists()


def test_each_cause_writer_failing_leaves_the_others_and_cleanup(gitted, monkeypatch):
    """X2: the state write, the export write and the recorder fail independently."""
    gitted.keys.results = [frozenset({b"unexpected"})]
    real_save = host_runtime.save_state

    def save(root, state):
        if "observer_exit" in state:
            raise OSError(28, "No space left on device")
        real_save(root, state)

    monkeypatch.setattr(host_runtime, "save_state", save)
    monkeypatch.setattr(post_mortem, "record", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    gitted.stop_at = 40
    gitted.run()
    assert gitted.export_file().exists()  # the file does not depend on the state write
    assert "observer_exit" not in gitted.state()
    assert not (gitted.root / "export/auth.sock").exists()  # retire_path still ran


@pytest.mark.parametrize("failure", ["recorder-none", "export-unwritable"])
def test_other_writer_failures_keep_the_remaining_channels(gitted, monkeypatch, failure):
    if failure == "recorder-none":
        monkeypatch.setattr(post_mortem, "record", lambda *a, **k: None)
        gitted.keys.results = [frozenset({b"unexpected"})]
    else:
        def keys(path):
            (gitted.root / "export").chmod(0o500)  # the cause file cannot be written
            return frozenset({b"unexpected"})

        monkeypatch.setattr(host_runtime, "agent_keys", keys)
    gitted.stop_at = 40
    try:
        gitted.run()
    finally:
        (gitted.root / "export").chmod(0o700)
    state = gitted.state()
    assert state is not None and state["observer_exit"]["code"] == "key-set-changed"
    if failure == "recorder-none":
        assert gitted.export_file().exists() and gitted.exit_records() == []
    else:
        assert not gitted.export_file().exists() and len(gitted.exit_records()) == 1


def test_exit_never_writes_over_a_successor(gitted, monkeypatch):
    """X2: a successor generation's state and export directory stay untouched; the record stays."""
    foreign = {"generation": "g2", "marker": "successor"}

    def keys(path):
        (gitted.root / "state.json").write_text(json.dumps(foreign))
        return frozenset({b"unexpected"})

    monkeypatch.setattr(host_runtime, "agent_keys", keys)
    gitted.stop_at = 40
    gitted.run()
    assert json.loads((gitted.root / "state.json").read_text()) == foreign
    assert not gitted.export_file().exists()
    assert [r["observed"]["state"] for r in gitted.exit_records()] == ["key-set-changed"]


def test_observer_exit_uses_no_docker_command(gitted):
    """X2: the observed-only record needs no Docker call."""
    gitted.keys.results = [frozenset({b"unexpected"})]
    gitted.docker.write_text(
        f"#!/bin/sh\necho called >> {gitted.root}/docker-calls\n"
        "echo 'Error response from daemon: No such container: x' >&2\nexit 1\n"
    )
    gitted.stop_at = 40
    gitted.run()
    assert not (gitted.root / "docker-calls").exists()
    assert len(gitted.exit_records()) == 1


# ---------------------------------------------------------------------------------------------
# AgentFilter in isolation.
# ---------------------------------------------------------------------------------------------


class ScriptedListener:
    """The real listener with a scripted accept(): errors, streams, or parking on a gate."""

    def __init__(self, real, script, flaky) -> None:
        self.real, self.script, self.flaky = real, list(script), flaky
        self.gate = threading.Event()
        self.reached = threading.Event()
        self.peers: list[socket.socket] = []

    def accept(self):
        if not self.script:
            return self.real.accept()
        step = self.script.pop(0)
        if step == "oserror":
            raise OSError(24, "Too many open files")
        if step in {"stream", "stream-fail"}:
            ours, theirs = socket.socketpair()
            self.peers.append(theirs)
            if step == "stream-fail":
                self.flaky.failing += 1
            return ours, None
        self.reached.set()
        self.gate.wait(10)
        raise TimeoutError

    def close(self):
        self.real.close()

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.fixture
def filters(tmp_path, monkeypatch):
    flaky = SimpleNamespace(failing=0)

    class FlakyThread(threading.Thread):
        def start(self):
            if flaky.failing:
                flaky.failing -= 1
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(git_agent, "threading", Shim(threading, Thread=FlakyThread))
    monkeypatch.setattr(git_agent, "SERVE_BACKOFF", 0)
    made = []

    def build(name="auth.sock", script=(), clients=None):
        directory = Path(tempfile.mkdtemp(prefix="djinn-f-"))
        server = git_agent.AgentFilter(directory / name, directory / "agent.sock",
                                       frozenset(), clients)
        server.listener = ScriptedListener(server.listener, script, flaky)
        errors: list[BaseException] = []

        def serve():
            try:
                server.serve()
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        made.append((server, thread))
        return server, thread, errors

    yield build
    for server, thread in made:
        server.stop.set()
        server.listener.gate.set()
        thread.join(timeout=5)
        server.retire_path()


def test_filter_survives_one_accept_failure(filters):
    """F1."""
    server, thread, errors = filters(script=["oserror"])
    thread.start()
    wait_for(lambda: not server.listener.script, 5)
    assert agent_keys(server.exported) == frozenset()
    assert thread.is_alive() and errors == []


def test_filter_survives_a_failed_client_thread_start(filters):
    """F2: the stream is closed, the slot released, the next client is served."""
    server, thread, errors = filters(script=["stream-fail"])
    thread.start()
    wait_for(lambda: server.listener.peers, 5)
    assert server.listener.peers[0].recv(1) == b""  # closed (after the slot was taken)
    wait_for(lambda: server.clients._value == 16, 5, "the client slot was not released")
    assert agent_keys(server.exported) == frozenset()
    assert thread.is_alive() and errors == []


def test_consecutive_failure_counter(filters):
    """F8: one counter, reset only by a successful dispatch, ending the thread at 50."""
    server, thread, errors = filters(
        script=["oserror"] * 25 + ["stream-fail"] * 24 + ["stream"]
        + ["oserror"] * 25 + ["stream-fail"] * 24 + ["wait"]
    )
    thread.start()
    assert server.listener.reached.wait(10), "the filter ended before the script was consumed"
    assert thread.is_alive() and errors == []
    server.listener.gate.set()
    server.stop.set()
    thread.join(timeout=5)
    for peer in server.listener.peers:
        peer.close()
    stopped, stopped_thread, ended = filters(script=["oserror"] * 25 + ["stream-fail"] * 25)
    stopped_thread.start()
    stopped_thread.join(timeout=5)
    assert not stopped_thread.is_alive() and len(ended) == 1
    assert all(peer.recv(1) == b"" for peer in stopped.listener.peers)
    assert stopped.clients._value == 16, "a slot leaked on a failed thread start"


def test_retiring_filter_never_unlinks_its_successors_socket(filters):
    """F6: serve() leaves the path; only a guarded owner removes it, by inode."""
    old, old_thread, _ = filters(script=["wait"])
    old_thread.start()
    assert old.listener.reached.wait(5)
    old.stop.set()  # the old thread is still parked inside accept()
    old.retire_path()
    successor = git_agent.AgentFilter(old.exported, old.private, frozenset())
    served = threading.Thread(target=successor.serve, daemon=True)
    served.start()
    try:
        old.listener.gate.set()  # barrier: the old thread exits only after the successor bound
        old_thread.join(timeout=5)
        assert not old_thread.is_alive()
        assert old.exported.exists() and old.exported.lstat().st_ino == successor.identity[1]
        assert agent_keys(old.exported) == frozenset()
        old.retire_path()  # a stale instance must not remove the successor either
        assert old.exported.exists()
    finally:
        successor.stop.set()
        served.join(timeout=5)
        successor.retire_path()


def test_failed_setup_after_bind_leaves_no_socket(tmp_path, monkeypatch):
    """F7: a post-bind failure closes the listener and unlinks only the new inode."""
    directory = Path(tempfile.mkdtemp(prefix="djinn-f-"))
    path = directory / "auth.sock"
    real = Path.chmod

    def chmod(self, mode, **kwargs):
        if self == path:
            raise PermissionError(1, "Operation not permitted")
        return real(self, mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", chmod)
    with pytest.raises(PermissionError):
        git_agent.AgentFilter(path, directory / "agent.sock", frozenset())
    assert not path.exists()
    monkeypatch.undo()
    working = git_agent.AgentFilter(path, directory / "agent.sock", frozenset())
    assert path.exists()
    working.listener.close()
    working.retire_path()


def test_control_guard_timeouts_and_busy_type(git_inputs, monkeypatch):
    """L3b: timeout=0 refuses at once without a holder lookup; the default stays 15 s."""
    clock = Clock()
    monkeypatch.setattr(hostctl, "time", Shim(time, monotonic=clock.monotonic, sleep=clock.sleep))
    described = []
    monkeypatch.setattr(
        hostctl, "describe_holders", lambda path: described.append(path) or "held by test"
    )
    lock = hostctl.state_root() / "control.lock"
    holder = hold(lock, 60)
    try:
        start = clock.now
        with pytest.raises(hostctl.HostctlBusy) as caught, hostctl.control_guard(timeout=0):
            pass
        assert clock.now == start and described == []
        assert str(caught.value) == "host control is busy; retry after the transition completes"
        with pytest.raises(hostctl.HostctlBusy, match="held by test"), hostctl.control_guard():
            pass
        assert 15 <= clock.now - start < 15.2 and described == [lock]
    finally:
        holder.terminate()
        holder.wait(timeout=5)
    with hostctl.control_guard(), hostctl.control_guard(timeout=0):  # reentrant, no lock needed
        pass
    assert issubclass(hostctl.HostctlBusy, hostctl.HostctlError)


def test_dev_stop_with_companions_retires_the_exported_socket(gitted, steps):
    """Dev stops while companions keep the observer alive: no Git delivery, no stale socket."""
    state = gitted.state()
    state["resources"] = COMPANIONS
    (gitted.root / "state.json").write_text(json.dumps(state))
    seen = []

    def hook(h, inside):
        if h.clock.ticks >= 4:
            h.dev = ("dev-1", False, "g1")
        if h.clock.ticks == 8:  # the observer lives on: the path must already be retired
            seen.append((h.root / "export/auth.sock").exists())

    gitted.hook = hook
    gitted.stop_at = 10
    gitted.run()
    assert seen == [False]
    assert gitted.agent.poll() is not None


def test_replacement_with_git_removes_the_socket_but_writes_no_export_file(gitted):
    """E3b: ownership of dev is unknown; Git delivery ends, the cause file is not written."""
    gitted.hook = lambda h, inside: (
        setattr(h, "dev", ("dev-2", True, "g1")) if h.clock.ticks >= 4 else None
    )
    gitted.stop_at = 40
    gitted.run()
    assert_channels(gitted, "dev-replaced", export=False)
    assert not (gitted.root / "export/auth.sock").exists()
