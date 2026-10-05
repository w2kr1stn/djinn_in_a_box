from pathlib import Path


def test_git_usage_and_no_host_ssh_delivery_drift():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text()
    implementation = (root / "IMPLEMENTATION.md").read_text()
    compose = (root / "docker-compose.yml").read_text()
    for text in (readme, implementation):
        assert "SSH_AUTH_SOCK" in text
        assert "git.signing_identity" in text or "signing default" in text
        assert "original" in text and "includeIf" in text
        assert "host terminal" in text
    assert "[git.identities.git-work]" in readme
    assert "~/.ssh:/home/dev/.ssh" not in compose
    assert "| `~/.ssh` |" not in readme
    assert "read-only `~/.ssh` and" not in implementation
