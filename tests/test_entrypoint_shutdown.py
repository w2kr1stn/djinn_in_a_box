"""Tests for the entrypoint's shutdown path — settings persistence on both exits.

The interactive shell exiting normally and `docker stop` (SIGTERM to PID 1) must
both reach the reverse-sync. The detached container has no other shutdown path,
so a regression here loses settings silently.

These run the real section lifted out of ``scripts/entrypoint.sh`` with the sync
helpers stubbed, so the control flow under test is the shipped one.
"""

from __future__ import annotations

import json
import os
import pty
import select
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path

import pytest
from test_output_lib import _entrypoint_environment

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "entrypoint.sh"
SECTION_MARKER = "# Interactive Shell (reverse-sync settings on exit)"

requires_zsh = pytest.mark.skipif(
    shutil.which("zsh") is None, reason="zsh is not installed on this host"
)


def _shutdown_section() -> str:
    """The shipped shutdown section, from its banner to the end of the file."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    marker = text.index(SECTION_MARKER)
    return text[text.rindex("# ====", 0, marker) :]


def _harness(tmp_path: Path) -> Path:
    """Wrap the real section in stubs so it can run outside a container."""
    log = tmp_path / "sync.log"
    (tmp_path / "home").mkdir()
    helper = tmp_path / "settings_copy.py"
    helper.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

    script = tmp_path / "harness.sh"
    script.write_text(
        "#!/bin/zsh\n"
        "set -euo pipefail\n"
        # Never inherit DJINN_DETACHED: djinn's own detached container exports it,
        # so running this suite inside one would flip the very branch under test.
        # Each test declares what it wants through DJINN_TEST_DETACHED instead.
        'DJINN_DETACHED="${DJINN_TEST_DETACHED:-}"\n'
        f'HOME="{tmp_path / "home"}"\n'
        f'TMPDIR="{tmp_path}"\n'
        f'SYNC_LOG="{log}"\n'
        f'source "{ROOT / "scripts" / "seed-lib.sh"}"\n'
        f'SETTINGS_COPY_HELPER="{helper}"\n'
        f'OPENCODE_RUNTIME_SETTINGS="{tmp_path / "opencode.json"}"\n'
        f'OPENCODE_PERSISTENT_SETTINGS="{tmp_path / "persistent-opencode.json"}"\n'
        'reverse_sync_file() { echo "${4}:file:$1" >> "$SYNC_LOG"; }\n'
        'reverse_sync_claude_settings() { echo "${4}:claude:$1" >> "$SYNC_LOG"; }\n'
        # The real UI library, not stubs. Variadic `$*` stubs accept any arity and
        # would hide an arity or `set -u` violation in the section under test —
        # which is exactly how a one-argument `ui_item` call reached production and
        # survived a full review round.
        f'source "{ROOT / "scripts" / "output-lib.sh"}"\n'
        "\n" + _shutdown_section(),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _sync_lines(tmp_path: Path) -> list[str]:
    log = tmp_path / "sync.log"
    if not log.exists():
        return []
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line]
    final = [line for line in lines if line.startswith("final:")]
    assert sum(line.endswith("/opencode.json") for line in final) == 1
    return [line for line in final if not line.endswith("/opencode.json")]


def _probe_until_answered(fd: int, deadline: float) -> bool:
    """Write a probe until the shell answers it, or the deadline passes."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        os.write(fd, b"echo PROBE_$((6*7))\n")
        if _read_until(fd, b"PROBE_42", timeout=1.0):
            return True
    return False


def _read_until(fd: int, needle: bytes, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    buffer = b""
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], 0.2)[0]:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # pragma: no cover - pty closed by the child dying
            break
        if not chunk:
            break
        buffer += chunk
        if needle in buffer:
            return True
    return False


@requires_zsh
def test_interactive_shell_survives_with_no_arguments(tmp_path: Path) -> None:
    """The container passes NO arguments — the shape the other tests never reach.

    `ENTRYPOINT ["/home/dev/entrypoint.sh"]` sets no CMD and no compose file sets
    `command:`, so `"$@"` is empty and the shell must be interactive on its own.
    Backgrounding it reassigns stdin to /dev/null unless the descriptor is handed
    over explicitly; zsh is then non-interactive, reads EOF and exits within
    milliseconds, taking the container with it. Passing `-c <cmd>` (as the tests
    below do) hides this entirely, because such a shell never needs a terminal.
    """
    harness = _harness(tmp_path)
    process, fd = _spawn_pty(harness)
    try:
        time.sleep(1.0)
        assert process.poll() is None, "the shell exited — stdin was not handed over"
        assert _probe_until_answered(fd, deadline=15.0), "the shell did not evaluate input"
    finally:
        _dispose(process)
        os.close(fd)


@requires_zsh
def test_without_a_tty_the_container_stays_up_instead_of_exiting(tmp_path: Path) -> None:
    """No terminal must not mean instant death — that is the whole failure class.

    `docker compose run` selects `-T` from the *client's* stdout, so merely
    redirecting output produces a container without a TTY, whatever stdin does.
    An interactive shell cannot run there, but `djinn enter` (docker exec, which
    brings its own TTY) can — so PID 1 stays up and says why, and a later
    `docker stop` still reaches the reverse-sync.
    """
    harness = _harness(tmp_path)
    sink_path = tmp_path / "output.log"
    with sink_path.open("w+") as sink:
        process = subprocess.Popen(
            [str(harness)],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            start_new_session=True,
        )
        group = os.getpgid(process.pid)
        try:
            time.sleep(1.0)
            assert process.poll() is None, "entrypoint exited instead of staying up"
            # PID 1 only — `docker stop` does not signal the process group. Sending
            # to the group would also hit the keeper directly, so a foreground
            # keeper (the regression `& `+`wait` exists to prevent) would still
            # look fine.
            os.kill(process.pid, signal.SIGTERM)
            assert process.wait(timeout=15) == 128 + signal.SIGTERM
        finally:
            # The keeper is orphaned once PID 1 exits; reap the whole group.
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)
            if process.poll() is None:  # pragma: no cover - only on regression
                process.wait(timeout=5)
        sink.seek(0)
        assert "No TTY available" in sink.read()

    assert len(_sync_lines(tmp_path)) == 2


@requires_zsh
def test_detached_uses_the_keeper_even_though_a_tty_exists(tmp_path: Path) -> None:
    """`--detach` must not leave an interactive shell as PID 1.

    The compose file sets `tty: true`, so a terminal exists and the no-TTY branch
    does not catch this mode. But nobody is on that terminal — consumers attach
    with `djinn enter`, which brings its own TTY through docker exec. An unused
    interactive shell as PID 1 makes the whole session hostage to it: a single EOF
    (a stray attach, a closed pty master, a Ctrl-D) ends the shell with 0, PID 1
    follows, and the container is gone without a signal or an error to point at.
    """
    harness = _harness(tmp_path)
    process, fd = _spawn_pty(harness, detached=True)
    try:
        time.sleep(1.0)
        assert process.poll() is None, "the keeper exited instead of holding"
        os.write(fd, b"echo PROBE_$((6*7))\n")
        assert not _read_until(fd, b"PROBE_42", timeout=2.0), "an interactive shell is running"
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=15) == 128 + signal.SIGTERM
    finally:
        _dispose(process)
        os.close(fd)
    assert len(_sync_lines(tmp_path)) == 2


@requires_zsh
def test_sigterm_persists_with_an_interactive_shell_running(tmp_path: Path) -> None:
    """The shape `docker stop` actually meets: PID 1 with a live interactive zsh.

    The SIGTERM tests below drive `-c sleep 60`, whose shell is non-interactive and
    therefore *does* die on SIGTERM — exactly the property the entrypoint's comment
    says an interactive shell lacks. A trap rewritten to signal the shell and wait
    for it would pass those tests and hang here until the grace period expired,
    losing every setting.
    """
    harness = _harness(tmp_path)
    process, fd = _spawn_pty(harness)
    try:
        time.sleep(1.0)
        assert process.poll() is None, "the interactive shell never came up"
        assert _probe_until_answered(fd, deadline=15.0), "the shell is not interactive"
        os.kill(process.pid, signal.SIGTERM)
        status = process.wait(timeout=15)
        assert status >= 0, "parent was killed rather than exiting through the trap"
        assert status == 128 + signal.SIGTERM
    finally:
        _dispose(process)
        os.close(fd)
    assert len(_sync_lines(tmp_path)) == 2


@requires_zsh
@pytest.mark.parametrize("exit_code", (0, 7))
def test_normal_shell_exit_persists_state_and_keeps_the_exit_code(
    tmp_path: Path, exit_code: int
) -> None:
    result = subprocess.run(
        [str(_harness(tmp_path)), "-c", f"exit {exit_code}"],
        capture_output=True,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == exit_code, result.stderr
    assert len(_sync_lines(tmp_path)) == 2


@requires_zsh
def test_sigterm_persists_state_before_exiting(tmp_path: Path) -> None:
    """`docker stop` is the only shutdown a detached container ever gets."""
    process = subprocess.Popen(
        [str(_harness(tmp_path)), "-c", "sleep 60"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_until_shell_started(process)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=15)
    finally:
        _dispose(process)

    assert process.returncode == 128 + signal.SIGTERM
    # Exactly two: the signal path must not double-run the normal path.
    assert len(_sync_lines(tmp_path)) == 2


@requires_zsh
def test_sigterm_persists_state_only_once(tmp_path: Path) -> None:
    """Repeated SIGTERMs must not double-write the sync.

    What this does NOT pin, deliberately: zsh blocks a signal while its own
    handler runs, so same-signal re-entry cannot happen whether or not
    `_DJINN_STATE_PERSISTED` exists — deleting the flag leaves this green. The
    flag actually guards the *cross-signal* case (TERM then INT), a known
    accepted residual of this change, and asserting today's behaviour there
    would freeze a defect rather than prevent one.
    """
    process = subprocess.Popen(
        [str(_harness(tmp_path)), "-c", "sleep 60"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_until_shell_started(process)
        for _ in range(3):
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        process.wait(timeout=15)
    finally:
        _dispose(process)

    assert len(_sync_lines(tmp_path)) == 2


def _wait_until_shell_started(process: subprocess.Popen[str]) -> None:
    """Let the harness reach `wait` before signalling it.

    Signalling earlier would race the trap installation and test nothing.
    """
    time.sleep(0.5)
    if process.poll() is not None:  # pragma: no cover - only on regression
        raise AssertionError("harness exited before it could be signalled")


def _dispose(process: subprocess.Popen) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def _spawn_pty(harness: Path, *, detached: bool = False) -> tuple[subprocess.Popen, int]:
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            ["/bin/zsh", str(harness)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env={**os.environ, "DJINN_TEST_DETACHED": "true" if detached else ""},
        )
    finally:
        os.close(slave)
    return process, master


CARRIER_NAMES = ("claude-state", "claude-settings", "opencode-settings")


def _wait_for(predicate: Callable[[], bool], description: str, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), description


def _atomic_edit(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + ".edit")
    temporary.write_bytes(content)
    temporary.replace(path)


class Session:
    """Full shipped entrypoint with owned storage and barrier-driven 30 s ticks."""

    def __init__(self, root: Path, persistent: Path | None = None, *, real_sleep: bool = False):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.home = root / "home"
        self.home.mkdir()
        self.persistent = persistent or root / "persistent"
        for name, directory in (
            (".claude", "claude"),
            (".claude_seed", "seed"),
            (".opencode", "opencode"),
        ):
            dest = self.persistent / directory
            dest.mkdir(parents=True, exist_ok=True)
            (self.home / name).symlink_to(dest, target_is_directory=True)
        baseline = self.persistent / "seed/settings.json"
        if not baseline.exists():
            baseline.write_text(
                json.dumps({"hooks": {key: [] for key in ("SessionStart", "PreToolUse", "Stop")}})
            )
            (baseline.parent / "AGENTS.md").write_text("Session instructions.\n")
            (self.persistent / "claude/claude.json").write_bytes(b'{"initial":true}\n')
        self.baseline = baseline.read_bytes()
        self.env = _entrypoint_environment(self.home)
        self.tmp = root / "private"
        self.tmp.mkdir()
        self.env["TMPDIR"] = str(self.tmp)
        self.env["NO_COLOR"] = "1"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.sleep_log = root / "sleeps"
        self.state_log = root / "state"
        self.events = root / "events"
        self.ready = root / "ready"
        self.stop = root / "stop"
        self.hold = root / "hold"
        self.release = root / "release"
        self.blocked = root / "blocked"
        self.fail = root / "fail"
        sleep = shutil.which("sleep")
        mktemp = shutil.which("mktemp")
        assert sleep and mktemp
        self._executable(
            "sleep",
            "#!/usr/bin/python3\nimport os, sys, time\nfrom pathlib import Path\n"
            'if sys.argv[1:] != ["30"]: os.execv('
            + repr(sleep)
            + ", ["
            + repr(sleep)
            + "] + sys.argv[1:])\n"
            f'with Path({str(self.sleep_log)!r}).open("a") as f:\n'
            '    f.write(str(os.getpid()) + "\\n")\n'
            + (
                f'os.execv({sleep!r}, [{sleep!r}, "30"])\n'
                if real_sleep
                else f'while not Path({str(root)!r}, "tick-" + str(os.getpid())).exists():\n'
                "    time.sleep(0.01)\n"
            ),
        )
        self._executable(
            "mktemp",
            "#!/usr/bin/python3\nimport subprocess, sys\nfrom pathlib import Path\n"
            f"r = subprocess.run([{mktemp!r}] + sys.argv[1:], stdin=subprocess.DEVNULL,\n"
            "                   capture_output=True, timeout=5)\n"
            'if r.returncode == 0 and sys.argv[1:3] == ["-d", "-t"]:\n'
            f"    Path({str(self.state_log)!r}).write_bytes(r.stdout)\n"
            "sys.stdout.buffer.write(r.stdout)\nsys.stderr.buffer.write(r.stderr)\nsys.exit(r.returncode)\n",
        )
        self.env["PATH"] = f"{self.bin}:{self.env['PATH']}"
        helper = root / "copier.py"
        helper.write_text(
            "import json, os, runpy, stat, sys, time\nfrom pathlib import Path\n"
            f"m = runpy.run_path({str(ROOT / 'scripts/settings-copy.py')!r})\n"
            f"events = Path({str(self.events)!r})\n"
            "def log(action):\n"
            '    with events.open("a") as f:\n'
            "        f.write(json.dumps([action, os.getpid(), os.getppid(), sys.argv[-1]])"
            ' + "\\n")\n'
            "replace, fsync = os.replace, os.fsync\n"
            "def recorded_fsync(fd):\n"
            '    log("directory-fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file-fsync")\n'
            "    fsync(fd)\n"
            "def recorded_replace(src, dst):\n"
            '    log("replace")\n'
            f"    if Path({str(self.hold)!r}).exists():\n"
            f"        Path({str(self.blocked)!r}).write_text(str(os.getppid()))\n"
            f"        while not Path({str(self.release)!r}).exists(): time.sleep(0.01)\n"
            f"    fault = Path({str(self.fail)!r})\n"
            '    if fault.exists() and fault.read_text() in ("all", str(dst)):\n'
            '        raise OSError("injected replacement failure")\n'
            "    replace(src, dst)\n"
            "os.replace, os.fsync = recorded_replace, recorded_fsync\n"
            'log("begin")\nrc = m["main"]()\nlog("end")\nsys.exit(rc)\n'
        )
        self.env["SETTINGS_COPY_HELPER"] = str(helper)
        self.process: subprocess.Popen | None = None
        self.out = root / "stdout"
        self.err = root / "stderr"
        self.shell = (
            f"touch {shlex.quote(str(self.ready))}; "
            f"while [[ ! -e {shlex.quote(str(self.stop))} ]]; do /bin/sleep 0.02; done; exit 7"
        )

    def _executable(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def start(self, entrypoint: Path = ENTRYPOINT) -> None:
        with self.out.open("w") as out, self.err.open("w") as err:
            self.process = subprocess.Popen(
                ["/bin/zsh", str(entrypoint), "-c", self.shell],
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        _wait_for(
            lambda: self.ready.exists() or self.process.poll() is not None, "shell never started"
        )
        assert self.process.poll() is None, self.err.read_text()

    @property
    def state(self) -> Path:
        return Path(self.state_log.read_text().strip())

    @property
    def runtimes(self) -> tuple[Path, Path, Path]:
        return (
            self.home / ".claude.json",
            self.home / ".claude/settings.json",
            self.home / "runtime-opencode/.opencode.json",
        )

    @property
    def targets(self) -> tuple[Path, Path, Path]:
        return (
            self.home / ".claude/claude.json",
            self.home / ".claude_seed/settings.local.json",
            self.home / ".opencode/.opencode.json",
        )

    def acknowledgements(self) -> tuple[Path, Path, Path]:
        return tuple(self.state / f"{name}.ack" for name in CARRIER_NAMES)

    def sleepers(self) -> list[int]:
        return (
            [int(line) for line in self.sleep_log.read_text().splitlines()]
            if self.sleep_log.exists()
            else []
        )

    def tick(self) -> None:
        _wait_for(lambda: bool(self.sleepers()), "checkpoint worker did not start")
        before = len(self.sleepers())
        (self.root / f"tick-{self.sleepers()[-1]}").touch()
        _wait_for(lambda: len(self.sleepers()) > before, "checkpoint did not finish its tick")

    def close(self) -> None:
        if self.process is not None:
            _dispose(self.process)
            self.process = None
        for state in self.tmp.glob("djinn-session-state.*"):
            state.chmod(0o700)
        shutil.rmtree(self.tmp)

    def finish(self, sig: signal.Signals | None = None) -> str:
        assert self.process is not None
        if sig is None:
            self.stop.touch()
        else:
            os.kill(self.process.pid, sig)
        assert self.process.wait(timeout=5) == (7 if sig is None else 128 + sig)
        assert self.out.read_bytes() == b""
        return self.err.read_text()


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[Callable[..., Session]]:
    instances: list[Session] = []

    def create(**kwargs) -> Session:
        session = Session(tmp_path / str(len(instances)), **kwargs)
        instances.append(session)
        return session

    yield create
    for session in instances:
        session.close()


def _changed_settings(version: int) -> bytes:
    return (
        json.dumps(
            {
                "version": version,
                "personal": True,
                "hooks": {
                    "SessionStart": [],
                    "PreToolUse": [],
                    "Stop": [],
                    "PostToolUse": [{"command": "personal-post"}],
                    "Notification": [{"command": "personal-notify"}],
                },
            }
        ).encode()
        + b"\n"
    )


def _assert_payload(target: Path, expected: bytes, index: int) -> None:
    if index == 1:
        data = json.loads(expected)
        for key in ("SessionStart", "PreToolUse", "Stop"):
            data["hooks"].pop(key, None)
        assert json.loads(target.read_bytes()) == data
    else:
        assert target.read_bytes() == expected


def test_checkpointed_all_three_carriers_survive_sigkill_and_restart(sessions) -> None:
    session = sessions()
    session.start()
    first = (b'{"nonce":"claude-78"}\n', _changed_settings(78), b'{"nonce":"opencode-78"}\n')
    _wait_for(
        lambda: all(path.exists() for path in session.acknowledgements()),
        "initial references missing",
    )
    for path, content in zip(session.runtimes, first, strict=True):
        _atomic_edit(path, content)
    session.tick()
    assert [p.read_bytes() for p in session.acknowledgements()] == list(first)
    for index, target in enumerate(session.targets):
        _assert_payload(target, first[index], index)
    assert session.process is not None
    destination_events = [
        json.loads(line)
        for line in session.events.read_text().splitlines()
        if json.loads(line)[3] in {str(path) for path in session.targets}
    ]
    assert not any(event[2] == session.process.pid for event in destination_events), (
        "final sync ran before the crash"
    )
    _atomic_edit(session.runtimes[0], b'{"unacknowledged":true}')
    assert session.process is not None
    os.killpg(session.process.pid, signal.SIGKILL)
    assert session.process.wait(timeout=5) == -signal.SIGKILL
    assert session.out.read_bytes() == b""
    assert "could not persist" not in session.err.read_text()
    assert (session.home / ".claude_seed/settings.json").read_bytes() == session.baseline
    restarted = sessions(persistent=session.persistent)
    restarted.start()
    assert restarted.runtimes[0].read_bytes() == first[0]
    assert restarted.runtimes[2].read_bytes() == first[2]
    settings = json.loads(restarted.runtimes[1].read_bytes())
    assert settings["version"] == 78 and settings["personal"] is True
    for key in ("SessionStart", "PreToolUse", "Stop"):
        assert settings["hooks"][key] == []
    assert settings["hooks"]["PostToolUse"] == [{"command": "personal-post"}]
    assert settings["hooks"]["Notification"] == [{"command": "personal-notify"}]
    assert (restarted.home / ".claude_seed/settings.json").read_bytes() == session.baseline
    assert "checkpoints" not in restarted.finish()


@pytest.mark.parametrize("index", range(3))
def test_host_only_destination_edit_survives_checkpoint_and_clean_stop(
    sessions, index: int
) -> None:
    session = sessions()
    session.start()
    target = session.targets[index]
    host = b'{"host-only":78}\n'
    _atomic_edit(target, host)
    before = target.stat().st_mtime_ns
    session.tick()
    session.tick()
    assert target.read_bytes() == host
    assert target.stat().st_mtime_ns == before
    assert "could not persist" not in session.finish()
    assert target.read_bytes() == host
    assert target.stat().st_mtime_ns == before


@pytest.mark.parametrize("index", range(3))
def test_later_runtime_change_wins_over_host_edit(sessions, index: int) -> None:
    session = sessions()
    session.start()
    for version in (1, 2):
        content = (
            _changed_settings(version) if index == 1 else json.dumps({"version": version}).encode()
        )
        _atomic_edit(session.runtimes[index], content)
        session.tick()
        assert session.acknowledgements()[index].read_bytes() == content
        _assert_payload(session.targets[index], content, index)
        _atomic_edit(session.targets[index], b'{"host":true}\n')
    session.tick()
    assert session.targets[index].read_bytes() == b'{"host":true}\n'
    assert "could not persist" not in session.finish()
    assert session.targets[index].read_bytes() == b'{"host":true}\n'


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize("invalid", (b"", b'{"partial":', b"{} {}"))
def test_invalid_runtime_json_is_silent_and_retried_next_valid_tick(
    sessions, index: int, invalid: bytes
) -> None:
    session = sessions()
    session.start()
    previous = session.targets[index].read_bytes() if session.targets[index].exists() else None
    ack = session.acknowledgements()[index].read_bytes()
    _atomic_edit(session.runtimes[index], invalid)
    session.tick()
    session.tick()
    assert session.acknowledgements()[index].read_bytes() == ack
    assert (
        session.targets[index].read_bytes() if session.targets[index].exists() else None
    ) == previous
    assert "could not persist" not in session.err.read_text()
    assert "settings copy failed" not in session.err.read_text()
    valid = _changed_settings(3) if index == 1 else b"false\n"
    _atomic_edit(session.runtimes[index], valid)
    session.tick()
    assert session.acknowledgements()[index].read_bytes() == valid
    _assert_payload(session.targets[index], valid, index)
    assert "could not persist" not in session.finish()


@pytest.mark.parametrize("fault", ("copier", "scratch", "deleted", "scratch-final"))
@pytest.mark.parametrize("stop_signal", (None, signal.SIGTERM, signal.SIGINT))
def test_failed_destination_write_warns_once_per_carrier_per_session(
    sessions, fault: str, stop_signal
) -> None:
    session = sessions()
    session.start()
    originals = [path.read_bytes() for path in session.acknowledgements()]
    state = session.state
    if fault == "copier":
        session.fail.write_text("all")
    elif fault in {"scratch", "scratch-final"}:
        assert os.geteuid() != 0
        state.chmod(0o500)
    else:
        shutil.rmtree(state)
    for index, path in enumerate(session.runtimes):
        _atomic_edit(path, _changed_settings(5) if index == 1 else b'{"version":5}\n')
    for _ in range(3):
        session.tick()
    for runtime, target in zip(session.runtimes, session.targets, strict=True):
        assert (
            session.err.read_text().count(f"  [warn] could not persist {runtime} → {target}\n") == 1
        )
    assert "settings copy failed" not in session.err.read_text()
    if fault != "deleted":
        assert [path.read_bytes() for path in session.acknowledgements()] == originals
    if fault == "scratch-final":
        stderr = session.finish(stop_signal)
        for runtime, target in zip(session.runtimes, session.targets, strict=True):
            assert stderr.count(f"  [warn] could not persist {runtime} → {target}\n") == 2
        assert "settings copy failed" not in stderr
        return
    if fault == "copier":
        session.fail.unlink()
    elif fault == "scratch":
        state.chmod(0o700)
    else:
        state.mkdir()
    session.tick()
    assert [path.read_bytes() for path in session.acknowledgements()] == [
        path.read_bytes() for path in session.runtimes
    ]
    session.fail.write_text("all")
    for index, path in enumerate(session.runtimes):
        _atomic_edit(path, _changed_settings(6) if index == 1 else b'{"version":6}\n')
    session.tick()
    for runtime, target in zip(session.runtimes, session.targets, strict=True):
        assert (
            session.err.read_text().count(f"  [warn] could not persist {runtime} → {target}\n") == 1
        )
    stderr = session.finish(stop_signal)
    for index in (0, 1):
        assert (
            stderr.count(
                f"  [warn] could not persist {session.runtimes[index]} → {session.targets[index]}\n"
            )
            == 2
        )
    assert stderr.count("  [warn] could not persist OpenCode personal settings\n") == 1
    assert stderr.count("settings copy failed\n") == 3


@pytest.mark.parametrize("index", range(3))
def test_all_carriers_use_atomic_helper_and_keep_previous_on_failure(sessions, index: int) -> None:
    session = sessions()
    session.start()
    session.targets[index].write_bytes(b'{"old":true}\n')
    original = session.acknowledgements()[index].read_bytes()
    session.fail.write_text(str(session.targets[index]))
    content = _changed_settings(9) if index == 1 else b'{"new":9}\n'
    _atomic_edit(session.runtimes[index], content)
    # A sibling's success proves failures do not abort the strict-option worker.
    sibling = (index + 1) % 3
    sibling_content = _changed_settings(10) if sibling == 1 else b'{"new":10}\n'
    _atomic_edit(session.runtimes[sibling], sibling_content)
    session.events.write_text("")
    session.tick()
    events = [json.loads(line) for line in session.events.read_text().splitlines()]
    assert [e[0] for e in events if e[3] == str(session.targets[index])] == [
        "begin",
        "file-fsync",
        "replace",
        "end",
    ]
    assert session.targets[index].read_bytes() == b'{"old":true}\n'
    assert session.acknowledgements()[index].read_bytes() == original
    assert session.acknowledgements()[sibling].read_bytes() == sibling_content
    assert not list(session.targets[index].parent.glob(".djinn-settings-*"))
    session.fail.unlink()
    session.tick()
    assert session.acknowledgements()[index].read_bytes() == content
    _assert_payload(session.targets[index], content, index)
    session.finish()


@pytest.mark.parametrize("stop_signal", (None, signal.SIGTERM, signal.SIGINT))
def test_clean_stop_joins_inflight_checkpoint_before_final_sync(sessions, stop_signal) -> None:
    session = sessions()
    session.start()
    _wait_for(lambda: bool(session.sleepers()), "no interval sleeper")
    sleeper = session.sleepers()[-1]
    first = b'{"version":1}\n'
    latest = b'{"version":2}\n'
    _atomic_edit(session.runtimes[0], first)
    for index in (1, 2):
        _atomic_edit(session.runtimes[index], _changed_settings(2) if index == 1 else latest)
    session.hold.touch()
    session.events.write_text("")
    (session.root / f"tick-{sleeper}").touch()
    _wait_for(session.blocked.exists, "copier never reached pre-replace barrier")
    worker = int(session.blocked.read_text())
    _atomic_edit(session.runtimes[0], latest)
    assert session.process is not None
    if stop_signal is None:
        session.stop.touch()
    else:
        os.kill(session.process.pid, stop_signal)
    time.sleep(0.15)
    events = [json.loads(line) for line in session.events.read_text().splitlines()]
    assert session.process.poll() is None, "parent exited before joining its in-flight checkpoint"
    assert [event[0] for event in events] == ["begin", "file-fsync", "replace"], (
        "final sync overlapped checkpoint"
    )
    session.hold.unlink()
    session.release.touch()
    assert session.process.wait(timeout=5) == (7 if stop_signal is None else 128 + stop_signal)
    events = [json.loads(line) for line in session.events.read_text().splitlines()]
    active = 0
    final_begins = []
    for action, _pid, ppid, target in events:
        if action == "begin":
            active += 1
            assert active == 1, "concurrent destination writers"
            if ppid == session.process.pid:
                final_begins.append(target)
        elif action == "end":
            active -= 1
    assert active == 0
    assert sorted(final_begins) == sorted(str(path) for path in session.targets)
    assert session.targets[0].read_bytes() == latest
    for index in (1, 2):
        _assert_payload(session.targets[index], session.runtimes[index].read_bytes(), index)
    assert not _pid_running(worker)
    assert not _pid_running(sleeper)
    assert len(session.sleepers()) == 1
    assert not session.state.exists()
    assert session.out.read_bytes() == b""
    assert "stopped unexpectedly" not in session.err.read_text()


def _pid_running(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_clean_stop_reaps_interval_sleeper_without_waiting_30_seconds(sessions) -> None:
    session = sessions(real_sleep=True)
    session.start()
    _wait_for(lambda: bool(session.sleepers()), "literal 30 s sleeper never started")
    sleeper = session.sleepers()[0]
    worker = int(Path(f"/proc/{sleeper}/stat").read_text().rsplit(")", 1)[1].split()[1])
    assert time.time() - session.sleep_log.stat().st_mtime < 2
    start = time.monotonic()
    assert "stopped unexpectedly" not in session.finish(signal.SIGTERM)
    assert time.monotonic() - start < 5
    _wait_for(lambda: not _pid_running(sleeper), "sleeper survived stop", timeout=1)
    assert not _pid_running(worker)
    assert len(session.sleepers()) == 1
    assert not session.state.exists()


@pytest.mark.parametrize("transient", (False, True))
@pytest.mark.parametrize("invalid", (False, True))
def test_unavailable_private_state_degrades_explicitly(
    sessions, transient: bool, invalid: bool
) -> None:
    assert os.geteuid() != 0
    session = sessions()
    session.tmp.chmod(0o500)
    session.start()
    previous = [path.read_bytes() if path.exists() else None for path in session.targets]
    for index, path in enumerate(session.runtimes):
        _atomic_edit(
            path,
            b'{"partial":'
            if invalid
            else (_changed_settings(11) if index == 1 else b'{"new":11}\n'),
        )
    # In degraded mode no reference exists; the documented exception overwrites host-only edits.
    if transient and not invalid:
        _atomic_edit(session.targets[1], b'{"host-only":true}')
    assert session.process is not None
    children = Path(f"/proc/{session.process.pid}/task/{session.process.pid}/children")
    assert len(children.read_text().split()) == 1, (
        "worker started despite unavailable private state"
    )
    assert not session.sleepers()
    assert not session.state_log.exists()
    if transient:
        session.tmp.chmod(0o700)
    stderr = session.finish()
    assert stderr.count("  [warn] settings checkpoints disabled for this session\n") == 1
    assert "stopped unexpectedly" not in stderr
    for index, (runtime, target) in enumerate(zip(session.runtimes, session.targets, strict=True)):
        if transient and not invalid:
            _assert_payload(target, runtime.read_bytes(), index)
        else:
            assert (target.read_bytes() if target.exists() else None) == previous[index]
            suffix = " (settings are not valid JSON)" if transient else ""
            assert stderr.count(f"  [warn] could not persist {runtime} → {target}{suffix}\n") == 1


@pytest.mark.parametrize("worker_signal", (signal.SIGKILL, signal.SIGINT))
def test_worker_exit_status_controls_unexpected_stop_warning(sessions, worker_signal) -> None:
    session = sessions()
    session.start()
    _wait_for(lambda: bool(session.sleepers()), "no sleeper")
    sleeper = session.sleepers()[0]
    worker = int(Path(f"/proc/{sleeper}/stat").read_text().rsplit(")", 1)[1].split()[1])
    os.kill(worker, worker_signal)
    if worker_signal == signal.SIGINT:
        # The worker inherits SIGINT as ignored, so a group-wide Ctrl-C cannot kill
        # an in-flight checkpoint child; only the parent's INT trap stops it.
        time.sleep(0.5)
        assert _pid_running(worker), "worker must ignore SIGINT"
    else:
        _wait_for(lambda: not _pid_running(worker), "worker did not exit")
    for index, path in enumerate(session.runtimes):
        _atomic_edit(path, _changed_settings(12) if index == 1 else b'{"version":12}\n')
    stderr = session.finish()
    assert stderr.count("  [warn] settings checkpoints stopped unexpectedly\n") == (
        1 if worker_signal == signal.SIGKILL else 0
    )
    for index, target in enumerate(session.targets):
        _assert_payload(target, session.runtimes[index].read_bytes(), index)


def test_initial_reference_is_runtime_content_before_shell_start(sessions) -> None:
    session = sessions()
    session.start()
    assert [path.read_bytes() for path in session.acknowledgements()] == [
        path.read_bytes() for path in session.runtimes
    ]
    assert session.state.stat().st_mode & 0o777 == 0o700
    # Startup runtime merge has managed hooks while the overlay contains only personal settings.
    session.targets[1].write_bytes(b'{"host":true}')
    session.tick()
    assert session.targets[1].read_bytes() == b'{"host":true}'
    session.finish()
    assert session.targets[1].read_bytes() == b'{"host":true}'


def test_initial_missing_runtime_can_be_created_later(sessions) -> None:
    session = sessions()
    (session.persistent / "claude/claude.json").unlink()
    session.start()
    assert "disabled" not in session.err.read_text()
    assert not session.acknowledgements()[0].exists()
    _atomic_edit(session.runtimes[0], b'{"created":true}\n')
    session.tick()
    assert (
        session.targets[0].read_bytes()
        == session.acknowledgements()[0].read_bytes()
        == b'{"created":true}\n'
    )
    session.finish()


def test_failed_initial_capture_disables_worker_and_retries_at_stop(sessions) -> None:
    assert os.geteuid() != 0
    session = sessions()
    for index, runtime in enumerate(session.runtimes):
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_bytes(_changed_settings(13) if index == 1 else b'{"initial":13}\n')
    session.runtimes[1].chmod(0)
    # Run the shipped suffix: make an existing file unreadable after startup's restore work.
    entrypoint = session.root / "suffix.zsh"
    entrypoint.write_text(
        "#!/bin/zsh\nset -euo pipefail\n"
        + f'source "{ROOT / "scripts/output-lib.sh"}"\n'
        + f'source "{ROOT / "scripts/seed-lib.sh"}"\n'
        + f'SETTINGS_COPY_HELPER="{ROOT / "scripts/settings-copy.py"}"\n'
        + f'OPENCODE_RUNTIME_SETTINGS="{session.runtimes[2]}"\n'
        + f'OPENCODE_PERSISTENT_SETTINGS="{session.targets[2]}"\n'
        + _shutdown_section()
    )
    session.start(entrypoint)
    assert not session.sleepers()
    assert not list(session.tmp.glob("djinn-session-state.*")), "partial initial state was retained"
    session.runtimes[1].chmod(0o600)
    session.targets[1].write_bytes(b'{"host-only":true}')
    stderr = session.finish()
    assert stderr.count("  [warn] settings checkpoints disabled for this session\n") == 1
    assert "stopped unexpectedly" not in stderr
    for index, target in enumerate(session.targets):
        _assert_payload(target, session.runtimes[index].read_bytes(), index)


@pytest.mark.parametrize("missing_state", (False, True))
def test_missing_destination_directory_is_never_created(sessions, missing_state: bool) -> None:
    session = sessions()
    if missing_state:
        session.tmp.chmod(0o500)
    session.start()
    for index, runtime in enumerate(session.runtimes):
        _atomic_edit(runtime, _changed_settings(14) if index == 1 else b'{"version":14}')
    # The seed target is missing; neither copier mkdir nor degraded final should create it.
    (session.home / ".claude_seed").unlink()
    if not missing_state:
        session.tick()
    stderr = session.finish()
    assert not (session.home / ".claude_seed").exists()
    assert str(session.targets[1]) not in stderr


def test_stop_is_idempotent_and_closes_worker_stdin_and_spare_fd(sessions) -> None:
    session = sessions()
    stdin_file = session.root / "spare-input"
    stdin_file.touch()
    with_fd = session.root / "with-fd.zsh"
    with_fd.write_text(
        ENTRYPOINT.read_text().replace(
            "set -euo pipefail", f"set -euo pipefail\nexec 3<{shlex.quote(str(stdin_file))}", 1
        )
    )
    session.start(with_fd)
    _wait_for(lambda: bool(session.sleepers()), "no sleeper")
    sleeper = session.sleepers()[0]
    worker = int(Path(f"/proc/{sleeper}/stat").read_text().rsplit(")", 1)[1].split()[1])
    assert Path(f"/proc/{worker}/fd/0").resolve() == Path("/dev/null")
    assert not Path(f"/proc/{worker}/fd/3").exists()
    # Supply a harmless wrapper that exercises the public internal stop twice.
    script = session.root / "twice.zsh"
    text = ENTRYPOINT.read_text()
    text = text.replace(
        "    stop_session_checkpointer || :",
        "    stop_session_checkpointer || :\n    stop_session_checkpointer || :",
    )
    script.write_text(text)
    session.finish()
    again = Session(session.root / "again")
    try:
        again.start(script)
        assert "stopped unexpectedly" not in again.finish(signal.SIGTERM)
    finally:
        again.close()


def test_stop_arriving_before_sleeper_pid_assignment_is_rechecked(sessions) -> None:
    session = sessions()
    script = session.root / "early-stop.zsh"
    text = ENTRYPOINT.read_text().replace(
        "        djinn_checkpoint_sleep_pid=$!",
        f"        while [[ ! -s {shlex.quote(str(session.sleep_log))} ]]; do\n"
        + "            /bin/sleep 0.01\n        done\n"
        + "        _session_checkpoint_on_stop\n        djinn_checkpoint_sleep_pid=$!",
    )
    # Arrange a latched stop exactly in the spawn/record window, without signalling foreign PIDs.
    script.write_text(text)
    session.start(script)
    _wait_for(lambda: bool(session.sleepers()), "sleeper was never launched")
    sleeper = session.sleepers()[0]
    _wait_for(lambda: not _pid_running(sleeper), "spawn/record stop left a live sleeper", timeout=1)
    assert "stopped unexpectedly" not in session.finish()


def test_lost_private_state_is_recreated_for_final_sync(sessions) -> None:
    session = sessions()
    session.start()
    shutil.rmtree(session.state)
    for index, runtime in enumerate(session.runtimes):
        _atomic_edit(runtime, _changed_settings(15) if index == 1 else b'{"version":15}')
    assert "could not persist" not in session.finish()
    for index, target in enumerate(session.targets):
        _assert_payload(target, session.runtimes[index].read_bytes(), index)


def test_sync_call_context_keeps_strict_worker_alive_and_preserves_final_status(sessions) -> None:
    session = sessions()
    script = session.root / "context.zsh"
    options = session.root / "options"
    wrapper = (
        "functions[_test_real_sync]=$functions[sync_session_state]\n"
        "sync_session_state() {\n"
        f'    echo "$options[errexit]:$options[nounset]:$options[pipefail]" >> "{options}"\n'
        "    false\n"
        '    _test_real_sync "$@"\n'
        "}\n"
    )
    script.write_text(
        ENTRYPOINT.read_text().replace(
            "\nstart_session_checkpointer\n", "\n" + wrapper + "start_session_checkpointer\n"
        )
    )
    session.start(script)
    _atomic_edit(session.runtimes[0], b'{"context":true}')
    session.tick()
    assert session.acknowledgements()[0].read_bytes() == b'{"context":true}'
    assert "stopped unexpectedly" not in session.finish()
    assert options.read_text().splitlines() == ["on:on:on", "on:on:on"]


def test_stop_does_not_signal_a_pid_outside_shell_jobs(sessions) -> None:
    session = sessions()
    sibling = subprocess.Popen(
        ["/bin/sleep", "60"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    script = session.root / "stale-pid.zsh"
    signalled = session.root / "signalled"
    # The stale PID belongs to a separate test-owned group. Intercept kill before
    # testing it; even the deliberately broken mutation cannot signal that sibling.
    injection = (
        "stop_session_checkpointer || :\n"
        "sleep 60 </dev/null >/dev/null 2>&1 &\n"
        f"djinn_checkpoint_pid={sibling.pid}\n"
        f'kill() {{ echo "$*" >> "{signalled}"; }}\n'
        "stop_session_checkpointer || :\n"
        "unfunction kill\n"
    )
    script.write_text(
        ENTRYPOINT.read_text().replace(
            "\nstart_session_checkpointer\n", "\nstart_session_checkpointer\n" + injection
        )
    )
    try:
        session.start(script)
        session.finish()
        assert not signalled.exists(), "stop signalled a PID outside this shell's jobs"
        assert sibling.poll() is None
    finally:
        _dispose(sibling)



def test_inflight_stop_has_no_stale_interval_pid_to_signal(sessions) -> None:
    session = sessions()
    script = session.root / "record-kill.zsh"
    signalled = session.root / "interval-signals"
    injection = (
        '    kill() {\n'
        f'        echo "$*" >> "{signalled}"\n'
        # Intercept every stale interval signal; never deliver it, even in a mutant.
        '        return 0\n'
        '    }\n'
    )
    script.write_text(ENTRYPOINT.read_text().replace(
        '_session_checkpoint_loop() {\n', '_session_checkpoint_loop() {\n' + injection
    ))
    session.start(script)
    _wait_for(lambda: bool(session.sleepers()), "no interval sleeper")
    sleeper = session.sleepers()[0]
    _atomic_edit(session.runtimes[0], b'{"inflight":true}')
    session.hold.touch()
    (session.root / f"tick-{sleeper}").touch()
    _wait_for(session.blocked.exists, "no in-flight copy")
    assert session.process is not None
    os.kill(session.process.pid, signal.SIGTERM)
    time.sleep(0.1)
    session.hold.unlink()
    session.release.touch()
    assert session.process.wait(timeout=5) == 143
    assert not signalled.exists(), "stop retained and tried to signal a reaped sleeper PID"


def test_parent_traps_are_installed_before_worker_start(sessions) -> None:
    session = sessions()
    script = session.root / "trap-readiness.zsh"
    wrapper = (
        'functions[_test_real_start]=$functions[start_session_checkpointer]\n'
        'start_session_checkpointer() {\n'
        '    kill -TERM $$\n'
        '    _test_real_start\n'
        '}\n'
    )
    text = ENTRYPOINT.read_text()
    anchor = '    exit $((128 + $1))\n}\n'
    assert anchor in text
    script.write_text(text.replace(anchor, anchor + wrapper, 1))
    result = subprocess.run(
        ["/bin/zsh", str(script), "-c", "true"], env=session.env,
        stdin=subprocess.DEVNULL, capture_output=True, start_new_session=True,
        timeout=5, check=False,
    )
    assert result.returncode == 143, "startup signal missed the installed parent trap"
    assert result.stdout == b""
    assert not session.sleepers(), "signal probe should exit before worker launch"
