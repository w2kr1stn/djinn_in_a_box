"""Credential-only persistence with ephemeral settings/history and inherited stdin."""

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "assistant/entrypoint.sh"


def prepare(tmp_path, agent):
    home = tmp_path / "home"
    home.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    names = {
        "claude": [".credentials.json"],
        "codex": ["auth.json"],
        "opencode": ["auth.json", "mcp-auth.json"],
    }[agent]
    for name in names:
        (store / name).write_text('{"token":"before"}')
    (store / "claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": "fixture@example.invalid"},
                "permissionMode": "bypassPermissions",
                "hooks": {"broken": True},
            }
        )
    )
    (store / "settings.json").write_text("broken settings must not load")
    text = ENTRYPOINT.read_text().replace(
        "store=/run/djinn-credentials", f"store={shlex.quote(str(store))}"
    )
    entry = tmp_path / "entry.sh"
    entry.write_text(text)
    env = {**os.environ, "HOME": str(home), "DJINN_ASSISTANT_AGENT": agent}
    native = (
        home / {"claude": ".claude", "codex": ".codex", "opencode": ".local/share/opencode"}[agent]
    )
    return entry, env, native, store, names


@pytest.mark.parametrize("agent", ("claude", "codex", "opencode"))
def test_credentials_refresh_only_and_stdin(tmp_path, agent):
    entry, env, native, store, names = prepare(tmp_path, agent)
    # A real child reads stdin, then atomically replaces its credential file.
    payload = (
        'read -r value; test "$value" = tty-input; '
        f'test ! -e {shlex.quote(str(native / "settings.json"))}; '
    )
    for name in names:
        path = shlex.quote(str(native / name))
        payload += f"test -f {path}; printf refreshed > {path}.tmp; mv {path}.tmp {path}; "
    payload += f"touch {shlex.quote(str(native / 'history.jsonl'))}; exit 23"
    before_metadata = (store / "claude.json").read_bytes()
    r = subprocess.run(
        ["bash", str(entry), "bash", "-c", payload],
        env=env,
        input="tty-input\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 23, r.stderr
    for name in names:
        assert (store / name).read_text() == "refreshed"
    assert not (store / "history.jsonl").exists()
    assert (store / "claude.json").read_bytes() == before_metadata
    if agent == "claude":
        assert json.loads((Path(env["HOME"]) / ".claude.json").read_text()) == {
            "oauthAccount": {"emailAddress": "fixture@example.invalid"}
        }


def test_redirected_native_credential_is_refused(tmp_path):
    entry, env, native, store, names = prepare(tmp_path, "codex")
    target = tmp_path / "target"
    target.write_text("unrelated")
    credential = shlex.quote(str(native / names[0]))
    payload = f"rm {credential}; ln -s {shlex.quote(str(target))} {credential}"
    r = subprocess.run(
        ["bash", str(entry), "bash", "-c", payload],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 1 and "Refusing redirected" in r.stderr
    assert target.read_text() == "unrelated"
    assert (store / names[0]).read_text() == '{"token":"before"}'


def test_signal_flushes_credentials(tmp_path):
    entry, env, native, store, names = prepare(tmp_path, "codex")
    payload = (
        f"printf signal-refresh > {shlex.quote(str(native / names[0]))}; kill -TERM $PPID; sleep 1"
    )
    r = subprocess.run(
        ["bash", str(entry), "bash", "-c", payload],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert r.returncode == 143, r.stderr
    assert (store / names[0]).read_text() == "signal-refresh"
