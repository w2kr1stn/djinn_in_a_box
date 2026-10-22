"""Credential-only persistence with ephemeral settings/history and inherited stdin."""

import json
import os
import secrets
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "assistant/entrypoint.sh"
FAKE_OPENCODE = Path(__file__).resolve().parent / "fake_opencode.py"


def prepare(tmp_path, agent):
    home = tmp_path / "home"
    home.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    names = {
        "claude": [".credentials.json"],
        "codex": ["auth.json"],
        "opencode": [],
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


@pytest.mark.parametrize("agent", ("claude", "codex"))
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


def test_signal_flushes_credentials(tmp_path, default_child_signals):
    entry, env, native, store, names = prepare(tmp_path, "codex")
    payload = (
        f"printf signal-refresh > {shlex.quote(str(native / names[0]))}; kill -TERM $PPID; sleep 1"
    )
    r = subprocess.run(
        ["bash", str(entry), "bash", "-c", payload],
        env=env,
        preexec_fn=default_child_signals,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert r.returncode == 143, r.stderr
    assert (store / names[0]).read_text() == "signal-refresh"


def credential(identity, integration="provider"):
    """One export-format entry with a runtime-generated dummy value."""
    return {
        "id": identity,
        "integrationID": integration,
        "label": "fixture",
        "active": True,
        "value": {"type": "key", "key": secrets.token_hex(8)},
    }


def prepare_opencode(tmp_path, seed=None):
    entry, env, native, store, _ = prepare(tmp_path, "opencode")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "opencode"
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_OPENCODE))}"
    wrapper.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
    wrapper.chmod(0o755)
    state = tmp_path / "database.json"
    log = tmp_path / "calls.log"
    env = {
        **env,
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        "FAKE_OPENCODE_STATE": str(state),
        "FAKE_OPENCODE_LOG": str(log),
        "OPENCODE_CONFIG_CONTENT": "agent-inline-config",
        "OPENCODE_CONFIG_DIR": str(tmp_path / "agent-config-dir"),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
    }
    if seed is not None:
        (store / "seed.json").write_text(json.dumps(seed))
    return entry, env, store, state, log


def run_entry(entry, env, payload, **extra):
    return subprocess.run(
        ["bash", str(entry), "bash", "-c", payload],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        **extra,
    )


def calls(log):
    return log.read_text().splitlines() if log.exists() else []


def test_opencode_imports_seed_and_exports_final(tmp_path):
    seed = [credential("cred_one"), credential("cred_two", "other")]
    entry, env, store, state, log = prepare_opencode(tmp_path, seed)
    marker = tmp_path / "child-env"
    payload = f'printf "%s" "$OPENCODE_CONFIG_CONTENT" > {shlex.quote(str(marker))}'
    r = run_entry(entry, env, payload)
    assert r.returncode == 0, r.stderr
    # The fake enforces the isolation: empty config home as cwd, no OPENCODE_CONFIG*, disable vars.
    assert "isolation violated" not in r.stderr and "Could not" not in r.stderr
    assert [line for line in calls(log) if line.startswith("fake ")] == [
        "fake import",
        "fake export",
    ]
    # The seed reached the import through stdin, the export carries what the import stored.
    assert {item["id"] for item in json.loads(state.read_text())["entries"]} == {
        "cred_one",
        "cred_two",
    }
    final = store / "final.json"
    assert {item["id"] for item in json.loads(final.read_text())} == {"cred_one", "cred_two"}
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert sorted(path.name for path in store.iterdir() if path.name.startswith(".")) == []
    assert json.loads((store / "seed.json").read_text()) == seed
    # The agent child keeps its own configuration.
    assert marker.read_text() == "agent-inline-config"


def test_opencode_without_seed_exports_only(tmp_path):
    entry, env, store, _, log = prepare_opencode(tmp_path)
    r = run_entry(entry, env, "exit 0")
    assert r.returncode == 0, r.stderr
    assert [line for line in calls(log) if line.startswith("fake ")] == ["fake export"]
    assert json.loads((store / "final.json").read_text()) == []


def test_opencode_symlinked_seed_is_ignored(tmp_path):
    entry, env, store, state, log = prepare_opencode(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps([credential("cred_linked")]))
    (store / "seed.json").symlink_to(target)
    r = run_entry(entry, env, "exit 0")
    assert r.returncode == 0, r.stderr
    assert "fake import" not in calls(log)
    assert json.loads((store / "final.json").read_text()) == []
    assert not state.exists() or json.loads(state.read_text())["entries"] == []


def test_opencode_failed_export_leaves_no_final_and_no_temporary(tmp_path):
    entry, env, store, _, _ = prepare_opencode(tmp_path)
    noise = tmp_path / "noise.txt"
    sentinel = "sentinel-" + secrets.token_hex(8)
    noise.write_text(sentinel)
    r = run_entry(entry, {**env, "FAKE_FAIL_AT": "1", "FAKE_STDERR_FILE": str(noise)}, "exit 0")
    assert r.returncode == 1
    assert "Could not export" in r.stderr
    assert sentinel not in r.stdout + r.stderr
    assert list(store.glob("final.json")) == []
    assert sorted(path.name for path in store.iterdir() if path.name.startswith(".")) == []


def test_opencode_failed_import_does_not_stop_the_audit(tmp_path):
    entry, env, store, _, log = prepare_opencode(tmp_path, [credential("cred_one")])
    marker = tmp_path / "ran"
    noise = tmp_path / "noise.txt"
    sentinel = "sentinel-" + secrets.token_hex(8)
    noise.write_text(sentinel)
    r = run_entry(
        entry,
        {**env, "FAKE_FAIL_AT": "1", "FAKE_STDERR_FILE": str(noise)},
        f"touch {shlex.quote(str(marker))}",
    )
    assert r.returncode == 0, r.stderr
    assert "Could not import" in r.stderr and marker.exists()
    assert sentinel not in r.stdout + r.stderr
    assert (store / "final.json").exists()


@pytest.mark.parametrize(("signal_name", "status"), (("TERM", 143), ("INT", 130), ("HUP", 129)))
def test_opencode_signal_still_exports(tmp_path, signal_name, status, default_child_signals):
    entry, env, store, _, _ = prepare_opencode(tmp_path, [credential("cred_one")])
    r = run_entry(
        entry, env, f"kill -{signal_name} $PPID; sleep 1", preexec_fn=default_child_signals
    )
    assert r.returncode == status, r.stderr
    assert {item["id"] for item in json.loads((store / "final.json").read_text())} == {"cred_one"}


def test_opencode_keeps_child_status(tmp_path):
    entry, env, store, _, _ = prepare_opencode(tmp_path)
    r = run_entry(entry, env, "exit 23")
    assert r.returncode == 23 and (store / "final.json").exists()


@pytest.mark.parametrize("agent", ("claude", "codex"))
def test_other_agents_never_touch_the_database_path(tmp_path, agent):
    entry, env, native, store, _ = prepare(tmp_path, agent)
    log = tmp_path / "calls.log"
    (store / "seed.json").write_text(json.dumps([credential("cred_one")]))
    env = {**env, "FAKE_OPENCODE_LOG": str(log)}
    r = run_entry(entry, env, "exit 0")
    assert r.returncode == 0, r.stderr
    assert not log.exists() and not (store / "final.json").exists()


def test_opencode_legacy_credential_files_are_neither_copied_nor_written_back(tmp_path):
    entry, env, store, _, _ = prepare_opencode(tmp_path)
    native = Path(env["HOME"]) / ".local/share/opencode"
    for name in ("auth.json", "mcp-auth.json"):
        (store / name).write_text('{"token":"before"}')
    payload = "; ".join(
        f"test ! -e {shlex.quote(str(native / name))}" for name in ("auth.json", "mcp-auth.json")
    )
    r = run_entry(entry, env, payload)
    assert r.returncode == 0, r.stderr
    assert (store / "auth.json").read_text() == '{"token":"before"}'
    assert (store / "mcp-auth.json").read_text() == '{"token":"before"}'


def test_opencode_export_never_writes_through_a_planted_name(tmp_path):
    entry, env, store, _, _ = prepare_opencode(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    for name in (".final.tmp", "final.tmp", ".final"):
        (store / name).symlink_to(victim)
    r = run_entry(entry, env, "exit 0")
    assert r.returncode == 0, r.stderr
    assert victim.read_text() == "untouched"
    assert json.loads((store / "final.json").read_text()) == []


def test_opencode_export_never_lands_in_a_directory_planted_as_final(tmp_path):
    entry, env, store, _, _ = prepare_opencode(tmp_path, [credential("cred_one")])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (store / "final.json").symlink_to(elsewhere)
    r = run_entry(entry, env, "exit 0")
    assert r.returncode == 0, r.stderr
    assert list(elsewhere.iterdir()) == []
    final = store / "final.json"
    assert not final.is_symlink() and final.is_file()
    assert [item["id"] for item in json.loads(final.read_text())] == ["cred_one"]


def test_opencode_export_fails_on_a_real_directory_named_final(tmp_path):
    entry, env, store, _, _ = prepare_opencode(tmp_path, [credential("cred_one")])
    (store / "final.json").mkdir()
    r = run_entry(entry, env, "exit 0")
    assert r.returncode == 1
    assert "Could not export" in r.stderr
    assert list((store / "final.json").iterdir()) == []
    assert sorted(path.name for path in store.iterdir() if path.name.startswith(".")) == []
