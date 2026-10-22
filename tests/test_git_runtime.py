from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from djinn_in_a_box.core import host_runtime, host_sealing, post_mortem
from djinn_in_a_box.core.git_agent import agent_keys
from djinn_in_a_box.core.host_runtime import (
    GENERATION_LABEL,
    git_runtime,
    inspect_object,
    runtime_root,
    stop_owned_process,
)
from djinn_in_a_box.core.ssh_delivery import GitSSHError, read_public_delivery


@pytest.mark.parametrize(
    "stderr",
    [
        "Error response from daemon: get X: no such volume",
        "Error response from daemon: No such container: X",
        "NO SUCH image: X",
    ],
)
def test_inspect_object_default_missing_match_is_unchanged(monkeypatch, stderr):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", stderr)
    )
    assert inspect_object("X", "/fake/docker", "volume") is None


@pytest.mark.parametrize(
    "stderr, missing, absent",
    [
        ("Error response from daemon: No such container: X", "No such container", True),
        ("Error: No such container: X", "No such container", True),
        ("open /x: no such file or directory", "No such container", False),
        ("Error response from daemon: get X: no such volume", "No such container", False),
        ("permission denied", "No such container", False),
        ("Error response from daemon: no such container: X", "No such container", False),
        ("Error response from daemon: No such container: X", "no such", False),
        ("Error response from daemon: get X: no such volume", "no such", True),
        ("Error response from daemon: network X not found", "network X not found", True),
    ],
)
def test_inspect_object_explicit_missing_is_case_sensitive(monkeypatch, stderr, missing, absent):
    calls = []

    def inspect(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, "", stderr)

    monkeypatch.setattr(subprocess, "run", inspect)
    if absent:
        assert inspect_object("X", "/fake/docker", missing=missing, timeout=1.25) is None
    else:
        with pytest.raises(GitSSHError, match="Docker inspection failed"):
            inspect_object("X", "/fake/docker", missing=missing, timeout=1.25)
    assert calls[0][0] == ["/fake/docker", "container", "inspect", "X"]
    assert calls[0][1]["timeout"] == 1.25


@pytest.fixture(autouse=True)
def container_uid_matches_host(monkeypatch):
    monkeypatch.setattr(host_runtime, "CONTAINER_USER_UID", os.getuid())


@pytest.fixture
def fake_docker(git_inputs, monkeypatch):
    root = Path(os.environ["XDG_RUNTIME_DIR"])
    state = root / "fake-dev.json"
    binary = root / "bin" / "docker"
    binary.parent.mkdir()
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"state = pathlib.Path({str(state)!r})\n"
        "with state.with_suffix('.calls').open('a') as calls:\n"
        "    calls.write(json.dumps({'ppid': os.getppid(), 'argv': sys.argv[1:]}) + '\\n')\n"
        "if sys.argv[1:3] == ['rm', '-f']:\n"
        "    state.unlink()\n"
        "    sys.exit(0)\n"
        "if state.exists():\n"
        "    print(state.read_text())\n"
        "else:\n"
        "    print('Error response from daemon: No such container: ' + sys.argv[-1],\n"
        "          file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    binary.chmod(0o700)
    monkeypatch.setattr(host_runtime, "DOCKER_EXECUTABLE", str(binary))
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    try:
        yield state
    finally:
        metadata = runtime_root() / "state.json"
        if metadata.exists():
            record = json.loads(metadata.read_text())
            if "observer_pid" in record:
                stop_owned_process(record["observer_pid"], record["observer_token"])
            stop_owned_process(record["agent_pid"], record["agent_token"])


def observed_dev(state, runtime, dev_id="dev-1", running=True):
    fd, temporary = tempfile.mkstemp(dir=state.parent)
    with os.fdopen(fd, "w") as stream:
        stream.write(
            json.dumps(
                [
                    {
                        "Id": dev_id,
                        "State": {"Running": running},
                        "Config": {"Labels": {GENERATION_LABEL: runtime.generation}},
                    }
                ]
            )
        )
    os.replace(temporary, state)


def wait_for_absence(path):
    deadline = time.monotonic() + 8
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not path.exists(), f"runtime leaked: {path}"


def test_runtime_root_ignores_environment(tmp_path, monkeypatch):
    expected = (
        Path(pwd.getpwuid(os.getuid()).pw_dir)
        / ".local"
        / "state"
        / "djinn"
        / "runtime"
        / "git-agent"
    )
    xdg = tmp_path / "xdg-runtime"
    xdg.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    monkeypatch.setenv("HOME", str(tmp_path / "first-home"))
    assert runtime_root() == expected

    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "second-home"))
    assert runtime_root() == expected


@pytest.mark.parametrize("vector", ["caller-directory", "pythonpath"])
def test_observer_ignores_shadow_package_in_caller_directory(
    git_inputs, fake_docker, tmp_path, monkeypatch, vector
):
    caller = tmp_path / "caller"
    shadow_package = caller / "djinn_in_a_box"
    shadow_package.mkdir(parents=True)
    marker = tmp_path / "shadow-imported"
    (shadow_package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('shadowed')\n"
    )
    if vector == "caller-directory":
        monkeypatch.chdir(caller)
    else:
        # The working directory alone is neutralised by cwd="/"; only -I ignores PYTHONPATH.
        monkeypatch.setenv("PYTHONPATH", str(caller))

    root = runtime_root()
    observer = None
    runtime = None
    try:
        with git_runtime(git_inputs, "djinn") as runtime:
            observed_dev(fake_docker, runtime)
            runtime.retain()
            observer = runtime.observer
            assert observer is not None and observer.poll() is None
            assert (root / "attached").exists()
            assert not marker.exists()
    finally:
        if observer is not None:
            if observer.poll() is None:
                observer.terminate()
            observer.wait(timeout=8)
        if runtime is not None:
            runtime.close()


@pytest.mark.parametrize("transition", ["stopped", "removed", "replaced"])
def test_observer_releases_on_external_dev_transition(git_inputs, fake_docker, transition):
    root = runtime_root()
    expected = read_public_delivery(git_inputs.git).blobs
    observer = None
    try:
        with git_runtime(git_inputs, "djinn") as runtime:
            observed_dev(fake_docker, runtime)
            runtime.retain()
            observer = runtime.observer
            assert agent_keys(root / "export/auth.sock") == expected
        assert observer is not None and observer.poll() is None
        assert root.stat().st_mode & 0o077 == 0
        assert (root / "export/auth.sock").stat().st_mode & 0o077 == 0
        if transition == "stopped":
            observed_dev(fake_docker, runtime, running=False)
        elif transition == "removed":
            fake_docker.unlink()
        else:
            observed_dev(fake_docker, runtime, dev_id="dev-2")
        if transition == "replaced":
            # A replacement is foreign: its state is kept with the recorded cause (issue #199).
            observer.wait(timeout=8)
            record = json.loads((root / "state.json").read_text())
            assert record["observer_exit"]["code"] == "dev-replaced"
        else:
            wait_for_absence(root / "state.json")
        assert not (root / "export/auth.sock").exists()
    finally:
        if observer is not None:
            if observer.poll() is None:
                observer.terminate()
            observer.wait(timeout=8)
        runtime.close()


@pytest.mark.parametrize("with_git", [True, False], ids=["git", "desktop-only"])
def test_interrupt_during_foreground_start_removes_dev_and_state(
    git_inputs, fake_docker, capsys, monkeypatch, with_git
):
    config = git_inputs
    if not with_git:
        config = git_inputs.model_copy(
            update={
                "git": git_inputs.git.model_copy(
                    update={"identities": {}, "signing_identity": None}
                )
            }
        )
    root = runtime_root()
    dev_id = "dev-interrupted"
    observer = None
    runtime = None
    shutdown_index = None
    try:
        with pytest.raises(KeyboardInterrupt), git_runtime(config, "djinn") as runtime:
            observed_dev(fake_docker, runtime, dev_id=dev_id)
            observer = runtime.observer
            assert observer is not None
            deadline = time.monotonic() + 8
            while not (root / "attached").exists() and time.monotonic() < deadline:
                assert observer.poll() is None
                time.sleep(0.05)
            assert (root / "attached").exists()
            assert observer.poll() is None
            state = json.loads((root / "state.json").read_text())
            assert state["dev_id"] == dev_id
            assert state["generation"] == runtime.generation
            assert state["resources"] == {}
            assert (runtime.agent is not None) == with_git
            assert not runtime.detached

            terminate = observer.terminate

            def record_shutdown():
                nonlocal shutdown_index
                shutdown_index = len(fake_docker.with_suffix(".calls").read_text().splitlines())
                terminate()

            monkeypatch.setattr(observer, "terminate", record_shutdown)
            raise KeyboardInterrupt

        assert observer.poll() is not None
        assert observer.returncode == 0
        calls = [
            json.loads(line) for line in fake_docker.with_suffix(".calls").read_text().splitlines()
        ]
        assert [call["argv"] for call in calls if call["argv"][0] in ("stop", "rm")] == [
            ["rm", "-f", dev_id],
        ]
        assert shutdown_index is not None
        removal_index = next(index for index, call in enumerate(calls)
                             if call["argv"] == ["rm", "-f", dev_id])
        assert calls[removal_index]["ppid"] == os.getpid()
        final_inspections = [
            index for index, call in enumerate(calls)
            if index >= shutdown_index and call == {
                "ppid": observer.pid, "argv": ["container", "inspect", "djinn"],
            }
        ]
        # Observer inspections after the shutdown cutoff and before the creator's
        # removal show the observer was still inspecting the running dev while it
        # shut down. Ordinary polling between cutoff and signal can count here too,
        # so this is supporting evidence only; the finalizer cleanup itself is pinned
        # by the observer tests (test_observer_tolerance, test_observer_releases_*).
        assert len(final_inspections) >= 2
        assert shutdown_index <= final_inspections[0] < final_inspections[-1] < removal_index
        assert not fake_docker.exists()
        assert not (root / "state.json").exists()
        assert "Runtime cleanup preserved resources" not in capsys.readouterr().err
    finally:
        if observer is not None:
            if observer.poll() is None:
                observer.terminate()
            observer.wait(timeout=8)
        if runtime is not None:
            runtime.close()


def test_detached_agent_survives_actual_creator_exit(git_inputs, fake_docker):
    driver = """
from types import SimpleNamespace
import json, os, sys, tempfile, pytest
from pathlib import Path
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import host_runtime
config = AppConfig.model_validate(json.loads(sys.argv[1]))
with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setattr(
        host_runtime.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=os.environ["DJINN_TEST_PASSWD_HOME"]),
    )
    monkeypatch.setattr(host_runtime, "CONTAINER_USER_UID", os.getuid())
    with host_runtime.git_runtime(config, "djinn") as runtime:
        state = Path(sys.argv[2])
        fd, temporary = tempfile.mkstemp(dir=state.parent)
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps([{
                "Id": "dev-detached", "State": {"Running": True},
                "Config": {"Labels": {host_runtime.GENERATION_LABEL: runtime.generation}}
            }]))
        os.replace(temporary, state)
        runtime.retain()
"""
    result = subprocess.run(
        [sys.executable, "-c", driver, git_inputs.model_dump_json(), str(fake_docker)],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    root = runtime_root()
    try:
        assert agent_keys(root / "export/auth.sock") == read_public_delivery(git_inputs.git).blobs
    finally:
        fake_docker.unlink(missing_ok=True)
        wait_for_absence(root / "state.json")


def test_failed_creator_releases_agent(git_inputs, fake_docker):
    root = runtime_root()
    with pytest.raises(RuntimeError, match="creation failure"), git_runtime(git_inputs, "djinn"):
        raise RuntimeError("creation failure")
    assert not (root / "state.json").exists()
    assert not (root / "export/auth.sock").exists()


def test_concurrent_creator_refusal_preserves_live_agent(git_inputs, fake_docker):
    with git_runtime(git_inputs, "djinn") as first:
        observed_dev(fake_docker, first)
        first.retain()
        try:
            with pytest.raises(GitSSHError, match="already owns"), git_runtime(git_inputs, "djinn"):
                pytest.fail("second creator must not run")
            assert (
                agent_keys(first.root / "export/auth.sock")
                == read_public_delivery(git_inputs.git).blobs
            )
        finally:
            first.detached = False


def test_pending_creator_lock_refuses_second_creator(git_inputs, fake_docker):
    with git_runtime(git_inputs, "djinn") as first:
        with pytest.raises(GitSSHError, match="pending"), git_runtime(git_inputs, "djinn"):
            pytest.fail("pending creator lock must serialize startup")
        assert (
            agent_keys(first.root / "export/auth.sock")
            == read_public_delivery(git_inputs.git).blobs
        )


def test_startup_orphan_cleanup_after_creator_hard_death(git_inputs, fake_docker):
    driver = """
from types import SimpleNamespace
import json, os, sys, pytest
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import host_runtime
with pytest.MonkeyPatch.context() as monkeypatch:
    monkeypatch.setattr(
        host_runtime.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=os.environ["DJINN_TEST_PASSWD_HOME"]),
    )
    monkeypatch.setattr(host_runtime, "CONTAINER_USER_UID", os.getuid())
    with host_runtime.git_runtime(AppConfig.model_validate(json.loads(sys.argv[1])), "djinn"):
        os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", driver, git_inputs.model_dump_json()],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    root = runtime_root()
    wait_for_absence(root / "state.json")
    assert not (root / "export/auth.sock").exists()


@pytest.mark.parametrize("problem", ["permissions", "uid"])
def test_runtime_requires_owner_only_directory_and_matching_uid(git_inputs, monkeypatch, problem):
    if problem == "permissions":
        runtime_root(create=True).chmod(0o777)
    else:
        monkeypatch.setattr(host_runtime, "CONTAINER_USER_UID", os.getuid() + 1)
    with pytest.raises(GitSSHError) as exc_info, git_runtime(git_inputs, "djinn"):
        pytest.fail("unsafe runtime must not prepare Git delivery")
    if problem == "uid":
        assert str(exc_info.value) == (
            f"Generated SSH delivery requires host numeric UID {os.getuid() + 1}, "
            "matching the dev image"
        )
    else:
        assert "0700" in str(exc_info.value)


def test_empty_identities_skip_git_runtime(git_inputs, fake_docker, monkeypatch):
    config = git_inputs.model_copy(
        update={
            "git": git_inputs.git.model_copy(
                update={"identities": {}, "signing_identity": None}
            )
        }
    )
    monkeypatch.setattr(host_runtime, "CONTAINER_USER_UID", os.getuid() + 1)
    monkeypatch.setattr(
        host_runtime,
        "start_agent",
        lambda *args: pytest.fail("empty Git identities must not start an agent"),
    )
    fragment = {"services": {"dev": {}}}
    root = runtime_root()

    with git_runtime(config, "djinn") as runtime:
        assert runtime.root == root
        assert runtime.agent is None
        assert runtime.observer is not None
        runtime.add_to_fragment(fragment)
        assert fragment["services"]["dev"] == {"labels": {GENERATION_LABEL: runtime.generation}}
    assert not (root / "export").exists()
    assert not (root / "public").exists()
    assert not (root / "state.json").exists()


def test_startup_deadline_releases_pending_runtime(git_inputs, monkeypatch):
    from djinn_in_a_box.core import host_runtime

    root = runtime_root(create=True)
    (root / "loaded").touch()
    (root / "state.json").write_text(
        json.dumps(
            {
                "generation": "pending",
                "agent_pid": 123456,
                "agent_token": "fake",
                "container_name": "djinn",
                "creator_pid": os.getpid(),
                "creator_token": host_runtime.process_token(os.getpid()),
                "keys": [], "git_enabled": True, "resources": {}, "volumes": [],
            }
        )
    )
    lock_fd = os.open(root / "test.lock", os.O_CREAT | os.O_RDWR, 0o600)
    observations = []
    stopped = []

    class FakeFilter:
        def __init__(self, *args):
            self.stop = threading.Event()

        def serve(self):
            self.stop.wait(3)

    def inspect(name, *args):
        observations.append(name)
        if len(observations) > 2:
            raise AssertionError("observer continued after its startup deadline")
        return None

    times = iter([0, 0])
    monkeypatch.setattr(host_runtime.time, "monotonic", lambda: next(times, 100))
    monkeypatch.setattr(host_runtime, "inspect_dev", inspect)
    monkeypatch.setattr(host_runtime, "AgentFilter", FakeFilter)
    monkeypatch.setattr(host_runtime, "agent_keys", lambda path: frozenset())
    monkeypatch.setattr(host_runtime, "stop_owned_process", lambda *args: stopped.append(args))
    monkeypatch.setattr(host_runtime.signal, "signal", lambda *args: None)
    host_runtime.observe(
        root, lock_fd, host_runtime.DOCKER_EXECUTABLE, post_mortem.records_root(create=False)
    )
    # Issue #199: the deadline is checked before any inspection; only the cleanup inspects.
    assert observations == ["djinn"]
    assert stopped == [(123456, "fake")]
    assert not (root / "state.json").exists()
    with pytest.raises(OSError):
        os.fstat(lock_fd)


def test_old_creator_cleanup_preserves_replacement_runtime(git_inputs, fake_docker):
    with git_runtime(git_inputs, "djinn") as old:
        observed_dev(fake_docker, old)
        old.retain()
    fake_docker.unlink()
    wait_for_absence(old.root / "state.json")
    with git_runtime(git_inputs, "djinn") as replacement:
        old.close()
        assert (
            agent_keys(replacement.root / "export/auth.sock")
            == read_public_delivery(git_inputs.git).blobs
        )
        assert (replacement.root / "loaded").exists()
        observed_dev(fake_docker, replacement)
        replacement.retain()
        replacement.detached = False
        # Exercise generation checking after the pending creation lock is released too.
        old.close()
        assert (
            agent_keys(replacement.root / "export/auth.sock")
            == read_public_delivery(git_inputs.git).blobs
        )


def test_doctor_reports_actual_live_agent_and_changed_declarations(git_inputs, fake_docker):
    from djinn_in_a_box.core.git_diagnostics import git_diagnostics

    with git_runtime(git_inputs, "djinn") as runtime:
        observed_dev(fake_docker, runtime)
        runtime.retain()
        runtime.detached = False
        rows = git_diagnostics(git_inputs)
        assert any(row.name == "Git agent" and row.status == "pass" for row in rows)
        changed = git_inputs.model_copy(
            update={
                "git": git_inputs.git.model_copy(
                    update={
                        "identities": {"git-work": git_inputs.git.identities["git-work"]},
                    }
                )
            }
        )
        rows = git_diagnostics(changed)
        assert any(row.name == "Git agent" and row.status == "warn" for row in rows)


def test_shared_state_parent_stays_private_under_a_permissive_umask(git_inputs, monkeypatch):
    # An implicit mkdir parent takes the umask's mode; hostctl refuses a group-writable one.
    import stat

    from djinn_in_a_box.core import hostctl

    previous = os.umask(0o002)
    try:
        parent = runtime_root(create=True).parent.parent
    finally:
        os.umask(previous)
    assert parent.name == "djinn"
    assert stat.S_IMODE(parent.stat().st_mode) & 0o022 == 0
    monkeypatch.setenv("XDG_STATE_HOME", str(parent.parent))
    assert hostctl.state_root().parent == parent


def test_live_runtime_control_files_are_private(git_inputs, fake_docker, monkeypatch):
    root = runtime_root()
    assert root.is_relative_to(Path(os.environ["DJINN_TEST_PASSWD_HOME"]))
    writes = set()
    original_open = Path.open

    def record_open(path, mode="r", *args, **kwargs):
        if path.is_relative_to(root) and any(flag in mode for flag in "wax+"):
            writes.add(path)
        return original_open(path, mode, *args, **kwargs)

    # Include state.tmp, which is renamed away before a directory snapshot can see it.
    monkeypatch.setattr(Path, "open", record_open)
    with git_runtime(git_inputs, "djinn") as runtime:
        observed_dev(fake_docker, runtime)
        runtime.retain()
        runtime.detached = False
        private = set(host_sealing.private_state_paths().values())
        files = {path for path in root.rglob("*") if not path.is_dir()} | writes
        files = {
            path for path in files
            if path.relative_to(root).parts[0] not in {"public", "export"}
        }
        assert root / "private/agent.sock" in files
        assert root / host_runtime.STATE_TMP in files
        assert root / host_runtime.ATTACHED in files
        assert all(
            any(path == entry or entry in path.parents for entry in private) for path in files
        )
        controls = {
            path for path in private
            if path.is_relative_to(root) and path.relative_to(root).parts[0] != "private"
        }
        assert controls == {path for path in files if path.parent == root}
