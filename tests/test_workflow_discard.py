from __future__ import annotations

import json
import multiprocessing
import os
import pwd
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import _lifecycle
from djinn_in_a_box.commands import config as command
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig
from djinn_in_a_box.core import config_sync as sync
from djinn_in_a_box.core import config_workflow as workflow
from djinn_in_a_box.core import host_runtime, hostctl, workflow_drift_backup
from djinn_in_a_box.core.docker import WorkflowImageCompatibility
from djinn_in_a_box.core.ssh_delivery import GitSSHError
from djinn_in_a_box.core.workflow_publisher import (
    CANONICAL_MANIFEST_NAME,
    RUNTIME_MANIFEST_NAME,
    BackedUpState,
)


def replace_file(path: Path, content: bytes) -> None:
    path.unlink(missing_ok=True)
    path.write_bytes(content)


def tree(root: Path) -> dict:
    """Include directory entries, modes, symlinks and bytes without following aliases."""
    result = {}
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(parent) / name
            entry = path.lstat()
            value = (os.readlink(path) if path.is_symlink()
                     else path.read_bytes() if stat.S_ISREG(entry.st_mode) else None)
            result[str(path.relative_to(root))] = (entry.st_mode, value)
    return result


@pytest.fixture
def discard_case(tmp_path, monkeypatch):
    project, home, code = (tmp_path / name for name in ("project", "home", "code"))
    home.mkdir(mode=0o700)
    code.mkdir()
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
    (project / "config/claude/AGENTS.md").write_bytes(b"original\n")
    config = AppConfig(code_dir=code, config_root=tmp_path / "runtime")
    case = SimpleNamespace(project=project, home=home, config=config, running=[], tmp=tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    # Undo the legacy Compose fixture: exercise the real guard-created home chain.
    def runtime_root(*, create=False):
        root = home / ".local/state/djinn/runtime/git-agent"
        if create:
            root.parent.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            host_runtime.private_directory(root.parent)
            host_runtime.private_directory(root)
        return root

    monkeypatch.setattr(host_runtime, "runtime_root", runtime_root)
    for module in (sync, workflow, command):
        monkeypatch.setattr(module, "load_config", lambda config_path=None: case.config)
    monkeypatch.setattr(command, "get_project_root", lambda: project)
    monkeypatch.setattr(workflow, "workflow_image_compatible",
                        lambda: WorkflowImageCompatibility.COMPATIBLE)
    monkeypatch.setattr(workflow, "ensure_host_env", lambda config: None)
    monkeypatch.setattr(_lifecycle, "get_running_containers", lambda **kwargs: case.running)
    case.invoke = lambda: CliRunner().invoke(app, ["config", "sync", "--discard-target-drift"])
    case.runtime = config.config_root / "codex"
    case.runtime.mkdir(parents=True)
    assert sync.sync_config(project).success
    assert workflow.prepare_config_workflow(
        project, workflow.start_workflow_targets(config), require_compose_host_env=True
    ).success
    assert not (home / ".local").exists()
    return case


def drift(case, *, canonical=True, runtime=True, source=True):
    if source:
        (case.project / "config/claude/AGENTS.md").write_bytes(b"source update\n")
    if canonical:
        replace_file(case.project / "config/opencode/AGENTS.md", b"canonical edit\n")
    if runtime:
        replace_file(case.runtime / "AGENTS.md", b"runtime edit\n")


def roots(case):
    return tree(case.project / "config"), tree(case.config.config_root)


def backup_stamp(case):
    stamps = list((case.home / ".local/state/djinn/workflow-drift").iterdir())
    assert len(stamps) == 1
    return stamps[0]


def test_barrier_guard_lock_handover_and_source_changed(discard_case, monkeypatch):
    case = discard_case
    drift(case)
    events, fsynced = [], []
    backup = workflow.backup_target_drift
    sync_config = workflow.sync_config
    delivery = workflow._publish_delivery
    fsync = os.fsync
    lease_depth = 0
    lock = workflow.canonical_lock

    @contextmanager
    def tracked_lock(*args, **kwargs):
        nonlocal lease_depth
        assert lease_depth == 0
        with lock(*args, **kwargs) as lease:
            lease_depth += 1
            try:
                yield lease
            finally:
                lease_depth -= 1

    def tracked_fsync(fd):
        fsynced.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
        return fsync(fd)

    def guard_held():
        assert hostctl._held.active
        with (
            pytest.raises(GitSSHError, match="creation is pending"),
            host_runtime.creation_guard(),
        ):
            pytest.fail("creation admitted during discard")

    def tracked_backup(*args, **kwargs):
        guard_held()
        if args[2] == case.project / "config":
            assert kwargs["canonical_lease"].exclusive
            assert "preflight_manifest" in kwargs
            assert lease_depth == 1
        else:
            assert lease_depth == 0
        entries = backup(*args, **kwargs)
        events.append(("backup", args[2]))
        return entries

    def tracked_sync(*args, **kwargs):
        guard_held()
        assert lease_depth == 0
        assert events == [("backup", case.project / "config"), ("backup", case.runtime)]
        names = (".local", "state", "djinn", "workflow-drift")
        chain = [case.home, *(case.home.joinpath(*names[:i]) for i in range(1, 5))]
        stamp = backup_stamp(case)
        assert fsynced[-8:] == [stamp / "codex", stamp / "config", stamp, *reversed(chain)]
        events.append(("sync", None))
        assert kwargs["accept_drift"]
        return sync_config(*args, **kwargs)

    def tracked_delivery(*args, **kwargs):
        guard_held()
        assert events[-1][0] == "sync"
        assert kwargs["accept_drift"]
        result = delivery(*args, **kwargs)
        guard_held()  # Covers release before the last publish completes.
        return result

    monkeypatch.setattr(workflow, "canonical_lock", tracked_lock)
    monkeypatch.setattr(workflow, "backup_target_drift", tracked_backup)
    monkeypatch.setattr(workflow, "sync_config", tracked_sync)
    monkeypatch.setattr(workflow, "_publish_delivery", tracked_delivery)
    monkeypatch.setattr(os, "fsync", tracked_fsync)
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert "config/opencode/AGENTS.md: backed up" in result.output
    assert "codex:AGENTS.md" in result.output and "Backed up to" in result.output
    stamp = backup_stamp(case)
    assert (stamp / "config/opencode/AGENTS.md").read_bytes() == b"canonical edit\n"
    assert (stamp / "codex/AGENTS.md").read_bytes() == b"runtime edit\n"
    assert (case.runtime / "AGENTS.md").read_bytes() == b"source update\n"
    assert (case.runtime / "AGENTS.md").stat().st_mode & 0o777 == 0o444
    assert not hostctl._held.active


@pytest.mark.parametrize("source_switch", [False, True])
def test_no_drift_publishes_like_plain_sync_without_backup(discard_case, source_switch):
    case = discard_case
    if source_switch:
        case.config = case.config.model_copy(
            update={"config_sync": ConfigSyncConfig(source="codex")}
        )
        (case.project / "config/codex/AGENTS.md").write_bytes(b"original\n")
    else:
        (case.project / "config/claude/AGENTS.md").write_bytes(b"source only\n")
    runtime = tree(case.config.config_root)
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert "no target drift" in result.output
    assert not (case.home / ".local/state/djinn/workflow-drift").exists()
    assert tree(case.config.config_root) == runtime  # Plain sync does not deliver runtime roots.
    assert sync.audit_config_sync(case.project).clean
    before = tree(case.project / "config")
    assert sync.sync_config(case.project).success
    assert tree(case.project / "config") == before


def test_source_switch_backup_set_equals_publish_set(discard_case, monkeypatch):
    case = discard_case
    case.config = case.config.model_copy(update={"config_sync": ConfigSyncConfig(source="codex")})
    (case.project / "config/codex/AGENTS.md").write_bytes(b"new selected source\n")
    (case.project / "config/claude/AGENTS.md").write_bytes(b"new selected source\n")
    drift(case, source=False)
    calls = []
    backup = workflow.backup_target_drift

    def traced(*args, **kwargs):
        if args[2] == case.project / "config":
            assert kwargs.get("preflight_manifest") is not None
        result = backup(*args, **kwargs)
        calls.extend(str(e.entry.path) for e in result)
        return result

    monkeypatch.setattr(workflow, "backup_target_drift", traced)
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert "codex/AGENTS.md" not in calls  # It is source-owned, never retired or overwritten.
    assert (case.project / "config/codex/AGENTS.md").read_bytes() == b"new selected source\n"
    assert sync.audit_config_sync(case.project).clean


@pytest.mark.parametrize("state", ["running", "unreachable", "timeout", "pending"])
def test_precondition_refuses_without_workflow_writes(discard_case, monkeypatch, state):
    case = discard_case
    drift(case)
    before = roots(case)
    fd = None
    if state == "pending":
        fd = host_runtime.acquire_creation_lock(host_runtime.runtime_root(create=True))
        monkeypatch.setattr(_lifecycle, "get_running_containers",
                            lambda **kwargs: pytest.fail("probe preceded guard"))
    elif state == "running":
        case.running = ["djinn-test"]
    else:
        from djinn_in_a_box.core import docker

        def daemon(argv, **kwargs):
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert 0 < kwargs["timeout"] <= 5
            if state == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(
                argv, 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                "Is the docker daemon running?\n",
            )

        monkeypatch.setattr(docker.subprocess, "run", daemon)
        monkeypatch.setattr(_lifecycle, "get_running_containers", docker.get_running_containers)
    try:
        result = case.invoke()
    finally:
        if fd is not None:
            os.close(fd)
    assert result.exit_code == 1, result.output
    assert roots(case) == before
    assert not (case.home / ".local/state/djinn/workflow-drift").exists()
    if state == "running":
        assert "Stop all containers first: `djinn clean`" in result.output
    elif state == "pending":
        assert "creation is pending" in result.output
    else:
        assert "Restore Docker access before discarding target drift" in result.output


@pytest.mark.parametrize("component", [".local", ".local/state", ".local/state/djinn",
                                        ".local/state/djinn/workflow-drift"])
@pytest.mark.parametrize("kind", ["symlink", "foreign", "file"])
def test_admission_unchanged_before_guard(discard_case, monkeypatch, component, kind):
    case = discard_case
    drift(case)
    path = case.home / component
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if kind == "symlink":
        outside = case.tmp / "outside"
        outside.mkdir()
        path.symlink_to(outside, target_is_directory=True)
    elif kind == "file":
        path.write_bytes(b"not a directory")
    else:
        path.mkdir(mode=0o700)
        original = Path.lstat

        def foreign(self, *args, **kwargs):
            entry = original(self, *args, **kwargs)
            if self == path:
                values = list(entry)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return entry

        monkeypatch.setattr(Path, "lstat", foreign)
    before = tree(case.tmp)
    result = case.invoke()
    assert result.exit_code == 1, result.output
    assert str(path) in result.output
    assert "Replace" in result.output or "ownership" in result.output
    assert tree(case.tmp) == before
    assert not (case.home / ".local/state/djinn/hostctl").exists()


@pytest.mark.parametrize("component", ["djinn", "workflow-drift"])
def test_admission_private_mode_is_not_repaired(discard_case, component):
    case = discard_case
    path = case.home / ".local/state/djinn"
    if component == "workflow-drift":
        path /= component
    path.mkdir(parents=True, mode=0o700)
    (case.home / ".local/state/djinn").chmod(0o700)
    path.chmod(0o750)
    before = tree(case.tmp)
    result = case.invoke()
    assert result.exit_code == 1 and "0700" in result.output
    assert str(path) in result.output
    assert tree(case.tmp) == before


@pytest.mark.parametrize("kind", ["installation", "config", "shared", "local", "code", "sessions",
                                   "bind", "config-ancestor", "zone-ancestor"])
def test_overlap_admission_does_not_create_guard_state(discard_case, monkeypatch, kind):
    case = discard_case
    backup = case.home / ".local/state/djinn/workflow-drift"
    updates = {}
    if kind == "installation":
        monkeypatch.setattr(command, "get_project_root", lambda: case.home)
    elif kind in {"config", "shared", "local"}:
        updates[f"{kind}_root"] = backup / "protected"
        if kind == "config":
            updates["shared_root"] = case.tmp / "separate-shared"
            updates["local_root"] = case.tmp / "separate-local"
    elif kind == "code":
        updates["code_dir"] = case.home
    elif kind == "sessions":
        (case.home / ".djinn").mkdir()
        (case.home / ".djinn/sessions").symlink_to(case.home, target_is_directory=True)
    elif kind == "bind":
        from djinn_in_a_box.config.declarations import BindDeclaration
        updates["mounts"] = {"data": BindDeclaration(source=str(case.home), target="/data")}
    elif kind == "config-ancestor":
        updates["config_root"] = case.home
    else:
        updates["shared_root"] = case.home
    case.config = case.config.model_copy(update=updates)
    before = tree(case.tmp)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert "overlaps" in result.output and "reconfigure" in result.output
    assert tree(case.tmp) == before
    assert not (case.home / ".local").exists()


def test_symlinked_passwd_home_and_umask_022(discard_case, monkeypatch):
    case = discard_case
    alias = case.tmp / "home-alias"
    alias.symlink_to(case.home, target_is_directory=True)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(alias)))
    drift(case)
    old = os.umask(0o022)
    try:
        result = case.invoke()
    finally:
        os.umask(old)
    assert result.exit_code == 0, (result.output, result.exception)
    stamp = backup_stamp(case)
    assert str(case.home / ".local/state/djinn/workflow-drift") in result.output
    assert (stamp.parent.stat().st_mode & 0o777) == 0o700
    for path in [stamp, *stamp.rglob("*")]:
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize("component", ["home", ".local", "state", "djinn", "workflow-drift",
                                        "stamp", "config", "codex"])
def test_any_chain_fsync_failure_aborts_before_publish(discard_case, monkeypatch, component):
    case = discard_case
    drift(case)
    before = roots(case)
    fsync = os.fsync
    hit = []

    def fault(fd):
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        is_stamp = path.parent.name == "workflow-drift"
        matches = (path == case.home if component == "home" else is_stamp if component == "stamp"
                   else path.name == component and path.is_relative_to(case.home))
        if matches:
            hit.append(path)
            raise OSError("injected directory fsync failure")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", fault)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert hit
    assert "Backed up to" in result.output
    assert roots(case) == before


def test_guard_created_djinn_is_fsynced_at_barrier(discard_case, monkeypatch):
    case = discard_case
    drift(case)
    before = roots(case)
    fsync = os.fsync
    backed_up = []
    backup = workflow.backup_target_drift

    def traced(*args, **kwargs):
        result = backup(*args, **kwargs)
        backed_up.append(args[2])
        return result

    def fault(fd):
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if path == case.home / ".local/state/djinn" and len(backed_up) == 2:
            raise OSError("guard-created directory barrier failure")
        fsync(fd)

    monkeypatch.setattr(workflow, "backup_target_drift", traced)
    monkeypatch.setattr(os, "fsync", fault)
    result = case.invoke()
    assert result.exit_code == 1, result.output
    assert roots(case) == before
    assert (backup_stamp(case) / "codex/AGENTS.md").read_bytes() == b"runtime edit\n"


def test_second_root_backup_failure_preserves_all_roots(discard_case, monkeypatch):
    case = discard_case
    drift(case)
    before = roots(case)
    backup = workflow.backup_target_drift
    calls = []

    def fail(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 2:
            raise OSError("second-root backup failure")
        return backup(*args, **kwargs)

    monkeypatch.setattr(workflow, "backup_target_drift", fail)
    result = case.invoke()
    assert result.exit_code == 1
    assert calls == [case.project / "config", case.runtime]
    assert roots(case) == before
    assert "Backed up to" in result.output


@pytest.mark.parametrize("kind", ["collision", "invalid", "canonical-invalid", "toml"])
def test_phase_zero_rejects_before_backup(discard_case, kind):
    case = discard_case
    drift(case)
    if kind == "collision":
        (case.runtime / "AGENTS.md").unlink()
        (case.runtime / "AGENTS.md").symlink_to(case.tmp / "missing")
    elif kind == "invalid":
        replace_file(case.runtime / RUNTIME_MANIFEST_NAME, b"{}")
    elif kind == "canonical-invalid":
        replace_file(case.project / "config" / CANONICAL_MANIFEST_NAME, b"{}")
    else:
        # This bridge fragment is present in every delivered Codex view.
        path = case.runtime / "config.toml"
        path.write_text('project_doc_fallback_filenames = [\n  "EDITED.md",\n  "X.md",\n]\n')
    before = roots(case)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert roots(case) == before
    assert not (case.home / ".local/state/djinn/workflow-drift").exists()
    assert any(item in result.output for item in ("AGENTS.md", ".json", "config.toml"))


@pytest.mark.parametrize("change", ["newer", "omitted", "mismatched", "collision", "new-item"])
def test_authorization_through_orchestration(discard_case, monkeypatch, change):
    case = discard_case
    drift(case)
    barrier = workflow_drift_backup.BackupDirectories.barrier
    deliver = workflow._deliver_target

    def after_barrier(self):
        barrier(self)
        if change == "newer":
            replace_file(case.runtime / "AGENTS.md", b"newer edit\n")
        elif change == "collision":
            (case.runtime / "AGENTS.md").unlink()
            (case.runtime / "AGENTS.md").symlink_to(case.tmp / "missing")
        elif change == "new-item":
            replace_file(case.runtime / RUNTIME_MANIFEST_NAME, b"{}")

    def authorization(*args, **kwargs):
        if change == "omitted":
            kwargs["accept_drift"] = {}
        elif change == "mismatched":
            kwargs["accept_drift"] = {
                key: BackedUpState("wrong") for key in kwargs["accept_drift"]
            }
        return deliver(*args, **kwargs)

    monkeypatch.setattr(workflow_drift_backup.BackupDirectories, "barrier", after_barrier)
    monkeypatch.setattr(workflow, "_deliver_target", authorization)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    if change == "newer":
        assert (case.runtime / "AGENTS.md").read_bytes() == b"newer edit\n"
    elif change == "collision":
        assert (case.runtime / "AGENTS.md").is_symlink()
    else:
        assert (case.runtime / "AGENTS.md").read_bytes() == b"runtime edit\n"
    assert "AGENTS.md" in result.output or RUNTIME_MANIFEST_NAME in result.output
    assert "Backed up to" in result.output


def test_missing_and_retired_items(discard_case):
    case = discard_case
    (case.project / "config/claude/skills/demo").mkdir(parents=True)
    (case.project / "config/claude/skills/demo/SKILL.md").write_text(
        "---\nname: demo\ndescription: A skill\n---\n\nSkill.\n"
    )
    assert workflow.prepare_config_workflow(
        case.project, workflow.start_workflow_targets(case.config), require_compose_host_env=True,
    ).success
    (case.runtime / "AGENTS.md").unlink()
    skill = case.runtime / "skills/demo/SKILL.md"
    replace_file(skill, b"retired edit\n")
    (case.project / "config/claude/skills/demo/SKILL.md").unlink()
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert "was missing" in result.output
    stamp = backup_stamp(case)
    assert not (stamp / "codex/AGENTS.md").exists()
    assert (stamp / "codex/skills/demo/SKILL.md").read_bytes() == b"retired edit\n"
    assert not skill.exists()
    assert (case.runtime / "AGENTS.md").exists()


def test_full_discard_finishes_under_timeout(discard_case):
    case = discard_case
    drift(case)
    parent, child = multiprocessing.get_context("fork").Pipe()

    def run():
        result = case.invoke()
        child.send((result.exit_code, result.output, repr(result.exception)))

    process = multiprocessing.get_context("fork").Process(target=run)
    process.start()
    process.join(10)
    if process.is_alive():
        process.terminate()  # Only this test's own child process.
        process.join(2)
        pytest.fail("discard exceeded test timeout (possible lock hand-over deadlock)")
    assert parent.poll(), process.exitcode
    result = parent.recv()
    assert result[0] == 0, result
    parent.close()
    child.close()


@pytest.mark.parametrize("change", ["none", "personal", "managed", "missing-key"])
def test_carrier_backup_keeps_personal_keys(discard_case, monkeypatch, change):
    case = discard_case
    path = case.runtime / "config.toml"
    path.write_text('project_doc_fallback_filenames = ["EDITED.md"]\npersonal = "keep"\n')
    original = path.read_bytes()
    barrier = workflow_drift_backup.BackupDirectories.barrier

    def after(self):
        barrier(self)
        if change == "personal":
            path.write_text('project_doc_fallback_filenames = ["EDITED.md"]\npersonal = "new"\n')
        elif change == "managed":
            path.write_text('project_doc_fallback_filenames = ["NEWER.md"]\npersonal = "new"\n')

    if change == "missing-key":
        path.write_text('personal = "keep"\n')
        original = path.read_bytes()
    monkeypatch.setattr(workflow_drift_backup.BackupDirectories, "barrier", after)
    result = case.invoke()
    assert result.exit_code == (1 if change == "managed" else 0), (result.output, result.exception)
    assert (backup_stamp(case) / "codex/config.toml").read_bytes() == original
    if change == "managed":
        assert "NEWER.md" in path.read_text() and 'personal = "new"' in path.read_text()
        assert "config.toml[project_doc_fallback_filenames]" in result.output
    else:
        assert 'project_doc_fallback_filenames = ["CLAUDE.md"]' in path.read_text()
        assert f'personal = "{"new" if change == "personal" else "keep"}"' in path.read_text()


@pytest.mark.parametrize("change", ["newer", "omitted", "mismatched"])
def test_canonical_authorization_through_orchestration(discard_case, monkeypatch, change):
    case = discard_case
    drift(case)
    sync_config = workflow.sync_config
    path = case.project / "config/opencode/AGENTS.md"

    def altered(*args, **kwargs):
        if change == "newer":
            path.write_bytes(b"newer canonical edit\n")
        elif change == "omitted":
            kwargs["accept_drift"] = {}
        else:
            kwargs["accept_drift"] = {
                key: BackedUpState("wrong") for key in kwargs["accept_drift"]
            }
        return sync_config(*args, **kwargs)

    monkeypatch.setattr(workflow, "sync_config", altered)
    runtime = tree(case.config.config_root)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert path.read_bytes() == (b"newer canonical edit\n" if change == "newer"
                                 else b"canonical edit\n")
    assert tree(case.config.config_root) == runtime
    assert "config/opencode/AGENTS.md" in result.output


def test_host_fallback_is_untouched(discard_case):
    case = discard_case
    host = case.home / ".codex"
    host.mkdir()
    (host / "AGENTS.md").write_bytes(b"host edit\n")
    (host / RUNTIME_MANIFEST_NAME).write_bytes(b"personal manifest\n")
    before = tree(host)
    drift(case)
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert tree(host) == before


def test_retired_carrier_key_with_personal_neighbor(discard_case):
    case = discard_case
    # Native-only Codex hooks are delivered with the shared source workflow.
    script = case.project / "config/codex/hooks/ready_notify.py"
    script.parent.mkdir()
    script.write_text("print('ready')\n")
    carrier = case.project / "config/codex/hooks.json"
    carrier.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
        "type": "command", "command": "python3 ~/.codex/hooks/ready_notify.py",
    }]}]}}))
    assert workflow.prepare_config_workflow(
        case.project, workflow.start_workflow_targets(case.config), require_compose_host_env=True,
    ).success
    runtime = case.runtime / "hooks.json"
    edited = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo edit"}]}]},
              "personal": {"keep": True}}
    runtime.write_text(json.dumps(edited))
    original = runtime.read_bytes()
    script.unlink()
    carrier.write_text('{"hooks":{}}')
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert (backup_stamp(case) / "codex/hooks.json").read_bytes() == original
    assert "Stop" not in json.loads(runtime.read_bytes()).get("hooks", {})
    assert json.loads(runtime.read_bytes())["personal"] == {"keep": True}
    assert not (case.runtime / "hooks/ready_notify.py").exists()


@pytest.mark.parametrize("race", ["alias", "moved-parent", "moved-workflow", "moved-stamp",
                                  "private-mode", "foreign-owner"])
def test_descriptor_walk_rechecks_after_admission(discard_case, monkeypatch, race):
    case = discard_case
    drift(case)
    before = roots(case)
    original_open = os.open
    original_fstat = os.fstat
    hit = []
    if race in {"alias", "moved-workflow"}:
        directory = case.home / ".local/state/djinn/workflow-drift"
        directory.mkdir(parents=True, mode=0o700)
        directory.parent.chmod(0o700)

    def open_race(path, flags, *args, **kwargs):
        fd_parent = kwargs.get("dir_fd")
        parent = Path(os.readlink(f"/proc/self/fd/{fd_parent}")) if fd_parent else None
        if path == "workflow-drift" and parent and not hit:
            hit.append(path)
            if race == "alias":
                (parent / path).rmdir()
                (parent / path).symlink_to(case.config.code_dir, target_is_directory=True)
            elif race == "moved-parent":
                parent.rename(case.config.code_dir / "moved")
            elif race == "moved-workflow":
                fd = original_open(path, flags, *args, **kwargs)
                (parent / path).rename(case.config.code_dir / "moved")
                return fd
        fd = original_open(path, flags, *args, **kwargs)
        if race == "moved-stamp" and parent and parent.name == "workflow-drift":
            hit.append(path)
            (parent / path).rename(case.config.code_dir / "moved")
        elif race == "private-mode" and path == "workflow-drift":
            os.fchmod(fd, 0o750)
        return fd

    def foreign(fd):
        entry = original_fstat(fd)
        if race == "foreign-owner" and Path(os.readlink(f"/proc/self/fd/{fd}")) == case.home:
            hit.append("home")
            fields = list(entry)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return entry

    monkeypatch.setattr(os, "open", open_race)
    monkeypatch.setattr(os, "fstat", foreign)
    result = case.invoke()
    assert result.exit_code == 1, (result.output, result.exception)
    assert hit
    assert roots(case) == before
    if race == "moved-parent":
        assert not (case.config.code_dir / "moved/workflow-drift").exists()
    elif race == "moved-workflow":
        assert list((case.config.code_dir / "moved").iterdir()) == []
    assert "Backed up to" in result.output


def test_new_backup_entries_fsync_parent_immediately(discard_case, monkeypatch):
    case = discard_case
    drift(case)
    original_mkdir, original_fsync, original_open = os.mkdir, os.fsync, os.open
    pending = []
    observed = []

    def mkdir(path, mode=0o777, *, dir_fd=None):
        original_mkdir(path, mode, dir_fd=dir_fd)
        if dir_fd is not None:
            parent = Path(os.readlink(f"/proc/self/fd/{dir_fd}"))
            if parent.is_relative_to(case.home):
                assert not pending
                assert mode == 0o700
                pending.append(dir_fd)
                observed.append(parent / path)

    def fsync(fd):
        if pending:
            assert pending.pop() == fd
        original_fsync(fd)

    def opened(path, flags, *args, **kwargs):
        assert not pending, "new entry opened before parent fsync"
        if kwargs.get("dir_fd") is not None and str(path) in {"workflow-drift", "config", "codex"}:
            assert flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "open", opened)
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    assert len(observed) >= 4
    assert not pending


def test_shared_running_check_forwards_prefix(monkeypatch):
    calls = []
    monkeypatch.setattr(_lifecycle, "get_running_containers",
                        lambda **kwargs: calls.append(kwargs) or [])
    _lifecycle.guard_no_containers_running("discarding target drift", prefix="djinn-test-only")
    assert calls == [{"prefix": "djinn-test-only"}]


def test_sessions_from_environment_home_are_protected(discard_case, monkeypatch):
    case = discard_case
    other_home = case.tmp / "session-home"
    (other_home / ".djinn").mkdir(parents=True)
    (other_home / ".djinn/sessions").symlink_to(case.home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(other_home))
    before = tree(case.tmp)
    result = case.invoke()
    assert result.exit_code == 1, result.output
    assert "sessions directory" in result.output
    assert tree(case.tmp) == before


def test_fixed_passwd_location_existing_parent_modes_and_stamp(discard_case, monkeypatch):
    import re

    case = discard_case
    state = case.home / ".local/state"
    state.mkdir(parents=True, mode=0o755)
    state.parent.chmod(0o755)
    monkeypatch.setenv("XDG_STATE_HOME", str(case.tmp / "other-state"))
    drift(case)
    result = case.invoke()
    assert result.exit_code == 0, (result.output, result.exception)
    stamp = backup_stamp(case)
    assert re.fullmatch(rf"\d{{8}}T\d{{6}}Z-{os.getpid()}", stamp.name)
    assert f"Backed up to {stamp}" in result.output
    assert state.stat().st_mode & 0o777 == 0o755
    assert state.parent.stat().st_mode & 0o777 == 0o755
    assert not (case.tmp / "other-state/djinn/workflow-drift").exists()


def test_stamps_are_not_reused(discard_case, monkeypatch):
    case = discard_case
    admission = workflow_drift_backup.admit_backup_root(case.project, case.config)
    admission.path.mkdir(parents=True, mode=0o700)
    (case.home / ".local/state/djinn").chmod(0o700)
    admission.path.parent.chmod(0o700)
    (admission.path / "keep").write_bytes(b"previous backup\n")
    monkeypatch.setattr(command, "admit_backup_root", lambda *args: admission)
    drift(case)
    before = roots(case)
    result = case.invoke()
    assert result.exit_code == 1, result.output
    assert roots(case) == before
    assert tree(admission.path) == {"keep": ((admission.path / "keep").stat().st_mode,
                                           b"previous backup\n")}
