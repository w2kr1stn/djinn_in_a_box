"""Verify the path guard contract with isolated subprocesses and configuration."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from djinn_in_a_box.config.declarations import COMPOSE_ENV_KEYS, DJINN_ENV_KEYS
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.docker import build_compose_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "djinn-path-guard.py"
TOOLS = [
    ("Read", "file_path"),
    ("Edit", "file_path"),
    ("Write", "file_path"),
    ("NotebookEdit", "notebook_path"),
    ("Grep", "path"),
    ("Glob", "path"),
]
INVALID_ENTRIES = ["relative", "/", "/a/../b", "//a", "/a/", "/a/./b", "/a//b", "/a\x00b", ""]


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    home, denied, cwd = (tmp_path / name for name in ("home", "blocked", "allowed"))
    for path in (home, denied, cwd):
        path.mkdir()
    return home, denied, cwd


def invoke(
    home: str | Path, configured: str | None, payload: object, script: Path = SCRIPT
) -> subprocess.CompletedProcess:
    env = {"HOME": str(home)}
    if configured is not None:
        env["DJINN_DENIED_PATHS"] = configured
    return subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        input=payload if isinstance(payload, bytes) else json.dumps(payload).encode(),
    )


def output(result: subprocess.CompletedProcess, message: str | None = None) -> None:
    expected = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }
    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout == ((json.dumps(expected) + "\n").encode() if message else b"")


def refusal(entry: str | Path) -> str:
    return (
        f"Access to {entry} is denied by Djinn's denied_paths. "
        "Reading or changing it needs the operator's explicit instruction and happens on the host."
    )


def failure(reason: str) -> str:
    return f"Djinn path guard failed ({reason}); the tool call is refused."


def payload(cwd: Path, tool: str = "Read", **args: object) -> dict:
    return {"tool_name": tool, "tool_input": args, "cwd": str(cwd)}


@pytest.mark.parametrize("configured", [None, "", "[]", " [ ] "])
@pytest.mark.parametrize("stdin", [b"invalid", b"\xff", b"x" * (2 << 20)])
def test_empty_configuration_is_silent(configured: str | None, stdin: bytes) -> None:
    output(invoke("relative", configured, stdin))


@pytest.mark.parametrize(("tool", "key"), TOOLS)
@pytest.mark.parametrize("blocked", [False, True])
def test_tool_path_keys(paths: tuple[Path, Path, Path], tool: str, key: str, blocked: bool) -> None:
    home, denied, cwd = paths
    target = denied if blocked else cwd
    output(
        invoke(home, json.dumps([str(denied)]), payload(cwd, tool, **{key: str(target)})),
        refusal(denied) if blocked else None,
    )


@pytest.mark.parametrize(
    "form",
    [
        "{entry}",
        "{entry}/x",
        "~{entry}/x",
        "$HOME{entry}/x",
        "${{HOME}}{entry}/x",
        "{home}{entry}/x",
        "../blocked/x",
        "../allowed/../blocked/x",
        "alias/x",
        "~",
        "~/x",
        "$HOME",
        "${HOME}",
    ],
)
def test_resolution(paths: tuple[Path, Path, Path], form: str) -> None:
    home, denied, cwd = paths
    (cwd / "alias").symlink_to(denied, target_is_directory=True)
    if form in ("~", "~/x", "$HOME", "${HOME}"):
        denied = home
    path = form.format(entry=denied, home=home) if "entry}" in form else form
    output(invoke(home, json.dumps([str(denied)]), payload(cwd, file_path=path)), refusal(denied))


def test_configured_symlink_and_segment_boundary(paths: tuple[Path, Path, Path]) -> None:
    home, denied, cwd = paths
    alias = cwd / "alias"
    alias.symlink_to(denied, target_is_directory=True)
    output(
        invoke(home, json.dumps([str(alias)]), payload(cwd, file_path=str(denied / "x"))),
        refusal(alias),
    )
    output(invoke(home, json.dumps([str(denied)]), payload(cwd, file_path=str(denied) + "x")))


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("ancestor", ["parent", "root", "home"])
def test_search_ancestor(
    paths: tuple[Path, Path, Path], tool: str, explicit: bool, ancestor: str
) -> None:
    home, denied, cwd = paths
    root = {"parent": denied.parent, "root": Path("/"), "home": home}[ancestor]
    args = {"path": str(root)} if explicit else {}
    output(
        invoke(home, json.dumps([str(denied)]), payload(cwd if explicit else root, tool, **args)),
        refusal(denied),
    )


@pytest.mark.parametrize(
    "form", ["{entry}/{wild}", "~{entry}/{wild}", "../blocked/{wild}", "{parent}/{wild}"]
)
@pytest.mark.parametrize("wild", ["*.txt", "?.txt", "[ab]/x", "{a,b}/x"])
def test_glob_pattern_prefix(paths: tuple[Path, Path, Path], form: str, wild: str) -> None:
    home, denied, cwd = paths
    pattern = form.format(entry=denied, parent=denied.parent, wild=wild)
    output(
        invoke(
            home, json.dumps([str(denied)]), payload(cwd, "Glob", path=str(cwd), pattern=pattern)
        ),
        refusal(denied),
    )


@pytest.mark.parametrize("pattern", ["blocked*", "*.txt", "{blocked,other}", "safe/*.txt"])
def test_glob_without_denied_prefix(paths: tuple[Path, Path, Path], pattern: str) -> None:
    home, denied, cwd = paths
    output(invoke(home, json.dumps([str(denied)]), payload(cwd, "Glob", pattern=pattern)))


@pytest.mark.parametrize(
    "tool", ["Bash", "apply_patch", "view_image", "mcp__files__read", "Unknown"]
)
def test_other_tools_pass(paths: tuple[Path, Path, Path], tool: str) -> None:
    home, denied, cwd = paths
    output(invoke(home, json.dumps([str(denied)]), payload(cwd, tool, path=str(denied))))


@pytest.mark.parametrize(
    "configured",
    ["invalid", "null", "{}", '"/a"', "[1]", "[null]"]
    + [json.dumps([entry]) for entry in INVALID_ENTRIES],
)
def test_invalid_configuration(paths: tuple[Path, Path, Path], configured: str) -> None:
    home, _, cwd = paths
    output(invoke(home, configured, payload(cwd)), failure("invalid DJINN_DENIED_PATHS"))


@pytest.mark.parametrize("home", ["relative", ""])
def test_invalid_home(paths: tuple[Path, Path, Path], home: str) -> None:
    _, denied, cwd = paths
    output(invoke(home, json.dumps([str(denied)]), payload(cwd)), failure("HOME is not absolute"))


@pytest.mark.parametrize(
    "invalid",
    [
        b"invalid",
        b"\xff",
        [],
        None,
        {},
        {"tool_name": 7, "tool_input": {}, "cwd": "/"},
        {"tool_name": "Read", "tool_input": [], "cwd": "/"},
    ],
)
def test_invalid_payload(paths: tuple[Path, Path, Path], invalid: object) -> None:
    home, denied, _ = paths
    output(invoke(home, json.dumps([str(denied)]), invalid), failure("invalid payload"))


@pytest.mark.parametrize("key", ["tool_name", "tool_input", "cwd"])
@pytest.mark.parametrize("value", ["missing", None, 7, []])
def test_required_payload_keys(paths: tuple[Path, Path, Path], key: str, value: object) -> None:
    home, denied, cwd = paths
    data = payload(cwd, file_path=str(cwd))
    if value == "missing":
        data.pop(key)
    else:
        data[key] = value
    output(invoke(home, json.dumps([str(denied)]), data), failure("invalid payload"))


@pytest.mark.parametrize(("tool", "key"), TOOLS + [("Glob", "pattern"), ("Grep", "pattern")])
@pytest.mark.parametrize("value", [None, 7, []])
def test_invalid_path_keys(
    paths: tuple[Path, Path, Path], tool: str, key: str, value: object
) -> None:
    home, denied, cwd = paths
    output(
        invoke(home, json.dumps([str(denied)]), payload(cwd, tool, **{key: value})),
        failure("invalid payload"),
    )


@pytest.mark.parametrize(("tool", "key"), TOOLS[:4])
def test_missing_file_key(paths: tuple[Path, Path, Path], tool: str, key: str) -> None:
    home, denied, cwd = paths
    output(
        invoke(home, json.dumps([str(denied)]), payload(cwd, tool, path=str(cwd))),
        failure("invalid payload"),
    )


def test_relative_cwd_and_unbounded_write(paths: tuple[Path, Path, Path]) -> None:
    home, denied, cwd = paths
    output(
        invoke(home, json.dumps([str(denied)]), payload(Path("relative"), file_path=str(cwd))),
        failure("invalid payload"),
    )
    output(
        invoke(
            home,
            json.dumps([str(denied)]),
            payload(cwd, "Write", file_path=str(denied / "x"), content="x" * (2 << 20)),
        ),
        refusal(denied),
    )


def test_unexpected_exception_fails_closed(paths: tuple[Path, Path, Path]) -> None:
    home, denied, cwd = paths
    runner = cwd / "fault.py"
    runner.write_text(
        "import runpy\nfrom unittest.mock import patch\n"
        "with patch('re.split', side_effect=RuntimeError('injected failure')):\n"
        f"    runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
    )
    output(
        invoke(home, json.dumps([str(denied)]), payload(cwd, "Glob", pattern="*.txt"), runner),
        failure("RuntimeError"),
    )


@pytest.mark.parametrize("entry", ["/a", "/a/b", "/a b", "/a\\b"])
def test_valid_config_entry(tmp_path: Path, entry: str) -> None:
    assert AppConfig(code_dir=tmp_path, denied_paths=(entry,)).denied_paths == (entry,)


@pytest.mark.parametrize("entry", INVALID_ENTRIES)
def test_invalid_config_entry(tmp_path: Path, entry: str) -> None:
    with pytest.raises(ValidationError) as error:
        AppConfig(code_dir=tmp_path, denied_paths=(entry,))
    assert error.value.errors()[0]["msg"] == (
        f"Value error, denied_paths entry {entry!r} must be a normalized absolute path other than /"
    )


@pytest.mark.parametrize("entries", [(), ("/a", "/b c")])
def test_config_roundtrip_and_transport(tmp_path: Path, entries: tuple[str, ...]) -> None:
    config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "config", denied_paths=entries)
    path = tmp_path / "config.toml"
    save_config(config, path)
    data = tomllib.loads(path.read_text())
    assert "denied_paths" not in data
    assert ("denied_paths" in data["general"]) == bool(entries)
    if entries:
        assert data["general"]["denied_paths"] == list(entries)
    assert load_config(path).denied_paths == entries
    assert build_compose_env(config)["DJINN_DENIED_PATHS"] == json.dumps(list(entries))
    assert "DJINN_DENIED_PATHS" not in build_compose_env(None)
    assert "DJINN_DENIED_PATHS" in COMPOSE_ENV_KEYS & DJINN_ENV_KEYS


def test_compose_and_managed_registration() -> None:
    compose = (ROOT / "docker-compose.yml").read_text().split("x-common-environment:", 1)[1]
    common = compose.split("\nservices:", 1)[0]
    assert "  DJINN_DENIED_PATHS: '${DJINN_DENIED_PATHS:-[]}'\n" in common
    settings = json.loads((SCRIPT.parent / "claude-managed-settings.json").read_text())
    command = "python3 /usr/local/libexec/djinn-path-guard.py"
    assert settings == {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read|Edit|Write|NotebookEdit|Grep|Glob|Bash",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert f"COPY --chmod=755 scripts/djinn-path-guard.py {command.split()[1]}\n" in dockerfile
    assert (
        "COPY --chmod=644 scripts/claude-managed-settings.json "
        "/etc/claude-code/managed-settings.json\n"
    ) in dockerfile
    setup = (
        "install -d -o root -g root -m 0755 /var/lib/djinn-packages /etc/codex /etc/claude-code\n"
    )
    assert dockerfile.index(setup) < dockerfile.index("USER $USERNAME\n")
