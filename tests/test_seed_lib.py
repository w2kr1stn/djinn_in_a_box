"""Tests for the sourceable seed synchronization library."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "seed-lib.sh"
OUTPUT_LIB = ROOT / "scripts" / "output-lib.sh"
RAW_UI_MARKERS = ("✓", "⚠", "✗", "ℹ", "↻", "⊕", "✕")


def assert_plain_startup_output(output: str) -> None:
    assert "\x1b[" not in output
    for marker in RAW_UI_MARKERS:
        assert marker not in output


def run_seed_lib(tmp_path: Path, command: str) -> subprocess.CompletedProcess[str]:
    jq = shutil.which("jq")
    assert jq is not None
    zsh = shutil.which("zsh")
    assert zsh is not None

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "NO_COLOR": "1",
        "PATH": f"{Path(jq).parent}:/usr/bin:/bin",
    }
    env.pop("DJINN_FORCE_UI_COLOR", None)

    return subprocess.run(
        [
            zsh,
            "-c",
            "set -euo pipefail; "
            f"source {shlex.quote(str(OUTPUT_LIB))}; "
            f"source {shlex.quote(str(SCRIPT))}; "
            f"SETTINGS_COPY_HELPER={shlex.quote(str(ROOT / 'scripts/settings-copy.py'))}; "
            f"typeset -A djinn_checkpoint_warned; {command}",
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        timeout=20,
    )


def write_json(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_minimal_claude_seed_without_skills_dir_merges_settings(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "AGENTS.md").write_text("Starter instructions.\n", encoding="utf-8")
    write_json(seed_dir / "settings.json", {"permissions": {"allow": ["Read"]}})

    target_settings = tmp_path / ".claude" / "settings.json"
    target_settings.parent.mkdir()

    result = run_seed_lib(
        tmp_path,
        "claude_settings_merge "
        f"{shlex.quote(str(seed_dir))} {shlex.quote(str(target_settings))}; "
        f"jq -e . {shlex.quote(str(target_settings))} >/dev/null",
    )

    assert result.returncode == 0, result.stderr
    assert "seed incomplete" not in result.stderr
    assert json.loads(target_settings.read_text(encoding="utf-8")) == {
        "permissions": {"allow": ["Read"]}
    }


def test_claude_settings_local_overlay_wins_over_baseline(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "AGENTS.md").write_text("Starter instructions.\n", encoding="utf-8")
    write_json(
        seed_dir / "settings.json",
        {
            "env": {"mode": "baseline", "kept": True},
            "enabledPlugins": {"baseline-plugin": True},
        },
    )
    write_json(
        seed_dir / "settings.local.json",
        {
            "env": {"mode": "local"},
            "enabledPlugins": {"local-plugin": True},
        },
    )

    target_settings = tmp_path / ".claude" / "settings.json"
    target_settings.parent.mkdir()

    result = run_seed_lib(
        tmp_path,
        "claude_settings_merge "
        f"{shlex.quote(str(seed_dir))} {shlex.quote(str(target_settings))}",
    )

    assert result.returncode == 0, result.stderr
    assert_plain_startup_output(result.stderr)
    assert "[merge] settings.json (baseline + local)" in result.stderr
    merged = json.loads(target_settings.read_text(encoding="utf-8"))
    assert merged == {
        "env": {"mode": "local", "kept": True},
        "enabledPlugins": {"local-plugin": True},
    }


def test_claude_managed_hooks_win_after_local_overlay(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "AGENTS.md").write_text("Starter instructions.\n", encoding="utf-8")
    write_json(
        seed_dir / "settings.json",
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"command": "baseline-start"}]}],
                "PreToolUse": [{"hooks": [{"command": "baseline-security"}]}],
                "Stop": [{"hooks": [{"command": "baseline-ready"}]}],
                "PostToolUse": [{"hooks": [{"command": "baseline-post"}]}],
            },
            "env": {"baseline": True},
        },
    )
    write_json(
        seed_dir / "settings.local.json",
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"command": "local-start"}]}],
                "PreToolUse": [{"hooks": [{"command": "local-security"}]}],
                "Stop": [{"hooks": [{"command": "local-ready"}]}],
                "PostToolUse": [{"hooks": [{"command": "local-post"}]}],
                "Notification": [{"hooks": [{"command": "local-notify"}]}],
            },
            "env": {"local": True},
        },
    )

    target_settings = tmp_path / ".claude" / "settings.json"
    target_settings.parent.mkdir()

    result = run_seed_lib(
        tmp_path,
        "claude_settings_merge "
        f"{shlex.quote(str(seed_dir))} {shlex.quote(str(target_settings))}",
    )

    assert result.returncode == 0, result.stderr
    merged = json.loads(target_settings.read_text(encoding="utf-8"))
    assert merged["hooks"]["SessionStart"] == [{"hooks": [{"command": "baseline-start"}]}]
    assert merged["hooks"]["PreToolUse"] == [{"hooks": [{"command": "baseline-security"}]}]
    assert merged["hooks"]["Stop"] == [{"hooks": [{"command": "baseline-ready"}]}]
    assert merged["hooks"]["PostToolUse"] == [{"hooks": [{"command": "local-post"}]}]
    assert merged["hooks"]["Notification"] == [{"hooks": [{"command": "local-notify"}]}]
    assert merged["env"] == {"baseline": True, "local": True}


def test_existing_target_settings_are_not_clobbered_without_local_overlay(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "AGENTS.md").write_text("Starter instructions.\n", encoding="utf-8")
    write_json(seed_dir / "settings.json", {"baseline": True})

    target_settings = tmp_path / ".claude" / "settings.json"
    target_settings.parent.mkdir()
    original_bytes = b'{\n  "user": true,\n  "theme": "kept"\n}\n'
    target_settings.write_bytes(original_bytes)

    result = run_seed_lib(
        tmp_path,
        "claude_settings_merge "
        f"{shlex.quote(str(seed_dir))} {shlex.quote(str(target_settings))}",
    )

    assert result.returncode == 0, result.stderr
    assert target_settings.read_bytes() == original_bytes


def test_malformed_local_overlay_fails_loud_and_keeps_existing_settings(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "AGENTS.md").write_text("Starter instructions.\n", encoding="utf-8")
    write_json(seed_dir / "settings.json", {"baseline": True})
    (seed_dir / "settings.local.json").write_text("{not valid json", encoding="utf-8")

    target_settings = tmp_path / ".claude" / "settings.json"
    target_settings.parent.mkdir()
    original_bytes = b'{\n  "user": true\n}\n'
    target_settings.write_bytes(original_bytes)

    result = run_seed_lib(
        tmp_path,
        "claude_settings_merge "
        f"{shlex.quote(str(seed_dir))} {shlex.quote(str(target_settings))}",
    )

    assert result.returncode == 0, result.stderr  # startup must continue
    assert "settings merge failed" in result.stderr
    assert "settings.local.json" in result.stderr  # names the offending file
    assert target_settings.read_bytes() == original_bytes  # existing kept
    assert not (target_settings.parent / "settings.json.tmp").exists()  # no litter


def test_malformed_local_overlay_on_fresh_store_still_initialises_baseline(
    tmp_path: Path,
) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "AGENTS.md").write_text("Starter instructions.\n", encoding="utf-8")
    write_json(seed_dir / "settings.json", {"baseline": True})
    (seed_dir / "settings.local.json").write_text("{not valid json", encoding="utf-8")

    target_settings = tmp_path / ".claude" / "settings.json"
    target_settings.parent.mkdir()  # fresh persistent settings store: no settings.json yet

    result = run_seed_lib(
        tmp_path,
        "claude_settings_merge "
        f"{shlex.quote(str(seed_dir))} {shlex.quote(str(target_settings))}",
    )

    assert result.returncode == 0, result.stderr
    assert "settings merge failed" in result.stderr
    # A fresh persistent settings store must never end up settings-less: baseline fallback applies.
    assert json.loads(target_settings.read_text(encoding="utf-8")) == {"baseline": True}


def test_malformed_baseline_is_named_and_never_installed(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "AGENTS.md").write_text("Starter instructions.\n", encoding="utf-8")
    # The BASELINE is malformed (the hand-edited file); the overlay is valid.
    (seed_dir / "settings.json").write_text("{trailing comma,}", encoding="utf-8")
    write_json(seed_dir / "settings.local.json", {"local": True})

    target_settings = tmp_path / ".claude" / "settings.json"
    target_settings.parent.mkdir()  # fresh persistent settings store

    result = run_seed_lib(
        tmp_path,
        "claude_settings_merge "
        f"{shlex.quote(str(seed_dir))} {shlex.quote(str(target_settings))}",
    )

    assert result.returncode == 0, result.stderr
    assert "settings merge failed" in result.stderr
    # The message must name the ACTUAL offender (the baseline), not the overlay.
    assert str(seed_dir / "settings.json") in result.stderr
    assert str(seed_dir / "settings.local.json") not in result.stderr
    # A malformed baseline must never be installed as the live settings.
    assert not target_settings.exists()
    assert "baseline itself is invalid" in result.stderr


def test_reverse_sync_file_copies_changed_file_and_skips_unchanged_file(tmp_path: Path) -> None:
    volume_changed = tmp_path / "volume" / "changed.json"
    seed_changed = tmp_path / "seed" / "changed.json"
    volume_unchanged = tmp_path / "volume" / "unchanged.json"
    seed_unchanged = tmp_path / "seed" / "unchanged.json"
    volume_changed.parent.mkdir()
    seed_changed.parent.mkdir()

    volume_changed.write_text('{"value":"new"}\n', encoding="utf-8")
    seed_changed.write_text('{"value":"old"}\n', encoding="utf-8")
    volume_unchanged.write_text('{"value":"same"}\n', encoding="utf-8")
    seed_unchanged.write_text('{"value":"same"}\n', encoding="utf-8")
    os.utime(seed_unchanged, ns=(1_000_000_000, 1_000_000_000))
    unchanged_mtime_ns = seed_unchanged.stat().st_mtime_ns
    changed_ack = tmp_path / "changed.ack"
    unchanged_ack = tmp_path / "unchanged.ack"
    changed_ack.write_bytes(seed_changed.read_bytes())
    unchanged_ack.write_bytes(volume_unchanged.read_bytes())

    result = run_seed_lib(
        tmp_path,
        "reverse_sync_file "
        f"{shlex.quote(str(volume_changed))} {shlex.quote(str(seed_changed))} "
        f"{shlex.quote(str(changed_ack))} final; "
        "reverse_sync_file "
        f"{shlex.quote(str(volume_unchanged))} {shlex.quote(str(seed_unchanged))} "
        f"{shlex.quote(str(unchanged_ack))} final",
    )

    assert result.returncode == 0, result.stderr
    assert seed_changed.read_text(encoding="utf-8") == '{"value":"new"}\n'
    assert seed_unchanged.read_text(encoding="utf-8") == '{"value":"same"}\n'
    assert seed_unchanged.stat().st_mtime_ns == unchanged_mtime_ns


def test_reverse_sync_claude_settings_strips_only_managed_hooks(tmp_path: Path) -> None:
    runtime_file = tmp_path / "volume" / "settings.json"
    target_file = tmp_path / "seed" / "settings.local.json"
    runtime_file.parent.mkdir()
    target_file.parent.mkdir()
    write_json(
        runtime_file,
        {
            "hooks": {
                "SessionStart": [{"hooks": [{"command": "generated-start"}]}],
                "PreToolUse": [{"hooks": [{"command": "generated-security"}]}],
                "Stop": [{"hooks": [{"command": "generated-ready"}]}],
                "PostToolUse": [{"hooks": [{"command": "personal-post"}]}],
                "Notification": [{"hooks": [{"command": "personal-notify"}]}],
            },
            "env": {"personal": True},
            "theme": "kept",
        },
    )

    result = run_seed_lib(
        tmp_path,
        "reverse_sync_claude_settings "
        f"{shlex.quote(str(runtime_file))} {shlex.quote(str(target_file))} "
        f"{shlex.quote(str(tmp_path / 'settings.ack'))} final",
    )

    assert result.returncode == 0, result.stderr
    persisted = json.loads(target_file.read_text(encoding="utf-8"))
    assert "SessionStart" not in persisted["hooks"]
    assert "PreToolUse" not in persisted["hooks"]
    assert "Stop" not in persisted["hooks"]
    assert persisted["hooks"]["PostToolUse"] == [{"hooks": [{"command": "personal-post"}]}]
    assert persisted["hooks"]["Notification"] == [{"hooks": [{"command": "personal-notify"}]}]
    assert persisted["env"] == {"personal": True}
    assert persisted["theme"] == "kept"


@pytest.mark.parametrize("mode", ("checkpoint", "final"))
def test_reverse_sync_claude_settings_keeps_existing_on_invalid_json(
    tmp_path: Path,
    mode: str,
) -> None:
    runtime_file = tmp_path / "volume" / "settings.json"
    target_file = tmp_path / "seed" / "settings.local.json"
    runtime_file.parent.mkdir()
    target_file.parent.mkdir()
    runtime_file.write_text("{not valid json", encoding="utf-8")
    original_bytes = b'{"personal":true}\n'
    target_file.write_bytes(original_bytes)
    ack = tmp_path / "settings.ack"
    ack.write_bytes(original_bytes)

    result = run_seed_lib(
        tmp_path,
        "reverse_sync_claude_settings "
        f"{shlex.quote(str(runtime_file))} {shlex.quote(str(target_file))} "
        f"{shlex.quote(str(ack))} {mode}",
    )

    assert result.returncode == 0, result.stderr
    if mode == "final":
        assert "settings are not valid JSON" in result.stderr
    else:
        assert result.stderr == ""
    assert result.stdout == ""
    assert target_file.read_bytes() == original_bytes
    assert ack.read_bytes() == original_bytes
    assert not (target_file.parent / "settings.local.json.tmp").exists()
    assert not list(tmp_path.glob("capture.*"))
    assert not list(tmp_path.glob("filter.*"))
    assert not list(target_file.parent.glob(".djinn-settings-*"))


@pytest.mark.parametrize("kind", ("raw", "claude"))
@pytest.mark.parametrize("mode", ("checkpoint", "final"))
def test_reverse_sync_host_edit_does_not_trigger_write(
    tmp_path: Path, kind: str, mode: str
) -> None:
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "target.json"
    ack = tmp_path / "runtime.ack"
    runtime.write_bytes(b'{"runtime":1}\n')
    ack.write_bytes(runtime.read_bytes())
    target.write_bytes(b'{"host":2}\n')
    before = target.stat().st_mtime_ns
    function = "reverse_sync_file" if kind == "raw" else "reverse_sync_claude_settings"
    result = run_seed_lib(tmp_path, f'{function} "{runtime}" "{target}" "{ack}" {mode} || :')
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert target.read_bytes() == b'{"host":2}\n'
    assert target.stat().st_mtime_ns == before


@pytest.mark.parametrize("kind", ("raw", "claude"))
def test_reverse_sync_failure_keeps_reference_and_retries(tmp_path: Path, kind: str) -> None:
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "target.json"
    ack = tmp_path / "runtime.ack"
    runtime.write_bytes(b'{"runtime":2}\n')
    target.write_bytes(b'{"runtime":1}\n')
    ack.write_bytes(target.read_bytes())
    helper = tmp_path / "fault.py"
    helper.write_text(
        "import runpy, sys\n"
        f"m = runpy.run_path({str(ROOT / 'scripts/settings-copy.py')!r})\n"
        'def fail(*args): raise OSError("injected replacement failure")\n'
        'm["os"].replace = fail\n'
        'sys.exit(m["main"]())\n'
    )
    function = "reverse_sync_file" if kind == "raw" else "reverse_sync_claude_settings"
    call = f'{function} "{runtime}" "{target}" "{ack}" checkpoint || :'
    result = run_seed_lib(tmp_path, f'SETTINGS_COPY_HELPER="{helper}"; {call}')
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == f"  [warn] could not persist {runtime} → {target}\n"
    assert target.read_bytes() == ack.read_bytes() == b'{"runtime":1}\n'
    result = run_seed_lib(tmp_path, call)
    assert result.returncode == 0
    assert result.stderr == result.stdout == ""
    assert ack.read_bytes() == runtime.read_bytes()
    if kind == "raw":
        assert target.read_bytes() == runtime.read_bytes()
    else:
        assert json.loads(target.read_bytes()) == json.loads(runtime.read_bytes())
    target.write_bytes(b'{"host":3}\n')
    assert run_seed_lib(tmp_path, call).returncode == 0
    assert target.read_bytes() == b'{"host":3}\n'
    assert not list(tmp_path.glob("capture.*"))
    assert not list(tmp_path.glob("filter.*"))
    assert not list(tmp_path.glob(".djinn-settings-*"))


def test_reverse_sync_snapshot_filters_and_acknowledges_one_capture(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "target.json"
    ack = tmp_path / "runtime.ack"
    first = b'{"version":1,"hooks":{"Stop":[],"Notification":[]}}\n'
    second = b'{"version":2,"hooks":{"Stop":[],"Notification":[]}}\n'
    runtime.write_bytes(first)
    helper = tmp_path / "rewrite.py"
    helper.write_text(
        "import runpy, sys\nfrom pathlib import Path\n"
        f"Path({str(runtime)!r}).write_bytes({second!r})\n"
        f"m = runpy.run_path({str(ROOT / 'scripts/settings-copy.py')!r})\n"
        'sys.exit(m["main"]())\n'
    )
    result = run_seed_lib(
        tmp_path,
        f'SETTINGS_COPY_HELPER="{helper}"; '
        f'reverse_sync_claude_settings "{runtime}" "{target}" "{ack}" checkpoint || :',
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert ack.read_bytes() == first
    assert runtime.read_bytes() == second
    assert json.loads(target.read_bytes()) == {"version": 1, "hooks": {"Notification": []}}
    result = run_seed_lib(
        tmp_path, f'reverse_sync_claude_settings "{runtime}" "{target}" "{ack}" checkpoint || :'
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert ack.read_bytes() == second
    assert json.loads(target.read_bytes())["version"] == 2
    assert not list(tmp_path.glob("capture.*"))
    assert not list(tmp_path.glob("filter.*"))


@pytest.mark.parametrize("mode", ("checkpoint", "final"))
@pytest.mark.parametrize(
    "fault",
    (
        "missing",
        "invalid",
        "filter",
        "unreadable",
        "scratch",
        "filter-scratch",
        "destination",
        "ack",
    ),
)
def test_reverse_sync_failure_classes(tmp_path: Path, mode: str, fault: str) -> None:
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "destination/target.json"
    target.parent.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    ack = state / "runtime.ack"
    previous = b'{"previous":true}\n'
    runtime.write_bytes(b'{"next":true}\n')
    target.write_bytes(previous)
    ack.write_bytes(previous)
    prefix = ""
    if fault == "missing":
        runtime.unlink()
    elif fault == "invalid":
        runtime.write_bytes(b'{"partial":')
    elif fault == "filter":
        runtime.write_bytes(b"[]")
    elif fault == "unreadable":
        assert os.geteuid() != 0, "permission tests require non-root execution"
        runtime.chmod(0)
    elif fault == "scratch":
        assert os.geteuid() != 0, "permission tests require non-root execution"
        state.chmod(0o500)
    elif fault == "destination":
        target.parent.chmod(0o500)
    elif fault == "filter-scratch":
        prefix = 'mktemp() { [[ "$1" == */filter.* ]] && return 1; command mktemp "$@"; }; '
    else:
        prefix = "mv() { return 1; }; "
    try:
        result = run_seed_lib(
            tmp_path,
            prefix
            + f'reverse_sync_claude_settings "{runtime}" "{target}" "{ack}" {mode} || :; '
            + "echo continued >&2",
        )
        assert result.returncode == 0
        assert result.stdout == ""
        invalid = fault in {"invalid", "filter"}
        silent = fault == "missing" or (invalid and mode == "checkpoint")
        message = f"  [warn] could not persist {runtime} → {target}"
        if fault == "destination":
            message += " (target directory not writable)"
        if invalid:
            message += " (settings are not valid JSON)"
        assert result.stderr == ("" if silent else message + "\n") + "continued\n"
        assert ack.read_bytes() == previous
        if fault != "ack":
            assert target.read_bytes() == previous
        else:
            assert json.loads(target.read_bytes()) == {"next": True}
        assert not list(state.glob("capture.*"))
        assert not list(state.glob("filter.*"))
    finally:
        runtime.chmod(0o600) if runtime.exists() else None
        state.chmod(0o700)
        target.parent.chmod(0o700)


@pytest.mark.parametrize("document", (b"null\n", b"false\n", b"", b"{} {}", b'{"bad":'))
def test_raw_sync_requires_exactly_one_json_document(tmp_path: Path, document: bytes) -> None:
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "target.json"
    ack = tmp_path / "runtime.ack"
    previous = b'{"previous":true}\n'
    runtime.write_bytes(document)
    target.write_bytes(previous)
    ack.write_bytes(previous)
    result = run_seed_lib(
        tmp_path, f'reverse_sync_file "{runtime}" "{target}" "{ack}" checkpoint || :'
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    expected = document if document in (b"null\n", b"false\n") else previous
    assert target.read_bytes() == ack.read_bytes() == expected


def test_final_warning_does_not_consult_checkpoint_throttle(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "target.json"
    ack = tmp_path / "runtime.ack"
    runtime.write_bytes(b'{"new":true}')
    target.write_bytes(b'{"old":true}')
    target.parent.chmod(0o500)
    try:
        result = run_seed_lib(
            tmp_path,
            f'reverse_sync_file "{runtime}" "{target}" "{ack}" checkpoint || :; '
            f'reverse_sync_file "{runtime}" "{target}" "{ack}" final || :',
        )
        # This directory is both scratch and destination; creation failure is class 3.
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == f"  [warn] could not persist {runtime} → {target}\n" * 2
        assert target.read_bytes() == b'{"old":true}'
        assert not ack.exists()
    finally:
        target.parent.chmod(0o700)


@pytest.mark.parametrize("mode", ("checkpoint", "final"))
@pytest.mark.parametrize("kind", ("raw", "claude"))
@pytest.mark.parametrize("content", (b'{"valid":true}', b'{"partial":', b'[]', b'null'))
def test_unchanged_runtime_skips_validation_and_filtering(
    tmp_path: Path, mode: str, kind: str, content: bytes
) -> None:
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "target.json"
    ack = tmp_path / "runtime.ack"
    runtime.write_bytes(content)
    ack.write_bytes(content)
    target.write_bytes(b'{"previous":true}')
    before = target.stat().st_mtime_ns
    function = "reverse_sync_file" if kind == "raw" else "reverse_sync_claude_settings"
    validation_log = tmp_path / "validation.log"
    result = run_seed_lib(
        tmp_path,
        f'jq() {{ print -r -- "$@" >> "{validation_log}"; command jq "$@"; }}; '
        f'{function} "{runtime}" "{target}" "{ack}" {mode} || :',
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert not validation_log.exists()
    assert target.read_bytes() == b'{"previous":true}'
    assert target.stat().st_mtime_ns == before
    assert ack.read_bytes() == content
    assert not list(tmp_path.glob("capture.*"))
    assert not list(tmp_path.glob("filter.*"))


@pytest.mark.parametrize("mode", ("checkpoint", "final"))
@pytest.mark.parametrize("kind", ("raw", "claude"))
@pytest.mark.parametrize("changed", (False, True))
def test_unreadable_reference_warns_preserves_and_retries(
    tmp_path: Path, mode: str, kind: str, changed: bool
) -> None:
    assert os.geteuid() != 0, "permission tests require non-root execution"
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "target.json"
    ack = tmp_path / "runtime.ack"
    previous = b'{"runtime":1}\n'
    runtime.write_bytes(b'{"runtime":2}\n' if changed else previous)
    target.write_bytes(b'{"host":3}\n')
    ack.write_bytes(previous)
    before = target.stat().st_mtime_ns
    ack_before = ack.stat().st_mtime_ns
    function = "reverse_sync_file" if kind == "raw" else "reverse_sync_claude_settings"
    call = f'{function} "{runtime}" "{target}" "{ack}" {mode} || :'
    ack.chmod(0)
    try:
        result = run_seed_lib(tmp_path, f"{call}; {call}")
        assert result.returncode == 0
        assert result.stdout == ""
        warning = f"  [warn] could not persist {runtime} → {target}\n"
        assert result.stderr == warning * (1 if mode == "checkpoint" else 2)
        assert target.read_bytes() == b'{"host":3}\n'
        assert target.stat().st_mtime_ns == before
        assert ack.stat().st_mtime_ns == ack_before
    finally:
        ack.chmod(0o600)
    assert ack.read_bytes() == previous
    assert not list(tmp_path.glob("capture.*"))
    assert not list(tmp_path.glob("filter.*"))

    result = run_seed_lib(tmp_path, call)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert ack.read_bytes() == runtime.read_bytes()
    if changed:
        assert json.loads(target.read_bytes()) == json.loads(runtime.read_bytes())
    else:
        assert target.read_bytes() == b'{"host":3}\n'
        assert target.stat().st_mtime_ns == before


@pytest.mark.parametrize("mode", ("checkpoint", "final"))
@pytest.mark.parametrize("content", (b'null', b'[]', b'false', b'{"hooks":[]}'))
def test_changed_claude_input_must_be_object_accepted_by_filter(
    tmp_path: Path, mode: str, content: bytes
) -> None:
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "target.json"
    ack = tmp_path / "runtime.ack"
    previous = b'{"previous":true}\n'
    runtime.write_bytes(content)
    target.write_bytes(previous)
    ack.write_bytes(previous)
    call = f'reverse_sync_claude_settings "{runtime}" "{target}" "{ack}" {mode} || :'
    result = run_seed_lib(tmp_path, f"{call}; {call}")
    assert result.returncode == 0
    assert result.stdout == ""
    warning = f"  [warn] could not persist {runtime} → {target} (settings are not valid JSON)\n"
    assert result.stderr == (warning * 2 if mode == "final" else "")
    assert target.read_bytes() == ack.read_bytes() == previous
    assert not list(tmp_path.glob("capture.*"))
    assert not list(tmp_path.glob("filter.*"))


@pytest.mark.parametrize("mode", ("checkpoint", "final"))
def test_claude_filtered_output_write_limit_warns_and_retries(tmp_path: Path, mode: str) -> None:
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "target.json"
    ack = tmp_path / "runtime.ack"
    previous = b'{"previous":true}\n'
    content = json.dumps({"items": [0] * 180}, separators=(",", ":")).encode()
    assert len(content) < 512
    runtime.write_bytes(content)
    target.write_bytes(previous)
    ack.write_bytes(previous)

    # zsh's file-size limit is in 512-byte blocks; capture fits, jq's output does not.
    limit = "ulimit -c 0; ulimit -f 1; "
    probe = tmp_path / "probe.filtered"
    result = run_seed_lib(
        tmp_path,
        limit + f'if _claude_filter_managed_hooks "{runtime}" "{probe}" 2>/dev/null; '
        'then echo unexpected; else print -r -- "$?"; fi',
    )
    assert result.returncode == 0
    assert result.stdout == "153\n"
    assert result.stderr == ""
    assert probe.stat().st_size == 512
    probe.unlink()

    call = f'reverse_sync_claude_settings "{runtime}" "{target}" "{ack}" {mode} || :'
    result = run_seed_lib(tmp_path, limit + f"{call}; {call}")
    assert result.returncode == 0
    assert result.stdout == ""
    warning = f"  [warn] could not persist {runtime} → {target}\n"
    assert result.stderr == warning * (1 if mode == "checkpoint" else 2)
    assert target.read_bytes() == ack.read_bytes() == previous
    assert not list(tmp_path.glob("capture.*"))
    assert not list(tmp_path.glob("filter.*"))
    assert not list(tmp_path.glob(".djinn-settings-*"))

    result = run_seed_lib(tmp_path, call)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert ack.read_bytes() == content
    assert json.loads(target.read_bytes()) == json.loads(content)
    target.write_bytes(b'{"host":true}\n')
    result = run_seed_lib(tmp_path, call)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert target.read_bytes() == b'{"host":true}\n'


@pytest.mark.parametrize("mode", ("checkpoint", "final"))
@pytest.mark.parametrize("kind", ("raw", "claude"))
@pytest.mark.parametrize("symlink", (False, True))
def test_acknowledgement_directory_warns_and_retries(
    tmp_path: Path, mode: str, kind: str, symlink: bool
) -> None:
    runtime = tmp_path / "runtime.json"
    target = tmp_path / "target.json"
    ack = tmp_path / "runtime.ack"
    directory = tmp_path / "ack-directory" if symlink else ack
    directory.mkdir()
    if symlink:
        ack.symlink_to(directory, target_is_directory=True)
    content = b'{"next":true}\n'
    runtime.write_bytes(content)
    target.write_bytes(b'{"previous":true}\n')
    function = "reverse_sync_file" if kind == "raw" else "reverse_sync_claude_settings"
    call = f'{function} "{runtime}" "{target}" "{ack}" {mode} || :'
    result = run_seed_lib(tmp_path, f"{call}; {call}")
    assert result.returncode == 0
    assert result.stdout == ""
    warning = f"  [warn] could not persist {runtime} → {target}\n"
    assert result.stderr == warning * (1 if mode == "checkpoint" else 2)
    assert ack.is_dir()
    assert ack.is_symlink() == symlink
    assert list(directory.iterdir()) == []
    assert json.loads(target.read_bytes()) == json.loads(content)
    assert not list(tmp_path.glob("capture.*"))
    assert not list(tmp_path.glob("filter.*"))

    ack.unlink() if symlink else ack.rmdir()
    result = run_seed_lib(tmp_path, call)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert ack.read_bytes() == content
    target.write_bytes(b'{"host":true}\n')
    result = run_seed_lib(tmp_path, call)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert target.read_bytes() == b'{"host":true}\n'



def test_capture_failure_returns_failure_without_os_chatter(tmp_path: Path) -> None:
    result = run_seed_lib(
        tmp_path,
        f'if _capture_session_runtime "{tmp_path / "missing"}" "{tmp_path / "capture"}"; '
        'then echo unexpected; else echo continued; fi',
    )
    assert result.returncode == 0
    assert result.stdout == "continued\n"
    assert result.stderr == ""


def test_opencode_protection_defaults_to_empty_under_nounset(tmp_path: Path) -> None:
    result = run_seed_lib(tmp_path, '_opencode_is_protected x')
    assert result.returncode == 1
    assert result.stdout == result.stderr == ""


def test_opencode_declarations_preserve_existing_protection(tmp_path: Path) -> None:
    result = run_seed_lib(
        tmp_path,
        'opencode_protected_paths=(cli.json); '
        f'source {shlex.quote(str(SCRIPT))}; '
        'typeset -a opencode_protected_paths; _opencode_is_protected cli.json',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_opencode_installer_itself_honors_protected_set(tmp_path: Path) -> None:
    carrier = tmp_path / ".opencode"
    carrier.mkdir()
    target = carrier / "cli.json"
    target.write_bytes(b"protected sentinel")
    payload = tmp_path / "payload"
    payload.write_bytes(b"{}")
    marker = tmp_path / "installed"
    fake = tmp_path / "publisher.py"
    fake.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    result = run_seed_lib(
        tmp_path,
        f'WORKFLOW_PUBLISHER={shlex.quote(str(fake))}; '
        'typeset -a opencode_protected_paths=(cli.json); '
        f'_install_opencode_capture {shlex.quote(str(payload))} {shlex.quote(str(target))} final',
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert target.read_bytes() == b"protected sentinel"


OPENCODE_PERSONAL_NAMES = ("opencode.json", "cli.json", "themes/night.json")


def _personal_sync_fixture(tmp_path: Path, relative: str) -> tuple[Path, Path, Path, str]:
    runtime, target, ack = (
        tmp_path / root / relative for root in ("runtime", ".opencode", "state/opencode.ack")
    )
    for path in (runtime, target, ack):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"previous":true}')
    publisher = ROOT / "src/djinn_in_a_box/core/workflow_publisher.py"
    command = (
        f'WORKFLOW_PUBLISHER={shlex.quote(str(publisher))}; '
        f'OPENCODE_RUNTIME_ROOT={shlex.quote(str(tmp_path / "runtime"))}; '
        f'djinn_state_dir={shlex.quote(str(tmp_path / "state"))}; '
        'typeset -a opencode_protected_paths; '
    )
    return runtime, target, ack, command


@pytest.mark.parametrize("mode", ("checkpoint", "final"))
def test_opencode_theme_install_refusal_keeps_reason_at_final(tmp_path: Path, mode: str) -> None:
    runtime, target, ack, command = _personal_sync_fixture(tmp_path, "themes/night.json")
    previous = target.read_bytes()
    target.unlink()
    target.parent.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "night.json"
    sentinel.write_bytes(previous)
    target.parent.symlink_to(outside, target_is_directory=True)
    runtime.write_bytes(b'{"changed":true}')
    result = run_seed_lib(tmp_path, command + f'_sync_opencode_personal {mode}')
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    reason = "refused themes/night.json: symlink or non-regular entry\n"
    warning = f"  [warn] could not persist {runtime} → {target}\n"
    assert result.stderr == (reason if mode == "final" else "") + warning
    assert sentinel.read_bytes() == ack.read_bytes() == previous
    assert target.parent.is_symlink()


@pytest.mark.parametrize("relative", OPENCODE_PERSONAL_NAMES)
@pytest.mark.parametrize(
    "document",
    (b'{"value":01}', b'{"value":NaN}', b'{"value":Infinity}', b'{"value":-Infinity}',
     b'{"value":"\xff"}', b'{} {}', b'// comment\n{}'),
    ids=("leading-zero", "nan", "infinity", "negative-infinity", "non-utf8", "two-docs", "jsonc"),
)
def test_opencode_strict_json_preserves_carrier_and_ack_then_retries(
    tmp_path: Path, relative: str, document: bytes
) -> None:
    runtime, target, ack, command = _personal_sync_fixture(tmp_path, relative)
    previous = target.read_bytes()
    runtime.write_bytes(document)
    for mode in ("checkpoint", "final"):
        calls = (
            '_sync_opencode_personal checkpoint; _sync_opencode_personal checkpoint'
            if mode == "checkpoint" else '_sync_opencode_personal final'
        )
        result = run_seed_lib(tmp_path, command + calls)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        expected = (
            f"  [warn] could not persist {runtime} → {target} (settings are not valid JSON)\n"
        )
        assert result.stderr == ("" if mode == "checkpoint" else expected)
        assert target.read_bytes() == ack.read_bytes() == previous
        assert [entry.name for entry in (tmp_path / "state").iterdir()] == ["opencode.ack"]
    repaired = b'{"repaired":{"nested":[null,false,1]}}'
    runtime.write_bytes(repaired)
    result = run_seed_lib(tmp_path, command + '_sync_opencode_personal checkpoint')
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert target.read_bytes() == ack.read_bytes() == repaired


@pytest.mark.parametrize("relative", OPENCODE_PERSONAL_NAMES)
@pytest.mark.parametrize(
    "document", (b'null', b'false', b'[]', b'{"nested":{"values":[1,true,null]}}')
)
def test_opencode_strict_json_accepts_all_document_types(tmp_path, relative, document):
    runtime, target, ack, command = _personal_sync_fixture(tmp_path, relative)
    runtime.write_bytes(document)
    result = run_seed_lib(tmp_path, command + '_sync_opencode_personal checkpoint')
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert target.read_bytes() == ack.read_bytes() == document


@pytest.mark.parametrize(
    "document", (b'{"value":01}', b'{"value":NaN}', b'{"value":Infinity}', b'{"value":"\xff"}')
)
def test_claude_raw_sync_retains_its_jq_acceptance(tmp_path: Path, document: bytes) -> None:
    runtime, target, ack = (tmp_path / name for name in ("runtime", "target", "ack"))
    runtime.write_bytes(document)
    target.write_bytes(b"previous")
    ack.write_bytes(b"previous")
    result = run_seed_lib(tmp_path, f'reverse_sync_file "{runtime}" "{target}" "{ack}" checkpoint')
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert target.read_bytes() == ack.read_bytes() == document


def _credential_command(tmp_path: Path) -> str:
    helper = tmp_path / "credentials.py"
    helper.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        f"with Path({str(tmp_path / 'helper-calls')!r}).open('a') as log:\n"
        "    log.write(json.dumps([sys.argv[1:], os.environ['PATH']]) + '\\n')\n"
        "mode = sys.argv[sys.argv.index('--mode') + 1]\n"
        "rc = int(os.environ.get('TEST_CREDENTIAL_RC', '0'))\n"
        "print('discard helper stdout')\n"
        "message = ('refused: entry is a symlink' if rc == 3 else 'failed: invalid JSON')\n"
        "print(message if rc else 'sync ' + mode + ': done', file=sys.stderr)\n"
        "if os.environ.get('TEST_CREDENTIAL_WARNINGS') == '1':\n"
        "    print('conflicting values for account (provider)', file=sys.stderr)\n"
        "    print('conflict copy is invalid and stays: copy-a', file=sys.stderr)\n"
        "    print('conflict copy stays, backup retried next time: copy-b', file=sys.stderr)\n"
        "sys.exit(rc)\n"
    )
    return f"OPENCODE_CREDENTIALS_HELPER={shlex.quote(str(helper))}; "


@pytest.mark.parametrize("mode", ("start", "checkpoint", "final"))
@pytest.mark.parametrize("rc", (0, 1, 3))
@pytest.mark.parametrize("warnings", (False, True), ids=("no-warnings", "warnings"))
def test_credential_sync_exit_code_and_output_mapping(tmp_path, mode, rc, warnings):
    command = _credential_command(tmp_path)
    result = run_seed_lib(
        tmp_path,
        command
        + f"export TEST_CREDENTIAL_RC={rc} TEST_CREDENTIAL_WARNINGS={int(warnings)}; "
        + f"_sync_opencode_credentials {mode}",
    )
    assert result.stdout == ""
    assert result.returncode == (1 if mode == "start" and rc == 3 else 0)
    output = result.stderr
    assert "discard helper stdout" not in output
    if mode == "start" and rc == 3:
        assert "[err] OpenCode credential sync refused:" in output
        assert "refused: entry is a symlink" in output
        assert "[warn]" not in output
    elif rc:
        assert "[warn] OpenCode credential sync" in output
        assert f"rc={rc}" in output
        assert ("refused: entry is a symlink" if rc == 3 else "failed: invalid JSON") in output
        assert "[err]" not in output
        if mode == "checkpoint":
            assert ("sync refused" if rc == 3 else "sync failed") in output
    else:
        if mode == "start":
            assert "[info] sync start: done" in output
        else:
            assert "sync " + mode + ": done" not in output
        for line in (
            "conflicting values for account (provider)",
            "conflict copy is invalid and stays: copy-a",
            "conflict copy stays, backup retried next time: copy-b",
        ):
            assert (line in output) is warnings
        assert output.count("[warn]") == (3 if mode != "checkpoint" else 1) * int(warnings)
        assert "[err]" not in output
        if mode != "start" and not warnings:
            assert output == ""


def test_credential_checkpoint_warns_once_across_failure_recovery_and_refusal(tmp_path):
    command = _credential_command(tmp_path)
    result = run_seed_lib(
        tmp_path,
        command
        + "export TEST_CREDENTIAL_RC=1; _sync_opencode_credentials checkpoint; "
        + "export TEST_CREDENTIAL_RC=0; _sync_opencode_credentials checkpoint; "
        + "export TEST_CREDENTIAL_WARNINGS=1; _sync_opencode_credentials checkpoint; "
        + "export TEST_CREDENTIAL_RC=3; _sync_opencode_credentials checkpoint; "
        + "_sync_opencode_credentials final",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr.count("[warn]") == 2
    assert result.stderr.count("rc=1") == 1
    assert result.stderr.count("rc=3") == 1
    # A new shell is a new session, even with the same helper and storage.
    again = run_seed_lib(
        tmp_path,
        command + "export TEST_CREDENTIAL_RC=1; _sync_opencode_credentials checkpoint",
    )
    assert again.returncode == 0
    assert again.stderr.count("[warn]") == 1


@pytest.mark.parametrize("mode", ("start", "checkpoint", "final"))
def test_credential_helper_arguments_and_callers_path(tmp_path, mode):
    tmp_path = tmp_path / "home with spaces"
    tmp_path.mkdir()
    command = _credential_command(tmp_path)
    bin_dir = tmp_path / "caller-bin"
    bin_dir.mkdir()
    probe = tmp_path / "python-ran"
    python = bin_dir / "python3"
    python.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(probe))}\nexec /usr/bin/python3 "$@"\n')
    python.chmod(0o755)
    result = run_seed_lib(
        tmp_path,
        command
        + f"export PATH={shlex.quote(str(bin_dir))}:$PATH; "
        + f"_sync_opencode_credentials {mode}",
    )
    assert result.returncode == 0, result.stderr
    assert probe.exists()
    calls = [json.loads(line) for line in (tmp_path / "helper-calls").read_text().splitlines()]
    assert len(calls) == 1
    argv, caller_path = calls[0]
    assert caller_path.split(":")[0] == str(bin_dir)
    assert argv == [
        "sync",
        "--carrier",
        str(tmp_path / ".opencode"),
        "--database",
        str(tmp_path / ".local/share/opencode/opencode.db"),
        "--host-state",
        str(tmp_path / ".local/share/opencode/djinn-credentials-host.json"),
        "--conflict-backup",
        str(tmp_path / ".local/state/djinn/opencode-credential-conflicts"),
        "--mode",
        mode,
    ]
