from __future__ import annotations

import importlib.util
import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import NoReturn, cast

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "settings-copy.py"


def _settings_module() -> tuple[ModuleType, Callable[..., bool]]:
    spec = importlib.util.spec_from_file_location("settings_copy", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = ModuleType(spec.name)
    spec.loader.exec_module(module)
    return module, cast(Callable[..., bool], vars(module)["copy_settings"])


def test_missing_ok_preserves_existing_target_without_temp_residue(tmp_path: Path) -> None:
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"previous\n")

    _module, copy_settings = _settings_module()
    result = copy_settings(tmp_path / "missing.json", destination, missing_ok=True)

    assert result
    assert destination.read_bytes() == b"previous\n"
    assert not list(tmp_path.glob(".djinn-settings-*"))


def test_main_reports_copy_failure_on_standard_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module, _copy_settings = _settings_module()
    main = cast(Callable[[list[str]], int], vars(module)["main"])

    result = main(
        ["--copy-settings", str(tmp_path / "missing.json"), str(tmp_path / "target.json")]
    )

    assert result == 1
    output = capsys.readouterr()
    assert output.err == "settings copy failed\n"
    assert output.out == ""


def test_atomic_copy_fault_keeps_previous_file_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.json"
    source.write_bytes(b"new\n")
    destination = tmp_path / "settings.json"
    destination.write_bytes(b"previous\n")
    module, copy_settings = _settings_module()

    def abort_replace(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("injected copy abort")

    os_module = cast(ModuleType, vars(module)["os"])
    monkeypatch.setattr(os_module, "replace", abort_replace)

    assert not copy_settings(source, destination)
    assert destination.read_bytes() == b"previous\n"
    assert not list(tmp_path.glob(".djinn-settings-*"))


def test_copy_settings_preserves_bytes_and_fsync_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    payload = b"\x00\xffarbitrary bytes\n\n"
    source.write_bytes(payload)
    target = tmp_path / "target.bin"
    target.write_bytes(b"previous")
    module, copy_settings = _settings_module()
    events: list[str] = []
    temporaries: list[Path] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            assert target.read_bytes() == payload
            events.append("directory-fsync")
        else:
            (temp,) = tmp_path.glob(".djinn-settings-*")
            assert temp.read_bytes() == payload, "flush must precede file fsync"
            assert stat.S_IMODE(temp.stat().st_mode) == 0o600
            events.append("file-fsync")
        real_fsync(fd)

    def replace(src, dst) -> None:
        assert events[-1] == "file-fsync"
        assert Path(src).parent == target.parent
        assert Path(dst) == target
        temporaries.append(Path(src))
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(vars(module)["os"], "fsync", fsync)
    monkeypatch.setattr(vars(module)["os"], "replace", replace)
    for _ in range(2):
        assert copy_settings(source, target)
        assert target.read_bytes() == payload
        assert not list(tmp_path.glob(".djinn-settings-*"))
    assert events == ["file-fsync", "replace", "directory-fsync"] * 2
    assert len(set(temporaries)) == 2
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
