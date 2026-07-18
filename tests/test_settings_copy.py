from __future__ import annotations

import importlib.util
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
    assert capsys.readouterr().err == "settings copy failed\n"


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
