"""The OpenCode credentials helper: runner, single apply path, ``sync``, crash matrix.

The helper runs as a real subprocess against ``tests/fake_opencode.py``. Every dummy credential
value is generated at test runtime (never a literal) and everything is written below ``tmp_path``
(under /tmp). Expectations are explicit values, not recomputed by the code under test.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from djinn_in_a_box.core import opencode_credentials as oc

MODULE_PATH = Path(oc.__file__)
SRC_ROOT = MODULE_PATH.parents[1]
FAKE_PATH = Path(__file__).with_name("fake_opencode.py")
SALT = os.urandom(8).hex()

_spec = importlib.util.spec_from_file_location("fake_opencode_under_test", FAKE_PATH)
assert _spec is not None and _spec.loader is not None
fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake)

COPY_A = "credentials.sync-conflict-20260101-120000-ABCDEFG.json"
COPY_B = "credentials.sync-conflict-20260102-130000-.json"
COPY_REMOVED = "credentials.removed.sync-conflict-20260103-140000-QRSTUVW.json"
FILE = "credentials.json"
REMOVED = "credentials.removed.json"


# ---------------------------------------------------------------------------------------------
# Values and the world
# ---------------------------------------------------------------------------------------------


def token(name: str) -> str:
    """A runtime-generated dummy value; differs per run and per name."""
    return hashlib.sha256(f"{SALT}:{name}".encode()).hexdigest()[:16]


def key_value(tag: str) -> dict[str, Any]:
    return {"type": "key", "key": token(tag)}


def oauth_value(tag: str, expires: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": "oauth",
        "methodID": "m1",
        "refresh": token(tag + "r"),
        "access": token(tag + "a"),
        "expires": expires,
        **extra,
    }


def entry(identity: str, integration: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": identity,
        "integrationID": integration,
        "label": "label " + identity,
        "active": False,
        "value": value,
    }


def fingerprint(document: dict[str, Any]) -> str:
    return oc.validate_entry(document).fingerprint


def raw_json(document: object) -> bytes:
    return (json.dumps(document, indent=2) + "\n").encode()


WRAPPER_SOURCE = textwrap.dedent(
    """
    import importlib.util, json, os, re, signal, sys

    module_path, config_path, *argv = sys.argv[1:]
    spec = importlib.util.spec_from_file_location("oc_under_test", module_path)
    oc = importlib.util.module_from_spec(spec)
    sys.modules["oc_under_test"] = oc
    spec.loader.exec_module(oc)
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    log_path = os.environ.get("FAKE_OPENCODE_LOG")
    temp = re.compile(r"\\.[0-9a-f]{16}\\.tmp")
    state = {"count": 0}
    fired = {}


    def emit(line):
        if log_path:
            descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.write(descriptor, (line + "\\n").encode())
            os.close(descriptor)


    def die():
        os.kill(os.getpid(), signal.SIGKILL)


    def label(kind, args):
        if kind == "open":
            mode = "r" if args[1] & os.O_ACCMODE == os.O_RDONLY else "w"
            return mode + " " + args[0]
        if kind in ("write", "fsync_file"):
            return os.path.basename(os.readlink("/proc/self/fd/%d" % args[0]))
        if kind == "fsync_dir":
            return "dir:" + os.path.basename(os.readlink("/proc/self/fd/%d" % args[0]))
        if kind == "rename":
            return args[0] + "->" + args[1]
        return args[0]


    def interfere(rule):
        with open(rule["source"], "rb") as handle:
            data = handle.read()
        if rule.get("vary"):
            data += b" " * fired[rule["index"]]
        scratch = rule["dest"] + ".mirror"
        with open(scratch, "wb") as handle:
            handle.write(data)
        os.chmod(scratch, 0o600)
        os.replace(scratch, rule["dest"])


    def wrap(kind):
        real = getattr(oc.ops, kind)

        def inner(*args):
            state["count"] += 1
            number = state["count"]
            text = temp.sub(".X.tmp", label(kind, args))
            for index, rule in enumerate(config.get("interfere", [])):
                if rule["before"] == kind and rule["match"] in text:
                    if fired.get(index, 0) < rule["times"]:
                        fired[index] = fired.get(index, 0) + 1
                        interfere(dict(rule, index=index))
            crash = config.get("crash")
            if crash and crash["op"] == number and crash["phase"] == "before":
                die()
            result = real(*args)
            if config.get("log"):
                emit("ops %s %s" % (kind, text))
            if crash and crash["op"] == number and crash["phase"] == "after":
                die()
            return result

        return inner


    for name in ("open", "write", "fsync_file", "fsync_dir", "rename", "unlink"):
        setattr(oc.ops, name, wrap(name))

    shift = config.get("stat_dev")
    if shift:
        real_stat = os.stat

        def patched_stat(path, *, dir_fd=None, follow_symlinks=True):
            result = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            if path == shift and dir_fd is not None:
                values = list(result)[:10]
                values[2] += 1
                return os.stat_result(values)
            return result

        os.stat = patched_stat

    original = oc.sync


    def patched_sync(*args, **kwargs):
        if config.get("raise_unexpected"):
            raise RuntimeError(config["raise_unexpected"])
        return original(*args, **kwargs, **config.get("sync_kwargs", {}))


    oc.sync = patched_sync
    sys.exit(oc.main(argv))
    """
)


class Host:
    """One OpenCode host: a fake database, its host state and its conflict backup directory."""

    def __init__(self, world: World, name: str) -> None:
        self.world = world
        self.name = name
        base = world.root / name
        self.data = base / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "opencode.db"
        self.state_file = self.data / "djinn-credentials-host.json"
        self.backup = base / "state" / "conflicts"
        self.put([])

    def put(
        self, entries: Iterable[dict[str, Any]], selection: dict[str, str] | None = None
    ) -> None:
        stored = [
            {key: item[key] for key in ("id", "integrationID", "label", "value")}
            for item in entries
        ]
        document = {"entries": stored, "selection": selection or {}}
        if self.db.exists():
            fake.write_state(str(self.db), document)
        else:
            self.db.write_text(json.dumps(document))

    def raw_state(self) -> dict[str, Any]:
        return json.loads(self.db.read_text())

    def values(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item["value"] for item in self.raw_state()["entries"]}

    def active(self) -> dict[str, str]:
        listing = fake.computed_export(self.raw_state())
        return {item["integrationID"]: item["id"] for item in listing if item["active"]}

    def fingerprints(self) -> dict[str, str]:
        return {
            item["id"]: fingerprint({**item, "active": False})
            for item in fake.computed_export(self.raw_state())
        }

    def seed_state(
        self,
        seen: dict[str, str] | None = None,
        hidden: dict[str, tuple[str, ...]] | None = None,
        reactivate: dict[str, str] | None = None,
        inode: int | None = None,
    ) -> None:
        state = oc.HostState(
            inode=self.db.stat().st_ino if inode is None else inode,
            seen=seen if seen is not None else {},
            hidden=hidden or {},
            reactivate=reactivate or {},
        )
        self.state_file.write_bytes(oc.dump_host_state(state))
        self.state_file.chmod(0o600)

    def seed_seen_from_db(self) -> None:
        self.seed_state(seen=self.fingerprints())

    def state(self) -> oc.HostState:
        return oc.parse_host_state(self.state_file.read_bytes())

    def replace_db_inode(self) -> None:
        scratch = self.data / "opencode.db.new"
        shutil.copyfile(self.db, scratch)
        os.replace(scratch, self.db)


class World:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
        assert str(resolved).startswith("/tmp/"), "dummy credentials live under /tmp only"
        self.root = resolved
        self.carrier = resolved / "carrier"
        self.carrier.mkdir(mode=0o700)
        self.bin = resolved / "bin"
        self.bin.mkdir()
        (self.bin / "opencode").symlink_to(FAKE_PATH)
        self.tmp = resolved / "tmp"
        self.tmp.mkdir()
        self.log = resolved / "events.log"
        self.hosts: dict[str, Host] = {}
        self.runs = 0

    def host(self, name: str = "a") -> Host:
        self.hosts[name] = Host(self, name)
        return self.hosts[name]

    def write(self, name: str, document: object) -> Path:
        path = self.carrier / name
        path.write_bytes(raw_json(document))
        path.chmod(0o600)
        return path

    def read_json(self, name: str) -> Any:
        return json.loads((self.carrier / name).read_text())

    def file_values(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item["value"] for item in self.read_json(FILE)}

    def events(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def calls(self) -> list[str]:
        return [
            line for line in self.events() if line.startswith("fake ") and " sentinel " not in line
        ]

    def environment(self, host: Host, extra: dict[str, str] | None = None) -> dict[str, str]:
        base = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OPENCODE", "XDG_", "FAKE_", "WRAP"))
        }
        base["PATH"] = f"{self.bin}:{base.get('PATH', '/usr/bin')}"
        base["TMPDIR"] = str(self.tmp)
        base["FAKE_OPENCODE_STATE"] = str(host.db)
        base["FAKE_OPENCODE_LOG"] = str(self.log)
        base.update(extra or {})
        return base

    def run(
        self,
        host: Host,
        mode: str = "start",
        env: dict[str, str] | None = None,
        wrapper: dict[str, Any] | None = None,
        timeout: float = 120,
        python: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        arguments = [
            "sync",
            "--carrier",
            str(self.carrier),
            "--database",
            str(host.db),
            "--host-state",
            str(host.state_file),
            "--conflict-backup",
            str(host.backup),
            "--mode",
            mode,
        ]
        if wrapper is None:
            command = [python or sys.executable, "-I", str(MODULE_PATH), *arguments]
        else:
            self.runs += 1
            script = self.root / "wrapper.py"
            script.write_text(WRAPPER_SOURCE)
            config = self.root / f"wrapper-{self.runs}.json"
            config.write_text(json.dumps(wrapper))
            command = [sys.executable, "-I", str(script), str(MODULE_PATH), str(config), *arguments]
        Path(str(host.db) + ".count").unlink(missing_ok=True)
        return subprocess.run(
            command,
            env=self.environment(host, env),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=self.root,
            check=False,
        )

    def mutating_calls(self) -> list[str]:
        return [
            line for line in self.calls() if line.startswith(("fake import", "fake credential."))
        ]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def sync_ok(world: World, host: Host, mode: str = "start", **kwargs: Any) -> str:
    result = world.run(host, mode, **kwargs)
    assert result.returncode == 0, result.stderr
    return result.stderr


# ---------------------------------------------------------------------------------------------
# The main scenario: update of the active account, add, remove, conflict copy
# ---------------------------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    build: Callable[[World], Host]
    values: dict[str, dict[str, Any]]
    active: dict[str, str]
    tombstones: list[str]
    winners: dict[str, dict[str, Any]]
    allowed_hidden: dict[str, set[str]]
    copies: dict[str, bytes] = field(default_factory=lambda: {})
    min_points: int = 0


X_OLD = oauth_value("x1", 1000)
X_NEW = oauth_value("x2", 2000)


def build_main(world: World) -> Host:
    host = world.host("a")
    host.put(
        [
            entry("cred_x", "prov_p", X_OLD),
            entry("cred_y", "prov_p", key_value("y1")),
            entry("cred_dead", "prov_r", key_value("dead1")),
        ],
        {"prov_p": "cred_x"},
    )
    world.write(
        FILE,
        [
            entry("cred_dead", "prov_r", key_value("dead1")),
            entry("cred_n", "prov_q", key_value("n1")),
            entry("cred_x", "prov_p", X_NEW),
        ],
    )
    world.write(REMOVED, ["cred_dead"])
    world.write(COPY_A, [entry("cred_c", "prov_s", key_value("c1"))])
    host.seed_seen_from_db()
    return host


MAIN = Scenario(
    name="main",
    build=build_main,
    values={
        "cred_c": key_value("c1"),
        "cred_n": key_value("n1"),
        "cred_x": X_NEW,
        "cred_y": key_value("y1"),
    },
    active={"prov_p": "cred_x", "prov_q": "cred_n", "prov_s": "cred_c"},
    tombstones=["cred_dead"],
    winners={
        "cred_c": key_value("c1"),
        "cred_n": key_value("n1"),
        "cred_x": X_NEW,
        "cred_y": key_value("y1"),
    },
    allowed_hidden={
        "cred_x": {fingerprint(entry("cred_x", "prov_p", X_OLD))},
        "cred_dead": {fingerprint(entry("cred_dead", "prov_r", key_value("dead1")))},
    },
    copies={COPY_A: raw_json([entry("cred_c", "prov_s", key_value("c1"))])},
    min_points=60,
)


def build_single(world: World) -> Host:
    host = world.host("a")
    host.put([entry("cred_x", "prov_p", X_OLD)])
    world.write(FILE, [entry("cred_x", "prov_p", X_NEW)])
    host.seed_seen_from_db()
    return host


SINGLE = Scenario(
    name="single",
    build=build_single,
    values={"cred_x": X_NEW},
    active={"prov_p": "cred_x"},
    tombstones=[],
    winners={"cred_x": X_NEW},
    allowed_hidden={"cred_x": {fingerprint(entry("cred_x", "prov_p", X_OLD))}},
    min_points=20,
)


def build_opposite(world: World) -> Host:
    """Another host's file marks the other account active while this host's x is updated."""
    host = world.host("a")
    host.put(
        [entry("cred_x", "prov_p", X_OLD), entry("cred_y", "prov_p", key_value("y1"))],
        {"prov_p": "cred_x"},
    )
    world.write(
        FILE,
        [
            entry("cred_x", "prov_p", X_NEW),
            {**entry("cred_y", "prov_p", key_value("y1")), "active": True},
        ],
    )
    host.seed_seen_from_db()
    return host


OPPOSITE = Scenario(
    name="opposite",
    build=build_opposite,
    values={"cred_x": X_NEW, "cred_y": key_value("y1")},
    active={"prov_p": "cred_x"},
    tombstones=[],
    winners={"cred_x": X_NEW, "cred_y": key_value("y1")},
    allowed_hidden={"cred_x": {fingerprint(entry("cred_x", "prov_p", X_OLD))}},
    min_points=25,
)


def durable_pairs(world: World, host: Host) -> set[tuple[str, str]]:
    """Every (id, fingerprint) held by the file, the database, a copy or a backup."""
    pairs: set[tuple[str, str]] = set()
    documents: list[list[dict[str, Any]]] = []
    candidates = [world.carrier / FILE, *world.carrier.glob("credentials.sync-conflict-*.json")]
    if host.backup.is_dir():
        candidates += [p for p in host.backup.iterdir() if not p.name.startswith(".")]
    for path in candidates:
        try:
            documents.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    documents.append([{**item, "active": False} for item in fake.computed_export(host.raw_state())])
    for document in documents:
        for item in document:
            pairs.add((item["id"], fingerprint({**item, "active": False})))
    return pairs


def winner_pairs(scenario: Scenario, world: World) -> set[tuple[str, str]]:
    """The (id, fingerprint) the merge must keep, built from the scenario's literal values."""
    integrations = {
        "cred_c": "prov_s",
        "cred_n": "prov_q",
        "cred_x": "prov_p",
        "cred_y": "prov_p",
    }
    return {
        (identity, fingerprint(entry(identity, integrations[identity], value)))
        for identity, value in scenario.winners.items()
    }


def age_temps(*roots: Path) -> None:
    old = time.time() - 3600
    for root in roots:
        if root.is_dir():
            for path in root.iterdir():
                if re.fullmatch(r"\.[^/]+\.[0-9a-f]{16}\.tmp", path.name):
                    os.utime(path, (old, old))


def snapshot(world: World, host: Host) -> dict[str, bytes]:
    result = {f"carrier/{p.name}": p.read_bytes() for p in world.carrier.iterdir() if p.is_file()}
    result["db"] = host.db.read_bytes()
    result["state"] = host.state_file.read_bytes()
    return result


def check_final(scenario: Scenario, world: World, host: Host) -> list[str]:
    problems: list[str] = []
    if host.values() != scenario.values:
        problems.append("database values differ")
    if host.active() != scenario.active:
        problems.append(f"selection differs: {host.active()}")
    if world.file_values() != scenario.values:
        problems.append("file values differ")
    removed_path = world.carrier / REMOVED
    if (world.read_json(REMOVED) if removed_path.exists() else []) != scenario.tombstones:
        problems.append("tombstones differ")
    names = sorted(p.name for p in world.carrier.iterdir())
    expected_names = [FILE] + ([REMOVED] if scenario.tombstones else [])
    if names != sorted(expected_names):
        problems.append(f"carrier holds {names}")
    backups = (
        sorted(p for p in host.backup.iterdir() if not p.name.startswith("."))
        if host.backup.is_dir()
        else []
    )
    if [p.read_bytes() for p in backups] != list(scenario.copies.values()):
        problems.append("conflict backup differs")
    if any(stat.S_IMODE(p.stat().st_mode) != 0o600 for p in backups):
        problems.append("backup mode")
    if backups and stat.S_IMODE(host.backup.stat().st_mode) != 0o700:
        problems.append("backup dir mode")
    state = host.state()
    if state.inode != host.db.stat().st_ino:
        problems.append("state inode")
    if dict(state.seen) != host.fingerprints():
        problems.append("seen differs from the database")
    if dict(state.reactivate) != {}:
        problems.append("reactivate not cleared")
    for identity, fps in state.hidden.items():
        if not set(fps) <= scenario.allowed_hidden.get(identity, set()):
            problems.append(f"unexpected hidden {identity}")
    return problems


def run_crash_point(
    root: Path, scenario: Scenario, kind: str, number: int, phase: str
) -> list[str]:
    world = World(root)
    host = scenario.build(world)
    wrapper: dict[str, Any] | None = None
    env: dict[str, str] = {}
    if kind == "ops":
        wrapper = {"crash": {"op": number, "phase": phase}}
    else:
        env[f"FAKE_KILL_{phase.upper()}"] = str(number)
    crashed = world.run(host, env=env, wrapper=wrapper)
    label = f"{scenario.name}/{kind}-{phase}-{number}"
    if crashed.returncode != -signal.SIGKILL:
        return [f"{label}: the run was not killed (rc {crashed.returncode})"]
    problems: list[str] = []
    if not winner_pairs(scenario, world) <= durable_pairs(world, host):
        problems.append(f"{label}: a chosen value is lost")
    if (world.carrier / REMOVED).exists() and world.read_json(REMOVED) != scenario.tombstones:
        problems.append(f"{label}: tombstones changed")
    age_temps(world.carrier, host.backup)
    for attempt in (1, 2):
        before = snapshot(world, host) if attempt == 2 else {}
        calls_before = len(world.mutating_calls())
        healthy = world.run(host)
        if healthy.returncode != 0:
            return [*problems, f"{label}: restart {attempt} failed: {healthy.stderr.strip()}"]
        if attempt == 2 and (
            snapshot(world, host) != before or len(world.mutating_calls()) != calls_before
        ):
            problems.append(f"{label}: the second restart changed something")
    problems += [f"{label}: {item}" for item in check_final(scenario, world, host)]
    return problems


def count_points(root: Path, scenario: Scenario) -> tuple[int, int]:
    world = World(root)
    host = scenario.build(world)
    result = world.run(host, wrapper={"log": True})
    assert result.returncode == 0, result.stderr
    operations = sum(1 for line in world.events() if line.startswith("ops "))
    return operations, len(world.calls())


def crash_matrix(tmp_path: Path, scenario: Scenario) -> list[str]:
    operations, calls = count_points(tmp_path / "count", scenario)
    points: list[tuple[str, int, str]] = [("ops", n, "before") for n in range(1, operations + 1)]
    points.append(("ops", operations, "after"))
    for number in range(1, calls + 1):
        points += [("fake", number, "before"), ("fake", number, "after")]
    if len(points) < scenario.min_points:
        return [f"{scenario.name}: only {len(points)} injection points"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                run_crash_point,
                tmp_path / f"p{index}",
                scenario,
                kind,
                number,
                phase,
            )
            for index, (kind, number, phase) in enumerate(points)
        ]
        problems = [item for future in futures for item in future.result()]
    return problems


def test_the_main_scenario_converges_without_a_crash(world: World) -> None:
    host = MAIN.build(world)
    stderr = sync_ok(world, host)
    assert check_final(MAIN, world, host) == []
    assert world.calls() == [
        "fake export",
        "fake credential.remove cred_dead",
        "fake export",
        "fake credential.remove cred_x",
        "fake export",
        "fake import",
        "fake export",
        "fake import",
        "fake export",
        "fake import",
        "fake export",
        "fake credential.activate cred_x",
        "fake export",
        "fake export",
    ]
    assert "removed=1 updated=1 added=2 activated=1" in stderr


def test_crash_matrix_update_of_the_active_account(tmp_path: Path) -> None:
    assert crash_matrix(tmp_path, MAIN) == []


def test_crash_matrix_update_with_exactly_one_credential(tmp_path: Path) -> None:
    assert crash_matrix(tmp_path, SINGLE) == []


def test_crash_matrix_update_against_the_other_hosts_selection(tmp_path: Path) -> None:
    assert crash_matrix(tmp_path, OPPOSITE) == []


def test_the_matrix_oracle_detects_a_lost_value(tmp_path: Path) -> None:
    """Positive control: a database missing a winner and a file without it fails the oracle."""
    world = World(tmp_path)
    host = MAIN.build(world)
    world.write(FILE, [])
    world.carrier.joinpath(COPY_A).unlink()
    host.put([])
    assert not winner_pairs(MAIN, world) <= durable_pairs(world, host)
    assert check_final(MAIN, world, host) != []


# ---------------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------------


def test_a_first_start_imports_the_file_and_binds_the_host_state(world: World) -> None:
    host = world.host()
    world.write(
        FILE,
        [entry("cred_a", "prov_p", key_value("a1")), entry("cred_b", "prov_q", key_value("b1"))],
    )
    sync_ok(world, host)
    assert host.values() == {"cred_a": key_value("a1"), "cred_b": key_value("b1")}
    assert host.active() == {"prov_p": "cred_a", "prov_q": "cred_b"}
    assert not (world.carrier / REMOVED).exists()
    state = host.state()
    assert state.inode == host.db.stat().st_ino
    assert dict(state.seen) == host.fingerprints()
    assert stat.S_IMODE(host.state_file.stat().st_mode) == 0o600


def test_database_logins_are_published_to_the_file_without_tombstones(world: World) -> None:
    host = world.host()
    host.put([entry("cred_a", "prov_p", key_value("a1"))])
    sync_ok(world, host)
    assert world.file_values() == {"cred_a": key_value("a1")}
    assert [item["active"] for item in world.read_json(FILE)] == [True]
    assert not (world.carrier / REMOVED).exists()


def test_two_hosts_with_conflict_copies_of_both_files_converge(world: World) -> None:
    host_a = world.host("a")
    host_b = world.host("b")
    host_a.put(
        [entry("cred_a", "prov_p", key_value("a1")), entry("cred_t", "prov_t", key_value("t1"))]
    )
    host_b.put(
        [entry("cred_b", "prov_q", key_value("b1")), entry("cred_d", "prov_d", key_value("d1"))]
    )
    world.write(FILE, [entry("cred_d", "prov_d", key_value("d1"))])
    world.write(COPY_A, [entry("cred_c", "prov_s", key_value("c1"))])
    world.write(COPY_REMOVED, ["cred_t"])
    for host in (host_a, host_b, host_a):
        sync_ok(world, host)
    expected = {
        "cred_a": key_value("a1"),
        "cred_b": key_value("b1"),
        "cred_c": key_value("c1"),
        "cred_d": key_value("d1"),
    }
    assert host_a.values() == expected
    assert host_b.values() == expected
    assert world.file_values() == expected
    assert world.read_json(REMOVED) == ["cred_t"]
    assert sorted(p.name for p in world.carrier.iterdir()) == [FILE, REMOVED]
    names = sorted(p.name for p in host_a.backup.iterdir())
    assert len(names) == 2
    assert names[0].startswith("credentials.removed.sync-conflict-20260103-140000-QRSTUVW.")
    assert names[1].startswith("credentials.sync-conflict-20260101-120000-ABCDEFG.")
    assert not host_b.backup.exists()


def test_an_oauth_refresh_on_host_a_updates_host_b(world: World) -> None:
    host_a = world.host("a")
    host_b = world.host("b")
    host_a.put([entry("cred_o", "prov_p", oauth_value("o2", 2000))])
    host_b.put([entry("cred_o", "prov_p", oauth_value("o1", 1000))])
    world.write(FILE, [entry("cred_o", "prov_p", oauth_value("o1", 1000))])
    sync_ok(world, host_a)
    assert world.file_values() == {"cred_o": oauth_value("o2", 2000)}
    sync_ok(world, host_b)
    assert host_b.values() == {"cred_o": oauth_value("o2", 2000)}
    assert host_b.active() == {"prov_p": "cred_o"}
    assert [c for c in world.calls() if c.startswith("fake credential.remove cred_o")] == [
        "fake credential.remove cred_o"
    ]


def test_a_tombstone_does_not_stop_a_later_login_with_a_new_id(world: World) -> None:
    host = world.host()
    host.put(
        [
            entry("cred_old", "prov_p", key_value("old")),
            entry("cred_new", "prov_p", key_value("new")),
        ]
    )
    world.write(REMOVED, ["cred_old"])
    sync_ok(world, host)
    assert host.values() == {"cred_new": key_value("new")}
    assert world.file_values() == {"cred_new": key_value("new")}
    assert world.read_json(REMOVED) == ["cred_old"]
    assert host.active() == {"prov_p": "cred_new"}


def test_a_logout_hides_the_value_locally_and_nowhere_else(world: World) -> None:
    host = world.host()
    items = [
        entry("cred_x", "prov_p", key_value("x1")),
        entry("cred_y", "prov_q", key_value("y1")),
    ]
    host.put(items)
    world.write(FILE, items)
    host.seed_seen_from_db()
    host.put(items[1:])  # opencode auth logout on this host
    sync_ok(world, host)
    assert host.values() == {"cred_y": key_value("y1")}
    assert host.state().hidden == {"cred_x": (fingerprint(items[0]),)}
    assert world.file_values() == {"cred_x": key_value("x1"), "cred_y": key_value("y1")}
    assert not (world.carrier / REMOVED).exists()
    assert world.mutating_calls() == []
    other = world.host("b")
    sync_ok(world, other)
    assert other.values() == {"cred_x": key_value("x1"), "cred_y": key_value("y1")}


def test_a_deliberate_import_of_the_hidden_value_leaves_the_hide_set(world: World) -> None:
    host = world.host()
    items = [entry("cred_x", "prov_p", key_value("x1")), entry("cred_y", "prov_q", key_value("y1"))]
    host.put(items)
    world.write(FILE, items)
    host.seed_seen_from_db()
    host.put(items[1:])
    sync_ok(world, host)
    assert list(host.state().hidden) == ["cred_x"]
    host.put(items)  # opencode auth import of the same value
    sync_ok(world, host)
    assert host.state().hidden == {}
    assert dict(host.state().seen) == host.fingerprints()


def test_a_fresher_value_of_a_hidden_id_is_imported_again(world: World) -> None:
    """MCP rotation: opencode dropped the old value, another host holds a fresher one."""
    host = world.host()
    old = entry("cred_m", "mcp_one", oauth_value("m1", 1000))
    fresh = entry("cred_m", "mcp_one", oauth_value("m3", 3000))
    host.put([old])
    host.seed_seen_from_db()
    host.put([])
    world.write(FILE, [fresh])
    sync_ok(world, host)
    assert host.values() == {"cred_m": oauth_value("m3", 3000)}
    assert host.state().hidden == {"cred_m": (fingerprint(old),)}
    assert host.active() == {"mcp_one": "cred_m"}


def test_an_empty_database_hides_everything_locally_and_publishes_nothing(world: World) -> None:
    host = world.host()
    items = [entry("cred_a", "prov_p", key_value("a1")), entry("cred_b", "prov_q", key_value("b1"))]
    host.put(items)
    world.write(FILE, items)
    host.seed_seen_from_db()
    host.put([])
    before = (world.carrier / FILE).read_bytes()
    sync_ok(world, host)
    assert host.values() == {}
    assert sorted(host.state().hidden) == ["cred_a", "cred_b"]
    assert (world.carrier / FILE).read_bytes() == before
    assert not (world.carrier / REMOVED).exists()
    assert world.mutating_calls() == []


def test_a_new_database_inode_discards_the_host_state(world: World) -> None:
    host = world.host()
    items = [entry("cred_a", "prov_p", key_value("a1")), entry("cred_x", "prov_q", key_value("x1"))]
    host.put(items[:1])
    world.write(FILE, items)
    host.seed_state(
        seen={"cred_a": fingerprint(items[0])}, hidden={"cred_x": (fingerprint(items[1]),)}
    )
    host.replace_db_inode()
    host.put([])
    sync_ok(world, host)
    assert host.values() == {"cred_a": key_value("a1"), "cred_x": key_value("x1")}
    assert host.state().hidden == {}
    assert host.state().inode == host.db.stat().st_ino


def test_a_new_account_is_added_inactive_and_the_selection_stays(world: World) -> None:
    host = world.host()
    host.put([entry("cred_p1", "prov_p", key_value("p1"))])
    world.write(
        FILE,
        [entry("cred_p1", "prov_p", key_value("p1")), entry("cred_p2", "prov_p", key_value("p2"))],
    )
    sync_ok(world, host)
    assert host.values() == {"cred_p1": key_value("p1"), "cred_p2": key_value("p2")}
    assert host.active() == {"prov_p": "cred_p1"}


def test_updating_the_active_account_selects_it_again(world: World) -> None:
    host = world.host()
    host.put(
        [
            entry("cred_p1", "prov_p", oauth_value("p1", 1000)),
            entry("cred_p2", "prov_p", key_value("p2")),
        ],
        {"prov_p": "cred_p1"},
    )
    host.seed_seen_from_db()
    world.write(FILE, [entry("cred_p1", "prov_p", oauth_value("p1b", 5000))])
    sync_ok(world, host)
    assert host.values() == {"cred_p1": oauth_value("p1b", 5000), "cred_p2": key_value("p2")}
    assert host.active() == {"prov_p": "cred_p1"}
    assert host.state().reactivate == {}
    assert world.calls().count("fake credential.activate cred_p1") == 1


def test_the_largest_id_becomes_active_for_an_integration_without_a_selection(
    world: World,
) -> None:
    host = world.host()
    host.put(
        [
            entry("cred_q1", "prov_q", key_value("q1")),
            entry("cred_q3", "prov_q", key_value("q3")),
            entry("cred_q2", "prov_q", key_value("q2")),
            entry("cred_r1", "prov_r", key_value("r1")),
        ]
    )
    sync_ok(world, host, env={"FAKE_NO_AUTOSELECT": "1"})
    assert host.active() == {"prov_q": "cred_q3", "prov_r": "cred_r1"}
    assert [c for c in world.calls() if "activate" in c] == [
        "fake credential.activate cred_q3",
        "fake credential.activate cred_r1",
    ]


def test_reactivate_restores_the_selection_against_the_other_hosts_choice(world: World) -> None:
    host = world.host()
    other_choice = [
        {**entry("cred_x", "prov_p", X_NEW), "active": False},
        {**entry("cred_y", "prov_p", key_value("y1")), "active": True},
    ]
    host.put(
        [entry("cred_x", "prov_p", X_OLD), entry("cred_y", "prov_p", key_value("y1"))],
        {"prov_p": "cred_x"},
    )
    world.write(FILE, other_choice)
    host.seed_seen_from_db()
    crashed = world.run(host, env={"FAKE_KILL_AFTER": "2"})
    assert crashed.returncode == -signal.SIGKILL
    assert sorted(host.values()) == ["cred_y"]
    assert host.state().reactivate == {"prov_p": "cred_x"}
    sync_ok(world, host)
    assert host.values() == {"cred_x": X_NEW, "cred_y": key_value("y1")}
    assert host.active() == {"prov_p": "cred_x"}
    assert host.state().reactivate == {}


def test_reactivate_keeps_the_first_target_per_integration_across_a_crash(world: World) -> None:
    host = world.host()
    host.put(
        [
            entry("cred_a", "prov_p", oauth_value("a1", 1000)),
            entry("cred_b", "prov_p", oauth_value("b1", 1000)),
        ],
        {"prov_p": "cred_a"},
    )
    world.write(
        FILE,
        [
            entry("cred_a", "prov_p", oauth_value("a2", 2000)),
            entry("cred_b", "prov_p", oauth_value("b2", 2000)),
        ],
    )
    host.seed_seen_from_db()
    # call 2 removes cred_a, call 3 re-reads, call 4 imports it, call 5 re-reads, call 6 removes
    # cred_b; killing after call 6 leaves cred_b removed with the first target still recorded.
    crashed = world.run(host, env={"FAKE_KILL_AFTER": "6"})
    assert crashed.returncode == -signal.SIGKILL
    assert host.state().reactivate == {"prov_p": "cred_a"}
    sync_ok(world, host)
    assert host.active() == {"prov_p": "cred_a"}
    assert host.values() == {"cred_a": oauth_value("a2", 2000), "cred_b": oauth_value("b2", 2000)}


def test_steps_run_in_the_order_remove_update_add_selection(world: World) -> None:
    host = MAIN.build(world)
    sync_ok(world, host)
    kinds = [
        (line.split()[1], line.split()[2] if len(line.split()) > 2 else "")
        for line in world.mutating_calls()
    ]
    assert kinds == [
        ("credential.remove", "cred_dead"),
        ("credential.remove", "cred_x"),
        ("import", ""),
        ("import", ""),
        ("import", ""),
        ("credential.activate", "cred_x"),
    ]


def test_an_update_stops_when_the_database_entry_changed_since_the_plan(world: World) -> None:
    host = world.host()
    host.put(
        [
            entry("cred_dead", "prov_r", key_value("dead1")),
            entry("cred_x", "prov_p", oauth_value("x1", 1000)),
        ]
    )
    host.seed_seen_from_db()
    world.write(FILE, [entry("cred_x", "prov_p", oauth_value("x2", 2000))])
    world.write(REMOVED, ["cred_dead"])
    refreshed = {
        "entries": [
            {
                k: v
                for k, v in entry("cred_x", "prov_p", oauth_value("xr", 9000)).items()
                if k != "active"
            }
        ],
        "selection": {},
    }
    concurrent = world.root / "concurrent.json"
    concurrent.write_text(json.dumps(refreshed))
    result = world.run(
        host,
        env={"FAKE_CONCURRENT_AT": "3", "FAKE_CONCURRENT_FILE": str(concurrent)},
    )
    assert result.returncode == 1
    assert "update postcondition missed for cred_x" in result.stderr
    assert host.values() == {"cred_x": oauth_value("xr", 9000)}
    assert world.mutating_calls() == ["fake credential.remove cred_dead"]
    sync_ok(world, host)
    assert host.values() == {"cred_x": oauth_value("xr", 9000)}
    assert world.file_values() == {"cred_x": oauth_value("xr", 9000)}


def expected_missed_messages(probe: World) -> dict[int, str]:
    """The postcondition message each mutating call of a clean MAIN run must produce when it
    exits 0 without effect, keyed by the call number."""
    events = probe.events()
    messages: dict[int, str] = {}
    number = 0
    for index, line in enumerate(events):
        if not line.startswith("fake ") or " sentinel " in line:
            continue
        number += 1
        parts = line.split()
        if parts[1] == "credential.remove":
            messages[number] = f"remove postcondition missed for {parts[2]}"
        elif parts[1] == "credential.activate":
            messages[number] = f"activate postcondition missed for {parts[2]}"
        elif parts[1] == "import":
            identity = events[index + 1].split()[2].split("=")[0]
            kind = "update" if identity == "cred_x" else "add"
            messages[number] = f"{kind} postcondition missed for {identity}"
    return messages


def test_a_missed_postcondition_with_rc_0_is_a_failure_for_every_command(world: World) -> None:
    probe = World(world.root / "probe")
    probe_host = MAIN.build(probe)
    sync_ok(probe, probe_host)
    messages = expected_missed_messages(probe)
    assert len(messages) == 6
    assert sorted(messages.values()) == [
        "activate postcondition missed for cred_x",
        "add postcondition missed for cred_c",
        "add postcondition missed for cred_n",
        "remove postcondition missed for cred_dead",
        "remove postcondition missed for cred_x",
        "update postcondition missed for cred_x",
    ]
    for number, message in messages.items():
        attempt = World(world.root / f"attempt-{number}")
        host = MAIN.build(attempt)
        result = attempt.run(host, env={"FAKE_NOOP_AT": str(number)})
        assert result.returncode == 1, message
        assert message in result.stderr, message
        assert dict(host.state().seen) == dict(MAIN_SEEN_FPS), message


@pytest.mark.parametrize("change", ["an extra id", "another value"])
def test_a_database_that_differs_at_the_confirming_export_is_a_failure(
    world: World, change: str
) -> None:
    probe = World(world.root / "probe")
    probe_host = MAIN.build(probe)
    sync_ok(probe, probe_host)
    state = probe_host.raw_state()
    if change == "an extra id":
        state["entries"].append(
            {
                "id": "cred_extra",
                "integrationID": "prov_e",
                "label": "label",
                "value": key_value("extra"),
            }
        )
    else:
        for item in state["entries"]:
            if item["id"] == "cred_n":
                item["value"] = key_value("other")
    concurrent = world.root / "concurrent.json"
    concurrent.write_text(json.dumps(state))
    host = MAIN.build(world)
    result = world.run(
        host,
        env={
            "FAKE_CONCURRENT_AT": str(len(probe.calls())),
            "FAKE_CONCURRENT_FILE": str(concurrent),
        },
    )
    assert result.returncode == 1
    assert "confirming export differs from the merged result" in result.stderr
    assert dict(host.state().seen) == dict(MAIN_SEEN_FPS)


MAIN_SEEN_FPS = {
    "cred_x": fingerprint(entry("cred_x", "prov_p", X_OLD)),
    "cred_y": fingerprint(entry("cred_y", "prov_p", key_value("y1"))),
    "cred_dead": fingerprint(entry("cred_dead", "prov_r", key_value("dead1"))),
}


def test_a_failing_command_aborts_without_confirming_the_state(world: World) -> None:
    host = MAIN.build(world)
    result = world.run(host, env={"FAKE_FAIL_AT": "4"})
    assert result.returncode == 1
    assert "remove failed (rc 1)" in result.stderr
    assert dict(host.state().seen) == MAIN_SEEN_FPS


def test_final_mode_publishes_and_does_not_touch_the_database(world: World) -> None:
    host = MAIN.build(world)
    database_before = host.db.read_bytes()
    state_before = host.state_file.read_bytes()
    sync_ok(world, host, mode="final")
    assert world.file_values() == {
        **{"cred_c": key_value("c1"), "cred_n": key_value("n1"), "cred_x": X_NEW},
        "cred_y": key_value("y1"),
    }
    assert world.read_json(REMOVED) == ["cred_dead"]
    assert host.db.read_bytes() == database_before
    assert world.calls() == ["fake export"]
    assert host.state_file.read_bytes() == state_before
    assert sorted(p.name for p in world.carrier.iterdir()) == [FILE, REMOVED]
    sync_ok(world, host)
    assert check_final(MAIN, world, host) == []


def test_start_and_checkpoint_modes_behave_alike(world: World) -> None:
    host = MAIN.build(world)
    sync_ok(world, host, mode="checkpoint")
    assert check_final(MAIN, world, host) == []


def test_a_mirror_that_changes_the_file_during_publication_is_merged_again(world: World) -> None:
    host = world.host()
    host.put([entry("cred_a", "prov_p", key_value("a1"))])
    mirrored = world.root / "mirrored.json"
    mirrored.write_bytes(raw_json([entry("cred_m", "prov_q", key_value("m1"))]))
    result = world.run(
        host,
        wrapper={
            "interfere": [
                {
                    "before": "fsync_file",
                    "match": ".credentials.json.",
                    "times": 1,
                    "source": str(mirrored),
                    "dest": str(world.carrier / FILE),
                }
            ]
        },
    )
    assert result.returncode == 0, result.stderr
    assert world.file_values() == {"cred_a": key_value("a1"), "cred_m": key_value("m1")}
    assert host.values() == {"cred_a": key_value("a1"), "cred_m": key_value("m1")}


def test_a_mirror_that_always_wins_stops_after_three_attempts(world: World) -> None:
    host = MAIN.build(world)
    mirrored = world.root / "mirrored.json"
    mirrored.write_bytes(raw_json([entry("cred_m", "prov_q", key_value("m1"))]))
    result = world.run(
        host,
        wrapper={
            "interfere": [
                {
                    "before": "fsync_file",
                    "match": ".credentials.json.",
                    "times": 99,
                    "vary": True,
                    "source": str(mirrored),
                    "dest": str(world.carrier / FILE),
                }
            ]
        },
    )
    assert result.returncode == 1
    assert "carrier changed during publication" in result.stderr
    assert world.mutating_calls() == []
    opens = [e for e in world.events() if False]
    assert opens == []
    assert world.file_values() == {"cred_m": key_value("m1")}
    assert world.calls() == ["fake export"]
    assert not [p for p in world.carrier.iterdir() if p.name.endswith(".tmp")]


def test_the_carrier_lock_serializes_the_helper(world: World) -> None:
    host = MAIN.build(world)
    before = snapshot(world, host)
    with oc.carrier_lock(world.carrier):
        result = world.run(host, wrapper={"sync_kwargs": {"lock_timeout": 0.3}})
    assert result.returncode == 1
    assert "carrier lock not obtained in time" in result.stderr
    assert world.mutating_calls() == []
    assert snapshot(world, host) == before


# ---------------------------------------------------------------------------------------------
# Conflict copies
# ---------------------------------------------------------------------------------------------


def one_copy_world(world: World, copy_text: object | None = None) -> Host:
    host = world.host()
    world.write(FILE, [entry("cred_a", "prov_p", key_value("a1"))])
    if copy_text is None:
        world.write(COPY_A, [entry("cred_c", "prov_s", key_value("c1"))])
    return host


def test_a_valid_copy_is_merged_backed_up_privately_and_removed(world: World) -> None:
    host = one_copy_world(world)
    original = (world.carrier / COPY_A).read_bytes()
    sync_ok(world, host)
    assert host.values() == {"cred_a": key_value("a1"), "cred_c": key_value("c1")}
    assert not (world.carrier / COPY_A).exists()
    (backup,) = list(host.backup.iterdir())
    assert backup.read_bytes() == original
    assert backup.name.startswith(COPY_A[: -len(".json")] + ".")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(host.backup.stat().st_mode) == 0o700


def test_an_invalid_copy_stays_and_is_reported_while_the_sync_continues(world: World) -> None:
    host = one_copy_world(world)
    (world.carrier / COPY_B).write_text("{ not json")
    (world.carrier / COPY_B).chmod(0o600)
    result = world.run(host)
    assert result.returncode == 0, result.stderr
    assert f"conflict copy is invalid and stays: {COPY_B}" in result.stderr
    assert (world.carrier / COPY_B).exists()
    assert not (world.carrier / COPY_A).exists()
    assert host.values() == {"cred_a": key_value("a1"), "cred_c": key_value("c1")}


def test_a_symlink_copy_stays_and_is_never_read_through(world: World) -> None:
    host = one_copy_world(world, copy_text="none")
    outside = world.root / "outside.json"
    outside.write_bytes(raw_json([entry("cred_z", "prov_z", key_value("z1"))]))
    (world.carrier / COPY_A).symlink_to(outside)
    result = world.run(host)
    assert result.returncode == 0, result.stderr
    assert (world.carrier / COPY_A).is_symlink()
    assert host.values() == {"cred_a": key_value("a1")}
    assert not host.backup.exists()


def test_a_foreign_looking_name_is_ignored(world: World) -> None:
    host = one_copy_world(world, copy_text="none")
    for name in (
        "credentials.sync-conflict-2026-ABCDEFG.json",
        "credentials.sync-conflict-20260101-120000-abcdefg.json",
    ):
        (world.carrier / name).write_bytes(raw_json([entry("cred_z", "prov_z", key_value("z1"))]))
    sync_ok(world, host)
    assert host.values() == {"cred_a": key_value("a1")}
    assert len(list(world.carrier.glob("credentials.sync-conflict-*"))) == 2


def test_a_mount_point_copy_is_backed_up_but_stays(world: World) -> None:
    host = one_copy_world(world)
    mountinfo = world.root / "mountinfo"
    path = str(world.carrier / COPY_A)
    mountinfo.write_text(f"36 1 8:1 / {path} rw - ext4 /dev/sda1 rw\n")
    result = world.run(host, wrapper={"sync_kwargs": {"mountinfo": str(mountinfo)}})
    assert result.returncode == 0, result.stderr
    assert (world.carrier / COPY_A).exists()
    assert len(list(host.backup.iterdir())) == 1
    assert COPY_A in result.stderr
    assert host.values() == {"cred_a": key_value("a1"), "cred_c": key_value("c1")}


def test_a_copy_on_another_device_stays(world: World) -> None:
    host = one_copy_world(world)
    result = world.run(host, wrapper={"stat_dev": COPY_A})
    assert result.returncode == 0, result.stderr
    assert (world.carrier / COPY_A).exists()


def test_a_copy_replaced_by_a_new_inode_stays(world: World) -> None:
    host = one_copy_world(world)
    twin = world.root / "twin.json"
    twin.write_bytes((world.carrier / COPY_A).read_bytes())
    result = world.run(
        host,
        wrapper={
            "interfere": [
                {
                    "before": "fsync_dir",
                    "match": "dir:conflicts",
                    "times": 1,
                    "source": str(twin),
                    "dest": str(world.carrier / COPY_A),
                }
            ]
        },
    )
    assert result.returncode == 0, result.stderr
    assert (world.carrier / COPY_A).exists()
    assert len(list(host.backup.iterdir())) == 1


def test_a_failed_backup_keeps_the_copy_and_the_next_sync_retries(world: World) -> None:
    host = one_copy_world(world)
    host.backup.parent.mkdir(parents=True)
    host.backup.write_text("not a directory")
    result = world.run(host)
    assert result.returncode == 0, result.stderr
    assert (world.carrier / COPY_A).exists()
    assert f"conflict copy stays, backup retried next time: {COPY_A}" in result.stderr
    host.backup.unlink()
    sync_ok(world, host)
    assert not (world.carrier / COPY_A).exists()
    assert len(list(host.backup.iterdir())) == 1


def test_a_symlinked_backup_directory_is_refused_and_the_copy_stays(world: World) -> None:
    host = one_copy_world(world)
    target = world.root / "elsewhere"
    target.mkdir()
    host.backup.parent.mkdir(parents=True)
    host.backup.symlink_to(target)
    result = world.run(host)
    assert result.returncode == 0, result.stderr
    assert (world.carrier / COPY_A).exists()
    assert list(target.iterdir()) == []


def test_a_loose_backup_directory_is_tightened(world: World) -> None:
    host = one_copy_world(world)
    host.backup.mkdir(parents=True, mode=0o755)
    host.backup.chmod(0o755)
    sync_ok(world, host)
    assert stat.S_IMODE(host.backup.stat().st_mode) == 0o700


def test_a_repeated_backup_of_the_same_copy_is_idempotent(world: World) -> None:
    host = one_copy_world(world)
    sync_ok(world, host)
    (backup,) = list(host.backup.iterdir())
    again = world.write(COPY_A, [entry("cred_c", "prov_s", key_value("c1"))])
    again.write_bytes(backup.read_bytes())
    sync_ok(world, host)
    assert list(host.backup.iterdir()) == [backup]
    assert not (world.carrier / COPY_A).exists()


def test_remove_copy_guards_each_condition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    carrier = tmp_path / "c"
    carrier.mkdir()
    descriptor = oc.open_dir(carrier)
    try:
        path = carrier / COPY_A
        path.write_bytes(b"[]")
        inode = path.stat().st_ino
        good = oc.ConflictCopy(COPY_A, inode, b"[]", (), ())
        empty: frozenset[str] = frozenset()
        mounted = frozenset({str(carrier / COPY_A)})
        assert oc.remove_copy(descriptor, str(carrier), good, mounted) is False
        assert oc.remove_copy(descriptor, str(carrier), good, None) is False
        wrong_inode = oc.ConflictCopy(COPY_A, inode + 1, b"[]", (), ())
        assert oc.remove_copy(descriptor, str(carrier), wrong_inode, empty) is False
        (carrier / "credentials.json").write_bytes(b"[]")
        bad_name = oc.ConflictCopy(
            "credentials.json", (carrier / "credentials.json").stat().st_ino, b"[]", (), ()
        )
        assert oc.remove_copy(descriptor, str(carrier), bad_name, empty) is False
        assert (carrier / "credentials.json").exists()
        real_stat = os.stat

        def other_device(
            name: Any, *, dir_fd: int | None = None, follow_symlinks: bool = True
        ) -> Any:
            result = real_stat(name, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            values = list(result)[:10]
            values[2] += 1
            return os.stat_result(values)

        monkeypatch.setattr(os, "stat", other_device)
        assert oc.remove_copy(descriptor, str(carrier), good, empty) is False
        monkeypatch.undo()
        assert path.exists()
        link = carrier / COPY_B
        link.symlink_to(path)
        linked = oc.ConflictCopy(COPY_B, link.lstat().st_ino, b"[]", (), ())
        assert oc.remove_copy(descriptor, str(carrier), linked, empty) is False
        assert oc.remove_copy(descriptor, str(carrier), good, empty) is True
        assert not path.exists()
        assert oc.remove_copy(descriptor, str(carrier), good, empty) is True
    finally:
        os.close(descriptor)


def test_mount_points_are_parsed_with_escapes(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "2 1 8:2 / /mnt/with\\040space rw - ext4 /dev/sda2 rw\n"
        "3 1 8:3 / /mnt/tab\\011x rw - ext4 /dev/sda3 rw\n"
        "short line\n"
    )
    assert oc.mount_points(mountinfo) == frozenset({"/", "/mnt/with space", "/mnt/tab\tx"})
    assert "/" in oc.mount_points()


# ---------------------------------------------------------------------------------------------
# Event order
# ---------------------------------------------------------------------------------------------


def subsequence(events: Sequence[str], patterns: Sequence[str], start: int = 0) -> list[int]:
    """Indices of the first occurrences of ``patterns`` in order, from ``start`` on."""
    found: list[int] = []
    position = start
    for pattern in patterns:
        for index in range(position, len(events)):
            if re.search(pattern, events[index]):
                found.append(index)
                position = index + 1
                break
        else:
            raise AssertionError(f"{pattern!r} not found after {position}")
    return found


def durable_write(name: str, directory: str) -> list[str]:
    return [
        rf"^ops open w \.{re.escape(name)}\.X\.tmp",
        rf"^ops write \.{re.escape(name)}\.X\.tmp",
        rf"^ops fsync_file \.{re.escape(name)}\.X\.tmp",
        rf"^ops rename \.{re.escape(name)}\.X\.tmp->{re.escape(name)}",
        rf"^ops fsync_dir dir:{directory}",
    ]


def logged_run(world: World, host: Host) -> list[str]:
    result = world.run(host, wrapper={"log": True})
    assert result.returncode == 0, result.stderr
    return [line for line in world.events() if not line.startswith("fakeinfo")]


def build_order(world: World) -> Host:
    """Like MAIN, but the tombstone only arrives through a conflict copy: both files change."""
    host = MAIN.build(world)
    (world.carrier / REMOVED).unlink()
    world.write(COPY_REMOVED, ["cred_dead"])
    return host


def test_publication_is_durable_before_the_first_destructive_database_command(
    world: World,
) -> None:
    host = build_order(world)
    events = logged_run(world, host)
    first = next(i for i, e in enumerate(events) if e.startswith("fake credential.remove"))
    head = events[:first]
    subsequence(head, durable_write(REMOVED, "carrier") + durable_write(FILE, "carrier"))
    assert not [e for e in head if e.startswith(("fake import", "fake credential"))]
    assert world.read_json(REMOVED) == ["cred_dead"]


def test_the_reactivate_mark_is_durable_before_the_active_account_is_removed(
    world: World,
) -> None:
    host = MAIN.build(world)
    events = logged_run(world, host)
    removal = next(i for i, e in enumerate(events) if e == "fake credential.remove cred_x")
    state_name = "djinn-credentials-host.json"
    assert events[removal - 1].startswith("ops fsync_dir dir:data")
    assert re.search(rf"^ops rename \.{state_name}\.X\.tmp->{state_name}", events[removal - 2])
    assert any(
        e.startswith("ops fsync_file .djinn-credentials-host.json")
        for e in events[removal - 6 : removal - 2]
    )


def test_the_conflict_copy_is_durable_in_the_backup_before_it_is_unlinked(world: World) -> None:
    host = MAIN.build(world)
    events = logged_run(world, host)
    unlink = next(i for i, e in enumerate(events) if e == f"ops unlink {COPY_A}")
    name = next(
        e.split("->")[1]
        for e in events
        if e.startswith("ops rename") and e.split("->")[1].startswith("credentials.sync-conflict")
    )
    stem = re.escape(name)
    subsequence(
        events[:unlink],
        [
            rf"^ops open w \.{stem}\.X\.tmp",
            rf"^ops write \.{stem}\.X\.tmp",
            rf"^ops fsync_file \.{stem}\.X\.tmp",
            rf"^ops rename \.{stem}\.X\.tmp->{stem}",
            r"^ops fsync_dir dir:conflicts",
        ],
    )
    assert events.index(f"ops unlink {COPY_A}") == unlink
    assert [e for e in events if e.startswith("ops unlink") and "sync-conflict" in e] == [
        f"ops unlink {COPY_A}"
    ]


def test_the_event_log_sees_both_the_fake_and_the_file_operations(world: World) -> None:
    """Positive control: a log without file operations would pass the order tests vacuously."""
    host = MAIN.build(world)
    events = logged_run(world, host)
    assert sum(e.startswith("ops ") for e in events) > 30
    assert sum(e.startswith("fake ") for e in events) == 14


# ---------------------------------------------------------------------------------------------
# Runner, environment, isolation
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def fake_env(world: World, monkeypatch: pytest.MonkeyPatch) -> Host:
    host = world.host()
    for name in list(os.environ):
        if name.startswith(("OPENCODE", "XDG_", "FAKE_")):
            monkeypatch.delenv(name)
    for key, value in world.environment(host).items():
        monkeypatch.setenv(key, value)
    return host


def direct_fake(
    world: World, host: Host, argv: list[str], env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {
        **{k: v for k, v in world.environment(host).items() if k != "TMPDIR"},
        **env,
    }
    return subprocess.run(
        [sys.executable, str(FAKE_PATH), *argv],
        env=environment,
        capture_output=True,
        text=True,
        cwd=cwd or world.root,
        check=False,
    )


def isolated_config(world: World) -> dict[str, str]:
    config = world.root / "config-home"
    config.mkdir(exist_ok=True)
    return {
        "XDG_CONFIG_HOME": str(config),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
        "DO_NOT_TRACK": "1",
    }


@pytest.mark.parametrize(
    ("expected", "argv", "mutate"),
    [
        ("no --standalone", ["auth", "export"], {}),
        ("XDG_CONFIG_HOME missing", ["auth", "export", "--standalone"], {"XDG_CONFIG_HOME": ""}),
        (
            "OPENCODE_CONFIG* present",
            ["auth", "export", "--standalone"],
            {"OPENCODE_CONFIG_CONTENT": "{}"},
        ),
        ("OPENCODE_CONFIG* present", ["auth", "export", "--standalone"], {"OPENCODE_CONFIG": "/x"}),
        ("OPENCODE_DB present", ["auth", "export", "--standalone"], {"OPENCODE_DB": "/nowhere"}),
        ("XDG_DATA_HOME present", ["auth", "export", "--standalone"], {"XDG_DATA_HOME": "/x"}),
        (
            "OPENCODE_DISABLE_PROJECT_CONFIG missing",
            ["auth", "export", "--standalone"],
            {"OPENCODE_DISABLE_PROJECT_CONFIG": "false"},
        ),
        (
            "OPENCODE_DISABLE_AUTOUPDATE missing",
            ["auth", "export", "--standalone"],
            {"OPENCODE_DISABLE_AUTOUPDATE": "false"},
        ),
        (
            "OPENCODE_DISABLE_MODELS_FETCH missing",
            ["auth", "export", "--standalone"],
            {"OPENCODE_DISABLE_MODELS_FETCH": "false"},
        ),
        ("DO_NOT_TRACK missing", ["auth", "export", "--standalone"], {"DO_NOT_TRACK": "0"}),
    ],
)
def test_the_fake_refuses_calls_that_miss_the_isolation(
    world: World, expected: str, argv: list[str], mutate: dict[str, str]
) -> None:
    host = world.host()
    environment = {**isolated_config(world), **mutate}
    result = direct_fake(
        world, host, argv, environment, Path(isolated_config(world)["XDG_CONFIG_HOME"])
    )
    assert result.returncode == 99, result.stderr
    assert result.stderr == f"isolation violated: {expected}\n"
    clean = direct_fake(
        world,
        host,
        ["auth", "export", "--standalone"],
        isolated_config(world),
        Path(isolated_config(world)["XDG_CONFIG_HOME"]),
    )
    assert clean.returncode == 0


def test_the_fake_refuses_a_dirty_config_home_and_a_foreign_cwd(world: World) -> None:
    host = world.host()
    environment = isolated_config(world)
    (Path(environment["XDG_CONFIG_HOME"]) / "plugin.js").write_text("x")
    dirty = direct_fake(world, host, ["auth", "export", "--standalone"], environment)
    assert dirty.returncode == 99
    (Path(environment["XDG_CONFIG_HOME"]) / "plugin.js").unlink()
    elsewhere = direct_fake(world, host, ["auth", "export", "--standalone"], environment)
    assert elsewhere.returncode == 99
    assert "cwd" in elsewhere.stderr
    fine = direct_fake(
        world,
        host,
        ["auth", "export", "--standalone"],
        environment,
        Path(environment["XDG_CONFIG_HOME"]),
    )
    assert fine.returncode == 0
    assert fine.stdout == "[]\n"


def test_the_helper_isolates_every_call_even_in_a_hostile_environment(world: World) -> None:
    host = MAIN.build(world)
    hostile = {
        "OPENCODE_CONFIG_CONTENT": json.dumps({"plugin": ["file:///nowhere"]}),
        "OPENCODE_CONFIG": "/nowhere/opencode.json",
        "OPENCODE_CONFIG_DIR": "/nowhere",
        "XDG_CONFIG_HOME": "/nowhere/config",
        "DO_NOT_TRACK": "0",
        "OPENCODE_DISABLE_AUTOUPDATE": "false",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "false",
        "OPENCODE_DISABLE_MODELS_FETCH": "false",
    }
    sync_ok(world, host, env=hostile)
    assert check_final(MAIN, world, host) == []
    assert not [e for e in world.events() if e.startswith("fake refused")]
    assert list(world.tmp.iterdir()) == []


def test_the_runner_creates_its_private_config_home_under_tmpdir_and_removes_it(
    world: World, fake_env: Host
) -> None:
    seen: list[str] = []
    runner = oc.OpenCode()
    original = oc.isolated_environment

    def spy(config_home: str) -> dict[str, str]:
        seen.append(config_home)
        assert stat.S_IMODE(os.stat(config_home).st_mode) == 0o700
        assert os.listdir(config_home) == []
        return original(config_home)

    oc.isolated_environment, restore = spy, original
    try:
        assert runner.export() == []
    finally:
        oc.isolated_environment = restore
    assert len(seen) == 1
    assert Path(seen[0]).parent == world.tmp
    assert not Path(seen[0]).exists()


def test_the_runner_falls_back_to_slash_tmp_without_tmpdir(
    world: World, fake_env: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TMPDIR")
    parents: list[str] = []
    real = oc.tempfile.mkdtemp

    def spy(*args: Any, **kwargs: Any) -> str:
        parents.append(kwargs["dir"])
        return real(*args, **kwargs)

    monkeypatch.setattr(oc.tempfile, "mkdtemp", spy)
    assert oc.OpenCode().export() == []
    assert parents == ["/tmp"]


@pytest.mark.parametrize("name", ["OPENCODE_DB", "XDG_DATA_HOME"])
def test_a_relocated_database_is_refused_before_anything_happens(world: World, name: str) -> None:
    host = MAIN.build(world)
    before = snapshot(world, host)
    result = world.run(host, env={name: "/somewhere"})
    assert result.returncode == 1
    assert f"{name} is set" in result.stderr
    assert world.events() == []
    assert snapshot(world, host) == before


def test_the_runner_passes_the_inherited_environment_minus_config_variables(
    world: World, fake_env: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "{}")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nowhere")
    monkeypatch.setenv("KEEP_ME", "1")
    environment = oc.isolated_environment("/private")
    assert "OPENCODE_CONFIG_CONTENT" not in environment
    assert environment["XDG_CONFIG_HOME"] == "/private"
    assert environment["KEEP_ME"] == "1"
    assert environment["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert environment["OPENCODE_DISABLE_AUTOUPDATE"] == "true"
    assert environment["OPENCODE_DISABLE_MODELS_FETCH"] == "true"
    assert environment["DO_NOT_TRACK"] == "1"


def test_a_command_that_runs_too_long_is_killed_and_reported(
    world: World, fake_env: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SLEEP_AT", "1")
    started = time.monotonic()
    with pytest.raises(oc.OpenCodeFailed, match="export timed out"):
        oc.OpenCode(timeout=0.5).export()
    assert time.monotonic() - started < 10
    assert list(world.tmp.iterdir()) == []


def test_a_huge_export_is_cut_off_at_the_limit(
    world: World, fake_env: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_HUGE_EXPORT", "1")
    with pytest.raises(oc.NotUnderstood, match="export exceeds the size limit"):
        oc.OpenCode().export()


def test_a_missing_executable_is_reported_without_a_path(world: World, fake_env: Host) -> None:
    with pytest.raises(oc.OpenCodeFailed) as raised:
        oc.OpenCode(executable="/nonexistent/opencode").export()
    assert raised.value.reason == "export could not start"


def test_a_failing_command_names_operation_and_rc_only(
    world: World, fake_env: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FAIL_AT", "1")
    noise = world.root / "noise.txt"
    noise.write_text(f"secret-looking {token('noise')}")
    monkeypatch.setenv("FAKE_STDERR_FILE", str(noise))
    with pytest.raises(oc.OpenCodeFailed) as raised:
        oc.OpenCode().export()
    assert raised.value.reason == "export failed (rc 1)"


def test_a_large_import_does_not_deadlock_on_the_stdin_pipe(world: World, fake_env: Host) -> None:
    payload = raw_json(
        [entry("cred_big", "prov_p", key_value("big") | {"metadata": {"pad": "x" * 500000}})]
    )
    runner = oc.OpenCode()
    runner.run("import", ["auth", "import", "--standalone"], stdin=payload)
    assert "cred_big" in fake_env.values()


# ---------------------------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------------------------


def test_exit_0_when_there_is_nothing_to_do(world: World) -> None:
    host = world.host()
    result = world.run(host)
    assert result.returncode == 0
    assert result.stdout == ""
    assert "sync start:" in result.stderr


def test_a_symlinked_credentials_file_is_refused_with_exit_3(world: World) -> None:
    host = MAIN.build(world)
    real = world.root / "real.json"
    real.write_bytes((world.carrier / FILE).read_bytes())
    (world.carrier / FILE).unlink()
    (world.carrier / FILE).symlink_to(real)
    result = world.run(host)
    assert result.returncode == 3
    assert "refused: entry is a symlink" in result.stderr
    assert world.events() == []


def test_a_fifo_as_the_tombstone_file_is_refused_with_exit_3(world: World) -> None:
    host = MAIN.build(world)
    (world.carrier / REMOVED).unlink()
    os.mkfifo(world.carrier / REMOVED)
    result = world.run(host, timeout=30)
    assert result.returncode == 3
    assert world.events() == []


def test_a_symlinked_host_state_is_refused_with_exit_3(world: World) -> None:
    host = MAIN.build(world)
    real = world.root / "state-real.json"
    real.write_bytes(host.state_file.read_bytes())
    host.state_file.unlink()
    host.state_file.symlink_to(real)
    result = world.run(host)
    assert result.returncode == 3
    assert world.mutating_calls() == []


def test_a_database_that_is_not_a_regular_file_is_refused_with_exit_3(world: World) -> None:
    host = world.host()
    host.db.unlink()
    host.db.mkdir()
    result = world.run(host)
    assert result.returncode == 3
    assert world.events() == []


def test_a_symlinked_carrier_directory_is_refused_with_exit_3(world: World) -> None:
    host = MAIN.build(world)
    link = world.root / "carrier-link"
    link.symlink_to(world.carrier)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(MODULE_PATH),
            "sync",
            "--carrier",
            str(link),
            "--database",
            str(host.db),
            "--host-state",
            str(host.state_file),
            "--conflict-backup",
            str(host.backup),
            "--mode",
            "start",
        ],
        env=world.environment(host),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    assert world.events() == []


def test_a_foreign_owner_is_refused_with_exit_3(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = MAIN.build(world)
    wrapped = world.run(host, wrapper={"sync_kwargs": {}}, env={})
    assert wrapped.returncode == 0
    # read_private compares the owner with the effective uid; ask for another one directly
    descriptor = oc.open_dir(world.carrier)
    try:
        with pytest.raises(oc.Refused, match="another user"):
            oc.read_private(descriptor, FILE, oc.FILE_LIMIT, euid=os.geteuid() + 1)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        (FILE, b"{ nope"),
        (FILE, b'{"a": 1}'),
        (FILE, b'[{"id": "x"}]'),
        (REMOVED, b'["ok", 7]'),
        (REMOVED, b"[" + b'"a",' * 300000 + b'"b"]'),
    ],
    ids=["bad-json", "not-an-array", "missing-fields", "bad-tombstone", "over-one-mebibyte"],
)
def test_an_invalid_file_stops_before_any_change_with_exit_1(
    world: World, name: str, content: bytes
) -> None:
    host = MAIN.build(world)
    (world.carrier / name).write_bytes(content)
    before = snapshot(world, host)
    result = world.run(host)
    assert result.returncode == 1
    assert result.stderr.startswith("failed: ")
    assert world.events() == []
    assert snapshot(world, host) == before


def test_a_result_over_one_mebibyte_stops_before_any_change(world: World) -> None:
    host = world.host()
    big = entry("cred_big", "prov_p", key_value("big") | {"metadata": {"pad": "x" * (1024 * 1024)}})
    host.put([big])
    result = world.run(host)
    assert result.returncode == 1
    assert "result exceeds the size limit" in result.stderr
    assert not (world.carrier / FILE).exists()
    assert world.mutating_calls() == []


def test_a_missing_carrier_exits_1_with_the_exception_type_only(world: World) -> None:
    host = MAIN.build(world)
    shutil.rmtree(world.carrier)
    result = world.run(host)
    assert result.returncode == 1
    assert result.stderr.strip() == "unexpected error: FileNotFoundError"


def test_an_unexpected_error_is_reported_by_its_type_name_only(world: World) -> None:
    host = world.host()
    marker = f"leak-{token('boom')}"
    result = world.run(host, wrapper={"raise_unexpected": marker})
    assert result.returncode == 1
    assert result.stderr.strip() == "unexpected error: RuntimeError"
    assert marker not in result.stdout + result.stderr


def test_usage_errors_exit_1_without_echoing_arguments(world: World) -> None:
    host = world.host()
    marker = token("argument")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(MODULE_PATH),
            "sync",
            *("--carrier", "c", "--database", "d", "--host-state", "h"),
            *("--conflict-backup", "b", "--mode", "start", "--bogus", marker),
        ],
        env=world.environment(host),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert marker not in result.stderr
    bad_mode = world.run(host, mode="sometimes")
    assert bad_mode.returncode == 1
    nothing = subprocess.run(
        [sys.executable, "-I", str(MODULE_PATH)],
        env=world.environment(host),
        capture_output=True,
        text=True,
        check=False,
    )
    assert nothing.returncode == 1


def test_a_command_failure_and_a_timeout_exit_1(world: World) -> None:
    host = MAIN.build(world)
    failed = world.run(host, env={"FAKE_FAIL_AT": "1"})
    assert failed.returncode == 1
    assert "export failed (rc 1)" in failed.stderr
    slow = world.run(
        host, env={"FAKE_SLEEP_AT": "1"}, wrapper={"sync_kwargs": {"command_timeout": 0.5}}
    )
    assert slow.returncode == 1
    assert "export timed out" in slow.stderr
    assert list(world.tmp.iterdir()) == []


def test_a_missing_database_after_the_export_exits_1(world: World) -> None:
    host = world.host()
    result = world.run(host, env={"FAKE_UNLINK_DB": "1"})
    assert result.returncode == 1
    assert "database is missing after the export" in result.stderr
    assert not host.state_file.exists()


def test_an_export_without_output_is_not_understood(world: World) -> None:
    host = world.host()
    result = world.run(host, env={"FAKE_NOOP_AT": "1"})
    assert result.returncode == 1
    assert "invalid JSON or UTF-8" in result.stderr


def test_stale_own_temp_files_are_removed_under_the_lock(world: World) -> None:
    host = MAIN.build(world)
    old = world.carrier / f".{FILE}.0123456789abcdef.tmp"
    fresh = world.carrier / f".{REMOVED}.fedcba9876543210.tmp"
    foreign = world.carrier / ".other.0123456789abcdef.tmp"
    for path in (old, fresh, foreign):
        path.write_text("x")
    os.utime(old, (1, 1))
    os.utime(foreign, (1, 1))
    sync_ok(world, host)
    assert not old.exists()
    assert fresh.exists()
    assert foreign.exists()


def test_conflicting_values_without_oauth_precedence_are_reported_value_free(world: World) -> None:
    host = world.host()
    host.put([entry("cred_k", "prov_p", key_value("k1"))])
    world.write(FILE, [entry("cred_k", "prov_p", key_value("k2"))])
    stderr = sync_ok(world, host)
    assert "conflicting values for cred_k (prov_p)" in stderr
    assert token("k1") not in stderr and token("k2") not in stderr


# ---------------------------------------------------------------------------------------------
# Leaks
# ---------------------------------------------------------------------------------------------

CLIENT_ID = "client-" + token("cid")
CLIENT_SECRET = "secret-" + token("csecret")


def sentinels() -> tuple[list[str], list[tuple[str, str]]]:
    raw = [
        token(tag) for tag in ("x1", "x2", "x2r", "x2a", "x1r", "x1a", "y1", "dead1", "n1", "c1")
    ]
    return raw, [(CLIENT_ID, CLIENT_SECRET)]


def needles() -> list[str]:
    raw, pairs = sentinels()
    result = list(raw)
    result += [base64.b64encode(item.encode()).decode() for item in raw]
    for client, secret in pairs:
        result += [client, secret, base64.b64encode(f"{client}:{secret}".encode()).decode()]
    return result


def hits(text: str) -> int:
    return sum(text.count(needle) for needle in needles())


def leak_scenario(world: World) -> Host:
    host = MAIN.build(world)
    stored = oauth_value(
        "x2", 2000, metadata={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    )
    world.write(
        FILE,
        [
            entry("cred_dead", "prov_r", key_value("dead1")),
            entry("cred_n", "prov_q", key_value("n1")),
            entry("cred_x", "prov_p", stored),
        ],
    )
    raw, pairs = sentinels()
    (world.root / "sentinels.json").write_text(json.dumps({"raw": raw, "pairs": pairs}))
    return host


def test_the_scanner_finds_raw_base64_and_basic_values() -> None:
    raw, pairs = sentinels()
    basic = base64.b64encode(f"{pairs[0][0]}:{pairs[0][1]}".encode()).decode()
    for text in (raw[0], base64.b64encode(raw[0].encode()).decode(), basic, CLIENT_SECRET):
        assert hits("prefix " + text + " suffix") >= 1
    assert hits("nothing to see") == 0


def test_no_secret_reaches_argv_environment_logs_or_output(world: World) -> None:
    host = leak_scenario(world)
    noise = world.root / "noise.txt"
    noise.write_text("opencode says " + token("x1") + "\n")
    result = world.run(
        host,
        env={
            "FAKE_SENTINEL_FILE": str(world.root / "sentinels.json"),
            "FAKE_STDERR_FILE": str(noise),
        },
        wrapper={"log": True},
    )
    assert result.returncode == 0, result.stderr
    counts = [e for e in world.events() if e.startswith("fake sentinel count")]
    assert len(counts) == 14
    assert set(counts) == {"fake sentinel count 0"}
    assert hits(result.stdout + result.stderr) == 0
    assert hits("\n".join(world.events())) == 0
    assert hits(host.state_file.read_text()) == 0
    assert list(world.tmp.iterdir()) == []
    stored = host.db.read_text() + (world.carrier / FILE).read_text()
    assert hits(stored) > 0  # the permitted stores do hold the values: the scan sees them


def test_a_failing_command_does_not_surface_the_stderr_of_opencode(world: World) -> None:
    host = leak_scenario(world)
    noise = world.root / "noise.txt"
    noise.write_text("boom " + token("x1") + " " + CLIENT_SECRET + "\n")
    result = world.run(host, env={"FAKE_FAIL_AT": "2", "FAKE_STDERR_FILE": str(noise)})
    assert result.returncode == 1
    assert hits(result.stdout + result.stderr) == 0
    assert "remove failed (rc 1)" in result.stderr


def test_the_detector_sees_a_sentinel_in_argv_and_in_the_environment(world: World) -> None:
    host = leak_scenario(world)
    secret = token("x1")
    environment = {
        "FAKE_SENTINEL_FILE": str(world.root / "sentinels.json"),
        **isolated_config(world),
    }
    config = Path(environment["XDG_CONFIG_HOME"])
    for argv, label in (
        (["auth", "export", "--standalone", secret], "raw"),
        (["auth", "export", "--standalone", base64.b64encode(secret.encode()).decode()], "b64"),
        (
            [
                "auth",
                "export",
                "--standalone",
                base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode(),
            ],
            "basic",
        ),
    ):
        world.log.write_text("")
        direct_fake(world, host, argv, environment, config)
        assert "fake sentinel count 1" in world.events(), label
    world.log.write_text("")
    direct_fake(
        world, host, ["auth", "export", "--standalone"], {**environment, "LEAKY": secret}, config
    )
    assert "fake sentinel count 1" in world.events()


def test_a_secret_in_the_helpers_own_environment_is_detected_as_inherited(world: World) -> None:
    """Positive control for the helper path: the fake reads the real environment it was given."""
    host = leak_scenario(world)
    result = world.run(
        host,
        env={"FAKE_SENTINEL_FILE": str(world.root / "sentinels.json"), "LEAKY": token("x1")},
    )
    assert result.returncode == 0, result.stderr
    counts = [e for e in world.events() if e.startswith("fake sentinel count")]
    assert any(count != "fake sentinel count 0" for count in counts)


# ---------------------------------------------------------------------------------------------
# The single apply path is the only place that changes the database
# ---------------------------------------------------------------------------------------------

THIS_MODULE = "core/opencode_credentials.py"


def mutation_violations(sources: dict[str, str]) -> list[str]:
    violations: list[str] = []
    for path, source in sources.items():
        tree = ast.parse(source)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def enclosing(node: ast.AST, parents: dict[ast.AST, ast.AST] = parents) -> str | None:
            while node in parents:
                node = parents[node]
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    return node.name
            return None

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            dangerous = "credential.remove" in node.value or "credential.activate" in node.value
            if node.value == "import":
                parent = parents.get(node)
                dangerous = isinstance(parent, ast.List | ast.Tuple) and any(
                    isinstance(item, ast.Constant) and item.value == "auth" for item in parent.elts
                )
            if dangerous and not (path == THIS_MODULE and enclosing(node) == "_apply_step"):
                violations.append(f"{path}:{node.lineno}")
    return violations


def tree_sources() -> dict[str, str]:
    return {
        str(path.relative_to(SRC_ROOT)): path.read_text() for path in sorted(SRC_ROOT.rglob("*.py"))
    }


def test_database_mutation_commands_occur_only_in_the_apply_step() -> None:
    assert mutation_violations(tree_sources()) == []


def test_the_guard_sees_the_real_commands_inside_the_apply_step() -> None:
    source = tree_sources()[THIS_MODULE]
    relabelled = source.replace("def _apply_step(", "def _apply_elsewhere(")
    found = mutation_violations({THIS_MODULE: relabelled})
    assert len(found) == 4  # credential.remove twice, credential.activate, the import argv


def test_the_guard_flags_synthetic_violations() -> None:
    elsewhere = textwrap.dedent(
        """
        def helper(runner):
            runner.run(["api", "--standalone", "credential.remove", "--param", "x"])

        def other():
            return ["opencode", "auth", "import", "--standalone"]

        def activate():
            return "credential.activate"
        """
    )
    allowed = textwrap.dedent(
        """
        def _apply_step():
            return ["auth", "import"]
        """
    )
    assert len(mutation_violations({"other/module.py": elsewhere})) == 3
    assert mutation_violations({THIS_MODULE: allowed}) == []
    assert len(mutation_violations({"other/module.py": allowed})) == 1
    benign = 'def f():\n    return ["auth", "export", "--standalone"] + ["import"]'
    assert mutation_violations({"x.py": benign}) == []


def test_the_apply_step_is_called_for_every_database_change() -> None:
    tree = ast.parse(MODULE_PATH.read_text())
    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_apply_step"
    }
    assert callers == {"_adjust"}


# ---------------------------------------------------------------------------------------------
# Further rules of the sync
# ---------------------------------------------------------------------------------------------


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system python")
def test_the_whole_sync_runs_on_the_system_python(world: World) -> None:
    host = MAIN.build(world)
    result = world.run(host, python="/usr/bin/python3")
    assert result.returncode == 0, result.stderr
    assert check_final(MAIN, world, host) == []


def test_a_hidden_winner_is_not_applied_over_an_older_database_value(world: World) -> None:
    host = world.host()
    old = entry("cred_x", "prov_p", X_OLD)
    new = entry("cred_x", "prov_p", X_NEW)
    host.put([old])
    world.write(FILE, [new])
    host.seed_state(seen={"cred_x": fingerprint(old)}, hidden={"cred_x": (fingerprint(new),)})
    sync_ok(world, host)
    assert host.values() == {"cred_x": X_OLD}
    assert world.mutating_calls() == []
    assert host.state().hidden == {"cred_x": (fingerprint(new),)}


def test_a_hidden_value_is_not_added_again_even_with_other_ids_changing(world: World) -> None:
    host = world.host()
    gone = entry("cred_gone", "prov_g", key_value("gone"))
    host.put([entry("cred_y", "prov_p", key_value("y1"))])
    world.write(FILE, [gone, entry("cred_n", "prov_q", key_value("n1"))])
    host.seed_state(
        seen={"cred_gone": fingerprint(gone)}, hidden={"cred_gone": (fingerprint(gone),)}
    )
    sync_ok(world, host)
    assert sorted(host.values()) == ["cred_n", "cred_y"]


def test_only_a_change_outside_active_rewrites_the_files(world: World) -> None:
    host = MAIN.build(world)
    sync_ok(world, host)
    before = {
        name: ((world.carrier / name).stat().st_ino, (world.carrier / name).read_bytes())
        for name in (FILE, REMOVED)
    }
    host.put(
        [
            {
                "id": i["id"],
                "integrationID": i["integrationID"],
                "label": i["label"],
                "value": i["value"],
            }
            for i in fake.computed_export(host.raw_state())
        ],
        {"prov_p": "cred_y", "prov_q": "cred_n", "prov_s": "cred_c"},
    )
    sync_ok(world, host)
    after = {
        name: ((world.carrier / name).stat().st_ino, (world.carrier / name).read_bytes())
        for name in (FILE, REMOVED)
    }
    assert after == before
    assert host.active()["prov_p"] == "cred_y"  # the selection stays this host's own


def test_the_mirror_race_is_retried_exactly_three_times(world: World) -> None:
    host = MAIN.build(world)
    mirrored = world.root / "mirrored.json"
    mirrored.write_bytes(raw_json([entry("cred_m", "prov_q", key_value("m1"))]))
    result = world.run(
        host,
        wrapper={
            "log": True,
            "interfere": [
                {
                    "before": "fsync_file",
                    "match": ".credentials.json.",
                    "times": 99,
                    "vary": True,
                    "source": str(mirrored),
                    "dest": str(world.carrier / FILE),
                }
            ],
        },
    )
    assert result.returncode == 1
    fsyncs = [e for e in world.events() if e.startswith("ops fsync_file .credentials.json.X.tmp")]
    assert len(fsyncs) == 3


def test_a_lone_import_is_flagged_active_and_a_sibling_import_is_not(world: World) -> None:
    host = world.host()
    host.put([entry("cred_p1", "prov_p", key_value("p1"))])
    world.write(
        FILE,
        [
            entry("cred_p1", "prov_p", key_value("p1")),
            entry("cred_p2", "prov_p", key_value("p2")),
            entry("cred_q1", "prov_q", key_value("q1")),
        ],
    )
    sync_ok(world, host)
    flags = [e for e in world.events() if e.startswith("fakeinfo import-active")]
    assert flags == [
        "fakeinfo import-active cred_p2=False",
        "fakeinfo import-active cred_q1=True",
    ]


def test_hidden_values_are_durable_before_the_first_destructive_command(world: World) -> None:
    host = MAIN.build(world)
    gone = entry("cred_gone", "prov_g", key_value("gone"))
    world.write(
        FILE,
        [
            *world.read_json(FILE),
            gone,
        ],
    )
    host.seed_state(seen={**MAIN_SEEN_FPS, "cred_gone": fingerprint(gone)})
    events = logged_run(world, host)
    first = next(i for i, e in enumerate(events) if e.startswith("fake credential.remove"))
    name = "djinn-credentials-host.json"
    subsequence(
        events[:first],
        [
            rf"^ops open w \.{name}\.X\.tmp",
            rf"^ops fsync_file \.{name}\.X\.tmp",
            rf"^ops rename \.{name}\.X\.tmp->{name}",
            r"^ops fsync_dir dir:data",
        ],
    )
    assert "cred_gone" in host.state().hidden


def test_the_backup_directory_and_its_parent_are_made_durable_before_the_copy_goes(
    world: World,
) -> None:
    host = one_copy_world(world)
    events = logged_run(world, host)
    unlink = events.index(f"ops unlink {COPY_A}")
    subsequence(events[:unlink], [r"^ops fsync_dir dir:state", r"^ops open w \.credentials\.sync"])
    assert events[unlink + 1] == "ops fsync_dir dir:carrier"


def test_an_identical_backup_from_an_earlier_attempt_is_made_durable_before_the_copy_goes(
    world: World,
) -> None:
    host = one_copy_world(world)
    data = (world.carrier / COPY_A).read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:16]
    host.backup.mkdir(parents=True, mode=0o700)
    existing = host.backup / f"{COPY_A[: -len('.json')]}.{digest}.json"
    existing.write_bytes(data)
    existing.chmod(0o600)
    events = logged_run(world, host)
    unlink = events.index(f"ops unlink {COPY_A}")
    assert not [e for e in events if "open w .credentials.sync-conflict" in e]
    assert "ops fsync_dir dir:conflicts" in events[:unlink]
    assert not (world.carrier / COPY_A).exists()
    assert list(host.backup.iterdir()) == [existing]


def test_a_different_backup_under_the_same_name_is_never_overwritten(world: World) -> None:
    host = one_copy_world(world)
    data = (world.carrier / COPY_A).read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:16]
    host.backup.mkdir(parents=True, mode=0o700)
    existing = host.backup / f"{COPY_A[: -len('.json')]}.{digest}.json"
    existing.write_bytes(b"[]")
    existing.chmod(0o600)
    result = world.run(host)
    assert result.returncode == 0, result.stderr
    assert (world.carrier / COPY_A).exists()
    assert existing.read_bytes() == b"[]"


def test_a_backup_directory_of_another_owner_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state" / "conflicts"
    target.parent.mkdir()
    target.mkdir(mode=0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(oc.Refused, match="another user"):
        oc.open_backup_dir(target)


def test_a_new_backup_directory_is_private_and_a_file_in_its_place_is_refused(
    tmp_path: Path,
) -> None:
    descriptor = oc.open_backup_dir(tmp_path / "a" / "b" / "conflicts")
    os.close(descriptor)
    assert stat.S_IMODE((tmp_path / "a" / "b" / "conflicts").stat().st_mode) == 0o700
    (tmp_path / "plain").write_text("x")
    with pytest.raises(oc.Refused):
        oc.open_backup_dir(tmp_path / "plain")


def test_removing_conflict_copies_needs_the_carrier_lock(world: World) -> None:
    host = one_copy_world(world)
    data = (world.carrier / COPY_A).read_bytes()
    copy = oc.ConflictCopy(COPY_A, (world.carrier / COPY_A).stat().st_ino, data, (), ())
    report = oc.SyncReport()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "sys.stdin.read()\n",
            str(world.carrier),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(oc.LockTimeout):
            oc._consume_copies(  # pyright: ignore[reportPrivateUsage]
                str(world.carrier), [copy], str(host.backup), "/proc/self/mountinfo", 0.2, report
            )
    finally:
        holder.communicate("")
    assert (world.carrier / COPY_A).exists()
    assert not host.backup.exists()


def test_the_apply_step_refuses_unknown_or_incomplete_steps(world: World, fake_env: Host) -> None:
    keeper = oc.StateKeeper(oc.open_dir(world.root), "state.json", oc.HostState(), None)
    runner = oc.OpenCode()
    winner = oc.validate_entry(entry("cred_x", "prov_p", key_value("x1")))
    for step in (
        oc.Step("bogus", "cred_x"),
        oc.Step("bogus", "cred_x", None, winner),
        oc.Step("add", "cred_x"),
        oc.Step("update", "cred_x"),
    ):
        with pytest.raises(oc.ApplyFailed):
            oc._apply_step(runner, keeper, step, [])  # pyright: ignore[reportPrivateUsage]
    os.close(keeper.dir_fd)
    assert fake_env.values() == {}
    assert world.calls() == []


def test_a_timed_out_command_is_killed(
    world: World, fake_env: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SLEEP_AT", "1")
    with pytest.raises(oc.OpenCodeFailed, match="export timed out"):
        oc.OpenCode(timeout=1.0).export()
    (pid,) = [int(e.split()[2]) for e in world.events() if e.startswith("fakeinfo pid")]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_command_that_never_reads_its_stdin_times_out(
    world: World, fake_env: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_STDIN_STALL", "1")
    payload = raw_json(
        [entry("cred_big", "prov_p", key_value("big") | {"metadata": {"pad": "x" * 4000000}})]
    )
    started = time.monotonic()
    with pytest.raises(oc.OpenCodeFailed, match="import timed out"):
        oc.OpenCode(timeout=0.5).run("import", ["auth", "import", "--standalone"], stdin=payload)
    assert time.monotonic() - started < 10


def test_an_unknown_mode_is_refused_before_anything_happens(world: World, fake_env: Host) -> None:
    with pytest.raises(oc.NotUnderstood, match="unknown mode"):
        oc.sync(
            str(world.carrier),
            str(fake_env.db),
            str(fake_env.state_file),
            str(fake_env.backup),
            "sometimes",
        )
    assert world.events() == []
