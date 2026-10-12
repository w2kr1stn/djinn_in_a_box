from __future__ import annotations

import os
import subprocess
import time
from threading import Thread

import pytest
from test_djinn_apt_root import HELPER, ROOT, FakeTools, executable


class ReplayHarness:
    def __init__(self, directory):
        self.tools = FakeTools(directory)
        self.script = executable(
            directory / "replay.sh", (ROOT / "scripts/package-replay.sh").read_text()
        )
        self.env = dict(
            os.environ,
            PATH=f"{directory}:/usr/bin:/bin",
            NO_COLOR="1",
            OUTPUT_LIB=str(ROOT / "scripts/output-lib.sh"),
        )

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


def test_wrapper_announcement_tags_and_untagged_lines(tmp_path):
    replay = ReplayHarness(tmp_path)
    replay.tools.configure(
        stdout="I\tnone recorded\nW\tproblem\nuntagged\nI\t  spaces\\kept\tand tab\nlast"
    )
    result = replay.run()
    announcement = f"[info] Packages: using sudo to run {HELPER} replay as root."
    assert result.returncode == 0 and result.stdout == ""
    assert [line.removeprefix("  ") for line in result.stderr.splitlines()] == [
        announcement,
        "[info] none recorded",
        "[warn] problem",
        "[info] untagged",
        "[info]   spaces\\kept\tand tab",
        "[info] last",
    ]
    assert replay.tools.events("sudo")[0]["announcement"] == result.stderr.splitlines()[0] + "\n"
    assert replay.tools.sudo_calls() == [["-n", "-u", "root", "--", HELPER, "replay"]]


@pytest.mark.parametrize("status", [0, 2, 37, 100])
def test_wrapper_status_warns_and_always_succeeds(tmp_path, status):
    replay = ReplayHarness(tmp_path)
    replay.tools.configure(status=status)
    result = replay.run()
    assert result.returncode == 0 and result.stdout == ""
    assert result.stderr.count("[warn]") == bool(status)
    assert len(result.stderr.splitlines()) == (2 if status else 1)
    if status:
        assert f"status {status}" in result.stderr


def test_wrapper_sudo_failure(tmp_path):
    replay = ReplayHarness(tmp_path)
    replay.tools.configure(sudo_status=1)
    result = replay.run()
    assert result.returncode == 0
    assert "sudo: a password is required" in result.stderr
    assert result.stderr.count("[warn]") == 1 and "status 1" in result.stderr


@pytest.mark.parametrize("library", ["missing", "broken"])
def test_wrapper_plain_fallback(tmp_path, library):
    replay = ReplayHarness(tmp_path)
    path = tmp_path / "output.sh"
    if library == "broken":
        path.write_text("return 1\n")
    replay.env["OUTPUT_LIB"] = str(path)
    replay.tools.configure(stdout="I\tnone recorded\nW\twarning\n")
    result = replay.run()
    assert result.returncode == 0
    assert result.stderr.startswith(
        f"[info] Packages: using sudo to run {HELPER} replay as root.\n"
    )
    assert result.stderr.endswith("[info] none recorded\n[warn] warning\n")


def test_wrapper_streams_status_before_helper_finishes(tmp_path):
    replay = ReplayHarness(tmp_path)
    sleep_ended = tmp_path / "sleep-ended"
    executable(
        tmp_path / "sudo",
        "#!/usr/bin/python3\nimport time\nfrom pathlib import Path\n"
        'print("I\\tinstalling 2 of 2", flush=True)\n'
        "time.sleep(2)\n"
        f"Path({str(sleep_ended)!r}).write_text(str(time.monotonic()))\n"
        'print("I\\tfinished", flush=True)\n',
    )
    lines = []
    with subprocess.Popen(
        ["/bin/bash", str(replay.script)],
        env=replay.env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    ) as process:
        def read_stderr():
            for line in process.stderr:
                lines.append((time.monotonic(), line))

        reader = Thread(target=read_stderr)
        reader.start()
        assert process.wait(timeout=5) == 0
        reader.join(timeout=1)
        assert not reader.is_alive()
        assert process.stdout.read() == ""
    first_status = [timestamp for timestamp, line in lines if "[info] installing 2 of 2" in line]
    assert len(first_status) == 1
    assert first_status[0] < float(sleep_ended.read_text())
    assert any("[info] finished" in line for _, line in lines)


def test_wrapper_fixed_targets_and_no_list_access(tmp_path):
    replay = ReplayHarness(tmp_path)
    replay.env.update(
        dict.fromkeys(
            ["APT_HELPER", "HELPER_PATH", "SUDO_TARGET", "helper_path", "sudo_target", "list_path"],
            "/hostile",
        )
    )
    assert replay.run().returncode == 0
    assert replay.tools.sudo_calls() == [["-n", "-u", "root", "--", HELPER, "replay"]]
    source = (ROOT / "scripts/package-replay.sh").read_text()
    for removed in (
        "list_path",
        "packages.txt",
        "djinn-packages",
        "dpkg",
        "apt-cache",
        "awk",
        "head ",
        "tr ",
    ):
        assert removed not in source
