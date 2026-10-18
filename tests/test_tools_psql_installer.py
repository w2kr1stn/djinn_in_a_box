"""Exercise psql placement and wrappers without apt or privileged operations."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "installers" / "psql.sh"
BINARIES = ("psql", "pg_dump", "pg_restore", "pg_dumpall")


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def psql_env(tmp_path: Path) -> dict[str, str]:
    # Shell metacharacters must remain literal in the installed absolute paths.
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "tools volume's $literal `name`"
    source = tmp_path / "postgresql source" / "18" / "bin"
    source.mkdir(parents=True)
    for name in BINARIES:
        write_executable(
            source / name,
            '#!/bin/sh\n'
            'if [ "${1:-}" = --version ]; then\n'
            f'    echo "{name} (PostgreSQL) 18.0"\n'
            '    exit 0\n'
            'fi\n'
            'printf "PID=%s\\nLD=%s\\n" "$$" "$LD_LIBRARY_PATH"\n'
            'printf "%s\\0" "$@"\n',
        )
    env = {
        **os.environ,
        "HOME": str(home),
        "TOOLS_DIR": str(tools),
        "TOOLS_BIN": str(tmp_path / "wrapper bin's $literal `name`"),
        "TOOLS_LIB": str(tmp_path / "library dir's $literal `name`"),
    }
    env.pop("LD_LIBRARY_PATH", None)
    return env


def source_bin(env: dict[str, str]) -> Path:
    return Path(env["HOME"]).parent / "postgresql source" / "18" / "bin"


def run_placement(
    env: dict[str, str], source: Path | None = None, *, entrypoint: str = "source"
) -> subprocess.CompletedProcess[str]:
    source = source or source_bin(env)
    if entrypoint == "main":
        # Keep the script guard and main's placement call; omit host provisioning.
        script = SCRIPT.read_text()
        start = script.index("    mkdir -p", script.index("main() {\n"))
        end = script.index('    install_psql_binaries "$PG_BIN_DIR"', start)
        main_script = Path(env["HOME"]) / "psql-main.sh"
        write_executable(
            main_script, script[:start] + '    local PG_BIN_DIR="$1"\n' + script[end:]
        )
        command = ["bash", str(main_script), str(source)]
    else:
        command = [
            "bash", "-c",
            'source "$1"; install_psql_binaries "$2"',
            "psql-test", str(SCRIPT), str(source),
        ]
    return subprocess.run(
        command,
        cwd=env["HOME"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def test_source_only_defines_installer(psql_env: dict[str, str]) -> None:
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; declare -F install_psql_binaries main', "test", str(SCRIPT)],
        env=psql_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["install_psql_binaries", "main"]
    assert result.stderr == ""
    assert not Path(psql_env["TOOLS_DIR"]).exists()
    assert not Path(psql_env["TOOLS_BIN"]).exists()
    assert not Path(psql_env["TOOLS_LIB"]).exists()


@pytest.mark.parametrize(
    "inherited", [None, "", "/existing lib:/other"], ids=["unset", "empty", "set"]
)
def test_wrappers_preserve_argv_pid_and_library_path(
    psql_env: dict[str, str], inherited: str | None
) -> None:
    installed = run_placement(psql_env)
    assert installed.returncode == 0, installed.stderr
    assert installed.stdout == "psql (PostgreSQL) 18.0\n"
    if inherited is not None:
        psql_env["LD_LIBRARY_PATH"] = inherited
    expected_lib = psql_env["TOOLS_LIB"] + (f":{inherited}" if inherited else "")
    args = ["words with spaces", "", "last", "'$HOME`literal`"]
    for name in BINARIES:
        wrapper = Path(psql_env["TOOLS_BIN"]) / name
        real = Path(psql_env["TOOLS_DIR"]) / "postgresql" / "bin" / name
        assert real.read_bytes() == (source_bin(psql_env) / name).read_bytes()
        assert stat.S_IMODE(real.stat().st_mode) == 0o755
        assert stat.S_IMODE(wrapper.stat().st_mode) == 0o755
        assert wrapper.read_text().splitlines()[0] == "#!/bin/sh"
        # Popen's PID must become the real binary's PID, proving exec semantics.
        with subprocess.Popen(
            [str(wrapper), *args],
            env=psql_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            assert stdout == (
                f"PID={process.pid}\nLD={expected_lib}\n" + "\0".join(args) + "\0"
            )


@pytest.mark.parametrize("empty", [False, True], ids=["unset", "empty"])
def test_default_cache_paths(psql_env: dict[str, str], empty: bool) -> None:
    for name in ("TOOLS_DIR", "TOOLS_BIN", "TOOLS_LIB"):
        if empty:
            psql_env[name] = ""
        else:
            psql_env.pop(name)
    result = run_placement(psql_env)
    assert result.returncode == 0, result.stderr
    cache = Path(psql_env["HOME"]) / ".cache" / "djinn-tools"
    for name in BINARIES:
        assert (cache / "bin" / name).is_file()
        assert (cache / "postgresql" / "bin" / name).is_file()
    result = subprocess.run(
        [str(cache / "bin" / "psql"), "probe"],
        env=psql_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"\nLD={cache / 'lib'}\n" in result.stdout


def test_relative_paths_are_baked_absolute(psql_env: dict[str, str]) -> None:
    for name in ("TOOLS_DIR", "TOOLS_BIN", "TOOLS_LIB"):
        psql_env[name] = f"relative {name.lower()}"
    psql_env["CDPATH"] = psql_env["HOME"]
    result = run_placement(psql_env)
    assert result.returncode == 0, result.stderr
    home = Path(psql_env["HOME"])
    wrapper = home / psql_env["TOOLS_BIN"] / "psql"
    child = subprocess.run(
        [str(wrapper), "probe"],
        cwd="/",
        env=psql_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.split("\n", 1)[1] == f"LD={home / psql_env['TOOLS_LIB']}\nprobe\0"


def test_final_verification_failure_is_returned(psql_env: dict[str, str]) -> None:
    write_executable(
        source_bin(psql_env) / "psql",
        '#!/bin/sh\necho "psql: error: connection to server failed" >&2\nexit 23\n',
    )
    result = run_placement(psql_env)
    assert result.returncode == 23, result.stdout + result.stderr
    assert "psql: error: connection to server failed" in result.stderr


def test_missing_source_directory_fails(psql_env: dict[str, str], tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    result = run_placement(psql_env, missing)
    assert result.returncode == 1
    assert f"Warning: PostgreSQL bin directory not found at {missing}" in result.stdout


@pytest.mark.parametrize("missing", BINARIES)
def test_missing_binary_prevents_all_publication(
    psql_env: dict[str, str], missing: str
) -> None:
    (source_bin(psql_env) / missing).unlink()
    result = run_placement(psql_env)
    assert result.returncode != 0, result.stdout + result.stderr
    assert result.stderr.splitlines() == [
        f"Error: PostgreSQL binary not found at {source_bin(psql_env) / missing}"
    ]
    for name in BINARIES:
        assert not (Path(psql_env["TOOLS_BIN"]) / name).exists()
        assert not (Path(psql_env["TOOLS_DIR"]) / "postgresql/bin" / name).exists()


@pytest.mark.parametrize("name", BINARIES[1:])
def test_sibling_verification_failure_is_returned(
    psql_env: dict[str, str], name: str
) -> None:
    error = (
        f"{name}: error while loading shared libraries: libpq.so.5: "
        "cannot open shared object file: No such file or directory"
    )
    write_executable(
        source_bin(psql_env) / name,
        f"#!/bin/sh\necho {shlex.quote(error)} >&2\nexit 127\n",
    )
    result = run_placement(psql_env)
    assert result.returncode == 127, result.stdout + result.stderr
    assert error in result.stderr


def test_verification_runs_psql_last_and_keeps_its_version_on_stdout(
    psql_env: dict[str, str], tmp_path: Path
) -> None:
    trace = tmp_path / "versions"
    for name in BINARIES:
        write_executable(
            source_bin(psql_env) / name,
            '#!/bin/sh\n[ "$1" = --version ] || exit 2\n'
            f"echo {name} >> {shlex.quote(str(trace))}\n"
            f'echo "{name} (PostgreSQL) 18.0"\n',
        )
    result = run_placement(psql_env)
    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [*BINARIES[1:], "psql"]
    assert result.stdout == "psql (PostgreSQL) 18.0\n"


@pytest.mark.parametrize("side", ["wrapper", "real"])
def test_directory_destination_fails_without_moving_stage_inside(
    psql_env: dict[str, str], side: str
) -> None:
    directory = (
        Path(psql_env["TOOLS_BIN"])
        if side == "wrapper"
        else Path(psql_env["TOOLS_DIR"]) / "postgresql/bin"
    )
    target = directory / "pg_dump"
    target.mkdir(parents=True)
    result = run_placement(psql_env)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "mv: cannot overwrite directory" in result.stderr
    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert list(directory.glob(".pg_dump.*")) == []


@pytest.mark.parametrize("entrypoint", ["source", "main"])
@pytest.mark.parametrize(
    ("tool", "side"),
    [
        ("mktemp", "real"), ("mktemp", "wrapper"), ("cp", "real"),
        ("chmod", "real"), ("chmod", "wrapper"), ("cat", "wrapper"),
        ("mv", "real"), ("mv", "wrapper"),
    ],
    ids=[
        "mktemp-real", "mktemp-wrapper", "cp-real", "chmod-real",
        "chmod-wrapper", "cat-wrapper", "mv-real", "mv-wrapper",
    ],
)
def test_failed_stage_is_cleaned_on_exit(
    psql_env: dict[str, str], tmp_path: Path, entrypoint: str, tool: str, side: str
) -> None:
    wrapper_dir = Path(psql_env["TOOLS_BIN"])
    real_dir = Path(psql_env["TOOLS_DIR"]) / "postgresql/bin"
    previous_stages = []
    for directory in (wrapper_dir, real_dir):
        directory.mkdir(parents=True)
        previous = directory / ".pg_dump.previous"
        previous.write_text("other invocation\n")
        previous_stages.append(previous)
    fail_dir = wrapper_dir if side == "wrapper" else real_dir
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    write_executable(
        fake_bin / tool,
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        f"tool = {tool!r}\n"
        "stage = (os.readlink('/proc/self/fd/1') if tool == 'cat' "
        "else sys.argv[-2] if tool == 'mv' else sys.argv[-1])\n"
        f"if pathlib.Path(stage).parent == pathlib.Path({str(fail_dir)!r}):\n"
        "    messages = {\n"
        "        'mktemp': f\"mktemp: failed to create file via template '{stage}': "
        "No space left on device\",\n"
        "        'cp': f\"cp: error writing '{stage}': No space left on device\",\n"
        "        'chmod': f\"chmod: changing permissions of '{stage}': "
        "Operation not permitted\",\n"
        "        'cat': 'cat: write error: No space left on device',\n"
        "        'mv': f\"mv: cannot move '{stage}' to '{sys.argv[-1]}': "
        "Permission denied\",\n"
        "    }\n"
        "    print(messages[tool], file=sys.stderr)\n"
        "    sys.exit(41)\n"
        "os.execv('/usr/bin/' + tool, [tool, *sys.argv[1:]])\n",
    )
    psql_env["PATH"] = f"{fake_bin}:{psql_env['PATH']}"
    result = run_placement(psql_env, entrypoint=entrypoint)
    assert result.returncode == 41, result.stdout + result.stderr
    assert result.stderr.startswith(f"{tool}:")
    for directory, previous in zip((wrapper_dir, real_dir), previous_stages, strict=True):
        assert previous.read_text() == "other invocation\n"
        stages = [*directory.rglob(".psql.*"), *directory.rglob(".pg_*.*")]
        assert stages == [previous]


def test_success_clears_stage_tracking_and_exit_trap(psql_env: dict[str, str]) -> None:
    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"\n'
            'mktemp() {\n'
            '    [[ -z "$psql_staged" ]] || return 43\n'
            '    command mktemp "$@"\n'
            '}\n'
            'install_psql_binaries "$2"\n'
            '[[ -z "$psql_staged" ]] || exit 44\n'
            'trap -p EXIT\n',
            "psql-test", str(SCRIPT), str(source_bin(psql_env)),
        ],
        cwd=psql_env["HOME"],
        env=psql_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "psql (PostgreSQL) 18.0\n"


def test_replacements_are_staged_executable_and_renamed(
    psql_env: dict[str, str], tmp_path: Path
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    trace = tmp_path / "renames.jsonl"
    # Observe the publication boundary, including permissions before each rename.
    write_executable(
        fake_bin / "mv",
        f"#!{sys.executable}\n"
        "import json, os, pathlib, stat, sys\n"
        "source, target = map(pathlib.Path, sys.argv[-2:])\n"
        f"with open({str(trace)!r}, 'a') as log:\n"
        "    log.write(json.dumps([sys.argv[1:-2], str(source), str(target), "
        "stat.S_IMODE(source.stat().st_mode)]) + '\\n')\n"
        "os.replace(source, target)\n",
    )
    psql_env["PATH"] = f"{fake_bin}:{psql_env['PATH']}"
    old_files: list[tuple[Path, bytes]] = []
    for directory in (
        Path(psql_env["TOOLS_BIN"]),
        Path(psql_env["TOOLS_DIR"]) / "postgresql/bin",
    ):
        directory.mkdir(parents=True)
        for name in BINARIES:
            target = directory / name
            write_executable(target, "#!/bin/sh\necho old\n")
            backup = directory / f"{name}.old"
            os.link(target, backup)
            old_files.append((backup, backup.read_bytes()))
    result = run_placement(psql_env)
    assert result.returncode == 0, result.stderr
    renames = [json.loads(line) for line in trace.read_text().splitlines()]
    expected_targets = [
        str(directory / name)
        for name in BINARIES
        for directory in (
            Path(psql_env["TOOLS_DIR"]) / "postgresql/bin",
            Path(psql_env["TOOLS_BIN"]),
        )
    ]
    assert [entry[2] for entry in renames] == expected_targets
    for flags, source, target, mode in renames:
        assert flags == ["-fT"]
        assert Path(source).parent == Path(target).parent
        assert source != target
        assert mode == 0o755
        assert not Path(source).exists()
    for backup, original in old_files:
        assert backup.read_bytes() == original
