"""Run substituted POSIX scripts only; never dispatch a host package tool."""

from __future__ import annotations

import fcntl
import json
import os
import re
import resource
import shlex
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = "/usr/local/libexec/djinn-apt-root"
LIST = "/var/lib/djinn-packages/packages.txt"
FIXED_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "DEBIAN_FRONTEND": "noninteractive",
    "LANG": "C.UTF-8",
    "HOME": "/root",
}
INSTALL = [
    "install",
    "-y",
    "--no-install-recommends",
    "--no-remove",
    "-o",
    "Dpkg::Options::=--force-confdef",
    "-o",
    "Dpkg::Options::=--force-confold",
    "--",
]
BAD_NAMES = [
    "jq*",
    "jq?",
    "jq[ab]",
    "Jq",
    "JQ",
    "jQ",
    "jq=1.6-2.1",
    "jq/bookworm",
    "jq:amd64",
    "/tmp/a.deb",
    "./a.deb",
    "../foo",
    "foo.deb",
    "a",
    "",
    "jq curl",
    " jq",
    "jq\t",
    "jq\nxx",
    "jq\rxx",
    "jq\x1bxx",
    ".jq",
    "+jq",
    "éé",
    "-jq",
]
BAD_OPERATIONS = [
    "remove",
    "purge",
    "autoremove",
    "upgrade",
    "dist-upgrade",
    "download",
    "clean",
    "autoclean",
    "check",
    "source",
    "indextargets",
    "invented",
]
BAD_FLAGS = [
    "-o",
    "--option",
    "-c",
    "--config-file",
    "-t",
    "--target-release",
    "-o=foo",
    "--option=foo",
    "-c=foo",
    "--config-file=foo",
    "-t=foo",
    "--target-release=foo",
    "--",
    "--version",
    "-s",
    "--allow-unauthenticated",
    "--allow-remove-essential",
    "-qqq",
]


def substitute(source: str, name: str, old: str, new: str | Path) -> str:
    assignment = f"{name}='{old}'"
    assert source.count(assignment) == 1, assignment
    return source.replace(assignment, f"{name}={shlex.quote(str(new))}")


def executable(path: Path, source: str) -> Path:
    path.write_text(source)
    path.chmod(0o755)
    return path


def assert_helper_dispatch(source: str) -> None:
    """Fail BEFORE running copies if any privileged dispatch escapes substitution."""
    for name, binary in {
        "id_bin": "id",
        "env_bin": "env",
        "apt_bin": "apt-get",
        "cache_bin": "apt-cache",
        "flock_bin": "flock",
        "grep_bin": "grep",
        "dpkg_bin": "dpkg-query",
    }.items():
        assert source.count(f"{name}='/usr/bin/{binary}'") == 1
    assert source.count('run_clean "$cache_bin" pkgnames -- "$name"') == 2
    # Tokenize quoted messages and command separators; never exempt an entire
    # printf line that could contain a second, unsafe dispatch.
    tokens = shlex.shlex(source, posix=True, punctuation_chars=True)
    tokens.whitespace_split = True
    for token in tokens:
        if token in (
            "apt_bin=/usr/bin/apt-get",
            "cache_bin=/usr/bin/apt-cache",
            "dpkg_bin=/usr/bin/dpkg-query",
        ):
            continue
        assert Path(token).name not in ("apt-get", "apt-cache", "dpkg-query"), token


FAKE_BODY = r"""
import json, os, sys, time
from pathlib import Path
config = json.loads(Path(config_path).read_text())
args = sys.argv[1:]
def event(kind, **extra):
    with open(log_path, "a") as handle:
        handle.write(json.dumps(dict(kind=kind, argv=args, pid=os.getpid(), **extra)) + "\n")
if role == "sudo":
    with open(nul_path, "ab") as handle:
        handle.write(b"\0".join(os.fsencode(arg) for arg in args) + b"\0\0")
    announcement = (
        Path(config["announcement_log"]).read_text() if config.get("announcement_log") else ""
    )
    event(role, announcement=announcement)
    if config.get("sudo_marker"):
        print("FAKE_SUDO", file=sys.stderr)
    if config.get("sudo_status"):
        print("sudo: a password is required", file=sys.stderr)
        sys.exit(config["sudo_status"])
    if config.get("dispatch"):
        os.execv("/bin/sh", ["/bin/sh", config["dispatch"], *args[5:]])
    role = "helper"
if role == "id":
    print(config.get("uid", "0"))
    sys.exit(0)
if role in ("apt", "cache", "dpkg"):
    current_umask = os.umask(0)
    os.umask(current_umask)
    event(role, env=dict(os.environ), stdin=sys.stdin.read(), umask=f"{current_umask:04o}")
else:
    event(role)
if role == "cache":
    time.sleep(config.get("cache_delay", 0))
    output = config.get("cache", {}).get(args[-1], args[-1] + "\n")
    sys.stdout.write(output)
    sys.exit(config.get("cache_status", 0))
if role == "dpkg":
    time.sleep(config.get("dpkg_delay", 0))
    state, status = config.get("states", {}).get(args[-1], ["", 1])
    sys.stdout.write(state)
    if status:
        print("dpkg-query: no packages found matching " + args[-1], file=sys.stderr)
    sys.exit(status)
if role == "apt" and config.get("barrier"):
    deadline = time.monotonic() + 5
    while not Path(config["barrier"]).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    event("apt_end")
stage = "install" if "install" in args else "update"
time.sleep(config.get(stage + "_delay", 0))
sys.stdout.write(config.get(stage + "_stdout", config.get("stdout", "")))
sys.stderr.write(config.get(stage + "_stderr", config.get("stderr", "")))
sys.exit(config.get(stage + "_status", config.get("status", 0)))
"""


class FakeTools:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(exist_ok=True)
        self.config = directory / "config.json"
        self.log = directory / "events.jsonl"
        self.nul = directory / "sudo.argv"
        self.configure()
        for role in ("id", "apt", "cache", "dpkg", "sudo"):
            executable(
                directory / role,
                "#!/usr/bin/python3\n"
                f"role={role!r}\nconfig_path={str(self.config)!r}\n"
                f"log_path={str(self.log)!r}\nnul_path={str(self.nul)!r}\n" + FAKE_BODY,
            )

    def configure(self, **kwargs):
        current = json.loads(self.config.read_text()) if self.config.exists() else {}
        self.config.write_text(json.dumps(current | kwargs))

    def events(self, kind=None):
        events = (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )
        return [event for event in events if kind is None or event["kind"] == kind]

    def sudo_calls(self):
        if not self.nul.exists():
            return []
        return [
            [os.fsdecode(arg) for arg in call.split(b"\0")]
            for call in self.nul.read_bytes().split(b"\0\0")[:-1]
        ]


class HelperHarness:
    def __init__(self, directory: Path):
        self.tools = FakeTools(directory)
        self.lock = directory / "root.lock"
        self.list_dir = directory / "packages"
        self.list_dir.mkdir(mode=0o755)
        self.list = self.list_dir / "packages.txt"
        source = (ROOT / "scripts/djinn-apt-root.sh").read_text()
        assert_helper_dispatch(source)
        for name, old, new in (
            ("dpkg_bin", "/usr/bin/dpkg-query", directory / "dpkg"),
            ("list_dir", "/var/lib/djinn-packages", self.list_dir),
            ("list_path", LIST, self.list),
            ("root_uid", "0", str(os.getuid())),
            ("work_template", "/run/djinn-apt-root.XXXXXX", directory / "work.XXXXXX"),
            ("apt_bin", "/usr/bin/apt-get", directory / "apt"),
            ("cache_bin", "/usr/bin/apt-cache", directory / "cache"),
            ("env_bin", "/usr/bin/env", "/usr/bin/env"),
            ("flock_bin", "/usr/bin/flock", "/usr/bin/flock"),
            ("grep_bin", "/usr/bin/grep", "/usr/bin/grep"),
            ("lock_path", "/run/djinn-apt-root.lock", self.lock),
            ("lock_wait", "300", "0.3"),
        ):
            source = substitute(source, name, old, new)
        # Run unprivileged copies with real id; only their expected UID changes.
        source = source.replace(
            '[ "$(run_clean "$id_bin" -u)" = 0 ]',
            f'[ "$(run_clean "$id_bin" -u)" = {os.getuid()} ]',
        )
        assert all(
            binary not in source
            for binary in ("/usr/bin/apt-get", "/usr/bin/apt-cache", "/usr/bin/dpkg-query")
        )
        self.script = executable(directory / "helper.sh", source)

    def tool(self, name, source):
        path = executable(self.tools.directory / (name + "-substitute"), source)
        self.script.write_text(
            substitute(self.script.read_text(), name + "_bin", "/usr/bin/" + name, path)
        )
        return path

    def run(self, args=None, **kwargs):
        return subprocess.run(
            ["/bin/sh", str(self.script), *(args if args is not None else ["replay"])],
            capture_output=True,
            text=True,
            input="caller input",
            timeout=8,
            check=False,
            **kwargs,
        )


def children_cpu_seconds():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def check_clock(helper, ticks):
    count = helper.script.parent / "clock-count"
    helper.tool(
        "date",
        "#!/usr/bin/python3\nfrom pathlib import Path\n"
        f"p = Path({str(count)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        f"print({ticks!r}[n])\n"
        "p.write_text(str(n + 1))\n",
    )


def test_helper_fixed_binary_dispatch():
    assert_helper_dispatch((ROOT / "scripts/djinn-apt-root.sh").read_text())


def test_helper_requires_root(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.script.write_text(
        helper.script.read_text().replace(
            f'[ "$(run_clean "$id_bin" -u)" = {os.getuid()} ]',
            f'[ "$(run_clean "$id_bin" -u)" = {os.getuid() + 1} ]',
        )
    )
    result = helper.run(["install", "jq"])
    assert result.returncode == 2 and "root" in result.stderr
    assert len(result.stderr.splitlines()) == 1
    assert not helper.lock.exists() and helper.tools.events() == []


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["install"],
        ["update", "jq"],
        ["replay", "jq"],
        ["-q", "replay"],
        ["-qq", "replay"],
        ["-q", "-qq", "update"],
        ["-qqq", "update"],
        ["-qqq", "install", "jq"],
        ["update", "-q"],
        ["--quiet", "update"],
        ["-y", "install", "jq"],
        ["--help"],
        *[[op] for op in BAD_OPERATIONS],
        *[["install", name] for name in BAD_NAMES],
        *[["install", "jq", flag] for flag in BAD_FLAGS],
    ],
)
def test_helper_revalidates_before_side_effects(tmp_path, args):
    helper = HelperHarness(tmp_path)
    result = helper.run(args)
    assert result.returncode == 2
    assert result.stdout == "" and len(result.stderr.splitlines()) == 1
    assert (
        "apt-get --help" in result.stderr and 'README section "Runtime Packages"' in result.stderr
    )
    assert not any(ord(char) < 32 for char in result.stderr.rstrip("\n"))
    assert not helper.lock.exists() and helper.tools.events() == []


@pytest.mark.parametrize(
    "name,output",
    [
        ("jq", "jqp\njquery-alternative-doc\n"),
        ("jqx", ""),
        ("awk", ""),
        ("jq.", "jqa\njq.extra\n"),
        ("xz.", "xz.utils\n"),
        ("bash-", "bash-doc\n"),
        ("jq+", "jq+extra\n"),
        ("python3.11", "python3.11\n"),
        ("g++", "g++\n"),
    ],
)
def test_helper_exact_name_all_before_apt(tmp_path, name, output):
    helper = HelperHarness(tmp_path)
    helper.tools.configure(cache={name: output})
    result = helper.run(["install", "curl", name])
    known = name in ("python3.11", "g++")
    assert result.returncode == (0 if known else 100)
    assert [event["argv"] for event in helper.tools.events("cache")] == [
        ["pkgnames", "--", "curl"],
        ["pkgnames", "--", name],
    ]
    if known:
        assert helper.tools.events("apt")[0]["argv"] == [*INSTALL, "curl", name]
    else:
        assert f"E: Unable to locate package {name}" in result.stderr
        assert "apt-get update" in result.stderr
        assert not helper.tools.events("apt")


@pytest.mark.parametrize("quiet", [[], ["-q"], ["-qq"]])
@pytest.mark.parametrize("operation", ["update", "install"])
@pytest.mark.parametrize("status", [0, 37, 100])
def test_helper_fixed_env_options_and_stdin(tmp_path, quiet, operation, status):
    helper = HelperHarness(tmp_path)
    helper.tools.configure(status=status)
    env = dict(
        os.environ,
        APT_CONFIG="hostile",
        HOME="hostile",
        PATH="/nonexistent",
        LANGUAGE="xx",
        LC_ALL="C",
        http_proxy="hostile",
        LD_LIBRARY_PATH="/nonexistent",
    )
    args = [*quiet, operation, *(["jq"] if operation == "install" else [])]
    result = helper.run(args, env=env)
    assert result.returncode == status
    assert result.stdout == result.stderr == ""
    events = helper.tools.events()
    assert events[-1]["argv"] == [
        *quiet,
        *(INSTALL + ["jq"] if operation == "install" else ["update"]),
    ]
    for event in events:
        assert event["env"] == FIXED_ENV and event["stdin"] == ""
    assert [event["kind"] for event in events] == (
        ["cache", "apt"] if operation == "install" else ["apt"]
    )


@pytest.mark.parametrize("operation", ["install", "update", "replay"])
def test_helper_apt_umask(tmp_path, operation):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    result = helper.run([operation, *(["jq"] if operation == "install" else [])])
    assert result.returncode == 0
    events = helper.tools.events("apt")
    assert len(events) == (2 if operation == "replay" else 1)
    assert all(event["umask"] == "0022" for event in events)
    assert helper.lock.stat().st_mode & 0o777 == 0o600


def test_helper_cache_failure(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.tools.configure(cache_status=42)
    result = helper.run(["install", "jq"])
    assert result.returncode == 100 and "apt-cache failed" in result.stderr
    assert not helper.tools.events("apt")


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, "fake process did not reach barrier"
        time.sleep(0.01)


def test_helper_lock_serializes_and_times_out(tmp_path):
    helper = HelperHarness(tmp_path)
    barrier = tmp_path / "release"
    helper.tools.configure(barrier=str(barrier), status=37)
    command = ["/bin/sh", str(helper.script), "install", "jq"]
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = None
    try:
        wait_for(lambda: helper.tools.events("apt"))
        assert helper.lock.stat().st_mode & 0o777 == 0o600
        inode = helper.lock.stat().st_ino
        second = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        time.sleep(0.15)
        assert len(helper.tools.events("cache")) == 1
        assert second.poll() is None
    finally:
        barrier.touch()
        first.communicate(timeout=3)
        if second is not None:
            second.communicate(timeout=3)
    assert first.returncode == second.returncode == 37
    assert [event["kind"] for event in helper.tools.events()] == [
        "cache",
        "apt",
        "apt_end",
        "cache",
        "apt",
        "apt_end",
    ]
    assert helper.lock.stat().st_ino == inode
    before = len(helper.tools.events())
    with helper.lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = helper.run(["update"])
    assert result.returncode == 100 and "lock" in result.stderr and "0.3" in result.stderr
    assert len(helper.tools.events()) == before
    helper.tools.configure(status=0)
    assert helper.run(["update"]).returncode == 0


def test_helper_lock_open_failure(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.lock.mkdir()
    result = helper.run(["update"])
    assert result.returncode == 100 and str(helper.lock) in result.stderr
    assert helper.tools.events() == []


@pytest.mark.parametrize(
    "args", [["A" * 32769], ["install", "A" * 32769], ["install", "\x01" * 65]]
)
def test_helper_long_token_refused_promptly(tmp_path, args):
    # Reachable directly through sudo: a slow refusal would pin a root shell.
    helper = HelperHarness(tmp_path)
    started = children_cpu_seconds()
    result = helper.run(args)
    assert children_cpu_seconds() - started < 2
    assert result.returncode == 2 and len(result.stderr.splitlines()) == 1
    assert f"({len(args[-1])}-byte argument)" in result.stderr
    assert not helper.lock.exists() and helper.tools.events() == []


@pytest.mark.parametrize("length", [128, 129])
def test_helper_name_byte_limit(tmp_path, length):
    helper = HelperHarness(tmp_path)
    name = "a" * length
    result = helper.run(["install", name])
    if length == 128:
        assert result.returncode == 0
        assert helper.tools.events("cache")[0]["argv"] == ["pkgnames", "--", name]
        assert helper.tools.events("apt")[0]["argv"] == [*INSTALL, name]
    else:
        assert result.returncode == 2 and len(result.stderr.splitlines()) == 1
        assert "package name longer than 128 bytes (129-byte argument)" in result.stderr
        assert not helper.lock.exists() and helper.tools.events() == []


@pytest.mark.parametrize(
    "initial",
    [
        None,
        b"",
        b"# retained\n\n  jq # inline\n\tcurl\t\n",
        b"# retained\ncurl",
        b"jqp\n",
        b"j\x00q\n",
    ],
)
def test_record_header_dedupe_verbatim_and_mode(tmp_path, initial):
    helper = HelperHarness(tmp_path)
    if initial is not None:
        helper.list.write_bytes(initial)
    result = helper.run(["install", "jq", "jq", "g++"])
    assert result.returncode == 0 and result.stderr == ""
    first = helper.list.read_bytes()
    prefix = (
        initial
        if initial is not None
        else b"# Djinn runtime packages: one name per line; "
        b"remove an entry with sudo (see README).\n"
    )
    assert first.startswith(prefix)
    expected = prefix + (b"\n" if prefix and not prefix.endswith(b"\n") else b"")
    expected += (b"" if initial and b"  jq # inline" in initial else b"jq\n") + b"g++\n"
    assert first == expected
    assert helper.run(["install", "jq", "python3.11", "g++"]).stderr == ""
    assert helper.list.read_bytes() == first + b"python3.11\n"
    assert helper.list.stat().st_mode & 0o777 == 0o644
    assert not list(helper.list_dir.glob(".packages.txt.*"))
    assert not list(tmp_path.glob("work.*"))


@pytest.mark.parametrize(
    "operation,status",
    [("install", 0), ("install", 37), ("update", 0), ("update", 37), ("replay", 0)],
)
@pytest.mark.parametrize("existing", [False, True])
def test_record_only_after_successful_install(tmp_path, operation, status, existing):
    helper = HelperHarness(tmp_path)
    initial = "# unchanged\ncurl\n"
    if existing:
        helper.list.write_text(initial)
    apt = tmp_path / "apt"
    source = apt.read_text().replace(
        'if role == "apt" and config.get("barrier"):',
        f"assert Path({str(helper.list)!r}).read_text() == {initial!r}\n"
        'if role == "apt" and config.get("barrier"):'
        if existing
        else f"assert not Path({str(helper.list)!r}).exists()\n"
        'if role == "apt" and config.get("barrier"):',
    )
    apt.write_text(source)
    helper.tools.configure(status=status)
    result = helper.run([operation, *(["jq"] if operation == "install" else [])])
    assert result.returncode == status
    if operation == "install" and status == 0:
        assert helper.list.read_text().endswith("jq\n")
    elif existing:
        assert helper.list.read_text() == initial
    else:
        assert not helper.list.exists()


@pytest.mark.parametrize("count", [255, 256, 257])
def test_record_name_cap(tmp_path, count):
    helper = HelperHarness(tmp_path)
    initial = "".join(f"pkg{i}\n" for i in range(count))
    helper.list.write_text(initial)
    result = helper.run(["install", "pkg0", "jq", "jq", "curl"])
    assert result.returncode == 0
    assert helper.list.read_text() == initial + ("jq\n" if count == 255 else "")
    assert f"{1 if count == 255 else 2} names not recorded" in result.stderr
    assert len(result.stderr.splitlines()) == 1


@pytest.mark.parametrize("count", [256, 257])
def test_record_many_new_names(tmp_path, count):
    helper = HelperHarness(tmp_path)
    fast_package_checks(helper)
    names = [f"pkg{i}" for i in range(count)]
    result = helper.run(["install", *names])
    assert result.returncode == 0
    assert helper.list.read_text().splitlines()[1:] == names[:256]
    assert ("1 names not recorded" in result.stderr) == (count == 257)


@pytest.mark.parametrize("spare", [2, 3, 4])
def test_record_byte_cap(tmp_path, spare):
    helper = HelperHarness(tmp_path)
    initial = b"#" + b" " * (1048576 - spare - 2) + b"\n"
    helper.list.write_bytes(initial)
    result = helper.run(["install", "jq"])
    assert result.returncode == 0
    assert helper.list.read_bytes() == initial + (b"jq\n" if spare >= 3 else b"")
    assert ("1 names not recorded" in result.stderr) == (spare < 3)
    assert helper.list.stat().st_size <= 1048576


@pytest.mark.parametrize("tool", ["mktemp", "chmod", "sync", "mv", "head", "tr", "awk"])
def test_record_failure_is_atomic_and_cleans_up(tmp_path, tool):
    helper = HelperHarness(tmp_path)
    initial = b"# keep\ncurl\n"
    helper.list.write_bytes(initial)
    helper.tool(tool, "#!/bin/sh\nprintf 'partial output'\nexit 42\n")
    result = helper.run(["install", "jq"])
    assert result.returncode == 0 and result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert "installed, but not recorded:" in result.stderr
    if tool == "sync":
        assert "cannot sync temporary list" in result.stderr
    if tool == "mv":
        assert "cannot replace list" in result.stderr
    assert helper.list.read_bytes() == initial
    assert not list(helper.list_dir.glob(".packages.txt.*"))
    assert not list(tmp_path.glob("work.*"))


TRUST_FAILURES = [
    "owner-file",
    "owner-directory",
    "group-file",
    "world-file",
    "group-directory",
    "world-directory",
    "symlink-file",
    "symlink-directory",
    "dangling-file",
    "fifo",
    "directory-file",
    "missing-directory",
    "oversize",
    "unreadable",
]


@pytest.mark.parametrize("condition", TRUST_FAILURES)
@pytest.mark.parametrize("operation", ["replay", "install"])
def test_trust_failure_ignores_list(tmp_path, condition, operation):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("curl\n")
    if condition.startswith("owner-"):
        if condition == "owner-directory":
            helper.script.write_text(
                helper.script.read_text().replace(
                    f"root_uid={os.getuid()}\n", f"root_uid={os.getuid() + 1}\n"
                )
            )
            # Only the directory has the wrong owner relative to root_uid.
            helper.tool(
                "stat",
                '#!/bin/sh\ncase "$*" in *self/fd/3*) '
                f'printf "81a4 {os.getuid() + 1} 5\\n" ;; '
                '*) exec /usr/bin/stat "$@" ;; esac\n',
            )
        else:
            # Preserve the real directory metadata, falsify only the descriptor owner.
            helper.tool(
                "stat",
                '#!/bin/sh\ncase "$*" in *self/fd/3*) printf "81a4 999999 5\\n" ;; '
                '*) exec /usr/bin/stat "$@" ;; esac\n',
            )
    elif condition.startswith(("group-", "world-")):
        path = helper.list_dir if condition.endswith("directory") else helper.list
        path.chmod(0o755 | (0o020 if condition.startswith("group") else 0o002))
    elif condition == "symlink-directory":
        moved = tmp_path / "elsewhere"
        helper.list_dir.rename(moved)
        helper.list_dir.symlink_to(moved, target_is_directory=True)
    elif condition in ("symlink-file", "dangling-file"):
        target = tmp_path / "elsewhere"
        helper.list.rename(target)
        helper.list.symlink_to(target)
        if condition == "dangling-file":
            target.unlink()
    elif condition in ("fifo", "directory-file", "missing-directory"):
        helper.list.unlink()
        if condition == "fifo":
            os.mkfifo(helper.list)
        elif condition == "directory-file":
            helper.list.mkdir()
        else:
            helper.list_dir.rmdir()
    elif condition == "oversize":
        helper.list.write_bytes(b"#" * 1048577)
    elif condition == "unreadable":
        helper.list.chmod(0)
    try:
        result = helper.run([operation, *(["jq"] if operation == "install" else [])])
    finally:
        if condition == "fifo":
            # Release a blocked mutated child if the regular-file guard was removed.
            fd = os.open(helper.list, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
        if condition == "unreadable":
            helper.list.chmod(0o644)
    assert result.returncode == 0
    output = result.stdout if operation == "replay" else result.stderr
    if condition == "missing-directory":
        assert "list directory is not a real directory" in output
    if condition == "oversize":
        assert "list is larger than 1 MiB" in output
    if condition in ("directory-file", "fifo", "symlink-file", "dangling-file"):
        assert "list is not a regular non-symlink file" in output
    if operation == "replay":
        assert result.stderr == "" and result.stdout.count("W\t") == 1
        assert "not trusted:" in result.stdout
        assert helper.tools.events() == []
    else:
        assert len(result.stderr.splitlines()) == 1
        assert "installed, but not recorded: not trusted:" in result.stderr
        assert [event["kind"] for event in helper.tools.events()] == ["cache", "apt"]
    assert not list(helper.list_dir.glob(".packages.txt.*"))


@pytest.mark.parametrize("original_trusted", [True, False])
def test_descriptor_stat_checks_opened_file_during_swap(tmp_path, original_trusted):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    helper.list.chmod(0o644 if original_trusted else 0o666)
    helper.tools.configure(states={"jq": ["installed", 0]})
    helper.tool(
        "stat",
        "#!/usr/bin/python3\nimport os, sys\nfrom pathlib import Path\n"
        f"p = Path({str(helper.list)!r})\n"
        "if sys.argv[2] == '%f %u %s':\n"
        "    p.rename(p.with_name('old'))\n    p.write_text('curl\\n')\n"
        f"    p.chmod({0o666 if original_trusted else 0o644})\n"
        "os.execv('/usr/bin/stat', ['/usr/bin/stat', *sys.argv[1:]])\n",
    )
    result = helper.run()
    assert result.returncode == 0 and result.stderr == ""
    assert ("1 recorded, all installed" in result.stdout) == original_trusted
    assert ("not trusted:" in result.stdout) != original_trusted
    assert [e["argv"][-1] for e in helper.tools.events("dpkg")] == (
        ["jq"] if original_trusted else []
    )


def test_replay_tagged_output_and_clean_environment(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("Bad\njq\n")
    result = helper.run(
        env=dict(os.environ, PATH="/nonexistent", APT_CONFIG="hostile", HOME="hostile")
    )
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout.splitlines()[0].startswith("W\tInvalid package name")
    assert result.stdout.splitlines()[1] == "I\tinstalling 1 of 1"
    for event in helper.tools.events():
        assert event["env"] == FIXED_ENV and event["stdin"] == ""
    for line in result.stdout.splitlines():
        assert re.match(r"^[IW]\t[^\r\n]*$", line)


def test_replay_install_has_no_timeout():
    source = (ROOT / "scripts/djinn-apt-root.sh").read_text()
    calls = [line for line in source.splitlines() if 'apt_install "$@"' in line]
    assert len(calls) == 2 and all("timeout" not in line for line in calls)
    assert 'run_clean "$timeout_bin" "$update_timeout" "$apt_bin" -qq update </dev/null' in source
    assert 'run_clean "$apt_bin" ${quiet:+"$quiet"} install' in source


def test_record_atomic_steps_and_serialized_recording(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("curl\n")
    trace = tmp_path / "steps"
    for tool in ("chmod", "sync", "mv"):
        helper.tool(
            tool,
            "#!/usr/bin/python3\nimport fcntl, os, sys\n"
            f"with open({str(helper.lock)!r}) as lock:\n"
            "    try:\n        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except BlockingIOError:\n        pass\n"
            "    else:\n        sys.exit(42)\n"
            f'with open({str(trace)!r}, "a") as log:\n'
            f'    log.write({tool!r} + " " + " ".join(sys.argv[1:]) + "\\n")\n'
            f'os.execv("/usr/bin/{tool}", ["/usr/bin/{tool}", *sys.argv[1:]])\n',
        )
    result = helper.run(["install", "jq"])
    assert result.returncode == 0 and result.stderr == ""
    steps = trace.read_text().splitlines()
    assert [line.split()[0] for line in steps] == ["chmod", "sync", "mv"]
    temporary = steps[0].split()[2]
    assert Path(temporary).parent == helper.list_dir
    assert Path(temporary).name.startswith(".packages.txt.")
    assert steps[0] == f"chmod 0644 {temporary}"
    assert steps[1] == f"sync -- {temporary}"
    assert steps[2] == f"mv -f -- {temporary} {helper.list}"
    assert helper.list.read_text() == "curl\njq\n"


@pytest.mark.parametrize("metadata", ["11a4 1000 3", "41ed 1000 3"])
def test_descriptor_regular_file_check(tmp_path, metadata):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    helper.tool(
        "stat",
        f'#!/bin/sh\ncase "$*" in *self/fd/3*) printf "{metadata}\\n" ;; '
        '*) exec /usr/bin/stat "$@" ;; esac\n',
    )
    result = helper.run()
    assert (
        result.returncode == 0 and "not trusted: opened list is not a regular file" in result.stdout
    )
    assert helper.tools.events() == []


def test_capped_descriptor_read_checks_actual_bytes(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    trace = tmp_path / "read-args"
    helper.tool(
        "head",
        "#!/usr/bin/python3\nimport os, sys\nfrom pathlib import Path\n"
        f'Path({str(trace)!r}).write_text(" ".join(sys.argv[1:]))\n'
        "assert os.fstat(0).st_ino == os.fstat(3).st_ino\n"
        'sys.stdout.write("#" * 1048577)\n',
    )
    result = helper.run()
    assert result.returncode == 0 and "not trusted: read exceeds 1 MiB" in result.stdout
    assert trace.read_text() == "-c 1048577"
    assert helper.tools.events() == []


@pytest.mark.parametrize("point", ["directory", "descriptor", "snapshot"])
def test_stat_failure_is_closed(tmp_path, point):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    fail_target = {
        "directory": str(helper.list_dir),
        "descriptor": "/proc/self/fd/3",
        "snapshot": "raw",
    }[point]
    helper.tool(
        "stat",
        "#!/bin/sh\nfor arg do\n"
        f'    case "$arg" in *{shlex.quote(fail_target)}) exit 42 ;; esac\ndone\n'
        'exec /usr/bin/stat "$@"\n',
    )
    result = helper.run()
    assert result.returncode == 0 and result.stdout.count("W\t") == 1
    assert helper.tools.events() == []


def test_replay_zero_elapsed_budget_boundary(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    helper.script.write_text(substitute(helper.script.read_text(), "check_budget", "60", "0"))
    helper.tool("date", "#!/bin/sh\nprintf '42\\n'\n")
    result = helper.run()
    assert result.returncode == 0 and "exceeded" not in result.stdout
    assert helper.tools.events("apt")[-1]["argv"] == ["-qq", *INSTALL, "jq"]


@pytest.mark.parametrize("call", [1, 2, 3, 4])
def test_replay_clock_failure_skips(tmp_path, call):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    count = tmp_path / "clock-count"
    helper.tool(
        "date",
        "#!/usr/bin/python3\nfrom pathlib import Path\nimport sys\n"
        f"p=Path({str(count)!r})\nn=int(p.read_text()) + 1 if p.exists() else 1\n"
        f"p.write_text(str(n))\nprint(42)\nsys.exit(42 if n == {call} else 0)\n",
    )
    result = helper.run()
    assert result.returncode == 0 and "Cannot read check clock; replay skipped." in result.stdout
    assert all("install" not in e["argv"] for e in helper.tools.events("apt"))


def test_replay_cache_failure(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    helper.tools.configure(cache_status=42)
    result = helper.run()
    assert result.returncode == 0 and "apt-cache failed" in result.stdout
    assert [event["argv"] for event in helper.tools.events("apt")] == [["-qq", "update"]]


@pytest.mark.parametrize("size", [64, 65])
def test_helper_refusal_display_boundary(tmp_path, size):
    result = HelperHarness(tmp_path).run(["install", "A" * size])
    assert result.returncode == 2
    assert (f"{size}-byte argument" in result.stderr) == (size > 64)
    assert ("A" * size in result.stderr) == (size <= 64)


@pytest.mark.parametrize("operation", ["update", "install", "replay"])
def test_every_operation_requires_lock(tmp_path, operation):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    with helper.lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = helper.run([operation, *(["jq"] if operation == "install" else [])])
    assert result.returncode == 100 and "lock" in result.stderr
    assert helper.tools.events() == []


def test_record_staging_failure(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("curl\n")
    count = tmp_path / "count"
    helper.tool(
        "mktemp",
        "#!/usr/bin/python3\nimport os, sys\nfrom pathlib import Path\n"
        f"p=Path({str(count)!r})\nn=int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\nif n == 2: sys.exit(42)\n"
        'os.execv("/usr/bin/mktemp", ["/usr/bin/mktemp", *sys.argv[1:]])\n',
    )
    result = helper.run(["install", "jq"])
    assert result.returncode == 0 and result.stderr.count("installed, but not recorded:") == 1
    assert helper.list.read_text() == "curl\n"
    assert not list(helper.list_dir.glob(".packages.txt.*"))
    assert not list(tmp_path.glob("work.*"))


def test_replay_failure_tail_read_failure(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    helper.tools.configure(install_status=37)
    helper.tool("tail", "#!/bin/sh\nexit 42\n")
    result = helper.run()
    assert result.returncode == 0
    assert result.stdout.endswith("W\tCannot read failure output.\n")


def test_all_utility_dispatch_is_absolute_and_clean(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.list.write_text("jq\n")
    helper.tools.configure(install_status=37)
    trace = tmp_path / "utilities"
    binaries = [
        "stat",
        "mktemp",
        "mv",
        "chmod",
        "sync",
        "head",
        "tr",
        "awk",
        "timeout",
        "date",
        "rm",
        "tail",
    ]
    for binary in binaries:
        helper.tool(
            binary,
            "#!/usr/bin/python3\nimport os, sys\n"
            f"assert dict(os.environ) == {FIXED_ENV!r}"
            + (" | {'LC_ALL': 'C'}\n" if binary == "awk" else "\n")
            + f'with open({str(trace)!r}, "a") as log: log.write({binary!r} + "\\n")\n'
            + f'os.execv("/usr/bin/{binary}", ["/usr/bin/{binary}", *sys.argv[1:]])\n',
        )
    env = dict(os.environ, PATH="/nonexistent", HOME="hostile", APT_CONFIG="hostile")
    result = helper.run(env=env)
    assert result.returncode == 0 and result.stderr == "" and "status 37" in result.stdout
    helper.tools.configure(install_status=0)
    result = helper.run(["install", "curl"], env=env)
    assert result.returncode == 0 and result.stderr == ""
    assert helper.list.read_text() == "jq\ncurl\n"
    assert set(trace.read_text().splitlines()) == set(binaries)
    # Exercise cleanup of an unpublished temporary file as well as normal cleanup.
    chmod = tmp_path / "chmod-substitute"
    source = chmod.read_text()
    chmod.write_text(
        source.replace('os.execv("/usr/bin/chmod",', 'sys.exit(42)\nos.execv("/usr/bin/chmod",')
    )
    result = helper.run(["install", "curl"], env=env)
    assert result.returncode == 0 and len(result.stderr.splitlines()) == 1
    assert "installed, but not recorded: cannot set list mode" in result.stderr
    assert not list(helper.list_dir.glob(".packages.txt.*"))


@pytest.mark.parametrize("operation", ["replay", "install"])
def test_one_linear_awk_pass(tmp_path, operation):
    helper = HelperHarness(tmp_path)
    helper.list.write_bytes(b" " * 1048576)
    trace = tmp_path / "awk-calls"
    helper.tool(
        "awk",
        f'#!/bin/sh\nprintf "pass\\n" >> {shlex.quote(str(trace))}\n'
        'exec /usr/bin/awk "$@"\n',
    )
    started = children_cpu_seconds()
    result = helper.run([operation, *(["jq"] if operation == "install" else [])])
    assert result.returncode == 0 and children_cpu_seconds() - started < 3
    assert trace.read_text() == "pass\n"
    assert helper.list.read_bytes() == b" " * 1048576


@pytest.mark.parametrize("contents", [None, "", "# only\n \t\n", "jq\njq # duplicate\n curl\n"])
def test_replay_no_network_when_satisfied(tmp_path, contents):
    replay = HelperHarness(tmp_path)
    if contents is not None:
        replay.list.write_text(contents)
    replay.tools.configure(states={"jq": ["installed", 0], "curl": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0 and result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    if contents and "jq" in contents:
        assert "2 recorded, all installed" in result.stdout
        assert [event["argv"] for event in replay.tools.events("dpkg")] == [
            ["-W", "-f=${db:Status-Status}", "--", "jq"],
            ["-W", "-f=${db:Status-Status}", "--", "curl"],
        ]
    else:
        assert "none recorded" in result.stdout
        assert replay.tools.events("dpkg") == []
    assert [event["argv"] for event in replay.tools.events("apt")] == [] and replay.tools.events(
        "cache"
    ) == []


def test_replay_missing_only_known(tmp_path):
    replay = HelperHarness(tmp_path)
    contents = "# retained\n jq # installed\n curl\ncurl\npython3.11\ng++\njqx\njq.\n"
    replay.list.write_text(contents)
    before = replay.list.stat()
    replay.tools.configure(
        states={
            "jq": ["installed", 0],
            "python3.11": ["half-installed", 0],
            "g++": ["config-files", 0],
        },
        cache={"jqx": "", "jq.": "jq.extra\n"},
    )
    result = replay.run()
    assert result.returncode == 0 and result.stderr == ""
    assert "installing 5 of 6" in result.stdout
    for name in ("jqx", "jq."):
        assert f"Unknown package {name};" in result.stdout
    assert [event["argv"] for event in replay.tools.events("apt")] == [
        ["-qq", "update"],
        ["-qq", *INSTALL, "curl", "python3.11", "g++"],
    ]
    assert [event["kind"] for event in replay.tools.events()] == ["dpkg"] * 6 + ["apt"] + [
        "cache"
    ] * 5 + ["apt"]
    assert [event["argv"][-1] for event in replay.tools.events("cache")] == [
        "curl",
        "python3.11",
        "g++",
        "jqx",
        "jq.",
    ]
    assert replay.list.read_text() == contents
    assert replay.list.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize(
    "name", [name for name in BAD_NAMES if name and name.strip() == name and "\n" not in name]
)
def test_replay_invalid_lines(tmp_path, name):
    replay = HelperHarness(tmp_path)
    replay.list.write_text(f"# ignored\n  \n{name}\njq # valid\njq\n")
    result = replay.run()
    assert result.returncode == 0 and result.stdout.count("Invalid package name") == 1
    assert result.stdout.count("W\t") == 1
    # One warning and one status; controls cannot add lines
    # or terminal escapes to an otherwise valid replay.
    assert len(result.stdout.splitlines()) == 2
    assert not any(ord(char) < 32 for char in result.stdout.replace("\n", "").replace("\t", ""))
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == ["jq"]
    assert replay.tools.events("apt")[-1]["argv"] == ["-qq", *INSTALL, "jq"]


@pytest.mark.parametrize(
    "state,status",
    [
        ("installed", 0),
        ("installed", 1),
        ("half-installed", 0),
        ("config-files", 0),
        ("", 1),
        ("installed\n", 0),
        (" installed", 0),
        ("installed\nextra", 0),
    ],
)
def test_replay_status_exact(tmp_path, state, status):
    replay = HelperHarness(tmp_path)
    replay.list.write_text(" # comment\n\tjq # one\njq\ncurl")
    replay.tools.configure(states={"jq": ["installed", 0], "curl": [state, status]})
    result = replay.run()
    assert result.returncode == 0
    assert "W\t" not in result.stdout and result.stderr == ""
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == ["jq", "curl"]
    if state == "installed" and status == 0:
        assert "2 recorded, all installed" in result.stdout
        assert [event["argv"] for event in replay.tools.events("apt")] == []
    else:
        assert "installing 1 of 2" in result.stdout
        assert replay.tools.events("apt")[-1]["argv"] == ["-qq", *INSTALL, "curl"]


@pytest.mark.parametrize("stage", ["update", "install"])
@pytest.mark.parametrize("status", [37, 100])
def test_replay_failures_are_warnings(tmp_path, stage, status):
    replay = HelperHarness(tmp_path)
    replay.list.write_text("jq\n")
    output = (
        "first omitted\nsecond omitted\nthird retained\nfourth retained\n"
        "E: Could not get lock /var/lib/dpkg/lock-frontend. "
        "It is held by process 1234 (apt-get)\n"
    )
    replay.tools.configure(**{stage + "_status": status, stage + "_stderr": output})
    result = replay.run()
    assert result.returncode == 0 and result.stderr == ""
    assert f"Package {stage} failed (status {status})" in result.stdout
    assert "first omitted" not in result.stdout and "second omitted" not in result.stdout
    assert result.stdout.endswith("".join("W\t" + line + "\n" for line in output.splitlines()[-3:]))
    assert len([event["argv"] for event in replay.tools.events("apt")]) == (
        1 if stage == "update" else 2
    )
    if stage == "update":
        assert replay.tools.events("cache") == []
    assert replay.list.read_text() == "jq\n"


def test_replay_skips_nul_line_instead_of_joining(tmp_path):
    # NUL must not join neighbouring characters: "j\0q" is not "jq".
    replay = HelperHarness(tmp_path)
    replay.list.write_bytes(b"j\x00q\ncurl\n")
    replay.tools.configure(states={"curl": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0
    assert result.stdout.count("Invalid package name") == 1
    assert "1 recorded, all installed" in result.stdout
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == ["curl"]
    assert [event["argv"] for event in replay.tools.events("apt")] == [] and replay.tools.events(
        "cache"
    ) == []
    assert "null byte" not in result.stdout


def test_replay_accepts_list_at_size_limit(tmp_path):
    replay = HelperHarness(tmp_path)
    replay.list.write_bytes(b"jq\n" + b"#" * (1048576 - 3))
    replay.tools.configure(states={"jq": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0 and "1 recorded, all installed" in result.stdout


FETCH_WARNINGS = (
    "W: Failed to fetch http://deb.debian.org/debian/dists/bookworm/InRelease  "
    "Temporary failure resolving 'deb.debian.org'\n"
    "W: Some index files failed to download. They have been ignored, or old ones used instead.\n"
    "E: An index was rejected.\n"
)


@pytest.mark.parametrize("output", [FETCH_WARNINGS, "Reading package lists...\n"])
def test_replay_surfaces_update_warnings(tmp_path, output):
    # Measured live: apt-get update without network exits 0 and only prints W: lines.
    replay = HelperHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.tools.configure(update_status=0, update_stderr=output)
    result = replay.run()
    assert result.returncode == 0 and result.stderr == ""
    reported = "W: Failed to fetch" in result.stdout
    assert reported == (output == FETCH_WARNINGS)
    if reported:
        assert "W: Failed to fetch http://deb.debian.org" in result.stdout
        assert "W: Some index files failed to download." in result.stdout
        assert "W\tE: An index was rejected." in result.stdout
    assert "Reading package lists" not in result.stdout


def fast_package_checks(replay):
    # Shell fakes keep the timing bound about replay work, not Python startup.
    log = shlex.quote(str(replay.tools.log))
    for role in ("dpkg", "cache"):
        executable(
            replay.tools.directory / role,
            "#!/bin/sh\n"
            f'printf \'{{"kind":"{role}","argv":['
            + ",".join(['"%s"'] * (4 if role == "dpkg" else 3))
            + f']}}\\n\' "$@" >> {log}\n'
            + ("exit 1\n" if role == "dpkg" else "printf '%s\\n' \"$3\"\n"),
        )


def test_replay_space_list_at_cap_is_prompt(tmp_path):
    replay = HelperHarness(tmp_path)
    replay.list.write_bytes(b" " * 1048576)
    # Child CPU bounds parser work without charging scheduler stalls.
    started = children_cpu_seconds()
    result = replay.run()
    cpu = children_cpu_seconds() - started
    print(f"1 MiB spaces: {cpu:.3f} CPU s")
    assert cpu < 3
    assert result.returncode == 0 and "none recorded" in result.stdout
    assert "W\t" not in result.stdout
    assert (
        replay.tools.events() == []
        and [event["argv"] for event in replay.tools.events("apt")] == []
    )


@pytest.mark.parametrize("count", [256, 257, 1048576 // 11])
def test_replay_distinct_name_cap(tmp_path, count):
    replay = HelperHarness(tmp_path)
    fast_package_checks(replay)
    names = [f"pkg{index:07d}" for index in range(count)]
    # Repeating the first overflow name must not count it twice.
    entries = names + [names[0], names[-1]] if count == 257 else names
    replay.list.write_text("\n".join(entries) + "\n")
    # Child CPU catches per-entry subprocesses/quadratic deduplication, independent of load.
    started = children_cpu_seconds()
    result = replay.run()
    cpu = children_cpu_seconds() - started
    print(f"{replay.list.stat().st_size} bytes / {count} distinct names: {cpu:.3f} CPU s")
    assert cpu < 3
    assert result.returncode == 0
    expected = names[:256]
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == expected
    assert [event["argv"][-1] for event in replay.tools.events("cache")] == expected
    assert len([event["argv"] for event in replay.tools.events("apt")]) == 2
    assert replay.tools.events("apt")[-1]["argv"] == ["-qq", *INSTALL, *expected]
    assert "installing 256 of 256" in result.stdout
    if count > 256:
        assert (
            f"More than 256 package names in {replay.list}; {count - 256} not replayed."
            in result.stdout
        )
        assert result.stdout.count("not replayed") == 1
    else:
        assert "not replayed" not in result.stdout


def test_replay_name_byte_limit(tmp_path):
    replay = HelperHarness(tmp_path)
    accepted = "a" * 128
    replay.list.write_text(f"{accepted}\n{'a' * 129}\n")
    result = replay.run()
    assert result.returncode == 0
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == [accepted]
    assert replay.tools.events("apt")[-1]["argv"] == ["-qq", *INSTALL, accepted]
    assert result.stdout.count("Invalid package name") == 1
    assert f'"{"a" * 32}..." (129 bytes) in {replay.list} (line 2)' in result.stdout


def test_replay_invalid_diagnostics_are_bounded(tmp_path):
    replay = HelperHarness(tmp_path)
    invalid = [b"j\x00q", b"\x1b" + b"A" * 31, b"A" * 33, b"\xff\x7f\x01", b"bad name"]
    replay.list.write_bytes(
        b"# heading\n\n" + b"\n".join(invalid + [b"JQ", b"JQ", b"foo.deb"]) + b"\njq\n"
    )
    replay.tools.configure(states={"jq": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0
    warnings = [line for line in result.stdout.splitlines() if "Invalid package name" in line]
    assert len(warnings) == 5
    shown = ["j?q", "?" + "A" * 31, "A" * 32 + "...", "???", "bad name"]
    for number, (line, display, entry) in enumerate(
        zip(warnings, shown, invalid, strict=True), start=3
    ):
        assert (
            f'Invalid package name "{display}" ({len(entry)} bytes) in '
            f"{replay.list} (line {number}); skipped."
        ) in line
    assert result.stdout.count("W\t") == 6
    assert f"3 more invalid lines in {replay.list} skipped." in result.stdout
    assert all(32 <= ord(char) <= 126 for char in result.stdout.replace("\n", "").replace("\t", ""))
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == ["jq"]


@pytest.mark.parametrize("count", [5, 6])
def test_replay_warning_cap_boundary(tmp_path, count):
    replay = HelperHarness(tmp_path)
    replay.list.write_text("\n".join([f"Bad{index}" for index in range(count)] + ["jq"]))
    replay.tools.configure(states={"jq": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0 and result.stdout.count("Invalid package name") == 5
    assert result.stdout.count("W\t") == (1 if count > 5 else 0) + 5
    if count == 6:
        assert f"1 more invalid lines in {replay.list} skipped." in result.stdout
    else:
        assert "more invalid lines" not in result.stdout


@pytest.mark.parametrize("tool", ["tr", "awk"])
@pytest.mark.parametrize("partial", [False, True])
def test_replay_conversion_failures_skip_partial_input(tmp_path, tool, partial):
    replay = HelperHarness(tmp_path)
    replay.list.write_text("jq\n")
    output = ("jq\\n" if tool == "tr" else "V\\tjq\\n") if partial else ""
    replay.tool(tool, f"#!/bin/sh\nprintf '{output}'\nexit 42\n")
    result = replay.run()
    assert result.returncode == 0 and "W\t" in result.stdout
    assert (
        replay.tools.events() == []
        and [event["argv"] for event in replay.tools.events("apt")] == []
    )


@pytest.mark.parametrize("budget", ["0", "1"])
@pytest.mark.parametrize("stage", ["dpkg", "cache"])
def test_replay_check_budget(tmp_path, budget, stage):
    replay = HelperHarness(tmp_path)
    names = [f"pkg{index}" for index in range(12)]
    replay.list.write_text("\n".join(names) + "\n")
    replay.script.write_text(substitute(replay.script.read_text(), "check_budget", "60", budget))
    ticks = [100, 101 + int(budget)] if stage == "dpkg" else [100] * 13 + [200, 201 + int(budget)]
    check_clock(replay, ticks)
    if stage == "dpkg":
        # An installed result must still be subject to the budget before continue.
        replay.tools.configure(states={name: ["installed", 0] for name in names})
    result = replay.run()
    assert result.returncode == 0
    assert result.stdout.count(f"Package checks exceeded {budget} s; replay skipped.") == 1
    assert 0 < len(replay.tools.events(stage)) < len(names)
    assert [event["argv"] for event in replay.tools.events("apt")] == (
        [] if stage == "dpkg" else [["-qq", "update"]]
    )


@pytest.mark.parametrize("stage", ["dpkg", "cache"])
def test_replay_budget_covers_final_check(tmp_path, stage):
    replay = HelperHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.script.write_text(substitute(replay.script.read_text(), "check_budget", "60", "0"))
    check_clock(replay, [100, 101] if stage == "dpkg" else [100, 100, 200, 201])
    result = replay.run()
    assert result.returncode == 0
    assert "Package checks exceeded 0 s; replay skipped." in result.stdout
    assert len(replay.tools.events(stage)) == 1
    assert len([event["argv"] for event in replay.tools.events("apt")]) == (
        0 if stage == "dpkg" else 1
    )


def test_replay_update_does_not_spend_check_budget(tmp_path):
    # After a recreation the update can take minutes; the cache checks still run.
    replay = HelperHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.script.write_text(substitute(replay.script.read_text(), "check_budget", "60", "1"))
    # The clock jumps during update; cache checks get a fresh budget afterward.
    check_clock(replay, [100, 100, 200, 200])
    result = replay.run()
    assert result.returncode == 0 and "Package checks exceeded" not in result.stdout
    assert replay.tools.events("apt")[-1]["argv"] == ["-qq", *INSTALL, "jq"]


def test_replay_update_timeout_kills_fake(tmp_path):
    replay = HelperHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.script.write_text(substitute(replay.script.read_text(), "update_timeout", "300", "1"))
    # Python sleeps in the fake itself: timeout can kill it without orphan children.
    replay.tools.configure(update_delay=3)
    result = replay.run()
    assert result.returncode == 0
    assert "Package list update timed out after 1 s; replay skipped." in result.stdout
    assert len([event["argv"] for event in replay.tools.events("apt")]) == 1
    assert replay.tools.events("cache") == []
    pid = replay.tools.events("apt")[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_replay_update_status_124(tmp_path):
    replay = HelperHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.tools.configure(update_status=124)
    result = replay.run()
    assert result.returncode == 0
    assert "Package list update timed out after 300 s; replay skipped." in result.stdout
    assert "Package update failed" not in result.stdout
    assert (
        len([event["argv"] for event in replay.tools.events("apt")]) == 1
        and replay.tools.events("cache") == []
    )


@pytest.mark.skipif(not Path("/usr/bin/mawk").exists(), reason="mawk unavailable")
def test_replay_explicit_mawk(tmp_path):
    replay = HelperHarness(tmp_path)
    replay.tool("awk", '#!/bin/sh\nexec /usr/bin/mawk "$@"\n')
    replay.list.write_bytes(b" \t\r\f\vjq\v\f\r\t # comment\njq\nfoo.deb\nj\x00q\n")
    replay.tools.configure(states={"jq": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0 and "1 recorded, all installed" in result.stdout
    assert result.stdout.count("Invalid package name") == 2
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == ["jq"]
    assert [event["argv"] for event in replay.tools.events("apt")] == [] and replay.tools.events(
        "cache"
    ) == []
