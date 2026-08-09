"""Startup banner rendering for the Djinn CLI."""

from __future__ import annotations

import codecs
from contextlib import suppress
from enum import Enum

from rich.console import Console
from rich.table import Table
from rich.text import Text

from djinn_in_a_box.core.console import err_console
from djinn_in_a_box.core.theme import PRIMARY, SECONDARY

PLAIN_TITLE = "Djinn in a Box"

BRAILLE_LOGO = """⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡀⢀⣤⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣴⣿⣷⠀⠻⣿⣿⣦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⠟⢁⣴⣦⣈⠻⠿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣾⣿⣿⣿⣿⣷⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠻⣉⡀⢀⣉⠟⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⣠⣤⣶⣶⣦⡈⠃⠘⢁⣴⣶⣦⣤⣀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⣴⣿⣿⡿⠿⠿⠿⣷⡀⢀⣠⣤⣤⣤⣤⣄⣁⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⣼⣿⣿⣿⣷⣶⣶⠦⠀⢀⡈⠛⠿⣿⣿⣿⣿⣿⣷⣄⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠉⠙⠛⠉⢉⣁⣤⣴⣾⣿⣿⣷⣦⣤⣈⡉⠙⠛⠛⠉⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠿⢿⣿⣿⣿⣿⠿⠟⠛⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣶⣤⣤⣤⣤⣤⣴⡶⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⣀⣴⣿⣿⣿⣿⣿⣿⣿⡿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠐⠲⠿⢿⣿⣿⣿⣿⣿⣿⡿⠟⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠉⠉⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀"""

WORDMARK = """██████╗      ██╗██╗███╗   ██╗███╗   ██╗
██╔══██╗     ██║██║████╗  ██║████╗  ██║
██║  ██║     ██║██║██╔██╗ ██║██╔██╗ ██║
██║  ██║██   ██║██║██║╚██╗██║██║╚██╗██║
██████╔╝╚█████╔╝██║██║ ╚████║██║ ╚████║
╚═════╝  ╚════╝ ╚═╝╚═╝  ╚═══╝╚═╝  ╚═══╝
            ─── in a box ───"""

_BRAILLE_BLANK = "⠀"
_FULL_WIDTH = 70


class _BannerMode(Enum):
    FULL = "full"
    WORDMARK = "wordmark"
    PLAIN = "plain"


def banner() -> None:
    """Render the best available startup banner to stderr."""
    try:
        _render_banner(err_console)
    except Exception:
        with suppress(Exception):
            _render_plain(err_console)


def _render_banner(console: Console) -> None:
    mode = _mode_for_console(console)
    if mode is _BannerMode.PLAIN:
        _render_plain(console)
        return

    # The graphical modes lead with a blank line, the same spacing convention
    # rule() follows: flush against the shell prompt, the logo's top row reads
    # as clipped. Plain mode stays exactly one line — it exists for NO_COLOR,
    # dumb, and non-UTF-8 terminals, where extra whitespace is noise.
    console.print()
    if mode is _BannerMode.FULL:
        _render_full(console)
    else:
        _render_wordmark(console)


def _mode_for_console(console: Console) -> _BannerMode:
    if _plain_required(console) or not _is_utf8(console):
        return _BannerMode.PLAIN
    if _has_color(console) and console.width >= _FULL_WIDTH:
        return _BannerMode.FULL
    return _BannerMode.WORDMARK


def _plain_required(console: Console) -> bool:
    return bool(console.no_color) or bool(console.is_dumb_terminal)


def _is_utf8(console: Console) -> bool:
    file = console.file
    encoding = getattr(file, "encoding", None)
    if not encoding:
        return False
    try:
        return codecs.lookup(encoding).name == "utf-8"
    except LookupError:
        return False


def _has_color(console: Console) -> bool:
    return console.color_system is not None and not console.no_color


def _render_full(console: Console) -> None:
    logo_lines = _trim_logo_lines()
    logo = _logo_text(logo_lines)
    top_padding = max(0, (len(logo_lines) - len(WORDMARK.splitlines())) // 2)
    wordmark = _wordmark_text(top_padding=top_padding)

    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    grid.add_column()
    grid.add_row(logo, wordmark)
    console.print(grid)


def _render_wordmark(console: Console) -> None:
    console.print(_wordmark_text())


def _render_plain(console: Console) -> None:
    console.print(PLAIN_TITLE)


def _trim_logo_lines() -> list[str]:
    lines: list[str] = list(BRAILLE_LOGO.splitlines())
    used_columns = [
        index
        for line in lines
        for index, char in enumerate(line)
        if char != _BRAILLE_BLANK
    ]
    if not used_columns:
        return lines

    start = min(used_columns)
    end = max(used_columns) + 1
    return [line[start:end] for line in lines]


def _logo_text(lines: list[str]) -> Text:
    logo = Text()
    total = len(lines)
    for index, line in enumerate(lines):
        if index:
            logo.append("\n")
        logo.append(line, style=_gradient_color(index, total))
    return logo


def _wordmark_text(*, top_padding: int = 0) -> Text:
    text = Text("\n" * top_padding)
    lines = WORDMARK.splitlines()
    last_index = len(lines) - 1
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        if index == last_index:
            text.append(line, style="muted bold")
        else:
            text.append(line, style="primary.bold")
    return text


def _gradient_color(index: int, total: int) -> str:
    if total <= 1:
        return PRIMARY

    ratio = index / (total - 1)
    return _interpolate_hex(PRIMARY, SECONDARY, ratio)


def _interpolate_hex(start: str, end: str, ratio: float) -> str:
    start_rgb = _hex_to_rgb(start)
    end_rgb = _hex_to_rgb(end)
    channels = [
        round(start_channel + (end_channel - start_channel) * ratio)
        for start_channel, end_channel in zip(start_rgb, end_rgb, strict=True)
    ]
    return f"#{channels[0]:02X}{channels[1]:02X}{channels[2]:02X}"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    stripped = value.removeprefix("#")
    return (
        int(stripped[0:2], 16),
        int(stripped[2:4], 16),
        int(stripped[4:6], 16),
    )
