"""The host allowlist contract, without DNS or Docker access."""

import socket
from unittest.mock import Mock

import pytest

from djinn_in_a_box.core.egress import EgressRule, plan_egress
from djinn_in_a_box.core.exceptions import EgressSpecificationError


@pytest.mark.parametrize(
    ("entry", "normalized", "token", "description"),
    [
        ("8.8.8.8", "8.8.8.8", "8.8.8.8/32", "8.8.8.8/32 any"),
        ("8.8.8.8:1", "8.8.8.8:1", "8.8.8.8/32:1", "8.8.8.8/32 tcp/1"),
        ("8.8.8.8:65535", "8.8.8.8:65535", "8.8.8.8/32:65535", "8.8.8.8/32 tcp/65535"),
        ("10.0.0.0/8", "10.0.0.0/8", "10.0.0.0/8", "10.0.0.0/8 any"),
        ("10.2.0.0/16:443", "10.2.0.0/16:443", "10.2.0.0/16:443", "10.2.0.0/16 tcp/443"),
        ("172.16.0.0/12", "172.16.0.0/12", "172.16.0.0/12", "172.16.0.0/12 any"),
        ("192.168.0.0/16", "192.168.0.0/16", "192.168.0.0/16", "192.168.0.0/16 any"),
        ("100.64.0.0/10", "100.64.0.0/10", "100.64.0.0/10", "100.64.0.0/10 any"),
        (
            "100.100.1.2/32:8080",
            "100.100.1.2/32:8080",
            "100.100.1.2/32:8080",
            "100.100.1.2/32 tcp/8080",
        ),
        ("API.Example:443", "api.example:443", "8.8.8.8/32:443", "8.8.8.8/32 tcp/443"),
        ("MODEL", "model", "8.8.8.8/32", "8.8.8.8/32 any"),
        ("a" * 63 + ".com", "a" * 63 + ".com", "8.8.8.8/32", "8.8.8.8/32 any"),
        (
            ".".join(["a" * 63] * 3 + ["b" * 61]),
            ".".join(["a" * 63] * 3 + ["b" * 61]),
            "8.8.8.8/32",
            "8.8.8.8/32 any",
        ),
        ("ab-c.123a", "ab-c.123a", "8.8.8.8/32", "8.8.8.8/32 any"),
    ],
)
def test_accepted_forms(entry, normalized, token, description):
    resolver = Mock(return_value=["8.8.8.8"])
    plan = plan_egress([entry], resolve=resolver)
    assert plan.entries == ((normalized, plan.rules),)
    assert plan.environment_value() == token
    assert plan.rules[0].describe() == description
    if normalized[0].isdigit():
        resolver.assert_not_called()
        assert plan.pins == ()
    else:
        resolver.assert_called_once_with(normalized.partition(":")[0])
        assert plan.pins == ((normalized.partition(":")[0], "8.8.8.8"),)


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("", "empty"),
        ("*", "wildcards"),
        ("*.example.com", "wildcards"),
        ("api example.com", "whitespace"),
        ("example.com\n", "whitespace"),
        ("example.com\t", "whitespace"),
        ("example.com\u2003", "whitespace"),
        ("::1", "IPv6"),
        ("[2001:db8::1]:443", "IPv6"),
        ("https://example.com", "port"),
        ("example.com/path", "name"),
        (":443", "name"),
        ("example.com:", "port"),
        ("example.com:0", "port"),
        ("example.com:65536", "port"),
        ("example.com:0443", "port"),
        ("example.com:+443", "port"),
        ("example.com:４４３", "port"),
        ("example.com:99999999999999999999", "port"),
        ("134744072", "canonical"),
        ("0x08080808", "canonical"),
        ("010.010.010.010", "canonical"),
        ("8.8.8", "canonical"),
        ("8.8.8.8/7", "prefix"),
        ("8.8.8.8/33", "prefix"),
        ("8.8.8.8/-8", "prefix"),
        ("8.8.8.8/", "prefix"),
        ("10.1.0.0/8", "host bits"),
        ("169.0.0.0/8", "excluded"),
        ("0.0.0.0/8", "excluded"),
        ("127.0.0.1", "excluded"),
        ("169.254.1.1", "excluded"),
        ("224.0.0.0/4", "prefix"),
        ("224.0.0.0/8", "excluded"),
        ("240.0.0.0/4", "prefix"),
        ("240.0.0.0/8", "excluded"),
        ("255.255.255.255", "excluded"),
        ("localhost", "already mapped"),
        ("x.LOCALHOST", "already mapped"),
        ("HOST.DOCKER.INTERNAL", "already mapped"),
        ("DJINN", "already mapped"),
        ("a" * 64 + ".com", "name"),
        (".".join(["a" * 63] * 3 + ["b" * 62]), "name"),
        (".example.com", "name"),
        ("example.com.", "name"),
        ("a..com", "name"),
        ("-api.com", "name"),
        ("api-.com", "name"),
        ("a_b.com", "name"),
        ("example.123", "name"),
        ("999.999.999.999", "name"),
        ("éxample.com", "name"),
        ("example.com\x00", "name"),
        ("example.com/24", "name"),
        ("example.com\\path", "name"),
    ],
)
def test_refused_forms(entry, reason):
    resolver = Mock(return_value=["8.8.8.8"])
    with pytest.raises(EgressSpecificationError) as exc:
        plan_egress([entry], resolve=resolver)
    assert repr(entry) in str(exc.value)
    assert reason in str(exc.value)
    assert len(str(exc.value).splitlines()) == 1
    resolver.assert_not_called()


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.1.1",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "192.0.2.1",
    ],
)
@pytest.mark.parametrize("mixed", [False, True])
def test_nonpublic_name_answers(ip, mixed):
    with pytest.raises(EgressSpecificationError) as exc:
        plan_egress(["api.example:443"], resolve=lambda _: ["8.8.8.8", ip] if mixed else [ip])
    assert "api.example:443" in str(exc.value)
    assert ip in str(exc.value)
    assert "list the address or network instead" in str(exc.value)


@pytest.mark.parametrize(
    "failure",
    [
        socket.gaierror(-2, "Name or service not known"),
        UnicodeError("encoding failed\non resolver"),
        OSError("Temporary failure in name resolution"),
    ],
)
def test_resolution_errors(failure):
    with pytest.raises(EgressSpecificationError, match="api.example.*resolution failed") as exc:
        plan_egress(["api.example"], resolve=Mock(side_effect=failure))
    assert len(str(exc.value).splitlines()) == 1


def test_no_a_records():
    with pytest.raises(EgressSpecificationError, match="api.example.*no IPv4 A records"):
        plan_egress(["api.example"], resolve=lambda _: [])


def test_empty_plan():
    with pytest.raises(EgressSpecificationError, match="at least one"):
        plan_egress([])


def test_resolution_snapshot_dedup_order_and_count():
    resolver = Mock(return_value=["8.8.8.8", "1.1.1.1", "8.8.8.8"])
    entries = ["Name:8443", "name", "NAME:443", "name:443", "8.8.8.8:443", "name:80"]
    plan = plan_egress(entries, resolve=resolver)
    resolver.assert_called_once_with("name")
    assert plan.pins == (("name", "1.1.1.1"), ("name", "8.8.8.8"))
    assert plan.rules == tuple(
        EgressRule(f"{ip}/32", port)
        for ip in ("1.1.1.1", "8.8.8.8")
        for port in (None, 80, 443, 8443)
    )
    assert plan.entries == tuple(sorted(plan.entries))
    assert len(plan.entries) == 5
    assert dict(plan.entries)["name:443"] == (
        EgressRule("1.1.1.1/32", 443),
        EgressRule("8.8.8.8/32", 443),
    )
    assert len(plan.rules) == len(plan.environment_value().split()) == 8
    assert plan.environment_value().split() == sorted(set(plan.environment_value().split()))
    assert plan == plan_egress(reversed(entries), resolve=lambda _: ["1.1.1.1", "8.8.8.8"])


def test_default_resolver_uses_ipv4_stream_and_keeps_every_answer(monkeypatch):
    resolver = Mock(
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
        ]
    )
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    plan = plan_egress(["api.example:443"])
    resolver.assert_called_once_with("api.example", None, socket.AF_INET, socket.SOCK_STREAM)
    assert plan.environment_value() == "1.1.1.1/32:443 8.8.8.8/32:443"


def test_no_artificial_entry_or_rule_bound():
    entries = [f"api{i}.example" for i in range(65)]
    answers = [f"8.8.{i // 256}.{i % 256}" for i in range(257)]
    plan = plan_egress(entries, resolve=lambda _: answers)
    assert len(plan.entries) == 65
    assert len(plan.rules) == 257


def test_prefix_leading_zeros_normalize_without_an_uncontracted_restriction():
    plan = plan_egress(["10.0.0.0/0008", "10.0.0.0/8"])
    assert plan.environment_value() == "10.0.0.0/8"
    assert plan.entries == (("10.0.0.0/8", (EgressRule("10.0.0.0/8"),)),)


@pytest.mark.parametrize("entry", ["\ud800.example", "example.\x00"])
def test_libc_encoding_or_nul_classification_still_refuses_cleanly(entry):
    with pytest.raises(EgressSpecificationError, match="name"):
        plan_egress([entry], resolve=lambda _: pytest.fail("invalid name resolved"))


def test_egress_error_is_a_value_error():
    assert issubclass(EgressSpecificationError, ValueError)
