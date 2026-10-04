from __future__ import annotations

import re
from pathlib import Path

from djinn_in_a_box.core.workflow_publisher import NATIVE_ONLY_SPEC_MATRIX

_TABLE_HEADER = [
    "Name",
    "Kind",
    "Script path in workflow source root",
    "Carrier",
    "Event",
]
_TOOL_HEADINGS = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode"}


def _cells(line: str) -> list[str]:
    return [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]


def _tool_table(section: str, heading: str) -> list[list[str]]:
    match = re.search(rf"^#### {re.escape(heading)}\s*$", section, re.MULTILINE)
    assert match is not None, f"Missing table for {heading}"
    following_heading = re.search(r"^#{2,4} ", section[match.end() :], re.MULTILINE)
    end = (
        match.end() + following_heading.start()
        if following_heading is not None
        else len(section)
    )
    table_lines = [
        line.strip()
        for line in section[match.end() : end].splitlines()
        if line.strip().startswith("|")
    ]
    assert len(table_lines) >= 2, f"Missing table rows for {heading}"
    assert _cells(table_lines[0]) == _TABLE_HEADER
    return [_cells(line) for line in table_lines[2:]]


def test_native_only_workflow_tables_match_spec_matrix() -> None:
    implementation = Path(__file__).resolve().parents[1] / "IMPLEMENTATION.md"
    markdown = implementation.read_text(encoding="utf-8")
    section_match = re.search(
        r"^### Native-only workflow artifacts\s*$", markdown, re.MULTILINE
    )
    assert section_match is not None
    following_heading = re.search(r"^## ", markdown[section_match.end() :], re.MULTILINE)
    section_end = (
        section_match.end() + following_heading.start()
        if following_heading is not None
        else len(markdown)
    )
    section = markdown[section_match.end() : section_end]

    expected_headings = set(_TOOL_HEADINGS.values())
    actual_headings = set(re.findall(r"^#### (.+)$", section, re.MULTILINE))
    assert actual_headings == expected_headings

    for tool, heading in _TOOL_HEADINGS.items():
        expected_rows = [
            [
                item.name,
                item.kind,
                item.script_path.as_posix(),
                item.carrier_path.as_posix() if item.carrier_path is not None else "—",
                item.event if item.event is not None else "—",
            ]
            for item in NATIVE_ONLY_SPEC_MATRIX[tool]
        ]
        assert _tool_table(section, heading) == expected_rows
