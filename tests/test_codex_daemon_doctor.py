from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from djinn_in_a_box.commands import doctor as doctor_mod
from djinn_in_a_box.config import zones as zones_mod
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import ZoneAssignments, load_zone_assignments
from djinn_in_a_box.core.docker import ZoneRoots, resolve_zone_roots


@dataclass
class DaemonTree:
    config: AppConfig
    roots: ZoneRoots
    assignments: ZoneAssignments

    @property
    def home(self) -> Path:
        return self.roots.local_root / "codex"

    @property
    def releases(self) -> Path:
        return self.home / "packages/app-server-daemon/releases"

    def settings(self, raw: bytes = b'{"updater":{"autoUpdateEnabled":false}}') -> Path:
        path = self.home / "app-server-daemon/settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def release(self, name: str = "0.163.0-test", *, selected: bool = True) -> Path:
        path = self.releases / name
        (path / "bin").mkdir(parents=True)
        (path / "bin/codex").write_bytes(b"x" * 1024)
        (path / "codex-package.json").write_text('{"version":"0.163.0","layoutVersion":1}')
        (path / "codex").symlink_to("bin/codex")
        if selected:
            # Codex's own shapes: relative `releases/<name>` or the container's absolute path.
            (self.releases.parent / "current").symlink_to(Path("releases") / name)
        return path

    def checks(self) -> list[doctor_mod.Check]:
        return doctor_mod._codex_daemon_checks(self.roots, self.assignments)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DaemonTree:
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(zones_mod, "ZONES_FILE", tmp_path / "zones.toml")
    config = AppConfig(code_dir=projects, config_root=tmp_path / "config")
    return DaemonTree(config, resolve_zone_roots(config), load_zone_assignments(config))


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        content = ""
        if stat.S_ISLNK(info.st_mode):
            content = os.readlink(path)
        elif stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        elif stat.S_ISREG(info.st_mode):
            content = hashlib.sha256(path.read_bytes()).hexdigest()
        result[str(path.relative_to(root))] = (info.st_mode, content)
    return result


def _expected_bytes(path: Path) -> int:
    # Independent fixture sum: rglob does not descend directory symlinks.
    return path.lstat().st_size + sum(child.lstat().st_size for child in path.rglob("*"))


def test_shipped_bounds_match_the_documented_contract() -> None:
    assert doctor_mod.CODEX_DAEMON_SCAN_MAX_ENTRIES == 200_000
    assert doctor_mod.CODEX_DAEMON_SETTINGS_MAX_BYTES == 64 * 1024


def test_rows_appended_to_zone_checks_use_resolved_local_root(tree: DaemonTree) -> None:
    tree.settings()
    tree.release()
    foreign = tree.roots.config_root / "codex/app-server-daemon/settings.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_text('{"updater":{"autoUpdateEnabled":true}}')
    checks = doctor_mod._zone_diagnostic_checks(tree.config, tree.assignments)
    assert [row.name for row in checks[-2:]] == ["Codex daemon updater", "Codex daemon releases"]
    assert all(row.status is doctor_mod.Status.PASS for row in checks[-2:])


@pytest.mark.parametrize("empty_dirs", [False, True])
def test_nothing_installed_passes_without_creating_directories(
    tree: DaemonTree, empty_dirs: bool
) -> None:
    if empty_dirs:
        (tree.home / "app-server-daemon").mkdir(parents=True)
        tree.releases.mkdir(parents=True)
    before = _snapshot(tree.config.code_dir.parent)
    for row in tree.checks():
        assert row.status is doctor_mod.Status.PASS
        assert "none installed" in row.detail
        assert not row.remedy
    assert "remote control: unset" in tree.checks()[0].detail
    assert tree.home.exists() is empty_dirs
    assert _snapshot(tree.config.code_dir.parent) == before


@pytest.mark.parametrize("remote, label", [(True, "on"), (False, "off"), (None, "unset")])
def test_false_updater_remote_control_is_information(
    tree: DaemonTree, remote: bool | None, label: str
) -> None:
    value: dict[str, Any] = {"updater": {"autoUpdateEnabled": False}}
    if remote is not None:
        value["remoteControlEnabled"] = remote
    tree.settings(json.dumps(value).encode())
    tree.release()
    row = tree.checks()[0]
    assert row.status is doctor_mod.Status.PASS
    assert "updater.autoUpdateEnabled: false" in row.detail
    assert f"remote control: {label}" in row.detail


def test_true_updater_warns(tree: DaemonTree) -> None:
    tree.settings(b'{"updater":{"autoUpdateEnabled":true},"remoteControlEnabled":true}')
    row = tree.checks()[0]
    assert row.status is doctor_mod.Status.WARN
    assert "autoUpdateEnabled: true" in row.detail
    assert "remote control: on" in row.detail
    assert "dev container" in row.remedy


@pytest.mark.parametrize("raw", [None, b"{}", b'{"updater":{}}'])
@pytest.mark.parametrize("managed", ["release", "current", "staging", "file"])
def test_missing_policy_with_managed_copy_warns(
    tree: DaemonTree, raw: bytes | None, managed: str
) -> None:
    if raw is not None:
        tree.settings(raw)
    if managed == "release":
        tree.release(selected=False)
    elif managed == "staging":
        (tree.releases / ".staging.install").mkdir(parents=True)
    elif managed == "file":
        tree.releases.mkdir(parents=True)
        (tree.releases / "install.lock").touch()
    else:
        tree.releases.parent.mkdir(parents=True)
        (tree.releases.parent / "current").symlink_to("releases/missing")
    row = tree.checks()[0]
    assert row.status is doctor_mod.Status.WARN
    assert "the updater runs by default" in row.detail
    assert "remote control: unset" in row.detail
    assert "start the dev container to apply the policy" in row.remedy.lower()


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b'{"updater":{"autoUpdateEnabled":false,"autoUpdateEnabled":true}}',
        b'{"updater":{"autoUpdateEnabled":true},"updater":{"autoUpdateEnabled":false}}',
        b'{"updater":{"autoUpdateEnabled":false},"neighbor":{"x":1,"x":2}}',
        b'{"updater":{"autoUpdateEnabled":false},"x":NaN}',
        b'{"updater":{"autoUpdateEnabled":false},"x":Infinity}',
        b'{"updater":{"autoUpdateEnabled":false},"x":-Infinity}',
        b'{"updater":{"autoUpdateEnabled":false},"x":1e999}',
        b'{"updater":{"autoUpdateEnabled":false},"x":-1e999}',
        b'{"updater":{"autoUpdateEnabled":false},"x":"\xff"}',
        b"[]",
        b"null",
        b'{"updater":null}',
        b'{"updater":[]}',
        b'{"updater":false}',
        b'{"updater":{"autoUpdateEnabled":0}}',
        b'{"updater":{"autoUpdateEnabled":"false"}}',
        b'{"updater":{"autoUpdateEnabled":null}}',
        b'{"updater":{"autoUpdateEnabled":false}}' + b" " * (65 * 1024),
    ],
    ids=[
        "malformed",
        "duplicate-setting",
        "duplicate-updater",
        "duplicate-neighbor",
        "nan",
        "infinity",
        "negative-infinity",
        "overflow",
        "negative-overflow",
        "utf8",
        "array",
        "null",
        "updater-null",
        "updater-array",
        "updater-bool",
        "zero",
        "string",
        "value-null",
        "oversized",
    ],
)
def test_invalid_settings_warn(tree: DaemonTree, raw: bytes) -> None:
    tree.settings(raw)
    row = tree.checks()[0]
    assert row.status is doctor_mod.Status.WARN
    assert "remote control: unset" in row.detail


def test_settings_at_size_boundary_and_finite_numbers_pass(tree: DaemonTree) -> None:
    raw = b'{"updater":{"autoUpdateEnabled":false},"x":1e100}'
    tree.settings(raw.ljust(64 * 1024, b" "))
    assert tree.checks()[0].status is doctor_mod.Status.PASS


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_settings_must_be_regular_without_following_links(
    tree: DaemonTree, tmp_path: Path, kind: str
) -> None:
    path = tree.home / "app-server-daemon/settings.json"
    path.parent.mkdir(parents=True)
    sentinel = tmp_path / "outside.json"
    sentinel.write_bytes(b'{"updater":{"autoUpdateEnabled":false}}')
    if kind == "symlink":
        path.symlink_to(sentinel)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    before = _snapshot(tmp_path)
    row = tree.checks()[0]
    assert row.status is doctor_mod.Status.WARN
    assert "not a regular file" in row.detail
    assert _snapshot(tmp_path) == before


def test_settings_open_uses_nofollow(tree: DaemonTree, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tree.settings()
    original = os.open
    seen: list[int] = []

    def guarded_open(file: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(file) == path:
            seen.append(flags)
            assert flags & os.O_NOFOLLOW
        return original(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    assert tree.checks()[0].status is doctor_mod.Status.PASS
    assert seen


def test_growing_settings_read_is_bounded(
    tree: DaemonTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tree.settings()
    original_open = os.open
    reads: list[int] = []
    original_fdopen = os.fdopen

    def growing_open(file: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(file) == path:
            path.write_bytes(path.read_bytes().ljust(65 * 1024, b" "))
        return original_open(file, flags, *args, **kwargs)

    class BoundedReader:
        def __init__(self, fd: int) -> None:
            self.source = original_fdopen(fd, "rb")

        def __enter__(self) -> BoundedReader:
            return self

        def __exit__(self, *args: Any) -> None:
            self.source.close()

        def fileno(self) -> int:
            return self.source.fileno()

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            assert 0 < size <= 64 * 1024 + 1
            return self.source.read(size)

    monkeypatch.setattr(os, "open", growing_open)
    monkeypatch.setattr(os, "fdopen", lambda fd, mode: BoundedReader(fd))
    row = tree.checks()[0]
    assert row.status is doctor_mod.Status.WARN
    assert "larger than 64 KiB" in row.detail
    assert reads


@pytest.mark.parametrize(
    "relative",
    [
        "local-root",
        ".",
        "app-server-daemon",
        "packages",
        "packages/app-server-daemon",
        "packages/app-server-daemon/releases",
    ],
)
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_directory_components_admitted_without_following_symlinks(
    tree: DaemonTree,
    tmp_path: Path,
    relative: str,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if relative == "local-root":
        path = tree.roots.local_root
    else:
        path = tree.home if relative == "." else tree.home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    (outside / "release/bin").mkdir(parents=True)
    (outside / "release/bin/codex").write_bytes(b"outside sentinel" * 100)
    if kind == "symlink":
        path.symlink_to(outside, target_is_directory=True)
    else:
        path.write_bytes(b"not a directory")
    original_lstat = os.lstat

    def guarded_lstat(file: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        assert not Path(file).is_relative_to(path) or Path(file) == path
        return original_lstat(file, *args, **kwargs)

    before = _snapshot(tmp_path)
    with monkeypatch.context() as guard:
        guard.setattr(os, "lstat", guarded_lstat)
        rows = tree.checks()
    for row in rows:
        assert row.status is doctor_mod.Status.WARN
        assert (
            f"unexpected {'symlink' if kind == 'symlink' else 'non-directory'} at {path}"
            in row.detail
        )
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("count", [0, 1, 2])
def test_release_count_and_complete_size(tree: DaemonTree, count: int) -> None:
    tree.settings()
    tree.releases.mkdir(parents=True)
    for index in range(count):
        tree.release(f"0.163.{index}-test", selected=index == 0)
    row = tree.checks()[1]
    assert row.status is (doctor_mod.Status.WARN if count > 1 else doctor_mod.Status.PASS)
    if not count:
        assert "none installed" in row.detail
    else:
        assert doctor_mod._format_size(_expected_bytes(tree.releases)) in row.detail
        assert "current: 0.163.0-test" in row.detail
        assert "2 releases" in row.detail if count == 2 else "0.163.0-test (" in row.detail
    if count > 1:
        assert "pruning runs at dev-container start" in row.detail


def test_staging_and_non_directories_not_counted_but_sized(
    tree: DaemonTree, tmp_path: Path
) -> None:
    tree.release()
    tree.release(".staging.install", selected=False)
    (tree.releases / "install.lock").write_bytes(b"lock")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload").write_bytes(b"x" * 50_000)
    (tree.releases / "symlink-release").symlink_to(outside, target_is_directory=True)
    row = tree.checks()[1]
    assert row.status is doctor_mod.Status.PASS
    assert "0.163.0-test (" in row.detail
    assert "staging: .staging.install" in row.detail
    assert doctor_mod._format_size(_expected_bytes(tree.releases)) in row.detail
    assert "symlink-release" not in row.detail


@pytest.mark.parametrize("absolute", [False, True])
def test_current_readlink_names_target_without_resolving(
    tree: DaemonTree, tmp_path: Path, absolute: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree.release(selected=False)
    # Codex writes an absolute container path; the host only checks its `releases/<name>` shape.
    container = Path("/home/dev/.codex/packages/app-server-daemon/releases/0.163.0-test")
    target = container if absolute else Path("releases/0.163.0-test")
    (tree.releases.parent / "current").symlink_to(target)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("current must be read as a link, never resolved")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "resolve", forbidden)
        row = tree.checks()[1]
    assert row.status is doctor_mod.Status.PASS
    assert f"current: {target.name}" in row.detail


@pytest.mark.parametrize(
    "target",
    [
        "releases/missing",
        "releases/not-present/../0.163.0-test",
        "elsewhere/../releases/0.163.0-test",
        "elsewhere/releases/0.163.0-test",
        "/other/home/packages/app-server-daemon/releases/0.163.0-test",
        "outside/0.163.0-test",
        "releases/no-manifest",
        "releases/.staging.abc",
    ],
)
def test_current_outside_the_helper_rule_warns(tree: DaemonTree, target: str) -> None:
    tree.release(selected=False)
    (tree.releases / "no-manifest").mkdir()
    (tree.releases / ".staging.abc").mkdir()
    (tree.releases.parent / "current").symlink_to(target)
    row = tree.checks()[1]
    assert row.status is doctor_mod.Status.WARN
    assert f"invalid current selection {target}; start-time pruning is skipped" in row.detail


def test_container_writable_names_are_escaped_in_both_rows(tree: DaemonTree) -> None:
    hostile = "rel\x1b]52;c;cGF5bG9hZA==\x07ease"
    tree.release(hostile)
    tree.release("second", selected=False)
    (tree.releases / ".staging.\x1b[31m").mkdir()
    tree.settings(b'{"updater":{"autoUpdateEnabled":false},"\\u001b]0;t\\u0007":1,"\\u001b]0;t\\u0007":2}')
    updater, releases = tree.checks()
    for row in (updater, releases):
        assert "\x1b" not in row.detail and "\x07" not in row.detail
    assert "\\x1b]52;c;cGF5bG9hZA==\\x07" in releases.detail
    assert "duplicate member" in updater.detail and "\\x1b]0;t\\x07" in updater.detail


def test_strict_walker_counts_symlink_bytes_without_visiting_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "release"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload").write_bytes(b"x" * 1024)
    link = root / "linked-dir"
    link.symlink_to(outside, target_is_directory=True)
    original = os.lstat
    visited: list[Path] = []

    def record(file: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        visited.append(Path(file))
        return original(file, *args, **kwargs)

    expected = root.lstat().st_size + link.lstat().st_size
    monkeypatch.setattr(os, "lstat", record)
    size, entries, unreadable, capped = doctor_mod._codex_daemon_tree_size(root)
    assert (size, entries, unreadable, capped) == (expected, 2, None, False)
    assert visited == [root, link]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read chmod 000 directories")
def test_nested_unreadable_directory_warns_without_partial_size(tree: DaemonTree) -> None:
    release = tree.release()
    blocked = release / "blocked"
    blocked.mkdir()
    (blocked / "payload").write_bytes(b"x" * 50_000)
    blocked.chmod(0)
    try:
        row = tree.checks()[1]
        assert row.status is doctor_mod.Status.WARN
        assert f"unreadable: {blocked}" in row.detail
        assert not any(unit in row.detail for unit in (" B", "KiB", "MiB"))
    finally:
        blocked.chmod(0o700)


def test_entry_cap_warns_without_partial_size(
    tree: DaemonTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree.release()
    monkeypatch.setattr(doctor_mod, "CODEX_DAEMON_SCAN_MAX_ENTRIES", 2)
    size, entries, unreadable, capped = doctor_mod._codex_daemon_tree_size(tree.releases)
    assert capped
    assert entries <= 2
    assert unreadable is None
    assert size >= 0
    row = tree.checks()[1]
    assert row.status is doctor_mod.Status.WARN
    assert "scan incomplete" in row.detail
    assert not any(unit in row.detail for unit in (" B", "KiB", "MiB"))


@pytest.mark.parametrize("entry", ["packages", "app-server-daemon"])
def test_skipped_overlay_prevents_all_inspection(
    tree: DaemonTree, entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conflict = tree.roots.config_root / "codex" / entry
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"overlay conflict")
    assignments = load_zone_assignments(tree.config)
    assert Path(entry) not in assignments.by_agent["codex"]["local"]

    original = os.lstat
    inspected: list[Path] = []

    def record(file: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        inspected.append(Path(file))
        return original(file, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(os, "lstat", record)
        rows = doctor_mod._codex_daemon_checks(tree.roots, assignments)
    assert not inspected
    for row in rows:
        assert row.status is doctor_mod.Status.WARN
        assert f"not inspected: {entry} overlay skipped" in row.detail
        assert "Skipped shipped zone defaults" in row.remedy
    assert "remote control: unset" in rows[0].detail


def test_diagnostics_preserve_every_input_byte(tree: DaemonTree, tmp_path: Path) -> None:
    tree.settings(b'{ "updater": {"autoUpdateEnabled": false}, "remoteControlEnabled": true }\n')
    tree.release()
    tree.release("old-release", selected=False)
    tree.release(".staging.install", selected=False)
    before = _snapshot(tmp_path)
    doctor_mod._zone_diagnostic_checks(tree.config, tree.assignments)
    assert _snapshot(tmp_path) == before


def test_oversized_settings_rejected_before_open(
    tree: DaemonTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree.settings(b" " * (65 * 1024))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("oversized settings must not be opened")

    monkeypatch.setattr(os, "open", forbidden)
    assert tree.checks()[0].status is doctor_mod.Status.WARN


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_settings_replacement_between_lstat_and_open_is_refused(
    tree: DaemonTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    path = tree.settings()
    target = tmp_path / "outside.json"
    target.write_bytes(path.read_bytes())
    original = os.open

    def replacing_open(file: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(file) == path:
            assert flags & os.O_NONBLOCK
            path.unlink()
            if replacement == "symlink":
                path.symlink_to(target)
            else:
                os.mkfifo(path)
        return original(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replacing_open)
    row = tree.checks()[0]
    assert row.status is doctor_mod.Status.WARN
    if replacement == "fifo":
        assert "not a regular file" in row.detail
    assert target.read_bytes() == b'{"updater":{"autoUpdateEnabled":false}}'


@pytest.mark.parametrize(
    "failure",
    [
        "directory-lstat",
        "current-lstat",
        "current-readlink",
        "settings-read",
        "release-lstat",
        "release-enumeration",
    ],
)
def test_unreadable_paths_warn(
    tree: DaemonTree, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    release = tree.release()
    settings = tree.settings()
    current = tree.releases.parent / "current"
    target = {
        "directory-lstat": tree.home / "packages",
        "current-lstat": current,
        "current-readlink": current,
        "settings-read": settings,
        "release-lstat": release / "bin/codex",
        "release-enumeration": tree.releases,
    }[failure]
    method = {
        "current-readlink": "readlink",
        "settings-read": "open",
        "release-enumeration": "scandir",
    }.get(failure, "lstat")
    original = getattr(os, method)
    calls = 0

    def denied(file: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        if Path(file) == target:
            calls += 1
            # First enumeration belongs to the strict walker; second counts releases.
            if failure != "release-enumeration" or calls == 2:
                raise PermissionError(13, "test unreadable", str(target))
        return original(file, *args, **kwargs)

    monkeypatch.setattr(os, method, denied)
    rows = tree.checks()
    row = rows[0] if failure == "settings-read" else rows[1]
    assert row.status is doctor_mod.Status.WARN
    assert str(target) in row.detail
    if failure != "settings-read":
        assert "unreadable:" in row.detail
        assert not any(unit in row.detail for unit in (" B", "KiB", "MiB"))


def test_parser_recursion_error_warns(tree: DaemonTree, monkeypatch: pytest.MonkeyPatch) -> None:
    tree.settings()

    def exhausted(*args: Any, **kwargs: Any) -> Any:
        raise RecursionError("test parser exhausted")

    monkeypatch.setattr(doctor_mod.json, "loads", exhausted)
    assert tree.checks()[0].status is doctor_mod.Status.WARN


def test_walker_handles_deep_tree_iteratively(tmp_path: Path) -> None:
    root = tmp_path / "deep"
    root.mkdir()
    path = root
    for _ in range(150):
        path /= "d"
        path.mkdir()
    expected = _expected_bytes(root)
    recursion_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(100)
        result = doctor_mod._codex_daemon_tree_size(root)
    finally:
        sys.setrecursionlimit(recursion_limit)
    assert result == (expected, 151, None, False)


def test_doctor_does_not_spawn_or_open_for_writing(
    tree: DaemonTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree.settings()
    tree.release()
    original = os.open

    def read_only(file: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert flags & os.O_ACCMODE == os.O_RDONLY
        return original(file, flags, *args, **kwargs)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("daemon diagnostics must not invoke commands")

    monkeypatch.setattr(os, "open", read_only)
    monkeypatch.setattr(doctor_mod.subprocess, "run", forbidden)
    monkeypatch.setattr(doctor_mod.subprocess, "Popen", forbidden)
    assert all(row.status is doctor_mod.Status.PASS for row in tree.checks())


def test_release_count_enumeration_stays_bounded_after_size_scan(
    tree: DaemonTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree.releases.mkdir(parents=True)
    for index in range(3):
        (tree.releases / f"release-{index}").mkdir()
    monkeypatch.setattr(doctor_mod, "CODEX_DAEMON_SCAN_MAX_ENTRIES", 2)
    # Model entries appearing after a completed size scan.
    monkeypatch.setattr(doctor_mod, "_codex_daemon_tree_size", lambda path: (1024, 1, None, False))
    row = tree.checks()[1]
    assert row.status is doctor_mod.Status.WARN
    assert "scan incomplete" in row.detail
    assert "KiB" not in row.detail
