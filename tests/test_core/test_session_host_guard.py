"""Host fallback guards use real inert Git repositories and never start an agent."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from djinn_in_a_box.config.models import AgentConfig
from djinn_in_a_box.core import session
from djinn_in_a_box.core.console import escape_nonprintable
from djinn_in_a_box.core.session import SessionManager, SessionTarget

REAL_RUN = subprocess.run
REFUSED = [
    ("codex", ".codex"),
    ("opencode", ".opencode"),
    ("opencode", "opencode.json"),
    ("opencode", "opencode.jsonc"),
    ("opencode", "tui.json"),
    ("opencode", "tui.jsonc"),
]


@pytest.fixture(autouse=True)
def _isolated_git_config(monkeypatch):
    # Real Git runs here; the developer's global config or template must not decide outcomes.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_TEMPLATE_DIR", raising=False)


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    REAL_RUN(["git", "init", "-q", "--template=", str(path)], check=True)


def config_value(workspace, key, value):
    REAL_RUN(["git", "config", "--file", str(workspace / ".git/config"), key, value], check=True)


def make_entry(path, kind):
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(path.parent / "missing-target")
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_text("entry")


@pytest.fixture
def launch(tmp_path, monkeypatch, request):
    mode = getattr(request, "param", "headless")
    agents = {
        name: AgentConfig(binary=f"fake-{name}")
        for name in ("claude", "codex", "opencode", "custom")
    }
    monkeypatch.setattr(session, "load_agents", lambda: agents)
    monkeypatch.setattr(session, "_HOST_SESSIONS_BASE", tmp_path / "sessions")
    manager = SessionManager("guard")
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[0] == "git":
            return REAL_RUN(cmd, **kwargs)
        assert cmd[0].startswith("fake-") or cmd[0] == session.DOCKER_EXECUTABLE
        return subprocess.CompletedProcess(cmd, 0, "agent output", "")

    monkeypatch.setattr(session.subprocess, "run", run)

    def invoke(workspace, agent="claude", container=False, **kwargs):
        target = SessionTarget("container" if container else None)
        if mode == "headless":
            return manager.run_headless(
                workspace_dir=workspace, agent=agent, prompt="prompt", target=target, **kwargs
            )
        return manager.run_interactive(
            workspace_dir=workspace, agent=agent, initial_prompt="prompt", target=target, **kwargs
        )

    invoke.calls = calls
    invoke.manager = manager
    invoke.run = run
    return invoke


def assert_refused(launch, workspace, result, offending, inits=0):
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.workspace_dir == workspace
    assert escape_nonprintable(str(workspace)) in result.stderr
    assert escape_nonprintable(str(offending)) in result.stderr
    assert result.stderr.endswith(
        "Run the session in the container (djinn start, then retry), "
        "or review and remove the entry on the host, then retry.\n"
    )
    assert all(cmd[0] == "git" for cmd, _ in launch.calls)
    assert sum(cmd[:2] == ["git", "init"] for cmd, _ in launch.calls) == inits


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("agent,name", REFUSED)
@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "fifo"])
@pytest.mark.parametrize("where", ["workspace", "parent", "sessions"])
def test_refused_agent_entries(launch, tmp_path, agent, name, kind, where):
    workspace = tmp_path / "sessions/parent/workspace"
    workspace.mkdir(parents=True)
    root = {
        "workspace": workspace,
        "parent": workspace.parent,
        "sessions": workspace.parent.parent,
    }[where]
    entry = root / name
    make_entry(entry, kind)
    assert_refused(launch, workspace, launch(workspace, agent), entry)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("name", [".git", "HEAD"])
@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
@pytest.mark.parametrize("where", ["parent", "sessions"])
def test_ancestor_git(launch, tmp_path, name, kind, where):
    workspace = tmp_path / "sessions/parent/workspace"
    workspace.mkdir(parents=True)
    parent = workspace.parent if where == "parent" else workspace.parent.parent
    make_entry(parent / name, kind)
    assert_refused(launch, workspace, launch(workspace), parent / name)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_workspace_root_head(launch, tmp_path, kind):
    # A root holding HEAD can stand in for a rejected .git during Git's discovery.
    workspace = tmp_path / "sessions/workspace"
    init_repo(workspace)
    make_entry(workspace / "HEAD", kind)
    assert_refused(launch, workspace, launch(workspace), workspace / "HEAD")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("relative", [False, True])
def test_workspace_redirected_outside_sessions(launch, tmp_path, relative):
    # A sessions entry replaced by a link into another writable tree skips no ancestor check.
    outside = tmp_path / "projects/x/ws"
    init_repo(outside)
    workspace = tmp_path / "sessions/proj"
    workspace.parent.mkdir(parents=True)
    target = Path("../projects/x/ws") if relative else outside
    workspace.symlink_to(target, target_is_directory=True)
    result = launch(workspace)
    assert_refused(launch, workspace, result, "does not resolve below")
    assert not any(cmd[:2] == ["git", "config"] for cmd, _ in launch.calls)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("through", ["link", "real"])
def test_workspace_redirected_with_linked_sessions_base(launch, tmp_path, through):
    # The configured base is a host-owned link; both spellings of the entry must be caught.
    real = tmp_path / "sessions-real"
    real.mkdir()
    (tmp_path / "sessions").symlink_to(real, target_is_directory=True)
    init_repo(tmp_path / "projects/x/ws")
    (real / "proj").symlink_to(Path("../projects/x/ws"), target_is_directory=True)
    workspace = tmp_path / ("sessions" if through == "link" else "sessions-real") / "proj"
    assert_refused(launch, workspace, launch(workspace), "does not resolve below")
    assert not any(cmd[:2] == ["git", "config"] for cmd, _ in launch.calls)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_workspace_redirected_to_sessions_root(launch, tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    workspace = sessions / "proj"
    workspace.symlink_to(Path("."), target_is_directory=True)
    assert_refused(launch, workspace, launch(workspace), "does not resolve below")
    assert not (sessions / ".git").exists()


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_workspace_link_inside_sessions(launch, tmp_path):
    real = tmp_path / "sessions/inner/real"
    init_repo(real)
    workspace = tmp_path / "sessions/link"
    workspace.symlink_to(real, target_is_directory=True)
    assert launch(workspace).success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("name", [".GIT", ".Git"])
def test_nested_repository_other_spelling(launch, tmp_path, name):
    init_repo(tmp_path)
    (tmp_path / "sub" / name).mkdir(parents=True)
    assert_refused(launch, tmp_path, launch(tmp_path), f"sub/{name}")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_remote_name_with_leading_slash(launch, tmp_path):
    # Git ignores such remote sections and treats branch.*.remote as a path instead.
    init_repo(tmp_path)
    config_value(tmp_path, "remote./srv/recv.url", "https://example.invalid/repo")
    config_value(tmp_path, "branch.main.remote", "/srv/recv")
    assert_refused(launch, tmp_path, launch(tmp_path), "remote./srv/recv.url")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_physical_ancestor(launch, tmp_path):
    physical = tmp_path / "sessions/physical/workspace"
    init_repo(physical.parent)
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    assert_refused(launch, alias, launch(alias), physical.parent / ".git")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_fresh_init_and_existing_repository(launch, tmp_path):
    result = launch(tmp_path)
    assert result.success
    assert [cmd for cmd, _ in launch.calls if cmd[:2] == ["git", "init"]] == [["git", "init", "-q"]]
    launch.calls.clear()
    assert launch(tmp_path).success
    assert not any(cmd[:2] == ["git", "init"] for cmd, _ in launch.calls)
    queries = [(cmd, options) for cmd, options in launch.calls if cmd[:2] == ["git", "config"]]
    assert len(queries) == 1
    cmd, options = queries[0]
    assert cmd == ["git", "config", "--file", str(tmp_path / ".git/config"), "--null", "--list"]
    assert options["cwd"] == "/" and options["stdin"] == subprocess.DEVNULL
    assert options["timeout"] == session._EXEC_TIMEOUT
    assert not options.get("text", False)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize(
    "key",
    [
        "core.fsmonitor",
        "include.path",
        "includeif.gitdir:example.path",
        "core.hookspath",
        "remote.x.uploadpack",
        "remote.x.receivepack",
        "core.sub.filemode",
        "core.pager",
        "core.editor",
        "core.sshcommand",
        "core.askpass",
        "core.worktree",
        "diff.external",
        "diff.x.textconv",
        "filter.x.clean",
        "filter.x.process",
        "merge.x.driver",
        "credential.helper",
        "gpg.program",
        "alias.x",
        "extensions.worktreeconfig",
        "user.signingkey",
        "x.core.filemode",
        "core.filemode.extra",
    ],
)
def test_forbidden_git_key(launch, tmp_path, key):
    init_repo(tmp_path)
    config_value(tmp_path, key, "refused-value")
    assert_refused(launch, tmp_path, launch(tmp_path), key)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_allowed_git_keys_and_valueless_record(launch, tmp_path):
    init_repo(tmp_path)
    with (tmp_path / ".git/config").open("a") as stream:
        stream.write("\n[core]\nignorecase=true\nprecomposeunicode=true\nsymlinks=false\n")
        stream.write("[extensions]\nobjectformat=sha1\nrefstorage=files\n")
        stream.write("[user]\nname=Example\nemail=example@example.test\n[core]\nbare\n")
    assert launch(tmp_path).success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("field", ["url", "pushurl"])
@pytest.mark.parametrize(
    "value",
    [
        "https://example.test/repo",
        "http://example.test/repo",
        "ssh://example.test/repo",
        "git://example.test/repo",
        "git@example.test:repo",
        "example.test:repo",
    ],
)
def test_network_destinations(launch, tmp_path, field, value):
    init_repo(tmp_path)
    if field == "pushurl":
        config_value(tmp_path, "remote.origin.url", "https://example.invalid/fetch")
    config_value(tmp_path, f"remote.origin.{field}", value)
    config_value(tmp_path, "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    config_value(tmp_path, "branch.main.remote", "origin")
    config_value(tmp_path, "branch.main.merge", "refs/heads/main")
    assert launch(tmp_path).success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("field", ["url", "pushurl", "branch"])
@pytest.mark.parametrize(
    "value",
    [
        "ext::helper",
        "fd::3",
        "unknown://example.test/repo",
        "file:///tmp/receiver",
        "/tmp/receiver",
        "../receiver",
        "./receiver",
        "receiver",
        "a/b:repo",
        "",
        "https://example.test/a::b",
        "user@host:two words",
    ],
)
def test_refused_destinations(launch, tmp_path, field, value):
    init_repo(tmp_path)
    key = "branch.main.remote" if field == "branch" else f"remote.origin.{field}"
    config_value(tmp_path, key, value)
    result = launch(tmp_path)
    assert_refused(launch, tmp_path, result, key)
    assert repr(value) in result.stderr


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("remote", [".", "origin"])
def test_branch_remote(launch, tmp_path, remote):
    init_repo(tmp_path)
    config_value(tmp_path, "remote.origin.url", "https://example.invalid/repo")
    config_value(tmp_path, "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    config_value(tmp_path, "branch.main.remote", remote)
    assert launch(tmp_path).success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize(
    "keys",
    [
        {"remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*"},
        {
            "remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*",
            "branch.main.remote": "origin",
            "branch.main.merge": "refs/heads/main",
        },
        {"remote.origin.pushurl": "https://example.invalid/repo"},
    ],
    ids=["fetch-only", "fetch-only-branch", "pushurl-only"],
)
def test_remote_without_url(launch, tmp_path, keys):
    # Git would use the remote name as a local destination, e.g. a receiver at ./origin.
    init_repo(tmp_path)
    for key, value in keys.items():
        config_value(tmp_path, key, value)
    assert_refused(launch, tmp_path, launch(tmp_path), "'origin' without url")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_invalid_utf8_in_allowed_value(launch, tmp_path):
    init_repo(tmp_path)
    with (tmp_path / ".git/config").open("ab") as stream:
        stream.write(b"\n[user]\n name = reviewer\xff\n")
    assert_refused(launch, tmp_path, launch(tmp_path), "cannot decode")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize(
    "entry,kind",
    [
        (".git", "file"),
        (".git", "symlink"),
        ("HEAD", "missing"),
        ("HEAD", "symlink"),
        ("HEAD", "directory"),
        ("objects", "missing"),
        ("objects", "symlink"),
        ("objects", "file"),
        ("refs", "missing"),
        ("refs", "symlink"),
        ("refs", "file"),
        ("config", "missing"),
        ("config", "symlink"),
        ("config", "fifo"),
        ("config", "directory"),
        ("commondir", "file"),
        ("commondir", "symlink"),
        ("commondir", "directory"),
        ("modules", "file"),
        ("modules", "symlink"),
        ("modules", "directory"),
        ("hooks", "symlink"),
        ("hooks", "file"),
    ],
)
def test_repository_shape(launch, tmp_path, entry, kind):
    init_repo(tmp_path)
    path = tmp_path / ".git" if entry == ".git" else tmp_path / ".git" / entry
    if path.exists():
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    if kind != "missing":
        make_entry(path, kind)
    assert_refused(launch, tmp_path, launch(tmp_path), path)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_active_hook(launch, tmp_path, kind):
    init_repo(tmp_path)
    hooks = tmp_path / ".git/hooks"
    hooks.mkdir()
    make_entry(hooks / "pre-commit", kind)
    assert_refused(launch, tmp_path, launch(tmp_path), hooks / "pre-commit")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_sample_hooks_and_empty_carriers(launch, tmp_path):
    init_repo(tmp_path)
    hooks = tmp_path / ".git/hooks"
    hooks.mkdir()
    make_entry(hooks / "pre-commit.sample", "symlink")
    for name in ("remotes", "branches"):
        (tmp_path / ".git" / name).mkdir(exist_ok=True)
    assert launch(tmp_path).success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("carrier", ["remotes", "branches"])
@pytest.mark.parametrize("kind", ["file", "symlink", "nonempty", "push"])
def test_legacy_destinations(launch, tmp_path, carrier, kind):
    workspace = tmp_path / "workspace"
    receiver = tmp_path / "receiver"
    init_repo(workspace)
    REAL_RUN(["git", "init", "--bare", "-q", "--template=", str(receiver)], check=True)
    marker = tmp_path / "push-marker"
    hooks = receiver / "hooks"
    hooks.mkdir()
    (hooks / "pre-receive").write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    (hooks / "pre-receive").chmod(0o700)
    path = workspace / ".git" / carrier
    if path.exists():
        path.rmdir()
    if kind in ("file", "symlink"):
        make_entry(path, kind)
        offending = path
    else:
        path.mkdir()
        offending = path / "origin"
        content = f"URL: {receiver}\n" if carrier == "remotes" else str(receiver)
        if kind == "push":
            content += "\nPush: refs/heads/main:refs/heads/main\n"
        offending.write_text(content)
    assert_refused(launch, workspace, launch(workspace), offending)
    assert not marker.exists()


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_incomplete_child_never_discovers_parent(launch, tmp_path):
    parent = tmp_path / "sessions/parent"
    init_repo(parent)
    marker = tmp_path / "fsmonitor-marker"
    config_value(parent, "core.fsmonitor", f"touch {marker}")
    workspace = parent / "workspace"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git/config").write_text("[core]\nbare=false\n")
    assert_refused(launch, workspace, launch(workspace), parent / ".git")
    assert not marker.exists()
    # Outside the sessions root, the incomplete child is still refused by its own shape.
    launch.calls.clear()
    with patch.object(session, "_HOST_SESSIONS_BASE", tmp_path / "elsewhere"):
        assert_refused(launch, workspace, launch(workspace), workspace / ".git/HEAD")
    assert not marker.exists()


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_nested_repository(launch, tmp_path, kind):
    init_repo(tmp_path)
    parent = tmp_path / "one/two/three"
    parent.mkdir(parents=True)
    entry = parent / ".git"
    make_entry(entry, kind)
    assert_refused(launch, tmp_path, launch(tmp_path), entry)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_symlink_directory_not_followed(launch, tmp_path):
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    init_repo(workspace)
    init_repo(other)
    (workspace / "link").symlink_to(other, target_is_directory=True)
    assert launch(workspace).success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_walk_entry_limit(launch, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    init_repo(workspace)
    (workspace / "file").touch()
    (workspace / "dir").mkdir()
    monkeypatch.setattr(session, "_HOST_WALK_MAX_ENTRIES", 1)
    assert_refused(launch, workspace, launch(workspace), "entry limit")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_walk_time_limit(launch, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    init_repo(workspace)
    monkeypatch.setattr(session, "_HOST_WALK_TIMEOUT", 5)
    ticks = iter([0, 6])
    monkeypatch.setattr(session.time, "monotonic", lambda: next(ticks))
    assert_refused(launch, workspace, launch(workspace), "time limit")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("error", [errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EIO])
@pytest.mark.parametrize("at_root", [False, True])
def test_walk_onerror(launch, tmp_path, monkeypatch, error, at_root):
    init_repo(tmp_path)
    failed = tmp_path if at_root else tmp_path / "vanished"

    def walk(root, *, onerror, followlinks):
        assert root == tmp_path and followlinks is False
        onerror(OSError(error, os.strerror(error), str(failed)))
        yield str(root), [".git"], []

    monkeypatch.setattr(session.os, "walk", walk)
    result = launch(tmp_path)
    if error in (errno.ENOENT, errno.ENOTDIR) and not at_root:
        assert result.success
    else:
        assert_refused(launch, tmp_path, result, failed)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("kind", ["missing", "file"])
def test_walk_root_must_remain(launch, tmp_path, monkeypatch, kind):
    workspace = tmp_path / "workspace"
    init_repo(workspace)

    def walk(root, **kwargs):
        yield str(root), [".git"], []
        shutil.rmtree(workspace)
        if kind == "file":
            workspace.touch()

    monkeypatch.setattr(session.os, "walk", walk)
    assert_refused(launch, workspace, launch(workspace), workspace)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_unreadable_subdirectory(launch, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions")
    init_repo(tmp_path)
    child = tmp_path / "unreadable"
    child.mkdir()
    child.chmod(0)
    try:
        assert_refused(launch, tmp_path, launch(tmp_path), child)
    finally:
        child.chmod(0o700)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("failure", ["partial", "utf8", "missing", "timeout", "oserror"])
def test_git_inspection_failure(launch, tmp_path, monkeypatch, failure):
    init_repo(tmp_path)

    def run(cmd, **kwargs):
        launch.calls.append((cmd, kwargs))
        assert cmd[:2] == ["git", "config"]
        if failure == "partial":
            return subprocess.CompletedProcess(cmd, 128, b"core.bare\nfalse\0", b"bad config")
        if failure == "utf8":
            return subprocess.CompletedProcess(cmd, 0, b"core.ba\xffre\nfalse\0", b"")
        if failure == "missing":
            raise FileNotFoundError(errno.ENOENT, "git missing", "git")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 10)
        raise OSError(errno.EIO, "inspection failed", str(tmp_path / ".git/config"))

    monkeypatch.setattr(session.subprocess, "run", run)
    assert_refused(launch, tmp_path, launch(tmp_path), "git" if failure == "missing" else "config")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("failure", ["rc", "valid-shape-rc", "missing", "oserror", "post-init"])
def test_git_init_failure(launch, tmp_path, monkeypatch, failure):
    def run(cmd, **kwargs):
        launch.calls.append((cmd, kwargs))
        assert cmd == ["git", "init", "-q"]
        if failure == "missing":
            raise FileNotFoundError(errno.ENOENT, "git missing", "git")
        if failure == "oserror":
            raise OSError(errno.EIO, "git init failed", str(tmp_path))
        if failure in ("valid-shape-rc", "post-init"):
            init_repo(tmp_path)
        if failure == "post-init":
            (tmp_path / ".git/modules").mkdir()
        return subprocess.CompletedProcess(cmd, 0 if failure == "post-init" else 1, b"", b"")

    monkeypatch.setattr(session.subprocess, "run", run)
    assert_refused(
        launch, tmp_path, launch(tmp_path), "modules" if failure == "post-init" else "git", inits=1
    )


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_git_init_failure_warning_is_escaped(launch, tmp_path, monkeypatch, caplog):
    workspace = tmp_path / "control\x1b[2Jname"
    workspace.mkdir()

    def run(cmd, **kwargs):
        launch.calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 1, b"", b"")

    monkeypatch.setattr(session.subprocess, "run", run)
    with caplog.at_level("WARNING", logger=session.__name__):
        assert_refused(launch, workspace, launch(workspace), "git init failed", inits=1)
    assert "Failed to initialize git" in caplog.text
    assert "\x1b" not in caplog.text
    assert "\\x1b" in caplog.text


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("which", ["workspace", "sessions"])
@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_resolution_failure(launch, tmp_path, monkeypatch, which, error_type):
    original = Path.resolve
    selected = tmp_path if which == "workspace" else session._HOST_SESSIONS_BASE

    def resolve(path, *args, **kwargs):
        if path == selected:
            raise error_type(f"resolution failed for {path}")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    original_realpath = os.path.realpath

    def realpath(path, **kwargs):
        if Path(path) == selected:
            raise error_type(f"resolution failed for {path}")
        return original_realpath(path, **kwargs)

    monkeypatch.setattr(session.os.path, "realpath", realpath)
    assert_refused(launch, tmp_path, launch(tmp_path), selected)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_lstat_error(launch, tmp_path, monkeypatch):
    original = Path.lstat
    entry = tmp_path / ".codex"

    def lstat(path, *args, **kwargs):
        if path == entry:
            raise PermissionError(errno.EACCES, "lstat failed", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    assert_refused(launch, tmp_path, launch(tmp_path, "codex"), entry)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_unknown_host_agent_and_container_bypass(launch, tmp_path):
    assert_refused(launch, tmp_path, launch(tmp_path, "custom"), "custom")
    launch.calls.clear()
    (tmp_path / ".codex").touch()
    assert launch(tmp_path, "custom", container=True).success
    assert len(launch.calls) == 1
    assert launch(tmp_path, "codex", container=True).success
    with pytest.raises(ValueError, match="Unknown agent"):
        launch(tmp_path, "not-configured")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("agent", ["claude", "codex", "opencode"])
def test_host_flags_and_environment(launch, tmp_path, monkeypatch, agent):
    init_repo(tmp_path)
    # The agent key, rather than the executable name, selects protection.
    launch.manager._agents[agent] = AgentConfig(
        binary="fake-agent",
        headless_flags=["run", "--setting-sources", "user,project"],
        write_flags=["--write", "--setting-sources", "user,project"],
        default_model="default-model",
    )
    monkeypatch.setenv("OPENCODE_DISABLE_PROJECT_CONFIG", "inherited")
    monkeypatch.setattr(
        session,
        "_SESSION_ENV",
        {**session._SESSION_ENV, "OPENCODE_DISABLE_PROJECT_CONFIG": "terminal"},
    )
    additions = {"OPENCODE_DISABLE_PROJECT_CONFIG": "caller"}
    original = additions.copy()
    assert launch(tmp_path, agent, env=additions, model="chosen-model").success
    cmd, options = launch.calls[-1]
    if agent == "claude":
        assert cmd[-3:] == ["--setting-sources", "user", "prompt"]
        assert cmd.index("chosen-model") < len(cmd) - 3
        assert cmd.index("user,project") < len(cmd) - 3
    else:
        assert cmd[-1] == "prompt" and "user" not in cmd
    assert options["cwd"] == tmp_path
    expected = "1" if agent == "opencode" else "terminal"
    assert options["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"] == expected
    assert additions == original


@pytest.mark.parametrize("mode", ["interactive", "headless"])
def test_claude_builders_without_prompt_and_container_unchanged(launch, mode):
    config = AgentConfig(
        binary="fake-claude",
        write_flags=["--write"],
        headless_flags=["-p"],
        default_model="default",
    )
    launch.manager._agents["claude"] = config
    if mode == "interactive":
        cmd = launch.manager._build_host_interactive_command(config, None, None)
        assert cmd == [config.binary, "--model", "default", "--write", "--setting-sources", "user"]
        container = launch.manager._build_interactive_command(config, None, None)
        assert container == f"{config.binary} --model default --write"
    else:
        cmd = launch.manager._build_host_headless_command(config, None, "prompt")
        assert cmd == [
            config.binary,
            "-p",
            "--model",
            "default",
            "--setting-sources",
            "user",
            "prompt",
        ]


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_guard_git_query_executes_no_configured_code(launch, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir()
    init_repo(workspace)
    markers = [tmp_path / name for name in ("fsmonitor", "pager", "hook")]
    script = tmp_path / "marker.sh"
    script.write_text("#!/bin/sh\n" + "\n".join(f"touch '{marker}'" for marker in markers) + "\n")
    script.chmod(0o700)
    (home / ".gitconfig").write_text(
        f"[core]\nfsmonitor={script}\npager={script}\nhooksPath={tmp_path}\n"
    )
    (tmp_path / "post-checkout").symlink_to(script)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_PAGER", str(script))
    config_value(workspace, "core.fsmonitor", str(script))
    assert_refused(launch, workspace, launch(workspace), "core.fsmonitor")
    assert not any(marker.exists() for marker in markers)
    launch.calls.clear()
    REAL_RUN(
        ["git", "config", "--file", str(workspace / ".git/config"), "--unset", "core.fsmonitor"],
        check=True,
    )
    assert launch(workspace).success
    assert not any(marker.exists() for marker in markers)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize(
    "entry", [".git", "HEAD", "objects", "refs", "config", "hooks", "remotes", "branches"]
)
def test_repository_real_symlink_refused(launch, tmp_path, entry):
    workspace = tmp_path / "workspace"
    init_repo(workspace)
    path = workspace / ".git" if entry == ".git" else workspace / ".git" / entry
    target = tmp_path / "target"
    if path.exists():
        path.rename(target)
    elif entry in ("hooks", "remotes", "branches"):
        target.mkdir()
    path.symlink_to(target, target_is_directory=target.is_dir())
    assert_refused(launch, workspace, launch(workspace), path)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_refusal_order_before_init(launch, tmp_path, monkeypatch):
    workspace = tmp_path / "sessions/parent/workspace"
    workspace.mkdir(parents=True)
    (workspace / ".codex").touch()
    (workspace.parent / ".git").touch()
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == workspace:
            raise AssertionError("workspace config must be checked before resolution")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert_refused(launch, workspace, launch(workspace, "codex"), workspace / ".codex")
    launch.calls.clear()
    with patch.object(Path, "lstat", side_effect=AssertionError("agent support must be first")):
        assert_refused(launch, workspace, launch(workspace, "custom"), "custom")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_walk_time_limit_inside_directory(launch, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    init_repo(workspace)
    (workspace / "file").touch()
    ticks = iter([0, 0, 6])
    monkeypatch.setattr(session.time, "monotonic", lambda: next(ticks))
    assert_refused(launch, workspace, launch(workspace), "time limit")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_vanishing_real_entry(launch, tmp_path, monkeypatch):
    init_repo(tmp_path)
    child = tmp_path / "vanishing"
    child.mkdir()
    walk = os.walk

    def vanishing(root, **kwargs):
        for item in walk(root, **kwargs):
            if Path(item[0]) == tmp_path:
                child.rmdir()
            yield item

    monkeypatch.setattr(session.os, "walk", vanishing)
    assert launch(tmp_path).success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("entry", ["hooks", "remotes", "branches"])
def test_directory_listing_error(launch, tmp_path, monkeypatch, entry):
    init_repo(tmp_path)
    selected = tmp_path / ".git" / entry
    selected.mkdir(exist_ok=True)
    original = Path.iterdir

    def iterdir(path):
        if path == selected:
            raise OSError(errno.EIO, "listing failed", str(path))
        return original(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    assert_refused(launch, tmp_path, launch(tmp_path), selected)


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_resolved_workspace_used_for_checks_and_init(launch, tmp_path):
    workspace = tmp_path / "physical"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    result = launch(alias)
    assert result.success and result.workspace_dir == alias
    assert launch.calls[0][1]["cwd"] == workspace
    assert launch.calls[1][0][3] == str(workspace / ".git/config")
    assert launch.calls[-1][1]["cwd"] == alias


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("which", ["workspace", "sessions"])
def test_symlink_resolution_loop(launch, tmp_path, monkeypatch, which):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    workspace = loop if which == "workspace" else tmp_path
    if which == "sessions":
        monkeypatch.setattr(session, "_HOST_SESSIONS_BASE", loop)
    with patch.object(session, "_host_lstat", wraps=session._host_lstat) as lookup:
        assert_refused(launch, workspace, launch(workspace), "loop")
    lookup.assert_not_called()


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("entries", [250_000, 250_001])
def test_default_walk_entry_budget(launch, tmp_path, monkeypatch, entries):
    init_repo(tmp_path)
    monkeypatch.setattr(session.time, "monotonic", lambda: 0)

    def walk(root, **kwargs):
        yield str(root), [".git"], [f"file-{number}" for number in range(entries)]

    monkeypatch.setattr(session.os, "walk", walk)
    result = launch(tmp_path)
    if entries == 250_000:
        assert result.success
    else:
        assert_refused(launch, tmp_path, result, "entry limit")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("elapsed", [5, 5.01])
def test_default_walk_time_budget(launch, tmp_path, monkeypatch, elapsed):
    workspace = tmp_path / "workspace"
    init_repo(workspace)
    ticks = iter([0])
    monkeypatch.setattr(session.time, "monotonic", lambda: next(ticks, elapsed))
    result = launch(workspace)
    if elapsed == 5:
        assert result.success
    else:
        assert_refused(launch, workspace, result, "time limit")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_physical_sessions_root(launch, tmp_path, monkeypatch):
    physical = tmp_path / "physical"
    workspace = physical / "parent/workspace"
    workspace.mkdir(parents=True)
    (physical / "parent/.codex").touch()
    alias = tmp_path / "sessions-alias"
    alias.symlink_to(physical, target_is_directory=True)
    monkeypatch.setattr(session, "_HOST_SESSIONS_BASE", alias)
    assert_refused(launch, workspace, launch(workspace, "codex"), physical / "parent/.codex")


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_outside_sessions_ancestors_trusted(launch, tmp_path):
    init_repo(tmp_path)
    (tmp_path / ".codex").touch()
    workspace = tmp_path / "workspace"
    init_repo(workspace)
    assert launch(workspace, "codex").success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_sessions_root_itself_is_not_its_ancestor(launch, tmp_path, monkeypatch):
    init_repo(tmp_path)
    monkeypatch.setattr(session, "_HOST_SESSIONS_BASE", tmp_path)
    assert launch(tmp_path).success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_dotted_remote_and_branch_names(launch, tmp_path):
    init_repo(tmp_path)
    config_value(tmp_path, "branch.feature.example.remote", "example.remote")
    config_value(tmp_path, "remote.example.remote.url", "https://example.test/repo")
    assert launch(tmp_path).success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
@pytest.mark.parametrize("entry", ["directory", "file"])
def test_top_git_entry_is_not_nested(launch, tmp_path, monkeypatch, entry):
    init_repo(tmp_path)

    def walk(root, **kwargs):
        dirs = [".git"] if entry == "directory" else []
        files = [".git"] if entry == "file" else []
        yield str(root), dirs, files

    monkeypatch.setattr(session.os, "walk", walk)
    assert launch(tmp_path).success


@pytest.mark.parametrize("launch", ["interactive", "headless"], indirect=True)
def test_init_active_template_hook_refused(launch, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    template = tmp_path / "template"
    (template / "hooks").mkdir(parents=True)
    marker = tmp_path / "hook-marker"
    hook = template / "hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o700)
    (home / ".gitconfig").write_text(f"[init]\ntemplateDir={template}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    assert_refused(
        launch, workspace, launch(workspace), workspace / ".git/hooks/post-checkout", inits=1
    )
    assert not marker.exists()
