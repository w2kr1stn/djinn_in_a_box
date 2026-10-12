from __future__ import annotations

import re
from pathlib import Path

from djinn_in_a_box.config.defaults import VOLUME_CATEGORIES
from djinn_in_a_box.core.docker import MANAGED_TARGET_ROOTS, MANAGED_VOLUME_REPAIR_TARGETS

_ROOT = Path(__file__).resolve().parents[1]


def _bounded_block(markdown: str, start: str, end: str) -> str:
    start_match = re.search(re.escape(start), markdown)
    assert start_match is not None, f"Missing section start: {start}"
    end_match = re.search(re.escape(end), markdown[start_match.end() :])
    assert end_match is not None, f"Missing section end: {end}"
    return markdown[start_match.end() : start_match.end() + end_match.start()]


def _heading_section(markdown: str, heading: str) -> tuple[int, str]:
    matches = list(re.finditer(rf"^## {re.escape(heading)}\s*$", markdown, re.MULTILINE))
    if not matches:
        return 0, ""
    start = matches[0].end()
    following_heading = re.search(r"^## ", markdown[start:], re.MULTILINE)
    end = start + following_heading.start() if following_heading else len(markdown)
    return len(matches), markdown[start:end]


def _table_rows(section: str) -> list[list[str]]:
    return [
        [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.strip().startswith("|")
    ]


def test_readme_package_volume_row_matches_compose_and_category_registry() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    section = _bounded_block(
        readme,
        "Named volumes are Docker-managed and host-local:",
        "The backup command includes credentials",
    )

    category_matches = [
        category for category, names in VOLUME_CATEGORIES.items() if "djinn-packages" in names
    ]
    assert len(category_matches) == 1

    volume_names = re.findall(r"(?m)^  packages:\s*\n    name: (\S+)\s*$", compose)
    mount_targets = re.findall(r"(?m)^\s+- packages:(\S+)\s*$", compose)
    assert volume_names == ["djinn-packages"]
    assert mount_targets == ["/var/lib/djinn-packages"]

    rows = _table_rows(section)
    package_rows = [row for row in rows if row and row[0] == volume_names[0]]
    assert package_rows == [[volume_names[0], mount_targets[0], category_matches[0]]]
    target = Path(mount_targets[0])
    assert target in MANAGED_TARGET_ROOTS and target not in MANAGED_VOLUME_REPAIR_TARGETS


def test_package_docs_describe_root_owned_list() -> None:
    readme = (_ROOT / "README.md").read_text()
    assert "sudo sed -i '/^NAME$/d' /var/lib/djinn-packages/packages.txt" in readme
    assert "root:root 0644" in readme and "root:root 0755" in readme
    assert "Agents cannot change it" in readme
    for name in (
        "README.md",
        "SECURITY-MODEL.md",
        "IMPLEMENTATION.md",
        "CHANGELOG.md",
        "docs/sync-across-machines.md",
    ):
        source = (_ROOT / name).read_text()
        assert "/home/dev/.local/share/djinn-packages" not in source
        assert "/var/lib/djinn-packages" in source


def test_runtime_package_security_heading_and_links_are_canonical() -> None:
    security = (_ROOT / "SECURITY-MODEL.md").read_text(encoding="utf-8")
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")

    heading_count, _ = _heading_section(security, "Runtime package installs")
    assert heading_count == 1

    _, threat_model = _heading_section(security, "Threat model")
    assert re.search(r"\[[^\]]+\]\(#runtime-package-installs\)", threat_model)
    assert re.search(r"\[[^\]]+\]\(SECURITY-MODEL\.md#runtime-package-installs\)", readme)
