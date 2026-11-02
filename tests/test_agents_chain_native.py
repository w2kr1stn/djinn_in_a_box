"""Pin how Claude Code, Codex and OpenCode load the AGENTS.md chain, against a loopback fake model.

Each installed CLI runs in a private HOME with a minimal allowlisted environment and talks only
to an in-process fake endpoint on 127.0.0.1. A tool test is skipped when its CLI is absent.
Re-run this module after bumping an agent version.
"""

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import tomllib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SCRIPT = REPO_ROOT / "scripts" / "djinn-agents-chain.py"
CODEX_CONFIG = REPO_ROOT / "scripts" / "codex-config.toml"
CLAUDE_SEED = REPO_ROOT / "templates" / "seed" / "config" / "claude" / "settings.json"

CHAIN = ("MARKER-ROOT-A1", "MARKER-GROUP-B2", "MARKER-REPO-C3", "MARKER-SUB-D4")
CLAUDE_MD_MARKER = "MARKER-RCLAUDE-E2"
RUN_TIMEOUT = 180

BASE_ENV_KEYS = {
    "PATH", "LANG", "TMPDIR", "HOME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
}  # fmt: skip
EXTRA_ENV_KEYS = {
    "CODEX_HOME", "CODEX_SQLITE_HOME", "DJINN_FAKE_KEY", "DJINN_WORKSPACE_TARGET",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "DISABLE_AUTOUPDATER",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
}  # fmt: skip

SUMMARY_TEMPLATE = (
    "## Objective\n- say hi\n\n## Requirements\n- (none)\n\n## Decisions\n- (none)\n\n"
    "## Work State\n### Completed\n- (none)\n\n### Active\n- (none)\n\n### Blocked\n- (none)\n\n"
    "## Next Move\n1. (none)\n\n## Relevant Files\n(none)\n\n## Important Context\n- FAKE-SUMMARY\n"
)


def wants_summary(body: dict[str, Any]) -> bool:
    last = json.dumps(body["messages"][-1]).lower()
    return "summary template" in last or "summarize the conversation" in last


# --- recording fake model endpoint -------------------------------------------------------------


class FakeModel(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.lock = threading.Lock()
        self.log: list[tuple[str, Any]] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def requests(self, suffix: str) -> list[Any]:
        with self.lock:
            return [body for path, body in self.log if path.endswith(suffix)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def reply_json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def reply_sse(self, events: list[dict[str, Any] | str], named: bool = True) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        for data in events:
            payload = data if isinstance(data, str) else json.dumps(data)
            head = f"event: {data['type']}\n" if named and isinstance(data, dict) else ""
            self.wfile.write(f"{head}data: {payload}\n\n".encode())
            self.wfile.flush()

    def do_GET(self) -> None:
        if self.path.split("?")[0].rstrip("/").endswith("/models"):
            self.reply_json({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
        else:
            self.reply_json({}, 404)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body: dict[str, Any] = json.loads(raw) if raw else {}
        path = self.path.split("?")[0]
        server = cast(FakeModel, self.server)
        with server.lock:
            server.log.append((path, body))
        if path.endswith("/responses"):
            self.codex_reply(body)
        elif path.endswith("/messages/count_tokens"):
            self.reply_json({"input_tokens": 100})
        elif path.endswith("/messages"):
            self.claude_reply(body)
        elif path.endswith("/chat/completions"):
            self.opencode_reply(body)
        else:
            self.reply_json({}, 404)

    def codex_reply(self, body: dict[str, Any]) -> None:
        text = json.dumps(body)
        total = 120
        if "CONTEXT CHECKPOINT COMPACTION" in text:
            item: dict[str, Any] = message_item("FAKE-SUMMARY")
        elif "FAKE-SUMMARY" in text or any(
            entry.get("type") == "function_call_output" for entry in body.get("input", [])
        ):
            item = message_item("FAKE-DONE")
        else:
            total = 10_000_000
            item = {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": "true"}),
            }
        usage = {
            "input_tokens": total - 20,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": total,
        }
        done = {"type": "response.output_item.done", "item": item}
        completed = {"id": "resp_1", "usage": usage}
        self.reply_sse(
            [
                {"type": "response.created", "response": {"id": "resp_1"}},
                done,
                {"type": "response.completed", "response": completed},
            ]
        )

    def claude_reply(self, body: dict[str, Any]) -> None:
        text = "FAKE-REPLY"
        message: dict[str, Any] = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "fake-model"),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 0},
        }
        if not body.get("stream"):
            message |= {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
            self.reply_json(message)
            return
        block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": text}
        end = {"stop_reason": "end_turn", "stop_sequence": None}
        self.reply_sse(
            [
                {"type": "message_start", "message": message},
                {"type": "content_block_start", "index": 0, "content_block": block},
                {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": end, "usage": {"output_tokens": 5}},
                {"type": "message_stop"},
            ]
        )

    def opencode_reply(self, body: dict[str, Any]) -> None:
        messages = body["messages"]
        system = json.dumps([m for m in messages if m["role"] == "system"])
        prompt, delta = 100, {"role": "assistant", "content": "FAKE-DONE"}
        finish = "stop"
        if "You name conversations" in system:
            delta["content"] = "Title"
        elif wants_summary(body):
            delta["content"] = SUMMARY_TEMPLATE
        elif "FAKE-SUMMARY" in json.dumps(body) or any(m["role"] == "tool" for m in messages):
            pass
        else:
            prompt, finish = 190_000, "tool_calls"
            call = {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "shell",
                    "arguments": json.dumps({"command": "true", "description": "probe"}),
                },
            }
            delta = {"role": "assistant", "tool_calls": [call]}
        base = {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "fake-model",
        }
        usage = {"prompt_tokens": prompt, "completion_tokens": 5, "total_tokens": prompt + 5}
        choice = {"index": 0, "delta": delta, "finish_reason": None}
        last: dict[str, Any] = {"index": 0, "delta": {}, "finish_reason": finish}
        self.reply_sse(
            [
                {**base, "choices": [choice]},
                {**base, "choices": [last], "usage": usage},
                "[DONE]",
            ],
            named=False,
        )


def message_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "id": "msg_1",
        "content": [{"type": "output_text", "text": text}],
    }


@pytest.fixture
def fake() -> Iterator[FakeModel]:
    server = FakeModel()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


# --- fixture tree and isolated child processes -------------------------------------------------


@pytest.fixture
def root() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="dac-"))
    (path / "tmp").mkdir()
    for directory, marker in {
        "": "MARKER-ROOT-A1 workspace root rule.",
        "group": "MARKER-GROUP-B2 group rule.",
        "group/repo": "MARKER-REPO-C3 repository rule.",
        "group/repo/sub": "MARKER-SUB-D4 working directory rule.",
    }.items():
        target = path / "home" / "ws" / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / "AGENTS.md").write_text(marker + "\n")
    subprocess.run(["git", "init", "-q"], cwd=repo_dir(path), env=child_env(path), check=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def workspace_dir(root: Path) -> Path:
    return root / "home" / "ws"


def repo_dir(root: Path) -> Path:
    return workspace_dir(root) / "group" / "repo"


def work_dir(root: Path) -> Path:
    return repo_dir(root) / "sub"


def child_env(root: Path, **extra: str) -> dict[str, str]:
    home = root / "home"
    return {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "TMPDIR": str(root / "tmp"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        **extra,
    }


def codex_env(root: Path) -> dict[str, str]:
    codex_home = root / "home" / ".codex"
    return child_env(
        root,
        CODEX_HOME=str(codex_home),
        CODEX_SQLITE_HOME=str(codex_home / "sqlite"),
        DJINN_FAKE_KEY="dummy",
        DJINN_WORKSPACE_TARGET=str(workspace_dir(root)),
    )


def claude_env(root: Path, fake: FakeModel) -> dict[str, str]:
    return child_env(
        root,
        ANTHROPIC_BASE_URL=fake.url,
        ANTHROPIC_API_KEY="dummy",
        DISABLE_AUTOUPDATER="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )


def run_cli(argv: list[str], env: dict[str, str], cwd: Path, stdin: str | None = None) -> str:
    """Run one CLI in its own process group; only that group is ever signalled."""
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        out, err = proc.communicate(input=stdin, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(5)
        raise
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    assert proc.returncode == 0, err[-2000:]
    return out


def counts(payload: object, markers: tuple[str, ...] = CHAIN) -> dict[str, int]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {marker: text.count(marker) for marker in markers}


def assert_once(payload: object, markers: tuple[str, ...] = CHAIN) -> None:
    assert counts(payload, markers) == dict.fromkeys(markers, 1)


# --- tests -------------------------------------------------------------------------------------


def test_child_env_drops_outer_profile_redirections(
    root: Path, fake: FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = {
        name: str(root / "outer" / name.lower())
        for name in (
            "CODEX_SQLITE_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME",
            "OPENCODE_CONFIG", "ANTHROPIC_API_KEY", "HTTPS_PROXY",
        )
    }  # fmt: skip
    for name, value in outer.items():
        monkeypatch.setenv(name, value)
    for env in (child_env(root), codex_env(root), claude_env(root, fake)):
        assert set(env) <= BASE_ENV_KEYS | EXTRA_ENV_KEYS
        assert not set(outer.values()) & set(env.values())


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI is not installed")
def test_codex_native_rule_stops_at_the_git_root(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(root / "outer" / "sqlite"))
    (root / "home" / ".codex").mkdir()
    out = run_cli(["codex", "debug", "prompt-input"], codex_env(root), work_dir(root))
    found = counts(out)
    assert found == {
        "MARKER-ROOT-A1": 0,
        "MARKER-GROUP-B2": 0,
        "MARKER-REPO-C3": 1,
        "MARKER-SUB-D4": 1,
    }
    assert not (root / "outer").exists()


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI is not installed")
def test_codex_hook_adds_the_chain_once_at_start_and_after_compaction(
    root: Path, fake: FakeModel
) -> None:
    text = CODEX_CONFIG.read_text()
    repo_hook = tomllib.loads(text)
    command = repo_hook["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    written = text.replace(command, f"python3 {shlex.quote(str(HOOK_SCRIPT))}")
    installed = tomllib.loads(written)
    for table in (repo_hook, installed):
        table["hooks"]["SessionStart"][0]["hooks"][0].pop("command")
    assert installed == repo_hook
    codex_home = root / "home" / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(written)
    provider = (
        f'{{name="fake", base_url="{fake.url}/v1", wire_api="responses", env_key="DJINN_FAKE_KEY"}}'
    )
    argv = [
        "codex", "exec", "--ephemeral", "--skip-git-repo-check", "--json",
        "--dangerously-bypass-hook-trust", "--dangerously-bypass-approvals-and-sandbox",
        "-c", "model_provider=fake", "-c", f"model_providers.fake={provider}",
        "-c", "model=fake-model", "-c", "model_auto_compact_token_limit=5000",
        "-c", "model_context_window=200000", "say hi",
    ]  # fmt: skip
    run_cli(argv, codex_env(root), work_dir(root))
    requests = fake.requests("/responses")
    assert any("CONTEXT CHECKPOINT COMPACTION" in json.dumps(body) for body in requests)
    assert_once(requests[0])
    assert_once(requests[-1])


def last_user_text(body: dict[str, Any]) -> str:
    for message in reversed(body["messages"]):
        if message["role"] == "user":
            content = message["content"]
            if isinstance(content, str):
                return content
            return [block["text"] for block in content if block.get("type") == "text"][-1]
    return ""


def claude_setup(root: Path, settings: dict[str, Any]) -> None:
    (repo_dir(root) / "CLAUDE.md").write_text(f"{CLAUDE_MD_MARKER} repo claude file.\n")
    (root / "home" / ".claude").mkdir()
    (root / "home" / ".claude" / "settings.json").write_text(json.dumps(settings))


CLAUDE_ARGV = [
    "claude",
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--model",
    "claude-sonnet-5-5",
]


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI is not installed")
def test_claude_seed_option_keeps_the_chain_beside_a_claude_md(root: Path, fake: FakeModel) -> None:
    seed = json.loads(CLAUDE_SEED.read_text())
    claude_setup(root, {"pluginConfigs": seed["pluginConfigs"]})
    lines = (
        json.dumps({"type": "user", "message": {"role": "user", "content": t}})
        for t in ("say hi", "/compact", "say again")
    )
    argv = [*CLAUDE_ARGV, "--input-format", "stream-json"]
    out = run_cli(argv, claude_env(root, fake), work_dir(root), stdin="\n".join(lines) + "\n")
    assert "compact_boundary" in out
    requests = fake.requests("/messages")
    first = next(body for body in requests if last_user_text(body) == "say hi")
    last = requests[-1]
    assert last_user_text(last) == "say again"
    for body in (first, last):
        assert_once(body, (*CHAIN, CLAUDE_MD_MARKER))


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI is not installed")
def test_claude_default_mode_drops_the_chain_beside_a_claude_md(
    root: Path, fake: FakeModel
) -> None:
    claude_setup(root, {})
    run_cli([*CLAUDE_ARGV, "say hi"], claude_env(root, fake), work_dir(root))
    body = next(body for body in fake.requests("/messages") if last_user_text(body) == "say hi")
    assert counts(body, (*CHAIN, CLAUDE_MD_MARKER)) == {
        "MARKER-ROOT-A1": 0, "MARKER-GROUP-B2": 0, "MARKER-REPO-C3": 0, "MARKER-SUB-D4": 0,
        CLAUDE_MD_MARKER: 1,
    }  # fmt: skip


@pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode CLI is not installed")
def test_opencode_loads_the_chain_natively_once_before_and_after_compaction(
    root: Path, fake: FakeModel
) -> None:
    config = root / "home" / ".config" / "opencode"
    config.mkdir(parents=True)
    model = {
        "name": "Fake Model",
        "capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
        "limit": {"context": 200000, "output": 4096},
    }
    provider = {
        "name": "Fake",
        "package": "@opencode/ai/providers/openai-compatible",
        "settings": {"baseURL": f"{fake.url}/v1", "apiKey": "{env:DJINN_FAKE_KEY}"},
        "models": {"fake-model": model},
    }
    (config / "opencode.json").write_text(json.dumps({"providers": {"fake": provider}}))
    argv = [
        "opencode",
        "run",
        "--standalone",
        "--auto",
        "--format",
        "json",
        "-m",
        "fake/fake-model",
        "say hi",
    ]
    run_cli(argv, child_env(root, DJINN_FAKE_KEY="dummy"), work_dir(root))
    requests = [
        body
        for body in fake.requests("/chat/completions")
        if "You name conversations"
        not in json.dumps([m for m in body["messages"] if m["role"] == "system"])
    ]
    assert any(wants_summary(body) for body in requests)
    assert_once(requests[0])
    assert_once(requests[-1])
