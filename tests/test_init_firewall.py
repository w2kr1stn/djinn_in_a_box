from __future__ import annotations

import re
from pathlib import Path


def test_debian_registry_allowed():
    source = (Path(__file__).resolve().parents[1] / "scripts/init-firewall.sh").read_text()
    array = re.search(r"(?ms)^ALLOWED_DOMAINS=\((.*?)^\)", source)
    assert array is not None
    registries = array.group(1).split("# Package registries", 1)[1].split("#", 1)[0]
    assert '"deb.debian.org"' in registries


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


def test_sign_in_token_hosts_match_bundled_clis() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "init-firewall.sh"
    ).read_text(encoding="utf-8")
    array = re.search(r"(?ms)^ALLOWED_DOMAINS=\((.*?)^\)", script)
    assert array is not None
    body = array.group(1)
    group = body.split("# Sign-in token endpoints", 1)[1].split("#", 1)[0]
    sign_in_hosts = set(re.findall(r"[\"']([^\"']+)[\"']", group))
    entries = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    domains = re.findall(r"[\"']([^\"']+)[\"']", entries)

    assert sign_in_hosts == {"platform.claude.com", "auth.openai.com"}
    assert "console.anthropic.com" not in domains
