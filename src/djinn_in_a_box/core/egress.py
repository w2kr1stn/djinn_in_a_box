"""Validate and resolve the destinations of one headless allowlist run."""

from __future__ import annotations

import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network

from djinn_in_a_box.core.exceptions import EgressSpecificationError

_EXCLUDED = tuple(
    IPv4Network(block)
    for block in ("0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4")
)
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", re.ASCII)


@dataclass(frozen=True, slots=True)
class EgressRule:
    network: str
    port: int | None = None

    def describe(self) -> str:
        return f"{self.network} " + (f"tcp/{self.port}" if self.port is not None else "any")


@dataclass(frozen=True, slots=True)
class EgressPlan:
    entries: tuple[tuple[str, tuple[EgressRule, ...]], ...]
    rules: tuple[EgressRule, ...]
    pins: tuple[tuple[str, str], ...]

    def environment_value(self) -> str:
        return " ".join(
            sorted(
                rule.network + (f":{rule.port}" if rule.port is not None else "")
                for rule in self.rules
            )
        )


def _rule_key(rule: EgressRule) -> tuple[str, bool, int]:
    return rule.network, rule.port is not None, rule.port or 0


def _resolve_ipv4(name: str) -> Iterable[str]:
    return (
        str(answer[4][0])
        for answer in socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
    )


def _refuse(entry: str, reason: str) -> EgressSpecificationError:
    return EgressSpecificationError(f"--allow {entry!r}: {' '.join(reason.splitlines())}")


def plan_egress(
    entries: Iterable[str],
    *,
    resolve: Callable[[str], Iterable[str]] | None = None,
) -> EgressPlan:
    """Build one immutable IPv4 snapshot, resolving each normalized name once."""
    resolver = resolve if resolve is not None else _resolve_ipv4
    snapshot: dict[str, tuple[str, ...]] = {}
    planned: dict[str, tuple[EgressRule, ...]] = {}
    pins: set[tuple[str, str]] = set()
    for entry in entries:
        if not entry or "*" in entry or any(char.isspace() for char in entry):
            raise _refuse(entry, "empty entries, wildcards and whitespace are forbidden")
        if entry.count(":") > 1:
            raise _refuse(entry, "IPv6 and URL schemes are forbidden; use IPv4 or a name")
        host, separator, port_text = entry.partition(":")
        port = None
        if separator:
            if (
                not re.fullmatch(r"[1-9][0-9]{0,4}", port_text, re.ASCII)
                or not 1 <= int(port_text) <= 65535
            ):
                raise _refuse(entry, "port must be 1–65535 without leading zeros")
            port = int(port_text)
        address, slash, prefix = host.partition("/")
        prefix = prefix.lstrip("0")
        try:
            literal = IPv4Address(address)
        except ValueError:
            literal = None
        if literal is not None:
            if slash and (
                not re.fullmatch(r"[0-9]{1,2}", prefix, re.ASCII) or not 8 <= int(prefix) <= 32
            ):
                raise _refuse(entry, "IPv4 prefix must be 8–32")
            try:
                network = IPv4Network(f"{literal}/{prefix if slash else 32}", strict=True)
            except ValueError as exc:
                raise _refuse(entry, f"IPv4 network must have zero host bits ({exc})") from None
            if any(network.overlaps(block) for block in _EXCLUDED):
                raise _refuse(entry, "IPv4 network overlaps an excluded address block")
            normalized = str(network) if slash else str(literal)
            rules = (EgressRule(str(network), port),)
        else:
            try:
                socket.inet_aton(address)
            except (OSError, UnicodeError, ValueError):
                pass
            else:
                raise _refuse(entry, "IPv4 address must use canonical dotted-quad spelling")
            if (
                slash
                or len(host) > 253
                or any(_LABEL.fullmatch(label) is None for label in host.split("."))
                or host.rsplit(".", 1)[-1].isascii()
                and host.rsplit(".", 1)[-1].isdigit()
            ):
                raise _refuse(
                    entry,
                    "name must use ASCII labels of 1–63 characters, at most "
                    "253 total, with no edge hyphen/dot or numeric last label",
                )
            normalized = host.lower()
            if normalized in {"localhost", "host.docker.internal", "djinn"} or normalized.endswith(
                ".localhost"
            ):
                raise _refuse(entry, "name is already mapped by the container")
            if normalized not in snapshot:
                try:
                    answers = sorted({str(IPv4Address(ip)) for ip in resolver(normalized)})
                except (OSError, UnicodeError, ValueError) as exc:
                    raise _refuse(entry, f"IPv4 resolution failed ({exc})") from None
                if not answers:
                    raise _refuse(entry, "name has no IPv4 A records")
                for ip in answers:
                    value = IPv4Address(ip)
                    if not value.is_global or any(value in block for block in _EXCLUDED):
                        raise _refuse(
                            entry,
                            f"name resolves to non-public address {ip}; "
                            "list the address or network instead",
                        )
                snapshot[normalized] = tuple(answers)
            rules = tuple(EgressRule(f"{ip}/32", port) for ip in snapshot[normalized])
            pins.update((normalized, ip) for ip in snapshot[normalized])
        if port is not None:
            normalized += f":{port}"
        planned[normalized] = rules
    if not planned:
        raise EgressSpecificationError("--allow requires at least one destination")
    return EgressPlan(
        entries=tuple(sorted(planned.items())),
        rules=tuple(sorted({rule for rules in planned.values() for rule in rules}, key=_rule_key)),
        pins=tuple(sorted(pins)),
    )
