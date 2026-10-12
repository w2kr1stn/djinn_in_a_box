from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest
from test_djinn_apt_root import BAD_NAMES, HELPER, LIST, ROOT, FakeTools, executable, substitute


class ReplayHarness:
    def __init__(self, directory):
        self.tools = FakeTools(directory)
        self.list = directory / "packages.txt"
        source = (ROOT / "scripts/package-replay.sh").read_text()
        for name, old, new in (
            ("list_path", LIST, self.list),
            ("helper_path", HELPER, HELPER),
            ("sudo_target", "root", "root"),
            ("dpkg_bin", "/usr/bin/dpkg-query", directory / "dpkg"),
            ("cache_bin", "/usr/bin/apt-cache", directory / "cache"),
        ):
            source = substitute(source, name, old, new)
        assert "/usr/bin/apt-cache" not in source and "/usr/bin/dpkg-query" not in source
        self.script = executable(directory / "replay.sh", source)
        self.env = dict(
            os.environ,
            PATH=f"{directory}:/usr/bin:/bin",
            NO_COLOR="1",
            OUTPUT_LIB=str(ROOT / "scripts/output-lib.sh"),
        )
        # Catch accidental use of the shim without ever reaching host apt.
        executable(directory / "apt-get", "#!/bin/sh\necho SHIM_CALLED >&2\nexit 99\n")

    def run(self):
        stderr_path = self.tools.directory / "stderr.log"
        self.tools.configure(announcement_log=str(stderr_path))
        with stderr_path.open("w") as stderr:
            result = subprocess.run(
                ["/bin/bash", str(self.script)],
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        result.stderr = stderr_path.read_text()
        return result


@pytest.mark.parametrize("contents", [None, "", "# only\n \t\n", "jq\njq # duplicate\n curl\n"])
def test_replay_no_network_when_satisfied(tmp_path, contents):
    replay = ReplayHarness(tmp_path)
    if contents is not None:
        replay.list.write_text(contents)
    replay.tools.configure(states={"jq": ["installed", 0], "curl": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0 and result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    if contents and "jq" in contents:
        assert "2 recorded, all installed" in result.stderr
        assert [event["argv"] for event in replay.tools.events("dpkg")] == [
            ["-W", "-f=${db:Status-Status}", "--", "jq"],
            ["-W", "-f=${db:Status-Status}", "--", "curl"],
        ]
    else:
        assert "none recorded" in result.stderr
        assert replay.tools.events("dpkg") == []
    assert replay.tools.sudo_calls() == [] and replay.tools.events("cache") == []


def test_replay_missing_only_known(tmp_path):
    replay = ReplayHarness(tmp_path)
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
        sudo_marker=True,
    )
    result = replay.run()
    assert result.returncode == 0 and result.stdout == ""
    assert "installing 5 of 6" in result.stderr
    assert result.stderr.count("using sudo") == 2
    announcement = f"Packages: using sudo to run {HELPER} as root."
    assert result.stderr.splitlines().count(announcement) == 2
    # The fake reads the parent's stderr file before dispatching each elevation.
    for index, event in enumerate(replay.tools.events("sudo"), start=1):
        assert event["announcement"].splitlines().count(announcement) == index
    assert "SHIM_CALLED" not in result.stderr
    for name in ("jqx", "jq."):
        assert f"Unknown package {name};" in result.stderr
    assert str(replay.list) in result.stderr
    prefix = ["-n", "-u", "root", "--", HELPER, "-qq"]
    assert replay.tools.sudo_calls() == [
        [*prefix, "update"],
        [*prefix, "install", "curl", "python3.11", "g++"],
    ]
    events = replay.tools.events()
    kinds = [event["kind"] for event in events]
    assert kinds == ["dpkg"] * 6 + ["sudo", "helper"] + ["cache"] * 5 + ["sudo", "helper"]
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
    replay = ReplayHarness(tmp_path)
    replay.list.write_text(f"# ignored\n  \n{name}\njq # valid\njq\n")
    result = replay.run()
    assert result.returncode == 0 and result.stderr.count("Invalid package name") == 1
    assert result.stderr.count("[warn]") == 1
    # One warning, one status, and two announcements; controls cannot add lines
    # or terminal escapes to an otherwise valid replay.
    assert len(result.stderr.splitlines()) == 4
    assert not any(ord(char) < 32 for char in result.stderr.replace("\n", ""))
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == ["jq"]
    assert replay.tools.sudo_calls()[-1][-2:] == ["install", "jq"]


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
    replay = ReplayHarness(tmp_path)
    replay.list.write_text(" # comment\n\tjq # one\njq\ncurl")
    replay.tools.configure(states={"jq": ["installed", 0], "curl": [state, status]})
    result = replay.run()
    assert result.returncode == 0
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == ["jq", "curl"]
    if state == "installed" and status == 0:
        assert "2 recorded, all installed" in result.stderr
        assert replay.tools.sudo_calls() == []
    else:
        assert "installing 1 of 2" in result.stderr
        assert replay.tools.sudo_calls()[-1][-2:] == ["install", "curl"]


@pytest.mark.parametrize("stage", ["update", "install"])
@pytest.mark.parametrize("status", [37, 100])
def test_replay_failures_are_warnings(tmp_path, stage, status):
    replay = ReplayHarness(tmp_path)
    replay.list.write_text("jq\n")
    output = (
        "first omitted\nsecond omitted\nthird retained\nfourth retained\n"
        "E: Could not get lock /var/lib/dpkg/lock-frontend. "
        "It is held by process 1234 (apt-get)\n"
    )
    replay.tools.configure(**{stage + "_status": status, stage + "_stderr": output})
    result = replay.run()
    assert result.returncode == 0 and result.stdout == ""
    assert f"Package {stage} failed (status {status})" in result.stderr
    assert "first omitted" not in result.stderr and "second omitted" not in result.stderr
    assert result.stderr.endswith("\n".join(output.splitlines()[-3:]) + "\n")
    assert len(replay.tools.sudo_calls()) == (1 if stage == "update" else 2)
    if stage == "update":
        assert replay.tools.events("cache") == []
    assert replay.list.read_text() == "jq\n"


def test_replay_skips_nul_line_instead_of_joining(tmp_path):
    # Bash drops NUL bytes in substitutions; "j\0q" must not become "jq".
    replay = ReplayHarness(tmp_path)
    replay.list.write_bytes(b"j\x00q\ncurl\n")
    replay.tools.configure(states={"curl": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0
    assert result.stderr.count("Invalid package name") == 1
    assert "1 recorded, all installed" in result.stderr
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == ["curl"]
    assert replay.tools.sudo_calls() == [] and replay.tools.events("cache") == []
    assert "null byte" not in result.stderr


def release_fifo(path, mode):
    # A regressed script blocks opening the FIFO; opening the other end lets it
    # finish instead of leaving a stuck child behind after the timeout.
    with contextlib.suppress(OSError):
        os.close(os.open(path, mode | os.O_NONBLOCK))


@pytest.mark.parametrize("kind", ["fifo", "oversize"])
def test_replay_skips_untrusted_list_shapes(tmp_path, kind):
    # A FIFO blocked the start forever; a huge file would stall it.
    replay = ReplayHarness(tmp_path)
    if kind == "fifo":
        os.mkfifo(replay.list)
    else:
        replay.list.write_bytes(b"jq\n" + b"#" * (1048576 - 2))
    try:
        result = replay.run()
    finally:
        if kind == "fifo":
            release_fifo(replay.list, os.O_WRONLY)
    assert result.returncode == 0 and "[warn]" in result.stderr
    assert str(replay.list) in result.stderr
    assert replay.tools.sudo_calls() == [] and replay.tools.events() == []


def test_replay_bounds_the_read_when_the_list_is_swapped(tmp_path):
    # The regular-file check sees the path; a FIFO swapped in before the open must
    # not hold the start: the bounded child gives up and the replay warns.
    replay = ReplayHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.script.write_text(substitute(replay.script.read_text(), "read_timeout", "10", "1"))
    executable(
        tmp_path / "head",
        f"#!/bin/sh\nrm -f {replay.list}\nmkfifo {replay.list}\nexec /usr/bin/head \"$@\"\n",
    )
    started = time.monotonic()
    try:
        result = replay.run()
    finally:
        release_fifo(replay.list, os.O_WRONLY)
    assert time.monotonic() - started < 3
    assert result.returncode == 0
    assert f"Cannot read package list {replay.list} within 1 s." in result.stderr
    assert replay.tools.events() == [] and replay.tools.sudo_calls() == []


def test_replay_accepts_list_at_size_limit(tmp_path):
    replay = ReplayHarness(tmp_path)
    replay.list.write_bytes(b"jq\n" + b"#" * (1048576 - 3))
    replay.tools.configure(states={"jq": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0 and "1 recorded, all installed" in result.stderr


FETCH_WARNINGS = (
    "W: Failed to fetch http://deb.debian.org/debian/dists/bookworm/InRelease  "
    "Temporary failure resolving 'deb.debian.org'\n"
    "W: Some index files failed to download. They have been ignored, or old ones used instead.\n"
)


@pytest.mark.parametrize("output", [FETCH_WARNINGS, "Reading package lists...\n"])
def test_replay_surfaces_update_warnings(tmp_path, output):
    # Measured live: apt-get update without network exits 0 and only prints W: lines.
    replay = ReplayHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.tools.configure(update_status=0, update_stderr=output)
    result = replay.run()
    assert result.returncode == 0 and result.stdout == ""
    reported = "Package list update reported problems" in result.stderr
    assert reported == (output == FETCH_WARNINGS)
    if reported:
        assert "W: Failed to fetch http://deb.debian.org" in result.stderr
        assert "W: Some index files failed to download." in result.stderr
    assert "Reading package lists" not in result.stderr


@pytest.mark.parametrize("failure", ["unreadable", "cache", "unknown", "sudo"])
def test_replay_operational_failures(tmp_path, failure):
    replay = ReplayHarness(tmp_path)
    if failure == "unreadable":
        replay.list.mkdir()
    else:
        replay.list.write_text("jq\n")
    if failure == "cache":
        replay.tools.configure(cache_status=42)
    elif failure == "unknown":
        replay.tools.configure(cache={"jq": "jqp\n"})
    elif failure == "sudo":
        replay.tools.configure(sudo_status=1)
    result = replay.run()
    assert result.returncode == 0 and "[warn]" in result.stderr
    assert all("install" not in call for call in replay.tools.sudo_calls())
    if failure == "unreadable":
        assert "none recorded" in result.stderr and str(replay.list) in result.stderr
        assert replay.tools.events() == []
    elif failure == "sudo":
        assert "sudo: a password is required" in result.stderr
        assert "status 1" in result.stderr
    elif failure == "cache":
        assert "apt-cache failed" in result.stderr
    else:
        assert "Unknown package jq" in result.stderr and str(replay.list) in result.stderr


@pytest.mark.parametrize("library", ["missing", "broken"])
def test_replay_plain_fallback(tmp_path, library):
    replay = ReplayHarness(tmp_path)
    path = tmp_path / "output.sh"
    if library == "broken":
        path.write_text("return 1\n")
    replay.env["OUTPUT_LIB"] = str(path)
    result = replay.run()
    assert result.returncode == 0 and result.stderr == "[info] none recorded\n"


def test_replay_fixed_targets(tmp_path):
    replay = ReplayHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.env.update(
        dict.fromkeys(
            [
                "APT_HELPER",
                "HELPER_PATH",
                "SUDO_TARGET",
                "helper_path",
                "sudo_target",
                "list_path",
                "dpkg_bin",
                "cache_bin",
            ],
            "/hostile",
        )
    )
    assert replay.run().returncode == 0
    assert replay.tools.sudo_calls()[-1] == [
        "-n",
        "-u",
        "root",
        "--",
        HELPER,
        "-qq",
        "install",
        "jq",
    ]


def fast_package_checks(replay):
    # Shell fakes keep the timing bound about replay work, not Python startup.
    log = shlex.quote(str(replay.tools.log))
    for role in ("dpkg", "cache"):
        executable(
            replay.tools.directory / role,
            "#!/bin/sh\n"
            f"printf '{{\"kind\":\"{role}\",\"argv\":["
            + ",".join(['\"%s\"'] * (4 if role == "dpkg" else 3))
            + f"]}}\\n' \"$@\" >> {log}\n"
            + (
                "exit 1\n" if role == "dpkg" else "printf '%s\\n' \"$3\"\n"
            ),
        )


def test_replay_space_list_at_cap_is_prompt(tmp_path):
    replay = ReplayHarness(tmp_path)
    replay.list.write_bytes(b" " * 1048576)
    started = time.monotonic()
    result = replay.run()
    elapsed = time.monotonic() - started
    print(f"1 MiB spaces: {elapsed:.3f} s")
    assert elapsed < 3
    assert result.returncode == 0 and "none recorded" in result.stderr
    assert "[warn]" not in result.stderr
    assert replay.tools.events() == [] and replay.tools.sudo_calls() == []


@pytest.mark.parametrize("count", [256, 257, 1048576 // 11])
def test_replay_distinct_name_cap(tmp_path, count):
    replay = ReplayHarness(tmp_path)
    fast_package_checks(replay)
    names = [f"pkg{index:07d}" for index in range(count)]
    # Repeating the first overflow name must not count it twice.
    entries = names + [names[0], names[-1]] if count == 257 else names
    replay.list.write_text("\n".join(entries) + "\n")
    started = time.monotonic()
    result = replay.run()
    elapsed = time.monotonic() - started
    print(f"{replay.list.stat().st_size} bytes / {count} distinct names: {elapsed:.3f} s")
    assert elapsed < 3
    assert result.returncode == 0
    expected = names[:256]
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == expected
    assert [event["argv"][-1] for event in replay.tools.events("cache")] == expected
    assert len(replay.tools.sudo_calls()) == 2
    assert replay.tools.sudo_calls()[-1][6:] == ["install", *expected]
    assert "installing 256 of 256" in result.stderr
    if count > 256:
        assert (
            f"More than 256 package names in {replay.list}; {count - 256} not replayed."
            in result.stderr
        )
        assert result.stderr.count("not replayed") == 1
    else:
        assert "not replayed" not in result.stderr


def test_replay_name_byte_limit(tmp_path):
    replay = ReplayHarness(tmp_path)
    accepted = "a" * 128
    replay.list.write_text(f"{accepted}\n{'a' * 129}\n")
    result = replay.run()
    assert result.returncode == 0
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == [accepted]
    assert replay.tools.sudo_calls()[-1][-2:] == ["install", accepted]
    assert result.stderr.count("Invalid package name") == 1
    assert f'"{"a" * 32}..." (129 bytes) in {replay.list} (line 2)' in result.stderr


def test_replay_invalid_diagnostics_are_bounded(tmp_path):
    replay = ReplayHarness(tmp_path)
    invalid = [b"j\x00q", b"\x1b" + b"A" * 31, b"A" * 33, b"\xff\x7f\x01", b"bad name"]
    replay.list.write_bytes(
        b"# heading\n\n" + b"\n".join(invalid + [b"JQ", b"JQ", b"foo.deb"]) + b"\njq\n"
    )
    replay.tools.configure(states={"jq": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0
    warnings = [line for line in result.stderr.splitlines() if "Invalid package name" in line]
    assert len(warnings) == 5
    shown = ["j?q", "?" + "A" * 31, "A" * 32 + "...", "???", "bad name"]
    for number, (line, display, entry) in enumerate(
        zip(warnings, shown, invalid, strict=True), start=3
    ):
        assert (
            f'Invalid package name "{display}" ({len(entry)} bytes) in '
            f"{replay.list} (line {number}); skipped."
        ) in line
    assert result.stderr.count("[warn]") == 6
    assert f"3 more invalid lines in {replay.list} skipped." in result.stderr
    assert all(32 <= ord(char) <= 126 for char in result.stderr.replace("\n", ""))
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == ["jq"]


@pytest.mark.parametrize("count", [5, 6])
def test_replay_warning_cap_boundary(tmp_path, count):
    replay = ReplayHarness(tmp_path)
    replay.list.write_text("\n".join([f"Bad{index}" for index in range(count)] + ["jq"]))
    replay.tools.configure(states={"jq": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0 and result.stderr.count("Invalid package name") == 5
    assert result.stderr.count("[warn]") == (1 if count > 5 else 0) + 5
    if count == 6:
        assert f"1 more invalid lines in {replay.list} skipped." in result.stderr
    else:
        assert "more invalid lines" not in result.stderr


@pytest.mark.parametrize("tool", ["tr", "awk"])
@pytest.mark.parametrize("partial", [False, True])
def test_replay_conversion_failures_skip_partial_input(tmp_path, tool, partial):
    replay = ReplayHarness(tmp_path)
    replay.list.write_text("jq\n")
    output = ("jq\\n" if tool == "tr" else "V\\tjq\\n") if partial else ""
    executable(tmp_path / tool, f"#!/bin/sh\nprintf '{output}'\nexit 42\n")
    result = replay.run()
    assert result.returncode == 0 and "[warn]" in result.stderr
    assert str(replay.list) in result.stderr
    assert replay.tools.events() == [] and replay.tools.sudo_calls() == []


@pytest.mark.parametrize("budget", ["0", "1"])
@pytest.mark.parametrize("stage", ["dpkg", "cache"])
def test_replay_check_budget(tmp_path, budget, stage):
    replay = ReplayHarness(tmp_path)
    names = [f"pkg{index}" for index in range(12)]
    replay.list.write_text("\n".join(names) + "\n")
    replay.script.write_text(substitute(replay.script.read_text(), "check_budget", "60", budget))
    replay.tools.configure(**{stage + "_delay": 0.2})
    if stage == "dpkg":
        # An installed result must still be subject to the budget before continue.
        replay.tools.configure(states={name: ["installed", 0] for name in names})
    started = time.monotonic()
    result = replay.run()
    assert time.monotonic() - started < 3
    assert result.returncode == 0
    assert result.stderr.count(f"Package checks exceeded {budget} s; replay skipped.") == 1
    assert 0 < len(replay.tools.events(stage)) < len(names)
    assert replay.tools.sudo_calls() == (
        [] if stage == "dpkg" else [["-n", "-u", "root", "--", HELPER, "-qq", "update"]]
    )


@pytest.mark.parametrize("stage", ["dpkg", "cache"])
def test_replay_budget_covers_final_check(tmp_path, stage):
    replay = ReplayHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.script.write_text(substitute(replay.script.read_text(), "check_budget", "60", "0"))
    replay.tools.configure(**{stage + "_delay": 1.1})
    result = replay.run()
    assert result.returncode == 0
    assert "Package checks exceeded 0 s; replay skipped." in result.stderr
    assert len(replay.tools.events(stage)) == 1
    assert len(replay.tools.sudo_calls()) == (0 if stage == "dpkg" else 1)


def test_replay_zero_budget_allows_zero_elapsed(tmp_path):
    replay = ReplayHarness(tmp_path)
    fast_package_checks(replay)
    replay.list.write_text("jq\n")
    replay.script.write_text(substitute(replay.script.read_text(), "check_budget", "60", "0"))
    # Freeze the elapsed clock at its boundary while preserving the comparison.
    source = replay.script.read_text().replace("checks_started=$SECONDS", "checks_started=0")
    source = source.replace(
        "if (( SECONDS - checks_started ", "SECONDS=0\n    if (( SECONDS - checks_started "
    )
    replay.script.write_text(source)
    result = replay.run()
    assert result.returncode == 0 and "Package checks exceeded" not in result.stderr
    assert replay.tools.sudo_calls()[-1][-2:] == ["install", "jq"]


def test_replay_slow_update_does_not_spend_check_budget(tmp_path):
    # After a recreation the update can take minutes; the cache checks still run.
    replay = ReplayHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.script.write_text(substitute(replay.script.read_text(), "check_budget", "60", "1"))
    replay.tools.configure(update_delay=2.2)
    result = replay.run()
    assert result.returncode == 0 and "Package checks exceeded" not in result.stderr
    assert replay.tools.sudo_calls()[-1][-2:] == ["install", "jq"]


def test_replay_update_timeout_kills_fake(tmp_path):
    replay = ReplayHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.script.write_text(substitute(replay.script.read_text(), "update_timeout", "300", "1"))
    # Python sleeps in the fake itself: timeout can kill it without orphan children.
    replay.tools.configure(update_delay=3)
    started = time.monotonic()
    result = replay.run()
    assert time.monotonic() - started < 2.5
    assert result.returncode == 0
    assert "Package list update timed out after 1 s; replay skipped." in result.stderr
    assert len(replay.tools.sudo_calls()) == 1
    assert replay.tools.events("cache") == []
    pid = replay.tools.events("helper")[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_replay_update_status_124(tmp_path):
    replay = ReplayHarness(tmp_path)
    replay.list.write_text("jq\n")
    replay.tools.configure(update_status=124)
    result = replay.run()
    assert result.returncode == 0
    assert "Package list update timed out after 300 s; replay skipped." in result.stderr
    assert "Package update failed" not in result.stderr
    assert len(replay.tools.sudo_calls()) == 1 and replay.tools.events("cache") == []


def test_replay_install_has_no_timeout():
    source = (ROOT / "scripts/package-replay.sh").read_text()
    install = [line for line in source.splitlines() if '"$helper_path" -qq install' in line]
    assert len(install) == 1
    assert install[0].startswith('if output=$(sudo ') and "timeout" not in install[0]


@pytest.mark.skipif(not Path("/usr/bin/mawk").exists(), reason="mawk unavailable")
def test_replay_explicit_mawk(tmp_path):
    replay = ReplayHarness(tmp_path)
    executable(tmp_path / "awk", '#!/bin/sh\nexec /usr/bin/mawk "$@"\n')
    replay.list.write_bytes(b" \t\r\f\vjq\v\f\r\t # comment\njq\nfoo.deb\nj\x00q\n")
    replay.tools.configure(states={"jq": ["installed", 0]})
    result = replay.run()
    assert result.returncode == 0 and "1 recorded, all installed" in result.stderr
    assert result.stderr.count("Invalid package name") == 2
    assert [event["argv"][-1] for event in replay.tools.events("dpkg")] == ["jq"]
    assert replay.tools.sudo_calls() == [] and replay.tools.events("cache") == []
