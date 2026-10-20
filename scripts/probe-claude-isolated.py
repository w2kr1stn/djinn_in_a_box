#!/usr/bin/env python3
"""Recheck the installed Claude CLI after each pin bump, inside the dev container.

Usage: uv run python scripts/probe-claude-isolated.py [--offline]
Phase 1 uses synthetic canaries, an allowlisted environment and a disposable HOME;
its two runs must end "Not logged in". --offline stops there. Phase 2 uses the
current login (and may refresh its token like any normal Claude run). It makes
exactly one hook-free, memory-disabled, nonpersistent positive control and two
preset runs in scratch projects. Claude may write its usual app state; this tool
only checks names/metadata in the real config root and counts fixed canaries in
history.jsonl. It never opens credentials or deletes real-profile artifacts.
Every phase-1 check and the CLAUDE.md check have a positive control in the same
probe; the AGENTS.md check has none (one control run per probe) and relies on
the one-time manual measurement. All scratch data is removed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

from djinn_in_a_box.config.defaults import DEFAULT_AGENTS
from djinn_in_a_box.core.agent_runner import build_agent_command

TIMEOUT = 120
CLAUDE_CANARY = "DJINN-PROBE-CANARY-CLAUDEMD-7319"
AGENTS_CANARY = "DJINN-PROBE-CANARY-AGENTSMD-7320"
DECOY = "DJINN-PROBE-DECOY-7399"
QUESTION = (
    'Return only one JSON object {"claude_md": bool, "agents_md": bool, "decoy": bool}. '
    "For each exact string below, say whether it occurs in the context BEFORE this "
    "message (ignore occurrences in this message). "
    f"claude_md: {CLAUDE_CANARY}; agents_md: {AGENTS_CANARY}; decoy: {DECOY}."
)
MCP_SERVER = """import json
import pathlib
import sys

pathlib.Path(sys.argv[1]).touch()
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        result = {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "probe", "version": "0.0.1"},
        }
    elif msg.get("method") == "tools/list":
        result = {"tools": [{"name": "probe_echo", "description": "probe tool",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
"""


class Observation(NamedTuple):
    init: dict[str, Any]
    result: dict[str, Any]
    markers: frozenset[str] = frozenset()
    error: str = ""


def preset_argv() -> list[str]:
    preset = DEFAULT_AGENTS["claude-isolated"]
    return [
        preset.binary,
        *preset.headless_flags,
        *preset.read_only_flags,
        "--output-format",
        "stream-json",
        "--verbose",
        "--",
        "probe",
    ]


def phase_one_env(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "TMPDIR": str(root / "tmp"),
    }


def write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def hook_settings(marker: Path) -> dict[str, Any]:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command", "command": f"touch -- {shlex.quote(str(marker))}"},
                    ]
                }
            ]
        }
    }


def setup_phase_one(root: Path) -> None:
    for key, value in phase_one_env(root).items():
        if key in {"HOME", "TMPDIR"} or key.startswith("XDG_"):
            Path(value).mkdir(parents=True, exist_ok=True)
    (root / "markers").mkdir()
    user = root / "home" / ".claude"
    project = root / "proj" / ".claude"
    settings = hook_settings(root / "markers" / "user-hook")
    settings.update(
        {
            "permissions": {"defaultMode": "bypassPermissions"},
            "skipDangerousModePermissionPrompt": True,
        }
    )
    write_file(user / "settings.json", json.dumps(settings))
    write_file(
        project / "settings.json", json.dumps(hook_settings(root / "markers" / "project-hook"))
    )
    for base, name in ((user, "probe-skill"), (project, "probe-project-skill")):
        write_file(
            base / "skills" / name / "SKILL.md",
            f"---\nname: {name}\ndescription: synthetic probe skill\n---\nprobe\n",
        )
    write_file(
        user / "agents" / "probe-agent.md",
        "---\nname: probe-agent\ndescription: synthetic probe agent\n---\nprobe\n",
    )
    write_file(
        user / "commands" / "probe-cmd.md",
        "---\ndescription: synthetic probe command\n---\nprobe\n",
    )
    write_file(root / "mcp_probe.py", MCP_SERVER)
    write_file(
        root / "home" / ".claude.json",
        json.dumps(
            {
                "mcpServers": {
                    "probe": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(root / "mcp_probe.py"), str(root / "markers" / "mcp-started")],
                    }
                }
            }
        ),
    )


def run_command(
    argv: list[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def parse_events(output: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        events = [json.loads(output)]
    except json.JSONDecodeError:
        events = []
        for line in output.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    init, result = {}, {}
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init = event
        if event.get("type") == "result":
            result = event
    return init, result


def observe(
    argv: list[str], cwd: Path, env: dict[str, str], markers: Path | None = None
) -> Observation:
    try:
        completed = run_command(argv, cwd, env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Observation({}, {}, error=f"run failed ({type(exc).__name__})")
    init, result = parse_events(completed.stdout)
    found = frozenset(p.name for p in markers.iterdir()) if markers else frozenset()
    error = "" if result else "no result event"
    return Observation(init, result, found, error)


def names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        item.get("name") if isinstance(item, dict) else item
        for item in value
        if isinstance(item, str) or isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def evaluate_phase_one(control: Observation, preset: Observation) -> list[str]:
    lines = []
    for label, run in (("control", control), ("isolation", preset)):
        if run.error:
            lines.append(f"FAIL {label} evidence: {run.error}")
        if run.result:
            if run.result.get("is_error") is True and "Not logged in" in str(
                run.result.get("result", "")
            ):
                lines.append(f"PASS credential-free {label}")
            else:
                lines.append("FAIL credentials reached the credential-free phase: " + label)
        if not run.init:
            lines.append(f"FAIL {label} init: no system/init event")
    a, b = control.init, preset.init
    hook_names = {"user-hook", "project-hook"}
    checks = [
        (
            "built-in tools",
            isinstance(a.get("tools"), list) and bool(a["tools"]),
            b.get("tools") == [],
        ),
        ("MCP", "probe" in names(a.get("mcp_servers")), b.get("mcp_servers") == []),
        (
            "skills",
            {"probe-skill", "probe-project-skill"} <= names(a.get("skills")),
            b.get("skills") == [],
        ),
        (
            "slash commands",
            "probe-cmd" in names(a.get("slash_commands")),
            b.get("slash_commands") == [],
        ),
        (
            "agents",
            "probe-agent" in names(a.get("agents")),
            isinstance(b.get("agents"), list) and "probe-agent" not in names(b["agents"]),
        ),
        ("auto-memory", "memory_paths" in a, "memory_paths" not in b),
        (
            "user settings",
            a.get("permissionMode") == "bypassPermissions",
            b.get("permissionMode") == "default",
        ),
        ("hooks", hook_names <= control.markers, not hook_names & preset.markers),
        ("MCP start", "mcp-started" in control.markers, "mcp-started" not in preset.markers),
    ]
    for name, shown, isolated in checks:
        if not shown:
            lines.append(f"FAIL control {name}: synthetic canary not observed")
        if not isolated:
            lines.append(f"FAIL isolation {name}: preset retained context")
        if shown and isolated:
            lines.append(f"PASS {name}")
    return lines


def phase_one() -> list[str]:
    root = Path(tempfile.mkdtemp(prefix="djinn-probe-"))
    try:
        setup_phase_one(root)
        env = phase_one_env(root)
        version = run_command(["claude", "--version"], root / "proj", env)
        print(
            f"claude --version: {' '.join(version.stdout.split())} (measured: 2.1.304)", flush=True
        )
        control = observe(
            ["claude", "-p", "--output-format", "stream-json", "--verbose", "--", "probe"],
            root / "proj",
            env,
            root / "markers",
        )
        for marker in (root / "markers").iterdir():
            marker.unlink()
        preset = observe(preset_argv(), root / "proj", env, root / "markers")
        lines = evaluate_phase_one(control, preset)
        if version.returncode != 0:
            lines.append("FAIL version: claude --version failed")
        return lines
    finally:
        shutil.rmtree(root)


def answer_json(result: dict[str, Any]) -> dict[str, Any]:
    text = result.get("result", "")
    if not isinstance(text, str):
        return {}
    start = text.find("{")
    if start < 0:
        return {}
    try:
        answer, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return {}
    return answer if isinstance(answer, dict) else {}


def evaluate_phase_two(run: Observation, label: str, field: str, expected: bool) -> list[str]:
    answer = answer_json(run.result)
    if (
        run.error
        or run.result.get("subtype") != "success"
        or run.result.get("is_error") is not False
        or answer.get(field) is not expected
        or answer.get("decoy") is not False
    ):
        return [f"FAIL {label}: expected successful JSON with {field}={expected}, decoy=False"]
    return [f"PASS {label}"]


def check_artifacts(cwd: Path, session_id: Any, env: dict[str, str], label: str) -> list[str]:
    root = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    paths = [root / "projects" / slug]
    lines = []
    if isinstance(session_id, str) and session_id and Path(session_id).name == session_id:
        paths.append(root / "session-env" / session_id)
    else:
        lines.append(f"FAIL artifacts {label}: missing or invalid session_id")
    for path in paths:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            lines.append(f"FAIL artifacts {label}: cannot check {path} ({type(exc).__name__})")
        else:
            lines.append(f"FAIL artifacts {label}: found {path}")
    history = root / "history.jsonl"
    try:
        text = history.read_text(encoding="utf-8")
        count = sum(text.count(canary) for canary in (CLAUDE_CANARY, AGENTS_CANARY, DECOY))
        if count:
            lines.append(f"FAIL artifacts {label}: {count} canary hit(s) in {history}")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError) as exc:
        lines.append(f"FAIL artifacts {label}: cannot check {history} ({type(exc).__name__})")
    return lines or [f"PASS artifacts {label}"]


def phase_two() -> list[str]:
    root = Path(tempfile.mkdtemp(prefix="djinn-probe-"))
    try:
        write_file(root / "ctl" / "CLAUDE.md", CLAUDE_CANARY + "\n")
        write_file(root / "agents" / "AGENTS.md", AGENTS_CANARY + "\n")
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER", "PULSE_COOKIE"}
        }
        control_argv = [
            "claude",
            "-p",
            "--no-session-persistence",
            "--setting-sources=project",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--tools=",
            "--output-format",
            "json",
            "--",
            QUESTION,
        ]
        command = build_agent_command(DEFAULT_AGENTS["claude-isolated"], json_output=True)
        runs = [
            (
                "control CLAUDE.md",
                root / "ctl",
                control_argv,
                {**env, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
                "claude_md",
                True,
            ),
            (
                "isolation CLAUDE.md",
                root / "ctl",
                ["bash", "-c", command],
                {**env, "AGENT_PROMPT": QUESTION},
                "claude_md",
                False,
            ),
            (
                "isolation AGENTS.md",
                root / "agents",
                ["bash", "-c", command],
                {**env, "AGENT_PROMPT": QUESTION},
                "agents_md",
                False,
            ),
        ]
        lines = []
        for label, cwd, argv, child_env, field, expected in runs:
            run = observe(argv, cwd, child_env)
            lines.extend(evaluate_phase_two(run, label, field, expected))
            lines.extend(check_artifacts(cwd, run.result.get("session_id"), child_env, label))
        return lines
    finally:
        shutil.rmtree(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="run only credential-free checks")
    args = parser.parse_args(argv)
    if sys.platform != "linux" or shutil.which("claude") is None:
        print("FAIL environment: Linux and claude on PATH are required")
        return 2
    try:
        lines = phase_one()
        if not args.offline:
            lines.extend(phase_two())
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL probe: {type(exc).__name__}")
        return 1
    for line in lines:
        print(line)
    return int(any(line.startswith("FAIL ") for line in lines))


if __name__ == "__main__":
    raise SystemExit(main())
