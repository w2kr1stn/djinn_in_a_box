"""Root bootstrap followed by an unprivileged, bounded desktop daemon launcher."""

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

POLICY = Path("/etc/djinn/policy.json")
DROP = ["setpriv", "--reuid=1000", "--regid=1000", "--clear-groups",
        "--bounding-set=-all", "--no-new-privs"]


def initialize(channel):
    # Root only hands the private directories to UID 1000: without CAP_FOWNER and
    # CAP_DAC_OVERRIDE it can no longer chmod or write them afterwards. /runtime comes
    # last because Docker recreates the tmpfs as root-owned 0755 on restart.
    for directory in ("/out", "/runtime/home", "/runtime/user", "/runtime"):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, 1000, 1000)
    os.environ.update(HOME="/runtime/home", XDG_RUNTIME_DIR="/runtime/user",
                      PULSE_CLIENTCONFIG="/etc/djinn/client.conf",
                      PULSE_CONFIG="/etc/djinn/daemon.conf")
    os.execvp(DROP[0], [*DROP, "python3", "-I", "/etc/djinn/helper.py", channel, "daemon"])


def prepare(channel):
    os.umask(0o022)
    for directory, mode in (("/out", 0o755), ("/runtime/home", 0o700), ("/runtime/user", 0o700)):
        Path(directory).chmod(mode)
    # Restart replaces the listener, while the relay credential survives.
    Path("/out/bus" if channel == "dbus" else "/out/native").unlink(missing_ok=True)
    if channel == "audio":
        cookie = Path("/out/cookie")
        if not cookie.exists():
            fd = os.open(cookie, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as stream:
                stream.write(os.urandom(256))
        info = cookie.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != 256 or info.st_uid != 1000:
            raise RuntimeError("invalid relay cookie")
        cookie.chmod(0o644)
        relay = Path("/etc/djinn/relay.template").read_text()
        if Path("/upstream-cookie").is_file():
            if len(Path("/upstream-cookie").read_bytes()) != 256:
                raise RuntimeError("invalid upstream cookie")
            relay = "\n".join(
                line + " cookie=/upstream-cookie" if "module-tunnel-" in line else line
                for line in relay.splitlines()
            ) + "\n"
        Path("/runtime/relay.pa").write_text(relay)


def client(*args):
    env = {**os.environ, "PULSE_SERVER": "unix:/out/native", "PULSE_COOKIE": "/out/cookie"}
    return subprocess.run(["pactl", *args], env=env, capture_output=True,
                          text=True, timeout=2, check=True).stdout


def launch(channel):
    argv = json.loads(POLICY.read_text())[channel]
    child = subprocess.Popen(argv, cwd="/")
    stopping = False

    def stop(signum, _frame):
        nonlocal stopping
        stopping = True
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        deadline = time.monotonic() + 12
        while child.poll() is None and not stopping:
            listener = Path("/out/bus" if channel == "dbus" else "/out/native")
            if listener.is_socket():
                listener.chmod(0o666)
                if channel == "dbus":
                    break
                try:
                    sinks = json.loads(client("--format=json", "list", "sinks"))
                    sources = json.loads(client("--format=json", "list", "sources"))
                    if (any(s["name"] == "djinn_host_sink" for s in sinks)
                            and any(s["name"] == "djinn_host_source" for s in sources)):
                        client("set-default-sink", "djinn_host_sink")
                        client("set-default-source", "djinn_host_source")
                        break
                except (subprocess.SubprocessError, ValueError):
                    pass  # Tunnel objects appear asynchronously; the deadline remains fixed.
            if time.monotonic() >= deadline:
                raise RuntimeError("desktop listener/tunnels did not become ready")
            time.sleep(0.1)
        while child.poll() is None and not stopping:
            time.sleep(0.1)
        if stopping:
            return 0
        return child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)


if __name__ == "__main__":
    if len(sys.argv) == 2:
        initialize(sys.argv[1])
    prepare(sys.argv[1])
    sys.exit(launch(sys.argv[1]))
