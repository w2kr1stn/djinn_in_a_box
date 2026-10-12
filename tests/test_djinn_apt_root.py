"""Run substituted POSIX scripts only; never dispatch a host package tool."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = "/usr/local/libexec/djinn-apt-root"
LIST = "/home/dev/.local/share/djinn-packages/packages.txt"
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
    }.items():
        assert source.count(f"{name}='/usr/bin/{binary}'") == 1
    assert source.count('run_clean "$apt_bin" "$@"') == 1
    assert source.count('run_clean "$cache_bin" pkgnames -- "$name"') == 1
    # Tokenize quoted messages and command separators; never exempt an entire
    # printf line that could contain a second, unsafe dispatch.
    tokens = shlex.shlex(source, posix=True, punctuation_chars=True)
    tokens.whitespace_split = True
    for token in tokens:
        if token in ("apt_bin=/usr/bin/apt-get", "cache_bin=/usr/bin/apt-cache"):
            continue
        assert Path(token).name not in ("apt-get", "apt-cache"), token


FAKE_BODY = r"""
import json, os, sys, time
from pathlib import Path
config = json.loads(Path(config_path).read_text())
args = sys.argv[1:]
def event(kind, **extra):
    with open(log_path, "a") as handle:
        handle.write(json.dumps(dict(kind=kind, argv=args, **extra)) + "\n")
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
if role in ("apt", "cache"):
    event(role, env=dict(os.environ), stdin=sys.stdin.read())
else:
    event(role)
if role == "cache":
    output = config.get("cache", {}).get(args[-1], args[-1] + "\n")
    sys.stdout.write(output)
    sys.exit(config.get("cache_status", 0))
if role == "dpkg":
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
        source = (ROOT / "scripts/djinn-apt-root.sh").read_text()
        assert_helper_dispatch(source)
        for name, old, new in (
            ("id_bin", "/usr/bin/id", directory / "id"),
            ("apt_bin", "/usr/bin/apt-get", directory / "apt"),
            ("cache_bin", "/usr/bin/apt-cache", directory / "cache"),
            ("env_bin", "/usr/bin/env", "/usr/bin/env"),
            ("flock_bin", "/usr/bin/flock", "/usr/bin/flock"),
            ("grep_bin", "/usr/bin/grep", "/usr/bin/grep"),
            ("lock_path", "/run/djinn-apt-root.lock", self.lock),
            ("lock_wait", "300", "0.3"),
        ):
            source = substitute(source, name, old, new)
        assert "/usr/bin/apt-get" not in source and "/usr/bin/apt-cache" not in source
        self.script = executable(directory / "helper.sh", source)

    def run(self, args, **kwargs):
        return subprocess.run(
            ["/bin/sh", str(self.script), *args],
            capture_output=True,
            text=True,
            input="caller input",
            timeout=3,
            check=False,
            **kwargs,
        )


def test_helper_fixed_binary_dispatch():
    assert_helper_dispatch((ROOT / "scripts/djinn-apt-root.sh").read_text())


def test_helper_requires_root(tmp_path):
    helper = HelperHarness(tmp_path)
    helper.tools.configure(uid="1000")
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
    started = time.monotonic()
    result = helper.run(args)
    assert time.monotonic() - started < 2
    assert result.returncode == 2 and len(result.stderr.splitlines()) == 1
    assert f"({len(args[-1])}-byte argument)" in result.stderr
    assert not helper.lock.exists() and helper.tools.events() == []
