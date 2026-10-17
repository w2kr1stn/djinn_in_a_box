"""Exercise psql placement and wrappers without apt or privileged operations."""

from __future__ import annotations

import json
import os
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
            '    echo "psql (PostgreSQL) 18.0"\n'
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
    env: dict[str, str], source: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash", "-c",
            'source "$1"; install_psql_binaries "$2"',
            "psql-test", str(SCRIPT), str(source or source_bin(env)),
        ],
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
    assert f"\nLD={home / psql_env['TOOLS_LIB']}\n" in child.stdout


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


def test_missing_optional_binary_is_skipped(psql_env: dict[str, str]) -> None:
    (source_bin(psql_env) / "pg_dumpall").unlink()
    result = run_placement(psql_env)
    assert result.returncode == 0, result.stderr
    assert not (Path(psql_env["TOOLS_BIN"]) / "pg_dumpall").exists()
    assert not (Path(psql_env["TOOLS_DIR"]) / "postgresql/bin/pg_dumpall").exists()


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
        assert flags == ["-f"]
        assert Path(source).parent == Path(target).parent
        assert source != target
        assert mode == 0o755
        assert not Path(source).exists()
    for backup, original in old_files:
        assert backup.read_bytes() == original
