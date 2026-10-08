"""Offline tests of the real Bash resolver and maintainer mode."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PINS = {"CLAUDE_CODE_VERSION": "2.1.300", "CODEX_VERSION": "0.161.0",
        "OPENCODE_VERSION": "1.18.40"}
NPM_FAILURE = (
    "npm error code EAI_AGAIN\n"
    "npm error syscall getaddrinfo\n"
    "npm error request to https://registry.npmjs.org/@openai%2fcodex failed, "
    "reason: getaddrinfo EAI_AGAIN registry.npmjs.org\n"
)
NPM_MISSING_PACKAGE = (
    "npm error code E404\n"
    "npm error 404 Not Found - GET https://registry.npmjs.org/opencode-ai - Not found\n"
)


@pytest.fixture
def script_env(tmp_path):
    project = tmp_path / "repo"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    for name in ("update-agents.sh", "output-lib.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (project / "Dockerfile").write_text(
        "ARG CLAUDE_CODE_VERSION=2.1.288\nARG CODEX_VERSION=0.160.0\nARG OPENCODE_VERSION=1.18.34\n"
    )
    (project / ".gitignore").write_text("config/\npackages.txt\ntools/\n")
    (project / "config").mkdir()
    (project / "config/local").write_text("operator config\n")
    (project / "packages.txt").write_text("curl\n")
    (project / "tools").mkdir()
    (project / "tools/tools.txt").write_text("example-tool\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "npm-calls"
    npm = bindir / "npm"
    npm.write_text(
        '#!/bin/bash\n'
        'printf "%s\\n" "$*" >> "$npm_calls"\n'
        'if [[ "$1" != view || "$3" != version || "$#" != 3 ]]; then exit 2; fi\n'
        'if [[ "${npm_notice:-}" == 1 ]]; then\n'
        '  printf "npm notice New major version of npm available! 11.0.0 -> 12.0.0\\n" >&2\n'
        'fi\n'
        'case "$2" in\n'
        '  @anthropic-ai/claude-code) printf "%s\\n" "${claude_value:-2.1.300}" ;;\n'
        '  @openai/codex)\n'
        '    if [[ "${npm_failure:-}" == network ]]; then\n'
        f"      printf '%s' '{NPM_FAILURE}' >&2; exit 1\n"
        '    fi\n'
        '    printf "%s\\n" "${codex_value:-0.161.0}" ;;\n'
        '  opencode-ai)\n'
        '    if [[ "${npm_failure:-}" == missing ]]; then\n'
        f"      printf '%s' '{NPM_MISSING_PACKAGE}' >&2; exit 1\n"
        '    fi\n'
        '    printf "%s\\n" "${opencode_value:-1.18.40}" ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n'
    )
    npm.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "npm_calls": str(calls),
           "TERM": "dumb", "NO_COLOR": "1"}
    return project, env, calls


def snapshot(project):
    return {str(p.relative_to(project)): p.read_bytes() if p.is_file() else None
            for p in project.rglob("*") if ".git" not in p.relative_to(project).parts}


def run_script(project, env, *args):
    return subprocess.run(
        [str(project / "scripts/update-agents.sh"), *args], cwd=project, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False,
    )


def test_print_outputs_exact_agent_set_without_writes(script_env):
    project, env, calls = script_env
    before = snapshot(project)
    forbidden_calls = Path(env["npm_calls"]).parent / "forbidden-calls"
    env["forbidden_calls"] = str(forbidden_calls)
    for name in ("grep", "sed", "git"):
        executable = Path(env["npm_calls"]).parent / "bin" / name
        executable.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$0 $*" >> "$forbidden_calls"\nexit 1\n'
        )
        executable.chmod(0o755)
    result = run_script(project, env, "--print")
    assert result.returncode == 0, result.stderr
    assert not forbidden_calls.exists()
    assert dict(line.split("=") for line in result.stdout.splitlines()) == PINS
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 3
    assert set(calls.read_text().splitlines()) == {
        "view @anthropic-ai/claude-code version", "view @openai/codex version",
        "view opencode-ai version",
    }
    assert snapshot(project) == before


@pytest.mark.parametrize("failure", ["network", "missing", "no-npm", "beta", "metadata", "bad"])
def test_print_failure_exits_nonzero_without_writes(script_env, failure):
    project, env, _ = script_env
    before = snapshot(project)
    if failure == "no-npm":
        bindir = Path(env["npm_calls"]).parent / "bin"
        (bindir / "npm").unlink()
        (bindir / "dirname").symlink_to(shutil.which("dirname"))
        env["PATH"] = str(bindir)
    elif failure in {"network", "missing"}:
        env["npm_failure"] = failure
    else:
        env["codex_value"] = {"beta": "0.161.0-beta", "metadata": "0.161.0+meta",
                              "bad": "not-a-version"}[failure]
    result = run_script(project, env, "--print")
    assert result.returncode != 0
    assert result.stderr
    if failure == "network":
        assert NPM_FAILURE in result.stderr
    elif failure == "missing":
        assert NPM_MISSING_PACKAGE in result.stderr
    elif failure == "no-npm":
        assert "npm: command not found" in result.stderr
    else:
        assert "Invalid version format" in result.stderr
    assert all(re.fullmatch(r"[A-Z_]+=[0-9]+\.[0-9]+\.[0-9]+", line)
               for line in result.stdout.splitlines())
    assert snapshot(project) == before


@pytest.mark.parametrize("args", [("--unknown",), ("--print", "--extra"),
                                 ("--print", "--print"), ("extra", "--print")])
def test_unknown_arguments_are_rejected(script_env, args):
    project, env, calls = script_env
    before = snapshot(project)
    result = run_script(project, env, *args)
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert result.stdout == ""
    assert not calls.exists()
    assert snapshot(project) == before


def test_maintainer_mode_updates_temp_dockerfile(script_env):
    project, env, _ = script_env
    for argv in (["git", "init", "-q"], ["git", "add", "."],
                 ["git", "-c", "user.name=Test Operator", "-c", "user.email=test@example.com",
                  "-c", "core.hooksPath=/dev/null", "commit", "-qm", "baseline"]):
        subprocess.run(argv, cwd=project, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=10, check=True)
    result = run_script(project, env)
    assert result.returncode == 0, result.stderr
    assert (project / "Dockerfile").read_text() == "".join(
        f"ARG {arg}={value}\n" for arg, value in PINS.items()
    )
    assert "Dockerfile updated!" in result.stdout
    assert "diff --git" in result.stdout
    assert "djinn build" in result.stdout and "djinn start" in result.stdout
    before = snapshot(project)
    result = run_script(project, env)
    assert result.returncode == 0
    assert "already up to date" in result.stdout
    assert snapshot(project) == before


def test_maintainer_mode_keeps_skip_on_failure_behavior(script_env):
    project, env, _ = script_env
    env["npm_failure"] = "network"
    # A diff outside a Git worktree would fail under pipefail; the fake Git
    # renders the same diff header while this test exercises skip-and-continue.
    git = Path(env["npm_calls"]).parent / "bin/git"
    git.write_text('#!/bin/bash\nprintf "diff --git a/Dockerfile b/Dockerfile\\n"\n')
    git.chmod(0o755)
    result = run_script(project, env)
    assert result.returncode == 0, result.stderr
    dockerfile = (project / "Dockerfile").read_text()
    assert "ARG CODEX_VERSION=0.160.0" in dockerfile
    assert "ARG CLAUDE_CODE_VERSION=2.1.300" in dockerfile
    assert "ARG OPENCODE_VERSION=1.18.40" in dockerfile
    assert "Failed to fetch version" in result.stdout
    assert NPM_FAILURE not in result.stderr


@pytest.mark.parametrize("redirect", ["combined", "separate"])
def test_print_preserves_redirected_output_and_diagnostics(script_env, redirect):
    project, env, _ = script_env
    env["npm_notice"] = "1"
    if redirect == "separate":
        env["npm_failure"] = "network"
    before = snapshot(project)
    logfile = project.parent / "resolver.log"
    logfile.write_text("Existing diagnostics\n")
    with logfile.open("a") as stream:
        result = subprocess.run(
            [str(project / "scripts/update-agents.sh"), "--print"], cwd=project, env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream if redirect == "combined" else subprocess.PIPE,
            stderr=subprocess.STDOUT if redirect == "combined" else stream,
            text=True, timeout=10, check=False,
        )
    log = logfile.read_text()
    assert log.startswith("Existing diagnostics\n")
    assert log.count("npm notice New major version of npm available!") == 3
    if redirect == "combined":
        assert result.returncode == 0
        assert {line for line in log.splitlines() if re.fullmatch(r"[A-Z_]+=[0-9.]+", line)} == {
            f"{arg}={value}" for arg, value in PINS.items()
        }
    else:
        assert result.returncode != 0
        assert NPM_FAILURE in log
        assert dict(line.split("=") for line in result.stdout.splitlines()) == {
            arg: value for arg, value in PINS.items() if arg != "CODEX_VERSION"
        }
    assert snapshot(project) == before
