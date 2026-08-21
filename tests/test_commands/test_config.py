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
        config = AppConfig(code_dir=tmp_path, build=BuildConfig(network="host"))
        after = _set_config_value(config, "general.timezone", "Europe/Berlin")
        assert after.timezone == "Europe/Berlin"
        assert after.build.network == "host"
