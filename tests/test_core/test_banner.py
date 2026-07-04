from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from rich.console import Console

from djinn_in_a_box.core import banner as banner_mod
from djinn_in_a_box.core.theme import DJINN_THEME


class RecordingFile(StringIO):
    def __init__(self, *, encoding: str) -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        return self._encoding


def _recording_console(
    *,
    width: int = 80,
    encoding: str = "utf-8",
    color_system: str | None = "truecolor",
    no_color: bool | None = False,
    environ: dict[str, str] | None = None,
) -> tuple[Console, RecordingFile]:
    output = RecordingFile(encoding=encoding)
    console = Console(
        file=output,
        force_terminal=True,
        color_system=color_system,
        no_color=no_color,
        width=width,
        theme=DJINN_THEME,
        _environ={} if environ is None else environ,
    )
    return console, output


def _render(monkeypatch: pytest.MonkeyPatch, console: Console, output: StringIO) -> str:
    monkeypatch.setattr(banner_mod, "err_console", console)
    banner_mod.banner()
    return output.getvalue()


def _has_braille(value: str) -> bool:
    return any("\u2800" <= char <= "\u28ff" for char in value)


def test_full_banner_contains_braille_and_wordmark(monkeypatch: pytest.MonkeyPatch) -> None:
    console, output = _recording_console(width=80)

    rendered = _render(monkeypatch, console, output)

    assert _has_braille(rendered)
    assert "in a box" in rendered and "██" in rendered


def test_wordmark_mode_omits_braille_when_narrow(monkeypatch: pytest.MonkeyPatch) -> None:
    console, output = _recording_console(width=50)

    rendered = _render(monkeypatch, console, output)

    assert not _has_braille(rendered)
    assert "in a box" in rendered and "██" in rendered


@pytest.mark.parametrize(
    "console_kwargs",
    [
        {"no_color": True},
        {"environ": {"TERM": "dumb"}},
    ],
)
def test_plain_mode_uses_single_title_line(
    monkeypatch: pytest.MonkeyPatch,
    console_kwargs: dict[str, Any],
) -> None:
    console, output = _recording_console(**console_kwargs)

    rendered = _render(monkeypatch, console, output)

    assert rendered.splitlines() == ["Djinn in a Box"]
    assert not _has_braille(rendered)


def test_ascii_console_never_raises_and_uses_plain_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, output = _recording_console(encoding="ascii")

    rendered = _render(monkeypatch, console, output)

    assert rendered.splitlines() == ["Djinn in a Box"]
    assert not _has_braille(rendered)
