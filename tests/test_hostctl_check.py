"""`djinn hostctl check`: preflight, isolated OpenSSH profile, private files, outcomes."""

from __future__ import annotations

import base64
import json
import os
import pwd
import re
import signal
import stat
import struct
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.config.ssh import HostctlConfig
from djinn_in_a_box.core import hostctl, ssh_delivery

REAL_COMMAND = hostctl.command


def _key(fill: bytes) -> str:
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + fill * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


KEY_A = _key(b"a")
KEY_B = _key(b"b")
HELPER_ID = "ab" * 32
GEN = "0123456789abcdef" * 2
STATUS_ARGS = ("exec", HELPER_ID, hostctl.SUPERVISOR, "status", GEN)
OPEN_REPLY = json.dumps({"generation": GEN, "admission": True, "paused": False, "closed": False})
NO_TERMINAL = "hostctl check requires a terminal on stdin and stderr"
NOT_OPEN = "hostctl window is not open or did not answer; run djinn hostctl status"
INCOMPLETE = "hostctl check state is incomplete; run djinn doctor"
TRUST_MISMATCH = "cached peer trust does not match the helper generation"
OPENING_MISMATCH = "opening configuration does not match the helper generation"

HOSTS = {
    "host-a": {"address": "host-a.example.ts.net", "user": "alice"},
    "host-b": {"address": "host-b.example.ts.net", "user": "bob"},
}


def peer(alias: str, *, ip: str, keys: tuple[str, ...] = (KEY_A,)) -> dict:
    address = HOSTS[alias]["address"]
    return {
        "address": address,
        "ip": ip,
        "ips": [ip],
        "host_key_alias": address,
        "keys": [*keys],
    }


def default_peers() -> dict:
    return {
        "host-a": peer("host-a", ip="100.64.0.1"),
        "host-b": peer("host-b", ip="100.64.0.2", keys=(KEY_B,)),
    }


def normalized(text: str) -> str:
    return " ".join(text.split())


class World:
    """Fake host: helper inspection, supervisor status and the ssh launcher."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.helper: dict | None = {
            "Id": HELPER_ID,
            "State": {"Running": True},
            "Config": {"Labels": {hostctl.GENERATION_LABEL: GEN}},
        }
        self.live: str | Exception = OPEN_REPLY
        self.terminal = True
        self.commands: list[tuple[str, ...]] = []
        self.runs: list[SimpleNamespace] = []
        self.codes: list[int | BaseException] = [0, 0, 0]
        self.hook = None
        monkeypatch.setattr(hostctl, "inspect_helper", lambda: self.helper)
        monkeypatch.setattr(hostctl, "command", self._command)
        monkeypatch.setattr(hostctl, "_stdio_is_terminal", lambda: self.terminal)
        monkeypatch.setattr(
            hostctl.shutil, "which", lambda name: "/opt/fake/ssh" if name == "ssh" else None
        )
        monkeypatch.setattr(subprocess, "run", self._run)
        for name in ("snapshot", "reconcile", "node_status", "journal", "open_window"):
            monkeypatch.setattr(hostctl, name, self._forbidden(name))
        self.root = hostctl.state_root()

    @staticmethod
    def _forbidden(name):
        def fail(*args, **kwargs):
            pytest.fail(f"check must not call hostctl.{name}")

        return fail

    def _command(self, *args, **kwargs):
        self.commands.append(args)
        if args != STATUS_ARGS:
            pytest.fail(f"unplanned control command {args}")
        if isinstance(self.live, Exception):
            raise self.live
        return self.live

    def _run(self, argv, **kwargs):
        config = Path(argv[2])
        record = SimpleNamespace(
            argv=list(argv),
            kwargs=kwargs,
            directory=config.parent,
            dir_mode=stat.S_IMODE(config.parent.stat().st_mode),
            names=sorted(path.name for path in config.parent.iterdir()),
            modes={
                path.name: stat.S_IMODE(path.lstat().st_mode) for path in config.parent.iterdir()
            },
            config=config.read_text(),
            known=(config.parent / "known_hosts").read_text(),
            lock_free=None,
        )
        self.runs.append(record)
        if self.hook:
            self.hook(record)
        code = self.codes[len(self.runs) - 1]
        if isinstance(code, BaseException):
            raise code
        return subprocess.CompletedProcess(argv, code)

    def state(
        self,
        *,
        hosts=HOSTS,
        peers=None,
        trust_gen=GEN,
        opening_gen=GEN,
        trust=True,
        opening=True,
    ) -> None:
        peers = default_peers() if peers is None else peers
        if trust:
            (self.root / "trust.json").write_text(
                json.dumps({"generation": trust_gen, "peers": peers, "routes": {}})
            )
        if opening:
            (self.root / "opening.json").write_text(
                json.dumps(
                    {
                        "generation": opening_gen,
                        "config": {"hosts": hosts},
                        "allow_unsealed": False,
                        "dev_id": None,
                        "agent": None,
                    }
                )
            )

    def check(self, *args: str):
        return CliRunner().invoke(app, ["hostctl", "check", *args])

    def leftovers(self) -> list[str]:
        return sorted(path.name for path in self.root.iterdir() if path.name.startswith("check-"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    value = World(tmp_path, monkeypatch)
    value.state()
    return value


def test_stdio_is_terminal_needs_stdin_and_stderr(monkeypatch):
    # Mutation P1: drop the stdin or stderr clause, or require stdout instead.
    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    def probe(stdin, stderr, stdout):
        monkeypatch.setattr(sys, "stdin", Stream(stdin))
        monkeypatch.setattr(sys, "stderr", Stream(stderr))
        monkeypatch.setattr(sys, "stdout", Stream(stdout))
        return hostctl._stdio_is_terminal()

    assert probe(True, True, True) is True
    assert probe(True, True, False) is True  # stdout may be redirected
    assert probe(False, True, True) is False
    assert probe(True, False, True) is False
    assert probe(False, False, False) is False


def test_check_requires_terminal(world):
    # Mutation P1d: move the guard after the helper inspection.
    world.terminal = False
    world.monkeypatch.setattr(
        hostctl, "inspect_helper", lambda: pytest.fail("guard must precede state reads")
    )
    result = world.check()
    assert result.exit_code == 1
    assert NO_TERMINAL in normalized(result.output)
    assert world.runs == [] and world.commands == [] and world.leftovers() == []


def test_check_requires_host_ssh(world):
    world.monkeypatch.setattr(hostctl.shutil, "which", lambda name: None)
    result = world.check()
    assert result.exit_code == 1
    assert "ssh client not found on host PATH" in normalized(result.output)
    assert world.runs == [] and world.commands == []


@pytest.mark.parametrize("case", ["absent", "stopped", "unadmitted", "paused", "closed", "expired"])
def test_check_window_refusals(world, case):
    # Mutation P4: drop the running check, admission is True, paused is False, or map the
    # status exec failure to incomplete state.
    if case == "absent":
        world.helper = None
    elif case == "stopped":
        world.helper["State"]["Running"] = False
    elif case == "unadmitted":
        world.live = json.dumps({"generation": GEN, "admission": False, "paused": False})
    elif case == "paused":
        world.live = json.dumps({"generation": GEN, "admission": True, "paused": True})
    else:
        # The supervisor refuses closed and expired windows; command() sanitizes the exec.
        world.live = hostctl.HostctlError("Docker exec failed (exit 1)")
    result = world.check()
    assert result.exit_code == 1
    assert NOT_OPEN in normalized(result.output)
    assert world.runs == [] and world.leftovers() == []


@pytest.mark.parametrize("case", ["trust-absent", "trust-old", "opening-absent", "opening-old"])
def test_check_rejects_stale_generation(world, case):
    # Mutation P11: accept absent/other trust; P11b: drop the opening comparison.
    old = "f" * 32
    if case == "trust-absent":
        (world.root / "trust.json").unlink()
        expected = TRUST_MISMATCH
    elif case == "trust-old":
        world.state(trust_gen=old)
        expected = TRUST_MISMATCH
    elif case == "opening-absent":
        (world.root / "opening.json").unlink()
        expected = OPENING_MISMATCH
    else:
        world.state(opening_gen=old)
        expected = OPENING_MISMATCH
    result = world.check()
    assert result.exit_code == 1
    assert expected in normalized(result.output)
    assert world.runs == [] and world.leftovers() == []


def corrupt(world, name, mutate):
    path = world.root / name
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value))


INCOMPLETE_CASES = {
    "live-not-json": lambda w: setattr(w, "live", "not json"),
    "live-not-object": lambda w: setattr(w, "live", "[]"),
    "live-no-admission": lambda w: setattr(w, "live", json.dumps({"generation": GEN})),
    "live-admission-type": lambda w: setattr(
        w, "live", json.dumps({"generation": GEN, "admission": "yes", "paused": False})
    ),
    "live-no-paused": lambda w: setattr(
        w, "live", json.dumps({"generation": GEN, "admission": True})
    ),
    "live-other-generation": lambda w: setattr(
        w, "live", json.dumps({"generation": "f" * 32, "admission": True, "paused": False})
    ),
    "helper-id-shape": lambda w: w.helper.update(Id="helper-a"),
    "helper-id-uppercase": lambda w: w.helper.update(Id="AB" * 32),
    "generation-shape": lambda w: w.helper["Config"]["Labels"].update(
        {hostctl.GENERATION_LABEL: "generation-a"}
    ),
    "opening-config-malformed": lambda w: corrupt(
        w, "opening.json", lambda v: v.update(config={"hosts": 5})
    ),
    "opening-no-hosts": lambda w: corrupt(w, "opening.json", lambda v: v.update(config={})),
    "opening-not-object": lambda w: (w.root / "opening.json").write_text("[]"),
    "opening-not-json": lambda w: (w.root / "opening.json").write_text("{"),
    "trust-not-json": lambda w: (w.root / "trust.json").write_text("{"),
    "peer-missing": lambda w: corrupt(w, "trust.json", lambda v: v["peers"].pop("host-b")),
    "peer-key-invalid": lambda w: corrupt(
        w, "trust.json", lambda v: v["peers"]["host-b"].update(keys=["ssh-ed25519 AAAA"])
    ),
    "peer-key-type": lambda w: corrupt(
        w, "trust.json", lambda v: v["peers"]["host-b"].update(keys=[5])
    ),
    "peer-no-keys": lambda w: corrupt(
        w, "trust.json", lambda v: v["peers"]["host-b"].update(keys=[])
    ),
    "peer-ip-invalid": lambda w: corrupt(
        w, "trust.json", lambda v: v["peers"]["host-b"].update(ip="not-an-ip")
    ),
    "peer-ip-not-tailnet": lambda w: corrupt(
        w, "trust.json", lambda v: v["peers"]["host-b"].update(ip="10.0.0.9")
    ),
    "peer-ip-scoped": lambda w: corrupt(
        w, "trust.json", lambda v: v["peers"]["host-b"].update(ip="fd7a:115c:a1e0::1%a b")
    ),
    "peer-address-mismatch": lambda w: corrupt(
        w, "trust.json", lambda v: v["peers"]["host-b"].update(address="other.example.ts.net")
    ),
    "peer-alias-unsafe": lambda w: corrupt(
        w, "trust.json", lambda v: v["peers"]["host-b"].update(host_key_alias="a b")
    ),
}


@pytest.mark.parametrize("case", sorted(INCOMPLETE_CASES))
def test_check_rejects_incomplete_state(world, case):
    # Mutation P17: validate lazily per alias (a later bad peer must stop every alias);
    # P40: drop the address/key shape checks; P45: drop the helper ID/generation shape check.
    INCOMPLETE_CASES[case](world)
    rendered = []
    real = ssh_delivery.host_check_files
    world.monkeypatch.setattr(
        ssh_delivery, "host_check_files", lambda *a, **k: rendered.append(1) or real(*a, **k)
    )
    result = world.check()
    assert result.exit_code == 1
    assert INCOMPLETE in normalized(result.output)
    assert world.runs == [] and rendered == [] and world.leftovers() == []
    if case.startswith(("helper-id", "generation")):
        assert world.commands == []  # nothing is executed for a malformed ID or generation


@pytest.mark.parametrize(
    "alias", ["nope", "100.64.0.1", "alice@host-a", "-oProxyCommand=bad", "[/oops]", "Host-A"]
)
def test_check_unknown_alias(world, alias):
    # Mutation P13: treat HOST as hostname/IP, allow user@alias, read HOST as markup.
    result = world.check("--", alias)
    assert result.exit_code == 1
    assert f"unknown hostctl alias: {alias}" in normalized(result.output)
    assert world.runs == [] and world.leftovers() == []


def test_check_option_syntax_error_starts_nothing(world):
    result = world.check("--bogus")
    assert result.exit_code == 2 and world.runs == []


def test_check_uses_frozen_declaration_order_and_user(world, monkeypatch):
    # Mutation P15: take user/order from current config or sort the aliases.
    reversed_hosts = {"host-b": HOSTS["host-b"], "host-a": HOSTS["host-a"]}
    dup = {**reversed_hosts, "host-c": {"address": "host-a.example.ts.net", "user": "carol"}}
    peers = {**default_peers(), "host-c": peer("host-a", ip="100.64.0.1")}
    peers["host-c"]["address"] = "host-a.example.ts.net"
    world.state(hosts=dup, peers=peers)
    monkeypatch.setattr(
        "djinn_in_a_box.config.loader.load_config",
        lambda *a, **k: pytest.fail("check must not read today's configuration"),
    )
    result = world.check()
    assert result.exit_code == 0, result.output
    assert [run.argv[-2] for run in world.runs] == ["host-b", "host-a", "host-c"]
    config = world.runs[0].config
    assert re.search(r"Host host-b\n.*\n.*\n    User bob\n", config)
    assert "    User alice\n" in config and "    User carol\n" in config
    assert config.index("Host host-b") < config.index("Host host-a") < config.index("Host host-c")
    # The explicit alias runs exactly once and only its block is rendered.
    world.runs.clear()
    assert world.check("host-a").exit_code == 0
    assert [run.argv[-2] for run in world.runs] == ["host-a"]
    assert "host-b" not in world.runs[0].config


def test_check_live_status_uses_exact_helper_and_generation(world):
    # Mutation P19: HELPER_NAME instead of the ID, no generation operand, literal `docker`.
    assert world.check().exit_code == 0
    assert world.commands == [STATUS_ARGS]
    proxy = re.search(r"ProxyCommand (.*)\n", world.runs[0].config).group(1)
    assert proxy.split() == [
        hostctl.DOCKER_EXECUTABLE,
        "exec",
        "-i",
        HELPER_ID,
        hostctl.SUPERVISOR,
        "connect",
        GEN,
        '"%h"',
    ]
    assert hostctl.HELPER_NAME not in proxy.replace("hostctl-supervisor", "")
    assert os.path.isabs(hostctl.DOCKER_EXECUTABLE)


@pytest.mark.parametrize("outcome", ["success", "failure", "renderer", "launch", "interrupt"])
def test_check_private_modes_and_cleanup(world, outcome):
    # Mutation P21: drop the explicit 0700; P22: 0644 / no O_EXCL; P24: cleanup outside
    # finally, skipped on KeyboardInterrupt.
    before = {name: (world.root / name).read_bytes() for name in ("trust.json", "opening.json")}
    if outcome == "failure":
        world.codes = [255, 0, 0]
    elif outcome == "launch":
        world.codes = [OSError("boom")]
    elif outcome == "interrupt":
        world.codes = [KeyboardInterrupt()]
    elif outcome == "renderer":

        def explode(*args, **kwargs):
            raise ssh_delivery.GitSSHError("renderer failed")

        world.monkeypatch.setattr(ssh_delivery, "host_check_files", explode)
    old = os.umask(0)
    try:
        result = world.check()
    finally:
        os.umask(old)
    expected = {"success": 0, "failure": 1, "renderer": 1, "launch": 1, "interrupt": 130}
    assert result.exit_code == expected[outcome], result.output
    if outcome != "renderer":
        run = world.runs[0]
        assert run.directory.parent == world.root
        assert run.dir_mode == 0o700
        assert run.names == ["config", "known_hosts"]
        assert run.modes == {"config": 0o600, "known_hosts": 0o600}
    assert world.leftovers() == []
    assert {name: (world.root / name).read_bytes() for name in before} == before
    assert not (world.root / "journal.jsonl").exists()


def test_private_files_are_exclusive_and_do_not_follow_links(tmp_path):
    # Mutation P22b: drop O_EXCL. (O_NOFOLLOW is not needed: O_CREAT|O_EXCL refuses a planted
    # link, live or dangling, so a separate NOFOLLOW mutant is equivalent.)
    target = tmp_path / "victim"
    target.write_text("keep")
    link = tmp_path / "config"
    link.symlink_to(target)
    with pytest.raises(OSError):
        hostctl._write_private_file(link, "overwritten")
    assert target.read_text() == "keep"
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "never-created")
    with pytest.raises(FileExistsError):
        hostctl._write_private_file(dangling, "x")
    assert not (tmp_path / "never-created").exists()
    reused = tmp_path / "known_hosts"
    reused.write_text("public")
    reused.chmod(0o644)
    with pytest.raises(FileExistsError):
        hostctl._write_private_file(reused, "x")
    fresh = tmp_path / "fresh"
    old = os.umask(0)
    try:
        hostctl._write_private_file(fresh, "x")
    finally:
        os.umask(old)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600


def test_private_file_is_never_wider_than_0600_even_before_fchmod(tmp_path, monkeypatch):
    # Mutation P22a: the creation mode itself must be 0600, not only the later fchmod.
    seen = []
    real = os.fchmod

    def spy(fd, mode):
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        real(fd, mode)

    monkeypatch.setattr(hostctl.os, "fchmod", spy)
    old = os.umask(0)
    try:
        hostctl._write_private_file(tmp_path / "early", "x")
    finally:
        os.umask(old)
    assert seen == [0o600]


def test_check_ssh_inherits_terminal(world):
    # Mutation P27 (-n, -o BatchMode, -o ConnectTimeout, -T, order), P28 (capture, stdin,
    # timeout), P38 (-F).
    assert world.check().exit_code == 0
    for run in world.runs:
        config = Path(run.argv[2])
        assert run.argv == ["/opt/fake/ssh", "-F", str(config), "-T", "-n", run.argv[-2], "true"]
        assert config.name == "config" and "-o" not in run.argv
        assert set(run.kwargs) <= {"check"} and run.kwargs.get("check") is False
        assert "ConnectTimeout" not in run.config
        assert "timeout" not in run.kwargs and "capture_output" not in run.kwargs


@pytest.mark.parametrize(
    "codes,exit_code,lines",
    [
        (
            [0, 0],
            0,
            [
                "Checking host-a...",
                "host-a: approved and reachable",
                "Checking host-b...",
                "host-b: approved and reachable",
            ],
        ),
        (
            [0, 255],
            1,
            [
                "Checking host-a...",
                "host-a: approved and reachable",
                "Checking host-b...",
                "host-b: check failed (ssh exit 255)",
            ],
        ),
        (
            [1, 0],
            1,
            [
                "Checking host-a...",
                "host-a: check failed (ssh exit 1)",
                "Checking host-b...",
                "host-b: approved and reachable",
            ],
        ),
    ],
)
def test_check_results_and_exit_codes(world, codes, exit_code, lines):
    world.codes = codes
    result = world.check()
    assert result.exit_code == exit_code
    assert [line.strip() for line in result.output.splitlines() if line.strip()] == lines


def test_check_ctrl_c(world):
    # Mutation P31: map to 1 / continue; omit the interrupted line; skip signal conversion.
    world.codes = [KeyboardInterrupt(), 0]
    result = world.check()
    assert result.exit_code == 130
    assert [line for line in result.output.splitlines() if line.strip()] == [
        "Checking host-a...",
        "host-a: check interrupted",
    ]
    assert len(world.runs) == 1 and world.leftovers() == []


@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP"])
def test_check_converts_termination_signals(world, name):
    number = getattr(signal, name)
    sentinel = lambda signum, frame: None  # noqa: E731
    previous = signal.signal(number, sentinel)
    try:
        world.hook = lambda record: signal.raise_signal(number)
        reports: list[str] = []
        with pytest.raises(KeyboardInterrupt):
            hostctl.check_hosts(None, reports.append)
        assert signal.getsignal(number) is sentinel  # restored afterwards
    finally:
        signal.signal(number, previous)
    assert reports == ["Checking host-a...", "host-a: check interrupted"]
    assert len(world.runs) == 1 and world.leftovers() == []


def test_check_reports_cleanup_failure(world):
    # A removal that fails for real (read-only directory) is reported, not swallowed.
    def lock(record):
        record.directory.chmod(0o500)

    world.hook = lock
    result = world.check("host-a")
    leftover = world.root / world.leftovers()[0]
    leftover.chmod(0o700)
    assert result.exit_code == 0
    assert f"Could not remove {leftover}" in normalized(result.output)
    assert "host-a: approved and reachable" in result.output


def test_check_retries_cleanup_after_interrupt(world):
    # Mutation P24c: no retry when the interrupt hits the cleanup.
    real = hostctl.shutil.rmtree
    attempts = []

    def flaky(path, **kwargs):
        attempts.append(path)
        if len(attempts) == 1:
            raise KeyboardInterrupt
        real(path, **kwargs)

    world.monkeypatch.setattr(hostctl.shutil, "rmtree", flaky)
    result = world.check()
    assert result.exit_code == 130
    assert len(attempts) == 2 and world.leftovers() == []


def test_check_no_snapshot_journal_or_long_lock(world):
    # Mutation P26: snapshot()/journal in check_hosts; P26b: hold control_guard over ssh.
    outcome = {}

    def other_thread(record):
        def take():
            try:
                with hostctl.control_guard():
                    outcome["acquired"] = True
            except hostctl.HostctlError as exc:
                outcome["error"] = str(exc)

        thread = threading.Thread(target=take)
        thread.start()
        thread.join(20)

    world.hook = other_thread
    assert world.check().exit_code == 0
    assert outcome == {"acquired": True}


def test_check_docker_failures_are_sanitized(world):
    # The captured exec fails with a URL in stderr; only the fixed diagnostic is shown.
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="see https://example.invalid/login/abc"
        )

    world.monkeypatch.setattr(hostctl, "command", REAL_COMMAND)
    world.monkeypatch.setattr(subprocess, "run", fake_run)
    result = world.check()
    assert result.exit_code == 1
    assert NOT_OPEN in normalized(result.output)
    assert "example.invalid" not in result.output
    assert len(seen) == 1 and seen[0][1:3] == ["exec", HELPER_ID]
    assert not (world.root / "journal.jsonl").exists()


def test_check_failure_message_hides_markup_in_host(world):
    result = world.check("--", "[bold]x[/bold]")
    assert result.exit_code == 1
    assert "unknown hostctl alias: [bold]x[/bold]" in normalized(result.output)


# ---- renderer: the real OpenSSH parser -------------------------------------------------


def render(tmp_path, *, aliases=("host-a",), docker="/usr/bin/docker", directory=None):
    directory = directory or tmp_path / "priv"
    directory.mkdir(exist_ok=True)
    peers = default_peers()
    config = HostctlConfig(hosts={alias: HOSTS[alias] for alias in aliases})
    files = ssh_delivery.host_check_files(
        config,
        {"peers": {alias: peers[alias] for alias in aliases}},
        docker=docker,
        helper_id=HELPER_ID,
        supervisor=hostctl.SUPERVISOR,
        generation=GEN,
        directory=directory,
    )
    for name, text in files.items():
        (directory / name).write_text(text)
    return files, directory


def ssh_g(config, alias, *extra):
    output = subprocess.run(
        ["ssh", "-G", "-F", str(config), *extra, alias],
        capture_output=True,
        text=True,
        check=True,
    )
    values: dict[str, list[str]] = {}
    for line in output.stdout.splitlines():
        key, _, value = line.partition(" ")
        values.setdefault(key, []).append(value)
    return values


EXPECTED_OPTIONS = {
    "hostname": ["100.64.0.1"],
    "hostkeyalias": ["host-a.example.ts.net"],
    "user": ["alice"],
    "identitiesonly": ["yes"],
    "identityagent": ["none"],
    "pubkeyauthentication": ["false", "no"],
    "passwordauthentication": ["no", "false"],
    "forwardagent": ["no", "false"],
    "forwardx11": ["no", "false"],
    "forwardx11trusted": ["no", "false"],
    "clearallforwardings": ["yes", "true"],
    "permitlocalcommand": ["no", "false"],
    "controlmaster": ["false", "no"],
    "controlpersist": ["no", "false"],
    "stricthostkeychecking": ["true", "yes"],
    "updatehostkeys": ["false", "no"],
    "globalknownhostsfile": ["/dev/null"],
    "loglevel": ["info"],
}


@pytest.mark.parametrize("option", sorted(EXPECTED_OPTIONS))
def test_check_ssh_g_options(tmp_path, option):
    # Mutation P34: delete or flip one option line; P36: known_hosts lines; P41: proxy.
    files, directory = render(tmp_path)
    values = ssh_g(directory / "config", "host-a")
    assert len(values[option]) == 1, values[option]
    assert values[option][0].lower() in EXPECTED_OPTIONS[option], (option, values[option])
    assert "IdentityFile" not in files["config"]
    assert values["userknownhostsfile"] == [str(directory / "known_hosts")]
    assert "~/.ssh/known_hosts" not in files["config"]
    proxy = re.search(r"    ProxyCommand (.*)\n", files["config"]).group(1)
    assert proxy == (f'/usr/bin/docker exec -i {HELPER_ID} {hostctl.SUPERVISOR} connect {GEN} "%h"')
    normalized_proxy = values["proxycommand"][0].replace('"', "")
    assert normalized_proxy.endswith(f"connect {GEN} %h")


def test_check_ssh_g_no_agent_or_password_fallbacks(tmp_path):
    # Mutation P34b: reuse the container profile line `IdentityFile none`.
    files, _ = render(tmp_path)
    assert "IdentityFile none" not in files["config"]
    assert "KbdInteractiveAuthentication" not in files["config"]


def test_check_ssh_isolation_sources(tmp_path):
    # Mutation P38: drop -F from the argv; P38b: render an `Include ~/.ssh/config` line.
    if not Path("/etc/ssh/ssh_config").exists():
        pytest.skip("no /etc/ssh/ssh_config on this host (a skip is not proof)")
    _, directory = render(tmp_path)
    reading = re.compile(r"Reading configuration data (.*)")

    def sources(*argv):
        result = subprocess.run(
            ["ssh", "-vvv", "-G", *argv], capture_output=True, text=True, check=True
        )
        return reading.findall(result.stderr), result.stderr

    plain, _ = sources("host-a")
    assert "/etc/ssh/ssh_config" in plain
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if (home / ".ssh/config").exists():
        assert str(home / ".ssh/config") in plain
    isolated, stderr = sources("-F", str(directory / "config"), "host-a")
    assert isolated == [str(directory / "config")]
    assert "Include" not in (directory / "config").read_text()
    # A hostile file is read only because it is named with -F.
    hostile = tmp_path / "hostile"
    hostile.write_text("Host host-a\n    User hostile-marker\n")
    assert ssh_g(hostile, "host-a")["user"] == ["hostile-marker"]


def test_check_known_hosts_frozen_only(tmp_path):
    # Mutation P36/P39: include non-selected or cached keys; drop deduplication.
    peers = default_peers()
    peers["host-b"]["keys"] = [KEY_A, KEY_A, KEY_B]
    peers["host-b"]["host_key_alias"] = "host-a.example.ts.net"
    config = HostctlConfig(hosts=HOSTS)
    files = ssh_delivery.host_check_files(
        config,
        {"peers": peers},
        docker="/usr/bin/docker",
        helper_id=HELPER_ID,
        supervisor=hostctl.SUPERVISOR,
        generation=GEN,
        directory=tmp_path,
    )
    lines = files["known_hosts"].splitlines()
    assert lines == [f"host-a.example.ts.net {KEY_A}", f"host-a.example.ts.net {KEY_B}"]
    only_a, _ = render(tmp_path, aliases=("host-a",), directory=tmp_path / "one")
    assert only_a["known_hosts"] == f"host-a.example.ts.net {KEY_A}\n"
    with pytest.raises(ssh_delivery.GitSSHError):
        ssh_delivery.host_check_files(
            HostctlConfig(hosts=HOSTS),
            {"peers": {"host-a": peers["host-a"]}},  # host-b has no frozen peer
            docker="/usr/bin/docker",
            helper_id=HELPER_ID,
            supervisor=hostctl.SUPERVISOR,
            generation=GEN,
            directory=tmp_path,
        )


def test_check_known_hosts_come_from_frozen_trust_in_run(world):
    assert world.check("host-b").exit_code == 0
    assert world.runs[0].known == f"host-b.example.ts.net {KEY_B}\n"


NASTY = "d o'c\"k$(touch MARK)${HOME}`id`%h%%x"


def test_check_proxy_and_path_quoting(tmp_path):
    # Mutation P41: skip %% escaping, single-quote %h, use raw HOST; P42: drop escaping.
    docker = tmp_path / NASTY
    docker.write_text('#!/bin/sh\nprintf "%s\\0" "$@" > "$RECORD"\n')
    docker.chmod(0o755)
    directory = tmp_path / "pri v'q\"b%x"
    files, directory = render(tmp_path, docker=str(docker), directory=directory)
    values = ssh_g(directory / "config", "host-a")
    assert values["userknownhostsfile"] == [str(directory / "known_hosts")]
    assert len(values["proxycommand"]) == 1
    proxy = re.search(r"    ProxyCommand (.*)\n", files["config"]).group(1)
    # Expand tokens as ssh does: %% -> %, %h -> the validated address.
    command = re.sub(r"%(.)", lambda m: "%" if m.group(1) == "%" else "100.64.0.1", proxy)
    record = tmp_path / "record"
    subprocess.run(["sh", "-c", command], check=True, env={**os.environ, "RECORD": str(record)})
    argv = record.read_bytes().split(b"\0")[:-1]
    assert [a.decode() for a in argv] == [
        "exec", "-i", HELPER_ID, hostctl.SUPERVISOR, "connect", GEN, "100.64.0.1",
    ]  # fmt: skip
    assert not (tmp_path / "MARK").exists()
    assert proxy.endswith(' "%h"') and "%%" in proxy
    assert proxy.replace("%%", "").count("%") == 1  # `%h` is the only token ssh expands


@pytest.mark.parametrize("bad", ["a$b", "a\nb", "a\0b", "${HOME}"])
def test_check_private_path_rejections(bad):
    # Mutation P21b/P42: drop the `$`, newline and NUL rejection.
    with pytest.raises(ssh_delivery.GitSSHError):
        ssh_delivery.ssh_config_path("/tmp/" + bad)


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\0b"])
def test_check_proxy_operand_rejections(bad):
    # Mutation P42c: drop the newline/NUL rejection of the fixed ProxyCommand operands.
    with pytest.raises(ssh_delivery.GitSSHError):
        ssh_delivery.host_proxy_command(bad, HELPER_ID, hostctl.SUPERVISOR, GEN)
    with pytest.raises(ssh_delivery.GitSSHError):
        ssh_delivery.host_proxy_command("/usr/bin/docker", HELPER_ID, hostctl.SUPERVISOR, bad)


def test_check_path_with_dollar_is_refused_without_leftovers(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s$tate"))
    value = World(tmp_path, monkeypatch)
    value.state()
    result = value.check()
    assert result.exit_code == 1
    assert "unsupported character" in normalized(result.output)
    assert value.runs == [] and value.leftovers() == []


# ---- container renderer is unchanged --------------------------------------------------

CONTAINER_BLOCK = """Host host-a
    HostName {hostname}
    HostKeyAlias host-a.example.ts.net
    User alice
    ProxyCommand /usr/local/bin/djinn-hostctl-connect %h %p
    StrictHostKeyChecking yes
    UpdateHostKeys no
    UserKnownHostsFile /home/dev/.ssh/tailnet_known_hosts
    GlobalKnownHostsFile /dev/null
    IdentityAgent none
    IdentityFile none
    PubkeyAuthentication no
    ForwardAgent no
    ForwardX11 no
    ForwardX11Trusted no
    ClearAllForwardings yes
    PermitLocalCommand no
    ControlMaster no
    ControlPersist no
"""


def test_container_tailnet_rendering_unchanged():
    # Mutation P43: change one byte of the default profile (order, IdentityFile none, a path).
    config = HostctlConfig(hosts={"host-a": HOSTS["host-a"]})
    trust = {"peers": {"host-a": default_peers()["host-a"]}}
    ready = ssh_delivery.tailnet_files(config, trust)
    assert ready == {
        "tailnet_config": CONTAINER_BLOCK.format(hostname="100.64.0.1"),
        "tailnet_known_hosts": f"host-a.example.ts.net {KEY_A}\n",
    }
    closed = ssh_delivery.tailnet_files(config, None)
    assert closed == {
        "tailnet_config": CONTAINER_BLOCK.format(hostname="host-a.example.ts.net"),
        "tailnet_known_hosts": "",
    }
    both = ssh_delivery.tailnet_files(HostctlConfig(hosts=HOSTS), trust)
    assert both["tailnet_config"].count("\nHost host-b\n") == 1
    assert "\n\nHost host-b\n" in both["tailnet_config"]
    assert "PasswordAuthentication" not in both["tailnet_config"]
    assert (
        "LogLevel" not in both["tailnet_config"] and "IdentitiesOnly" not in both["tailnet_config"]
    )


# ---- on hint ----------------------------------------------------------------------------


def test_on_adds_check_hint_without_waiting(world, monkeypatch):
    from djinn_in_a_box.commands import hostctl as cli

    calls = []
    monkeypatch.setattr(hostctl, "open_window", lambda *a, **k: calls.append(k) or GEN)
    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace())
    result = CliRunner().invoke(app, ["hostctl", "on", "--allow-unsealed"])
    assert result.exit_code == 0, result.output
    lines = normalized(result.output)
    hint = (
        "Once status reports open, djinn hostctl check shows the SSH approval link "
        "in this terminal."
    )
    assert lines.count(hint) == 1
    first = "Window opening. Login time counts; use djinn hostctl status for login/readiness."
    second = (
        "Relay waits for enrollment, peer trust, sealing, direct-route probe and journal readiness."
    )
    override = (
        "Sealing override recorded; dev host authority makes the window an operating aid only."
    )
    assert lines.index(first) < lines.index(second) < lines.index(hint) < lines.index(override)
    assert len(calls) == 1 and world.runs == [] and world.commands == []
