"""Tests for `djinn config set/get` handling of build settings."""

from pathlib import Path

import pytest

from djinn_in_a_box.commands.config import (
    ALLOWED_CONFIG_KEYS,
    _format_config_value,
    _set_config_value,
)
from djinn_in_a_box.config.models import AppConfig, BuildConfig


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(code_dir=tmp_path)


class TestBuildNetworkKey:
    """`build.network` is the opt-in for hosts whose build network has no DNS."""

    def test_key_is_settable(self) -> None:
        assert "build.network" in ALLOWED_CONFIG_KEYS

    def test_set_then_read_back(self, config: AppConfig) -> None:
        updated = _set_config_value(config, "build.network", "host")
        assert updated.build.network == "host"
        assert _format_config_value(updated, "build.network") == "host"

    def test_value_is_normalized(self, config: AppConfig) -> None:
        assert _set_config_value(config, "build.network", "  HOST  ").build.network == "host"

    def test_named_network_is_refused(self, config: AppConfig) -> None:
        # Compose would interpolate it happily; buildkit refuses it mid-build.
        with pytest.raises(Exception, match="network"):
            _set_config_value(config, "build.network", "djinn-network")

    def test_unrelated_set_preserves_it(self, tmp_path: Path) -> None:
        """Setting any other key must not silently reset this one.

        `_build_config` rebuilds the whole `AppConfig`, so a field it forgets to
        carry over reverts to its default without a word — and the next build
        would fail exactly the way this setting exists to prevent.
        """
        config = AppConfig(code_dir=tmp_path, workspace="aios", build=BuildConfig(network="host"))
        after = _set_config_value(config, "general.timezone", "Europe/Berlin")
        assert after.timezone == "Europe/Berlin"
        assert after.build.network == "host"
        assert after.workspace == "aios"


@pytest.mark.parametrize("key", ALLOWED_CONFIG_KEYS)
def test_every_scalar_set_preserves_declarations(tmp_path, key):
    from djinn_in_a_box.config.loader import load_config, save_config

    values = {
        "general.code_dir": str(tmp_path),
        "general.workspace": "aios",
        "general.timezone": "Europe/Berlin",
        "general.config_root": str(tmp_path / "root"),
        "general.shared_root": str(tmp_path / "shared"),
        "general.local_root": str(tmp_path / "local"),
        "general.sops_age_key_file": str(tmp_path / "key"),
        "resources.cpu_limit": "4",
        "resources.memory_limit": "4G",
        "resources.cpu_reservation": "2",
        "resources.memory_reservation": "2G",
        "shell.skip_mounts": "true",
        "shell.omp_theme_path": str(tmp_path / "theme"),
        "config_sync.source": "codex",
        "build.network": "host",
        "git.signing_identity": "none",
        "git.allowed_signers_file": str(tmp_path / "allowed_signers"),
        "hostctl.default_duration": "20m",
    }
    before = AppConfig(
        code_dir=tmp_path,
        mounts={
            "archive": {"source": "/offline", "target": "/archive", "marker": ".ready"},
            "worker": {"volume": True, "target": "/worker", "backup": "none"},
        },
        environment={"CDP_HOST": "$HOST\n[brackets]", "EMPTY": ""},
    )
    path = tmp_path / "config.toml"
    save_config(_set_config_value(before, key, values[key]), path)
    after = load_config(path)
    assert after.mounts == before.mounts
    assert after.environment == before.environment
    field_path = key.removeprefix("general.").split(".")
    left, right = before.model_dump(), after.model_dump()
    left_leaf, right_leaf = left, right
    for part in field_path[:-1]:
        left_leaf, right_leaf = left_leaf[part], right_leaf[part]
    left_leaf.pop(field_path[-1])
    right_leaf.pop(field_path[-1])
    assert left == right
