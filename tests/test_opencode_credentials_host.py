"""Host APIs of the OpenCode credentials module: seed, union-only return, audit sessions,
recovery, restore parking, tombstones and doctor findings.

Docker is never touched: the container checks and the export are injected fakes. Every dummy
credential value is generated at test runtime (never a literal) and everything is written below
``tmp_path`` (under /tmp). Expectations are literals, not recomputed by the code under test.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from djinn_in_a_box.core import opencode_credentials as oc

MODULE_PATH = Path(oc.__file__)
SALT = os.urandom(8).hex()
FILE = "credentials.json"
REMOVED = "credentials.removed.json"
COPY_A = "credentials.sync-conflict-20260101-120000-ABCDEFG.json"
COPY_REMOVED = "credentials.removed.sync-conflict-20260103-140000-QRSTUVW.json"


# ---------------------------------------------------------------------------------------------
# Values and the world
# ---------------------------------------------------------------------------------------------


def token(name: str) -> str:
    """A runtime-generated dummy value; differs per run and per name."""
    return hashlib.sha256(f"{SALT}:{name}".encode()).hexdigest()[:16]


def entry(
    identity: str, integration: str = "prov_a", *, active: bool = False, expires: int | None = None
) -> dict[str, Any]:
    if expires is None:
        value: dict[str, Any] = {"type": "key", "key": token(identity)}
    else:
        value = {
            "type": "oauth",
            "methodID": "m1",
            "refresh": token(identity + "r"),
            "access": token(identity + str(expires)),
            "expires": expires,
        }
    return {
        "id": identity,
        "integrationID": integration,
        "label": "label " + identity,
        "active": active,
        "value": value,
    }


def raw_json(document: object) -> bytes:
    return (json.dumps(document, indent=2) + "\n").encode()


class World:
    """A temporary home, a carrier directory and the injected helpers."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home"
        self.home.mkdir(mode=0o700)
        self.carrier = base / "carrier"
        self.carrier.mkdir(mode=0o700)
        self.root = self.home / ".local" / "state" / "djinn" / "opencode-audit"
        self.policy = oc.RootPolicy(home=str(self.home))

    def put(self, name: str, document: object | bytes) -> None:
        data = document if isinstance(document, bytes) else raw_json(document)
        target = self.carrier / name
        target.write_bytes(data)
        target.chmod(0o600)

    def ids(self, name: str = FILE) -> list[str]:
        path = self.carrier / name
        if not path.exists():
            return []
        loaded = json.loads(path.read_text())
        return sorted(item["id"] if isinstance(item, dict) else item for item in loaded)

    def snapshot(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(self.carrier.iterdir()) if p.is_file()}

    def entries(self) -> dict[str, dict[str, Any]]:
        path = self.carrier / FILE
        return {item["id"]: item for item in json.loads(path.read_text())} if path.exists() else {}

    def session(self) -> oc.Session:
        return oc.create_audit_session(self.root, policy=self.policy)

    def finished_session(
        self, *, final: object | None = None, database: bool = False, data_junk: bool = False
    ) -> str:
        """A closed session as left behind by a finished or killed audit."""
        with self.session() as session:
            identity = session.id
        base = self.root / identity
        if final is not None:
            target = base / "io" / "final.json"
            target.write_bytes(final if isinstance(final, bytes) else raw_json(final))
            target.chmod(0o600)
        if database:
            (base / "data" / "opencode.db").write_bytes(b"db-" + token(identity).encode())
        if data_junk:
            (base / "data" / "opencode.log").write_bytes(b"log")
        return identity

    def recover(
        self,
        export_db: Callable[[str], bytes] | None = None,
        container_exists: Callable[[str], bool] | None = None,
        own_session: oc.Session | None = None,
        **extra: Any,
    ) -> oc.RecoveryReport:
        def refuse(_: str) -> bytes:
            raise AssertionError("export_db must not be called")

        return oc.recover_audit_sessions(
            self.root,
            self.carrier,
            export_db or refuse,
            container_exists or (lambda _: False),
            own_session,
            policy=self.policy,
            **extra,
        )

    def sessions(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir()) if self.root.exists() else []


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def locked(world: World) -> Any:
    return oc.carrier_lock(world.carrier)


# ---------------------------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------------------------


def test_seed_is_the_join_of_file_and_copies_minus_all_tombstones(world: World) -> None:
    world.put(FILE, [entry("cred_a"), entry("cred_b"), entry("cred_dead")])
    world.put(COPY_A, [entry("cred_c"), entry("cred_dead2")])
    world.put(REMOVED, ["cred_dead"])
    world.put(COPY_REMOVED, ["cred_dead2"])
    with locked(world) as lease:
        seed = oc.materialize_seed(world.carrier, lease)
    assert [item["id"] for item in json.loads(seed)] == ["cred_a", "cred_b", "cred_c"]


def test_seed_omits_a_targeted_tombstone_without_any_dev_sync(world: World) -> None:
    world.put(FILE, [entry("cred_a"), entry("cred_b")])
    with locked(world) as lease:
        oc.tombstone_ids(world.carrier, ["cred_b"], lease)
        seed = oc.materialize_seed(world.carrier, lease)
    assert [item["id"] for item in json.loads(seed)] == ["cred_a"]
    assert world.ids() == ["cred_a", "cred_b"]


def test_seed_keeps_the_selection_of_the_file_and_the_larger_oauth_expiry(world: World) -> None:
    world.put(FILE, [entry("cred_a", active=True, expires=100), entry("cred_b")])
    world.put(COPY_A, [entry("cred_a", expires=900)])
    with locked(world) as lease:
        seed = json.loads(oc.materialize_seed(world.carrier, lease))
    assert [(item["id"], item["active"]) for item in seed] == [("cred_a", True), ("cred_b", False)]
    assert seed[0]["value"]["expires"] == 900


def test_seed_without_any_carrier_file_is_an_empty_array(world: World) -> None:
    with locked(world) as lease:
        assert json.loads(oc.materialize_seed(world.carrier, lease)) == []


@pytest.mark.parametrize("name", [FILE, REMOVED])
def test_seed_raises_not_understood_for_an_invalid_carrier_file(world: World, name: str) -> None:
    world.put(FILE, [entry("cred_a")])
    world.put(name, b"{not json")
    with locked(world) as lease, pytest.raises(oc.NotUnderstood):
        oc.materialize_seed(world.carrier, lease)


def test_a_lease_of_another_directory_or_a_released_lease_is_refused(
    world: World, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    with oc.carrier_lock(other) as foreign, pytest.raises(oc.CredentialsError):
        oc.materialize_seed(world.carrier, foreign)
    with locked(world) as lease:
        pass
    with pytest.raises(oc.CredentialsError):
        oc.materialize_seed(world.carrier, lease)


# ---------------------------------------------------------------------------------------------
# merge_back
# ---------------------------------------------------------------------------------------------


def export_of(*items: dict[str, Any]) -> bytes:
    return raw_json(list(items))


def test_merge_back_unions_the_export_with_the_carrier(world: World) -> None:
    world.put(FILE, [entry("cred_a", active=True), entry("cred_b", expires=100)])
    final = export_of(entry("cred_b", expires=500), entry("cred_c", active=True), entry("cred_a"))
    with locked(world) as lease:
        summary = oc.merge_back(world.carrier, final, lease)
    assert summary == oc.MergeBackSummary(1, 1, 0, True, False)
    stored = world.entries()
    assert sorted(stored) == ["cred_a", "cred_b", "cred_c"]
    assert stored["cred_b"]["value"]["expires"] == 500
    # the file's own selection wins; the new integration takes the export's
    assert stored["cred_a"]["active"] is True


def test_merge_back_never_creates_a_tombstone_and_respects_existing_ones(world: World) -> None:
    world.put(FILE, [entry("cred_a")])
    final = export_of(entry("cred_new"), entry("cred_a", "prov_a"))
    with locked(world) as lease:
        summary = oc.merge_back(world.carrier, final, lease)
    assert not (world.carrier / REMOVED).exists()
    assert summary.wrote_tombstones is False
    world.put(REMOVED, ["cred_dead"])
    before = (world.carrier / REMOVED).read_bytes()
    with locked(world) as lease:
        summary = oc.merge_back(
            world.carrier, export_of(entry("cred_dead"), entry("cred_z", "prov_z")), lease
        )
    assert summary.tombstoned == 1
    assert world.ids() == ["cred_a", "cred_new", "cred_z"]
    assert (world.carrier / REMOVED).read_bytes() == before


def test_merge_back_leaves_unchanged_files_untouched_and_is_idempotent(world: World) -> None:
    world.put(FILE, [entry("cred_a", active=True)])
    before = world.snapshot()
    inode = (world.carrier / FILE).stat().st_ino
    with locked(world) as lease:
        summary = oc.merge_back(world.carrier, export_of(entry("cred_a", active=False)), lease)
    assert summary == oc.MergeBackSummary(0, 0, 0, False, False)
    assert world.snapshot() == before
    assert (world.carrier / FILE).stat().st_ino == inode


def test_merge_back_retries_when_a_non_cooperating_writer_changes_the_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.put(FILE, [entry("cred_a")])
    calls = {"n": 0}
    real = oc.ops.fsync_file

    def interfere(fd: int) -> None:
        calls["n"] += 1
        real(fd)
        if calls["n"] == 1:
            world.put(FILE, [entry("cred_a"), entry("cred_mirror", "prov_m")])

    monkeypatch.setattr(oc.ops, "fsync_file", interfere)
    with locked(world) as lease:
        oc.merge_back(world.carrier, export_of(entry("cred_b", "prov_b")), lease)
    assert world.ids() == ["cred_a", "cred_b", "cred_mirror"]


def test_merge_back_gives_up_after_exactly_three_attempts(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.put(FILE, [entry("cred_a")])
    calls = {"n": 0}
    real = oc.ops.fsync_file

    def interfere(fd: int) -> None:
        calls["n"] += 1
        real(fd)
        world.put(FILE, [entry("cred_a"), entry(f"cred_m{calls['n']}", "prov_m")])

    monkeypatch.setattr(oc.ops, "fsync_file", interfere)
    with locked(world) as lease, pytest.raises(oc.ChangedOnDisk):
        oc.merge_back(world.carrier, export_of(entry("cred_b", "prov_b")), lease)
    assert calls["n"] == 3


def test_merge_back_rejects_an_invalid_export_or_carrier_and_changes_nothing(
    world: World,
) -> None:
    world.put(FILE, [entry("cred_a")])
    before = world.snapshot()
    with locked(world) as lease:
        for bad in (b"{", raw_json([{"id": "x"}]), raw_json([entry("cred_a"), entry("cred_a")])):
            with pytest.raises(oc.NotUnderstood):
                oc.merge_back(world.carrier, bad, lease)
    assert world.snapshot() == before
    world.put(REMOVED, b"[1]")
    before = world.snapshot()
    with locked(world) as lease, pytest.raises(oc.NotUnderstood):
        oc.merge_back(world.carrier, export_of(entry("cred_b", "prov_b")), lease)
    assert world.snapshot() == before


def test_writers_remove_their_own_stale_temp_files_under_the_lock(world: World) -> None:
    world.put(FILE, [entry("cred_a")])
    old = world.carrier / f".{FILE}.{'a' * 16}.tmp"
    fresh = world.carrier / f".{REMOVED}.{'b' * 16}.tmp"
    foreign = world.carrier / f".{FILE}.zz.tmp"
    for path in (old, fresh, foreign):
        path.write_bytes(b"x")
    past = time.time() - 3600
    os.utime(old, (past, past))
    os.utime(foreign, (past, past))
    with locked(world) as lease:
        oc.merge_back(world.carrier, export_of(entry("cred_b", "prov_b")), lease)
    assert not old.exists() and fresh.exists() and foreign.exists()
    os.utime(fresh, (past, past))
    with locked(world) as lease:
        oc.tombstone_ids(world.carrier, ["cred_a"], lease)
    assert not fresh.exists()


# ---------------------------------------------------------------------------------------------
# Audit session creation and path rules
# ---------------------------------------------------------------------------------------------


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_a_session_has_private_layout_random_id_and_container_name(world: World) -> None:
    with world.session() as first, world.session() as second:
        assert re.fullmatch(r"[0-9a-f]{32}", first.id)
        assert first.id != second.id
        assert first.container_name == "djinn-assistant-" + first.id
        assert Path(first.path) == world.root / first.id
        assert sorted(p.name for p in world.root.iterdir()) == sorted([first.id, second.id])
        for path in (
            world.root,
            world.root.parent,
            Path(first.path),
            Path(first.io_path),
            Path(first.data_path),
        ):
            assert mode_of(path) == 0o700
            assert path.stat().st_uid == os.geteuid()


def test_the_lease_survives_the_rename_and_ends_with_close(world: World) -> None:
    session = world.session()
    probe = os.open(session.path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session.close()
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(probe)
    session.close()


def test_a_recovery_in_between_ignores_the_unfinished_session(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = oc.ops.rename

    def spy(source: str, target: str, dir_fd: int) -> None:
        if source.startswith(".creating-") and "report" not in seen:
            seen["names"] = world.sessions()
            seen["report"] = world.recover()
        real(source, target, dir_fd)

    monkeypatch.setattr(oc.ops, "rename", spy)
    with world.session() as session:
        pass
    assert seen["names"] == [".creating-" + session.id]
    assert seen["report"].removed == []
    assert seen["report"].kept == [(session.id, "in use")]
    assert world.sessions() == [session.id]


def test_a_session_is_created_under_a_creating_name_first(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    renames: list[tuple[str, str]] = []
    real = oc.ops.rename

    def record(source: str, target: str, dir_fd: int) -> None:
        renames.append((source, target))
        real(source, target, dir_fd)

    monkeypatch.setattr(oc.ops, "rename", record)
    with world.session() as session:
        pass
    assert renames == [(".creating-" + session.id, session.id)]


def test_write_seed_is_0600_fsynced_before_it_returns(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    for name in ("fsync_file", "fsync_dir", "rename"):
        real = getattr(oc.ops, name)

        def wrap(*args: Any, _real: Any = real, _name: str = name) -> Any:
            events.append(_name)
            return _real(*args)

        monkeypatch.setattr(oc.ops, name, wrap)
    with world.session() as session:
        events.clear()
        oc.write_seed(session, b"[]\n")
        target = Path(session.io_path) / "seed.json"
        assert target.read_bytes() == b"[]\n"
        assert mode_of(target) == 0o600
        oc.write_seed(session, b"[ ]\n")
        assert target.read_bytes() == b"[ ]\n"
    assert events[:3] == ["fsync_file", "rename", "fsync_dir"]


def test_write_seed_refuses_an_oversized_seed(world: World) -> None:
    with world.session() as session, pytest.raises(oc.NotUnderstood):
        oc.write_seed(session, b" " * (oc.FILE_LIMIT + 1))


def test_a_symlinked_ancestor_is_refused(world: World, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (world.home / ".local").symlink_to(elsewhere)
    with pytest.raises(oc.Refused):
        world.session()
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_root_is_refused(world: World, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    world.root.parent.mkdir(parents=True, mode=0o700)
    world.root.symlink_to(elsewhere)
    with pytest.raises(oc.Refused):
        world.session()
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("bits", [0o020, 0o002])
def test_a_group_or_world_writable_ancestor_is_refused(world: World, bits: int) -> None:
    ancestor = world.home / ".local"
    ancestor.mkdir(mode=0o700)
    ancestor.chmod(0o700 | bits)
    with pytest.raises(oc.Refused):
        world.session()
    assert not world.root.exists()


def test_a_writable_home_directory_is_refused(world: World) -> None:
    world.home.chmod(0o770)
    with pytest.raises(oc.Refused):
        world.session()


def test_a_foreign_owner_is_refused(world: World) -> None:
    foreign = oc.RootPolicy(home=str(world.home), euid=os.geteuid() + 1)
    with pytest.raises(oc.Refused):
        oc.create_audit_session(world.root, policy=foreign)
    with world.session():
        pass
    with pytest.raises(oc.Refused):
        oc.create_audit_session(world.root, policy=foreign)


def test_a_root_outside_the_home_directory_is_refused(world: World, tmp_path: Path) -> None:
    with pytest.raises(oc.Refused):
        oc.create_audit_session(tmp_path / "outside", policy=world.policy)
    with pytest.raises(oc.Refused):
        oc.create_audit_session(world.home, policy=world.policy)


def test_the_directory_is_fsynced_before_the_session_gets_its_final_name(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_rename, real_fsync = oc.ops.rename, oc.ops.fsync_dir
    monkeypatch.setattr(oc.ops, "rename", lambda *a: (events.append("rename"), real_rename(*a))[1])
    monkeypatch.setattr(oc.ops, "fsync_dir", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    with world.session():
        events.clear()
        with world.session():
            pass
    assert events == ["fsync", "rename", "fsync"]


def test_a_session_that_vanishes_under_the_lease_is_not_recovered(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = world.finished_session(final=[entry("cred_x", "prov_x")])
    real = os.open

    def vanish(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = real(path, flags, *args, **kwargs)
        if path == identity and "dir_fd" in kwargs:
            for child in ("io", "data"):
                for leaf in (world.root / identity / child).iterdir():
                    leaf.unlink()
                (world.root / identity / child).rmdir()
            (world.root / identity).rmdir()
        return descriptor

    monkeypatch.setattr(oc.os, "open", vanish)
    report = world.recover()
    monkeypatch.undo()
    assert report == oc.RecoveryReport()
    assert world.ids() == []


def test_a_failing_removal_keeps_the_merge_and_the_session_is_swept_next_time(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = world.finished_session(final=[entry("cred_x", "prov_x")])

    def broken(*_: Any) -> None:
        raise OSError("busy")

    monkeypatch.setattr(oc, "_remove_tree", broken)
    report = world.recover()
    assert report.recovered == [] and report.kept == [(identity, "removal failed")]
    monkeypatch.undo()
    assert world.ids() == ["cred_x"]
    assert world.sessions() == [".deleting-" + identity]
    assert world.recover().removed == [identity]
    assert world.sessions() == []


@pytest.mark.parametrize("function", ["ids", "all"])
def test_an_invalid_carrier_stops_before_stale_temp_files_are_removed(
    world: World, function: str
) -> None:
    world.put(REMOVED, b"{broken")
    stale = world.carrier / f".{FILE}.{'e' * 16}.tmp"
    stale.write_bytes(b"x")
    past = time.time() - 3600
    os.utime(stale, (past, past))
    with locked(world) as lease, pytest.raises(oc.NotUnderstood):
        if function == "ids":
            oc.tombstone_ids(world.carrier, ["cred_a"], lease)
        else:
            oc.tombstone_all(world.carrier, [], lease)
    assert stale.exists()


def test_the_ancestor_and_private_directory_rules_check_owner_and_mode(world: World) -> None:
    policy = oc.RootPolicy(home=str(world.home), euid=1000)

    def status(uid: int, mode: int) -> os.stat_result:
        return os.stat_result((stat.S_IFDIR | mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))

    oc._check_ancestor(status(1000, 0o755), policy)
    oc._check_ancestor(status(0, 0o755), policy)
    for uid, mode in ((1001, 0o755), (1000, 0o775), (1000, 0o757)):
        with pytest.raises(oc.Refused):
            oc._check_ancestor(status(uid, mode), policy)
    descriptor = os.open(world.home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(oc.Refused):
            oc._check_private(descriptor, status(0, 0o700), policy)
        with pytest.raises(oc.Refused):
            oc._check_private(descriptor, status(1001, 0o700), policy)
    finally:
        os.close(descriptor)


def test_removing_a_session_never_follows_a_symlink(world: World, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_bytes(b"x")
    identity = world.finished_session(final=[], database=True)
    (world.root / identity / "data" / "link").symlink_to(outside)
    (world.root / identity / "data" / "file-link").symlink_to(outside / "precious")
    assert world.recover().recovered == [identity]
    assert (outside / "precious").read_bytes() == b"x"
    assert world.sessions() == []


def test_loose_private_directories_are_tightened_to_0700(world: World) -> None:
    world.root.mkdir(parents=True, mode=0o700)
    world.root.parent.chmod(0o750)
    world.root.chmod(0o755)
    with world.session() as session:
        assert mode_of(world.root) == 0o700
        assert mode_of(world.root.parent) == 0o700
        assert mode_of(Path(session.path)) == 0o700


# ---------------------------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------------------------


def test_a_valid_final_export_is_adopted_and_the_session_deleted(world: World) -> None:
    world.put(FILE, [entry("cred_a")])
    identity = world.finished_session(final=[entry("cred_new", "prov_n")], database=True)
    report = world.recover()
    assert report.recovered == [identity] and report.kept == [] and report.removed == []
    assert world.ids() == ["cred_a", "cred_new"]
    assert world.sessions() == []


def test_an_empty_valid_final_export_is_authoritative(world: World) -> None:
    identity = world.finished_session(final=[], database=True)
    report = world.recover()
    assert report.recovered == [identity]
    assert world.sessions() == []


def test_without_a_final_export_the_database_is_exported_and_merged(world: World) -> None:
    identity = world.finished_session(database=True)
    calls: list[str] = []

    def export_db(path: str) -> bytes:
        calls.append(path)
        return export_of(entry("cred_db", "prov_d"))

    report = world.recover(export_db)
    assert report.recovered == [identity]
    assert calls == [str(world.root / identity / "data")]
    assert world.ids() == ["cred_db"]
    assert world.sessions() == []


@pytest.mark.parametrize("failure", ["raises", "invalid"])
def test_a_failing_export_keeps_the_session_and_changes_nothing(world: World, failure: str) -> None:
    world.put(FILE, [entry("cred_a")])
    before = world.snapshot()
    identity = world.finished_session(database=True)

    def export_db(_: str) -> bytes:
        if failure == "raises":
            raise RuntimeError(token("secret in message"))
        return b"{not an export"

    report = world.recover(export_db)
    assert report.kept == [(identity, "export failed")] and report.recovered == []
    assert world.sessions() == [identity]
    assert world.snapshot() == before
    assert (world.root / identity / "data" / "opencode.db").exists()


def test_a_running_container_keeps_the_session_unless_it_is_stopped(world: World) -> None:
    identity = world.finished_session(final=[entry("cred_x", "prov_x")])
    exported: list[str] = []
    report = world.recover(container_exists=lambda name: name == "djinn-assistant-" + identity)
    assert report.kept == [(identity, "container exists")]
    assert world.sessions() == [identity] and world.ids() == []
    # stopped: recovered; stop that does not help: kept
    running = {"yes": True}

    def stop(_: str) -> None:
        running["yes"] = False
        exported.append("stopped")

    report = world.recover(container_exists=lambda _: running["yes"], stop_container=stop)
    assert report.recovered == [identity] and exported == ["stopped"]
    again = world.finished_session(final=[entry("cred_y", "prov_y")])
    report = world.recover(container_exists=lambda _: True, stop_container=lambda _: None)
    assert report.kept == [(again, "container exists")]


def test_a_failing_container_check_keeps_the_session(world: World) -> None:
    identity = world.finished_session(final=[entry("cred_x", "prov_x")])

    def broken(_: str) -> bool:
        raise OSError("daemon gone")

    assert world.recover(container_exists=broken).kept == [(identity, "container state unknown")]
    assert world.sessions() == [identity]


def test_the_caller_s_own_session_is_recovered_with_its_held_lease(world: World) -> None:
    other = world.finished_session(final=[entry("cred_other", "prov_o")])
    with world.session() as own:
        (Path(own.io_path) / "final.json").write_bytes(export_of(entry("cred_own", "prov_w")))
        (Path(own.io_path) / "final.json").chmod(0o600)
        report = world.recover(own_session=own)
        assert sorted(report.recovered) == sorted([other, own.id])
    assert world.ids() == ["cred_other", "cred_own"]
    assert world.sessions() == []


def test_a_held_lease_is_skipped_without_touching_anything(world: World) -> None:
    with world.session() as running:
        (Path(running.io_path) / "final.json").write_bytes(export_of(entry("cred_x", "prov_x")))
        (Path(running.io_path) / "final.json").chmod(0o600)
        (Path(running.data_path) / "opencode.db").write_bytes(b"db")
        report = world.recover()
        assert report.kept == [(running.id, "in use")]
        assert report.recovered == [] and report.removed == []
        assert world.ids() == []
        assert (Path(running.io_path) / "final.json").exists()


@pytest.mark.parametrize("layout", ["empty", "absent"])
def test_an_empty_session_is_removed_without_a_merge(world: World, layout: str) -> None:
    world.put(FILE, [entry("cred_a")])
    before = world.snapshot()
    identity = world.finished_session()
    if layout == "absent":
        (world.root / identity / "data").rmdir()
    report = world.recover()
    assert report.removed == [identity] and report.recovered == []
    assert world.sessions() == [] and world.snapshot() == before


def test_unclear_sessions_are_kept(world: World, tmp_path: Path) -> None:
    junk = world.finished_session(data_junk=True)
    bad_final = world.finished_session(final=b"{broken")
    symlinked = world.finished_session()
    (world.root / symlinked / "data").rmdir()
    (world.root / symlinked / "data").symlink_to(tmp_path)
    (tmp_path / "opencode.db").write_bytes(b"db")
    running = world.finished_session()
    report = world.recover(container_exists=lambda name: name.endswith(running))
    assert sorted(report.kept) == sorted(
        [
            (junk, "data holds no database"),
            (bad_final, "final export invalid"),
            (symlinked, "session layout refused"),
            (running, "container exists"),
        ]
    )
    assert report.removed == [] and world.ids() == []
    assert len(world.sessions()) == 4


def test_a_session_with_a_final_export_is_never_deleted_unmerged(world: World) -> None:
    identity = world.finished_session(final=b"{broken", database=False)
    (world.root / identity / "data").rmdir()
    report = world.recover()
    assert report.kept == [(identity, "final export invalid")]
    assert world.sessions() == [identity]


def test_unfinished_creating_directories_are_collected_only_when_free_and_empty(
    world: World,
) -> None:
    world.root.mkdir(parents=True, mode=0o700)
    free = world.root / (".creating-" + "1" * 32)
    free.mkdir(mode=0o700)
    (free / "io").mkdir()
    (free / "data").mkdir()
    full = world.root / (".creating-" + "2" * 32)
    (full / "data").mkdir(parents=True, mode=0o700)
    (full / "data" / "opencode.db").write_bytes(b"x")
    held = world.root / (".creating-" + "3" * 32)
    held.mkdir(mode=0o700)
    holder = os.open(held, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        report = world.recover()
    finally:
        os.close(holder)
    assert report.removed == ["1" * 32]
    assert sorted(report.kept) == [
        ("2" * 32, "unfinished session holds data"),
        ("3" * 32, "in use"),
    ]
    assert sorted(p.name for p in world.root.iterdir()) == sorted(
        [".creating-" + "2" * 32, ".creating-" + "3" * 32]
    )


def test_a_half_deleted_session_is_swept(world: World) -> None:
    leftover = world.root / (".deleting-" + "4" * 32)
    (leftover / "data").mkdir(parents=True, mode=0o700)
    (leftover / "data" / "opencode.db").write_bytes(b"x")
    report = world.recover()
    assert report.removed == ["4" * 32]
    assert world.sessions() == []


def test_names_that_are_not_sessions_are_left_alone(world: World) -> None:
    world.root.mkdir(parents=True, mode=0o700)
    (world.root / "notes.txt").write_bytes(b"x")
    (world.root / "deadbeef").mkdir()
    report = world.recover()
    assert report == oc.RecoveryReport()
    assert sorted(world.sessions()) == ["deadbeef", "notes.txt"]


def test_a_missing_root_recovers_nothing(world: World) -> None:
    assert world.recover() == oc.RecoveryReport()
    assert not world.root.exists()


def test_a_session_entry_that_is_not_a_directory_is_kept(world: World) -> None:
    world.root.mkdir(parents=True, mode=0o700)
    (world.root / ("5" * 32)).write_bytes(b"x")
    assert world.recover().kept == [("5" * 32, "not a directory")]


def test_an_invalid_carrier_keeps_every_session(world: World) -> None:
    world.put(FILE, b"{broken")
    identity = world.finished_session(final=[entry("cred_x", "prov_x")])
    before = world.snapshot()
    report = world.recover()
    assert report.kept == [(identity, "carrier not understood")]
    assert world.sessions() == [identity] and world.snapshot() == before


def test_a_busy_carrier_lock_keeps_the_sessions(world: World) -> None:
    identity = world.finished_session(final=[entry("cred_x", "prov_x")])
    holder = os.open(world.carrier, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        report = world.recover(lock_timeout=0.2)
    finally:
        os.close(holder)
    assert report.kept == [(identity, "carrier lock busy")]
    assert world.sessions() == [identity] and world.ids() == []
    assert world.recover().recovered == [identity]


def test_the_carrier_lock_is_taken_once_and_after_all_exports(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(3):
        world.finished_session(database=True)
        del index
    taken: list[str] = []
    real = oc.carrier_lock

    def counting(*args: Any, **kwargs: Any) -> Any:
        taken.append("lock")
        return real(*args, **kwargs)

    monkeypatch.setattr(oc, "carrier_lock", counting)
    free_during_export: list[bool] = []

    def export_db(path: str) -> bytes:
        probe = os.open(world.carrier, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            free_during_export.append(True)
        except BlockingIOError:
            free_during_export.append(False)
        finally:
            os.close(probe)
        return export_of(entry("cred_" + Path(path).parent.name[:6], "prov_" + path[-12:-5]))

    report = world.recover(export_db)
    assert len(report.recovered) == 3 and taken == ["lock"]
    assert free_during_export == [True, True, True]
    assert len(world.ids()) == 3


def test_a_session_is_deleted_only_after_the_publication_is_durable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = world.finished_session(final=[entry("cred_x", "prov_x")])
    events: list[str] = []
    carrier_inode = world.carrier.stat().st_ino
    real_rename, real_fsync = oc.ops.rename, oc.ops.fsync_dir

    def rename(source: str, target: str, dir_fd: int) -> None:
        events.append(f"rename {source}->{target}")
        real_rename(source, target, dir_fd)

    def fsync_dir(fd: int) -> None:
        events.append("fsync carrier" if os.fstat(fd).st_ino == carrier_inode else "fsync other")
        real_fsync(fd)

    monkeypatch.setattr(oc.ops, "rename", rename)
    monkeypatch.setattr(oc.ops, "fsync_dir", fsync_dir)
    world.recover()
    marked = events.index(f"rename {identity}->.deleting-{identity}")
    publish_done = max(i for i, e in enumerate(events[:marked]) if e == "fsync carrier")
    assert publish_done < marked


# --- processes: concurrency and SIGKILL -----------------------------------------------------

DRIVER_SOURCE = textwrap.dedent(
    """
    import importlib.util, json, os, signal, sys, time

    module_path, config_path = sys.argv[1:3]
    spec = importlib.util.spec_from_file_location("oc_under_test", module_path)
    oc = importlib.util.module_from_spec(spec)
    sys.modules["oc_under_test"] = oc
    spec.loader.exec_module(oc)
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    kill = config.get("kill")
    policy = oc.RootPolicy(home=config["home"])


    def note(line):
        descriptor = os.open(config["log"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.write(descriptor, (line + "\\n").encode())
        os.close(descriptor)


    def die():
        os.kill(os.getpid(), signal.SIGKILL)


    while config.get("gate") and not os.path.exists(config["gate"]):
        time.sleep(0.01)

    if config.get("mode") == "park":
        with open(config["export"], "rb") as handle:
            volume = handle.read()
        with oc.carrier_lock(config["carrier"]) as lease:
            identity = oc.park_for_restore(
                config["root"], config["carrier"], volume, lease, policy=policy
            )
        note("parked " + identity)
        if kill == "after-park":
            die()
        sys.exit(0)


    def export_db(path):
        note("export")
        time.sleep(config.get("wait", 0))
        if kill == "after-export":
            die()
        with open(config["export"], "rb") as handle:
            return handle.read()


    real_merge = oc.merge_back


    def merge_back(*args, **kwargs):
        note("merge")
        result = real_merge(*args, **kwargs)
        note("published")
        if kill == "after-publish":
            die()
        return result


    oc.merge_back = merge_back
    real_discard = oc._discard_session


    def discard(*args):
        if kill == "before-delete":
            die()
        return real_discard(*args)


    oc._discard_session = discard
    real_tree = oc._remove_tree


    def tree(*args):
        if kill == "mid-delete":
            die()
        return real_tree(*args)


    oc._remove_tree = tree
    real_rename = oc.ops.rename


    def rename(source, target, dir_fd):
        if kill == "before-publish-rename" and target == "credentials.json":
            die()
        return real_rename(source, target, dir_fd)


    oc.ops.rename = rename
    report = oc.recover_audit_sessions(
        config["root"], config["carrier"], export_db, lambda name: False, policy=policy
    )
    print(json.dumps(
        {"recovered": report.recovered, "kept": report.kept, "removed": report.removed}
    ))
    """
)


class Driver:
    def __init__(self, world: World, tmp_path: Path) -> None:
        self.world = world
        self.script = tmp_path / "driver.py"
        self.script.write_text(DRIVER_SOURCE)
        self.export = tmp_path / "export.json"
        self.log = tmp_path / "driver.log"
        self.count = 0

    def start(self, **extra: Any) -> subprocess.Popen[str]:
        self.count += 1
        config = self.script.with_name(f"config{self.count}.json")
        config.write_text(
            json.dumps(
                {
                    "home": str(self.world.home),
                    "root": str(self.world.root),
                    "carrier": str(self.world.carrier),
                    "export": str(self.export),
                    "log": str(self.log),
                    **extra,
                }
            )
        )
        return subprocess.Popen(
            [sys.executable, "-I", str(self.script), str(MODULE_PATH), str(config)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run(self, **extra: Any) -> tuple[int, str]:
        process = self.start(**extra)
        try:
            out, _ = process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        return process.returncode, out

    def lines(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def driver(world: World, tmp_path: Path) -> Driver:
    return Driver(world, tmp_path)


def test_two_recovering_processes_one_wins_with_a_single_merge(
    world: World, driver: Driver, tmp_path: Path
) -> None:
    world.put(FILE, [entry("cred_a")])
    identity = world.finished_session(database=True)
    driver.export.write_bytes(export_of(entry("cred_db", "prov_d")))
    gate = tmp_path / "go"
    first = driver.start(gate=str(gate), wait=1.0)
    second = driver.start(gate=str(gate), wait=1.0)
    gate.write_bytes(b"")
    outputs = [json.loads(p.communicate(timeout=60)[0]) for p in (first, second)]
    assert first.returncode == 0 and second.returncode == 0
    assert sorted(item["recovered"] for item in outputs) == [[], [identity]]
    assert [kept for item in outputs for kept in item["kept"]] in ([], [[identity, "in use"]])
    assert driver.lines().count("merge") == 1 and driver.lines().count("export") == 1
    assert world.ids() == ["cred_a", "cred_db"]
    assert world.sessions() == []


@pytest.mark.parametrize(
    "kill",
    ["after-export", "before-publish-rename", "after-publish", "before-delete", "mid-delete"],
)
def test_sigkill_at_each_step_is_completed_by_the_next_recovery(
    world: World, driver: Driver, kill: str
) -> None:
    world.put(FILE, [entry("cred_a")])
    identity = world.finished_session(database=True)
    driver.export.write_bytes(export_of(entry("cred_db", "prov_d")))
    code, _ = driver.run(kill=kill)
    assert code == -signal.SIGKILL
    published = kill in {"after-publish", "before-delete", "mid-delete"}
    assert world.ids() == (["cred_a", "cred_db"] if published else ["cred_a"])
    code, out = driver.run()
    assert code == 0
    assert world.ids() == ["cred_a", "cred_db"]
    assert world.sessions() == []
    assert json.loads(out)["kept"] == []
    after = world.snapshot()
    code, out = driver.run()
    assert code == 0 and json.loads(out) == {"recovered": [], "kept": [], "removed": []}
    assert world.snapshot() == after
    del identity


# ---------------------------------------------------------------------------------------------
# Restore parking
# ---------------------------------------------------------------------------------------------


def test_parking_writes_the_union_before_returning_and_releases_the_lease(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.put(FILE, [entry("cred_now", "prov_n", active=True), entry("cred_dead", "prov_d")])
    world.put(REMOVED, ["cred_dead"])
    volume = export_of(entry("cred_vol", "prov_v"), entry("cred_dead", "prov_d"))
    events: list[str] = []
    real_file, real_dir = oc.ops.fsync_file, oc.ops.fsync_dir
    monkeypatch.setattr(oc.ops, "fsync_file", lambda fd: (events.append("file"), real_file(fd)))
    monkeypatch.setattr(oc.ops, "fsync_dir", lambda fd: (events.append("dir"), real_dir(fd)))
    with locked(world) as lease:
        identity = oc.park_for_restore(
            world.root, world.carrier, volume, lease, policy=world.policy
        )
    assert events[-2:] == ["file", "dir"]
    assert re.fullmatch(r"[0-9a-f]{32}", identity)
    final = world.root / identity / "io" / "final.json"
    assert mode_of(final) == 0o600
    parked = json.loads(final.read_text())
    assert [(item["id"], item["active"]) for item in parked] == [
        ("cred_now", True),
        ("cred_vol", False),
    ]
    probe = os.open(world.root / identity, os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(probe)


def test_a_restore_keeps_logins_made_after_the_backup(world: World) -> None:
    world.put(FILE, [entry("cred_old", "prov_o"), entry("cred_new", "prov_n")])
    volume = export_of(entry("cred_vol", "prov_v"))
    with locked(world) as lease:
        oc.park_for_restore(world.root, world.carrier, volume, lease, policy=world.policy)
    # the restore: the carrier is replaced by the backup, the volume by the backup volume
    world.put(FILE, [entry("cred_old", "prov_o")])
    report = world.recover(export_db=lambda _: b"[]")
    assert len(report.recovered) == 1
    assert world.ids() == ["cred_new", "cred_old", "cred_vol"]
    assert world.sessions() == []


def test_a_kill_between_parking_and_the_restore_loses_nothing(world: World, driver: Driver) -> None:
    world.put(FILE, [entry("cred_old", "prov_o"), entry("cred_new", "prov_n")])
    driver.export.write_bytes(export_of(entry("cred_vol", "prov_v")))
    code, _ = driver.run(mode="park", kill="after-park")
    assert code == -signal.SIGKILL
    world.put(FILE, [entry("cred_old", "prov_o")])
    code, out = driver.run()
    assert code == 0 and len(json.loads(out)["recovered"]) == 1
    assert world.ids() == ["cred_new", "cred_old", "cred_vol"]


def test_parking_an_invalid_input_leaves_no_session(world: World) -> None:
    world.put(FILE, [entry("cred_a")])
    with locked(world) as lease:
        with pytest.raises(oc.NotUnderstood):
            oc.park_for_restore(world.root, world.carrier, b"{", lease, policy=world.policy)
        world.put(REMOVED, b"{")
        with pytest.raises(oc.NotUnderstood):
            oc.park_for_restore(world.root, world.carrier, b"[]", lease, policy=world.policy)
    assert world.sessions() == []


def test_a_failing_final_write_removes_the_parked_session(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(oc.ops, "fsync_file", broken)
    with locked(world) as lease, pytest.raises(OSError):
        oc.park_for_restore(world.root, world.carrier, b"[]", lease, policy=world.policy)
    assert world.sessions() == []


# ---------------------------------------------------------------------------------------------
# Tombstones
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "a b", "x\n", "a" * 201, "ü", "id;rm", "a\u00a0b"])
def test_tombstone_ids_accepts_only_the_id_charset(world: World, bad: str) -> None:
    world.put(FILE, [entry("cred_a")])
    before = world.snapshot()
    with locked(world) as lease, pytest.raises(oc.NotUnderstood):
        oc.tombstone_ids(world.carrier, ["cred_a", bad], lease)
    assert world.snapshot() == before


def test_tombstone_ids_reports_unknown_ids_but_still_records_them(world: World) -> None:
    world.put(FILE, [entry("cred_a")])
    world.put(COPY_A, [entry("cred_copy", "prov_c")])
    world.put(REMOVED, ["cred_old"])
    entries_before = (world.carrier / FILE).read_bytes()
    with locked(world) as lease:
        report = oc.tombstone_ids(
            world.carrier, ["cred_a", "cred_copy", "cred_ghost", "cred_a"], lease
        )
    assert report == oc.TombstoneReport(
        added=("cred_a", "cred_copy", "cred_ghost"), unknown=("cred_ghost",), total=4
    )
    assert world.ids(REMOVED) == ["cred_a", "cred_copy", "cred_ghost", "cred_old"]
    assert (world.carrier / FILE).read_bytes() == entries_before
    assert mode_of(world.carrier / REMOVED) == 0o600


def test_tombstone_ids_is_atomic_and_idempotent(world: World) -> None:
    world.put(FILE, [entry("cred_a")])
    with locked(world) as lease:
        oc.tombstone_ids(world.carrier, ["cred_a"], lease)
        first = (world.carrier / REMOVED).stat()
        report = oc.tombstone_ids(world.carrier, ["cred_a"], lease)
    second = (world.carrier / REMOVED).stat()
    assert report.added == () and first.st_ino == second.st_ino
    assert [p.name for p in world.carrier.iterdir() if p.name.endswith(".tmp")] == []


def test_tombstone_ids_unions_the_tombstones_of_copies(world: World) -> None:
    world.put(COPY_REMOVED, ["cred_from_copy"])
    with locked(world) as lease:
        oc.tombstone_ids(world.carrier, ["cred_x"], lease)
    assert world.ids(REMOVED) == ["cred_from_copy", "cred_x"]


@pytest.mark.parametrize("name", [FILE, REMOVED])
@pytest.mark.parametrize("function", ["ids", "all", "clear"])
def test_an_invalid_carrier_file_stops_tombstones_and_clearing_without_a_change(
    world: World, name: str, function: str
) -> None:
    world.put(FILE, [entry("cred_a")])
    world.put(name, b"{broken")
    before = world.snapshot()
    with locked(world) as lease, pytest.raises(oc.NotUnderstood):
        if function == "ids":
            oc.tombstone_ids(world.carrier, ["cred_a"], lease)
        elif function == "all":
            oc.tombstone_all(world.carrier, ["cred_b"], lease)
        else:
            oc.clear_carrier_except_tombstones(world.carrier, lease)
    assert world.snapshot() == before


def test_tombstone_all_unions_file_copies_extras_and_older_tombstones(world: World) -> None:
    world.put(FILE, [entry("cred_a"), entry("cred_b", "prov_b")])
    world.put(COPY_A, [entry("cred_c", "prov_c")])
    world.put(REMOVED, ["cred_older"])
    world.put(COPY_REMOVED, ["cred_older_copy"])
    entries_before = (world.carrier / FILE).read_bytes()
    with locked(world) as lease:
        report = oc.tombstone_all(world.carrier, ["cred_vol", "cred_session"], lease)
    assert world.ids(REMOVED) == [
        "cred_a",
        "cred_b",
        "cred_c",
        "cred_older",
        "cred_older_copy",
        "cred_session",
        "cred_vol",
    ]
    assert report.total == 7 and report.unknown == ("cred_session", "cred_vol")
    assert (world.carrier / FILE).read_bytes() == entries_before


def test_tombstone_all_rejects_an_invalid_extra_id_and_changes_nothing(world: World) -> None:
    world.put(FILE, [entry("cred_a")])
    before = world.snapshot()
    with locked(world) as lease, pytest.raises(oc.NotUnderstood):
        oc.tombstone_all(world.carrier, ["bad id"], lease)
    assert world.snapshot() == before


def test_tombstone_writes_retry_against_a_concurrent_writer_at_most_three_times(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.put(REMOVED, ["cred_old"])
    calls = {"n": 0}
    real = oc.ops.fsync_file

    def interfere(fd: int) -> None:
        calls["n"] += 1
        real(fd)
        if calls["n"] == 1:
            world.put(REMOVED, ["cred_mirror", "cred_old"])

    monkeypatch.setattr(oc.ops, "fsync_file", interfere)
    with locked(world) as lease:
        oc.tombstone_ids(world.carrier, ["cred_new"], lease)
    assert world.ids(REMOVED) == ["cred_mirror", "cred_new", "cred_old"]

    def always(fd: int) -> None:
        calls["n"] += 1
        real(fd)
        world.put(REMOVED, ["cred_old", f"cred_race{calls['n']}"])

    calls["n"] = 100
    monkeypatch.setattr(oc.ops, "fsync_file", always)
    with locked(world) as lease, pytest.raises(oc.ChangedOnDisk):
        oc.tombstone_ids(world.carrier, ["cred_other"], lease)
    assert calls["n"] == 103


def test_clearing_names_everything_but_the_tombstones_and_deletes_nothing(world: World) -> None:
    world.put(FILE, [entry("cred_a")])
    world.put(REMOVED, ["cred_a"])
    world.put(COPY_A, [])
    (world.carrier / "sub").mkdir()
    (world.carrier / f".{FILE}.{'c' * 16}.tmp").write_bytes(b"x")
    (world.carrier / "link").symlink_to("sub")
    before = sorted(p.name for p in world.carrier.iterdir())
    with locked(world) as lease:
        names = oc.clear_carrier_except_tombstones(world.carrier, lease)
    assert names == sorted(
        [FILE, COPY_A, "sub", "link", f".{FILE}.{'c' * 16}.tmp"],
    )
    assert sorted(p.name for p in world.carrier.iterdir()) == before


# ---------------------------------------------------------------------------------------------
# Doctor findings
# ---------------------------------------------------------------------------------------------


def findings(world: World) -> list[oc.Finding]:
    return oc.carrier_findings(world.carrier, world.root, policy=world.policy)


def test_a_clean_carrier_has_no_findings(world: World) -> None:
    world.put(FILE, [entry("cred_a")])
    world.put(REMOVED, ["cred_b"])
    assert findings(world) == []


def test_every_finding_kind_is_reported(world: World) -> None:
    other = entry("cred_a")
    other["value"] = {"type": "key", "key": token("different")}
    world.put(FILE, [entry("cred_a"), entry("cred_same", "prov_s")])
    world.put(COPY_A, [other, entry("cred_same", "prov_s")])
    world.put(COPY_REMOVED, ["cred_gone"])
    broken = "credentials.sync-conflict-20260102-130000-.json"
    world.put(broken, b"{broken")
    temp = f".{REMOVED}.{'d' * 16}.tmp"
    (world.carrier / temp).write_bytes(b"x")
    kept = world.finished_session(final=[entry("cred_x", "prov_x")])
    (world.root / (".creating-" + "6" * 32)).mkdir()
    assert findings(world) == [
        oc.Finding("stale-temp", temp),
        oc.Finding("conflict-copy", COPY_REMOVED),
        oc.Finding("conflict-copy", COPY_A),
        oc.Finding("differing-value", "cred_a", "prov_a"),
        oc.Finding("conflict-copy", broken),
        oc.Finding("invalid-file", broken, "invalid JSON or UTF-8"),
        oc.Finding("audit-session", "6" * 32, "unfinished"),
        oc.Finding("audit-session", kept, "retained"),
    ]


def test_invalid_main_files_are_reported(world: World) -> None:
    world.put(FILE, b"{broken")
    world.put(REMOVED, b'["bad id"]')
    assert findings(world) == [
        oc.Finding("invalid-file", FILE, "invalid JSON or UTF-8"),
        oc.Finding("invalid-file", REMOVED, "a tombstone is not a valid id"),
    ]


def test_a_refused_session_root_and_a_missing_carrier_are_findings_not_errors(
    world: World, tmp_path: Path
) -> None:
    world.root.parent.mkdir(parents=True, mode=0o700)
    world.root.symlink_to(tmp_path)
    got = oc.carrier_findings(tmp_path / "missing", world.root, policy=world.policy)
    assert got == [oc.Finding("audit-root", "", "a path component is a symlink or not a directory")]


def test_findings_carry_no_values(world: World) -> None:
    sentinel = token("sentinel")
    risky = entry("cred_a")
    risky["label"] = sentinel
    risky["value"] = {"type": "key", "key": sentinel + "k"}
    other = entry("cred_a")
    other["value"] = {"type": "key", "key": sentinel + "o"}
    world.put(FILE, [risky])
    world.put(COPY_A, [other])
    world.put("credentials.sync-conflict-20260102-130000-.json", sentinel.encode())
    world.finished_session(final=[risky])
    got = findings(world)
    assert any(item.kind == "differing-value" for item in got)
    assert any(item.kind == "invalid-file" for item in got)
    assert sentinel not in repr(got)
    # positive control: the scan does see a sentinel when one is present
    assert sentinel in repr(got + [oc.Finding("x", sentinel)])
