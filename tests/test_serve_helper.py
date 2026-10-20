"""Tests of the in-container ``djinn serve`` helper (src/djinn_in_a_box/core/serve_helper.py).

Spawn, supervise, stop and readiness run in production shape: ``python -I -c <source> <subcommand>``
as a subprocess with the test-only state-dir override and a fake ``HOME``/``PATH`` (fake
``opencode`` and ``claude`` scripts). Pure functions and the signal floor are imported by file path.
The daemons the helper leaves behind are re-parented to this test process (child subreaper), found
by walking /proc and reaped at teardown. The real ``/tmp/djinn-serve`` is never touched.
"""

from __future__ import annotations

import ast
import contextlib
import ctypes
import fcntl
import importlib.util
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HELPER = Path(__file__).resolve().parents[1] / "src/djinn_in_a_box/core/serve_helper.py"
SOURCE = HELPER.read_text(encoding="utf-8")
REAL_STATE_DIR = "/tmp/djinn-serve"  # noqa: S108 - asserted never to be touched
PR_SET_CHILD_SUBREAPER = 36
TEXT_REFUSAL = "Error: You must be logged in to use Remote Control."
Json = dict[str, Any]


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("serve_helper_under_test", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = load_module()


# --------------------------------------------------------------------------- fakes

FAKE_OPENCODE = """#!/usr/bin/python3
import json, os, signal, socket, subprocess, sys, time
fake = os.environ["FAKE_DIR"]
args = sys.argv[1:]
with open(os.path.join(fake, "opencode-calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": args, "pid": os.getpid(), "cwd": os.getcwd(),
                         "environ": dict(os.environ)}) + "\\n")
mode = os.environ.get("FAKE_OC_MODE", "normal")
if args[:1] == ["service"]:
    print("stopped")
    sys.exit(0)
if args[:1] == ["pair"]:
    print("pairing links")
    sys.exit(0)
if args[:2] != ["serve", "--service"]:
    sys.exit(2)
port = int(args[args.index("--port") + 1])
if mode == "exit-early":
    print("\\x1b[31mError: address in use\\x1b[0m", flush=True)
    sys.exit(1)
listener = None
if mode != "nolisten":
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args[args.index("--hostname") + 1], port))
    listener.listen(5)
disc = int(os.environ.get("FAKE_OC_DISCOVERY_PID") or os.getpid())
state = os.path.join(os.environ["HOME"], ".local/state/opencode")
os.makedirs(state, exist_ok=True)
with open(os.path.join(state, "service.json"), "w") as fh:
    json.dump({"pid": disc, "url": "http://127.0.0.1:%d" % port}, fh)
if mode == "grandchild":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c",
        "import signal, time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "while True: time.sleep(0.2)"])
    with open(os.path.join(fake, "grandchild.pid"), "w") as fh:
        fh.write(str(child.pid))
ctl = os.path.join(fake, "oc-ctl")
while True:
    if listener is not None and os.path.exists(ctl):
        listener.close()
        listener = None
    time.sleep(0.1)
"""

FAKE_CLAUDE = """#!/usr/bin/python3
import fcntl, json, os, signal, struct, subprocess, sys, termios, time
fake = os.environ["FAKE_DIR"]
mode = os.environ.get("FAKE_CLAUDE_MODE", "normal")
try:
    size = list(struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\\0" * 8)))
except OSError:
    size = None
with open(os.path.join(fake, "claude-calls.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
                         "tty": os.isatty(0), "winsize": size, "pgid": os.getpgrp(),
                         "sid": os.getsid(0)}) + "\\n")
if mode == "refusal":
    print("%s" % os.environ["FAKE_CLAUDE_TEXT"], flush=True)
    sys.exit(1)
if mode == "signal-group":
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    code = ("import os, signal, time\\n"
            "signal.signal(signal.SIGHUP, signal.SIG_IGN)\\n"
            "open(%r, 'w').write(str(os.getpid()))\\nwhile True: time.sleep(0.2)" %
            os.path.join(fake, "grandchild.pid"))
    subprocess.Popen([sys.executable, "-c", code])
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    code = ("import os, signal, time\\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
            "signal.signal(signal.SIGHUP, signal.SIG_IGN)\\n"
            "open(%r, 'w').write(str(os.getpid()))\\nwhile True: time.sleep(0.2)" %
            os.path.join(fake, "grandchild.pid"))
    subprocess.Popen([sys.executable, "-c", code])
if mode == "flood":
    line = ("x" * 99 + "\\n").encode()
    for _ in range(30000):
        os.write(1, line)
    os.write(1, b"LAST-LINE-MARKER\\n")
else:
    print("Remote Control ready", flush=True)
while True:
    time.sleep(0.2)
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def descendants() -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            fields = mod.proc_stat(int(entry))
            if fields is not None:
                children.setdefault(int(fields[1]), []).append(int(entry))
    found: list[int] = []
    queue = [os.getpid()]
    while queue:
        for child in children.get(queue.pop(), []):
            found.append(child)
            queue.append(child)
    return found


@pytest.fixture(autouse=True)
def subreaper() -> Iterator[None]:
    """Daemons spawned by a helper subprocess re-parent to this process; reap them at teardown."""
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    try:
        yield
    finally:
        for _ in range(40):
            pids = descendants()
            if not pids:
                break
            for pid in pids:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            for pid in pids:
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
            time.sleep(0.05)
        libc.prctl(PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0)


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def is_gone(pid: int) -> bool:
    return bool(wait_for(lambda: mod.proc_start(pid) is None, 8.0))


def is_alive(pid: int) -> bool:
    return mod.proc_start(pid) is not None


@dataclass
class Ctx:
    root: Path
    home: Path
    state: Path
    fake: Path
    work: Path
    env: dict[str, str]
    port: int = field(default_factory=free_port)

    def install(self, tool: str) -> None:
        script = {"opencode": FAKE_OPENCODE, "claude": FAKE_CLAUDE}[tool]
        path = self.fake / "bin" / tool
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)

    def run(
        self,
        *args: str,
        stdin: str | None = None,
        timing: str | None = None,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
        source: str = SOURCE,
    ) -> tuple[int, Json]:
        merged = {**self.env, **(env or {})}
        if timing is not None:
            merged["DJINN_SERVE_TEST_TIMING"] = timing
        proc = subprocess.run(
            [sys.executable, "-I", "-c", source, *args],
            input=stdin if stdin is not None else "",
            capture_output=True,
            text=True,
            env=merged,
            cwd=self.work,
            timeout=timeout,
            check=False,
        )
        lines = proc.stdout.splitlines()
        assert len(lines) == 1, (proc.stdout, proc.stderr)
        self.last_raw = proc.stdout + proc.stderr
        return proc.returncode, json.loads(lines[0])

    last_raw: str = ""

    def status(self, **kw: Any) -> Json:
        rc, out = self.run("status", **kw)
        assert rc == 0, out
        return out

    def claude(self, timing: str = "claude_grace=0.4", **kw: Any) -> tuple[int, Json]:
        return self.run("claude-start", "--prefix", "my-host", timing=timing, **kw)

    def opencode(
        self,
        password: str = "pw-secret-1",
        *,
        cors: str | None = None,
        timing: str = "opencode_ready=6,stop_wait=3",
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> tuple[int, Json]:
        args = ["opencode-start", "--port", str(self.port)]
        if cors is not None:
            args += ["--cors", cors]
        body = stdin if stdin is not None else json.dumps({"password": password})
        return self.run(*args, stdin=body, timing=timing, env=env)

    def stop(self, *names: str, timing: str = "stop_wait=2,kill_wait=2") -> tuple[int, Json]:
        args: list[str] = []
        for name in names:
            args += ["--name", name]
        return self.run("stop", *args, timing=timing)

    def calls(self, tool: str) -> list[Json]:
        path = self.fake / f"{tool}-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def record(self, name: str) -> Json:
        return json.loads((self.state / f"{name}.json").read_text())

    def service_json(self) -> Path:
        return self.home / ".config/opencode/service.json"


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Ctx]:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "root"
    home = root / "home"
    fake = root / "fake"
    state = root / "state"
    work = root / "work"
    for directory in (home, fake / "bin", work):
        directory.mkdir(parents=True)
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    env = {
        "PATH": f"{fake / 'bin'}:/usr/bin:/bin",
        "HOME": str(home),
        "DJINN_SERVE_TEST_STATE_DIR": str(state),
        "DJINN_SERVE_TEST_HOME": str(home),
        "FAKE_DIR": str(fake),
        "FAKE_CLAUDE_TEXT": TEXT_REFUSAL,
    }
    assert str(state) != REAL_STATE_DIR and str(state).startswith(str(tmp_path))
    before = os.stat(REAL_STATE_DIR).st_mtime_ns if os.path.isdir(REAL_STATE_DIR) else None
    context = Ctx(root, home, state, fake, work, env)
    context.install("opencode")
    context.install("claude")
    yield context
    after = os.stat(REAL_STATE_DIR).st_mtime_ns if os.path.isdir(REAL_STATE_DIR) else None
    assert before == after, "the real /tmp/djinn-serve was touched"


# ------------------------------------------------------------------- source / schema


def test_helper_source_limits_and_python_311() -> None:
    assert len(SOURCE.encode()) <= 100 * 1024
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("djinn_in_a_box") for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("djinn_in_a_box")
    debian = Path("/usr/bin/python3")
    if debian.exists():
        probe = subprocess.run(
            [
                str(debian),
                "-I",
                "-c",
                "import sys; compile(open(sys.argv[1]).read(), 'h', 'exec')",
                str(HELPER),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert probe.returncode == 0, probe.stderr


def test_protocol_enums_are_frozen() -> None:
    assert mod.STATES == ("running", "degraded", "exited", "stopped")
    assert mod.START_RESULTS == ("started", "already-running", "restarted", "exited")
    assert mod.STOP_RESULTS == ("stopped", "not-running", "survived")
    assert mod.ERROR_CODES == (
        "state-dir",
        "state-path",
        "env-root",
        "password",
        "service-json",
        "service-config",
        "remote-config",
        "spawn",
        "not-registered",
        "busy",
        "timeout",
        "usage",
    )
    assert mod.NAMES == ("claude", "opencode")


def test_source_has_no_service_set_stop_or_password_plumbing() -> None:
    """No ``opencode service set|stop`` argv, no ``--remote``, no env handed to an exec."""
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple):
            words = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            for index, word in enumerate(words[:-1]):
                assert not (word == "service" and words[index + 1] in ("set", "stop", "unset"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value != "--remote"
            assert "--password" not in node.value
        if isinstance(node, ast.Call):
            args = [
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            for index, word in enumerate(args[:-1]):
                assert not (word == "service" and args[index + 1] in ("set", "stop", "unset"))
            func = ast.unparse(node.func)
            assert func not in ("os.execvpe", "os.execve", "os.putenv")
            assert not func.startswith("subprocess.")
            assert not any(k.arg == "env" for k in node.keywords)
    assert "OPENCODE_PASSWORD" not in SOURCE and "OPENCODE_SERVER_PASSWORD" not in SOURCE


# ------------------------------------------------------------------ pure functions


def test_tail_strips_ansi_and_caps_lines(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    noisy = "\x1b[31mred\x1b[0m [/x] [link=http://x]\r\n\x1b]0;title\x07tab\there\x00nul\x07bell\n"
    log.write_bytes(
        (noisy + "".join(f"line {i}\x1b[2K\n" for i in range(40)) + "\x1b\x1b[Zend\n").encode()
    )
    tail = mod.sanitize_tail(str(log))
    assert len(tail) == 20 and tail[-1] == "end"
    assert tail[-2] == "line 39"
    flat = "\n".join(mod.sanitize_tail(str(log), 100))
    assert "\x1b" not in flat and "\x00" not in flat and "\x07" not in flat
    short = tmp_path / "y.log"
    short.write_bytes(noisy.encode())
    lines = mod.sanitize_tail(str(short))
    assert lines[0] == "red [/x] [link=http://x]"
    assert "tab here" in lines[1] and "nulbell" in lines[1]
    odd = tmp_path / "odd.log"
    odd.write_text("a" + chr(0x202E) + "b" + chr(0x200B) + "c" + chr(0x2028) + "d\n")
    assert mod.sanitize_tail(str(odd)) == ["abcd"]
    assert mod.sanitize_tail(str(tmp_path / "missing.log")) == []
    big = tmp_path / "big.log"
    big.write_bytes(b"A" * 200000 + b"\nlast\n")
    assert mod.sanitize_tail(str(big)) == ["last"]


def test_record_codec_roundtrip_and_rejections() -> None:
    record = mod.Record(
        pid=5,
        start="12",
        pid1_start="3",
        cwd="/w",
        argv_tag="t",
        started_at="2026-01-01T00:00:00Z",
        child_pid=6,
        child_start="13",
        port=4096,
        cors="https://box.example.ts.net",
    )
    assert mod.decode_record(mod.encode_record(record)) == record
    good = json.loads(mod.encode_record(record))
    for key, value in (
        ("pid", True),
        ("pid", "5"),
        ("start", "x1"),
        ("pid1_start", 3),
        ("cwd", 1),
        ("child_start", None),
        ("port", "4096"),
        ("cors", True),
        ("cors", 42),
        ("cors", []),
        ("cors", ""),
        ("cors", "https://box.example.ts.net/path"),
        ("cors", "https://box.example.ts.net\n"),
    ):
        assert mod.decode_record(json.dumps({**good, key: value}).encode()) is None, key
    assert mod.decode_record(b"[1]") is None and mod.decode_record(b"\xff") is None
    for old in ({k: v for k, v in good.items() if k != "cors"}, {**good, "cors": None}):
        decoded = mod.decode_record(json.dumps(old).encode())
        assert decoded is not None and decoded.cors is None


@pytest.mark.parametrize("location", ["config", "discovery", "record"])
def test_nofollow_fifo_returns_structured_error(ctx: Ctx, location: str) -> None:
    source = SOURCE
    if location == "record":
        path = ctx.state / "claude.json"
        command, code = "status", "state-dir"
    elif location == "config":
        path = ctx.service_json()
        command, code = "opencode-preflight", "service-json"
    else:
        path = ctx.home / ".local/state/opencode/service.json"
        command, code = "status", "service-json"
        # Exercise the discovery reader through the helper's normal error encoder.
        source = (
            SOURCE.rsplit('if __name__ == "__main__":', 1)[0]
            + """
dispatch = lambda argv, stdin: read_discovery(opencode_home())
sys.exit(main(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout))
"""
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path, 0o600)
    rc, out = ctx.run(command, source=source, timeout=3)
    assert rc == 1 and out["ok"] is False and out["code"] == code, out
    assert "not a regular file" in out["message"]


def test_merge_password_keeps_keys_mode_and_refuses_symlink(tmp_path: Path) -> None:
    home = tmp_path / "h"
    home.mkdir()
    config = home / ".config/opencode"
    config.mkdir(parents=True)
    (config / "service.json").write_text(json.dumps({"password": "old", "other": [1]}))
    mod.merge_password(str(home), "new-pw")
    data = json.loads((config / "service.json").read_text())
    assert data == {"password": "new-pw", "other": [1]}
    assert stat.S_IMODE((config / "service.json").stat().st_mode) == 0o600
    assert [p.name for p in config.iterdir()] == ["service.json"]
    assert mod.stored_password_equals(str(home), "new-pw")
    assert not mod.stored_password_equals(str(home), "old")
    outside = tmp_path / "outside.json"
    outside.write_text('{"password": "x"}')
    (config / "service.json").unlink()
    (config / "service.json").symlink_to(outside)
    with pytest.raises(mod.HelperError) as caught:
        mod.merge_password(str(home), "p")
    assert caught.value.code == "state-path"
    assert outside.read_text() == '{"password": "x"}'
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    mod.merge_password(str(fresh), "pw")
    assert json.loads((fresh / ".config/opencode/service.json").read_text()) == {"password": "pw"}
    assert not mod.stored_password_equals(str(tmp_path / "nohome"), "pw")


def test_group_liveness_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    stats: dict[int, list[str]] = {}
    members: list[int] = []

    def fields(state: str, start: str) -> list[str]:
        return [state] + ["0"] * 18 + [start]

    def fake_stat(pid: int) -> list[str] | None:
        return stats.get(pid)

    def fake_members(_pgid: int) -> list[int]:
        return list(members)

    monkeypatch.setattr(mod, "proc_stat", fake_stat)
    monkeypatch.setattr(mod, "group_members", fake_members)
    stats[10] = fields("S", "100")
    assert mod.group_state(10, "100") == "alive"
    assert mod.group_state(10, "99") == "stale"  # pid reused: never signalled
    stats.clear()
    assert mod.group_state(10, "100") == "gone"
    members.append(150)  # leader gone, a member started after the record
    assert mod.group_state(10, "100") == "alive"
    members[:] = [50]  # only older processes: not ours
    assert mod.group_state(10, "100") == "gone"
    stats[10] = fields("Z", "100")  # a zombie leader counts as gone
    members[:] = []
    assert mod.group_state(10, "100") == "gone"
    members[:] = [150]
    for state in ("Z", "X"):
        stats[10] = fields(state, "100")
        assert mod.group_state(10, "100") == "alive"
        assert mod.group_state(10, "99") == "stale"
    stats[10] = ["Z"]
    assert mod.group_state(10, "100") == "stale"


CASES: list[tuple[str, bool, dict[int, str], bool, int | None, str, str | None]] = [
    # name, same pid1, group states by pid, listening, discovery pid, state, reason
    ("claude", True, {1: "alive", 2: "alive"}, False, None, "running", None),
    ("claude", True, {1: "alive", 2: "gone"}, False, None, "degraded", "child gone"),
    ("claude", True, {1: "gone", 2: "alive"}, False, None, "degraded", "supervisor gone"),
    ("claude", True, {1: "gone", 2: "gone"}, False, None, "exited", None),
    ("claude", True, {1: "alive", 2: "stale"}, False, None, "stopped", "stale record"),
    ("claude", True, {1: "stale", 2: "alive"}, False, None, "stopped", "stale record"),
    ("claude", False, {1: "alive", 2: "alive"}, False, None, "stopped", "stale record"),
    ("opencode", True, {1: "alive"}, True, 1, "running", None),
    ("opencode", True, {1: "alive"}, False, 1, "degraded", "listener lost"),
    ("opencode", True, {1: "alive"}, True, None, "degraded", "not registered"),
    ("opencode", True, {1: "alive"}, True, 77, "degraded", "registered to another process"),
    ("opencode", True, {1: "gone"}, False, None, "exited", None),
    ("opencode", True, {1: "stale"}, True, 1, "stopped", "stale record"),
    ("opencode", False, {1: "alive"}, True, 1, "stopped", "stale record"),
]


@pytest.mark.parametrize(("name", "same", "groups", "listening", "disc", "state", "reason"), CASES)
def test_state_function_is_total(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    same: bool,
    groups: dict[int, str],
    listening: bool,
    disc: int | None,
    state: str,
    reason: str | None,
) -> None:
    def fake_pid1() -> str:
        return "100" if same else "999"

    def fake_group(pid: int, _start: str) -> str:
        return groups[pid]

    def fake_listening(_pid: int, _port: int) -> bool:
        return listening

    def fake_discovery(_home: str) -> int | None:
        return disc

    def fake_foreign(_pid: int | None, _port: int) -> bool:
        return False

    monkeypatch.setattr(mod, "pid1_start", fake_pid1)
    monkeypatch.setattr(mod, "group_state", fake_group)
    monkeypatch.setattr(mod, "listener_ours", fake_listening)
    monkeypatch.setattr(mod, "discovery_pid", fake_discovery)
    monkeypatch.setattr(mod, "foreign_listener", fake_foreign)
    record = mod.Record(
        pid=1,
        start="5",
        pid1_start="100",
        cwd="/",
        argv_tag="t",
        started_at="t",
        child_pid=2 if name == "claude" else None,
        child_start="6" if name == "claude" else None,
        port=4096 if name == "opencode" else None,
    )
    ev = mod.evaluate(name, record, "/h")
    assert (ev.state, ev.reason) == (state, reason)
    assert ev.state in mod.STATES
    assert mod.evaluate(name, None, "/h").state == "stopped"


def test_deadline_cuts_a_step_that_outlives_it(monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock:
        now = 1000.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(mod, "time", clock)
    assert mod.Deadline(100).wait(1, lambda: False) is False  # own bound first: plain False
    with pytest.raises(mod.DeadlineError):
        mod.Deadline(1).wait(10, lambda: False)  # cut by the deadline
    assert mod.Deadline(1).wait(10, lambda: True) is True


def test_refuses_to_signal_pid1_and_own_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """In-process: os.kill/os.killpg are recorders; pid 1 and our own group are refused."""
    sent: list[tuple[str, int, int]] = []

    def record_kill(pid: int, sig: int) -> None:
        sent.append(("kill", pid, sig))

    def record_killpg(pgid: int, sig: int) -> None:
        sent.append(("killpg", pgid, sig))

    monkeypatch.setattr(mod.os, "kill", record_kill)
    monkeypatch.setattr(mod.os, "killpg", record_killpg)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setenv(mod.TEST_STATE_ENV, str(state))
    leader = os.getpgrp()
    for pid in (1, leader):
        start = mod.raw_start(pid)
        assert start is not None
        record = mod.Record(
            pid=pid,
            start=start,
            pid1_start=mod.pid1_start(),
            cwd="/",
            argv_tag="t",
            started_at="t",
            port=4096,
        )
        (state / "opencode.json").write_bytes(mod.encode_record(record))
        out = io.StringIO()
        code = mod.main(["stop", "--name", "opencode"], stdin=io.StringIO(), stdout=out)
        result = json.loads(out.getvalue())
        assert code == 1 and result["ok"] is False and result["code"] == "state-dir", result
    assert sent == []
    for call in (lambda: mod.signal_pid(1, 15), lambda: mod.signal_pid(os.getpid(), 15)):
        with pytest.raises(mod.HelperError):
            call()
    with pytest.raises(mod.HelperError):
        mod.signal_group(0, 15)
    assert sent == []


# --------------------------------------------------------------- production shape


def test_status_on_fresh_state(ctx: Ctx) -> None:
    out = ctx.status()
    assert set(out) == {"ok", "pid1_start", "busy", "servers"} and out["busy"] is False
    assert out["pid1_start"] == mod.pid1_start()
    claude, opencode = out["servers"]["claude"], out["servers"]["opencode"]
    assert set(claude) == {"state", "reason", "pid", "since", "last_line"}
    assert set(opencode) == set(claude) | {"listening", "discovery_pid_ok", "foreign_listener"}
    assert claude["state"] == opencode["state"] == "stopped"
    assert stat.S_IMODE(ctx.state.stat().st_mode) == 0o700
    assert stat.S_IMODE((ctx.state / "lock").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("bogus",),
        ("status", "x"),
        ("stop",),
        ("stop", "--name", "codex"),
        ("claude-start",),
        ("claude-start", "--prefix", "Bad_Host"),
        ("claude-start", "--x", "y"),
        ("opencode-start", "--port", "abc"),
        ("opencode-start", "--port", "99999"),
    ],
)
def test_usage_errors(ctx: Ctx, args: tuple[str, ...]) -> None:
    rc, out = ctx.run(*args)
    assert rc == 1 and out["ok"] is False and out["code"] == "usage"
    assert out["code"] in mod.ERROR_CODES


def test_state_dir_must_be_private_and_real(ctx: Ctx, tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)
    rc, out = ctx.run("status", env={"DJINN_SERVE_TEST_STATE_DIR": str(loose)})
    assert rc == 1 and out["code"] == "state-dir"
    link = tmp_path / "link"
    link.symlink_to(ctx.state)
    rc, out = ctx.run("status", env={"DJINN_SERVE_TEST_STATE_DIR": str(link)})
    assert rc == 1 and out["code"] == "state-dir"
    rc, out = ctx.run("status", env={"DJINN_SERVE_TEST_STATE_DIR": str(tmp_path / "a/b/c")})
    assert rc == 1 and out["code"] == "state-dir"


# ------------------------------------------------------------------------- Claude


def test_claude_argv_cwd_and_window_size(ctx: Ctx) -> None:
    rc, out = ctx.claude()
    assert rc == 0 and out["result"] == "started" and out["name"] == "claude"
    assert out["ok"] is True and out["pid"] and out["log_tail"] == []
    (call,) = ctx.calls("claude")
    assert call["argv"] == [
        "remote-control",
        "--remote-control-session-name-prefix",
        "my-host",
    ]
    assert call["cwd"] == str(ctx.work)  # the working directory is kept
    assert ctx.record("claude")["cwd"] == str(ctx.work)
    assert call["tty"] is True and call["winsize"][:2] == [40, 120]
    ctx.stop("claude")


def test_spawned_pid_is_group_leader(ctx: Ctx) -> None:
    rc, out = ctx.claude()
    assert rc == 0
    record = ctx.record("claude")
    assert os.getpgid(record["pid"]) == record["pid"]
    assert os.getpgid(record["child_pid"]) == record["child_pid"] != record["pid"]
    ctx.stop("claude")
    rc, out = ctx.opencode()
    assert rc == 0 and out["result"] == "started", out
    pid = ctx.record("opencode")["pid"]
    assert os.getpgid(pid) == pid
    assert ctx.record("opencode")["port"] == ctx.port


def test_record_has_child_pid_before_started(ctx: Ctx) -> None:
    rc, out = ctx.claude()
    assert rc == 0 and out["result"] == "started"
    record = ctx.record("claude")
    assert record["pid"] == out["pid"] and record["pid1_start"] == mod.pid1_start()
    assert record["child_start"] == mod.proc_start(record["child_pid"])
    assert record["start"] == mod.proc_start(record["pid"])
    ctx.stop("claude")


def test_stop_right_after_start_reaches_child(ctx: Ctx) -> None:
    rc, _ = ctx.claude()
    assert rc == 0
    record = ctx.record("claude")
    rc, out = ctx.stop("claude")
    assert rc == 0 and out["results"]["claude"] == {"result": "stopped", "pid": record["pid"]}
    assert is_gone(record["child_pid"]) and is_gone(record["pid"])
    assert not (ctx.state / "claude.json").exists()


def test_stop_reaches_pty_child_group(ctx: Ctx) -> None:
    ctx.env["FAKE_CLAUDE_MODE"] = "stubborn"
    rc, _ = ctx.claude()
    assert rc == 0
    grandchild = ctx.fake / "grandchild.pid"
    assert wait_for(grandchild.exists)
    assert wait_for(lambda: grandchild.read_text() != "")
    gpid = int(grandchild.read_text())
    record = ctx.record("claude")
    assert is_alive(gpid) and is_alive(record["child_pid"])
    rc, out = ctx.stop("claude", timing="stop_wait=1,kill_wait=3")
    assert rc == 0 and out["results"]["claude"]["result"] == "stopped", out
    assert is_gone(gpid) and is_gone(record["child_pid"]) and is_gone(record["pid"])


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["term", "int"])
def test_supervisor_forwards_signal_to_child_group(ctx: Ctx, signum: int) -> None:
    ctx.env["FAKE_CLAUDE_MODE"] = "signal-group"
    rc, out = ctx.claude()
    assert rc == 0 and out["result"] == "started", out
    record = ctx.record("claude")
    grandchild = ctx.fake / "grandchild.pid"
    assert wait_for(lambda: grandchild.exists() and bool(grandchild.read_text()))
    gpid = int(grandchild.read_text())
    assert os.getpgid(gpid) == record["child_pid"]
    assert record["pid"] != record["child_pid"]
    os.kill(record["pid"], signum)  # Only the supervisor receives the test's signal.
    assert is_gone(record["child_pid"])
    assert is_gone(record["pid"])
    assert is_gone(gpid)
    assert mod.group_state(record["child_pid"], record["child_start"]) == "gone"


@pytest.mark.parametrize("gone", ["child_pid", "pid"], ids=["child", "supervisor"])
def test_claude_grace_exits_when_either_group_dies(ctx: Ctx, gone: str) -> None:
    # Keep both real groups alive but present one as gone to the grace predicate.
    injected = """
real_group_state = group_state
def one_group_gone(pid, start):
    path = os.path.join(os.environ[TEST_STATE_ENV], "claude.json")
    if os.path.exists(path):
        with open(path) as stream:
            record = json.load(stream)
        if pid == record[os.environ["GONE_GROUP"]]:
            return "gone"
    return real_group_state(pid, start)
group_state = one_group_gone
sys.exit(main(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout))
"""
    source = SOURCE.rsplit('if __name__ == "__main__":', 1)[0] + injected
    rc, out = ctx.claude(source=source, env={"GONE_GROUP": gone})
    assert rc == 0 and out["result"] == "exited", out
    record = ctx.record("claude")
    assert is_alive(record["pid"]) and is_alive(record["child_pid"])
    ctx.stop("claude")


def test_second_start_is_noop_same_pid(ctx: Ctx) -> None:
    _, first = ctx.claude()
    rc, second = ctx.claude()
    assert rc == 0 and second["result"] == "already-running" and second["pid"] == first["pid"]
    assert len(ctx.calls("claude")) == 1
    assert ctx.status()["servers"]["claude"]["state"] == "running"


def test_exited_record_reports_last_line_until_next_start(ctx: Ctx) -> None:
    ctx.env["FAKE_CLAUDE_MODE"] = "refusal"
    rc, out = ctx.claude()
    assert rc == 0 and out["result"] == "exited" and out["pid"] is None
    assert out["log_tail"] == [TEXT_REFUSAL]
    for _ in range(2):  # kept: status does not consume the record
        entry = ctx.status()["servers"]["claude"]
        assert entry["state"] == "exited" and entry["last_line"] == TEXT_REFUSAL
    assert (ctx.state / "claude.json").exists()
    ctx.env["FAKE_CLAUDE_MODE"] = "normal"
    rc, out = ctx.claude()
    assert rc == 0 and out["result"] == "started"
    entry = ctx.status()["servers"]["claude"]
    assert entry["state"] == "running" and entry["last_line"] is None
    assert TEXT_REFUSAL not in (ctx.state / "claude.log").read_text()


def test_missing_claude_binary_exits_with_reason(ctx: Ctx) -> None:
    (ctx.fake / "bin/claude").unlink()
    rc, out = ctx.claude()
    assert rc == 0 and out["result"] == "exited"
    assert any("claude" in line for line in out["log_tail"])


def test_log_cap_truncates_and_keeps_the_newest_output(ctx: Ctx) -> None:
    ctx.env["FAKE_CLAUDE_MODE"] = "flood"
    rc, _ = ctx.claude()
    assert rc == 0
    log = ctx.state / "claude.log"
    assert wait_for(lambda: "LAST-LINE-MARKER" in log.read_text(errors="replace"), 30)
    assert log.stat().st_size <= mod.LOG_CAP
    assert "log truncated" in log.read_text(errors="replace")
    ctx.stop("claude")


def test_helper_returns_while_daemon_runs(ctx: Ctx) -> None:
    started = time.monotonic()
    rc, _ = ctx.claude()
    assert rc == 0 and time.monotonic() - started < 5
    record = ctx.record("claude")
    for pid in (record["pid"], record["child_pid"]):
        assert is_alive(pid)
    logs = str(ctx.state / "claude.log")
    fds = {
        int(n): os.readlink(f"/proc/{record['pid']}/fd/{n}")
        for n in os.listdir(f"/proc/{record['pid']}/fd")
    }
    assert fds[1] == logs and fds[2] == logs
    assert all("pipe:" not in target for target in fds.values())
    assert not any(target.endswith("/lock") for target in fds.values())
    ctx.stop("claude")
    rc, _ = ctx.opencode()
    assert rc == 0
    opencode_fds = {
        os.readlink(f"/proc/{ctx.record('opencode')['pid']}/fd/{n}")
        for n in os.listdir(f"/proc/{ctx.record('opencode')['pid']}/fd")
    }
    assert not any(t.startswith("pipe:") or t.endswith("/lock") for t in opencode_fds)


def test_lock_free_after_claude_start(ctx: Ctx) -> None:
    rc, _ = ctx.claude()
    assert rc == 0
    handle = os.open(ctx.state / "lock", os.O_RDWR)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if a daemon kept the lock fd
    finally:
        os.close(handle)
    ctx.stop("claude")


# ------------------------------------------------------------------ lock, deadline


def hold_lock(ctx: Ctx) -> int:
    handle = os.open(ctx.state / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def test_concurrent_starts_yield_one_server(ctx: Ctx) -> None:
    handle = hold_lock(ctx)
    procs = [
        subprocess.Popen(
            [sys.executable, "-I", "-c", SOURCE, "claude-start", "--prefix", "my-host"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ctx.work,
            env={**ctx.env, "DJINN_SERVE_TEST_TIMING": "claude_grace=0.4,lock_wait=20"},
        )
        for _ in range(2)
    ]
    time.sleep(0.8)  # both are waiting on the lock the test holds
    os.close(handle)
    outs = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    assert sorted(o["result"] for o in outs) == ["already-running", "started"], outs
    assert len({o["pid"] for o in outs}) == 1
    assert len(ctx.calls("claude")) == 1
    ctx.stop("claude")


def test_busy_lock_reports_busy_and_status_still_answers(ctx: Ctx) -> None:
    handle = hold_lock(ctx)
    try:
        rc, out = ctx.claude(timing="lock_wait=0.3")
        assert rc == 1 and out["code"] == "busy"
        assert ctx.status()["busy"] is True
        rc, out = ctx.claude(timing="deadline=0.4,lock_wait=5")  # lock wait is inside the deadline
        assert rc == 1 and out["code"] == "timeout"
    finally:
        os.close(handle)
    assert ctx.status()["busy"] is False


def test_deadline_cuts_and_releases_lock(ctx: Ctx) -> None:
    started = time.monotonic()
    rc, out = ctx.claude(timing="deadline=1.5,claude_grace=12", timeout=30)
    assert rc == 1 and out["code"] == "timeout" and out["code"] in mod.ERROR_CODES
    assert time.monotonic() - started < 9
    handle = os.open(ctx.state / "lock", os.O_RDWR)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released after the cut
    finally:
        os.close(handle)
    assert ctx.status()["servers"]["claude"]["state"] == "running"  # reported, retry is safe
    ctx.stop("claude")


# --------------------------------------------------------------------------- stop


def test_stop_not_running_is_success(ctx: Ctx) -> None:
    rc, out = ctx.stop("claude", "opencode", "claude")
    assert rc == 0 and out["ok"] is True
    assert out["results"] == {
        "claude": {"result": "not-running", "pid": None},
        "opencode": {"result": "not-running", "pid": None},
    }
    assert all(r["result"] in mod.STOP_RESULTS for r in out["results"].values())


def test_stop_ends_whole_group(ctx: Ctx) -> None:
    ctx.env["FAKE_OC_MODE"] = "grandchild"
    rc, out = ctx.opencode()
    assert rc == 0 and out["result"] == "started", out
    assert wait_for((ctx.fake / "grandchild.pid").exists)
    gpid = int((ctx.fake / "grandchild.pid").read_text())
    assert os.getpgid(gpid) == ctx.record("opencode")["pid"]
    leader = ctx.record("opencode")["pid"]
    rc, out = ctx.stop("opencode", timing="stop_wait=1,kill_wait=2")
    assert rc == 0 and out["results"]["opencode"]["result"] == "stopped", out
    assert is_gone(leader) and is_gone(gpid)


@pytest.mark.parametrize("wrong", ["pid1", "start"])
def test_stale_record_after_pid1_change_not_running_not_signalled(ctx: Ctx, wrong: str) -> None:
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    start = mod.raw_start(bystander.pid)
    assert start is not None
    record = mod.Record(
        pid=bystander.pid,
        start=str(int(start) + 1) if wrong == "start" else start,
        pid1_start=str(int(mod.pid1_start()) + 1) if wrong == "pid1" else mod.pid1_start(),
        cwd="/",
        argv_tag="t",
        started_at="2026-01-01T00:00:00Z",
    )
    path = ctx.state / "claude.json"
    path.write_bytes(mod.encode_record(record))
    assert ctx.status()["servers"]["claude"]["state"] == "stopped"
    assert not path.exists()  # stale: removed
    path.write_bytes(mod.encode_record(record))
    rc, out = ctx.stop("claude", "opencode")
    assert rc == 0 and out["results"]["claude"]["result"] == "not-running"
    time.sleep(0.3)
    assert bystander.poll() is None  # never signalled
    bystander.kill()
    bystander.wait()


# ------------------------------------------------------------------------ OpenCode


def test_opencode_start_call_set_is_exact_and_password_stays_out(ctx: Ctx) -> None:
    password = "pw-secret-1"
    rc, out = ctx.opencode(password, cors="https://box.tail.ts.net")
    assert rc == 0 and out["result"] == "started" and out["stopped_previous"] is False, out
    assert out["ok"] is True and out["name"] == "opencode" and out["log_tail"] == []
    (call,) = ctx.calls("opencode")  # exactly one call: the serve itself
    assert call["argv"] == [
        "serve",
        "--service",
        "--hostname",
        "0.0.0.0",
        "--port",
        str(ctx.port),
        "--cors",
        "https://box.tail.ts.net",
    ]
    assert password not in json.dumps(call["argv"]) and "password" not in "".join(call["argv"])
    assert all(password not in value for value in call["environ"].values())
    assert not any("PASSWORD" in key.upper() for key in call["environ"])
    assert json.loads(ctx.service_json().read_text()) == {"password": password}
    assert stat.S_IMODE(ctx.service_json().stat().st_mode) == 0o600
    assert password not in ctx.last_raw
    leaked = "".join(p.read_text(errors="replace") for p in ctx.state.iterdir() if p.is_file())
    assert password not in leaked
    entry = ctx.status()["servers"]["opencode"]
    assert entry["state"] == "running" and entry["listening"] and entry["discovery_pid_ok"]
    assert entry["foreign_listener"] is False and entry["pid"] == out["pid"]
    ctx.stop("opencode")


def test_opencode_without_cors_has_no_cors_flag(ctx: Ctx) -> None:
    rc, _ = ctx.opencode()
    assert rc == 0
    assert ctx.calls("opencode")[0]["argv"][-2:] == ["--port", str(ctx.port)]
    ctx.stop("opencode")


def test_spawned_server_has_no_password_in_argv_or_env(ctx: Ctx) -> None:
    rc, _ = ctx.opencode("zz-top-secret")
    assert rc == 0
    (call,) = ctx.calls("opencode")
    assert not any("zz-top-secret" in token for token in call["argv"])
    assert not any("zz-top-secret" in value for value in call["environ"].values())
    assert not any(k in call["environ"] for k in ("OPENCODE_PASSWORD", "OPENCODE_SERVER_PASSWORD"))
    ctx.stop("opencode")


def test_helper_never_prints_password(ctx: Ctx) -> None:
    password = "pw-never-printed"
    cases: list[tuple[str, dict[str, str], str]] = [
        ("ok", {}, "ok"),
        ("exit-early", {"FAKE_OC_MODE": "exit-early"}, "ok"),
        ("bad-env", {"XDG_STATE_HOME": "/x"}, "fail"),
    ]
    for _label, env, _kind in cases:
        _, out = ctx.opencode(password, env=env)
        assert password not in json.dumps(out) and password not in ctx.last_raw
        ctx.stop("opencode")
    ctx.service_json().write_text(json.dumps({"remote": True, "password": password}))
    rc, out = ctx.opencode(password)
    assert rc == 1 and password not in ctx.last_raw
    rc, out = ctx.opencode(stdin=json.dumps({"password": 5, "x": password}))
    assert rc == 1 and password not in ctx.last_raw


def test_ready_requires_discovery_pid_ours(ctx: Ctx) -> None:
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    ctx.env["FAKE_OC_DISCOVERY_PID"] = str(other.pid)
    rc, out = ctx.opencode(timing="opencode_ready=2,stop_wait=3")
    assert rc == 1 and out["code"] == "not-registered", out
    (call,) = ctx.calls("opencode")
    assert is_gone(call["pid"]) and not (ctx.state / "opencode.json").exists()
    assert other.poll() is None  # the registered foreign pid is not ours to signal
    other.kill()
    other.wait()


def test_ready_requires_listener_of_our_pid(ctx: Ctx) -> None:
    ctx.env["FAKE_OC_MODE"] = "nolisten"
    with socket.socket() as foreign:  # someone else listens on the port; we own nothing
        foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        foreign.bind(("0.0.0.0", ctx.port))  # noqa: S104
        foreign.listen(1)
        rc, out = ctx.opencode(timing="opencode_ready=2,stop_wait=3")
    assert rc == 1 and out["code"] == "spawn", out
    (call,) = ctx.calls("opencode")
    assert is_gone(call["pid"]) and not (ctx.state / "opencode.json").exists()


@pytest.mark.parametrize(
    ("previous", "requested", "outcome"),
    [
        (None, "https://box.example.ts.net", "restarted"),
        ("https://box.example.ts.net", "https://other.example.ts.net", "restarted"),
        ("https://box.example.ts.net", None, "restarted"),
        ("https://box.example.ts.net", "https://box.example.ts.net", "already-running"),
        (None, None, "already-running"),
    ],
    ids=["add", "change", "remove", "same-url", "same-none"],
)
def test_opencode_cors_controls_restart(
    ctx: Ctx, previous: str | None, requested: str | None, outcome: str
) -> None:
    rc, first = ctx.opencode(cors=previous)
    assert rc == 0 and first["result"] == "started", first
    assert ctx.record("opencode")["cors"] == previous
    rc, second = ctx.opencode(cors=requested)
    assert rc == 0 and second["result"] == outcome, second
    assert ctx.record("opencode")["cors"] == requested
    restarted = outcome == "restarted"
    assert (second["pid"] != first["pid"]) is restarted
    assert second["stopped_previous"] is restarted
    assert len(ctx.calls("opencode")) == (2 if restarted else 1)
    argv = ctx.calls("opencode")[-1]["argv"]
    if requested is None:
        assert "--cors" not in argv
    else:
        assert argv[-2:] == ["--cors", requested]
    if restarted:
        assert is_gone(first["pid"])
    ctx.stop("opencode")


def test_changed_password_restarts_ours(ctx: Ctx) -> None:
    _, first = ctx.opencode("pw-one")
    assert first["result"] == "started"
    _, again = ctx.opencode("pw-one")
    assert again["result"] == "already-running" and again["pid"] == first["pid"]
    assert len(ctx.calls("opencode")) == 1
    rc, second = ctx.opencode("pw-two")
    assert rc == 0 and second["result"] == "restarted", second
    assert second["pid"] != first["pid"] and second["stopped_previous"] is True
    assert is_gone(first["pid"])
    assert json.loads(ctx.service_json().read_text())["password"] == "pw-two"
    assert ctx.status()["servers"]["opencode"]["pid"] == second["pid"]
    ctx.stop("opencode")


def test_degraded_ours_is_stopped_then_restarted(ctx: Ctx) -> None:
    _, first = ctx.opencode()
    (ctx.fake / "oc-ctl").write_text("close")  # the fake drops its listener, keeps running
    assert wait_for(lambda: ctx.status()["servers"]["opencode"]["state"] == "degraded")
    entry = ctx.status()["servers"]["opencode"]
    assert entry["reason"] == "listener lost" and entry["listening"] is False
    with socket.socket() as squatter:  # a foreign process now holds the port
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("0.0.0.0", ctx.port))  # noqa: S104
        squatter.listen(1)
        assert ctx.status()["servers"]["opencode"]["foreign_listener"] is True
    (ctx.fake / "oc-ctl").unlink()
    rc, second = ctx.opencode()
    assert rc == 0 and second["result"] == "restarted" and second["pid"] != first["pid"], second
    assert is_gone(first["pid"])
    servers = [c["pid"] for c in ctx.calls("opencode") if is_alive(c["pid"])]
    assert servers == [second["pid"]]
    assert ctx.record("opencode")["pid"] == second["pid"]
    assert ctx.status()["servers"]["opencode"]["state"] == "running"
    ctx.stop("opencode")


def test_opencode_early_exit_returns_exited_with_clean_tail(ctx: Ctx) -> None:
    ctx.env["FAKE_OC_MODE"] = "exit-early"
    rc, out = ctx.opencode()
    assert rc == 0 and out["result"] == "exited" and out["pid"] is None, out
    assert out["log_tail"] == ["Error: address in use"]
    entry = ctx.status()["servers"]["opencode"]
    assert entry["state"] == "exited" and entry["last_line"] == "Error: address in use"
    rc, stopped = ctx.stop("opencode")
    assert stopped["results"]["opencode"]["result"] == "not-running"
    assert not (ctx.state / "opencode.json").exists()


def start_default_fake(
    ctx: Ctx, mode: str = "normal", executable: str = "opencode.exe"
) -> tuple[subprocess.Popen[bytes], int]:
    port = free_port()
    # Python opens a script named serve, preserving the native-shaped /proc argv.
    (ctx.fake / "serve").write_text(
        FAKE_OPENCODE.replace("args = sys.argv[1:]", 'args = ["serve", *sys.argv[1:]]')
    )
    proc = subprocess.Popen(
        [
            str(ctx.fake / "bin" / executable),
            "serve",
            "--service",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        executable=sys.executable,
        cwd=ctx.fake,
        stdin=subprocess.DEVNULL,
        env={**ctx.env, "FAKE_OC_MODE": mode},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    disc = ctx.home / ".local/state/opencode/service.json"
    assert wait_for(lambda: disc.exists() and f":{port}" in disc.read_text())
    if mode == "normal":
        assert wait_for(lambda: bool(mod.listener_inodes(port, wildcard_v4=False)))
    return proc, port


def test_stale_discovery_spares_unrelated_process(ctx: Ctx) -> None:
    disc = ctx.home / ".local/state/opencode/service.json"
    disc.parent.mkdir(parents=True)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    disc.write_text(json.dumps({"pid": sleeper.pid, "url": f"http://127.0.0.1:{free_port()}"}))
    rc, out = ctx.opencode()
    assert rc == 0 and out["result"] == "started" and out["stopped_previous"] is False, out
    assert sleeper.poll() is None  # stale registration: nothing signalled
    ctx.stop("opencode")
    # an unrelated process that does listen on the registered port, but is not an OpenCode service
    port = free_port()
    code = (
        "import socket, time\ns = socket.socket()\n"
        f"s.bind(('127.0.0.1', {port}))\ns.listen(1)\ntime.sleep(120)"
    )
    listener = subprocess.Popen([sys.executable, "-c", code])
    assert wait_for(lambda: bool(mod.listener_inodes(port, wildcard_v4=False)))
    disc.write_text(json.dumps({"pid": listener.pid, "url": f"http://127.0.0.1:{port}"}))
    rc, out = ctx.opencode()
    assert rc == 0 and out["result"] == "started" and out["stopped_previous"] is False, out
    assert listener.poll() is None and sleeper.poll() is None
    ctx.stop("opencode")
    # an OpenCode service by cmdline that registered itself but owns no listener on its url port
    quiet, _ = start_default_fake(ctx, "nolisten")
    rc, out = ctx.opencode()
    assert rc == 0 and out["result"] == "started" and out["stopped_previous"] is False, out
    assert quiet.poll() is None
    ctx.stop("opencode")
    for proc in (sleeper, listener, quiet):
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("executable", ["opencode", "opencode.exe"])
def test_verified_default_service_is_replaced_without_native_stop(
    ctx: Ctx, executable: str
) -> None:
    default, _ = start_default_fake(ctx, executable=executable)
    rc, out = ctx.opencode()
    assert rc == 0 and out["result"] == "started" and out["stopped_previous"] is True, out
    assert wait_for(lambda: default.poll() is not None)
    assert [c["argv"] for c in ctx.calls("opencode")].count(["service", "stop"]) == 0
    assert ctx.status()["servers"]["opencode"]["state"] == "running"
    ctx.stop("opencode")


def test_never_calls_native_service_stop(ctx: Ctx) -> None:
    """Start, rotation, degraded restart, replacing a verified default and stop: no native call."""
    default, _ = start_default_fake(ctx)
    ctx.opencode("a")
    ctx.opencode("b")  # rotation
    (ctx.fake / "oc-ctl").write_text("close")
    assert wait_for(lambda: ctx.status()["servers"]["opencode"]["state"] == "degraded")
    (ctx.fake / "oc-ctl").unlink()
    ctx.opencode("b")  # degraded restart
    ctx.stop("opencode")
    argvs = [c["argv"] for c in ctx.calls("opencode")]
    assert default.poll() is not None
    assert argvs and all(a[:2] == ["serve", "--service"] for a in argvs), argvs


def test_remote_config_refused_before_any_change(ctx: Ctx) -> None:
    for remote in ({"remote": {"route": "x"}}, {"remote": True}):
        ctx.service_json().parent.mkdir(parents=True, exist_ok=True)
        ctx.service_json().write_text(json.dumps(remote))
        before = ctx.service_json().read_bytes()
        rc, out = ctx.opencode()
        assert rc == 1 and out["code"] == "remote-config", out
        assert "opencode service unset remote" in out["message"]
        assert ctx.service_json().read_bytes() == before
        assert ctx.calls("opencode") == []
        assert not [p for p in ctx.state.iterdir() if p.name != "lock"]


@pytest.mark.parametrize("key", ["hostname", "port", "cors", "env", "disabled", "x-unknown"])
def test_service_config_allowlist(ctx: Ctx, key: str) -> None:
    ctx.service_json().parent.mkdir(parents=True)
    ctx.service_json().write_text(json.dumps({"password": "old", key: 1}))
    before = ctx.service_json().read_bytes()
    rc, out = ctx.opencode()
    assert rc == 1 and out["code"] == "service-config", out
    assert f"opencode service unset {key}" in out["message"]
    assert ctx.service_json().read_bytes() == before
    assert ctx.calls("opencode") == [] and not [p for p in ctx.state.iterdir() if p.name != "lock"]


def test_service_config_password_only_and_absent_are_accepted(ctx: Ctx) -> None:
    ctx.service_json().parent.mkdir(parents=True)
    ctx.service_json().write_text(json.dumps({"password": "old"}))
    rc, out = ctx.opencode("new")
    assert rc == 0 and out["result"] == "started"
    ctx.stop("opencode")
    ctx.service_json().write_text("[1]")
    rc, out = ctx.opencode("new")
    assert rc == 1 and out["code"] == "service-json"


@pytest.mark.parametrize(
    "env",
    [
        {"HOME": "/home/other"},
        {"HOME": ""},
        {"XDG_CONFIG_HOME": "/x"},
        {"XDG_STATE_HOME": "/x"},
        {"OPENCODE_CONFIG_DIR": "/x"},
    ],
)
def test_env_root_refused(ctx: Ctx, env: dict[str, str]) -> None:
    resolved = {k: v.replace("{home}", str(ctx.home)) for k, v in env.items()}
    rc, out = ctx.opencode(env=resolved)
    assert rc == 1 and out["code"] == "env-root", out
    assert ctx.calls("opencode") == [] and not ctx.service_json().exists()
    assert not [p for p in ctx.state.iterdir() if p.name != "lock"]


@pytest.mark.parametrize("home_value", ["{home}/", "{home}/.", "{home}//"])
def test_env_root_accepts_normalized_home(ctx: Ctx, home_value: str) -> None:
    rc, out = ctx.opencode(env={"HOME": home_value.replace("{home}", str(ctx.home))})
    assert rc == 0 and out["result"] == "started", out


def test_env_root_refuses_home_through_a_symlink(ctx: Ctx) -> None:
    ctx.home.mkdir(exist_ok=True)
    alias = ctx.root / "home-alias"
    alias.symlink_to(ctx.home)
    rc, out = ctx.opencode(env={"HOME": str(alias)})
    assert rc == 1 and out["code"] == "env-root", out
    assert ctx.calls("opencode") == [] and not ctx.service_json().exists()


@pytest.mark.parametrize("which", [".config", ".local/state/opencode", "service.json"])
def test_symlinked_settings_path_refused(ctx: Ctx, which: str) -> None:
    target = ctx.root / "elsewhere"
    (target / "opencode").mkdir(parents=True)
    if which == ".config":
        (ctx.home).mkdir(exist_ok=True)
        (ctx.home / ".config").symlink_to(target)
    elif which == "service.json":
        (ctx.home / ".config/opencode").mkdir(parents=True)
        (target / "real.json").write_text("{}")
        ctx.service_json().symlink_to(target / "real.json")
    else:
        (ctx.home / ".local/state").mkdir(parents=True)
        (ctx.home / ".local/state/opencode").symlink_to(target / "opencode")
    rc, out = ctx.opencode()
    assert rc == 1 and out["code"] == "state-path", out
    assert ctx.calls("opencode") == []
    assert not [p for p in ctx.state.iterdir() if p.name != "lock"]
    assert not list(target.rglob("*.tmp")) and not (target / "opencode/service.json").exists()


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "[]",
        '{"password": 5}',
        '{"password": ""}',
        json.dumps({"password": "x" * 5000}),
    ],
)
def test_invalid_password_input_is_refused(ctx: Ctx, stdin: str) -> None:
    rc, out = ctx.opencode(stdin=stdin)
    assert rc == 1 and out["code"] == "password", out
    assert ctx.calls("opencode") == [] and not ctx.service_json().exists()


def test_reused_zombie_leader_is_stale_and_never_signalled(ctx: Ctx) -> None:
    rc, out = ctx.opencode(env={"FAKE_OC_MODE": "grandchild"})
    assert rc == 0, out
    record = ctx.record("opencode")
    pid = record["pid"]
    grandchild_file = ctx.fake / "grandchild.pid"
    assert wait_for(grandchild_file.exists)
    grandchild = int(grandchild_file.read_text())
    os.kill(pid, signal.SIGKILL)
    assert wait_for(lambda: (mod.proc_stat(pid) or [None])[0] == "Z")
    record["start"] = str(int(record["start"]) - 1)
    path = ctx.state / "opencode.json"
    raw = json.dumps(record)
    path.write_text(raw)
    assert ctx.status()["servers"]["opencode"]["state"] == "stopped"
    assert not path.exists()
    path.write_text(raw)
    rc, out = ctx.stop("opencode")
    assert rc == 0 and out["results"]["opencode"]["result"] == "not-running", out
    assert is_alive(grandchild)
    assert not path.exists()


@pytest.mark.parametrize("state", ["running", "degraded", "cors-change"])
def test_failed_stop_ours_aborts_before_password_merge(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    rc, out = ctx.opencode("old-password")
    assert rc == 0, out
    if state == "degraded":
        (ctx.fake / "oc-ctl").write_text("close")
        assert wait_for(lambda: ctx.status()["servers"]["opencode"]["state"] == "degraded")
    service_before = ctx.service_json().read_bytes()
    record_path = ctx.state / "opencode.json"
    record_before = record_path.read_bytes()
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "OPENCODE_CONFIG_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, value in ctx.env.items():
        monkeypatch.setenv(key, value)
    stops = []
    monkeypatch.setattr(mod, "stop_groups", lambda groups, _deadline: stops.append(groups) or False)
    spawned = []

    def no_spawn(*args: Any, **kwargs: Any) -> int:
        spawned.append(args)
        pytest.fail("replacement spawned after a failed stop")

    monkeypatch.setattr(mod, "spawn", no_spawn)
    stdout = io.StringIO()
    args = ["opencode-start", "--port", str(ctx.port)]
    if state == "cors-change":
        args += ["--cors", "https://box.example.ts.net"]
    code = mod.main(
        args,
        stdin=io.StringIO(
            json.dumps({"password": "old-password" if state == "cors-change" else "new-password"})
        ),
        stdout=stdout,
    )
    reply = json.loads(stdout.getvalue())
    assert code == 1 and reply["code"] == "spawn", reply
    assert stops and not spawned
    assert ctx.service_json().read_bytes() == service_before
    assert record_path.read_bytes() == record_before
    assert is_alive(json.loads(record_before)["pid"])
    ctx.stop("opencode")


def test_readiness_failed_cleanup_retains_surviving_record(ctx: Ctx) -> None:
    source = (
        SOURCE.rsplit('if __name__ == "__main__":', 1)[0]
        + """
stop_groups = lambda *_: False
sys.exit(main(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout))
"""
    )
    rc, out = ctx.run(
        "opencode-start",
        "--port",
        str(ctx.port),
        stdin='{"password":"synthetic-password"}',
        timing="opencode_ready=0.5",
        env={"FAKE_OC_MODE": "nolisten"},
        source=source,
    )
    assert rc == 1 and out["code"] == "spawn", out
    record = ctx.record("opencode")
    assert is_alive(record["pid"])
    assert ctx.status()["servers"]["opencode"]["state"] == "degraded"
    assert "could not be stopped" in out["message"]
    rc, out = ctx.stop("opencode")
    assert rc == 0 and out["results"]["opencode"]["result"] == "stopped", out
    assert is_gone(record["pid"])


@pytest.mark.parametrize("name", ["claude", "opencode"])
@pytest.mark.parametrize("boundary", ["build", "save", "slow-save"])
@pytest.mark.parametrize("cleanup", ["ok", "false", "raises"])
def test_failed_new_record_cleans_spawned_groups(
    ctx: Ctx, name: str, boundary: str, cleanup: str
) -> None:
    injected = """
import errno
real_fresh_record = _fresh_record
real_kill_failed_launch = _kill_failed_launch
real_deadline_init = Deadline.__init__
launch_end = 0
def remember_deadline(self, seconds):
    global launch_end
    real_deadline_init(self, seconds)
    launch_end = self.end
Deadline.__init__ = remember_deadline
def remember_spawn(pid, handoff, tag, port, cors=None):
    with open(os.path.join(os.environ["FAKE_DIR"], "spawned.json"), "w") as stream:
        json.dump({"leader": pid, "child": handoff[0] if handoff else None}, stream)
    end = time.monotonic() + 5
    while handoff is None and not os.path.exists(
        os.path.join(os.environ["FAKE_DIR"], "grandchild.pid")
    ):
        assert time.monotonic() < end, "fake server did not start its grandchild"
        time.sleep(0.01)
    if os.environ["FAIL_RECORD_AT"] == "build":
        raise OSError(errno.ENOSPC, "No space left on device")
    return real_fresh_record(pid, handoff, tag, port, cors)
def fail_save(*_):
    if os.environ["FAIL_RECORD_AT"] == "slow-save":
        time.sleep(max(0, launch_end - time.monotonic() - 0.01))
    raise OSError(errno.ENOSPC, "No space left on device")
def record_cleanup(pid, handoff, end):
    groups = ([handoff] if handoff else []) + [(pid, raw_start(pid) or "0")]
    with open(os.path.join(os.environ["FAKE_DIR"], "cleanup.json"), "w") as stream:
        json.dump(groups, stream)
    if os.environ["FAIL_CLEANUP"] == "raises":
        raise OSError(errno.EPERM, "Operation not permitted")
    if os.environ["FAIL_CLEANUP"] == "false":
        return False
    return real_kill_failed_launch(pid, handoff, end)
_fresh_record = remember_spawn
save_record = fail_save
_kill_failed_launch = record_cleanup
sys.exit(main(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout))
"""
    source = SOURCE.rsplit('if __name__ == "__main__":', 1)[0] + injected
    args = (
        ["claude-start", "--prefix", "test"]
        if name == "claude"
        else ["opencode-start", "--port", str(ctx.port)]
    )
    rc, out = ctx.run(
        *args,
        stdin=None if name == "claude" else '{"password":"synthetic-password"}',
        timing="deadline=0.8,launch_cleanup=0.5",
        env={
            "FAIL_RECORD_AT": boundary,
            "FAIL_CLEANUP": cleanup,
            "FAKE_CLAUDE_MODE": "stubborn",
            "FAKE_OC_MODE": "grandchild",
        },
        source=source,
    )
    assert rc == 1 and out["ok"] is False and out["code"] == "state-dir", out
    assert f"could not build or publish the {name} process record" in out["message"]
    spawned = json.loads((ctx.fake / "spawned.json").read_text())
    pids = [spawned["leader"]]
    if name == "claude":
        pids.insert(0, spawned["child"])
    all_pids = list(pids)
    grandchild = ctx.fake / "grandchild.pid"
    if name == "claude":
        assert ctx.calls("claude") == [] and not grandchild.exists()
    else:
        all_pids.append(int(grandchild.read_text()))
    assert not (ctx.state / f"{name}.json").exists()
    if cleanup == "ok" or name == "claude":
        assert "may survive" not in out["message"]
        assert all(not is_alive(pid) for pid in all_pids)
    else:
        assert "could not stop" in out["message"] and "may survive" in out["message"]
        assert all(is_alive(pid) for pid in all_pids)
    groups = json.loads((ctx.fake / "cleanup.json").read_text())
    assert [group[0] for group in groups] == pids
    assert all(group[1].isdigit() and int(group[1]) > 0 for group in groups)


@pytest.mark.parametrize("name,delay", [("opencode", 1.4), ("claude", 0.8)])
def test_failed_handshake_kills_child_before_late_setsid(ctx: Ctx, name: str, delay: float) -> None:
    # Delay only the forked child's setsid; all handshake/cleanup code is production code.
    source = SOURCE.replace(
        "        os.setsid()", f"        time.sleep({delay})\n        os.setsid()"
    )
    injected = """
real_spawn = spawn
def remember_spawn(*args, **kwargs):
    pid = real_spawn(*args, **kwargs)
    with open(os.path.join(os.environ["FAKE_DIR"], "spawned.pid"), "w") as stream:
        stream.write(str(pid))
    return pid
spawn = remember_spawn
sys.exit(main(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout))
"""
    source = source.rsplit('if __name__ == "__main__":', 1)[0] + injected
    args = (
        ["claude-start", "--prefix", "test"]
        if name == "claude"
        else ["opencode-start", "--port", str(ctx.port)]
    )
    before = time.monotonic()
    rc, out = ctx.run(
        *args,
        stdin='{"password":"synthetic-password"}',
        source=source,
        timing="handoff_wait=0.1,launch_cleanup=0.5",
    )
    elapsed = time.monotonic() - before
    pid = int((ctx.fake / "spawned.pid").read_text())
    assert rc == 1 and out["ok"] is False and out["code"] == "spawn", out
    assert "may survive" not in out["message"]
    assert elapsed < delay, "cleanup must KILL immediately, before the child can finish setsid"
    assert not is_alive(pid)
    time.sleep(delay)
    assert not is_alive(pid) and ctx.calls(name) == []
    assert not (ctx.state / f"{name}.json").exists()


def test_claude_missing_handoff_cleans_unreported_child(ctx: Ctx) -> None:
    # Model descheduling after pty.fork(), before the child identity is handed over.
    source = SOURCE.replace(
        '        payload = f"{child}',
        '        open(os.path.join(os.environ["FAKE_DIR"], "child.pid"), "w").write(str(child))\n'
        '        time.sleep(0.6)\n        payload = f"{child}',
    )
    rc, out = ctx.claude(
        source=source,
        timing="handoff_wait=0.3,launch_cleanup=0.5",
        env={"FAKE_CLAUDE_MODE": "stubborn"},
    )
    assert rc == 1 and out["code"] == "spawn", out
    assert not (ctx.state / "claude.json").exists()
    assert ctx.calls("claude") == []
    assert is_gone(int((ctx.fake / "child.pid").read_text()))
    assert not (ctx.fake / "grandchild.pid").exists()


def test_claude_exec_waits_for_published_identity_and_ack(ctx: Ctx) -> None:
    injected = """
helper_pid = os.getpid()
real_save = save_record
real_write = os.write
real_exec = os.execvp
def delayed_save(dirfd, name, record):
    time.sleep(0.25)
    assert not os.path.exists(os.path.join(os.environ["FAKE_DIR"], "claude-calls.jsonl"))
    return real_save(dirfd, name, record)
def delayed_ack(fd, data):
    if os.getpid() == helper_pid and data == _LAUNCH_ACK:
        assert os.path.exists(os.path.join(os.environ[TEST_STATE_ENV], "claude.json"))
        time.sleep(0.25)
        assert not os.path.exists(os.path.join(os.environ["FAKE_DIR"], "claude-calls.jsonl"))
    return real_write(fd, data)
def checked_exec(file, argv):
    assert os.path.exists(os.path.join(os.environ[TEST_STATE_ENV], "claude.json"))
    return real_exec(file, argv)
save_record = delayed_save
os.write = delayed_ack
os.execvp = checked_exec
sys.exit(main(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout))
"""
    source = SOURCE.rsplit('if __name__ == "__main__":', 1)[0] + injected
    rc, out = ctx.claude(source=source)
    assert rc == 0 and out["result"] == "started", out
    assert len(ctx.calls("claude")) == 1
    assert is_alive(ctx.record("claude")["child_pid"])
    assert ctx.stop("claude")[0] == 0


@pytest.mark.parametrize(
    "fault", ["closed", "wrong-ack", "timeout", "wrong-go", "empty-go", "ack-error", "gate-error"]
)
def test_unacked_or_invalid_gate_never_execs_and_exits(ctx: Ctx, fault: str) -> None:
    source = SOURCE
    if fault == "wrong-go":
        source = source.replace(
            "os.write(gate_write, _LAUNCH_GO)", 'os.write(gate_write, b"wrong")'
        )
    elif fault == "empty-go":
        source = source.replace("os.write(gate_write, _LAUNCH_GO)", 'os.write(gate_write, b"")')
    elif fault == "ack-error":
        source = source.replace(
            '        ready = select.select([ack], [], [], timing("deadline"))[0]',
            '        raise OSError("synthetic acknowledgement read error")',
        )
    elif fault == "gate-error":
        source = source.replace(
            "            go = os.read(gate_read, len(_LAUNCH_GO) + 1)",
            '            raise OSError("synthetic gate read error")',
        )
    injected = """
reader, writer = os.pipe()
ack_read, ack_write = os.pipe()
fd = open_state_dir()
before = time.monotonic()
pid = spawn(fd, "claude", ["claude", "remote-control"], pty_mode=True,
            handoff=writer, ack=ack_read)
os.close(writer)
os.close(ack_read)
handoff = _read_handoff(reader, 1)
os.close(reader)
assert handoff is not None
fault = os.environ["GATE_FAULT"]
if fault == "wrong-ack":
    os.write(ack_write, b"wrong")
elif fault in ("wrong-go", "empty-go", "gate-error"):
    os.write(ack_write, _LAUNCH_ACK)
if fault != "timeout":
    os.close(ack_write)
_, status = os.waitpid(pid, 0)
print(json.dumps({"status": status, "pid": pid, "child": handoff[0],
                  "elapsed": time.monotonic() - before}))
"""
    source = source.rsplit('if __name__ == "__main__":', 1)[0] + injected
    proc = subprocess.run(
        [sys.executable, "-I", "-c", source],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        cwd=ctx.work,
        env={**ctx.env, "GATE_FAULT": fault, "DJINN_SERVE_TEST_TIMING": "deadline=0.7"},
        timeout=3,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert os.WIFEXITED(out["status"]) and os.WEXITSTATUS(out["status"]) == 0, out
    assert ctx.calls("claude") == []
    assert not is_alive(out["pid"]) and is_gone(out["child"])
    assert out["elapsed"] < (1.5 if fault == "timeout" else 0.6), out


@pytest.mark.parametrize("fault", ["handoff", "publication", "ack"])
def test_claude_launch_guard_closes_ack_exactly_once(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    pipes, held, closes = [], [], []
    real_pipe, real_close, real_write = os.pipe, os.close, os.write

    def remember_pipe():
        pair = real_pipe()
        pipes.append(pair)
        return pair

    def fake_spawn(*_args, **kwargs):
        held.append(os.dup(kwargs["ack"]))
        return 700001

    def remember_close(fd):
        if fd < 0 or (len(pipes) == 2 and fd == pipes[1][1]):
            closes.append(fd)
        real_close(fd)

    def fail_ack(fd, data):
        if data == mod._LAUNCH_ACK:
            raise BrokenPipeError("synthetic acknowledgement failure")
        return real_write(fd, data)

    def fail_save(*_):
        raise OSError("synthetic publication failure")

    for key, value in ctx.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(mod.os, "pipe", remember_pipe)
    monkeypatch.setattr(mod.os, "close", remember_close)
    monkeypatch.setattr(mod.os, "write", fail_ack)
    monkeypatch.setattr(mod, "spawn", fake_spawn)
    monkeypatch.setattr(mod, "_read_handoff", lambda *_: (700002, "123"))
    monkeypatch.setattr(mod, "_wait_leader", lambda *_: fault != "handoff")
    monkeypatch.setattr(mod, "raw_start", lambda _: "123")
    monkeypatch.setattr(mod, "group_state", lambda *_: "gone")
    monkeypatch.setattr(mod, "_kill_failed_launch", lambda *_: None)
    if fault == "publication":
        monkeypatch.setattr(mod, "save_record", fail_save)
    try:
        with pytest.raises(mod.HelperError):
            mod.cmd_claude_start(["--prefix", "test-host"])
        assert closes == [pipes[1][1]]
    finally:
        for fd in held:
            real_close(fd)


@pytest.mark.parametrize("entry", ["guard", "publication"])
def test_launch_guard_closes_ack_without_acking_before_kill(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    reader, writer = os.pipe()
    observed = []
    monkeypatch.setattr(mod, "raw_start", lambda _: "123")
    monkeypatch.setattr(mod, "group_state", lambda *_: "gone")

    def remember_kill(*_):
        ready = mod.select.select([reader], [], [], 0)[0]
        observed.append(os.read(reader, 32) if ready else None)

    def fail_record(*_):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(mod, "_kill_failed_launch", remember_kill)
    monkeypatch.setattr(mod, "_fresh_record", fail_record)
    try:
        with pytest.raises(mod.HelperError):
            if entry == "guard":
                mod._fail_launch(700001, None, "spawn", "failed", OSError(), ack=writer)
            else:
                mod._publish_started_record(-1, "claude", 700001, None, "tag", None, ack=writer)
        assert observed == [b""]
    finally:
        os.close(reader)
        with contextlib.suppress(OSError):
            os.close(writer)


def test_claude_failed_leader_handshake_cleans_handoff_groups(ctx: Ctx) -> None:
    injected = """
helper_pid = os.getpid()
real_wait_leader = _wait_leader
real_read_handoff = _read_handoff
def remember_handoff(fd, seconds):
    handoff = real_read_handoff(fd, seconds)
    with open(os.path.join(os.environ["FAKE_DIR"], "child.json"), "w") as stream:
        json.dump(handoff, stream)
    return handoff
def fail_supervisor_leader(pid, seconds):
    if os.getpid() != helper_pid:
        return real_wait_leader(pid, seconds)
    with open(os.path.join(os.environ["FAKE_DIR"], "spawned.pid"), "w") as stream:
        stream.write(str(pid))
    return False
_read_handoff = remember_handoff
_wait_leader = fail_supervisor_leader
sys.exit(main(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout))
"""
    source = SOURCE.rsplit('if __name__ == "__main__":', 1)[0] + injected
    rc, out = ctx.claude(source=source, env={"FAKE_CLAUDE_MODE": "stubborn"})
    assert rc == 1 and out["code"] == "spawn", out
    assert "may survive" not in out["message"]
    child = json.loads((ctx.fake / "child.json").read_text())[0]
    assert not is_alive(child)
    assert not is_alive(int((ctx.fake / "spawned.pid").read_text()))
    assert ctx.calls("claude") == []
    assert not (ctx.state / "claude.json").exists()


def test_launch_guard_kills_child_and_group_before_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    pid, child = 700001, 700002
    monkeypatch.setattr(mod.os, "kill", lambda *args: calls.append(("kill", *args)))
    monkeypatch.setattr(mod.os, "killpg", lambda *args: calls.append(("killpg", *args)))
    reaps = iter([0, pid])

    def reap(*args):
        calls.append(("waitpid", *args))
        return next(reaps), 0

    monkeypatch.setattr(mod.os, "waitpid", reap)
    monkeypatch.setattr(mod, "group_state", lambda *_: "alive")
    mod._kill_failed_launch(pid, (child, "123"), time.monotonic() + 0.5)
    # The group KILL precedes the reap: the unreaped zombie keeps its PID from being reused.
    assert calls == [
        ("kill", pid, signal.SIGKILL),
        ("killpg", pid, signal.SIGKILL),
        ("waitpid", pid, os.WNOHANG),
        ("waitpid", pid, os.WNOHANG),
        ("killpg", child, signal.SIGKILL),
    ]


def test_launch_guard_reap_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []
    monkeypatch.setattr(mod.os, "kill", lambda *_: None)
    monkeypatch.setattr(mod.os, "killpg", lambda *args: sent.append(args))
    monkeypatch.setattr(mod.os, "waitpid", lambda *_: (0, 0))
    ticks = iter([10.0, 10.02])
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(mod.time, "sleep", lambda _: None)
    mod._kill_failed_launch(700001, None, 10.01)
    assert sent == [(700001, signal.SIGKILL)]


@pytest.mark.parametrize("pid", [-1, 0, 1, os.getpid(), os.getpgrp()])
def test_launch_guard_signal_floor(monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
    sent = []
    monkeypatch.setattr(mod.os, "kill", lambda *args: sent.append(args))
    monkeypatch.setattr(mod.os, "killpg", lambda *args: sent.append(args))
    monkeypatch.setattr(mod.os, "waitpid", lambda *_: (pid, 0))
    with pytest.raises(mod.HelperError, match="protected"):
        mod._kill_failed_launch(pid, None, time.monotonic() + 0.1)
    assert sent == []


@pytest.mark.parametrize("child", [0, 1, os.getpgrp()])
def test_launch_guard_handoff_signal_floor(monkeypatch: pytest.MonkeyPatch, child: int) -> None:
    sent = []
    monkeypatch.setattr(mod.os, "kill", lambda *_: None)
    monkeypatch.setattr(mod.os, "killpg", lambda *args: sent.append(args))
    monkeypatch.setattr(mod.os, "waitpid", lambda *_: (700001, 0))
    monkeypatch.setattr(mod, "group_state", lambda *_: "alive")
    mod._kill_failed_launch(700001, (child, "123"), time.monotonic() + 0.1)
    assert sent == [(700001, signal.SIGKILL)]


def test_launch_guard_skips_mismatched_handoff_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []
    monkeypatch.setattr(mod.os, "kill", lambda *_: None)
    monkeypatch.setattr(mod.os, "killpg", lambda *args: sent.append(args))
    monkeypatch.setattr(mod.os, "waitpid", lambda *_: (700001, 0))
    # Keep the real group_state check, with a live PID carrying a different start time.
    fields = ["S"] + ["0"] * 18 + ["456"]
    monkeypatch.setattr(mod, "proc_stat", lambda _: fields)
    mod._kill_failed_launch(700001, (700002, "123"), time.monotonic() + 0.1)
    assert sent == [(700001, signal.SIGKILL)]


@pytest.mark.parametrize("cleanup", ["false", "raises"])
def test_launch_guard_does_not_warn_without_proven_survivors(
    monkeypatch: pytest.MonkeyPatch, cleanup: str
) -> None:
    def fail_cleanup(*_):
        if cleanup == "raises":
            raise OSError("synthetic cleanup failure")
        return False

    monkeypatch.setattr(mod, "_kill_failed_launch", fail_cleanup)
    monkeypatch.setattr(mod, "raw_start", lambda _: "123")
    monkeypatch.setattr(mod, "group_state", lambda *_: "stale")
    with pytest.raises(mod.HelperError) as caught:
        mod._fail_launch(700001, (700002, "123"), "spawn", "handshake failed", OSError())
    assert caught.value.code == "spawn"
    assert "may survive" not in caught.value.message


def test_launch_cleanup_budget_fits_host_timeout() -> None:
    assert 0 < mod._TIMING["launch_cleanup"] <= 3
    assert mod._TIMING["deadline"] + mod._TIMING["launch_cleanup"] < 60


def test_launch_guard_uses_its_own_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    ends = []
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setenv(mod.TEST_TIMING_ENV, "deadline=0.01,launch_cleanup=0.3")
    monkeypatch.setattr(mod, "raw_start", lambda _: "123")
    monkeypatch.setattr(mod, "_kill_failed_launch", lambda _pid, _handoff, end: ends.append(end))
    monkeypatch.setattr(mod, "group_state", lambda *_: "alive" if clock[0] < 100.2 else "gone")
    with pytest.raises(mod.HelperError) as caught:
        mod._fail_launch(700001, None, "state-dir", "record failed", OSError())
    assert ends == [100.3]
    assert 100.2 <= clock[0] < 100.3
    assert "may survive" not in caught.value.message


def test_broken_handoff_kills_stubborn_child_group(ctx: Ctx) -> None:
    source = (
        SOURCE.rsplit('if __name__ == "__main__":', 1)[0]
        + """
wait_leader = _wait_leader
def remember_child(pid, seconds):
    ready = wait_leader(pid, seconds)
    with open(os.path.join(os.environ["FAKE_DIR"], "child.pid"), "w") as stream:
        stream.write(str(pid))
    return ready
_wait_leader = remember_child
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
reader, writer = os.pipe()
ack_read, ack_write = os.pipe()
os.close(reader)
fd = open_state_dir()
pid = spawn(fd, "claude", ["claude", "remote-control"], pty_mode=True,
            handoff=writer, ack=ack_read)
os.close(writer)
os.close(ack_read)
os.close(ack_write)
os.close(fd)
_, status = os.waitpid(pid, 0)
print(json.dumps({"status": status, "pid": pid}))
"""
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", source],
        input="",
        capture_output=True,
        text=True,
        env={**ctx.env, "FAKE_CLAUDE_MODE": "stubborn"},
        cwd=ctx.work,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert os.WIFEXITED(out["status"]) and os.WEXITSTATUS(out["status"]) == 0, out
    child = int((ctx.fake / "child.pid").read_text())
    assert is_gone(child) and ctx.calls("claude") == []
    assert not (ctx.state / "claude.json").exists()


@pytest.mark.parametrize("name", ["claude", "opencode"])
def test_exec_children_reset_sigpipe(ctx: Ctx, name: str) -> None:
    prefix = """
import os, signal
real_exec = os.execvp
def record_exec(file, argv):
    with open(os.path.join(os.environ["FAKE_DIR"], "exec-signals"), "w") as stream:
        json.dump([int(signal.getsignal(sig)) for sig in (signal.SIGPIPE, signal.SIGXFSZ)], stream)
    real_exec(file, argv)
os.execvp = record_exec
signal.signal(signal.SIGPIPE, signal.SIG_IGN)
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
"""
    source = (
        SOURCE.rsplit('if __name__ == "__main__":', 1)[0]
        + prefix
        + ("\nsys.exit(main(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout))\n")
    )
    args = (
        ["claude-start", "--prefix", "test"]
        if name == "claude"
        else ["opencode-start", "--port", str(ctx.port)]
    )
    rc, out = ctx.run(
        *args,
        stdin=None if name == "claude" else '{"password":"dummy"}',
        timing="claude_grace=0.3,opencode_ready=3",
        source=source,
    )
    assert rc == 0, out
    assert json.loads((ctx.fake / "exec-signals").read_text()) == [int(signal.SIG_DFL)] * 2
    ctx.stop(name)


@pytest.mark.parametrize(
    "rule,code",
    [
        ("env", "env-root"),
        ("path", "state-path"),
        ("remote", "remote-config"),
        ("extra", "service-config"),
        ("json", "service-json"),
        ("safe", None),
    ],
)
def test_opencode_preflight_is_read_only_and_matches_start(
    ctx: Ctx, rule: str, code: str | None
) -> None:
    env = {"XDG_STATE_HOME": "/synthetic/relocated"} if rule == "env" else {}
    service = ctx.service_json()
    service.parent.mkdir(parents=True)
    if rule == "path":
        service.write_text('{"password":"old"}')
        target = ctx.fake / "settings"
        service.parent.rename(target)
        service.parent.symlink_to(target, target_is_directory=True)
    else:
        service.write_text(
            {"remote": '{"remote":true}', "extra": '{"port":42}', "json": "broken"}.get(
                rule, '{"password":"old"}'
            )
        )
    before = None if service.is_symlink() else service.read_bytes()
    ctx.state.rmdir()
    rc, out = ctx.run("opencode-preflight", env=env)
    assert rc == (0 if code is None else 1), out
    assert out == {"ok": True} if code is None else out["code"] == code
    assert not ctx.state.exists()
    assert not ctx.calls("opencode")
    if before is not None:
        assert service.read_bytes() == before
    if code is not None:
        rc, out = ctx.opencode(env=env)
        assert rc == 1 and out["code"] == code, out
        assert not ctx.calls("opencode")


@pytest.mark.parametrize(
    "argv,accepted",
    [
        (["/native/opencode", "serve", "--service"], True),
        (["/native/opencode.exe", "serve", "--service", "--port", "1"], True),
        (["/native/other", "serve", "--service"], False),
        (["/native/opencode.exe", "serve"], False),
        (["/native/opencode.exe", "serve", "--other"], False),
        (["/native/opencode.exe", "pair", "--service"], False),
        (["/usr/bin/python3", "/native/opencode", "serve", "--service"], False),
    ],
)
def test_default_service_cmdline_exact(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], accepted: bool
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(
            "builtins.open", lambda *_a, **_kw: io.BytesIO("\0".join(argv).encode() + b"\0")
        )
        assert mod.cmdline_is_opencode_service(12345) is accepted


def test_opencode_preflight_refuses_arguments(ctx: Ctx) -> None:
    rc, out = ctx.run("opencode-preflight", "--unexpected", "value")
    assert rc == 1 and out["code"] == "usage", out
    assert not ctx.calls("opencode")
