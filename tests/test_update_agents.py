"""Offline tests of the real Bash resolver and maintainer mode."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PINS = {"CLAUDE_CODE_VERSION": "2.1.300", "CODEX_VERSION": "0.161.0",
        "OPENCODE_VERSION": "2.0.25"}
TOOL_PINS = {"OH_MY_ZSH_COMMIT": "a" * 40,
             "OH_MY_ZSH_SHA256": hashlib.sha256(b"fixture archive bytes").hexdigest(),
             "OH_MY_POSH_VERSION": "31.9.2", "OH_MY_POSH_SHA256": "b" * 64,
             "FNM_VERSION": "1.39.1", "FNM_SHA256": "c" * 64}
TOOL_DEFAULTS = {name: "1" * (40 if name.endswith("COMMIT") else 64)
                 if not name.endswith("VERSION") else "1.0.0" for name in TOOL_PINS}

NPM_FAILURE = (
    "npm error code EAI_AGAIN\n"
    "npm error syscall getaddrinfo\n"
    "npm error request to https://registry.npmjs.org/@openai%2fcodex failed, "
    "reason: getaddrinfo EAI_AGAIN registry.npmjs.org\n"
)
NPM_MISSING_PACKAGE = (
    "npm error code E404\n"
    "npm error 404 Not Found - GET https://registry.npmjs.org/@opencode/cli - Not found\n"
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
        + "".join(f"ARG {name}={value}\n" for name, value in TOOL_DEFAULTS.items())
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
        '  @opencode/cli)\n'
        '    if [[ "${npm_failure:-}" == missing ]]; then\n'
        f"      printf '%s' '{NPM_MISSING_PACKAGE}' >&2; exit 1\n"
        '    fi\n'
        '    printf "%s\\n" "${opencode_value:-2.0.25}" ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n'
    )
    npm.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "npm_calls": str(calls),
           "TERM": "dumb", "NO_COLOR": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    curl_data = tmp_path / "curl-data.json"
    curl_data.write_text(json.dumps({
        "https://api.github.com/repos/JanDeDobbeleer/oh-my-posh/releases/latest":
            json.dumps({"tag_name": "v31.9.2"}),
        "https://github.com/JanDeDobbeleer/oh-my-posh/releases/download/v31.9.2/checksums.txt":
            "b" * 64 + "  posh-linux-amd64\n",
        "https://api.github.com/repos/Schniz/fnm/releases/latest": json.dumps({
            "tag_name": "v1.39.1",
            "assets": [{"name": "fnm-linux.zip", "digest": "sha256:" + "c" * 64}]}),
        "https://api.github.com/repos/ohmyzsh/ohmyzsh/commits/master":
            json.dumps({"sha": "a" * 40}),
        "https://codeload.github.com/ohmyzsh/ohmyzsh/tar.gz/" + "a" * 40: "fixture archive bytes",
    }))
    env.update(curl_data=str(curl_data), curl_calls=str(tmp_path / "curl-calls"))
    curl = bindir / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['curl_calls'], 'a') as f: f.write(json.dumps(args) + '\\n')\n"
        "for flag in ['--proto', '--proto-redir']:\n"
        "    assert args[args.index(flag) + 1] == '=https'\n"
        "url = next(a for a in args if a.startswith('https://'))\n"
        "data = json.load(open(os.environ['curl_data']))\n"
        "if url not in data:\n"
        "    print('curl: (22) The requested URL returned error: 404', file=sys.stderr)\n"
        "    sys.exit(22)\n"
        "if data[url] is None:\n"
        "    print('curl: (6) Could not resolve host: api.github.com', file=sys.stderr)\n"
        "    sys.exit(6)\n"
        "if '-o' in args: Path(args[args.index('-o') + 1]).write_text(data[url])\n"
        "else: sys.stdout.write(data[url])\n"
        "if os.environ.get('curl_failure') == url:\n"
        "    print('curl: (22) The requested URL returned error: 500', file=sys.stderr)\n"
        "    sys.exit(22)\n"
    )
    curl.chmod(0o755)
    git = bindir / "git"
    git.write_text('#!/bin/sh\nprintf "diff --git a/Dockerfile b/Dockerfile\\n"\n')
    git.chmod(0o755)
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
    for name in ("grep", "sed", "git", "curl"):
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
        "view @opencode/cli version",
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
    assert all(re.fullmatch(r"[A-Z0-9_]+=[0-9]+\.[0-9]+\.[0-9]+", line)
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
    git_env = {**env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for argv in (["git", "init", "-q"], ["git", "add", "."],
                 ["git", "-c", "user.name=Test Operator", "-c", "user.email=test@example.com",
                  "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                  "commit", "-qm", "baseline"]):
        subprocess.run(argv, cwd=project, env=git_env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=10, check=True)
    result = run_script(project, env)
    assert result.returncode == 0, result.stderr
    assert (project / "Dockerfile").read_text() == "".join(
        f"ARG {arg}={value}\n" for arg, value in {**PINS, **TOOL_PINS}.items()
    )
    assert "Dockerfile updated!" in result.stdout
    assert "diff --git" in result.stdout
    assert "djinn build" in result.stdout and "djinn start" in result.stdout
    assert "agent-versions.toml aside" in result.stdout
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
    assert "ARG OPENCODE_VERSION=2.0.25" in dockerfile
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
        assert {line for line in log.splitlines() if re.fullmatch(r"[A-Z0-9_]+=[0-9.]+", line)} == {
            f"{arg}={value}" for arg, value in PINS.items()
        }
    else:
        assert result.returncode != 0
        assert NPM_FAILURE in log
        assert dict(line.split("=") for line in result.stdout.splitlines()) == {
            arg: value for arg, value in PINS.items() if arg != "CODEX_VERSION"
        }
    assert snapshot(project) == before


def dockerfile_pins(project):
    return dict(re.findall(r"^ARG ([A-Z0-9_]+)=(.*)$", (project / "Dockerfile").read_text(), re.M))


@pytest.mark.parametrize("tool,problem", [
    ("oh-my-posh", "newline"), ("oh-my-posh", "nul"),
    ("oh-my-posh", "bad-tag"), ("oh-my-posh", "missing"), ("oh-my-posh", "duplicate"),
    ("oh-my-posh", "bad-hash"), ("oh-my-posh", "extra-field"), ("oh-my-posh", "network"),
    ("fnm", "newline"), ("fnm", "nul"),
    ("fnm", "bad-tag"), ("fnm", "missing"), ("fnm", "duplicate"),
    ("fnm", "digest-newline"),
    ("fnm", "bad-hash"), ("fnm", "wrong-algorithm"), ("fnm", "null-digest"),
    ("oh-my-zsh", "newline"), ("oh-my-zsh", "nul"),
    ("oh-my-zsh", "bad-commit"), ("oh-my-zsh", "network"),
    ("oh-my-zsh", "archive-failure"),
])
def test_failed_tool_pair_is_unchanged_while_other_pairs_update(script_env, tool, problem):
    project, env, _ = script_env
    datafile = Path(env["curl_data"])
    data = json.loads(datafile.read_text())
    apis = {"oh-my-posh": "https://api.github.com/repos/JanDeDobbeleer/oh-my-posh/releases/latest",
            "fnm": "https://api.github.com/repos/Schniz/fnm/releases/latest",
            "oh-my-zsh": "https://api.github.com/repos/ohmyzsh/ohmyzsh/commits/master"}
    api = apis[tool]
    release = json.loads(data[api])
    if problem in {"newline", "nul"}:
        field = "sha" if tool == "oh-my-zsh" else "tag_name"
        release[field] += "\n" if problem == "newline" else "\x00"
    elif problem == "bad-tag":
        release["tag_name"] = "v1.2.3-beta"
        if tool == "oh-my-posh":
            url = ("https://github.com/JanDeDobbeleer/oh-my-posh/releases/download/"
                   "v1.2.3-beta/checksums.txt")
            data[url] = "b" * 64 + "  posh-linux-amd64\n"
    elif tool == "oh-my-posh" and problem != "network":
        url = "https://github.com/JanDeDobbeleer/oh-my-posh/releases/download/v31.9.2/checksums.txt"
        good = data[url]
        data[url] = {"missing": "b" * 64 + "  other-binary\n", "duplicate": good * 2,
                     "bad-hash": "z" * 64 + "  posh-linux-amd64\n",
                     "extra-field": good.strip() + " trailing\n"}[problem]
    elif tool == "fnm":
        asset = release["assets"][0]
        if problem == "missing":
            asset["name"] = "fnm-arm64.zip"
        elif problem == "duplicate":
            release["assets"].append(asset.copy())
        else:
            asset["digest"] = {"bad-hash": "sha256:" + "G" * 64,
                               "wrong-algorithm": "sha512:" + "c" * 64,
                               "null-digest": None,
                               "digest-newline": "sha256:" + "c" * 64 + "\n"}[problem]
    elif problem == "bad-commit":
        release["sha"] = "a" * 39
        url = "https://codeload.github.com/ohmyzsh/ohmyzsh/tar.gz/" + "a" * 39
        data[url] = "fixture archive bytes"
    data[api] = None if problem == "network" else json.dumps(release)
    if problem == "archive-failure":
        data.pop("https://codeload.github.com/ohmyzsh/ohmyzsh/tar.gz/" + "a" * 40)
    datafile.write_text(json.dumps(data))
    before = dockerfile_pins(project)
    result = run_script(project, env)
    assert result.returncode == 0, result.stderr
    assert f"{tool}:" in result.stderr
    prefix = {"oh-my-posh": "OH_MY_POSH_", "fnm": "FNM_", "oh-my-zsh": "OH_MY_ZSH_"}[tool]
    expected = {**PINS, **TOOL_PINS}
    for name in TOOL_PINS:
        if name.startswith(prefix):
            expected[name] = before[name]
    assert dockerfile_pins(project) == expected


@pytest.mark.parametrize("partner", list(TOOL_PINS))
@pytest.mark.parametrize("problem", ["missing", "duplicate", "noncanonical"])
def test_pair_requires_one_canonical_line_for_each_partner(script_env, partner, problem):
    project, env, _ = script_env
    dockerfile = project / "Dockerfile"
    original = dockerfile.read_text()
    line = f"ARG {partner}={TOOL_DEFAULTS[partner]}\n"
    replacement = {"missing": "", "duplicate": line * 2,
                   "noncanonical": "  " + line}[problem]
    dockerfile.write_text(original.replace(line, replacement))
    result = run_script(project, env)
    assert result.returncode == 0, result.stderr
    assert "Expected exactly one canonical ARG" in result.stderr
    prefix = partner.rsplit("_", 1)[0] + "_"
    # For both the duplicated/missing line and its partner, preserve the exact bytes.
    before_lines = [s for s in (original.replace(line, replacement)).splitlines(True)
                    if prefix in s]
    after_lines = [s for s in dockerfile.read_text().splitlines(True) if prefix in s]
    assert after_lines == before_lines
    for name, value in TOOL_PINS.items():
        if not name.startswith(prefix):
            assert f"ARG {name}={value}\n" in dockerfile.read_text()


def test_tool_hash_change_at_same_version_updates_the_whole_pair(script_env):
    project, env, _ = script_env
    dockerfile = project / "Dockerfile"
    for name in TOOL_PINS:
        if name.endswith(("VERSION", "COMMIT")):
            dockerfile.write_text(dockerfile.read_text().replace(
                f"ARG {name}={TOOL_DEFAULTS[name]}", f"ARG {name}={TOOL_PINS[name]}"))
    result = run_script(project, env)
    assert result.returncode == 0, result.stderr
    assert dockerfile_pins(project) == {**PINS, **TOOL_PINS}


def test_each_pair_uses_one_sed_write_with_both_anchored_expressions(script_env):
    project, env, _ = script_env
    bindir = Path(env["npm_calls"]).parent / "bin"
    sed = bindir / "sed"
    env["sed_calls"] = str(bindir.parent / "sed-calls")
    sed.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$sed_calls"\n'
        f'exec {shutil.which("sed")} "$@"\n'
    )
    sed.chmod(0o755)
    result = run_script(project, env)
    assert result.returncode == 0, result.stderr
    calls = Path(env["sed_calls"]).read_text().splitlines()
    for prefix in ("OH_MY_POSH", "OH_MY_ZSH", "FNM"):
        matching = [line for line in calls if f"s/^ARG {prefix}_" in line]
        assert len(matching) == 1
        assert f"ARG {prefix}_SHA256=" in matching[0]
        assert f"ARG {prefix}_{'COMMIT' if prefix == 'OH_MY_ZSH' else 'VERSION'}=" in matching[0]
        assert matching[0].count("-e s/^ARG ") == 2


def test_tool_arg_names_match_real_dockerfile():
    script = (ROOT / "scripts/update-agents.sh").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    names = set(re.findall(r"(?:arg_name|hash_name)=((?:OH_MY_|FNM_)[A-Z0-9_]+)", script))
    assert names == set(TOOL_PINS)
    for name in names:
        assert len(re.findall(rf"^ARG {name}=", dockerfile, re.M)) == 1


@pytest.mark.parametrize("url,tool", [
    ("https://api.github.com/repos/JanDeDobbeleer/oh-my-posh/releases/latest", "oh-my-posh"),
    ("https://github.com/JanDeDobbeleer/oh-my-posh/releases/download/v31.9.2/checksums.txt",
     "oh-my-posh"),
    ("https://api.github.com/repos/Schniz/fnm/releases/latest", "fnm"),
    ("https://api.github.com/repos/ohmyzsh/ohmyzsh/commits/master", "oh-my-zsh"),
    ("https://codeload.github.com/ohmyzsh/ohmyzsh/tar.gz/" + "a" * 40, "oh-my-zsh"),
])
def test_curl_error_with_valid_partial_output_keeps_pair(script_env, url, tool):
    project, env, _ = script_env
    env["curl_failure"] = url
    result = run_script(project, env)
    assert result.returncode == 0, result.stderr
    assert f"{tool}: Failed to resolve pin pair" in result.stderr
    prefix = {"oh-my-posh": "OH_MY_POSH_", "fnm": "FNM_", "oh-my-zsh": "OH_MY_ZSH_"}[tool]
    for name, value in TOOL_DEFAULTS.items():
        if name.startswith(prefix):
            assert dockerfile_pins(project)[name] == value
