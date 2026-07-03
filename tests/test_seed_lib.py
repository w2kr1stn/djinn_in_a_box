"""Tests for the sourceable seed synchronization library."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "seed-lib.sh"


def run_seed_lib(tmp_path: Path, command: str) -> subprocess.CompletedProcess[str]:
    jq = shutil.which("jq")
    assert jq is not None
    zsh = shutil.which("zsh")
    assert zsh is not None

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{Path(jq).parent}:/usr/bin:/bin",
    }

    return subprocess.run(
        [
            zsh,
            "-c",
            f"set -euo pipefail; source {shlex.quote(str(SCRIPT))}; {command}",
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def write_json(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_minimal_claude_seed_without_skills_dir_merges_settings(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "CLAUDE.md").write_text("Starter instructions.\n", encoding="utf-8")
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
    (seed_dir / "CLAUDE.md").write_text("Starter instructions.\n", encoding="utf-8")
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
    merged = json.loads(target_settings.read_text(encoding="utf-8"))
    assert merged == {
        "env": {"mode": "local", "kept": True},
        "enabledPlugins": {"local-plugin": True},
    }


def test_existing_target_settings_are_not_clobbered_without_local_overlay(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "CLAUDE.md").write_text("Starter instructions.\n", encoding="utf-8")
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
    (seed_dir / "CLAUDE.md").write_text("Starter instructions.\n", encoding="utf-8")
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


def test_malformed_local_overlay_on_fresh_volume_still_initialises_baseline(
    tmp_path: Path,
) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "CLAUDE.md").write_text("Starter instructions.\n", encoding="utf-8")
    write_json(seed_dir / "settings.json", {"baseline": True})
    (seed_dir / "settings.local.json").write_text("{not valid json", encoding="utf-8")

    target_settings = tmp_path / ".claude" / "settings.json"
    target_settings.parent.mkdir()  # fresh volume: no settings.json yet

    result = run_seed_lib(
        tmp_path,
        "claude_settings_merge "
        f"{shlex.quote(str(seed_dir))} {shlex.quote(str(target_settings))}",
    )

    assert result.returncode == 0, result.stderr
    assert "settings merge failed" in result.stderr
    # A fresh volume must never end up settings-less: baseline fallback applies.
    assert json.loads(target_settings.read_text(encoding="utf-8")) == {"baseline": True}


def test_malformed_baseline_is_named_and_never_installed(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "CLAUDE.md").write_text("Starter instructions.\n", encoding="utf-8")
    # The BASELINE is malformed (the hand-edited file); the overlay is valid.
    (seed_dir / "settings.json").write_text("{trailing comma,}", encoding="utf-8")
    write_json(seed_dir / "settings.local.json", {"local": True})

    target_settings = tmp_path / ".claude" / "settings.json"
    target_settings.parent.mkdir()  # fresh volume

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

    result = run_seed_lib(
        tmp_path,
        "reverse_sync_file "
        f"{shlex.quote(str(volume_changed))} {shlex.quote(str(seed_changed))}; "
        "reverse_sync_file "
        f"{shlex.quote(str(volume_unchanged))} {shlex.quote(str(seed_unchanged))}",
    )

    assert result.returncode == 0, result.stderr
    assert seed_changed.read_text(encoding="utf-8") == '{"value":"new"}\n'
    assert seed_unchanged.read_text(encoding="utf-8") == '{"value":"same"}\n'
    assert seed_unchanged.stat().st_mtime_ns == unchanged_mtime_ns
