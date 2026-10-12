from __future__ import annotations

import contextlib
import os
import subprocess
import time

import pytest
from test_djinn_apt_root import (
    BAD_FLAGS,
    BAD_NAMES,
    BAD_OPERATIONS,
    HELPER,
    LIST,
    ROOT,
    FakeTools,
    HelperHarness,
    executable,
    substitute,
)


class ShimHarness:
    def __init__(self, directory):
        self.tools = FakeTools(directory)
        self.tools.configure(uid="1000")
        self.list = directory / "packages.txt"
        source = (ROOT / "scripts/apt-get-shim.sh").read_text()
        for name, old, new in (
            ("id_bin", "/usr/bin/id", directory / "id"),
            ("real_apt", "/usr/bin/apt-get", directory / "apt"),
            ("list_path", LIST, self.list),
        ):
            source = substitute(source, name, old, new)
        self.script = executable(directory / "shim.sh", source)
        self.env = dict(os.environ, PATH=f"{directory}:/usr/bin:/bin")

    def run(self, args, **kwargs):
        return subprocess.run(
            ["/bin/sh", str(self.script), *args],
            capture_output=True,
            text=True,
            input="caller input",
            env=self.env,
            timeout=5,
            check=False,
            **kwargs,
        )


@pytest.mark.parametrize("status", [0, 17, 100])
@pytest.mark.parametrize("streams", [False, True])
def test_root_passthrough(tmp_path, status, streams):
    shim = ShimHarness(tmp_path)
    shim.tools.configure(
        uid="0", status=status, stdout="out\n" if streams else "", stderr="err\n" if streams else ""
    )
    # A directory at the list path detects accidental recording without touching home.
    shim.list.mkdir()
    args = ["install", "spaced name", "", "*", "--", "-o", "--help", "-h"]
    shim.env["LC_ALL"] = "C.UTF-8"
    shim.env["APT_CONFIG"] = "preserved"
    result = shim.run(args)
    assert result.returncode == status
    assert result.stdout == ("out\n" if streams else "")
    assert result.stderr == ("err\n" if streams else "")
    assert shim.tools.sudo_calls() == [] and list(shim.list.iterdir()) == []
    event = shim.tools.events("apt")[0]
    assert event["argv"] == args and event["stdin"] == "caller input"
    assert event["env"]["LC_ALL"] == "C.UTF-8"
    assert event["env"]["APT_CONFIG"] == "preserved"


@pytest.mark.parametrize(
    "args",
    [
        ["-h"],
        ["--help"],
        ["nonsense", "-o", "--help", "bad/name"],
        ["install", "", "-h"],
        ["--help", "remove"],
    ],
)
def test_dev_help_anywhere(tmp_path, args):
    shim = ShimHarness(tmp_path)
    result = shim.run(args)
    assert result.returncode == 0 and result.stderr == ""
    for text in (
        "update",
        "install",
        "NAME",
        "-y",
        "--yes",
        "--assume-yes",
        "-q",
        "--quiet",
        "-qq",
        "--no-install-recommends",
        "always",
        "never remove",
        "sudo",
        "root",
        HELPER,
        LIST,
        "Deleting a line",
        "/usr/bin/apt-get",
        "unprivileged",
        'README section "Runtime Packages"',
    ):
        assert text in result.stdout
    assert not shim.list.exists() and shim.tools.events() == []


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["-y"],
        ["install"],
        ["update", "jq"],
        *[[op] for op in BAD_OPERATIONS],
        *[["install", name] for name in BAD_NAMES],
        *[[flag, "install", "jq"] for flag in BAD_FLAGS],
        *[["install", flag, "jq"] for flag in BAD_FLAGS],
        *[["install", "jq", flag] for flag in BAD_FLAGS],
        ["install", "jq", "-o", "APT::Config=x"],
    ],
)
def test_dev_refusal_matrix(tmp_path, args):
    shim = ShimHarness(tmp_path)
    shim.list.write_text("# keep\ncurl\n")
    result = shim.run(args)
    assert result.returncode == 2 and result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert (
        "apt-get --help" in result.stderr and 'README section "Runtime Packages"' in result.stderr
    )
    assert not any(ord(char) < 32 for char in result.stderr.rstrip("\n"))
    assert "using sudo" not in result.stderr and shim.tools.events() == []
    assert shim.list.read_text() == "# keep\ncurl\n"


@pytest.mark.parametrize(
    "args,forwarded",
    [
        (["update"], ["update"]),
        (["-q", "update"], ["-q", "update"]),
        (["update", "--quiet"], ["-q", "update"]),
        (["-qq", "update"], ["-qq", "update"]),
        (["-q", "update", "-q"], ["-qq", "update"]),
        (["-qq", "--quiet", "update", "-qq"], ["-qq", "update"]),
        (["install", "jq"], ["install", "jq"]),
        (
            [
                "--yes",
                "install",
                "g++",
                "--assume-yes",
                "python3.11",
                "-y",
                "--no-install-recommends",
            ],
            ["install", "g++", "python3.11"],
        ),
        (["install", "jq", "-q", "curl", "--quiet"], ["-qq", "install", "jq", "curl"]),
        (["install", "ab", "a-b", "aa0"], ["install", "ab", "a-b", "aa0"]),
    ],
)
def test_allowed_argv_and_quiet(tmp_path, args, forwarded):
    shim = ShimHarness(tmp_path)
    result = shim.run(args)
    assert result.returncode == 0
    assert shim.tools.sudo_calls() == [["-n", "-u", "root", "--", HELPER, *forwarded]]
    if "install" in forwarded:
        names = forwarded[forwarded.index("install") + 1 :]
        assert shim.list.read_text().splitlines()[1:] == names
    else:
        assert not shim.list.exists()


@pytest.mark.parametrize("name", ["bash-", "jq.", "xz.", "jq+", "g++", "python3.11"])
def test_dev_exact_name_refusal(tmp_path, name):
    shim = ShimHarness(tmp_path)
    helper = HelperHarness(tmp_path / "root-fakes")
    known = name in ("g++", "python3.11")
    helper.tools.configure(cache={name: name + ("\n" if known else "extra\n")})
    shim.tools.configure(dispatch=str(helper.script))
    result = shim.run(["install", name])
    assert result.returncode == (0 if known else 100)
    assert result.stderr.count("using sudo") == 1
    if known:
        assert shim.list.read_text().splitlines()[1:] == [name]
    else:
        assert f"E: Unable to locate package {name}" in result.stderr
        assert "apt-get update" in result.stderr
        assert not shim.list.exists() and helper.tools.events("apt") == []


@pytest.mark.parametrize("status", [0, 1, 100])
def test_announcement_each_elevation(tmp_path, status):
    shim = ShimHarness(tmp_path)
    shim.tools.configure(sudo_marker=True, sudo_status=status if status == 1 else 0, status=status)
    for operation in ("update", "install", "update"):
        result = shim.run([operation, *(["jq"] if operation == "install" else [])])
        assert result.returncode == status
        lines = result.stderr.splitlines()
        assert lines[0] == f"apt-get: using sudo to run {HELPER} as root."
        assert lines[1] == "FAKE_SUDO"
        assert result.stderr.count("using sudo") == 1
        if status == 1:
            assert "sudo: a password is required" in result.stderr


def test_fixed_targets_ignore_environment(tmp_path):
    shim = ShimHarness(tmp_path)
    shim.env.update(
        dict.fromkeys(
            ["APT_HELPER", "HELPER_PATH", "SUDO_TARGET", "helper_path", "sudo_target", "list_path"],
            "/hostile",
        )
    )
    result = shim.run(["install", "jq"])
    assert result.returncode == 0
    assert shim.tools.sudo_calls() == [["-n", "-u", "root", "--", HELPER, "install", "jq"]]
    assert shim.list.read_text().endswith("\njq\n")


@pytest.mark.parametrize("status", [0, 17, 100])
@pytest.mark.parametrize("operation", ["update", "install"])
def test_success_only_recording(tmp_path, status, operation):
    shim = ShimHarness(tmp_path)
    shim.tools.configure(status=status)
    # The fake inspects the list at dispatch time to catch premature recording.
    sudo = tmp_path / "sudo"
    source = sudo.read_text().replace(
        'if role == "sudo":',
        f'if role == "sudo":\n    assert not Path({str(shim.list)!r}).exists()',
    )
    sudo.write_text(source)
    args = [operation, *(["jq"] if operation == "install" else [])]
    assert shim.run(args).returncode == status
    assert shim.list.exists() == (status == 0 and operation == "install")
    if status:
        shim.list.write_text("# unchanged\ncurl\n")
        sudo.write_text(
            source.replace(
                f"assert not Path({str(shim.list)!r}).exists()",
                "pass",
            )
        )
        assert shim.run(args).returncode == status
        assert shim.list.read_text() == "# unchanged\ncurl\n"


@pytest.mark.parametrize(
    "initial,existing_names",
    [
        (None, []),
        ("", []),
        ("# retained\n\n  jq # inline\n\tcurl\t\n", ["jq", "curl"]),
        ("# retained\ncurl", ["curl"]),
        ("# retained\njqp\n", ["jqp"]),
    ],
)
def test_record_header_and_dedupe(tmp_path, initial, existing_names):
    shim = ShimHarness(tmp_path)
    if initial is not None:
        shim.list.write_text(initial)
    assert shim.run(["install", "jq", "jq", "g++"]).returncode == 0
    first = shim.list.read_text()
    assert shim.run(["install", "jq", "python3.11", "g++"]).returncode == 0
    result = shim.list.read_text()
    assert result.startswith(first) and result.endswith("python3.11\n")
    if initial:
        assert result.startswith(initial)
    assert sum(line.startswith("#") for line in result.splitlines()) == 1
    names = [line.split("#", 1)[0].strip() for line in result.splitlines()]
    names = [name for name in names if name]
    assert names == list(dict.fromkeys([*existing_names, "jq", "g++", "python3.11"]))


@pytest.mark.parametrize("failure", ["missing-directory", "directory", "read", "tail", "append"])
def test_record_failure_preserves_apt_status(tmp_path, failure):
    shim = ShimHarness(tmp_path)
    if failure == "missing-directory":
        new_list = tmp_path / "missing" / "packages.txt"
        shim.script.write_text(shim.script.read_text().replace(str(shim.list), str(new_list)))
        shim.list = new_list
    elif failure == "directory":
        shim.list.mkdir()
    else:
        shim.list.write_text("# retained\ncurl\n")
        if failure in ("read", "tail"):
            executable(tmp_path / ("sed" if failure == "read" else "tail"), "#!/bin/sh\nexit 1\n")
        else:
            # Turn the test file into a directory after the read, even under root.
            executable(
                tmp_path / "sed",
                "#!/usr/bin/python3\nfrom pathlib import Path\n"
                f"p = Path({str(shim.list)!r})\nprint(p.read_text())\n"
                "p.unlink()\np.mkdir()\n",
            )
            executable(tmp_path / "tail", "#!/bin/sh\nprintf '\\n'\n")
    result = shim.run(["install", "jq"])
    assert result.returncode == 0
    assert len(result.stderr.splitlines()) == 2
    assert "install succeeded but was not recorded" in result.stderr
    assert str(shim.list) in result.stderr
    if failure == "missing-directory":
        assert not shim.list.parent.exists()


LONG = "A" * 32769


@pytest.mark.parametrize(
    "args", [[LONG], ["install", LONG], ["install", "jq", "-" + LONG[1:]], ["install", "\x01" * 65]]
)
def test_long_token_refused_promptly(tmp_path, args):
    # Per-byte escaping of a 32 KiB token took seconds; long tokens are sized instead.
    shim = ShimHarness(tmp_path)
    started = time.monotonic()
    result = shim.run(args)
    assert time.monotonic() - started < 2
    assert result.returncode == 2 and len(result.stderr.splitlines()) == 1
    assert f"({len(args[-1])}-byte argument)" in result.stderr
    assert shim.tools.sudo_calls() == [] and not shim.list.exists()


def test_record_dedupe_does_not_join_around_nul(tmp_path):
    # "j\0q" is not "jq": a substitution that drops the NUL must not hide jq.
    shim = ShimHarness(tmp_path)
    shim.list.write_bytes(b"# retained\nj\x00q\n")
    assert shim.run(["install", "jq"]).returncode == 0
    assert shim.list.read_bytes() == b"# retained\nj\x00q\njq\n"


@pytest.mark.parametrize("partial", ["", "hello\n"])
def test_record_conversion_failure_warns(tmp_path, partial):
    # A failing tr must not hand partial output to the duplicate check: "hello"
    # in a truncated read would hide that hello was never recorded.
    shim = ShimHarness(tmp_path)
    shim.list.write_text("# retained\nhello-extra\n")
    executable(tmp_path / "tr", f"#!/bin/sh\nprintf '{partial}'\nexit 42\n")
    result = shim.run(["install", "hello"])
    assert result.returncode == 0
    assert "install succeeded but was not recorded" in result.stderr
    assert shim.list.read_text() == "# retained\nhello-extra\n"


def test_record_into_fifo_warns_promptly(tmp_path):
    # Appending to a FIFO without a reader would block after a successful install.
    shim = ShimHarness(tmp_path)
    os.mkfifo(shim.list)
    try:
        result = shim.run(["install", "jq"])
    finally:
        # A regressed shim blocks opening the FIFO; a reader end releases it.
        with contextlib.suppress(OSError):
            os.close(os.open(shim.list, os.O_RDONLY | os.O_NONBLOCK))
    assert result.returncode == 0
    assert "install succeeded but was not recorded" in result.stderr
