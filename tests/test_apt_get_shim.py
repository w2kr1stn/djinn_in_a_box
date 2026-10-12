from __future__ import annotations

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
        source = (ROOT / "scripts/apt-get-shim.sh").read_text()
        for name, old, new in (
            ("id_bin", "/usr/bin/id", directory / "id"),
            ("real_apt", "/usr/bin/apt-get", directory / "apt"),
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
    args = ["install", "spaced name", "", "*", "--", "-o", "--help", "-h"]
    shim.env["LC_ALL"] = "C.UTF-8"
    shim.env["APT_CONFIG"] = "preserved"
    result = shim.run(args)
    assert result.returncode == status
    assert result.stdout == ("out\n" if streams else "")
    assert result.stderr == ("err\n" if streams else "")
    assert shim.tools.sudo_calls() == []
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
        "at most 128 bytes",
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
        "Agents cannot edit it",
        "root helper records",
        "sudo sed -i '/^NAME$/d'",
        "/usr/bin/apt-get",
        "unprivileged",
        'README section "Runtime Packages"',
    ):
        assert text in result.stdout
    assert shim.tools.events() == []


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
    result = shim.run(args)
    assert result.returncode == 2 and result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    assert (
        "apt-get --help" in result.stderr and 'README section "Runtime Packages"' in result.stderr
    )
    assert not any(ord(char) < 32 for char in result.stderr.rstrip("\n"))
    assert "using sudo" not in result.stderr and shim.tools.events() == []


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
    if not known:
        assert f"E: Unable to locate package {name}" in result.stderr
        assert "apt-get update" in result.stderr
        assert helper.tools.events("apt") == []


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
    assert shim.tools.sudo_calls() == []


@pytest.mark.parametrize("length", [128, 129])
def test_dev_name_byte_limit(tmp_path, length):
    shim = ShimHarness(tmp_path)
    name = "a" * length
    result = shim.run(["install", name])
    if length == 128:
        assert result.returncode == 0
        assert shim.tools.sudo_calls() == [["-n", "-u", "root", "--", HELPER, "install", name]]
    else:
        assert result.returncode == 2 and len(result.stderr.splitlines()) == 1
        assert "package name longer than 128 bytes (129-byte argument)" in result.stderr
        assert shim.tools.events() == [] and shim.tools.sudo_calls() == []


def test_shim_has_no_list_access():
    source = (ROOT / "scripts/apt-get-shim.sh").read_text()
    # The public help names the list; executable code has no list path or recorder.
    before, help_and_after = source.split("cat <<'HELP'", 1)
    _, after = help_and_after.split("\nHELP\n", 1)
    code = before + after
    for removed in (
        "list_path",
        "record_packages",
        "packages.txt",
        "djinn-packages",
        "tail ",
        "tr ",
    ):
        assert removed not in code


@pytest.mark.parametrize("size", [64, 65])
def test_shim_refusal_display_boundary(tmp_path, size):
    result = ShimHarness(tmp_path).run(["install", "A" * size])
    assert result.returncode == 2
    assert (f"{size}-byte argument" in result.stderr) == (size > 64)
    assert ("A" * size in result.stderr) == (size <= 64)
