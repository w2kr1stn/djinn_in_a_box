from __future__ import annotations

import re
from pathlib import Path


def test_allowlist_excludes_google_and_keeps_supported_api_hosts() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "init-firewall.sh"
    ).read_text(encoding="utf-8")
    array = re.search(r"(?ms)^ALLOWED_DOMAINS=\((.*?)^\)", script)
    assert array is not None
    entries = "\n".join(line.split("#", 1)[0] for line in array.group(1).splitlines())
    domains = re.findall(r"[\"']([^\"']+)[\"']", entries)

    assert all("google" not in domain.casefold() for domain in domains)
    assert {"api.anthropic.com", "api.openai.com", "opencode.ai"}.issubset(domains)
