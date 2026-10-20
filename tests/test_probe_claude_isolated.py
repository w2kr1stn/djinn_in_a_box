"""Synthetic CLI events only; real Claude and credentials are never used here."""

import copy
import errno
import importlib.util
import io
import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from djinn_in_a_box.config.defaults import DEFAULT_AGENTS
from djinn_in_a_box.core.agent_runner import build_agent_command

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe-claude-isolated.py"
spec = importlib.util.spec_from_file_location("probe_claude_isolated", SCRIPT)
assert spec and spec.loader
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

CONTROL_INIT = {
    "type": "system",
    "subtype": "init",
    "tools": ["Read", "Bash"],
    "mcp_servers": [{"name": "probe", "status": "connected"}],
    "skills": ["probe-skill", "probe-project-skill"],
    "slash_commands": ["probe-cmd"],
    "agents": ["general-purpose", "probe-agent"],
    "permissionMode": "bypassPermissions",
    "memory_paths": {"project": "/tmp/synthetic-memory"},
}
PRESET_INIT = {
    "type": "system",
    "subtype": "init",
    "tools": [],
    "mcp_servers": [],
    "skills": [],
    "slash_commands": [],
    "agents": ["general-purpose"],
    "permissionMode": "default",
}
UNAUTHENTICATED = {
    "type": "result",
    "subtype": "error_during_execution",
    "is_error": True,
    "result": "Not logged in · Please run /login",
    "session_id": "offline-session",
}
MARKERS = frozenset({"user-hook", "project-hook", "mcp-started"})
CHECKS = [
    "built-in tools",
    "MCP",
    "skills",
    "slash commands",
    "agents",
    "auto-memory",
    "user settings",
    "hooks",
    "MCP start",
]


@pytest.fixture(autouse=True)
def isolated_profile(monkeypatch, tmp_path):
    # Even a broken root selector in a mutation must stay in the fake profile.
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin"})
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")


def observations():
    return [
        probe.Observation(copy.deepcopy(CONTROL_INIT), dict(UNAUTHENTICATED), MARKERS),
        probe.Observation(copy.deepcopy(PRESET_INIT), dict(UNAUTHENTICATED)),
    ]


class FakeCLI:
    def __init__(self):
        self.calls = []
        self.after_run = lambda _cwd, _result: None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        cwd, env = Path(kwargs["cwd"]), kwargs["env"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 120
        assert kwargs["check"] is False
        if argv == ["claude", "--version"]:
            return subprocess.CompletedProcess(argv, 0, "2.1.304 (Claude Code)\n", "")
        if "stream-json" in argv:
            isolated = "--safe-mode" in argv
            if not isolated:
                for marker in MARKERS:
                    (cwd.parent / "markers" / marker).touch()
            events = [PRESET_INIT if isolated else CONTROL_INIT, UNAUTHENTICATED]
            return subprocess.CompletedProcess(argv, 1, "\n".join(map(json.dumps, events)), "")
        control = argv[0] == "claude"
        answer = {"claude_md": control, "agents_md": False, "decoy": False}
        result = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "session-" + str(len(self.calls)),
            "result": "```json\n" + json.dumps(answer) + "\n```",
        }
        assert env.get("AGENT_PROMPT", argv[-1]) == probe.probe_question
        self.after_run(cwd, result)
        return subprocess.CompletedProcess(argv, 0, json.dumps(result, indent=2), "")


@pytest.fixture
def cli(monkeypatch, tmp_path):
    fake = FakeCLI()
    monkeypatch.setattr(probe.subprocess, "run", fake)
    monkeypatch.setattr(probe.shutil, "which", lambda _name: "/synthetic/claude")
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile"))
    return fake


def test_preset_argv_matches_frozen_definition():
    expected = [
        "claude",
        "-p",
        "--no-session-persistence",
        "--safe-mode",
        "--setting-sources=",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--tools=",
        "--output-format",
        "stream-json",
        "--verbose",
        "--",
        "probe",
    ]
    assert probe.preset_argv() == expected
    preset = DEFAULT_AGENTS["claude-isolated"]
    assert expected == [
        preset.binary,
        *preset.headless_flags,
        *preset.read_only_flags,
        "--output-format",
        "stream-json",
        "--verbose",
        "--",
        "probe",
    ]
    assert probe.probe_question == (
        'Return only one JSON object {"claude_md": bool, "agents_md": bool, "decoy": bool}. '
        "For each exact string below, say whether it occurs in the context BEFORE this "
        "message (ignore occurrences in this message). "
        "claude_md: DJINN-PROBE-CANARY-CLAUDEMD-7319; "
        "agents_md: DJINN-PROBE-CANARY-AGENTSMD-7320; decoy: DJINN-PROBE-DECOY-7399."
    )


def test_phase_one_environment_commands_cleanup(cli, monkeypatch):
    ambient = {
        "ANTHROPIC_API_KEY": "synthetic",
        "CLAUDE_CODE_OAUTH_TOKEN": "synthetic",
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "HTTPS_PROXY": "synthetic",
        "CLAUDE_CONFIG_DIR": "/synthetic/profile",
        "PROVIDER": "synthetic",
        "DBUS_SESSION_BUS_ADDRESS": "synthetic",
        "PULSE_SERVER": "synthetic",
    }
    for key, value in ambient.items():
        monkeypatch.setenv(key, value)
    assert all(line.startswith("PASS ") for line in probe.phase_one())
    assert len(cli.calls) == 3
    assert cli.calls[0][0] == ["claude", "--version"]
    assert cli.calls[1][0] == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--",
        "probe",
    ]
    assert cli.calls[2][0] == probe.preset_argv()
    roots = set()
    for _, kwargs in cli.calls:
        env = kwargs["env"]
        root = Path(kwargs["cwd"]).parent
        roots.add(root)
        assert root.name.startswith("djinn-probe-")
        assert env == {
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
        assert not set(ambient) & env.keys()
        assert not root.exists()
    assert len(roots) == 1


def test_canary_placement_and_mcp_protocol(tmp_path, monkeypatch):
    root = tmp_path / "scratch with spaces"
    probe.setup_phase_one(root)
    for key, value in probe.phase_one_env(root).items():
        if key in {"HOME", "TMPDIR"} or key.startswith("XDG_"):
            assert Path(value).is_dir()
    user = root / "home" / ".claude"
    project = root / "proj" / ".claude"
    settings = json.loads((user / "settings.json").read_text())
    assert settings["permissions"] == {"defaultMode": "bypassPermissions"}
    assert settings["skipDangerousModePermissionPrompt"] is True
    for base, marker in ((user, "user-hook"), (project, "project-hook")):
        config = json.loads((base / "settings.json").read_text())
        hooks = config["hooks"]["SessionStart"]
        assert len(hooks) == 1 and len(hooks[0]["hooks"]) == 1
        hook = hooks[0]["hooks"][0]
        assert hook["type"] == "command"
        assert shlex.split(hook["command"]) == ["touch", "--", str(root / "markers" / marker)]
    for base, relative, header in (
        (user, "skills/probe-skill/SKILL.md", "name: probe-skill"),
        (project, "skills/probe-project-skill/SKILL.md", "name: probe-project-skill"),
        (user, "agents/probe-agent.md", "name: probe-agent"),
        (user, "commands/probe-cmd.md", "description: synthetic probe command"),
    ):
        text = (base / relative).read_text()
        assert text.startswith("---\n") and header in text and "\n---\nprobe\n" in text
        if relative.endswith("SKILL.md"):
            assert "description: synthetic probe skill\n" in text
        elif relative.startswith("agents/"):
            assert "description: synthetic probe agent\n" in text
    config = json.loads((root / "home" / ".claude.json").read_text())
    assert config == {
        "mcpServers": {
            "probe": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(root / "mcp_probe.py"), str(root / "markers" / "mcp-started")],
            }
        }
    }
    messages = [
        "not json",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"id": 2, "method": "tools/list"}),
        json.dumps({"id": 3, "method": "unknown"}),
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(messages)))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(
        sys, "argv", [str(root / "mcp_probe.py"), str(root / "markers" / "mcp-started")]
    )
    exec(compile((root / "mcp_probe.py").read_text(), "mcp_probe.py", "exec"), {})
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert (root / "markers" / "mcp-started").exists()
    assert replies == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "probe", "version": "0.0.1"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {
                        "name": "probe_echo",
                        "description": "probe tool",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {}},
    ]


def test_evaluator_all_pass():
    assert probe.evaluate_phase_one(*observations()) == [
        "PASS credential-free control",
        "PASS credential-free isolation",
        *("PASS " + name for name in CHECKS),
    ]


WRONG_FIELDS = [
    ("tools", [], ["Read"], "built-in tools"),
    ("mcp_servers", [], [{"name": "probe", "status": "connected"}], "MCP"),
    ("skills", ["probe-skill"], ["probe-skill"], "skills"),
    ("skills", ["probe-project-skill"], ["probe-project-skill"], "skills"),
    ("slash_commands", [], ["probe-cmd"], "slash commands"),
    ("agents", ["general-purpose"], ["probe-agent"], "agents"),
    ("memory_paths", None, {}, "auto-memory"),
    ("permissionMode", "default", "bypassPermissions", "user settings"),
]


@pytest.mark.parametrize("side", [0, 1], ids=["control", "isolation"])
@pytest.mark.parametrize(
    "field,wrong_control,wrong_preset,name",
    WRONG_FIELDS,
    ids=["tools", "mcp", "user-skill", "project-skill", "commands", "agents", "memory", "settings"],
)
def test_evaluator_wrong_field(side, field, wrong_control, wrong_preset, name):
    runs = observations()
    if side == 0 and field == "memory_paths":
        del runs[side].init[field]
    else:
        runs[side].init[field] = wrong_preset if side else wrong_control
    failures = [line for line in probe.evaluate_phase_one(*runs) if line.startswith("FAIL ")]
    assert len(failures) == 1
    assert failures[0].startswith(f"FAIL {'isolation' if side else 'control'} {name}:")


@pytest.mark.parametrize("side", [0, 1], ids=["control", "isolation"])
@pytest.mark.parametrize(
    "marker,name", [("user-hook", "hooks"), ("project-hook", "hooks"), ("mcp-started", "MCP start")]
)
def test_evaluator_wrong_marker(side, marker, name):
    runs = observations()
    markers = {marker} if side else MARKERS - {marker}
    runs[side] = runs[side]._replace(markers=frozenset(markers))
    failures = [line for line in probe.evaluate_phase_one(*runs) if line.startswith("FAIL ")]
    assert len(failures) == 1
    assert failures[0].startswith(f"FAIL {'isolation' if side else 'control'} {name}:")


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("change", [{"is_error": False}, {"result": "success"}])
def test_authenticated_phase_one_fails(side, change):
    runs = observations()
    runs[side].result.update(change)
    assert any(
        line.startswith("FAIL credentials reached the credential-free phase:")
        for line in probe.evaluate_phase_one(*runs)
    )


@pytest.mark.parametrize("side", [0, 1])
def test_missing_init_fails(side):
    runs = observations()
    runs[side] = runs[side]._replace(init={})
    assert f"FAIL {'isolation' if side else 'control'} init: no system/init event" in (
        probe.evaluate_phase_one(*runs)
    )


@pytest.mark.parametrize("output", ["", "[]", "not json", '{"type":"system","subtype":"status"}'])
def test_missing_result_is_not_evidence(cli, output, monkeypatch, tmp_path):
    monkeypatch.setattr(
        probe, "run_command", lambda *_a: subprocess.CompletedProcess([], 1, output)
    )
    run = probe.observe(["claude"], tmp_path, {})
    assert run.error == "no result event"
    assert "FAIL control evidence: no result event" in probe.evaluate_phase_one(
        run, observations()[1]
    )


def test_parse_stream_and_wrapped_answer():
    stream = "ignored line\n42\n" + "\n".join(map(json.dumps, [CONTROL_INIT, UNAUTHENTICATED]))
    assert probe.parse_events(stream) == (CONTROL_INIT, UNAUTHENTICATED)
    assert probe.parse_events(json.dumps(UNAUTHENTICATED, indent=2)) == ({}, UNAUTHENTICATED)
    assert probe.answer_json({"result": 'prefix {"claude_md":true,"decoy":false} suffix'}) == {
        "claude_md": True,
        "decoy": False,
    }


@pytest.mark.parametrize("text", [None, "no JSON", "{broken}", "[true]"])
def test_invalid_answer(text):
    assert probe.answer_json({"result": text}) == {}


@pytest.mark.parametrize(
    "change",
    [
        {"subtype": "error_during_execution"},
        {"is_error": True},
        {"result": '{"claude_md":false,"decoy":false}'},
        {"result": '{"claude_md":true,"decoy":true}'},
        {"result": '{"claude_md":1,"decoy":false}'},
        {"result": '{"claude_md":true,"decoy":0}'},
    ],
)
def test_phase_two_wrong_result(change):
    result = {"subtype": "success", "is_error": False, "result": '{"claude_md":true,"decoy":false}'}
    result.update(change)
    assert probe.evaluate_phase_two(probe.Observation({}, result), "control", "claude_md", True)[
        0
    ].startswith("FAIL control:")


def test_phase_two_commands_safety_and_order(cli, monkeypatch):
    for key in ("DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER", "PULSE_COOKIE"):
        monkeypatch.setenv(key, "synthetic")
    monkeypatch.setenv("PROBE_KEEP", "inherited")
    checked = []
    original = probe.check_artifacts

    def check(cwd, session_id, env, label):
        checked.append((cwd, session_id, label, len(cli.calls)))
        return original(cwd, session_id, env, label)

    monkeypatch.setattr(probe, "check_artifacts", check)

    def inspect(cwd, _result):
        root = cwd.parent
        assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == [
            "agents/AGENTS.md",
            "ctl/CLAUDE.md",
        ]
        assert (root / "ctl" / "CLAUDE.md").read_text() == probe.claude_canary + "\n"
        assert (root / "agents" / "AGENTS.md").read_text() == probe.agents_canary + "\n"
        written = "".join(p.read_text() for p in root.rglob("*") if p.is_file())
        assert probe.decoy_canary not in written

    cli.after_run = inspect
    assert probe.phase_two() == [
        "PASS control CLAUDE.md",
        "PASS artifacts control CLAUDE.md",
        "PASS isolation CLAUDE.md",
        "PASS artifacts isolation CLAUDE.md",
        "PASS isolation AGENTS.md",
        "PASS artifacts isolation AGENTS.md",
    ]
    assert len(cli.calls) == 3
    argv, kwargs = cli.calls[0]
    assert argv == [
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
        probe.probe_question,
    ]
    assert kwargs["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    command = build_agent_command(DEFAULT_AGENTS["claude-isolated"], json_output=True)
    for index, (argv, kwargs) in enumerate(cli.calls):
        if index:
            assert argv == ["bash", "-c", command]
            assert kwargs["env"]["AGENT_PROMPT"] == probe.probe_question
        assert kwargs["env"]["PROBE_KEEP"] == "inherited"
        assert (
            not {"DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER", "PULSE_COOKIE"} & kwargs["env"].keys()
        )
        assert kwargs["cwd"].name == ("agents" if index == 2 else "ctl")
        assert not kwargs["cwd"].parent.exists()
    assert len({kwargs["cwd"].parent for _, kwargs in cli.calls}) == 1
    assert [cwd for cwd, _, _, _ in checked] == [kwargs["cwd"] for _, kwargs in cli.calls]
    assert [(sid, label, count) for _, sid, label, count in checked] == [
        ("session-1", "control CLAUDE.md", 1),
        ("session-2", "isolation CLAUDE.md", 2),
        ("session-3", "isolation AGENTS.md", 3),
    ]


@pytest.mark.parametrize(
    "name",
    [
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "BASH_FUNC_claude%%",
        "BASH_FUNC_helper%%",
        "BASH_FUNC_without_suffix",
    ],
)
def test_phase_two_env_drops_shell_startup_inputs(monkeypatch, name):
    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin:/bin", name: "inherited"})
    assert probe.phase_two_env() == {"PATH": "/usr/bin:/bin"}


def test_phase_two_real_bash_ignores_startup_and_exported_function(tmp_path, monkeypatch):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "fake-binary-session",
        "result": json.dumps({"claude_md": False, "agents_md": False, "decoy": False}),
    }
    binary = binary_dir / "claude"
    binary.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(json.dumps(result)) + "\n")
    binary.chmod(0o700)
    marker = tmp_path / "startup-ran"
    startup = tmp_path / "startup.sh"
    startup.write_text("touch -- " + shlex.quote(str(marker)) + "\n")
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("PATH", f"{binary_dir}:/usr/bin:/bin")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    monkeypatch.setenv("BASH_ENV", str(startup))
    monkeypatch.setenv("BASH_FUNC_claude%%", "() { echo hijack; }")
    original_run = subprocess.run
    replies = []
    controls = []

    def run(argv, **kwargs):
        if argv[0] == "claude":
            controls.append(argv)
            control = {
                **result,
                "result": json.dumps({"claude_md": True, "agents_md": False, "decoy": False}),
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(control), "")
        assert argv[:2] == ["bash", "-c"]
        completed = original_run(["/bin/bash", *argv[1:]], **kwargs)
        replies.append(probe.parse_events(completed.stdout)[1])
        return completed

    monkeypatch.setattr(probe.subprocess, "run", run)
    lines = probe.phase_two()
    assert not marker.exists()
    assert replies == [result, result]
    assert len(controls) == 1
    assert len(lines) == 6 and all(line.startswith("PASS ") for line in lines)


@pytest.mark.parametrize("returncode", [0, 23, -9])
def test_logged_in_exit_status_reaches_evaluator(monkeypatch, tmp_path, returncode):
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "synthetic-session",
        "result": '{"claude_md":false,"agents_md":false,"decoy":false}',
    }
    monkeypatch.setattr(
        probe,
        "run_command",
        lambda *_a: subprocess.CompletedProcess([], returncode, json.dumps(result), ""),
    )
    run = probe.observe(["claude"], tmp_path, {}, authenticated=True)
    assert run.error == (f"unexpected exit status {returncode}" if returncode else "")
    lines = probe.evaluate_phase_two(run, "isolation CLAUDE.md", "claude_md", False)
    assert len(lines) == 1
    assert lines[0].startswith("FAIL " if returncode else "PASS ")


@pytest.mark.parametrize("side", [0, 1], ids=["control", "isolation"])
@pytest.mark.parametrize("returncode", [0, 1, 2, 23, 137, -9])
def test_credential_free_exit_status(monkeypatch, tmp_path, returncode, side):
    runs = observations()
    events = [runs[side].init, runs[side].result]
    monkeypatch.setattr(
        probe,
        "run_command",
        lambda *_a: subprocess.CompletedProcess([], returncode, "\n".join(map(json.dumps, events))),
    )
    run = probe.observe(["claude"], tmp_path, {}, authenticated=False)
    runs[side] = run._replace(markers=runs[side].markers)
    assert run.error == ("" if returncode == 1 else f"unexpected exit status {returncode}")
    failures = [line for line in probe.evaluate_phase_one(*runs) if line.startswith("FAIL ")]
    label = "control" if side == 0 else "isolation"
    assert failures == (
        [] if returncode == 1 else [f"FAIL {label} evidence: unexpected exit status {returncode}"]
    )


@pytest.mark.parametrize("passed", [False, True], ids=["failed", "passed"])
@pytest.mark.parametrize("offline", [False, True], ids=["full", "offline"])
def test_main_gates_logged_in_phase(cli, monkeypatch, capsys, passed, offline):
    first = [
        "PASS credential-free control",
        "PASS isolation hooks" if passed else "FAIL isolation hooks: preset retained context",
    ]
    called = []

    def phase_two():
        called.append(True)
        return ["PASS logged-in phase"]

    monkeypatch.setattr(probe, "phase_one", lambda: list(first))
    monkeypatch.setattr(probe, "phase_two", phase_two)
    assert probe.main(["--offline"] if offline else []) == (0 if passed else 1)
    assert called == ([True] if passed and not offline else [])
    expected = list(first)
    if not offline:
        expected.append(
            "PASS logged-in phase"
            if passed else "FAIL logged-in phase skipped: credential-free checks failed"
        )
    assert capsys.readouterr().out.splitlines() == expected


def snapshot(root):
    return {
        str(p.relative_to(root)): (
            p.lstat().st_size,
            p.lstat().st_mtime_ns,
            p.read_bytes() if p.is_file() else None,
        )
        for p in [root, *root.rglob("*")]
    }


@pytest.mark.parametrize("kind", ["projects", "memory", "session-env", "claude", "agents", "decoy"])
def test_post_run_artifacts_fail_without_mutation(tmp_path, kind):
    root = tmp_path / "profile"
    cwd = Path("/tmp/x/proj")
    env = {"CLAUDE_CONFIG_DIR": str(root)}
    if kind in {"projects", "memory"}:
        path = root / "projects" / "-tmp-x-proj"
        (path / "memory" if kind == "memory" else path).mkdir(parents=True)
    elif kind == "session-env":
        path = root / "session-env" / "session-test"
        path.mkdir(parents=True)
    else:
        path = root / "history.jsonl"
        root.mkdir()
        canary = {
            "claude": probe.claude_canary,
            "agents": probe.agents_canary,
            "decoy": probe.decoy_canary,
        }[kind]
        path.write_text(json.dumps({"display": canary}) + "\n")
    before = snapshot(root)
    assert any(
        line.startswith("FAIL artifacts control:") and str(path) in line
        for line in probe.check_artifacts(cwd, "session-test", env, "control")
    )
    assert snapshot(root) == before


def test_full_run_preserves_profile(cli, tmp_path, monkeypatch, capsys):
    root = tmp_path / "profile"
    root.mkdir()
    history = root / "history.jsonl"
    history.write_text("unrelated synthetic history\n")
    (root / "projects" / "existing").mkdir(parents=True)
    (root / "session-env" / "existing").mkdir(parents=True)
    (root / "unrelated.txt").write_text("keep\n")
    before = snapshot(root)
    original = os.open
    reads = []

    def guarded_open(path, flags, *args, **kwargs):
        if Path(path).is_relative_to(root):
            assert Path(path) == history
            assert flags & os.O_ACCMODE == os.O_RDONLY
            reads.append(Path(path))
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    assert probe.main([]) == 0
    assert reads == [history, history, history]
    monkeypatch.setattr(os, "open", original)
    assert snapshot(root) == before
    assert len(cli.calls) == 6
    output = capsys.readouterr().out.splitlines()
    assert output[0] == "claude --version: 2.1.304 (Claude Code) (measured: 2.1.304)"
    assert output[1:] == [
        "PASS credential-free control",
        "PASS credential-free isolation",
        *("PASS " + name for name in CHECKS),
        "PASS control CLAUDE.md",
        "PASS artifacts control CLAUDE.md",
        "PASS isolation CLAUDE.md",
        "PASS artifacts isolation CLAUDE.md",
        "PASS isolation AGENTS.md",
        "PASS artifacts isolation AGENTS.md",
    ]
    assert len({kwargs["cwd"].parent for _, kwargs in cli.calls}) == 2


@pytest.mark.parametrize("run_index", [1, 2, 3])
def test_artifacts_checked_after_every_phase_two_run(cli, tmp_path, run_index):
    root = tmp_path / "profile"
    paths = []

    def leave_artifact(_cwd, result):
        if len(cli.calls) == run_index:
            path = root / "session-env" / result["session_id"]
            path.mkdir(parents=True)
            paths.append(path)

    cli.after_run = leave_artifact
    lines = probe.phase_two()
    assert len(paths) == 1 and paths[0].exists()
    assert any(line.startswith("FAIL artifacts ") and str(paths[0]) in line for line in lines)


def test_missing_session_id_fails(tmp_path):
    for session_id in (None, "", "../escape"):
        assert probe.check_artifacts(
            Path("/tmp/x/proj"), session_id, {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
        ) == ["FAIL artifacts control: missing or invalid session_id"]


def test_default_profile_root(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".claude" / "session-env" / "session-test"
    path.mkdir(parents=True)
    assert str(path) in probe.check_artifacts(Path("/tmp/x/proj"), "session-test", {}, "control")[0]


def test_history_read_failure_is_reported(tmp_path, monkeypatch):
    history = tmp_path / "history.jsonl"
    history.write_text("synthetic history\n")

    def failed_read(*_args):
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "read", failed_read)
    assert probe.check_artifacts(
        Path("/tmp/x/proj"), "session-test", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
    ) == [f"FAIL artifacts control: cannot check {history} ([Errno 5] Input/output error)"]


@pytest.mark.parametrize("kind", ["symlink", "dangling-symlink", "fifo", "hardlink"])
def test_history_aliases_and_special_files_are_refused(tmp_path, monkeypatch, kind):
    history = tmp_path / "history.jsonl"
    target = tmp_path / "synthetic-history.txt"
    if kind != "dangling-symlink":
        target.write_text(probe.claude_canary)
    if kind in {"symlink", "dangling-symlink"}:
        history.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(history, 0o600)
    else:
        history.hardlink_to(target)

    def forbidden_read(*_args):
        pytest.fail("history must be validated before any bytes are read")

    def timed_out(*_args):
        pytest.fail("history inspection blocked on a FIFO")

    monkeypatch.setattr(os, "read", forbidden_read)
    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(2)
    try:
        with pytest.raises((OSError, ValueError)):
            probe.count_canaries(history)
        lines = probe.check_artifacts(
            tmp_path / "proj", "synthetic-session", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
        )
        assert len(lines) == 1
        assert lines[0].startswith(f"FAIL artifacts control: cannot check {history} (")
        assert "canary hit" not in lines[0]
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_history_counts_raw_bytes(tmp_path):
    history = tmp_path / "history.jsonl"
    history.write_bytes(
        b"\xff"
        + (probe.claude_canary * 2 + probe.agents_canary + probe.decoy_canary).encode()
        + b"\xfe"
    )
    assert probe.count_canaries(history) == 4
    assert probe.check_artifacts(
        tmp_path / "proj", "synthetic-session", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
    ) == [f"FAIL artifacts control: 4 canary hit(s) in {history}"]


def test_absent_history_passes(tmp_path):
    with pytest.raises(FileNotFoundError):
        probe.count_canaries(tmp_path / "history.jsonl")
    assert probe.check_artifacts(
        tmp_path / "proj", "synthetic-session", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
    ) == ["PASS artifacts control"]


@pytest.mark.parametrize(
    "flag", [os.O_RDONLY, os.O_NOFOLLOW, os.O_NONBLOCK, os.O_CLOEXEC],
    ids=["readonly", "nofollow", "nonblock", "cloexec"],
)
def test_history_open_flags(tmp_path, monkeypatch, flag):
    history = tmp_path / "history.jsonl"
    history.write_text(probe.claude_canary)
    original = os.open
    calls = []

    def checked_open(path, flags, *args, **kwargs):
        calls.append(path)
        if flag == os.O_RDONLY:
            assert flags & os.O_ACCMODE == os.O_RDONLY
        else:
            assert flags & flag
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", checked_open)
    assert probe.count_canaries(history) == 1
    assert calls == [history]


def test_history_reads_checked_descriptor_until_eof(tmp_path, monkeypatch):
    history = tmp_path / "history.jsonl"
    history.write_text(probe.claude_canary)
    original_open, original_stat, original_read = os.open, os.fstat, os.read
    opened, checked = [], []

    def replaced(path, flags):
        fd = original_open(path, flags)
        opened.append(fd)
        history.rename(tmp_path / "checked-history.txt")
        history.write_text(probe.decoy_canary * 2)
        return fd

    def checked_stat(fd):
        checked.append(fd)
        return original_stat(fd)

    def short_read(fd, size):
        assert checked == [fd] == opened
        return original_read(fd, min(size, 7))

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", replaced)
        patch.setattr(os, "fstat", checked_stat)
        patch.setattr(os, "read", short_read)
        assert probe.count_canaries(history) == 1
    assert len(opened) == 1
    with pytest.raises(OSError) as exc_info:
        original_stat(opened[0])
    assert exc_info.value.errno == errno.EBADF


def test_offline_skips_login(cli, monkeypatch):
    def forbidden():
        pytest.fail("logged-in phase attempted with --offline")

    monkeypatch.setattr(probe, "phase_two", forbidden)
    assert probe.main(["--offline"]) == 0
    assert len(cli.calls) == 3


@pytest.mark.parametrize("platform,binary", [("darwin", "/synthetic/claude"), ("linux", None)])
def test_environment_exit_two(cli, monkeypatch, platform, binary, capsys):
    monkeypatch.setattr(probe.sys, "platform", platform)
    monkeypatch.setattr(probe.shutil, "which", lambda _name: binary)
    assert probe.main(["--offline"]) == 2
    assert cli.calls == []
    assert capsys.readouterr().out == "FAIL environment: Linux and claude on PATH are required\n"


def test_failed_check_exit_one(cli, monkeypatch):
    monkeypatch.setattr(
        probe, "evaluate_phase_one", lambda *_a: ["FAIL control tools: no evidence"]
    )
    assert probe.main(["--offline"]) == 1


@pytest.mark.parametrize("phase", ["phase_one", "phase_two"])
def test_cleanup_on_exception(cli, monkeypatch, phase):
    roots = []
    original = probe.tempfile.mkdtemp

    def mkdtemp(**kwargs):
        assert kwargs == {"prefix": "djinn-probe-"}
        path = original(**kwargs)
        roots.append(Path(path))
        return path

    def fail(*_a, **_kw):
        raise OSError("synthetic I/O failure")

    monkeypatch.setattr(probe.tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(probe, "run_command", fail)
    if phase == "phase_one":
        with pytest.raises(OSError, match="synthetic I/O failure"):
            probe.phase_one()
    else:
        assert all(line.startswith("FAIL ") for line in probe.phase_two())
    assert all(not root.exists() for root in roots)


@pytest.mark.parametrize(
    "exception", [OSError("synthetic I/O failure"), subprocess.TimeoutExpired(["claude"], 120)]
)
def test_execution_failure_is_fail(cli, monkeypatch, tmp_path, exception):
    def fail(*_a):
        raise exception

    monkeypatch.setattr(probe, "run_command", fail)
    run = probe.observe(["claude"], tmp_path, {})
    assert run.error == f"run failed ({type(exception).__name__})"
    assert probe.main(["--offline"]) == 1


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("field", ["tools", "mcp_servers", "skills", "slash_commands", "agents"])
def test_missing_inventory_field_fails(side, field):
    runs = observations()
    del runs[side].init[field]
    assert any(line.startswith("FAIL ") for line in probe.evaluate_phase_one(*runs))


def test_tool_control_requires_a_list():
    runs = observations()
    runs[0].init["tools"] = "Read"
    assert "FAIL control built-in tools: synthetic canary not observed" in probe.evaluate_phase_one(
        *runs
    )


def test_names_accepts_only_lists_of_names():
    assert probe.names(None) == set()
    assert probe.names("probe") == set()
    assert probe.names(["one", {"name": "two"}, {"status": "connected"}, 42]) == {"one", "two"}


@pytest.mark.parametrize("run_index", [1, 2, 3])
@pytest.mark.parametrize("variant", ["answer", "decoy", "subtype", "is_error"])
def test_phase_two_each_run_rejects_wrong_result(cli, run_index, variant):
    labels = ["control CLAUDE.md", "isolation CLAUDE.md", "isolation AGENTS.md"]

    def corrupt(_cwd, result):
        if len(cli.calls) != run_index:
            return
        if variant in {"answer", "decoy"}:
            answer = probe.answer_json(result)
            field = (
                "decoy" if variant == "decoy" else ("agents_md" if run_index == 3 else "claude_md")
            )
            answer[field] = not answer[field]
            result["result"] = json.dumps(answer)
        elif variant == "subtype":
            result["subtype"] = "error_during_execution"
        else:
            result["is_error"] = True

    cli.after_run = corrupt
    lines = probe.phase_two()
    assert len(cli.calls) == 3
    assert len([line for line in lines if line.startswith("FAIL ")]) == 1
    assert any(line.startswith("FAIL " + labels[run_index - 1] + ":") for line in lines)


def test_phase_two_execution_error_cannot_pass():
    result = {"subtype": "success", "is_error": False, "result": '{"claude_md":true,"decoy":false}'}
    run = probe.Observation({}, result, error="run failed (TimeoutExpired)")
    assert probe.evaluate_phase_two(run, "control", "claude_md", True)[0].startswith("FAIL ")


def test_version_failure_is_a_failed_check(cli, monkeypatch):
    original = probe.run_command

    def run(argv, cwd, env):
        completed = original(argv, cwd, env)
        if argv == ["claude", "--version"]:
            completed.returncode = 1
        return completed

    monkeypatch.setattr(probe, "run_command", run)
    assert probe.main(["--offline"]) == 1


def test_artifact_checks_count_without_printing_history(tmp_path):
    history = tmp_path / "history.jsonl"
    history.write_text("synthetic private history: " + probe.claude_canary + probe.claude_canary)
    assert probe.check_artifacts(
        Path("/tmp/x/proj"), "session-test", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
    ) == [f"FAIL artifacts control: 2 canary hit(s) in {history}"]


def test_dangling_artifact_link_is_evidence(tmp_path):
    path = tmp_path / "projects" / "-tmp-x-proj"
    path.parent.mkdir()
    path.symlink_to(tmp_path / "absent")
    before = path.lstat()
    assert (
        str(path)
        in probe.check_artifacts(
            Path("/tmp/x/proj"), "session-test", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
        )[0]
    )
    assert path.lstat() == before


def test_artifact_metadata_error_is_not_a_pass(tmp_path, monkeypatch):
    path = tmp_path / "projects" / "-tmp-x-proj"
    original = os.lstat

    def denied(target, *args, **kwargs):
        if Path(target) == path:
            raise PermissionError("Permission denied")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)
    assert probe.check_artifacts(
        Path("/tmp/x/proj"), "session-test", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
    ) == [f"FAIL artifacts control: cannot check {path} (PermissionError)"]


def test_history_access_error_is_not_a_pass(tmp_path, monkeypatch):
    history = tmp_path / "history.jsonl"
    original_open = os.open

    def denied_open(path, *args, **kwargs):
        if path == history:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied_open)
    assert probe.check_artifacts(
        Path("/tmp/x/proj"), "session-test", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
    ) == [f"FAIL artifacts control: cannot check {history} ([Errno 13] Permission denied)"]


def test_post_run_artifacts_use_the_resolved_working_directory(tmp_path):
    real = tmp_path / "real" / "proj"
    real.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "real")
    root = tmp_path / "profile"
    found = root / "projects" / str(real).replace("/", "-").replace("_", "-").replace(".", "-")
    found.mkdir(parents=True)
    env = {"CLAUDE_CONFIG_DIR": str(root)}
    lines = probe.check_artifacts(alias / "proj", "session-test", env, "c")
    assert lines == [f"FAIL artifacts c: found {found}"]


def test_undecodable_output_is_a_failure_not_a_crash(tmp_path):
    script = tmp_path / "noisy"
    script.write_text("#!/bin/sh\nprintf '\\377\\376 not json'\nexit 1\n")
    script.chmod(0o755)
    run = probe.observe([str(script)], tmp_path, {"PATH": os.environ["PATH"]}, authenticated=False)
    assert run.error == "no result event"
