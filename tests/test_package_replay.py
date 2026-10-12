from __future__ import annotations

import os
import subprocess

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
