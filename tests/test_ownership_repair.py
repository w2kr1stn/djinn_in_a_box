"""Exercise the shipped helper and entrypoint without privileged changes."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from djinn_in_a_box.core.docker import MANAGED_VOLUME_REPAIR_TARGETS

ROOT = Path(__file__).resolve().parents[1]


def helper_module():
    spec = importlib.util.spec_from_file_location(
        "ownership_repair", ROOT / "scripts/ownership-repair.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "populated",
        "dotfile",
        "writable",
        "identity",
        "unreadable-empty",
        "unreadable-populated",
        "transport",
        "relative",
        "split",
        "privileged-failure",
    ],
)
def test_declared_volume_ownership(tmp_path, monkeypatch, capsys, case):
    helper = helper_module()
    root = tmp_path / "space\n dollar$root"
    root.mkdir()
    if case in ("populated", "unreadable-populated"):
        (root / "sentinel").write_text("untouched")
    if case == "dotfile":
        (root / ".hidden").touch()
    encoded = json.dumps([str(root)])
    if case == "transport":
        encoded = "{broken"
    elif case == "relative":
        encoded = json.dumps([str(root), "relative"])
    accesses = []

    def access(path, mode):
        accesses.append((path, mode, os.geteuid(), os.getgroups()))
        return case == "writable"

    monkeypatch.setattr(helper.os, "access", access)
    real_scandir = helper.os.scandir
    if case.startswith("unreadable"):

        def unreadable(path):
            raise PermissionError("cannot enumerate")

        monkeypatch.setattr(helper.os, "scandir", unreadable)
    calls = []

    def privileged(cmd, **kwargs):
        calls.append(cmd)
        assert cmd[:2] in (["sudo", "ls"], ["sudo", "chown"])
        if case == "privileged-failure":
            raise subprocess.CalledProcessError(1, cmd)
        return MagicMock(stdout=b"sentinel\n" if case == "unreadable-populated" else b"")

    monkeypatch.setattr(helper.subprocess, "run", privileged)
    if case in ("transport", "relative"):
        with pytest.raises(ValueError, match="DJINN_DECLARED_VOLUME_TARGETS"):
            helper.repair_targets(encoded)
        assert accesses == [] and calls == []
    elif case == "privileged-failure":
        with pytest.raises(subprocess.CalledProcessError):
            helper.repair_targets(encoded)
    else:
        helper.repair_targets(encoded)
        changes = [cmd for cmd in calls if cmd[1] == "chown"]
        if case in ("empty", "identity", "unreadable-empty", "split"):
            assert changes == [
                ["sudo", "chown", "-h", f"{os.getuid()}:{os.getgid()}", "--", str(root)]
            ]
        else:
            assert changes == []
        if "populated" in case or case == "dotfile":
            assert str(root) in capsys.readouterr().err
        if case.startswith("unreadable"):
            assert calls[0] == ["sudo", "ls", "-A", "--", str(root)]
        assert accesses[0][2:] == (os.geteuid(), os.getgroups())
    monkeypatch.setattr(helper.os, "scandir", real_scandir)
    if (root / "sentinel").exists():
        assert (root / "sentinel").read_text() == "untouched"


def test_managed_volume_repair_targets_match_entrypoint():
    import re

    entrypoint = (ROOT / "scripts/entrypoint.sh").read_text()
    match = re.search(r"^for dir in (.+); do$", entrypoint, re.M)
    targets = tuple(Path(path.replace("~", "/home/dev", 1)) for path in match[1].split())
    assert targets == MANAGED_VOLUME_REPAIR_TARGETS


@pytest.mark.parametrize("case", ["success", "failure", "unreadable"])
def test_entrypoint_invokes_ownership_repair(tmp_path, case):
    helper = ROOT / "scripts/ownership-repair.py"
    text = (ROOT / "scripts/entrypoint.sh").read_text()
    section = text[text.index("# Fix ownership") : text.index("# Git Configuration")]
    assert "sudo python3" not in section
    log = tmp_path / "sudo.log"
    stub = tmp_path / "sudo"
    stub.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        'with open(os.environ["REPAIR_LOG"], "a") as handle:\n'
        '    handle.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        'fail = os.environ["REPAIR_CASE"] == "failure" and sys.argv[1:3] == ["chown", "-h"]\n'
        "sys.exit(9 if fail else 0)\n"
    )
    stub.chmod(0o755)
    root = tmp_path / "LF\n spaces$dollar"
    root.mkdir()
    root.chmod(0o000 if case == "unreadable" else 0o500)
    fixed = tmp_path / ".cache/uv"
    fixed.mkdir(parents=True)
    fixed.chmod(0o500)
    script = tmp_path / "harness.zsh"
    script.write_text("set -euo pipefail\n" + section + "\nprint FOLLOWING_STARTUP_STEP\n")
    result = subprocess.run(
        ["zsh", str(script)],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": str(tmp_path) + ":" + os.environ["PATH"],
            "OWNERSHIP_REPAIR_HELPER": str(helper),
            "DJINN_DECLARED_VOLUME_TARGETS": json.dumps([str(root)]),
            "REPAIR_LOG": str(log),
            "REPAIR_CASE": case,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    root.chmod(0o700)
    fixed.chmod(0o700)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0][0:2] == ["chown", "-R"] and calls[0][-1] == str(fixed)
    if case == "failure":
        assert result.returncode == 1 and "FOLLOWING_STARTUP_STEP" not in result.stdout
        assert "ownership repair failed" in result.stderr and repr(str(root)) in result.stderr
        assert calls[-1] == ["chown", "-h", f"{os.getuid()}:{os.getgid()}", "--", str(root)]
    else:
        assert result.returncode == 0, result.stderr
        assert "FOLLOWING_STARTUP_STEP" in result.stdout
        changes = [cmd for cmd in calls[1:] if cmd[0] == "chown"]
        assert changes == [["chown", "-h", f"{os.getuid()}:{os.getgid()}", "--", str(root)]]
        if case == "unreadable":
            assert calls[1] == ["ls", "-A", "--", str(root)]
    assert section.index("done") < section.index("python3")
    assert (
        'python3 "$OWNERSHIP_REPAIR_HELPER" --targets "${DJINN_DECLARED_VOLUME_TARGETS:-[]}"'
        in section
    )
