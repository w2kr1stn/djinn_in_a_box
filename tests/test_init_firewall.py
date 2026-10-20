from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


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


# ---------------------------------------------------------------------------
# Allowlist mode (`--allow`) and the unchanged default mode
# ---------------------------------------------------------------------------
# The copies run here substitute every privileged path with a fake that keeps
# kernel-like rule state (formats as measured on iptables 1.8.9 nf_tables), so
# the script's call sequence and its readback comparison are the real ones.

ROOT = Path(__file__).resolve().parents[1]
FIREWALL = ROOT / "scripts" / "init-firewall.sh"
SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"

FAKE_IPTABLES = r"""#!{python}
import json, sys
from pathlib import Path

STATE = Path({state!r})
V6 = {v6!r}
NAME = "ip6tables" if V6 else "iptables"
argv = sys.argv[1:]
log = STATE / "calls.log"
with log.open("a") as handle:
    handle.write(json.dumps([NAME, argv]) + "\n")
mode_path = STATE / (NAME + ".mode")
mode = json.loads(mode_path.read_text()) if mode_path.exists() else {{}}
count = sum(1 for line in log.read_text().splitlines() if json.loads(line)[0] == NAME)
if mode.get("fail_all") or mode.get("fail_call") == count:
    sys.stderr.write("fake " + NAME + " failure\n")
    sys.exit(4)
rules_path = STATE / (NAME + ".rules")
rules = json.loads(rules_path.read_text()) if rules_path.exists() else []
STATE_RANK = {{"RELATED": 0, "ESTABLISHED": 1}}


def canonical(args):
    out = ["-A", "OUTPUT"]
    index = 2
    while index < len(args):
        item = args[index]
        if item == "-d":
            value = args[index + 1]
            out += ["-d", value if "/" in value else value + "/32"]
            index += 2
        elif item == "-p":
            out += ["-p", args[index + 1]]
            index += 2
        elif item == "--dport":
            at = out.index("-p")
            out = out[: at + 2] + ["-m", args[at + 1], "--dport", args[index + 1]]
            index += 2
        elif item == "-m":
            out += ["-m", args[index + 1]]
            index += 2
        elif item == "--state":
            parts = sorted(args[index + 1].split(","), key=STATE_RANK.get)
            out += ["--state", ",".join(parts)]
            index += 2
        elif item == "-j":
            out += ["-j", args[index + 1]]
            if args[index + 1] == "REJECT":
                kind = "icmp6" if V6 else "icmp"
                out += ["--reject-with", kind + "-port-unreachable"]
            index += 2
        else:
            out.append(item)
            index += 1
    return " ".join(out)


if argv[:2] == ["-F", "OUTPUT"]:
    rules = []
elif argv[:2] == ["-A", "OUTPUT"]:
    rules.append(canonical(argv))
elif argv[:2] == ["-S", "OUTPUT"]:
    shown = list(rules)
    policy = mode.get("policy", "ACCEPT")
    if mode.get("readback") == "extra":
        shown.insert(-1, "-A OUTPUT -d 198.51.100.1/32 -j ACCEPT")
    elif mode.get("readback") == "no_reject":
        shown = shown[:-1]
    elif mode.get("readback") == "reorder":
        shown[1], shown[2] = shown[2], shown[1]
    sys.stdout.write("\n".join(["-P OUTPUT " + policy] + shown) + "\n")
else:
    sys.exit(2)
rules_path.write_text(json.dumps(rules))
"""


class Fakes:
    """Stateful fake iptables/ip6tables plus the call log they share."""

    def __init__(self, base: Path) -> None:
        self.state = base / "state"
        self.state.mkdir(parents=True)
        self.bin = base / "fakebin"
        self.bin.mkdir()
        for name, v6 in (("iptables", False), ("ip6tables", True)):
            path = self.bin / name
            path.write_text(
                FAKE_IPTABLES.format(python=sys.executable, state=str(self.state), v6=v6)
            )
            path.chmod(0o755)

    def mode(self, name: str, **settings: object) -> None:
        (self.state / f"{name}.mode").write_text(json.dumps(settings))

    def calls(self, name: str | None = None) -> list[tuple[str, list[str]]]:
        log = self.state / "calls.log"
        if not log.exists():
            return []
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        return [(row[0], row[1]) for row in rows if name is None or row[0] == name]


def substitute(source: str, name: str, old: str, new: str | Path) -> str:
    assignment = f"{name}='{old}'"
    assert source.count(assignment) == 1, assignment
    return source.replace(assignment, f"{name}={shlex.quote(str(new))}")


def function_body(source: str) -> str:
    start = source.index("_djinn_allowlist_mode() {")
    end = source.index('if [[ "${1-}" == "--allow" ]]')
    return source[start:end]


def command_text(body: str) -> str:
    """Body without comments and without quoted strings (except the bin variables)."""
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        stripped = stripped.replace('"$iptables_bin"', "BIN4").replace('"$ip6tables_bin"', "BIN6")
        lines.append(re.sub(r'"[^"]*"|\'[^\']*\'', '""', stripped))
    return "\n".join(lines)


def assert_allowlist_dispatch(source: str) -> None:
    """Fail BEFORE running copies if a privileged dispatch escapes substitution."""
    assert source.count("local iptables_bin='/usr/sbin/iptables'") == 1
    assert source.count("local ip6tables_bin='/usr/sbin/ip6tables'") == 1
    assert source.count("local if_inet6_path='/proc/net/if_inet6'") == 1
    text = command_text(function_body(source))
    assert not re.search(r"(?<![\w$])(ip6?tables|setpriv|getent)\b", text)
    assert not re.search(r"(^|[\s;&|(])(source|eval)\s", text)
    assert not re.search(r"(^|[;&|(]\s*)\.\s", text)
    assert not re.search(r"\$\{?(OUTPUT_LIB|PATH)\b.*\bBIN", text)


def firewall_copy(
    base: Path,
    fakes: Fakes,
    *,
    inet6: str | None = "lo-only",
    mutate: tuple[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    """Return (script copy, poisoned library marker, if_inet6 file)."""
    source = FIREWALL.read_text()
    assert_allowlist_dispatch(source)
    marker = base / "library-ran"
    library = base / "poison-lib.sh"
    library.write_text(f"touch {shlex.quote(str(marker))}\n")
    inet6_path = base / "if_inet6"
    if inet6 == "lo-only":
        inet6_path.write_text("00000000000000000000000000000001 01 80 10 80       lo\n")
    elif inet6 is not None:
        inet6_path.write_text(inet6)
    source = substitute(source, "local iptables_bin", "/usr/sbin/iptables", fakes.bin / "iptables")
    source = substitute(
        source, "local ip6tables_bin", "/usr/sbin/ip6tables", fakes.bin / "ip6tables"
    )
    source = substitute(source, "local if_inet6_path", "/proc/net/if_inet6", inet6_path)
    default = 'OUTPUT_LIB="${OUTPUT_LIB:-/home/dev/output-lib.sh}"'
    assert source.count(default) == 1
    source = source.replace(default, f'OUTPUT_LIB="${{OUTPUT_LIB:-{library}}}"')
    if mutate is not None:
        assert source.count(mutate[0]) == 1, mutate[0]
        source = source.replace(mutate[0], mutate[1])
    script = base / "init-firewall.sh"
    script.write_text(source)
    script.chmod(0o755)
    return script, marker, inet6_path


def run_allow(
    script: Path, tokens: str | None, fakes: Fakes, *, extra_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    path = os.environ["PATH"] if extra_path is None else f"{extra_path}:{os.environ['PATH']}"
    argv = ["bash", "-p", str(script), "--allow"]
    if tokens is not None:
        argv.append(tokens)
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        env={"PATH": path, "HOME": str(fakes.state)},
    )


def allow_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in result.stderr.splitlines() if line.startswith("djinn-egress:")]


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    return Fakes(tmp_path)


IPV4_PREFIX_CALLS = [
    ["-F", "OUTPUT"],
    ["-A", "OUTPUT", "-d", "127.0.0.11/32", "-j", "REJECT"],
    ["-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
    ["-A", "OUTPUT", "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
]


def test_allowlist_applies_exact_ipv4_sequence_and_prints_readback(
    tmp_path: Path, fakes: Fakes
) -> None:
    script, _, _ = firewall_copy(tmp_path, fakes)
    result = run_allow(script, "10.0.0.0/8 203.0.113.10/32:443 203.0.113.10/32:8443", fakes)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert [argv for _, argv in fakes.calls("iptables")] == [
        *IPV4_PREFIX_CALLS,
        ["-A", "OUTPUT", "-d", "10.0.0.0/8", "-j", "ACCEPT"],
        ["-A", "OUTPUT", "-d", "203.0.113.10/32", "-p", "tcp", "--dport", "443", "-j", "ACCEPT"],
        ["-A", "OUTPUT", "-d", "203.0.113.10/32", "-p", "tcp", "--dport", "8443", "-j", "ACCEPT"],
        ["-A", "OUTPUT", "-j", "REJECT"],
        ["-S", "OUTPUT"],
    ]
    assert [argv for _, argv in fakes.calls("ip6tables")] == [
        ["-F", "OUTPUT"],
        ["-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
        ["-A", "OUTPUT", "-j", "REJECT"],
        ["-S", "OUTPUT"],
    ]
    assert allow_lines(result) == [
        "djinn-egress: allow 10.0.0.0/8 any",
        "djinn-egress: allow 203.0.113.10/32 tcp/443",
        "djinn-egress: allow 203.0.113.10/32 tcp/8443",
    ]
    # The allow lines are the last output of the privileged phase.
    assert result.stderr.splitlines()[-3:] == allow_lines(result)


@pytest.mark.parametrize(
    "tokens",
    [
        "10.0.0.0/8",
        "1.2.3.4/32:1",
        "1.2.3.4/32:65535",
        "126.255.255.255/32",
        "128.0.0.0/8",
        "169.253.255.255/32",
        "169.255.0.0/16",
        "169.0.0.0/9",
        "223.255.255.255/32",
        "100.64.0.0/10",
        "172.16.0.0/12 192.168.0.0/16:8080",
        "1.0.0.0/8",
    ],
)
def test_allowlist_accepts_grammar_boundaries(tmp_path: Path, fakes: Fakes, tokens: str) -> None:
    script, _, _ = firewall_copy(tmp_path, fakes)
    result = run_allow(script, tokens, fakes)

    assert result.returncode == 0, result.stderr
    assert len(allow_lines(result)) == len(tokens.split())


REFUSED_TOKENS = [
    "",
    " ",
    " 10.0.0.0/8",
    "10.0.0.0/8 ",
    "10.0.0.0/8  10.0.0.1/32",
    "10.0.0.0/8\t10.0.0.1/32",
    "10.0.0.0/8\n10.0.0.1/32",
    "10.0.0.0/7",
    "10.0.0.0/33",
    "10.0.0.0/08",
    "10.0.0.0/0",
    "10.0.0.0",
    "10.0.0.1/8",
    "010.0.0.0/8",
    "256.0.0.0/8",
    "1.2.3/8",
    "1.2.3.4.5/32",
    "1.2.3.4/32:0",
    "1.2.3.4/32:65536",
    "1.2.3.4/32:0443",
    "1.2.3.4/32:",
    "1.2.3.4/32:80:90",
    "1.2.3.4/32:-1",
    ":443",
    "example.com",
    "example.com:443",
    "1.2.3.4/32,5.6.7.8/32",
    "::1",
    "*",
    "1.2.3.4/32 1.2.3.4/32",
    "1.2.3.4/32:443 1.2.3.4/32:443",
    "0.0.0.0/8",
    "0.1.2.3/32",
    "127.0.0.1/32",
    "127.0.0.0/8",
    "127.0.0.11/32",
    "169.254.0.0/16",
    "169.254.1.1/32",
    "169.0.0.0/8",
    "169.128.0.0/9",
    "224.0.0.0/8",
    "239.1.1.1/32",
    "240.0.0.0/8",
    "255.255.255.255/32",
    "10.0.0.0/8 127.0.0.1/32",
]


@pytest.mark.parametrize("tokens", REFUSED_TOKENS)
def test_allowlist_refuses_bad_tokens_before_touching_rules(
    tmp_path: Path, fakes: Fakes, tokens: str
) -> None:
    script, _, _ = firewall_copy(tmp_path, fakes)
    result = run_allow(script, tokens, fakes)

    assert result.returncode != 0
    assert fakes.calls() == []
    assert allow_lines(result) == []
    assert "[err]" in result.stderr


@pytest.mark.parametrize(
    "token",
    [
        "10.0.0.0/7",
        "10.0.0.0/33",
        "10.0.0.0/99",
        "256.0.0.0/8",
        "1.256.3.4/32",
        "1.2.256.4/32",
        "1.2.3.256/32",
        "1.2.3.4/32:65536",
        "1.2.3.4/32:99999",
    ],
)
def test_allowlist_names_range_violations(tmp_path: Path, fakes: Fakes, token: str) -> None:
    script, _, _ = firewall_copy(tmp_path, fakes)
    result = run_allow(script, token, fakes)

    assert result.returncode != 0
    assert "out of range" in result.stderr


@pytest.mark.parametrize("extra", [[], ["10.0.0.0/8", "10.0.0.0/8"]])
def test_allowlist_requires_exactly_one_argument(
    tmp_path: Path, fakes: Fakes, extra: list[str]
) -> None:
    script, _, _ = firewall_copy(tmp_path, fakes)
    result = subprocess.run(
        ["bash", "-p", str(script), "--allow", *extra],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        env={"PATH": os.environ["PATH"]},
    )

    assert result.returncode != 0
    assert fakes.calls() == []
    assert allow_lines(result) == []


@pytest.mark.parametrize("call", range(1, 8))
def test_allowlist_aborts_when_an_ipv4_step_fails(tmp_path: Path, fakes: Fakes, call: int) -> None:
    script, _, _ = firewall_copy(tmp_path, fakes)
    fakes.mode("iptables", fail_call=call)
    result = run_allow(script, "10.0.0.0/8 203.0.113.10/32:443", fakes)

    assert result.returncode != 0
    assert allow_lines(result) == []
    # The sequence stops at the failing call (the readback is call 8).
    assert len(fakes.calls("iptables")) == min(call, 8)


@pytest.mark.parametrize(
    "mode",
    [
        {"readback": "extra"},
        {"readback": "no_reject"},
        {"readback": "reorder"},
        {"policy": "DROP"},
    ],
)
def test_allowlist_fails_when_ipv4_readback_differs(
    tmp_path: Path, fakes: Fakes, mode: dict[str, str]
) -> None:
    script, _, _ = firewall_copy(tmp_path, fakes)
    fakes.mode("iptables", **mode)
    result = run_allow(script, "10.0.0.0/8 203.0.113.10/32:443", fakes)

    assert result.returncode != 0
    assert allow_lines(result) == []
    assert "readback differs" in result.stderr


@pytest.mark.parametrize(
    "mode", [{"readback": "extra"}, {"readback": "no_reject"}, {"policy": "DROP"}]
)
def test_allowlist_fails_when_ipv6_readback_differs(
    tmp_path: Path, fakes: Fakes, mode: dict[str, str]
) -> None:
    script, _, _ = firewall_copy(tmp_path, fakes)
    fakes.mode("ip6tables", **mode)
    result = run_allow(script, "10.0.0.0/8", fakes)

    assert result.returncode != 0
    assert allow_lines(result) == []
    assert "ip6tables: readback differs" in result.stderr


ETH0_V6 = (
    "00000000000000000000000000000001 01 80 10 80       lo\n"
    "fe800000000000000000000000000001 02 40 20 80     eth0\n"
)


@pytest.mark.parametrize("call", [1, 2, 3, 4])
def test_allowlist_fails_on_ipv6_failure_when_a_non_loopback_address_exists(
    tmp_path: Path, fakes: Fakes, call: int
) -> None:
    script, _, _ = firewall_copy(tmp_path, fakes, inet6=ETH0_V6)
    fakes.mode("ip6tables", fail_call=call)
    result = run_allow(script, "10.0.0.0/8", fakes)

    assert result.returncode != 0
    assert allow_lines(result) == []
    assert "other than lo" in result.stderr


@pytest.mark.parametrize("inet6", ["", "garbage\n", "x y\n"])
def test_allowlist_treats_malformed_if_inet6_as_present(
    tmp_path: Path, fakes: Fakes, inet6: str
) -> None:
    script, _, _ = firewall_copy(tmp_path, fakes, inet6=inet6)
    fakes.mode("ip6tables", fail_all=True)
    result = run_allow(script, "10.0.0.0/8", fakes)

    # An empty file lists no interface at all; anything else that is not `lo` fails closed.
    assert (result.returncode == 0) == (inet6 == "")


@pytest.mark.parametrize("inet6", ["lo-only", None, ""])
def test_allowlist_continues_without_ipv6_when_only_loopback_exists(
    tmp_path: Path, fakes: Fakes, inet6: str | None
) -> None:
    script, _, inet6_path = firewall_copy(tmp_path, fakes, inet6=inet6)
    fakes.mode("ip6tables", fail_all=True)
    result = run_allow(script, "10.0.0.0/8", fakes)

    assert result.returncode == 0, result.stderr
    assert allow_lines(result) == ["djinn-egress: allow 10.0.0.0/8 any"]
    assert "[warn] ip6tables unavailable" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_allowlist_fails_closed_when_if_inet6_is_unreadable(tmp_path: Path, fakes: Fakes) -> None:
    script, _, inet6_path = firewall_copy(tmp_path, fakes)
    inet6_path.chmod(0)
    fakes.mode("ip6tables", fail_all=True)
    result = run_allow(script, "10.0.0.0/8", fakes)

    assert result.returncode != 0
    assert allow_lines(result) == []
    assert "unreadable" in result.stderr


def test_allowlist_mode_sources_no_library_and_resolves_no_names(
    tmp_path: Path, fakes: Fakes
) -> None:
    script, marker, _ = firewall_copy(tmp_path, fakes)
    getent_marker = tmp_path / "getent-ran"
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    for name in ("getent", "iptables", "ip6tables", "setpriv"):
        fake = shadow / name
        fake.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(getent_marker))}\n")
        fake.chmod(0o755)
    result = run_allow(script, "10.0.0.0/8", fakes, extra_path=shadow)

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert not getent_marker.exists()
    assert "output library" not in result.stderr


def test_privileged_paths_use_no_source_dot_or_eval() -> None:
    text = command_text(function_body(FIREWALL.read_text()))
    wrapper = command_text((ROOT / "scripts" / "egress-init.sh").read_text())

    for body in (text, wrapper):
        assert not re.search(r"(^|[\s;&|(])(source|eval)\s", body)
        assert not re.search(r"(^|[;&|(]\s*)\.\s", body)
    # And the library is only ever reached by the default mode, after the dispatch.
    source = FIREWALL.read_text()
    assert source.index('if [[ "${1-}" == "--allow" ]]') < source.index('source "$OUTPUT_LIB"')


def test_allowlist_dispatch_precedes_library_and_default_rules() -> None:
    source = FIREWALL.read_text()
    dispatch = source.index('if [[ "${1-}" == "--allow" ]]')

    assert dispatch < source.index('if [[ -r "$OUTPUT_LIB" ]]')
    assert dispatch < source.index("# Whitelisted domains")


# --- default mode: byte-for-byte the same call sequence as before the allowlist ---

FAKE_GETENT = """#!/bin/sh
echo "$@" >> {log}
[ "$1" = ahostsv4 ] || exit 2
for failing in {failing}; do
    [ "$2" = "$failing" ] && exit 2
done
index=$(printf '%s' "$2" | cksum | cut -d' ' -f1)
a=$((index % 200 + 1))
echo "198.51.100.$a STREAM $2"
echo "198.51.100.$a DGRAM"
if [ $((index % 3)) -eq 0 ]; then
    echo "203.0.113.$a STREAM $2"
fi
"""


def default_mode_trace(
    base: Path, script: Path, *, failing: tuple[str, ...] = ()
) -> tuple[int, list[tuple[str, list[str]]], str]:
    fakes = Fakes(base / "default-mode")
    getent_log = base / "getent.log"
    getent = fakes.bin / "getent"
    getent.write_text(
        FAKE_GETENT.format(log=shlex.quote(str(getent_log)), failing=" ".join(failing))
    )
    getent.chmod(0o755)
    result = subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        env={
            "PATH": f"{fakes.bin}:{os.environ['PATH']}",
            "OUTPUT_LIB": str(base / "missing-lib.sh"),
        },
    )
    getent_calls = getent_log.read_text().splitlines() if getent_log.exists() else []
    return result.returncode, [*fakes.calls(), ("getent", getent_calls)], result.stderr


def expected_default_trace(domains: list[str], failing_at: str | None = None) -> list[object]:
    def ips(domain: str) -> list[str]:
        out = subprocess.run(
            ["cksum"], input=domain, capture_output=True, text=True, check=True
        ).stdout.split()[0]
        index = int(out)
        a = index % 200 + 1
        found = [f"198.51.100.{a}"]
        if index % 3 == 0:
            found.append(f"203.0.113.{a}")
        return sorted(found)

    trace: list[object] = [
        ("iptables", ["-F", "OUTPUT"]),
        ("iptables", ["-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"]),
        (
            "iptables",
            ["-A", "OUTPUT", "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ),
        ("iptables", ["-A", "OUTPUT", "-p", "udp", "--dport", "53", "-j", "ACCEPT"]),
        ("iptables", ["-A", "OUTPUT", "-p", "tcp", "--dport", "53", "-j", "ACCEPT"]),
    ]
    for network in ("172.16.0.0/12", "192.168.0.0/16", "10.0.0.0/8"):
        trace.append(("iptables", ["-A", "OUTPUT", "-d", network, "-j", "ACCEPT"]))
    for domain in domains:
        if domain == failing_at:
            return trace
        for ip in ips(domain):
            trace.append(("iptables", ["-A", "OUTPUT", "-d", ip, "-j", "ACCEPT"]))
    trace.append(("iptables", ["-A", "OUTPUT", "-j", "REJECT"]))
    return trace


def default_domains() -> list[str]:
    script = FIREWALL.read_text()
    array = re.search(r"(?ms)^ALLOWED_DOMAINS=\((.*?)^\)", script)
    assert array is not None
    entries = "\n".join(line.split("#", 1)[0] for line in array.group(1).splitlines())
    return re.findall(r"[\"']([^\"']+)[\"']", entries)


def test_default_mode_call_sequence_is_unchanged(tmp_path: Path) -> None:
    domains = default_domains()
    code, calls, stderr = default_mode_trace(tmp_path, FIREWALL)

    assert code == 0, stderr
    getent_calls = dict(calls)["getent"]
    iptables_calls = [call for call in calls if call[0] == "iptables"]
    assert iptables_calls == expected_default_trace(domains)
    assert getent_calls == [f"ahostsv4 {domain}" for domain in domains]
    assert "Firewall initialized" in stderr
    assert "djinn-egress" not in stderr


def test_default_mode_still_aborts_when_a_lookup_fails(tmp_path: Path) -> None:
    domains = default_domains()
    victim = domains[3]
    code, calls, stderr = default_mode_trace(tmp_path, FIREWALL, failing=(victim,))

    assert code != 0
    assert [call for call in calls if call[0] == "iptables"] == expected_default_trace(
        domains, failing_at=victim
    )
    assert "Firewall initialized" not in stderr
