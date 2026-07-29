"""Tests for the OpenCode config-root credential reconciliation helper."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

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
    assert result.stderr == (
        "  [info] Migrated OpenCode credential auth.json to the config root.\n"
    )
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
    assert result.stderr == (
        "  [warn] OpenCode credential conflict for auth.json; "
        "set aside auth.json.pre-migration.\n"
    )
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
        secret = f"{name}-secret"
        config_path.write_text(secret, encoding="utf-8")
        config_path.chmod(0o600)
        volume_path = volume_credential(tmp_path, name)
        volume_path.parent.mkdir(parents=True, exist_ok=True)
        volume_path.symlink_to(config_path)

    first_result = run_credentials(tmp_path)

    assert first_result.returncode == 0, first_result.stderr
    assert first_result.stdout == ""
    assert first_result.stderr == ""
    for name in ("auth.json", "mcp-auth.json"):
        assert config_credential(tmp_path, name).read_text(encoding="utf-8") == (
            f"{name}-secret"
        )

    second_result = run_credentials(tmp_path)

    assert second_result.returncode == 0, second_result.stderr
    assert second_result.stdout == ""
    assert second_result.stderr == ""
    for name in ("auth.json", "mcp-auth.json"):
        assert config_credential(tmp_path, name).read_text(encoding="utf-8") == (
            f"{name}-secret"
        )


def test_conflicts_use_a_numbered_set_aside_without_replacing_an_earlier_one(
    tmp_path: Path,
) -> None:
    for name in ("auth.json", "mcp-auth.json"):
        config_path = config_credential(tmp_path, name)
        config_path.parent.mkdir(exist_ok=True)
        config_path.write_text(f"{name}-config", encoding="utf-8")
        config_path.chmod(0o600)

        volume_path = volume_credential(tmp_path, name)
        volume_path.parent.mkdir(parents=True, exist_ok=True)
        volume_path.write_text(f"{name}-restored-volume", encoding="utf-8")
        volume_path.chmod(0o640)
        earlier_set_aside = volume_path.with_name(f"{name}.pre-migration")
        earlier_set_aside.write_text(f"{name}-earlier-set-aside", encoding="utf-8")
        earlier_set_aside.chmod(0o600)

    result = run_credentials(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == (
        "  [warn] OpenCode credential conflict for auth.json; "
        "set aside auth.json.pre-migration.1.\n"
        "  [warn] OpenCode credential conflict for mcp-auth.json; "
        "set aside mcp-auth.json.pre-migration.1.\n"
    )
    for name in ("auth.json", "mcp-auth.json"):
        volume_path = volume_credential(tmp_path, name)
        assert volume_path.is_symlink()
        assert volume_path.resolve() == config_credential(tmp_path, name)
        assert (
            volume_path.with_name(f"{name}.pre-migration").read_text(
                encoding="utf-8"
            )
            == f"{name}-earlier-set-aside"
        )
        numbered_set_aside = volume_path.with_name(f"{name}.pre-migration.1")
        assert numbered_set_aside.read_text(encoding="utf-8") == (
            f"{name}-restored-volume"
        )
        assert_restrictive_mode(numbered_set_aside)


@pytest.mark.parametrize("name", ("auth.json", "mcp-auth.json"))
def test_noncanonical_volume_symlink_is_refused_without_mutation(
    tmp_path: Path,
    name: str,
) -> None:
    host_managed_path = tmp_path / "host-managed" / name
    host_managed_path.parent.mkdir()
    host_managed_path.write_text(f"{name}-host-managed", encoding="utf-8")
    host_managed_path.chmod(0o640)

    volume_path = volume_credential(tmp_path, name)
    volume_path.parent.mkdir(parents=True, exist_ok=True)
    volume_path.symlink_to(host_managed_path)

    result = run_credentials(tmp_path)

    config_path = config_credential(tmp_path, name)
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == (
        f"  [err] OpenCode credential {name} at {volume_path} must be a regular "
        f"file or the canonical symlink to {config_path}; refusing to change it.\n"
    )
    assert not config_path.exists()
    assert not config_path.is_symlink()
    assert volume_path.is_symlink()
    assert volume_path.resolve() == host_managed_path
    assert host_managed_path.read_text(encoding="utf-8") == f"{name}-host-managed"
    assert stat.S_IMODE(host_managed_path.stat().st_mode) == 0o640


@pytest.mark.parametrize("name", ("auth.json", "mcp-auth.json"))
def test_config_root_symlink_is_refused_without_mutation(
    tmp_path: Path,
    name: str,
) -> None:
    host_managed_path = tmp_path / "host-managed" / name
    host_managed_path.parent.mkdir()
    host_managed_path.write_text(f"{name}-host-managed", encoding="utf-8")
    host_managed_path.chmod(0o640)

    config_path = config_credential(tmp_path, name)
    config_path.parent.mkdir(exist_ok=True)
    config_path.symlink_to(host_managed_path)

    result = run_credentials(tmp_path)

    volume_path = volume_credential(tmp_path, name)
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == (
        f"  [err] OpenCode credential {name} at {config_path} must be a regular "
        "file or be absent; refusing to change it.\n"
    )
    assert config_path.is_symlink()
    assert config_path.resolve() == host_managed_path
    assert not volume_path.exists()
    assert not volume_path.is_symlink()
    assert host_managed_path.read_text(encoding="utf-8") == f"{name}-host-managed"
    assert stat.S_IMODE(host_managed_path.stat().st_mode) == 0o640


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


def test_credentials_clean_leaves_a_startable_state(tmp_path: Path) -> None:
    """`clean volumes --credentials` must not make the next start refuse.

    That command empties the config-root credential directory while the named
    volume keeps its symlinks, so both links dangle. Refusing there would make
    the very cleanup #19 enabled leave Djinn unable to start.
    """
    first = run_credentials(tmp_path)
    assert first.returncode == 0, first.stderr

    # Exactly what clear_sync_path does to the config-root credential directory.
    for name in ("auth.json", "mcp-auth.json"):
        config_credential(tmp_path, name).unlink()
        assert volume_credential(tmp_path, name).is_symlink()
        assert not volume_credential(tmp_path, name).exists()  # dangling

    result = run_credentials(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    for name in ("auth.json", "mcp-auth.json"):
        config_path = config_credential(tmp_path, name)
        volume_path = volume_credential(tmp_path, name)
        assert config_path.is_file() and not config_path.is_symlink()
        assert config_path.read_text(encoding="utf-8") == "{}"
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        assert volume_path.is_symlink()
        assert volume_path.resolve() == config_path


def test_relative_canonical_link_survives_a_credentials_clean(tmp_path: Path) -> None:
    """An equivalent link spelling must recover like the one the helper writes.

    `-ef` accepts a relative or `..`-containing link while the target exists, so
    such a link is a valid already-done state. Comparing the raw link text after
    a clean would reject exactly that state and abort the start — the same
    regression as before, one spelling further out.
    """
    for name in ("auth.json", "mcp-auth.json"):
        config_path = config_credential(tmp_path, name)
        config_path.parent.mkdir(exist_ok=True)
        config_path.write_text(f"{name}-secret", encoding="utf-8")
        config_path.chmod(0o600)
        volume_path = volume_credential(tmp_path, name)
        volume_path.parent.mkdir(parents=True, exist_ok=True)
        volume_path.symlink_to(Path("../../..") / ".opencode" / name)

    assert run_credentials(tmp_path).returncode == 0

    for name in ("auth.json", "mcp-auth.json"):
        config_credential(tmp_path, name).unlink()

    result = run_credentials(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    for name in ("auth.json", "mcp-auth.json"):
        config_path = config_credential(tmp_path, name)
        assert config_path.read_text(encoding="utf-8") == "{}"
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        assert volume_credential(tmp_path, name).is_symlink()


def test_relative_link_to_an_unrelated_file_is_still_refused(tmp_path: Path) -> None:
    external = tmp_path / "external.json"
    external.write_text("external-secret", encoding="utf-8")
    external.chmod(0o640)
    volume_path = volume_credential(tmp_path, "auth.json")
    volume_path.parent.mkdir(parents=True, exist_ok=True)
    volume_path.symlink_to(Path("../../..") / "external.json")

    result = run_credentials(tmp_path)

    assert result.returncode == 1
    assert "refusing to change it" in result.stderr
    assert external.read_text(encoding="utf-8") == "external-secret"
    assert stat.S_IMODE(external.stat().st_mode) == 0o640


def test_aliased_directory_link_is_refused_consistently(tmp_path: Path) -> None:
    """One definition of canonical, before and after a clean.

    `-ef` accepted a link through an aliased directory while the target existed
    and the text comparison refused it afterwards, so the same arrangement was
    valid on one start and unstartable on the next. Lexical comparison refuses it
    in both, which is the intended handling of a deliberate redirect.
    """
    config_path = config_credential(tmp_path, "auth.json")
    config_path.parent.mkdir(exist_ok=True)
    config_path.write_text("secret", encoding="utf-8")
    alias_dir = tmp_path / ".opencode-alias"
    alias_dir.symlink_to(config_path.parent, target_is_directory=True)
    volume_path = volume_credential(tmp_path, "auth.json")
    volume_path.parent.mkdir(parents=True, exist_ok=True)
    volume_path.symlink_to(alias_dir / "auth.json")

    before = run_credentials(tmp_path)
    config_path.unlink()
    after = run_credentials(tmp_path)

    assert before.returncode == 1
    assert after.returncode == 1
    assert "refusing to change it" in before.stderr


def test_hard_link_target_is_refused_consistently(tmp_path: Path) -> None:
    config_path = config_credential(tmp_path, "auth.json")
    config_path.parent.mkdir(exist_ok=True)
    config_path.write_text("secret", encoding="utf-8")
    hard_link = tmp_path / "hard-link.json"
    os.link(config_path, hard_link)
    volume_path = volume_credential(tmp_path, "auth.json")
    volume_path.parent.mkdir(parents=True, exist_ok=True)
    volume_path.symlink_to(hard_link)

    before = run_credentials(tmp_path)
    config_path.unlink()
    after = run_credentials(tmp_path)

    assert before.returncode == 1
    assert after.returncode == 1
    assert hard_link.read_text(encoding="utf-8") == "secret"
