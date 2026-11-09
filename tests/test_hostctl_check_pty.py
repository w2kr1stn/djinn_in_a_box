"""`djinn hostctl check` on a real pseudo-terminal with a fake ssh (no Docker, no network)."""

from __future__ import annotations

import fcntl
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from djinn_in_a_box.core import hostctl

HELPER_ID = "ab" * 32
GEN = "0123456789abcdef" * 2
BANNER = (
    b"# Tailscale SSH requires an additional check.\n"
    b"# To authenticate, visit: https://example.invalid/a/0123456789\n"
)

DRIVER = """
import json, sys
from djinn_in_a_box.core import hostctl
from djinn_in_a_box.commands.hostctl import app

HELPER_ID, GEN = {helper_id!r}, {gen!r}
hostctl.inspect_helper = lambda: {{
    "Id": HELPER_ID,
    "State": {{"Running": True}},
    "Config": {{"Labels": {{hostctl.GENERATION_LABEL: GEN}}}},
}}
hostctl.command = lambda *a, **k: json.dumps(
    {{"generation": GEN, "admission": True, "paused": False}}
)
app(["check"], prog_name="hostctl")
"""

# The fake ssh records what it inherited; the banner goes to stderr like a real client's.
FAKE_SSH = """#!{python}
import json, os, signal, subprocess, sys, time
out = os.environ["FAKE_OUT"]
info = {{
    "argv": sys.argv[1:],
    "stdin_tty": os.isatty(0),
    "stdout_tty": os.isatty(1),
    "stderr_tty": os.isatty(2),
    "pgrp": os.getpgrp(),
    "config_exists": os.path.exists(sys.argv[2]),
    "pid": os.getpid(),
}}
def write(value):
    json.dump(value, open(out + ".tmp", "w"))
    os.replace(out + ".tmp", out)


if os.environ.get("FAKE_MODE") == "banner":
    os.write(2, {banner!r})
    write(info)
    sys.exit(0)
proxy = subprocess.Popen(["sleep", "300"])
info["proxy"] = proxy.pid
write(info)
try:
    proxy.wait()
except KeyboardInterrupt:
    proxy.wait()
    sys.exit(255)
"""


class Terminal:
    def __init__(self, tmp_path: Path, mode: str) -> None:
        self.state = tmp_path / "state"
        self.out = tmp_path / "fake-ssh.json"
        bindir = tmp_path / "bin"
        bindir.mkdir()
        fake = bindir / "ssh"
        fake.write_text(FAKE_SSH.format(python=sys.executable, banner=BANNER))
        fake.chmod(0o755)
        driver = tmp_path / "driver.py"
        driver.write_text(DRIVER.format(helper_id=HELPER_ID, gen=GEN))
        env = {
            **os.environ,
            "XDG_STATE_HOME": str(self.state),
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "FAKE_OUT": str(self.out),
            "FAKE_MODE": mode,
            "TERM": "dumb",
            "COLUMNS": "200",
        }
        self.root = hostctl.state_root()
        (self.root / "trust.json").write_text(json.dumps(self.trust()))
        (self.root / "opening.json").write_text(json.dumps(self.opening()))
        self.master, slave = os.openpty()

        def controlling_terminal() -> None:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.process = subprocess.Popen(
            [sys.executable, "-I", str(driver)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
            preexec_fn=controlling_terminal,
        )
        os.close(slave)
        self.transcript = b""

    @staticmethod
    def trust() -> dict:
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFh"
        peer = {
            "address": "host-a.example.ts.net",
            "ip": "100.64.0.1",
            "ips": ["100.64.0.1"],
            "host_key_alias": "host-a.example.ts.net",
            "keys": [key],
        }
        return {"generation": GEN, "peers": {"host-a": peer}, "routes": {}}

    @staticmethod
    def opening() -> dict:
        hosts = {"host-a": {"address": "host-a.example.ts.net", "user": "alice"}}
        return {"generation": GEN, "config": {"hosts": hosts}}

    def pump(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if not ready:
                if self.process.poll() is not None:
                    return
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                return
            if not chunk:
                return
            self.transcript += chunk

    def wait_for(self, predicate, timeout: float = 15) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            self.pump(0.2)
        return False

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait()
        os.close(self.master)


@pytest.fixture
def make_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    made = []

    def build(mode: str) -> Terminal:
        value = Terminal(tmp_path, mode)
        made.append(value)
        return value

    yield build
    for value in made:
        value.close()


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


def test_check_banner_inherits_terminal(make_terminal):
    # Mutation P28: capture stdout/stderr or detach stdin; the banner must reach the terminal.
    term = make_terminal("banner")
    term.pump(20)
    assert term.process.wait(10) == 0
    text = term.transcript.replace(b"\r\n", b"\n")  # the line discipline adds the CR
    start, result = b"Checking host-a...", b"host-a: approved and reachable"
    assert start in text and result in text and BANNER in text
    assert text.index(start) < text.index(BANNER) < text.index(result)
    info = json.loads(term.out.read_text())
    assert info["stdin_tty"] and info["stderr_tty"] and info["stdout_tty"]
    assert info["argv"][0] == "-F" and info["argv"][2:] == ["-T", "-n", "host-a", "true"]
    assert info["config_exists"]
    assert not (term.root / "journal.jsonl").exists()
    assert [p.name for p in term.root.iterdir() if p.name.startswith("check-")] == []
    for path in term.root.iterdir():
        if path.is_file():
            assert b"example.invalid" not in path.read_bytes()


def test_check_ctrl_c_pty_owned_processes(make_terminal):
    # Mutation P24/P31: no cleanup on interrupt; wrong exit code or missing interrupted line.
    term = make_terminal("wait")
    assert term.wait_for(term.out.exists), term.transcript
    info = json.loads(term.out.read_text())
    assert any(p.name.startswith("check-") for p in term.root.iterdir())
    os.write(term.master, b"\x03")
    term.pump(20)
    assert term.process.wait(10) == 130, term.transcript
    assert b"host-a: check interrupted" in term.transcript
    assert not alive(info["pid"]) and not alive(info["proxy"])
    assert [p.name for p in term.root.iterdir() if p.name.startswith("check-")] == []


def test_on_wait_then_open_under_script_pty(tmp_path):
    calls = tmp_path / "supervisor-calls.json"
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(f"""
import json, sys
from pathlib import Path
calls = Path({str(calls)!r})
seen = json.loads(calls.read_text()) if calls.exists() else []
seen.append(sys.argv[1:])
calls.write_text(json.dumps(seen))
assert sys.argv[1:] == ["status", {GEN!r}]
print(json.dumps({{"generation": {GEN!r}, "admission": len(seen) >= 2,
                  "paused": False, "closed": False}}))
""")
    driver = tmp_path / "wait-driver.py"
    driver.write_text(f"""
import os, subprocess, sys
from djinn_in_a_box.commands import hostctl as cli
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import hostctl, host_sealing
assert all(os.isatty(fd) for fd in (0, 1, 2))
cli.load_config = lambda: AppConfig(code_dir={str(tmp_path)!r})
hostctl.open_window = lambda *a, **k: {GEN!r}
host_sealing.inspect_assessment = lambda: host_sealing.Assessment(None)
hostctl.inspect_helper = lambda: {{
    "Id": {HELPER_ID!r}, "State": {{"Running": True}},
    "Config": {{"Labels": {{hostctl.GENERATION_LABEL: {GEN!r}}}}},
}}
hostctl.read_window = lambda helper: {{"generation": {GEN!r}, "closed": False}}
hostctl.node_status = lambda helper: {{
    "BackendState": "NeedsLogin", "AuthURL": "https://example.invalid/login",
}}
hostctl.cached_trust = lambda: None
def command(*args, **kwargs):
    assert args[:3] == ("exec", {HELPER_ID!r}, hostctl.SUPERVISOR)
    return subprocess.check_output(
        [sys.executable, {str(supervisor)!r}, *args[3:]], text=True,
    )
hostctl.command = command
cli.app(["on", "--wait", "--timeout", "5"], prog_name="hostctl")
""")
    command = shlex.join([sys.executable, "-I", str(driver)])
    result = subprocess.run(
        ["script", "-qec", command, "/dev/null"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"), "COLUMNS": "200"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("Waiting for the window to open (timeout 5 s) ...") == 1
    assert result.stdout.count("Login on the host: https://example.invalid/login") == 1
    assert "Observation: unavailable; journal/log gaps are unknown." in result.stdout
    assert json.loads(calls.read_text()) == [["status", GEN], ["status", GEN]]
