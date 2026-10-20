"""Synthetic CLI events only; real Claude and credentials are never used here."""

import copy
import importlib.util
import io
import json
import os
import shlex
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
        assert env.get("AGENT_PROMPT", argv[-1]) == probe.QUESTION
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
    assert probe.QUESTION == (
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
        assert (root / "ctl" / "CLAUDE.md").read_text() == probe.CLAUDE_CANARY + "\n"
        assert (root / "agents" / "AGENTS.md").read_text() == probe.AGENTS_CANARY + "\n"
        assert probe.DECOY not in "".join(p.read_text() for p in root.rglob("*") if p.is_file())

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
        probe.QUESTION,
    ]
    assert kwargs["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    command = build_agent_command(DEFAULT_AGENTS["claude-isolated"], json_output=True)
    for index, (argv, kwargs) in enumerate(cli.calls):
        if index:
            assert argv == ["bash", "-c", command]
            assert kwargs["env"]["AGENT_PROMPT"] == probe.QUESTION
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
            "claude": probe.CLAUDE_CANARY,
            "agents": probe.AGENTS_CANARY,
            "decoy": probe.DECOY,
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
    original = Path.open
    reads = []

    def guarded_open(path, *args, **kwargs):
        if path.is_relative_to(root):
            assert path == history
            assert not args or args[0] == "r"
            assert kwargs.get("mode", "r") == "r"
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    assert probe.main([]) == 0
    assert reads == [history, history, history]
    monkeypatch.setattr(Path, "open", original)
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


def test_history_read_failure_is_reported(tmp_path):
    history = tmp_path / "history.jsonl"
    history.write_bytes(b"\xff")
    assert probe.check_artifacts(
        Path("/tmp/x/proj"), "session-test", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
    ) == [f"FAIL artifacts control: cannot check {history} (UnicodeDecodeError)"]


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
    history.write_text("synthetic private history: " + probe.CLAUDE_CANARY + probe.CLAUDE_CANARY)
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
    original_stat, original_open = Path.stat, Path.open

    def denied_stat(path, *args, **kwargs):
        if path == history:
            raise PermissionError("Permission denied")
        return original_stat(path, *args, **kwargs)

    def denied_open(path, *args, **kwargs):
        if path == history:
            raise PermissionError("Permission denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    monkeypatch.setattr(Path, "open", denied_open)
    assert probe.check_artifacts(
        Path("/tmp/x/proj"), "session-test", {"CLAUDE_CONFIG_DIR": str(tmp_path)}, "control"
    ) == [f"FAIL artifacts control: cannot check {history} (PermissionError)"]
