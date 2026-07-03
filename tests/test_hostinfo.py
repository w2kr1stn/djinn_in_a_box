"""Tests for host detection helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from djinn_in_a_box.config.models import ResourceLimits
from djinn_in_a_box.core import hostinfo


class TestDetectTimezone:
    """detect_timezone resolves only valid zoneinfo symlinks."""

    class UnreadableLocaltimePath:
        """Path-like test double that fails before symlink resolution."""

        def is_symlink(self) -> bool:
            raise OSError("unreadable")

    def test_valid_zoneinfo_symlink_returns_iana_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zone_file = tmp_path / "usr" / "share" / "zoneinfo" / "America" / "New_York"
        zone_file.parent.mkdir(parents=True)
        zone_file.touch()
        localtime = tmp_path / "localtime"
        localtime.symlink_to(zone_file)

        monkeypatch.setattr(hostinfo, "LOCALTIME_PATH", localtime)
        monkeypatch.setattr(hostinfo, "available_timezones", lambda: {"America/New_York"})

        assert hostinfo.detect_timezone() == "America/New_York"

    def test_non_symlink_returns_utc(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        localtime = tmp_path / "localtime"
        localtime.write_text("UTC\n")

        monkeypatch.setattr(hostinfo, "LOCALTIME_PATH", localtime)

        assert hostinfo.detect_timezone() == "UTC"

    def test_unreadable_path_returns_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hostinfo, "LOCALTIME_PATH", self.UnreadableLocaltimePath())

        assert hostinfo.detect_timezone() == "UTC"

    def test_broken_symlink_returns_utc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        localtime = tmp_path / "localtime"
        localtime.symlink_to(tmp_path / "usr" / "share" / "zoneinfo" / "America" / "New_York")

        monkeypatch.setattr(hostinfo, "LOCALTIME_PATH", localtime)
        monkeypatch.setattr(hostinfo, "available_timezones", lambda: {"America/New_York"})

        assert hostinfo.detect_timezone() == "UTC"

    def test_bogus_zone_name_returns_utc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zone_file = tmp_path / "usr" / "share" / "zoneinfo" / "Mars" / "Base"
        zone_file.parent.mkdir(parents=True)
        zone_file.touch()
        localtime = tmp_path / "localtime"
        localtime.symlink_to(zone_file)

        monkeypatch.setattr(hostinfo, "LOCALTIME_PATH", localtime)
        monkeypatch.setattr(hostinfo, "available_timezones", lambda: {"America/New_York"})

        assert hostinfo.detect_timezone() == "UTC"

    def test_unavailable_zoneinfo_database_returns_utc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zone_file = tmp_path / "usr" / "share" / "zoneinfo" / "America" / "New_York"
        zone_file.parent.mkdir(parents=True)
        zone_file.touch()
        localtime = tmp_path / "localtime"
        localtime.symlink_to(zone_file)

        def unavailable_timezones() -> set[str]:
            raise hostinfo.ZoneInfoNotFoundError

        monkeypatch.setattr(hostinfo, "LOCALTIME_PATH", localtime)
        monkeypatch.setattr(hostinfo, "available_timezones", unavailable_timezones)

        assert hostinfo.detect_timezone() == "UTC"


class TestSuggestResources:
    """suggest_resources autosizes conservatively and validates the result."""

    def test_synthetic_host_mem_and_cpu_returns_expected_limits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       33554432 kB\n")

        monkeypatch.setattr(hostinfo, "MEMINFO_PATH", meminfo)
        monkeypatch.setattr(hostinfo.os, "cpu_count", lambda: 16)

        limits = hostinfo.suggest_resources()

        assert limits == ResourceLimits(
            cpu_limit=8,
            memory_limit="16G",
            cpu_reservation=2,
            memory_reservation="4G",
        )
        ResourceLimits.model_validate(limits.model_dump())

    @pytest.mark.parametrize("contents", [None, "MemAvailable: 123 kB\n", "MemTotal: nope kB\n"])
    def test_missing_or_unparsable_meminfo_returns_defaults(
        self, contents: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        meminfo = tmp_path / "meminfo"
        if contents is not None:
            meminfo.write_text(contents)

        monkeypatch.setattr(hostinfo, "MEMINFO_PATH", meminfo)
        monkeypatch.setattr(hostinfo.os, "cpu_count", lambda: 16)

        assert hostinfo.suggest_resources() == ResourceLimits()

    def test_suggested_resources_satisfy_validator_invariants(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       33554432 kB\n")

        monkeypatch.setattr(hostinfo, "MEMINFO_PATH", meminfo)
        monkeypatch.setattr(hostinfo.os, "cpu_count", lambda: 16)

        limits = ResourceLimits.model_validate(hostinfo.suggest_resources().model_dump())

        assert limits.cpu_reservation <= limits.cpu_limit
        assert limits.memory_reservation == "4G"

    def test_many_core_host_clamps_cpu_limit_to_field_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       33554432 kB\n")

        monkeypatch.setattr(hostinfo, "MEMINFO_PATH", meminfo)
        monkeypatch.setattr(hostinfo.os, "cpu_count", lambda: 512)

        limits = hostinfo.suggest_resources()

        assert limits.cpu_limit == 128
        assert limits.cpu_reservation == 32

    def test_resource_limits_construction_errors_are_not_silenced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       33554432 kB\n")

        def broken_resource_limits(**_kwargs: object) -> ResourceLimits:
            raise RuntimeError("formula bug")

        monkeypatch.setattr(hostinfo, "MEMINFO_PATH", meminfo)
        monkeypatch.setattr(hostinfo.os, "cpu_count", lambda: 16)
        monkeypatch.setattr(hostinfo, "ResourceLimits", broken_resource_limits)

        with pytest.raises(RuntimeError, match="formula bug"):
            hostinfo.suggest_resources()
