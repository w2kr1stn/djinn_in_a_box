"""Container policy tested against disposable homes and a Codex-shaped CLI."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/codex-daemon-policy.py"
PINNED = (
    "local-5709e443bbea01852a7e5419d3ec6c167fc386fd5c150bfc1cbe84c888d172eb"
    "-x86_64-unknown-linux-musl"
)
# Real Codex 0.163.0 output: `app-server daemon update --from-cli --yes` and a settings parse error.
UPDATED = '{"status":"updated","installedVersion":"0.163.0","runningVersion":null}'
PARSE_ERROR = "EOF while parsing a value at line 2 column 0"


def test_helper_parses_with_the_image_python_grammar():
    # The image runs the helper with Debian bookworm's Python 3.11; the suite runs a newer one.
    ast.parse(SCRIPT.read_text(), feature_version=(3, 11))


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("codex_daemon_policy", SCRIPT)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def home(tmp_path: Path) -> Path:
    result = tmp_path / ".codex"
    result.mkdir()
    return result


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    result = tmp_path / "codex"
    result.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, signal, subprocess, sys, time\n"
        f"PINNED = {PINNED!r}\n"
        f"UPDATED = {UPDATED!r}\n"
        f"PARSE_ERROR = {PARSE_ERROR!r}\n"
        + '''
home = pathlib.Path(os.environ["CODEX_HOME"])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "home": str(home),
        "home_env": os.environ["CODEX_HOME"],
        "settings": (home / "app-server-daemon/settings.json").read_text()
            if (home / "app-server-daemon/settings.json").exists() else None,
        "null_stdin": os.path.samefile("/proc/self/fd/0", "/dev/null"),
        "pid": os.getpid(), "pgid": os.getpgrp(), "sid": os.getsid(0)}) + "\\n")
if sys.argv[1:] == ["--version"]:
    if os.environ.get("FAKE_VERSION_SLEEP"):
        time.sleep(2)
    print(os.environ.get("FAKE_VERSION", "codex-cli 0.163.0"))
    sys.exit(int(os.environ.get("FAKE_VERSION_EXIT", "0")))
assert sys.argv[1:] == ["app-server", "daemon", "update", "--from-cli", "--yes"]
mode = os.environ.get("FAKE_MODE", "publish")
if mode == "timeout":
    # npm-shaped launcher: the native child ignores TERM and would publish later.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c", """
import os, pathlib, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
home = pathlib.Path(os.environ['CODEX_HOME'])
(home / 'native.pid').write_text(str(os.getpid()))
time.sleep(1.0)
(home / 'published').write_text('late publication')
p = home / 'packages/app-server-daemon'
(p / 'current').unlink()
(p / 'current').symlink_to('releases/late')
"""])
    child.wait()
    sys.exit(0)
if mode == "silent-fail":
    sys.exit(7)
if mode == "remove-current":
    (home / "packages/app-server-daemon/current").unlink()
if mode in ("publish", "publish-fail", "publish-unsafe", "publish-dangling", "publish-no-manifest"):
    package = home / "packages/app-server-daemon"
    release = package / "releases" / PINNED
    release.mkdir(exist_ok=True)
    (release / "codex-package.json").write_text('{"version":"0.163.0"}')
    (package / "current").unlink()
    target = release if mode != "publish-unsafe" else pathlib.Path(os.environ["FAKE_OUTSIDE"])
    if mode == "publish-dangling":
        target = package / "releases/missing"
    if mode == "publish-no-manifest":
        (release / "codex-package.json").unlink()
    (package / "current").symlink_to(target)
    print(UPDATED, flush=True)
if mode in ("fail", "publish-fail"):
    print("Error: failed to parse settings "
          + str(home / "app-server-daemon/settings.json"), flush=True)
    print(PARSE_ERROR, file=sys.stderr, flush=True)
    sys.exit(1)
'''
    )
    result.chmod(0o755)
    return result


def run(
    home: Path,
    fake: Path,
    *,
    overrides: dict[str, str] | None = None,
    constants: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT)]
    if constants:
        command = [
            sys.executable,
            "-c",
            f"import runpy, sys; ns=runpy.run_path({str(SCRIPT)!r}); "
            f"ns['main'].__globals__.update({constants}); sys.exit(ns['main']())",
        ]
    return subprocess.run(
        command + ["--codex-home", str(home), "--codex", str(fake)],
        env={**os.environ, "FAKE_LOG": str(home.parent / "calls.jsonl"), **(overrides or {})},
        input="not devnull\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def calls(home: Path) -> list[dict]:
    log = home.parent / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def updates(home: Path) -> list[dict]:
    return [call for call in calls(home) if call["argv"] != ["--version"]]


def releases(home: Path) -> Path:
    return home / "packages/app-server-daemon/releases"


def release(home: Path, name: str, version: str = "0.163.0") -> Path:
    result = releases(home) / name
    result.mkdir(parents=True, exist_ok=True)
    (result / "codex-package.json").write_text(json.dumps({"version": version}))
    (result / "payload").write_bytes(b"payload")
    return result


def select(home: Path, target: Path | str) -> None:
    current = releases(home).parent / "current"
    if current.is_symlink():
        current.unlink()
    current.symlink_to(target)


def settings(home: Path, content: bytes, mode: int = 0o644) -> Path:
    directory = home / "app-server-daemon"
    directory.mkdir(exist_ok=True)
    target = directory / "settings.json"
    target.write_bytes(content)
    target.chmod(mode)
    return target


def assert_warning(result: subprocess.CompletedProcess[str], path: Path) -> None:
    assert result.returncode == 1, result
    assert "warning:" in result.stderr
    assert str(path) in result.stderr
    assert "Traceback" not in result.stderr


def test_absent_settings_created_and_no_current_does_not_call_codex(home, fake):
    result = run(home, fake)
    assert result.returncode == 0, result.stderr
    target = home / "app-server-daemon/settings.json"
    assert target.read_text() == '{\n  "updater": {\n    "autoUpdateEnabled": false\n  }\n}\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert calls(home) == []


@pytest.mark.parametrize("old", ["true", "0", "null", '"false"'])
def test_merge_preserves_neighbours_order_and_mode(home, fake, old):
    target = settings(
        home,
        (
            '{"first": [1,2], "updater":{"interval":60,"autoUpdateEnabled":'
            + old
            + ',"after":"yes"},"remoteControlEnabled":true,"last":{}}'
        ).encode(),
        0o600,
    )
    old_inode = target.stat().st_ino
    result = run(home, fake)
    assert result.returncode == 0, result.stderr
    assert json.loads(target.read_text()) == {
        "first": [1, 2],
        "updater": {"interval": 60, "autoUpdateEnabled": False, "after": "yes"},
        "remoteControlEnabled": True,
        "last": {},
    }
    assert list(json.loads(target.read_text())) == [
        "first",
        "updater",
        "remoteControlEnabled",
        "last",
    ]
    assert list(json.loads(target.read_text())["updater"]) == [
        "interval",
        "autoUpdateEnabled",
        "after",
    ]
    assert target.stat().st_ino != old_inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_missing_updater_preserves_top_level(home, fake):
    target = settings(home, b'{"remoteControlEnabled":true,"keep":42}')
    assert run(home, fake).returncode == 0
    assert json.loads(target.read_text()) == {
        "remoteControlEnabled": True,
        "keep": 42,
        "updater": {"autoUpdateEnabled": False},
    }


def test_already_false_is_byte_inode_and_mtime_noop(home, fake):
    original = b'{ "remoteControlEnabled":true,"updater": {"autoUpdateEnabled": false} }\n'
    target = settings(home, original)
    before = target.stat()
    assert run(home, fake).returncode == 0
    assert target.read_bytes() == original
    after = target.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize(
    "content",
    [
        b'{"updater":',
        b"[]",
        b"null",
        b'{"updater":1}',
        b'{"updater":null}',
        b'{"keep":1,"keep":2}',
        b'{"updater":{"x":1,"x":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1e999}',
        b'{"updater":{"autoUpdateEnabled":false},"x":1e999}',
        b'{"updater":{"autoUpdateEnabled":false},"x":NaN}',
        b'{"updater":{"autoUpdateEnabled":false},"x":Infinity}',
        b'{"updater":{"autoUpdateEnabled":false},"x":-Infinity}',
        b'{"x":"\xff"}',
    ],
)
def test_invalid_settings_untouched(home, fake, content):
    target = settings(home, content)
    before = target.stat()
    result = run(home, fake)
    assert_warning(result, target)
    assert target.read_bytes() == content
    assert target.stat().st_ino == before.st_ino
    assert not list(target.parent.glob(".djinn-codex-policy-*"))


@pytest.mark.parametrize(
    "kind", ["symlink-file", "symlink-directory", "file-directory", "directory-file"]
)
def test_unsafe_settings_paths_untouched(home, fake, kind):
    outside = home.parent / "outside"
    outside.mkdir()
    sentinel = outside / "settings.json"
    sentinel.write_bytes(b'{"updater":{"autoUpdateEnabled":true}}')
    state = home / "app-server-daemon"
    if kind == "symlink-directory":
        state.symlink_to(outside)
        path = state
    elif kind == "file-directory":
        state.write_bytes(b"not a directory")
        path = state
    else:
        state.mkdir()
        path = state / "settings.json"
        if kind == "symlink-file":
            path.symlink_to(sentinel)
        else:
            path.mkdir()
    result = run(home, fake)
    assert_warning(result, path)
    assert sentinel.read_bytes() == b'{"updater":{"autoUpdateEnabled":true}}'
    assert sorted(p.name for p in outside.iterdir()) == ["settings.json"]
    assert calls(home) == []


def test_settings_fsync_replace_directory_fsync(home, monkeypatch):
    target = settings(home, b'{"updater":{"autoUpdateEnabled":true}}', 0o600)
    policy = module()
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            assert json.loads(target.read_text())["updater"]["autoUpdateEnabled"] is False
            events.append("directory-fsync")
        else:
            (temporary,) = target.parent.glob(".djinn-codex-policy-*")
            assert temporary.read_text().endswith("\n")
            assert json.loads(temporary.read_text())["updater"]["autoUpdateEnabled"] is False
            assert stat.S_IMODE(temporary.stat().st_mode) == 0o600
            events.append("file-fsync")
        real_fsync(fd)

    def replace(source, destination):
        assert events == ["file-fsync"]
        assert Path(source).parent == target.parent
        assert Path(destination) == target
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(policy.os, "fsync", fsync)
    monkeypatch.setattr(policy.os, "replace", replace)
    assert policy.main(["--codex-home", str(home)]) == 0
    assert events == ["file-fsync", "replace", "directory-fsync"]
    assert not list(target.parent.glob(".djinn-codex-policy-*"))


def test_replace_failure_preserves_original_and_cleans_temp(home, monkeypatch, capsys):
    content = b'{"updater":{"autoUpdateEnabled":true}}'
    target = settings(home, content)
    before = target.stat()
    policy = module()

    def abort(*args, **kwargs):
        (temporary,) = target.parent.glob(".djinn-codex-policy-*")
        assert temporary.read_bytes() != content
        raise OSError("injected replace failure")

    monkeypatch.setattr(policy.os, "replace", abort)
    assert policy.main(["--codex-home", str(home)]) == 1
    assert "injected replace failure" in capsys.readouterr().err
    assert target.read_bytes() == content
    assert target.stat().st_ino == before.st_ino
    assert not list(target.parent.glob(".djinn-codex-policy-*"))


def test_readonly_directory_failure(home, fake):
    original = b'{"updater":{"autoUpdateEnabled":true}}'
    target = settings(home, original)
    target.parent.chmod(0o500)
    try:
        result = run(home, fake)
        assert_warning(result, target)
        assert "Permission denied" in result.stderr
        assert target.read_bytes() == original
        assert not list(target.parent.glob(".djinn-codex-policy-*"))
    finally:
        target.parent.chmod(0o700)


@pytest.mark.parametrize("relative", [False, True])
def test_equal_version_skips_update_and_prunes_safely(home, fake, relative):
    kept = release(home, "keep")
    old = release(home, "old")
    staging = releases(home) / ".staging.abcd"
    staging.mkdir()
    (staging / "partial").write_bytes(b"partial")
    outside = home.parent / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside survives")
    (old / "outside-link").symlink_to(outside)
    (releases(home) / "symlink-release").symlink_to(outside)
    (releases(home) / "stray-file").write_bytes(b"stray")
    expected_size = sum(
        p.lstat().st_size
        for p in [
            old,
            old / "codex-package.json",
            old / "payload",
            old / "outside-link",
            staging,
            staging / "partial",
            releases(home) / "symlink-release",
            releases(home) / "stray-file",
        ]
    )
    select(home, "releases/keep" if relative else kept)
    result = run(home, fake)
    assert result.returncode == 0, result.stderr
    assert updates(home) == []
    assert {p.name for p in releases(home).iterdir()} == {"keep"}
    assert sentinel.read_bytes() == b"outside survives"
    assert (kept / "codex-package.json").is_file()
    assert (
        result.stdout
        == f"Codex daemon: kept keep; removed 4 entries; freed {expected_size} bytes\n"
    )
    assert calls(home)[0]["null_stdin"] is True


@pytest.mark.parametrize(
    "cause", ["different", "dangling", "invalid", "missing-manifest", "unreadable"]
)
def test_reconcile_pins_cli_package_and_binds_home(home, fake, cause):
    old = release(home, "old", "0.160.0")
    select(home, old if cause != "dangling" else "releases/missing")
    manifest = old / "codex-package.json"
    if cause == "invalid":
        manifest.write_text("not json")
    elif cause == "missing-manifest":
        manifest.unlink()
    elif cause == "unreadable":
        manifest.chmod(0)
    try:
        result = run(home, fake, overrides={"CODEX_HOME": str(home.parent / "conflict")})
    finally:
        if manifest.exists():
            manifest.chmod(0o644)
    assert result.returncode == 0, result.stderr
    (update,) = updates(home)
    assert update["argv"] == ["app-server", "daemon", "update", "--from-cli", "--yes"]
    assert all(call["home"] == str(home) and call["null_stdin"] for call in calls(home))
    assert all(
        json.loads(call["settings"])["updater"]["autoUpdateEnabled"] is False
        for call in calls(home)
    )
    assert update["pid"] == update["pgid"] == update["sid"]
    assert {p.name for p in releases(home).iterdir()} == {PINNED}
    assert (releases(home).parent / "current").resolve() == releases(home) / PINNED


@pytest.mark.parametrize(
    "component", ["packages", "packages/app-server-daemon", "packages/app-server-daemon/releases"]
)
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_package_ancestor_gate_prevents_update_and_deletion(home, fake, component, kind):
    outside = home.parent / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"untouched")
    path = home / component
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        path.symlink_to(outside)
        # A plausible outside layout makes following the link observably unsafe.
        below = Path("packages/app-server-daemon/releases").relative_to(component)
        root = outside / below
        root.mkdir(parents=True, exist_ok=True)
        (root / "old").mkdir()
        (root / "old/codex-package.json").write_text('{"version":"0.160.0"}')
        current_parent = root.parent if below != Path(".") else path.parent
        # Keep the link spelled through the unsafe ancestor: later lexical
        # target validation must not accidentally mask a missing ancestor gate.
        (current_parent / "current").symlink_to(releases(home) / "old")
    else:
        path.write_bytes(b"not directory")
    before = {
        str(p.relative_to(outside)): p.read_bytes() for p in outside.rglob("*") if p.is_file()
    }
    result = run(home, fake)
    assert_warning(result, path)
    assert calls(home) == []
    assert sentinel.read_bytes() == b"untouched"
    assert {
        str(p.relative_to(outside)): p.read_bytes() for p in outside.rglob("*") if p.is_file()
    } == before


def test_state_directory_symlink_blocks_native_pid_and_lock_writes(home, fake):
    outside = home.parent / "outside"
    outside.mkdir()
    (outside / "settings.json").write_text('{"updater":{"autoUpdateEnabled":false}}')
    before = {p.name: p.read_bytes() for p in outside.iterdir()}
    (home / "app-server-daemon").symlink_to(outside)
    select(home, release(home, "old", "0.160.0"))
    result = run(home, fake)
    assert_warning(result, home / "app-server-daemon")
    assert updates(home) == []
    assert {p.name: p.read_bytes() for p in outside.iterdir()} == before
    assert (releases(home) / "old").is_dir()


@pytest.mark.parametrize("mode", ["fail", "publish-fail"])
def test_failed_update_uses_fresh_current_and_warns_last_output_line(home, fake, mode):
    old = release(home, "old", "0.160.0")
    release(home, "stale")
    select(home, old)
    result = run(home, fake, overrides={"FAKE_MODE": mode})
    assert_warning(result, releases(home).parent / "current")
    assert PARSE_ERROR in result.stderr
    assert len(updates(home)) == 1
    kept = PINNED if mode == "publish-fail" else "old"
    assert {p.name for p in releases(home).iterdir()} == {kept}
    assert (releases(home).parent / "current").resolve().is_dir()


@pytest.mark.parametrize(
    "cause", ["missing", "unusable", "extra-version-text", "failed-version", "version-timeout"]
)
def test_cli_version_failure_warns_and_never_prunes(home, fake, cause):
    select(home, release(home, "keep", "0.160.0"))
    release(home, "stale")
    overrides = {}
    constants = None
    if cause == "missing":
        fake = fake.parent / "missing-binary"
    elif cause == "unusable":
        overrides["FAKE_VERSION"] = "not codex"
    elif cause == "extra-version-text":
        overrides["FAKE_VERSION"] = "prefix codex-cli 0.163.0 suffix"
    elif cause == "failed-version":
        overrides["FAKE_VERSION_EXIT"] = "1"
    else:
        overrides["FAKE_VERSION_SLEEP"] = "1"
        constants = "VERSION_TIMEOUT=0.1"
    result = run(home, fake, overrides=overrides, constants=constants)
    assert_warning(result, releases(home).parent / "current")
    assert updates(home) == []
    assert {p.name for p in releases(home).iterdir()} == {"keep", "stale"}


@pytest.mark.parametrize(
    "cause",
    [
        "outside",
        "nested",
        "symlink-release",
        "file-release",
        "symlink-manifest",
        "directory-manifest",
        "regular-current",
        "missing-manifest",
    ],
)
def test_unsafe_selection_never_prunes(home, fake, cause):
    kept = release(home, "keep")
    stale = release(home, "stale")
    outside = home.parent / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"survives")
    target = kept
    if cause == "outside":
        target = outside
    elif cause == "nested":
        target = kept / "nested"
        target.mkdir()
        (target / "codex-package.json").write_text('{"version":"0.163.0"}')
    elif cause == "file-release":
        target = releases(home) / "not-directory"
        target.write_bytes(b"not a directory")
    elif cause == "symlink-release":
        target = releases(home) / "linked"
        target.symlink_to(kept)
    elif cause in ("symlink-manifest", "directory-manifest", "missing-manifest"):
        manifest = kept / "codex-package.json"
        manifest.unlink()
        if cause == "symlink-manifest":
            manifest.symlink_to(stale / "codex-package.json")
        elif cause == "directory-manifest":
            manifest.mkdir()
    select(home, target)
    if cause == "regular-current":
        current = releases(home).parent / "current"
        current.unlink()
        current.write_bytes(b"not a link")
    result = run(home, fake, overrides={"FAKE_MODE": "fail"})
    assert_warning(result, releases(home).parent / "current")
    assert stale.is_dir() and kept.is_dir()
    assert sentinel.read_bytes() == b"survives"
    assert "removed" not in result.stdout
    if cause != "missing-manifest":
        assert updates(home) == []


def test_fresh_selection_admission_after_update(home, fake):
    select(home, release(home, "old", "0.160.0"))
    outside = home.parent / "outside"
    outside.mkdir()
    result = run(
        home, fake, overrides={"FAKE_MODE": "publish-unsafe", "FAKE_OUTSIDE": str(outside)}
    )
    assert_warning(result, releases(home).parent / "current")
    assert (releases(home) / "old").is_dir()
    assert (releases(home) / PINNED).is_dir()
    assert "removed" not in result.stdout


def test_update_timeout_kills_launcher_and_native_child_and_skips_prune(home, fake):
    select(home, release(home, "old", "0.160.0"))
    release(home, "stale")
    result = run(home, fake, overrides={"FAKE_MODE": "timeout"}, constants="UPDATE_TIMEOUT=0.3")
    (update,) = updates(home)
    native = int((home / "native.pid").read_text())
    try:
        assert_warning(result, releases(home).parent / "current")
        assert "timed out" in result.stderr
        for pid in (update["pid"], native):
            status = Path(f"/proc/{pid}/stat")
            assert not status.exists() or status.read_text().split()[2] == "Z"
        time.sleep(1.1)
        assert not (home / "published").exists()
        assert {p.name for p in releases(home).iterdir()} == {"old", "stale"}
        assert os.readlink(releases(home).parent / "current").endswith("/old")
    finally:
        # Own fake PIDs only; verify before cleaning up a surviving mutation.
        for pid in (update["pid"], native):
            probe = subprocess.run(
                ["ps", "-o", "pid,pgid,args", "-p", str(pid)], capture_output=True
            )
            if probe.returncode == 0:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


def test_json_recursion_failure_warns_without_traceback(home, monkeypatch, capsys):
    content = b'{"keep":42}'
    target = settings(home, content)
    policy = module()

    def fail(*args, **kwargs):
        raise RecursionError("injected JSON nesting failure")

    monkeypatch.setattr(policy.json, "loads", fail)
    assert policy.main(["--codex-home", str(home)]) == 1
    captured = capsys.readouterr()
    assert "warning:" in captured.err and str(target) in captured.err
    assert "injected JSON nesting failure" in captured.err
    assert "Traceback" not in captured.err
    assert target.read_bytes() == content


def test_settings_failure_does_not_skip_reconcile_and_prune(home, fake):
    target = settings(home, b'{"updater":')
    select(home, release(home, "old", "0.160.0"))
    result = run(home, fake)
    assert_warning(result, target)
    assert len(updates(home)) == 1
    assert target.read_bytes() == b'{"updater":'
    assert {p.name for p in releases(home).iterdir()} == {PINNED}


def test_default_codex_binary_comes_from_path(home, fake):
    select(home, release(home, "old", "0.160.0"))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-home", str(home)],
        env={
            **os.environ,
            "PATH": f"{fake.parent}:{os.environ['PATH']}",
            "FAKE_LOG": str(home.parent / "calls.jsonl"),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert len(updates(home)) == 1


def test_serialization_disallows_nan(home, monkeypatch):
    policy = module()
    real_dumps = json.dumps
    kwargs_seen = []

    def dumps(*args, **kwargs):
        kwargs_seen.append(kwargs)
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(policy.json, "dumps", dumps)
    assert policy.main(["--codex-home", str(home)]) == 0
    assert kwargs_seen == [{"indent": 2, "allow_nan": False}]


def test_prune_uses_nofollow_directory_fd_for_enumeration_and_deletion(home, monkeypatch):
    selected = release(home, "keep")
    old = release(home, "old")
    (releases(home) / "file").write_bytes(b"file")
    policy = module()
    actual_open, actual_listdir = os.open, os.listdir
    actual_rmtree, actual_unlink = policy.shutil.rmtree, os.unlink
    opened = []
    deleted = []

    def open_directory(path, flags, *args, **kwargs):
        assert flags & os.O_NOFOLLOW
        assert flags & os.O_DIRECTORY
        fd = actual_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    def list_directory(path):
        assert isinstance(path, int) and path in opened
        return actual_listdir(path)

    def rmtree(path, *, dir_fd):
        assert path == old.name
        assert dir_fd == opened[0]
        deleted.append(path)
        # shutil owns its own symlink-safe recursion; inspect the policy boundary only.
        with monkeypatch.context() as context:
            context.setattr(policy.os, "open", actual_open)
            context.setattr(policy.os, "listdir", actual_listdir)
            context.setattr(policy.os, "unlink", actual_unlink)
            actual_rmtree(path, dir_fd=dir_fd)

    def unlink(path, *, dir_fd):
        assert path == "file" and dir_fd == opened[0]
        deleted.append(path)
        actual_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(policy.os, "open", open_directory)
    monkeypatch.setattr(policy.os, "listdir", list_directory)
    monkeypatch.setattr(policy.shutil, "rmtree", rmtree)
    monkeypatch.setattr(policy.os, "unlink", unlink)
    policy.Policy(home, "unused").prune(selected)
    assert sorted(deleted) == ["file", "old"]


def test_prune_failure_warns_without_ignoring_errors(home, fake, monkeypatch, capsys):
    select(home, release(home, "keep"))
    stale = release(home, "stale")
    policy = module()
    monkeypatch.setenv("FAKE_LOG", str(home.parent / "calls.jsonl"))

    def fail(*args, **kwargs):
        assert not kwargs.get("ignore_errors", False)
        raise PermissionError(f"{stale}: injected prune failure")

    monkeypatch.setattr(policy.shutil, "rmtree", fail)
    assert policy.main(["--codex-home", str(home), "--codex", str(fake)]) == 1
    captured = capsys.readouterr()
    assert "injected prune failure" in captured.err
    assert "Traceback" not in captured.err
    assert stale.is_dir()


@pytest.mark.parametrize("relative", [False, True])
def test_home_argument_is_bound_exactly_including_trailing_slash(home, fake, relative):
    select(home, release(home, "keep"))
    argument = (home.name if relative else str(home)) + "/"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-home", argument, "--codex", str(fake)],
        cwd=home.parent,
        env={
            **os.environ,
            "FAKE_LOG": str(home.parent / "calls.jsonl"),
            "CODEX_HOME": "/conflicting/inherited/value",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert calls(home)[0]["home_env"] == argument
    assert updates(home) == []


def tree(root: Path) -> list[tuple[str, bool, bytes]]:
    return sorted(
        (
            str(path.relative_to(root)),
            path.is_symlink(),
            path.read_bytes() if path.is_file() and not path.is_symlink() else b"",
        )
        for path in root.rglob("*")
    )


def test_parent_traversal_in_current_is_refused_without_update_or_prune(home, fake):
    # The filesystem cannot resolve "not-present/.."; lexical collapsing would accept "keep".
    release(home, "keep")
    stale = release(home, "stale")
    select(home, "releases/not-present/../keep")
    result = run(home, fake)
    assert_warning(result, releases(home).parent / "current")
    assert "'..'" in result.stderr
    assert updates(home) == []
    assert (releases(home) / "keep").is_dir() and stale.is_dir()


def test_parent_traversal_through_alias_never_deletes_the_real_selection(home, fake):
    # current resolves to selected/keep through the alias; "keep" is only its lexical spelling.
    release(home, "keep")
    selected = releases(home) / "selected"
    release(home, "selected/subdir")
    release(home, "selected/keep")
    (releases(home) / "alias").symlink_to(selected / "subdir")
    select(home, "releases/alias/../keep")
    before = tree(releases(home))
    result = run(home, fake)
    assert_warning(result, releases(home).parent / "current")
    assert updates(home) == []
    assert tree(releases(home)) == before


@pytest.mark.parametrize("mode", ["remove-current", "publish-dangling", "publish-no-manifest"])
def test_update_without_usable_fresh_selection_cannot_prune(home, fake, mode):
    select(home, release(home, "old", "0.160.0"))
    release(home, "stale")
    result = run(home, fake, overrides={"FAKE_MODE": mode})
    assert_warning(result, releases(home).parent / "current")
    assert len(updates(home)) == 1
    assert (releases(home) / "old").is_dir()
    assert (releases(home) / "stale").is_dir()
    assert "removed" not in result.stdout


def test_manifest_recursion_failure_triggers_repair(home, fake, monkeypatch):
    select(home, release(home, "old", "0.160.0"))
    policy = module()
    real_read = policy.read_object
    monkeypatch.setenv("FAKE_LOG", str(home.parent / "calls.jsonl"))

    def read(path):
        if path.name == "codex-package.json" and path.parent.name == "old":
            raise RecursionError("injected manifest nesting failure")
        return real_read(path)

    monkeypatch.setattr(policy, "read_object", read)
    assert policy.main(["--codex-home", str(home), "--codex", str(fake)]) == 0
    assert len(updates(home)) == 1
    assert {p.name for p in releases(home).iterdir()} == {PINNED}


@pytest.mark.parametrize("grace_expires", [False, True])
def test_update_timeout_signal_grace_kill_and_reap_order(home, monkeypatch, grace_expires):
    policy = module()
    events = []

    class Process:
        pid = 123456

        def communicate(self, timeout=None):
            events.append(("communicate", timeout))
            if timeout == policy.UPDATE_TIMEOUT or (timeout and grace_expires):
                raise subprocess.TimeoutExpired("owned-fake", timeout)
            return b"", None

    def popen(argv, **kwargs):
        assert argv == ["fake-codex", "app-server", "daemon", "update", "--from-cli", "--yes"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["CODEX_HOME"] == str(home)
        return Process()

    def signal_group(pid, sig):
        assert pid == Process.pid
        events.append(("signal", sig))

    monkeypatch.setattr(policy.subprocess, "Popen", popen)
    monkeypatch.setattr(policy.os, "killpg", signal_group)
    instance = policy.Policy(home, "fake-codex")
    assert instance.update() is False
    assert instance.warned is True
    assert policy.VERSION_TIMEOUT == 30
    assert policy.UPDATE_TIMEOUT == 300
    assert 0 < policy.TERMINATE_GRACE < 5
    assert events == [
        ("communicate", 300),
        ("signal", signal.SIGTERM),
        ("communicate", policy.TERMINATE_GRACE),
        ("signal", signal.SIGKILL),
        ("communicate", None),
    ]


@pytest.mark.parametrize("version", ["0.160.0", "1.2.3"])
def test_installed_cli_version_is_the_pin(home, fake, version):
    select(home, release(home, "keep", version))
    release(home, "old")
    result = run(home, fake, overrides={"FAKE_VERSION": f"codex-cli {version}"})
    assert result.returncode == 0, result.stderr
    assert updates(home) == []
    assert {p.name for p in releases(home).iterdir()} == {"keep"}


def test_update_failure_without_output_still_warns(home, fake):
    select(home, release(home, "keep", "0.160.0"))
    release(home, "stale")
    result = run(home, fake, overrides={"FAKE_MODE": "silent-fail"})
    assert_warning(result, releases(home).parent / "current")
    assert "update exited 7" in result.stderr
    assert {p.name for p in releases(home).iterdir()} == {"keep"}


def test_helper_imports_only_standard_library():
    imports = set()
    for node in ast.walk(ast.parse(SCRIPT.read_text())):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imports.add((node.module or "").split(".")[0])
    assert imports <= sys.stdlib_module_names


def test_prune_recursion_failure_warns_without_traceback(home, fake, monkeypatch, capsys):
    select(home, release(home, "keep"))
    stale = release(home, "stale")
    policy = module()
    monkeypatch.setenv("FAKE_LOG", str(home.parent / "calls.jsonl"))

    def fail(*args, **kwargs):
        raise RecursionError("injected release nesting failure")

    monkeypatch.setattr(policy, "entry_size", fail)
    assert policy.main(["--codex-home", str(home), "--codex", str(fake)]) == 1
    captured = capsys.readouterr()
    assert "warning:" in captured.err and str(releases(home).parent / "current") in captured.err
    assert "injected release nesting failure" in captured.err
    assert "Traceback" not in captured.err
    assert stale.is_dir()
