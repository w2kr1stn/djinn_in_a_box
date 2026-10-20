from pathlib import Path


def test_hostctl_approval_limits_documented():
    root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "SECURITY-MODEL.md"):
        text = " ".join((root / name).read_text().split())
        assert "`checkPeriod` (default 12 hours)" in text
        assert (
            "exact cache key across source node, SSH user and destination is undocumented" in text
        )
        assert (
            'With `checkPeriod: "always"`, a host check does not approve '
            "the agent's later connection." in text
        )
        assert "Each check is a real login as the target user running `true`" in text
        assert (
            "visible as a connection in the relay log and as a login/session "
            "in the target's auth log." in text
        )


def test_hostctl_check_usage_documented():
    root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "IMPLEMENTATION.md"):
        assert "djinn hostctl check" in (root / name).read_text()
