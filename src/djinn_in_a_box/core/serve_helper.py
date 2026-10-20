"""Container-side helper of ``djinn serve``.

Runs inside the Djinn container as ``python3 -I -c <this source> <subcommand> [args]``
(Debian's Python 3.11, standard library only; nothing from ``djinn_in_a_box`` is imported).
It is read as text by the host side and shipped as one argv element, so it stays below
100 KB and uses no syntax or API newer than Python 3.11.

Subcommands (one JSON object plus a newline on stdout, exit 0 = ok, 1 = error):

* ``status``
* ``claude-start --prefix <slug>``
* ``opencode-start --port <port> [--cors <url>]`` (``{"password": "..."}`` on stdin)
* ``stop --name <name> [--name <name>]``

Process state lives in ``/tmp/djinn-serve`` (outside volumes and binds, so it ends with the
container). Test-only environment overrides, never set in production:
``DJINN_SERVE_TEST_STATE_DIR`` (state directory), ``DJINN_SERVE_TEST_HOME`` (the home directory
OpenCode must use instead of ``/home/dev``) and ``DJINN_SERVE_TEST_TIMING`` (comma separated
``name=seconds`` pairs for the keys of ``_TIMING``).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hmac
import json
import os
import pty
import re
import select
import signal
import stat
import struct
import sys
import termios
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from types import FrameType
from typing import IO, Final, NoReturn, cast

STATE_DIR: Final = "/tmp/djinn-serve"  # nosec B108 - fixed, 0700, dev-owned and fstat-checked
HOME_DIR: Final = "/home/dev"
OPENCODE_PORT: Final = 4096
TEST_STATE_ENV: Final = "DJINN_SERVE_TEST_STATE_DIR"
TEST_HOME_ENV: Final = "DJINN_SERVE_TEST_HOME"
TEST_TIMING_ENV: Final = "DJINN_SERVE_TEST_TIMING"

NAMES: Final = ("claude", "opencode")
# Frozen protocol enums (PLAN section 2); the host keeps a copy and one schema test compares both.
STATES: Final = ("running", "degraded", "exited", "stopped")
START_RESULTS: Final = ("started", "already-running", "restarted", "exited")
STOP_RESULTS: Final = ("stopped", "not-running", "survived")
ERROR_CODES: Final = (
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

LOCK_WAIT: Final = 10.0
LOG_CAP: Final = 1 << 20
TAIL_LINES: Final = 20
PASSWORD_CAP: Final = 4096
MAX_FILE: Final = 1 << 20
TAIL_BYTES: Final = 64 * 1024
WINSIZE: Final = (40, 120)
_TIMING: Final = {
    "deadline": 50.0,
    "lock_wait": LOCK_WAIT,
    "stop_wait": 10.0,
    "kill_wait": 2.0,
    "opencode_ready": 15.0,
    "claude_grace": 5.0,
    "handoff_wait": 2.0,
    "launch_cleanup": 3.0,
}

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b")
_UNPRINTABLE: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
_BIND_ALL: Final = "0.0.0.0"  # nosec B104 - in-container bind; the host publishes loopback only
_PREFIX = re.compile(r"[a-z0-9-]{1,63}")
_CORS = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?")
_KEY = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_URL_PORT = re.compile(r"https?://[^/:]+:([0-9]{1,5})(?:/|$)")
_TRUNCATED: Final = b"[djinn serve: log truncated]\n"
_LAUNCH_ACK: Final = b"ack\n"
_LAUNCH_GO: Final = b"go\n"


class HelperError(Exception):
    """A protocol error: ``code`` is one of ``ERROR_CODES``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DeadlineError(Exception):
    """The one monotonic deadline of the subcommand ran out."""


def timing(name: str) -> float:
    """Return a duration; only the test environment may override the defaults."""
    for item in os.environ.get(TEST_TIMING_ENV, "").split(","):
        key, _, value = item.partition("=")
        if key == name:
            with contextlib.suppress(ValueError):
                return float(value)
    return _TIMING[name]


class Deadline:
    """One monotonic deadline per subcommand; every step gets min(its bound, time left)."""

    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        return self.end - time.monotonic()

    def wait(self, bound: float, done: Callable[[], bool], interval: float = 0.05) -> bool:
        """Poll ``done()`` for up to ``bound`` seconds; a step cut by the deadline raises."""
        own_end = time.monotonic() + bound
        end = min(own_end, self.end)
        while True:
            if done():
                return True
            now = time.monotonic()
            if now >= end:
                if own_end > self.end:
                    raise DeadlineError
                return False
            time.sleep(min(interval, max(end - now, 0.0)))


# --------------------------------------------------------------------------- /proc


def proc_stat(pid: int) -> list[str] | None:
    """Fields after the command name in ``/proc/<pid>/stat`` (state first), or None."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        return text.rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None


def raw_start(pid: int) -> str | None:
    """Start time in clock ticks (field 22), also of a zombie that is still unreaped."""
    fields = proc_stat(pid)
    return fields[19] if fields is not None and len(fields) > 19 else None


def proc_start(pid: int) -> str | None:
    """Start time of a live process; None when gone (state ``Z``/``X`` counts as gone)."""
    fields = proc_stat(pid)
    if fields is None or len(fields) <= 19 or fields[0] in ("Z", "X"):
        return None
    return fields[19]


def pid1_start() -> str:
    start = raw_start(1)
    if start is None:
        raise HelperError("state-dir", "cannot read the start time of PID 1")
    return start


def group_members(pgid: int) -> list[int]:
    """Start times (ticks) of the live processes whose process group is ``pgid``."""
    starts: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fields = proc_stat(int(entry))
        if (
            fields is not None
            and len(fields) > 19
            and fields[0] not in ("Z", "X")
            and fields[2] == str(pgid)
            and fields[19].isdigit()
        ):
            starts.append(int(fields[19]))
    return starts


def group_state(pid: int, start: str) -> str:
    """``alive`` | ``gone`` | ``stale`` for the group led by recorded ``pid``/``start``.

    A recorded pid that still exists must carry exactly the recorded start time (else the pid
    was reused: stale). If the leader no longer exists, the group is alive while any process has
    ``pgrp == pid`` and a start time at or after the recorded one.
    """
    fields = proc_stat(pid)
    if fields is not None:
        if len(fields) <= 19 or fields[19] != start:
            return "stale"
        if fields[0] not in ("Z", "X"):
            return "alive"
    return "alive" if any(s >= int(start) for s in group_members(pid)) else "gone"


def socket_inodes(pid: int) -> set[int]:
    inodes: set[int] = set()
    try:
        entries = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return inodes
    for entry in entries:
        with contextlib.suppress(OSError):
            target = os.readlink(f"/proc/{pid}/fd/{entry}")
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(int(target[8:-1]))
    return inodes


def listener_inodes(port: int, *, wildcard_v4: bool) -> set[int]:
    """Inodes of LISTEN sockets on ``port`` (``wildcard_v4``: only 0.0.0.0, no IPv6)."""
    inodes: set[int] = set()
    for table in ("tcp",) if wildcard_v4 else ("tcp", "tcp6"):
        try:
            with open(f"/proc/net/{table}", encoding="utf-8") as handle:
                rows = handle.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            address, _, hexport = fields[1].partition(":")
            if wildcard_v4 and address != "00000000":
                continue
            if int(hexport, 16) == port and fields[9].isdigit():
                inodes.add(int(fields[9]))
    return inodes


def listener_ours(pid: int, port: int) -> bool:
    """A LISTEN socket on 0.0.0.0:<port> whose inode belongs to ``pid``."""
    return bool(listener_inodes(port, wildcard_v4=True) & socket_inodes(pid))


def foreign_listener(pid: int | None, port: int) -> bool:
    """Some LISTEN socket on the port that ``pid`` does not own."""
    mine: set[int] = socket_inodes(pid) if pid is not None else set()
    return bool(listener_inodes(port, wildcard_v4=False) - mine)


def cmdline_is_opencode_service(pid: int) -> bool:
    """Require the native OpenCode executable followed by ``serve --service``."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read(65536)
    except OSError:
        return False
    args = raw.decode("utf-8", "replace").split("\0")
    return (
        len(args) > 2
        and os.path.basename(args[0]) in ("opencode", "opencode.exe")
        and args[1:3] == ["serve", "--service"]
    )


# ------------------------------------------------------------------------- signals


def signal_group(pgid: int, sig: int) -> None:
    """Signal a process group; never a pid or group <= 1 and never our own group."""
    if pgid <= 1 or pgid == os.getpgrp():
        raise HelperError("state-dir", "refusing to signal a protected process group")
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def signal_pid(pid: int, sig: int) -> None:
    """Signal one process; never pid <= 1, ourselves or a member of our own group."""
    if pid <= 1 or pid == os.getpid():
        raise HelperError("state-dir", "refusing to signal a protected process")
    try:
        if os.getpgid(pid) == os.getpgrp():
            raise HelperError("state-dir", "refusing to signal a member of the helper's group")
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return


# ---------------------------------------------------------------------- records


@dataclass(frozen=True)
class Record:
    pid: int
    start: str
    pid1_start: str
    cwd: str
    argv_tag: str
    started_at: str
    child_pid: int | None = None
    child_start: str | None = None
    port: int | None = None
    cors: str | None = None


def _as_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return {str(k): v for k, v in cast("dict[object, object]", value).items()}
    return None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ticks(value: object) -> str | None:
    return value if isinstance(value, str) and value.isdigit() else None


def encode_record(record: Record) -> bytes:
    return (json.dumps(asdict(record), sort_keys=True) + "\n").encode("utf-8")


def decode_record(raw: bytes) -> Record | None:
    """Parse a record; anything not matching the schema is None."""
    try:
        data = _as_dict(json.loads(raw.decode("utf-8")))
    except ValueError:
        return None
    if data is None:
        return None
    pid, start, p1 = (
        _int(data.get("pid")),
        _ticks(data.get("start")),
        _ticks(data.get("pid1_start")),
    )
    cwd, tag, at = data.get("cwd"), data.get("argv_tag"), data.get("started_at")
    if pid is None or start is None or p1 is None:
        return None
    if not (isinstance(cwd, str) and isinstance(tag, str) and isinstance(at, str)):
        return None
    child_pid, child_start, port = (
        _int(data.get("child_pid")),
        _ticks(data.get("child_start")),
        _int(data.get("port")),
    )
    if (child_pid is None) != (child_start is None):
        return None
    if data.get("port") is not None and port is None:
        return None
    cors = data.get("cors")
    if cors is not None and (not isinstance(cors, str) or not _CORS.fullmatch(cors)):
        return None
    return Record(pid, start, p1, cwd, tag, at, child_pid, child_start, port, cors)


def load_record(dirfd: int, name: str) -> Record | None:
    raw = read_nofollow(f"{name}.json", dirfd, "state-dir")
    return decode_record(raw) if raw is not None else None


def remove_file(dirfd: int, name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=dirfd)


def read_nofollow(name: str, dirfd: int | None, code: str) -> bytes | None:
    """Read a small regular file without following a symlink; None when absent."""
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HelperError("state-path", f"{os.path.basename(name)} is a symbolic link") from exc
        raise HelperError(code, f"cannot open {os.path.basename(name)}: {exc.strerror}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HelperError(code, f"{os.path.basename(name)} is not a regular file")
        return os.read(fd, MAX_FILE)
    finally:
        os.close(fd)


def write_atomic(dirfd: int, final: str, data: bytes) -> None:
    """Write ``final`` via a random temp file in the same directory and ``os.replace``."""
    tmp = f".{final}.{os.urandom(6).hex()}.tmp"
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dirfd,
    )
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except BaseException:
        remove_file(dirfd, tmp)
        raise


def save_record(dirfd: int, name: str, record: Record) -> None:
    write_atomic(dirfd, f"{name}.json", encode_record(record))


# ------------------------------------------------------------------ state dir, lock


def open_state_dir() -> int:
    path = os.environ.get(TEST_STATE_ENV) or STATE_DIR
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise HelperError("state-dir", f"cannot create {path}: {exc.strerror}") from exc
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise HelperError("state-dir", f"cannot open {path}: {exc.strerror}") from exc
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        raise HelperError(
            "state-dir", f"{path} must be a directory owned by the user with mode 0700"
        )
    return fd


def acquire_lock(dirfd: int, deadline: Deadline | None) -> int | None:
    """Take the state lock. Polled with LOCK_NB for ``lock_wait`` inside the deadline.

    ``deadline=None`` is the non-blocking probe of ``status`` and returns None when busy.
    """
    fd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dirfd)
    own_end = time.monotonic() + timing("lock_wait")
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            pass
        if deadline is None:
            os.close(fd)
            return None
        now = time.monotonic()
        if now >= deadline.end:
            os.close(fd)
            raise DeadlineError
        if now >= own_end:
            os.close(fd)
            raise HelperError("busy", "another djinn serve start or stop holds the lock")
        time.sleep(0.05)


# --------------------------------------------------------------------- log tail


def clean_lines(text: str, count: int = TAIL_LINES) -> list[str]:
    """ANSI/control stripped, blank lines dropped, the last ``count`` lines, each <= 300 chars."""
    text = _ANSI.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [
        "".join(
            c for c in line.replace("\t", " ") if unicodedata.category(c) not in _UNPRINTABLE
        ).strip()[:300]
        for line in text.split("\n")
    ]
    return [line for line in lines if line][-count:]


def sanitize_tail(path: str, count: int = TAIL_LINES, *, dir_fd: int | None = None) -> list[str]:
    """Sanitized last lines of the log at ``path`` (read-only, bounded, no symlink)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return []
    try:
        size = os.fstat(fd).st_size
        offset = max(0, size - TAIL_BYTES)
        data = os.pread(fd, TAIL_BYTES, offset)
    finally:
        os.close(fd)
    text = data.decode("utf-8", "replace")
    if offset:
        text = text.partition("\n")[2]
    return clean_lines(text, count)


# --------------------------------------------------------------- OpenCode settings


def opencode_home() -> str:
    return os.environ.get(TEST_HOME_ENV) or HOME_DIR


def check_env_root(home: str) -> None:
    """Refuse relocated OpenCode roots (normalized HOME, no symlink resolution)."""
    if os.path.normpath(os.environ.get("HOME", "")) != home:
        raise HelperError("env-root", "HOME does not name the container user's home directory")
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "OPENCODE_CONFIG_DIR"):
        if name in os.environ:
            raise HelperError("env-root", f"{name} relocates OpenCode's settings")


def check_settings_paths(home: str) -> None:
    """Refuse a symbolic link on any component of OpenCode's settings paths."""
    for parts in (
        (".config", "opencode", "service.json"),
        (".local", "state", "opencode", "service.json"),
    ):
        path = home
        for part in parts:
            path = os.path.join(path, part)
            try:
                mode = os.lstat(path).st_mode
            except FileNotFoundError:
                break
            except OSError as exc:
                raise HelperError("state-path", f"cannot inspect {path}: {exc.strerror}") from exc
            if stat.S_ISLNK(mode):
                raise HelperError(
                    "state-path", f"OpenCode's settings path contains a symbolic link: {path}"
                )


def read_service_config(home: str) -> dict[str, object] | None:
    raw = read_nofollow(os.path.join(home, ".config/opencode/service.json"), None, "service-json")
    if raw is None:
        return None
    data = None
    with contextlib.suppress(ValueError):
        data = _as_dict(json.loads(raw.decode("utf-8")))
    if data is None:
        raise HelperError("service-json", "OpenCode's service.json is not a JSON object")
    return data


def check_service_config(config: dict[str, object] | None) -> None:
    """Allowlist: only ``password`` may be present (``remote`` has its own code)."""
    extra = sorted(key for key in (config or {}) if key != "password")
    if not extra:
        return
    parts = [
        f"service.json contains '{k}'; remove it with: opencode service unset {k}"
        if _KEY.fullmatch(k)
        else "service.json contains an unsupported key"
        for k in extra
    ]
    raise HelperError("remote-config" if "remote" in extra else "service-config", "; ".join(parts))


def stored_password_equals(home: str, password: str) -> bool:
    """Compare with the stored password; the value is never printed."""
    config = read_service_config(home)
    stored = config.get("password") if config else None
    return isinstance(stored, str) and hmac.compare_digest(stored.encode(), password.encode())


def _open_settings_dir(home: str) -> int:
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in (".config", "opencode"):
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=fd)
            try:
                nxt = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
                )
            except OSError as exc:
                raise HelperError(
                    "state-path", f"cannot open {part} without following links"
                ) from exc
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def merge_password(home: str, password: str) -> None:
    """Merge ``{"password": ...}`` into service.json (0600, other keys kept, no symlink)."""
    fd = _open_settings_dir(home)
    try:
        config = read_service_config(home) or {}
        config["password"] = password
        write_atomic(fd, "service.json", (json.dumps(config, indent=2) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def read_discovery(home: str) -> tuple[int, str] | None:
    """``(pid, url)`` of OpenCode's discovery file; only these two fields are ever read."""
    raw = read_nofollow(
        os.path.join(home, ".local/state/opencode/service.json"), None, "service-json"
    )
    data = _as_dict(json.loads(raw.decode("utf-8", "replace"))) if raw else None
    pid = _int(data.get("pid")) if data else None
    url = data.get("url") if data else None
    if pid is None:
        return None
    return pid, url if isinstance(url, str) else ""


def discovery_pid(home: str) -> int | None:
    try:
        found = read_discovery(home)
    except (HelperError, ValueError):
        return None
    return found[0] if found else None


# -------------------------------------------------------------------- state function


@dataclass(frozen=True)
class Eval:
    state: str
    reason: str | None = None
    stale: bool = False
    listening: bool = False
    discovery_ok: bool = False
    foreign: bool = False


def record_groups(record: Record) -> list[tuple[int, str]]:
    """Recorded groups in signal order (Claude: PTY child group first, then the supervisor)."""
    groups = [(record.pid, record.start)]
    if record.child_pid is not None and record.child_start is not None:
        groups.insert(0, (record.child_pid, record.child_start))
    return groups


def evaluate(name: str, record: Record | None, home: str) -> Eval:
    """Total state function: no record, stale, exited, degraded or running."""
    port = record.port if record and record.port else OPENCODE_PORT
    if record is None:
        return Eval("stopped", foreign=name == "opencode" and foreign_listener(None, port))
    if record.pid1_start != pid1_start():
        return Eval("stopped", "stale record", stale=True)
    main = group_state(record.pid, record.start)
    if name == "claude":
        child = (
            group_state(record.child_pid, record.child_start)
            if record.child_pid is not None and record.child_start is not None
            else "gone"
        )
        if "stale" in (main, child):
            return Eval("stopped", "stale record", stale=True)
        if main == "alive" and child == "alive":
            return Eval("running")
        if main == "alive":
            return Eval("degraded", "child gone")
        if child == "alive":
            return Eval("degraded", "supervisor gone")
        return Eval("exited")
    if main == "stale":
        return Eval("stopped", "stale record", stale=True)
    if main == "gone":
        return Eval("exited", foreign=foreign_listener(None, port))
    listening = listener_ours(record.pid, port)
    found = discovery_pid(home)
    ours = found == record.pid
    if listening and ours:
        return Eval("running", listening=True, discovery_ok=True)
    if not listening:
        reason = "listener lost"
    elif found is None:
        reason = "not registered"
    else:
        reason = "registered to another process"
    return Eval(
        "degraded",
        reason,
        listening=listening,
        discovery_ok=ours,
        foreign=foreign_listener(record.pid, port),
    )


# ------------------------------------------------------------------------- spawning


def _reset_signals() -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)


def _close_other_fds(*keep: int | None) -> None:
    """Explicitly close every descriptor above 2 except ``keep`` (no reliance on CLOEXEC)."""
    for entry in os.listdir("/proc/self/fd"):
        fd = int(entry)
        if fd > 2 and fd not in keep:
            with contextlib.suppress(OSError):
                os.close(fd)


def _write_log(data: bytes, size: int) -> int:
    """Append to the log on fd 1 as its sole writer; past ``LOG_CAP`` keep one marker line."""
    if size + len(data) > LOG_CAP:
        with contextlib.suppress(OSError):
            os.ftruncate(1, 0)
            os.write(1, _TRUNCATED)
        size = len(_TRUNCATED)
    with contextlib.suppress(OSError):
        os.write(1, data)
    return size + len(data)


def _wait_leader(pid: int, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with contextlib.suppress(OSError):
            if os.getpgid(pid) == pid:
                return True
        time.sleep(0.01)
    return False


def supervise_pty(argv: list[str], handoff: int, ack: int) -> NoReturn:
    """Run ``argv`` under a PTY; keeps reading it, caps the log, forwards TERM/INT."""
    pending: list[int] = []
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        pending.append(signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    gate_read, gate_write = os.pipe()
    child, master = pty.fork()
    if child == 0:
        try:
            os.close(gate_write)
            os.close(handoff)
            os.close(ack)
            _close_other_fds(gate_read)
            go = os.read(gate_read, len(_LAUNCH_GO) + 1)
            os.close(gate_read)
            if go != _LAUNCH_GO:
                os._exit(125)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            _reset_signals()
            os.environ.setdefault("TERM", "xterm-256color")
            os.execvp(argv[0], argv)  # nosec B606 - fixed argv, no shell
        except BaseException as exc:
            os.write(2, f"djinn serve: cannot run {argv[0]}: {exc}\n".encode("utf-8", "replace"))
            os._exit(127)
    os.close(gate_read)
    leader = _wait_leader(child, 2.0)
    with contextlib.suppress(OSError):
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", WINSIZE[0], WINSIZE[1], 0, 0))
    try:
        payload = f"{child} {raw_start(child) or ''}\n".encode()
        os.write(handoff, payload)
        ready = select.select([ack], [], [], timing("deadline"))[0]
        if ready and os.read(ack, len(_LAUNCH_ACK) + 1) == _LAUNCH_ACK:
            os.write(gate_write, _LAUNCH_GO)
    except OSError:
        pending.append(signal.SIGKILL)
    finally:
        os.close(handoff)
        os.close(ack)
        os.close(gate_write)
    size = 0
    exited = False
    eof = False
    while True:
        while pending:
            sig = pending.pop(0)
            if leader and child > 1:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(child, sig)
        ready = [] if eof else select.select([master], [], [], 0.2)[0]
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            if data:
                size = _write_log(data, size)
                continue
            eof = True
        elif eof:
            time.sleep(0.1)
        if not exited:
            exited = os.waitpid(child, os.WNOHANG)[0] != 0
        elif eof or not ready:
            break
    os._exit(0)


def spawn(
    dirfd: int,
    name: str,
    argv: list[str],
    *,
    pty_mode: bool,
    handoff: int | None = None,
    ack: int | None = None,
) -> int:
    """Fork once: the child leads its own session/group, stdio goes to the log, then it execs.

    Returns the child pid (the recorded leader). The helper never waits for the daemon.
    """
    remove_file(dirfd, f"{name}.log")
    logfd = os.open(
        f"{name}.log",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_APPEND | os.O_CLOEXEC,
        0o600,
        dir_fd=dirfd,
    )
    pid = os.fork()
    if pid:
        os.close(logfd)
        return pid
    try:
        os.setsid()
        if os.getpgid(0) != os.getpid():
            os._exit(126)
        devnull = os.open("/dev/null", os.O_RDONLY)
        os.dup2(devnull, 0)
        os.dup2(logfd, 1)
        os.dup2(logfd, 2)
        _close_other_fds(handoff, ack)
        if pty_mode:
            if handoff is None or ack is None:
                os._exit(126)
            supervise_pty(argv, handoff, ack)
        _reset_signals()
        os.execvp(argv[0], argv)  # nosec B606 - fixed argv, no shell
    except BaseException as exc:
        with contextlib.suppress(OSError):
            os.write(2, f"djinn serve: cannot run {argv[0]}: {exc}\n".encode("utf-8", "replace"))
    os._exit(127)


# ------------------------------------------------------------------------ stop path


def stop_groups(groups: list[tuple[int, str]], deadline: Deadline) -> bool:
    """TERM every live recorded group, wait, KILL what remains; True when all are gone."""
    live = [g for g in groups if group_state(*g) == "alive"]
    for pgid, _ in live:
        signal_group(pgid, signal.SIGTERM)

    def gone() -> bool:
        return all(group_state(*g) != "alive" for g in live)

    if deadline.wait(timing("stop_wait"), gone):
        return True
    for g in live:
        if group_state(*g) == "alive":
            signal_group(g[0], signal.SIGKILL)
    return deadline.wait(timing("kill_wait"), gone)


def stop_default_service(home: str, deadline: Deadline) -> bool:
    """Stop the registered default service, but only with proven identity.

    Proof: the discovery pid's cmdline is ``opencode serve --service`` and it owns a LISTEN
    socket on the port of the discovery url. The start time is snapshotted and re-checked before
    SIGKILL. Anything unprovable or stale signals nothing. The native ``opencode service stop``
    is never used (it signals the discovery pid without identity checks).
    """
    try:
        found = read_discovery(home)
    except (HelperError, ValueError):
        return False
    if found is None:
        return False
    pid, url = found
    snapshot = proc_start(pid)
    match = _URL_PORT.match(url)
    if pid <= 1 or snapshot is None or match is None or not cmdline_is_opencode_service(pid):
        return False
    if not (listener_inodes(int(match.group(1)), wildcard_v4=False) & socket_inodes(pid)):
        return False
    signal_pid(pid, signal.SIGTERM)

    def gone() -> bool:
        return proc_start(pid) != snapshot

    if not deadline.wait(timing("stop_wait"), gone):
        if proc_start(pid) == snapshot:
            signal_pid(pid, signal.SIGKILL)
        if not deadline.wait(timing("kill_wait"), gone):
            raise HelperError("spawn", "the previous OpenCode service did not stop")
    return True


# ------------------------------------------------------------------------ commands


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args(
    args: list[str], single: tuple[str, ...], multi: tuple[str, ...] = ()
) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    index = 0
    while index < len(args):
        option = args[index]
        if option not in single + multi or index + 1 >= len(args):
            raise HelperError("usage", "unknown or incomplete argument")
        if option in found and option in single:
            raise HelperError("usage", f"{option} given twice")
        found.setdefault(option, []).append(args[index + 1])
        index += 2
    return found


def _status_entry(name: str, record: Record | None, ev: Eval, dirfd: int) -> dict[str, object]:
    entry: dict[str, object] = {
        "state": ev.state,
        "reason": ev.reason,
        "pid": record.pid if record and ev.state in ("running", "degraded") else None,
        "since": record.started_at if record and not ev.stale else None,
        "last_line": None,
    }
    if ev.state == "exited":
        tail = sanitize_tail(f"{name}.log", 1, dir_fd=dirfd)
        entry["last_line"] = tail[-1] if tail else None
    if name == "opencode":
        entry.update(
            listening=ev.listening, discovery_pid_ok=ev.discovery_ok, foreign_listener=ev.foreign
        )
    return entry


def cmd_status(args: list[str]) -> dict[str, object]:
    parse_args(args, ())
    home = opencode_home()
    dirfd = open_state_dir()
    lockfd = acquire_lock(dirfd, None)
    try:
        servers: dict[str, object] = {}
        for name in NAMES:
            record = load_record(dirfd, name)
            ev = evaluate(name, record, home)
            if ev.stale and lockfd is not None:
                remove_file(dirfd, f"{name}.json")
            servers[name] = _status_entry(name, record, ev, dirfd)
        return {"ok": True, "pid1_start": pid1_start(), "busy": lockfd is None, "servers": servers}
    finally:
        if lockfd is not None:
            os.close(lockfd)
        os.close(dirfd)


def cmd_stop(args: list[str]) -> dict[str, object]:
    names = parse_args(args, (), ("--name",)).get("--name", [])
    if not names or any(n not in NAMES for n in names):
        raise HelperError("usage", "stop needs --name claude|opencode")
    deadline = Deadline(timing("deadline"))
    home = opencode_home()
    dirfd = open_state_dir()
    lockfd = acquire_lock(dirfd, deadline)
    results: dict[str, object] = {}
    try:
        for name in dict.fromkeys(names):
            record = load_record(dirfd, name)
            ev = evaluate(name, record, home)
            if record is None or ev.state in ("stopped", "exited"):
                remove_file(dirfd, f"{name}.json")
                results[name] = {"result": "not-running", "pid": None}
            elif stop_groups(record_groups(record), deadline):
                remove_file(dirfd, f"{name}.json")
                results[name] = {"result": "stopped", "pid": record.pid}
            else:
                results[name] = {"result": "survived", "pid": record.pid}
        return {"ok": True, "results": results}
    finally:
        if lockfd is not None:
            os.close(lockfd)
        os.close(dirfd)


def _start_result(
    result: str, record: Record | None, tail: list[str], **extra: object
) -> dict[str, object]:
    return {
        "ok": True,
        "name": extra.pop("name"),
        "result": result,
        "pid": record.pid if record and result != "exited" else None,
        "since": record.started_at if record else None,
        "log_tail": tail,
        **extra,
    }


def _prepare(dirfd: int, name: str, home: str, deadline: Deadline) -> tuple[Record | None, bool]:
    """Returns ``(running record, restarted)``; degraded remains are stopped first."""
    record = load_record(dirfd, name)
    ev = evaluate(name, record, home)
    if record is not None and ev.state == "running":
        return record, False
    if record is not None and ev.state == "degraded":
        if not stop_groups(record_groups(record), deadline):
            raise HelperError("spawn", f"the degraded {name} server could not be stopped")
        remove_file(dirfd, f"{name}.json")
        return None, True
    remove_file(dirfd, f"{name}.json")
    return None, False


def _fresh_record(
    pid: int,
    handoff: tuple[int, str] | None,
    tag: str,
    port: int | None,
    cors: str | None = None,
) -> Record:
    start = raw_start(pid)
    if start is None or not start.isdigit():
        raise HelperError("spawn", "the started process vanished")
    return Record(
        pid=pid,
        start=start,
        pid1_start=pid1_start(),
        cwd=os.getcwd(),
        argv_tag=tag,
        started_at=now_iso(),
        child_pid=handoff[0] if handoff else None,
        child_start=handoff[1] if handoff else None,
        port=port,
        cors=cors,
    )


def _publish_started_record(
    dirfd: int,
    name: str,
    pid: int,
    handoff: tuple[int, str] | None,
    tag: str,
    port: int | None,
    *,
    ack: int | None = None,
    cors: str | None = None,
) -> Record:
    try:
        record = _fresh_record(pid, handoff, tag, port, cors)
        save_record(dirfd, name, record)
        return record
    except Exception as exc:
        _fail_launch(
            pid,
            handoff,
            "state-dir",
            f"could not build or publish the {name} process record",
            exc,
            ack=ack,
        )


def _kill_failed_launch(pid: int, handoff: tuple[int, str] | None, end: float) -> None:
    """KILL the unreaped direct child and its possible late-created group, then reap it."""
    if pid <= 1 or pid in (os.getpid(), os.getpgrp()):
        raise HelperError("state-dir", "refusing to signal a protected launch process")
    # Unlike signal_pid, this permits our child before setsid(), in our group. The
    # unreaped child is the identity anchor; only the PID receives this first signal.
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    # The unreaped zombie still reserves the PID as a group ID; reaping first would free it.
    with contextlib.suppress(OSError, HelperError):
        signal_group(pid, signal.SIGKILL)
    while True:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                break
        except ChildProcessError:
            break
        left = end - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(0.01, left))
    if handoff is not None and group_state(*handoff) == "alive":
        with contextlib.suppress(OSError, HelperError):
            signal_group(handoff[0], signal.SIGKILL)


def _fail_launch(
    pid: int,
    handoff: tuple[int, str] | None,
    code: str,
    message: str,
    cause: Exception,
    *,
    ack: int | None = None,
) -> NoReturn:
    """One guard for all post-spawn failures before publication, independent of Deadline.

    The 50 s helper deadline plus this 3 s reserve stays below the host's 60 s timeout.
    A never-published launch gets no TERM grace. Warn only for proven surviving members.
    """
    end = time.monotonic() + timing("launch_cleanup")
    if ack is not None:
        with contextlib.suppress(OSError):
            os.close(ack)
    groups = [(pid, raw_start(pid) or "0")]
    if handoff is not None:
        groups.append(handoff)
    with contextlib.suppress(Exception):
        _kill_failed_launch(pid, handoff, end)
    while any(group_state(*group) == "alive" for group in groups):
        left = end - time.monotonic()
        if left <= 0:
            message += "; cleanup could not stop the new process groups; the process may survive"
            break
        time.sleep(min(0.01, left))
    raise HelperError(code, message) from cause


def _read_handoff(readfd: int, seconds: float) -> tuple[int, str] | None:
    end = time.monotonic() + seconds
    data = b""
    while b"\n" not in data:
        left = end - time.monotonic()
        if left <= 0 or not select.select([readfd], [], [], left)[0]:
            return None
        chunk = os.read(readfd, 256)
        if not chunk:
            break
        data += chunk
    parts = data.decode("ascii", "replace").split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[0]) > 1:
        return int(parts[0]), parts[1]
    return None


def cmd_claude_start(args: list[str]) -> dict[str, object]:
    prefix = parse_args(args, ("--prefix",)).get("--prefix", [""])[0]
    if not _PREFIX.fullmatch(prefix):
        raise HelperError("usage", "claude-start needs --prefix <slug> of [a-z0-9-]")
    deadline = Deadline(timing("deadline"))
    home = opencode_home()
    dirfd = open_state_dir()
    lockfd = acquire_lock(dirfd, deadline)
    ack_read = ack_write = -1
    try:
        running, restarted = _prepare(dirfd, "claude", home, deadline)
        if running is not None:
            return _start_result("already-running", running, [], name="claude")
        argv = ["claude", "remote-control", "--remote-control-session-name-prefix", prefix]
        readfd, writefd = os.pipe()
        ack_read, ack_write = os.pipe()
        pid = None
        handoff = None
        try:
            try:
                pid = spawn(dirfd, "claude", argv, pty_mode=True, handoff=writefd, ack=ack_read)
                os.close(ack_read)
                ack_read = -1
                os.close(writefd)
                writefd = -1
                handoff = _read_handoff(readfd, timing("handoff_wait"))
            finally:
                os.close(readfd)
                if writefd >= 0:
                    os.close(writefd)
            if handoff is None or not _wait_leader(pid, 1.0):
                raise HelperError("spawn", "the Claude supervisor did not report its child")
        except Exception as exc:
            if pid is None:
                raise
            ack, ack_write = ack_write, -1
            _fail_launch(
                pid,
                handoff,
                "spawn",
                "the Claude supervisor did not report its child",
                exc,
                ack=ack,
            )
        try:
            record = _publish_started_record(
                dirfd, "claude", pid, handoff, "claude-remote-control", None, ack=ack_write
            )
        except HelperError:
            ack_write = -1  # The publication guard closed it before cleaning up the launch.
            raise
        try:
            os.write(ack_write, _LAUNCH_ACK)
        except OSError as exc:
            ack, ack_write = ack_write, -1
            _fail_launch(
                pid, handoff, "spawn", "could not acknowledge the Claude child", exc, ack=ack
            )
        finally:
            if ack_write >= 0:
                with contextlib.suppress(OSError):
                    os.close(ack_write)
            ack_write = -1
        child = (record.child_pid or 0, record.child_start or "0")

        def died() -> bool:
            return group_state(*child) != "alive" or group_state(pid, record.start) != "alive"

        if deadline.wait(timing("claude_grace"), died):
            deadline.wait(2.0, lambda: group_state(pid, record.start) != "alive")
            return _start_result(
                "exited", record, sanitize_tail("claude.log", dir_fd=dirfd), name="claude"
            )
        return _start_result("restarted" if restarted else "started", record, [], name="claude")
    finally:
        for fd in (ack_read, ack_write):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        if lockfd is not None:
            os.close(lockfd)
        os.close(dirfd)


def _read_password(stdin: IO[str]) -> str:
    raw = stdin.read(PASSWORD_CAP + 1)
    if len(raw) > PASSWORD_CAP:
        raise HelperError("password", "the password input is too large")
    data = None
    with contextlib.suppress(ValueError):
        data = _as_dict(json.loads(raw))
    password = data.get("password") if data else None
    if not isinstance(password, str) or not password:
        raise HelperError("password", 'expected {"password": "<non-empty text>"} on stdin')
    return password


def check_opencode_preconditions(home: str) -> None:
    """The same read-only refusals for preflight and immediately before a start."""
    check_env_root(home)
    check_settings_paths(home)
    check_service_config(read_service_config(home))


def cmd_opencode_preflight(args: list[str]) -> dict[str, object]:
    parse_args(args, ())
    check_opencode_preconditions(opencode_home())
    return {"ok": True}


def cmd_opencode_start(args: list[str], stdin: IO[str]) -> dict[str, object]:
    options = parse_args(args, ("--port", "--cors"))
    port_text = options.get("--port", [""])[0]
    cors = options.get("--cors", [None])[0]
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise HelperError("usage", "opencode-start needs --port <1-65535>")
    if cors is not None and not _CORS.fullmatch(cors):
        raise HelperError("usage", "--cors must be an http(s) origin")
    port = int(port_text)
    home = opencode_home()
    # Nothing is changed before these refusals.
    check_opencode_preconditions(home)
    password = _read_password(stdin)
    deadline = Deadline(timing("deadline"))
    dirfd = open_state_dir()
    lockfd = acquire_lock(dirfd, deadline)
    try:
        running, restarted = _prepare(dirfd, "opencode", home, deadline)
        if running is not None:
            if stored_password_equals(home, password) and running.cors == cors:
                return _start_result(
                    "already-running", running, [], name="opencode", stopped_previous=False
                )
            if not stop_groups(record_groups(running), deadline):
                raise HelperError("spawn", "the running OpenCode server could not be stopped")
            remove_file(dirfd, "opencode.json")
            restarted = True
        merge_password(home, password)
        stopped_default = stop_default_service(home, deadline)
        argv = ["opencode", "serve", "--service", "--hostname", _BIND_ALL, "--port", str(port)]
        if cors is not None:
            argv += ["--cors", cors]
        pid = spawn(dirfd, "opencode", argv, pty_mode=False)
        try:
            if not _wait_leader(pid, 1.0):
                raise HelperError("spawn", "the OpenCode process did not become a group leader")
        except Exception as exc:
            _fail_launch(
                pid, None, "spawn", "the OpenCode process did not become a group leader", exc
            )
        record = _publish_started_record(
            dirfd, "opencode", pid, None, "opencode-serve", port, cors=cors
        )

        def settled() -> bool:
            if group_state(pid, record.start) != "alive":
                return True
            return listener_ours(pid, port) and discovery_pid(home) == pid

        extra = {"name": "opencode", "stopped_previous": restarted or stopped_default}
        if not deadline.wait(timing("opencode_ready"), settled):
            found = discovery_pid(home)
            if not stop_groups([(pid, record.start)], deadline):
                raise HelperError(
                    "spawn",
                    "OpenCode did not become ready and its process group could not be stopped",
                )
            remove_file(dirfd, "opencode.json")
            if found is not None and found != pid:
                raise HelperError("not-registered", "another OpenCode service registered itself")
            raise HelperError("spawn", "OpenCode did not become ready")
        if group_state(pid, record.start) != "alive":
            return _start_result(
                "exited", record, sanitize_tail("opencode.log", dir_fd=dirfd), **extra
            )
        return _start_result("restarted" if restarted else "started", record, [], **extra)
    finally:
        if lockfd is not None:
            os.close(lockfd)
        os.close(dirfd)


def dispatch(argv: list[str], stdin: IO[str]) -> dict[str, object]:
    if not argv:
        raise HelperError("usage", "missing subcommand")
    command, args = argv[0], argv[1:]
    if command == "status":
        return cmd_status(args)
    if command == "stop":
        return cmd_stop(args)
    if command == "claude-start":
        return cmd_claude_start(args)
    if command == "opencode-start":
        return cmd_opencode_start(args, stdin)
    if command == "opencode-preflight":
        return cmd_opencode_preflight(args)
    raise HelperError("usage", "unknown subcommand")


def main(argv: list[str], *, stdin: IO[str], stdout: IO[str]) -> int:
    """Run one subcommand; exactly one JSON object plus a newline goes to ``stdout``."""
    try:
        out = dispatch(argv, stdin)
        code = 0
    except HelperError as failure:
        out, code = {"ok": False, "code": failure.code, "message": failure.message}, 1
    except DeadlineError:
        out = {
            "ok": False,
            "code": "timeout",
            "message": "the helper ran out of time; the server state is unknown",
        }
        code = 1
    stdout.write(json.dumps(out, separators=(",", ":")) + "\n")
    stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout))
