import ast
import re
from pathlib import Path

import pytest

from djinn_in_a_box.core.theme import (
    BORDER,
    DJINN_THEME,
    ERROR,
    INFO,
    MUTED,
    PATH,
    PRIMARY,
    SECONDARY,
    SUCCESS,
    WARNING,
)

ROOT = Path(__file__).resolve().parents[2]
THEME_SOURCE = ROOT / "src" / "djinn_in_a_box" / "core" / "theme.py"
HEX_COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}\b")
NAMED_COLOR_RICH_TAG_PATTERN = re.compile(
    r"\[/?(?:[a-z]+\s+)*(cyan|magenta|green|red|yellow|blue|white|black)"
    r"(?:\s+[a-z]+)*\]",
    re.IGNORECASE,
)
NAMED_COLOR_STYLE_TOKENS = {
    "cyan",
    "magenta",
    "green",
    "red",
    "yellow",
    "blue",
    "white",
    "black",
}
RICH_STYLE_MODIFIER_TOKENS = {
    "bold",
    "dim",
    "italic",
    "underline",
    "blink",
    "blink2",
    "reverse",
    "strike",
    "not",
    "overline",
}

EXPECTED_PALETTE = {
    "primary": "#69B9A1",
    "secondary": "#226666",
    "success": "#C1FF62",
    "error": "#F53263",
    "warning": "#FAF870",
    "info": "blue",
    "path": "#8608B8",
    "muted": "#333676",
    "border": "#29526D",
}

PALETTE_VALUES = frozenset(EXPECTED_PALETTE.values())
HEX_PALETTE_VALUES = frozenset(
    value for value in EXPECTED_PALETTE.values() if value.startswith("#")
)
DJINN_ROLE_NAMES = (
    "success",
    "error",
    "warning",
    "info",
    "info.bold",
    "path",
    "primary",
    "primary.bold",
    "secondary",
    "secondary.bold",
    "muted",
    "border",
    "header",
    "status.enabled",
    "status.disabled",
    "status.error",
    "table.title",
    "table.header",
    "table.category",
    "table.value",
)


def _style_color_value(role_name: str) -> str:
    style = DJINN_THEME.styles[role_name]
    assert style.color is not None
    triplet = style.color.triplet
    if triplet is None:
        return style.color.name
    return f"#{triplet.red:02X}{triplet.green:02X}{triplet.blue:02X}"


@pytest.mark.parametrize("role_name", DJINN_ROLE_NAMES)
def test_theme_role_color_comes_from_palette(role_name: str) -> None:
    assert role_name in DJINN_THEME.styles
    assert _style_color_value(role_name) in PALETTE_VALUES


def test_palette_constants_are_authoritative_values() -> None:
    assert {
        "primary": PRIMARY,
        "secondary": SECONDARY,
        "success": SUCCESS,
        "error": ERROR,
        "warning": WARNING,
        "info": INFO,
        "path": PATH,
        "muted": MUTED,
        "border": BORDER,
    } == EXPECTED_PALETTE


def test_theme_role_remappings_follow_spec() -> None:
    assert _style_color_value("header") == PRIMARY
    assert DJINN_THEME.styles["header"].bold is True
    assert _style_color_value("table.title") == PRIMARY
    assert DJINN_THEME.styles["table.title"].bold is True
    assert _style_color_value("table.category") == SECONDARY
    assert _style_color_value("table.value") == MUTED
    assert _style_color_value("status.enabled") == SUCCESS
    assert _style_color_value("status.disabled") == WARNING
    assert _style_color_value("status.error") == ERROR
    assert _style_color_value("path") == PATH
    assert _style_color_value("info") == INFO
    assert DJINN_THEME.styles["info"].color is not None
    assert DJINN_THEME.styles["info"].color.number == 4
    assert DJINN_THEME.styles["info"].bold is not True
    assert DJINN_THEME.styles["info.bold"].bold is True


@pytest.mark.parametrize("hex_value", HEX_PALETTE_VALUES)
def test_each_palette_hex_appears_once_in_theme_source(hex_value: str) -> None:
    source = THEME_SOURCE.read_text(encoding="utf-8").casefold()
    assert source.count(hex_value.casefold()) == 1


@pytest.mark.parametrize(
    "relative_directory",
    [
        Path("src") / "djinn_in_a_box" / "commands",
        Path("src") / "djinn_in_a_box" / "cli",
    ],
)
def test_no_command_or_cli_hex_color_literals(relative_directory: Path) -> None:
    directory = ROOT / relative_directory
    if not directory.exists():
        return

    matches: list[str] = []
    for path in directory.rglob("*.py"):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if HEX_COLOR_PATTERN.search(line):
                matches.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")

    assert matches == []


def _is_named_color_style_literal(value: str) -> bool:
    tokens = value.strip().casefold().split()
    if not tokens:
        return False
    if not any(token in NAMED_COLOR_STYLE_TOKENS for token in tokens):
        return False
    return all(
        token in NAMED_COLOR_STYLE_TOKENS or token in RICH_STYLE_MODIFIER_TOKENS
        for token in tokens
    )


@pytest.mark.parametrize(
    "relative_directory",
    [
        Path("src") / "djinn_in_a_box" / "commands",
        Path("src") / "djinn_in_a_box" / "cli",
    ],
)
def test_no_command_or_cli_named_color_style_literals(relative_directory: Path) -> None:
    directory = ROOT / relative_directory
    if not directory.exists():
        return

    matches: list[str] = []
    for path in directory.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            if NAMED_COLOR_RICH_TAG_PATTERN.search(line):
                matches.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _is_named_color_style_literal(node.value)
            ):
                matches.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value!r}")

    assert matches == []
