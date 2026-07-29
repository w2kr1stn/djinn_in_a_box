"""Tests for the OpenCode config-root credential reconciliation helper."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "opencode-credentials.sh"
OUTPUT_LIB = ROOT / "scripts" / "output-lib.sh"


def run_credentials(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    zsh = shutil.which("zsh")
    assert zsh is not None

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "NO_COLOR": "1",
    }
    env.pop("DJINN_FORCE_UI_COLOR", None)

    return subprocess.run(
        [
            zsh,
            "-c",
            (
                "set -euo pipefail; "
                f'source "{OUTPUT_LIB}"; '
                f'source "{SCRIPT}"; '
                "ensure_opencode_credentials"
            ),
        ],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def config_credential(tmp_path: Path, name: str = "auth.json") -> Path:
    return tmp_path / ".opencode" / name


def volume_credential(tmp_path: Path, name: str = "auth.json") -> Path:
    return tmp_path / ".local" / "share" / "opencode" / name


def assert_restrictive_mode(path: Path) -> None:
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_fresh_credentials_are_created_in_config_root_and_linked(tmp_path: Path) -> None:
    result = run_credentials(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    for name in ("auth.json", "mcp-auth.json"):
        config_path = config_credential(tmp_path, name)
        volume_path = volume_credential(tmp_path, name)
        assert config_path.read_text(encoding="utf-8") == "{}"
        assert_restrictive_mode(config_path)
        assert volume_path.is_symlink()
        assert volume_path.resolve() == config_path


def test_existing_volume_credential_migrates_with_restrictive_mode(tmp_path: Path) -> None:
    volume_path = volume_credential(tmp_path)
    volume_path.parent.mkdir(parents=True)
    secret = "migrated-provider-token"
    volume_path.write_text(secret, encoding="utf-8")
    volume_path.chmod(0o600)

    result = run_credentials(tmp_path)

    config_path = config_credential(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "Migrated OpenCode credential auth.json" in result.stderr
    assert secret not in result.stderr
    assert config_path.read_text(encoding="utf-8") == secret
    assert_restrictive_mode(config_path)
    assert volume_path.is_symlink()
    assert volume_path.resolve() == config_path


def test_conflicting_credential_keeps_config_and_sets_aside_volume_file(
    tmp_path: Path,
) -> None:
    config_path = config_credential(tmp_path)
    config_path.parent.mkdir()
    config_secret = "config-root-token"
    config_path.write_text(config_secret, encoding="utf-8")
    config_path.chmod(0o600)

    volume_path = volume_credential(tmp_path)
    volume_path.parent.mkdir(parents=True)
    volume_secret = "volume-token-to-preserve"
    volume_path.write_text(volume_secret, encoding="utf-8")
    volume_path.chmod(0o600)

    result = run_credentials(tmp_path)

    set_aside_path = volume_path.with_name("auth.json.pre-migration")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "OpenCode credential conflict for auth.json" in result.stderr
    assert volume_secret not in result.stderr
    assert config_secret not in result.stderr
    assert config_path.read_text(encoding="utf-8") == config_secret
    assert_restrictive_mode(config_path)
    assert set_aside_path.exists()
    assert set_aside_path.read_text(encoding="utf-8") == volume_secret
    assert volume_path.is_symlink()
    assert volume_path.resolve() == config_path


def test_existing_correct_symlinks_are_idempotent_and_silent(tmp_path: Path) -> None:
    for name in ("auth.json", "mcp-auth.json"):
        config_path = config_credential(tmp_path, name)
        config_path.parent.mkdir(exist_ok=True)
        config_path.write_text(f"{name}-secret", encoding="utf-8")
        config_path.chmod(0o600)
        volume_path = volume_credential(tmp_path, name)
        volume_path.parent.mkdir(parents=True, exist_ok=True)
        volume_path.symlink_to(config_path)

    first_result = run_credentials(tmp_path)
    second_result = run_credentials(tmp_path)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert first_result.stdout == second_result.stdout == ""
    assert first_result.stderr == second_result.stderr == ""


def test_missing_volume_links_are_reestablished_silently(tmp_path: Path) -> None:
    for name in ("auth.json", "mcp-auth.json"):
        config_path = config_credential(tmp_path, name)
        config_path.parent.mkdir(exist_ok=True)
        config_path.write_text(f"{name}-secret", encoding="utf-8")
        config_path.chmod(0o600)

    result = run_credentials(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    for name in ("auth.json", "mcp-auth.json"):
        volume_path = volume_credential(tmp_path, name)
        assert volume_path.is_symlink()
        assert volume_path.resolve() == config_credential(tmp_path, name)
