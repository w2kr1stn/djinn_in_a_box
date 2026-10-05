"""Read-only evidence and ordinary-client probes; never loads a module."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

FLOOR = "0.1.6-1+deb13u3"
DROP = ["setpriv", "--reuid=1000", "--regid=1000", "--clear-groups",
        "--bounding-set=-all", "--no-new-privs"]


def run(argv, env=None):
    return subprocess.run(argv, capture_output=True, text=True, timeout=2,
                          env=env, check=True).stdout.strip()


def evidence(channel):
    if os.getuid() != 1000 or os.getgid() != 1000 or os.getgroups():
        raise RuntimeError("health clients must run as UID/GID 1000 without supplementary groups")
    policy = json.loads(Path("/etc/djinn/policy.json").read_text())
    argv = policy[channel]
    found = False
    for proc in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = proc.read_bytes().rstrip(b"\0").decode().split("\0")
            if args == argv:
                uid = proc.parent.stat().st_uid
                found = uid == 1000
        except (OSError, UnicodeError):
            continue
    if not found:
        raise RuntimeError("daemon argv/identity does not match image policy")
    out = Path("/out").stat()
    socket = Path("/out/bus" if channel == "dbus" else "/out/native").stat()
    if (out.st_uid != 1000 or out.st_gid != 1000 or stat.S_IMODE(out.st_mode) != 0o755
            or not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != 1000
            or stat.S_IMODE(socket.st_mode) != 0o666):
        raise RuntimeError("invalid desktop output ownership/modes")
    result = {"policy": policy, "health_ok": True}
    if channel == "dbus":
        version = run(["dpkg-query", "-W", "-f=${Version}", "xdg-dbus-proxy"])
        run(["dpkg", "--compare-versions", version, "ge", FLOOR])
        run(["dbus-send", "--bus=unix:path=/out/bus", "--type=method_call",
             "--print-reply", "--reply-timeout=1500", "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus", "org.freedesktop.DBus.GetId"])
        result.update(version=version, floor=FLOOR, floor_ok=True)
    else:
        cookie = Path("/out/cookie").lstat()
        if (not stat.S_ISREG(cookie.st_mode) or cookie.st_size != 256 or cookie.st_uid != 1000
                or cookie.st_gid != 1000 or stat.S_IMODE(cookie.st_mode) != 0o644):
            raise RuntimeError("invalid relay credential")
        env = {**os.environ, "PULSE_SERVER": "unix:/out/native", "PULSE_COOKIE": "/out/cookie",
               "PULSE_CLIENTCONFIG": "/etc/djinn/client.conf"}
        def pactl(*args):
            return run(["pactl", *args], env)
        pactl("info")
        modules = json.loads(pactl("--format=json", "list", "modules"))
        template = Path("/etc/djinn/relay.template").read_text()
        expected = [line.removeprefix("load-module ").split(" ", 1)
                    for line in Path("/runtime/relay.pa").read_text().splitlines()]
        if sorted((m["name"], m["argument"]) for m in modules) != sorted(map(tuple, expected)):
            raise RuntimeError("unexpected PulseAudio module set/arguments")
        if (pactl("get-default-sink") != "djinn_host_sink"
                or pactl("get-default-source") != "djinn_host_source"):
            raise RuntimeError("relay defaults are not the host tunnels")
        for kind, name in (("sinks", "djinn_host_sink"), ("sources", "djinn_host_source")):
            if not any(o["name"] == name for o in json.loads(pactl("--format=json", "list", kind))):
                raise RuntimeError("missing host tunnel")
        upstream_env = {**env, "PULSE_SERVER": "unix:/upstream/pulse/native"}
        upstream_env.pop("PULSE_COOKIE", None)
        if Path("/upstream-cookie").is_file():
            upstream_env["PULSE_COOKIE"] = "/upstream-cookie"
        run(["pactl", "info"], upstream_env)
        result.update(relay=template, daemon=Path("/etc/djinn/daemon.conf").read_text(),
                      client=Path("/etc/djinn/client.conf").read_text())
    return result


if __name__ == "__main__":
    if os.getuid() == 0:
        os.environ.update(HOME="/runtime/home", XDG_RUNTIME_DIR="/runtime/user")
        os.execvp(DROP[0], [*DROP, "python3", "-I", "/etc/djinn/health.py", *sys.argv[1:]])
    print(json.dumps(evidence(sys.argv[1])))
