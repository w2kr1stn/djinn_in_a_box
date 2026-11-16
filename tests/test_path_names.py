"""Check the fixed names command through isolated subprocesses and a fake SSH."""

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "djinn-path-names.py"


@pytest.fixture
def ssh(tmp_path: Path) -> tuple[dict[str, str], Path]:
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh/tailnet_config").write_text("Host testhost\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorded = tmp_path / "argv.json"
    fake = bin_dir / "ssh"
    fake.write_text(
        f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
        f"Path({str(recorded)!r}).write_text(json.dumps(sys.argv))\nraise SystemExit(17)\n"
    )
    fake.chmod(0o755)
    return {
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "DJINN_DENIED_PATHS": '["/srv/private"]',
    }, recorded


@pytest.mark.parametrize(
    ("path", "quoted", "depth"),
    [
        ("/srv/private", "/srv/private", None),
        ("/srv/private/it's a dir", """'/srv/private/it'"'"'s a dir'""", "3"),
    ],
)
def test_ssh_argv_and_status(ssh: tuple[dict[str, str], Path], path, quoted, depth) -> None:
    env, recorded = ssh
    args = ["testhost", "alice", path] + ([] if depth is None else [depth])
    result = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True)
    remote = (
        f"cd /tmp && sudo -n -u alice find -- {quoted} "
        f"-maxdepth {depth or '1'} -printf '%y %s %T+ %p\\n'"
    )
    assert result.returncode == 17
    assert result.stdout == result.stderr == b""
    argv = json.loads(recorded.read_text())
    assert argv == [str(Path(env["PATH"].split(":")[0]) / "ssh"), "testhost", remote]
    assert shlex.split(argv[2])[9:12] == [path, "-maxdepth", depth or "1"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("args", ["testhost", "alice"]),
        ("args", ["testhost", "alice", "/srv/private", "1", "extra"]),
        (0, "unknown"),
        ("hostfile", None),
        (1, "Alice"),
        (1, "a;b"),
        (2, "srv/private"),
        (2, "/srv/private/../x"),
        (2, "/srv/private/"),
        (2, "/srv/privateX"),
        (2, "/srv"),
        *[(2, "/srv/private/" + chr(code)) for code in [*range(1, 32), 127]],
        (3, "6"),
        (3, "-1"),
        (3, "x"),
        *[("configured", value) for value in ["invalid", "{}", "[1]", "null", "", "[]", None]],
    ],
)
def test_invalid_input(ssh: tuple[dict[str, str], Path], field, value) -> None:
    env, recorded = ssh
    args = ["testhost", "alice", "/srv/private", "1"]
    if field == "args":
        args = value
    elif field == "hostfile":
        (Path(env["HOME"]) / ".ssh/tailnet_config").unlink()
    elif field == "configured":
        if value is None:
            env.pop("DJINN_DENIED_PATHS")
        else:
            env["DJINN_DENIED_PATHS"] = value
    else:
        args[field] = value
    result = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr.startswith(b"djinn-path-names: ")
    assert len(result.stderr.splitlines()) == 1
    assert not recorded.exists()
    if field == "args":
        assert (
            result.stderr == b"djinn-path-names: usage: djinn-path-names HOST USER PATH [DEPTH]\n"
        )
    if field == "configured" and value in ("", "[]", None):
        assert result.stderr == b"djinn-path-names: PATH is not below a denied path\n"
