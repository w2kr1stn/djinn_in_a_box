"""Real isolated observers against an absolute fake Docker executable."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest

from djinn_in_a_box.config.ssh import GitConfig
from djinn_in_a_box.core import docker, host_runtime, hostctl, post_mortem
from djinn_in_a_box.core.ssh_delivery import GitSSHError


def write_json(path, data):
    """A concurrent fake Docker call must never read a partial file."""
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(data))
    os.replace(temporary, path)


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


def dev(owner, running=True, identifier="dev-id"):
    return {
        "Id": identifier,
        "State": {
            "Running": running, "Status": "running" if running else "exited",
            "ExitCode": 0 if running else 137, "OOMKilled": False, "Error": "",
            "StartedAt": "run-a", "FinishedAt": "" if running else "finish-a",
        },
        "Config": {
            "Labels": {
                host_runtime.GENERATION_LABEL: owner.generation,
                "com.docker.compose.project": "djinn-in-a-box",
            }
        },
    }


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.parametrize("git_enabled", [False, True])
def test_observer_desktop_stop_start_remove_and_pinned_path(fake_owner, monkeypatch, git_enabled):
    config, binary, objects, log = fake_owner
    if not git_enabled:
        config = config.model_copy(update={"git": GitConfig()})
    planted = binary.parent.parent / "earlier"
    planted.mkdir()
    monkeypatch.setenv(
        "PATH", str(planted) + os.pathsep + str(binary.parent) + os.pathsep + os.environ["PATH"]
    )
    marker = planted / "executed"
    with host_runtime.git_runtime(config, "djinn") as owner:
        helper = {
            "Id": "helper-id",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    host_runtime.GENERATION_LABEL: owner.generation,
                    "com.docker.compose.project": "djinn-in-a-box",
                    "com.docker.compose.service": "audio-helper",
                }
            },
        }
        volume = {
            "Name": "djinn-desktop-audio",
            "Labels": {host_runtime.GENERATION_LABEL: owner.generation},
        }
        data = {"djinn": dev(owner), "helper-id": helper, "djinn-desktop-audio": volume}
        write_json(objects, data)
        owner.register("audio-helper", helper, "djinn-desktop-audio")
        owner.retain()
        (planted / "docker").write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        (planted / "docker").chmod(0o700)
        data["djinn"] = dev(owner, False)
        write_json(objects, data)
        wait_for(lambda: ["stop", "helper-id"] in calls(log))
        assert json.loads(objects.read_text())["helper-id"]["State"]["Running"] is False
        assert (owner.root / "state.json").exists()
        data = json.loads(objects.read_text())
        data["djinn"] = dev(owner)
        write_json(objects, data)
        wait_for(lambda: ["start", "helper-id"] in calls(log))
        data = json.loads(objects.read_text())
        del data["djinn"]
        write_json(objects, data)
        wait_for(lambda: not (owner.root / "state.json").exists())
        assert "helper-id" not in json.loads(objects.read_text())
        assert "djinn-desktop-audio" not in json.loads(objects.read_text())
        assert ["rm", "-f", "helper-id"] in calls(log)
        assert ["volume", "rm", "djinn-desktop-audio"] in calls(log)
        assert not marker.exists()


@pytest.mark.parametrize("creator", ["foreground", "detached", "clean"])
def test_empty_identity_pending_guard_refuses_every_path(fake_owner, creator, monkeypatch):
    config, binary, objects, log = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
    monkeypatch.setattr(docker, "validate_managed_bind_sources", lambda config: None)
    monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
    monkeypatch.setattr(
        docker, "_prepare_companions", lambda *args: pytest.fail("loser touched helpers")
    )
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda *args: [])
    monkeypatch.setattr(docker, "get_sops_age_key_mount_args", lambda *args: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *args: ([], ()))
    monkeypatch.setattr(docker, "is_own_container", lambda *args: False)
    with host_runtime.git_runtime(config, "djinn") as owner:
        assert owner.agent is None
        before = calls(log)
        if creator == "clean":
            result = docker.compose_down()
            assert result.returncode == 1 and "pending" in result.stderr
        else:
            with pytest.raises(GitSSHError, match="pending"):
                if creator == "detached":
                    docker.compose_up_detached(config, docker.ContainerOptions())
                else:
                    docker.compose_run(
                        config, docker.ContainerOptions(), interactive=creator == "foreground"
                    )
        assert calls(log) == before
        assert host_runtime.read_state(owner.root)["generation"] == owner.generation


@pytest.mark.parametrize("condition", ["replacement", "unknown", "consumed", "owned"])
def test_cleanup_uses_generation_ids_and_consumers(fake_owner, condition, monkeypatch):
    _, binary, objects, log = fake_owner
    root = host_runtime.runtime_root(create=True)
    generation = "mine"
    record = {"id": "agent-id", "service": "agent-docker"}
    state = {
        "generation": generation,
        "container_name": "djinn",
        "dev_id": "dev-id",
        "resources": {"agent-docker": record},
        "volumes": [],
    }
    (root / "state.json").write_text(json.dumps(state))
    obj = {
        "Id": "agent-id",
        "Config": {
            "Labels": {
                host_runtime.GENERATION_LABEL: generation,
                "com.docker.compose.project": "djinn-in-a-box",
                "com.docker.compose.service": "agent-docker",
            }
        },
    }
    write_json(objects, {"agent-id": obj})
    if condition == "replacement":
        state["generation"] = "replacement"
        (root / "state.json").write_text(json.dumps(state))
    elif condition == "unknown":
        monkeypatch.setattr(
            host_runtime,
            "inspect_dev",
            lambda *args: (_ for _ in ()).throw(GitSSHError("daemon unavailable")),
        )
    elif condition == "consumed":
        write_json(
            objects,
            {
                "agent-id": obj,
                "djinn": {
                    "Id": "dev-id",
                    "State": {"Running": True},
                    "Config": {"Labels": {host_runtime.GENERATION_LABEL: generation}},
                },
            },
        )
    try:
        host_runtime.cleanup_owned(root, generation, str(binary))
    except GitSSHError:
        assert condition == "unknown"
    mutations = [c for c in calls(log) if c[0] in {"rm", "stop", "volume"}]
    assert mutations == (
        [["rm", "-f", "agent-id"]]
        if condition == "owned"
        else []
    )
    assert ("agent-id" in json.loads(objects.read_text())) == (condition != "owned")


def test_clean_removes_dev_before_helpers_and_joins_outside_guard(fake_owner, monkeypatch):
    config, binary, objects, log = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
    monkeypatch.setattr(docker, "is_own_container", lambda *args: False)
    with host_runtime.git_runtime(config, "djinn") as owner:
        write_json(objects, {"djinn": dev(owner)})
        owner.retain()

        def down(*args, **kwargs):
            assert host_runtime.inspect_dev("djinn", str(binary)) is None
            return docker.RunResult(0)

        monkeypatch.setattr(docker, "_run_compose", down)
        original_stop = host_runtime.stop_owned_process

        def joined_outside_guard(pid, token):
            with host_runtime.creation_guard(owner.root):
                pass
            original_stop(pid, token)

        monkeypatch.setattr(host_runtime, "stop_owned_process", joined_outside_guard)
        assert docker.compose_down().success
        assert ["rm", "-f", "dev-id"] in calls(log)
        assert not (owner.root / "state.json").exists()
        owner.observer.wait(timeout=5)




def test_creator_guard_survives_observer_failure_before_dev(fake_owner):
    config, _, _, _ = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
    with host_runtime.git_runtime(config, "djinn") as owner:
        owner.observer.terminate()
        owner.observer.wait(timeout=5)
        with pytest.raises(GitSSHError, match="pending"), host_runtime.git_runtime(config, "djinn"):
            pytest.fail("observer failure released the creator's preparation guard")


def test_degraded_foreground_releases_guard_after_actual_id(fake_owner, monkeypatch):
    config, _, objects, _ = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
    original_spawn = host_runtime.subprocess.Popen

    def spawn(argv, **kwargs):
        if "djinn_in_a_box.core.host_runtime" in argv:
            raise OSError("observer unavailable")
        return original_spawn(argv, **kwargs)

    monkeypatch.setattr(host_runtime.subprocess, "Popen", spawn)
    with host_runtime.git_runtime(config, "djinn") as owner:
        assert owner.observer is None
        owner.begin_creation()
        write_json(objects, {"djinn": dev(owner)})

        def guard_available():
            try:
                with host_runtime.creation_guard(owner.root):
                    return True
            except GitSSHError:
                return False

        wait_for(guard_available)
        assert host_runtime.read_state(owner.root)["dev_id"] == "dev-id"


def test_degraded_detached_retain_never_rewrites_released_ownership(fake_owner, monkeypatch):
    _, binary, objects, _ = fake_owner
    root = host_runtime.runtime_root(create=True)
    owner = host_runtime.GitRuntime(
        root, "mine", git_enabled=False, docker_path=str(binary), owns_generation=True
    )
    state = {"generation": "mine", "dev_id": "dev-id", "container_name": "djinn"}
    (root / "state.json").write_text(json.dumps(state))
    write_json(objects, {"djinn": dev(owner)})
    monkeypatch.setattr(
        host_runtime, "save_state", lambda *args: pytest.fail("state rewritten after guard release")
    )
    owner.retain()
    assert owner.detached
    assert host_runtime.read_state(root) == state


@pytest.mark.parametrize("condition", ["stale", "running", "foreign", "observed"])
def test_creator_reclaims_only_a_stopped_recorded_dev(fake_owner, condition):
    # A host reboot leaves the recorded dev stopped, its observer gone and helpers restarted.
    config, _, objects, log = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
    root = host_runtime.runtime_root(create=True)
    # A disposable live process stands in for a surviving observer.
    observer = subprocess.Popen(["sleep", "30"]) if condition == "observed" else None
    state = {
        "generation": "old",
        "container_name": "djinn",
        "dev_id": "dev-id",
        "observer_pid": observer.pid if observer else -1,
        "observer_token": host_runtime.process_token(observer.pid) if observer else "",
        "agent_pid": -1,
        "agent_token": "",
        "resources": {"dbus-helper": {"id": "helper-id", "service": "dbus-helper"}},
        "volumes": [],
    }
    (root / "state.json").write_text(json.dumps(state))
    label = host_runtime.GENERATION_LABEL
    dev = {
        "Id": "dev-id",
        "State": {"Running": condition == "running"},
        "Config": {"Labels": {label: "other" if condition == "foreign" else "old"}},
    }
    helper = {
        "Id": "helper-id",
        "Config": {
            "Labels": {
                label: "old",
                "com.docker.compose.project": "djinn-in-a-box",
                "com.docker.compose.service": "dbus-helper",
            }
        },
    }
    write_json(objects, {"djinn": dev, "helper-id": helper})
    if condition == "stale":
        with host_runtime.git_runtime(config, "djinn") as owner:
            assert owner.generation != "old"
        assert ["rm", "-f", "dev-id"] in calls(log)
        assert ["rm", "-f", "helper-id"] in calls(log)
        assert json.loads(objects.read_text()) == {}
    else:
        with (
            pytest.raises(GitSSHError, match="already owns the runtime"),
            host_runtime.git_runtime(config, "djinn"),
        ):
            pass
        assert not [c for c in calls(log) if c[0] == "rm"]
        assert set(json.loads(objects.read_text())) == {"djinn", "helper-id"}
    if observer:
        observer.terminate()
        observer.wait(timeout=5)


def test_fixture_isolates_hostctl_docker(fake_owner, monkeypatch):
    _, binary, _, log = fake_owner
    execute = subprocess.run

    def isolated(argv, **kwargs):
        assert argv[0] == str(binary), "test reached a real Docker client"
        return execute(argv, **kwargs)

    monkeypatch.setattr(host_runtime.subprocess, "run", isolated)
    assert hostctl.inspect_helper() is None
    assert calls(log) == [["container", "inspect", "djinn-hostctl"]]


def test_unit_tests_cannot_change_the_real_docker_host():
    from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE

    with pytest.raises(AssertionError, match="would change the real Docker host"):
        subprocess.run([DOCKER_EXECUTABLE, "rm", "-f", "djinn-unit-test-guard"], check=False)


def cleanup_inventory(
    fake_owner, *, service="audio-helper", dev_running=None, helper_running=False
):
    _, binary, objects, _ = fake_owner
    root = host_runtime.runtime_root(create=True)
    owner = host_runtime.GitRuntime(
        root, "mine", git_enabled=False, docker_path=str(binary), owns_generation=True
    )
    helper = dev(owner, helper_running, "helper-id")
    helper["Config"]["Labels"]["com.docker.compose.service"] = service
    owner.resources[service] = {"id": "helper-id", "service": service}
    state = {
        "generation": owner.generation, "container_name": "djinn", "dev_id": "dev-id",
        "resources": owner.resources, "volumes": [], "keys": [], "git_enabled": False,
        "agent_pid": -1, "agent_token": "", "observer_pid": -1, "observer_token": "",
        "creator_pid": os.getpid(), "creator_token": host_runtime.process_token(os.getpid()),
    }
    host_runtime.save_state(root, state)
    data = {"helper-id": helper}
    if dev_running is not None:
        data["djinn"] = dev(owner, dev_running)
    objects.write_text(json.dumps(data))
    return owner


def behavior(fake_owner, **values):
    fake_owner[1].parent.joinpath("behavior.json").write_text(json.dumps(values))


def records():
    return [
        json.loads(p.read_text()) for p in post_mortem.records_root(create=False).glob("*Z-*.json")
    ]


def markers():
    path = post_mortem.records_root(create=False) / "provenance.json"
    return json.loads(path.read_text()) if path.exists() else {}


def observe_ticks(owner, binary, monkeypatch, transition, *, ticks=2):
    class Ticks:
        tick = 0

        def wait(self, seconds):
            self.tick += 1
            transition(self.tick)
            return self.tick > ticks

        def set(self):
            pass

    monkeypatch.setattr(host_runtime.threading, "Event", Ticks)
    monkeypatch.setattr(host_runtime.signal, "signal", lambda *args: None)
    (owner.root / "loaded").touch()
    try:
        host_runtime.observe(
            owner.root, host_runtime.acquire_creation_lock(owner.root), str(binary),
            post_mortem.records_root(create=False),
        )
    finally:
        state = host_runtime.read_state(owner.root)
        if state is not None:
            state["observer_pid"], state["observer_token"] = -1, ""
            host_runtime.save_state(owner.root, state)


@pytest.mark.parametrize("route", ["start-reclaim", "runtime-close", "observer-cleanup"])
def test_record_published_before_first_mutation(fake_owner, monkeypatch, route):
    config, binary, _, log = fake_owner
    owner = cleanup_inventory(fake_owner, dev_running=False)
    behavior(fake_owner, record={
        "trigger": route, "generation": "mine", "ids": ["dev-id", "helper-id"],
    })
    if route == "start-reclaim":
        config = config.model_copy(update={"git": GitConfig()})
        with host_runtime.git_runtime(config, "djinn"):
            pass
    elif route == "runtime-close":
        owner.close()
    else:
        observe_ticks(owner, binary, monkeypatch, lambda tick: None, ticks=0)
    assert ["rm", "-f", "helper-id"] in calls(log)
    assert records()[0]["trigger"] == route
    assert {s["id"] for s in records()[0]["subjects"]} == {"dev-id", "helper-id"}
    assert records()[0]["subjects"][0]["log_tail"] == "isolated container log\n"


@pytest.mark.parametrize("condition", ["running", "oneoff", "marked", "restarted"])
def test_cleanup_subject_episodes(fake_owner, condition):
    _, binary, objects, _ = fake_owner
    owner = cleanup_inventory(fake_owner, dev_running=condition == "running")
    data = json.loads(objects.read_text())
    if condition == "running":
        data["helper-id"]["State"]["Running"] = True
    elif condition == "oneoff":
        data.pop("helper-id")
        state = host_runtime.read_state(owner.root)
        state["resources"] = {}
        host_runtime.save_state(owner.root, state)
        data["djinn"]["Config"]["Labels"]["com.docker.compose.oneoff"] = "True"
        data["djinn"]["HostConfig"] = {"AutoRemove": True}
    else:
        data.pop("djinn")
        post_mortem.mark_terminated(str(binary), "helper-id", "run-a")
        if condition == "restarted":
            data["helper-id"]["State"].update(StartedAt="run-b", FinishedAt="finish-b")
    objects.write_text(json.dumps(data))
    if condition == "restarted":
        behavior(fake_owner, record={
            "trigger": "runtime-close", "generation": "mine", "ids": ["helper-id"],
        })
    else:
        behavior(fake_owner, no_record=True)
    owner.close()
    assert bool(records()) == (condition == "restarted")


@pytest.mark.parametrize("boundary", ["state", "generation", "dev-id", "running", "resource"])
def test_record_hook_follows_ownership_preflight(fake_owner, monkeypatch, boundary):
    _, binary, objects, log = fake_owner
    owner = cleanup_inventory(fake_owner, dev_running=False)
    state = host_runtime.read_state(owner.root)
    if boundary == "state":
        (owner.root / "state.json").unlink()
    elif boundary == "generation":
        state["generation"] = "other"
    elif boundary == "dev-id":
        state["dev_id"] = "replacement"
    else:
        data = json.loads(objects.read_text())
        if boundary == "running":
            data["djinn"]["State"]["Running"] = True
        else:
            data["helper-id"]["Config"]["Labels"][host_runtime.GENERATION_LABEL] = "other"
        objects.write_text(json.dumps(data))
    if boundary != "state":
        host_runtime.save_state(owner.root, state)
    monkeypatch.setattr(post_mortem, "record", lambda *a, **k: pytest.fail("premature hook"))
    if boundary == "resource":
        with pytest.raises(GitSSHError, match="ownership changed"):
            host_runtime.cleanup_owned(owner.root, "mine", str(binary), incident="probe")
    else:
        assert not host_runtime.cleanup_owned(owner.root, "mine", str(binary), incident="probe")
    assert not [c for c in calls(log) if c[0] in {"stop", "rm"}]


@pytest.mark.parametrize("target", ["stop", "dev-rm", "resource-rm"])
@pytest.mark.parametrize("outcome", ["running", "stopped", "unclear", "different-run"])
def test_cleanup_verified_outcome_retry(fake_owner, monkeypatch, target, outcome):
    _, binary, objects, log = fake_owner
    owner = cleanup_inventory(
        fake_owner, service="agent-docker" if target == "stop" else "audio-helper",
        helper_running=True, dev_running=True if target == "dev-rm" else None,
    )
    identity = "dev-id" if target == "dev-rm" else "helper-id"
    argv = ["stop", "-t", "20", identity] if target == "stop" else ["rm", "-f", identity]
    failure = {"argv": argv}
    if outcome == "stopped":
        failure["running"] = False
    elif outcome == "unclear":
        failure["unclear"] = True
    elif outcome == "different-run":
        failure.update(running=False, new_started="run-b")
    behavior(fake_owner, no_record=True, failure=failure)
    execute = subprocess.run
    original_error = subprocess.TimeoutExpired([str(binary), *argv], 60)

    def run(command, **kwargs):
        assert kwargs.get("stdin") is subprocess.DEVNULL
        result = execute(command, **kwargs)
        if command == [str(binary), *argv] and outcome == "stopped":
            raise original_error
        return result

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises((GitSSHError, subprocess.TimeoutExpired)) as caught:
        host_runtime.cleanup_owned(owner.root, "mine", str(binary), terminate_dev=True)
    if outcome == "stopped" and target == "stop":
        assert caught.value is original_error
    elif outcome != "stopped":
        assert str(caught.value) == "operation refused"
    assert markers() == ({identity: "run-a"} if outcome == "stopped" else {})
    if target == "dev-rm":
        assert ["rm", "-f", "helper-id"] not in calls(log)
        assert "helper-id" not in markers()  # An unreached operation grants no provenance.
    data = json.loads(objects.read_text())
    key = "djinn" if target == "dev-rm" else "helper-id"
    data[key].pop("InspectionError", None)
    data[key]["State"]["Running"] = False  # External stop of the same unmarked run.
    objects.write_text(json.dumps(data))
    monkeypatch.setattr(subprocess, "run", execute)
    if outcome == "stopped":
        behavior(fake_owner, no_record=True)
    else:
        behavior(fake_owner, record={"trigger": "retry", "generation": "mine", "ids": [identity]})
    assert host_runtime.cleanup_owned(
        owner.root, "mine", str(binary), terminate_dev=True, incident="retry"
    )
    assert bool(records()) == (outcome != "stopped")


def test_companion_stop_success_marks_before_rm(fake_owner):
    _, binary, _, log = fake_owner
    owner = cleanup_inventory(fake_owner, service="agent-docker", helper_running=True)
    behavior(fake_owner, markers={"helper-id": "run-a"})
    assert host_runtime.cleanup_owned(owner.root, "mine", str(binary))
    assert markers() == {"helper-id": "run-a"}
    assert calls(log).index(["stop", "-t", "20", "helper-id"]) < calls(log).index(
        ["rm", "-f", "helper-id"]
    )


@pytest.mark.parametrize("target", ["dev", "resource", "stopped-companion"])
def test_successful_removal_needs_no_marker(fake_owner, target):
    _, binary, _, _ = fake_owner
    owner = cleanup_inventory(
        fake_owner, service="agent-docker" if target == "stopped-companion" else "audio-helper",
        dev_running=True if target == "dev" else None, helper_running=target == "resource",
    )
    assert host_runtime.cleanup_owned(owner.root, "mine", str(binary), terminate_dev=True)
    assert markers() == {}


@pytest.mark.parametrize("fault", ["record", "marker"])
@pytest.mark.parametrize("failed", [False, True])
def test_diagnostics_failure_preserves_cleanup_result(fake_owner, monkeypatch, fault, failed):
    _, binary, _, _ = fake_owner
    owner = cleanup_inventory(fake_owner, service="agent-docker", helper_running=True)
    if failed:
        behavior(fake_owner, failure={"argv": ["stop", "-t", "20", "helper-id"], "running": False})
    def broken(*args, **kwargs):
        raise RuntimeError("diagnostic failure")
    monkeypatch.setattr(post_mortem, "record" if fault == "record" else "mark_terminated", broken)
    if failed:
        with pytest.raises(GitSSHError, match="^operation refused$"):
            host_runtime.cleanup_owned(owner.root, "mine", str(binary), incident="probe")
    else:
        assert host_runtime.cleanup_owned(owner.root, "mine", str(binary), incident="probe")


@pytest.mark.parametrize(
    "site", ["dev", "resource", "cleanup-volume", "remove-volume", "discard-volume"]
)
@pytest.mark.parametrize("expired", [False, True])
def test_removal_deadline_and_context(fake_owner, monkeypatch, site, expired):
    _, binary, objects, _ = fake_owner
    owner = cleanup_inventory(fake_owner, dev_running=False if site == "dev" else None)
    state = host_runtime.read_state(owner.root)
    data = json.loads(objects.read_text())
    identity = "dev-id" if site == "dev" else "helper-id"
    kind = "container"
    if "volume" in site:
        identity, kind = "runtime-volume", "volume"
        data = {identity: {"Name": identity, "Labels": {
            host_runtime.GENERATION_LABEL: "mine", "com.docker.compose.project": "djinn-in-a-box",
        }}}
        state["resources"], state["volumes"] = {}, [identity]
    elif site == "dev":
        data.pop("helper-id")
        state["resources"] = {}
    objects.write_text(json.dumps(data))
    host_runtime.save_state(owner.root, state)
    execute = subprocess.run
    removals = []
    def run(argv, **kwargs):
        assert kwargs.get("stdin") is subprocess.DEVNULL
        if argv[1] == "rm" or argv[1:3] == ["volume", "rm"]:
            assert argv[1:] == (
                ["rm", "-f", identity] if kind == "container" else ["volume", "rm", identity]
            )
            assert kwargs["timeout"] == 60
            removals.append(argv)
            if expired:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return execute(argv, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    def remove():
        if site == "discard-volume":
            owner.discard_volume(identity)
        elif site == "remove-volume":
            host_runtime.remove_runtime_volume(identity, str(binary))
        else:
            return host_runtime.cleanup_owned(
                owner.root, "mine", str(binary), terminate_dev=True, incident="probe"
            )
    if expired:
        with pytest.raises(GitSSHError) as caught:
            remove()
        assert str(caught.value) == (
            f"Docker is still removing {kind} {identity} after 60 s; "
            "the daemon may finish it in the background. Retrying is safe."
        )
        assert identity in json.dumps(json.loads(objects.read_text()))
        if kind == "container":
            assert records()[0]["subjects"][0]["id"] == identity
    else:
        remove()
        assert not json.loads(objects.read_text())
        if site == "discard-volume":
            assert host_runtime.read_state(owner.root)["volumes"] == []
    assert len(removals) == 1


@pytest.mark.parametrize("site", ["helper", "companion", "resume-dev"])
@pytest.mark.parametrize("outcome", ["success", "running", "stopped", "unclear", "already-stopped"])
def test_observer_stop_verified_outcomes(fake_owner, monkeypatch, site, outcome):
    _, binary, objects, log = fake_owner
    owner = cleanup_inventory(
        fake_owner, service="audio-helper" if site == "helper" else "agent-docker",
        helper_running=outcome != "already-stopped", dev_running=True,
    )
    identity = "dev-id" if site == "resume-dev" else "helper-id"
    argv = (["stop", "-t", "3", identity] if site == "resume-dev" else
            ["stop", "-t", "20", identity] if site == "companion" else ["stop", identity])
    if outcome in {"running", "stopped", "unclear"}:
        failure = {"argv": argv}
        if outcome == "stopped":
            failure["running"] = False
        elif outcome == "unclear":
            failure["unclear"] = True
        behavior(fake_owner, failure=failure)
    marker_roots = []
    mark_terminated = post_mortem.mark_terminated
    def mark(*args, **kwargs):
        if args[1] == identity:
            marker_roots.append(kwargs.get("root"))
        return mark_terminated(*args, **kwargs)
    monkeypatch.setattr(post_mortem, "mark_terminated", mark)
    execute = subprocess.run
    error = subprocess.TimeoutExpired([str(binary), *argv], 5)
    def run(command, **kwargs):
        assert kwargs.get("stdin") is subprocess.DEVNULL
        result = execute(command, **kwargs)
        if command == [str(binary), *argv] and outcome == "stopped":
            raise error
        return result
    monkeypatch.setattr(subprocess, "run", run)
    expected = {identity: "run-a"} if outcome in {"success", "stopped"} else {}
    cleanup_calls = []
    supplied_root = post_mortem.records_root(create=False)
    def final_cleanup(root, generation, docker_path, **kwargs):
        assert kwargs == {"incident": "observer-cleanup", "records_root": supplied_root}
        assert markers() == expected
        cleanup_calls.append(True)
        return False
    monkeypatch.setattr(host_runtime, "cleanup_owned", final_cleanup)
    def transition(tick):
        data = json.loads(objects.read_text())
        if tick == 2:
            data["djinn"]["State"]["Running"] = False
        elif tick == 3 and site == "resume-dev":
            data["djinn"]["State"]["Running"] = True
        objects.write_text(json.dumps(data))
    if site == "resume-dev":
        def failed_resume(*args):
            data = json.loads(objects.read_text())
            data["helper-id"]["State"]["Running"] = True
            if outcome == "already-stopped":
                data["djinn"]["State"]["Running"] = False
            objects.write_text(json.dumps(data))
            # Discard the earlier companion marker to isolate the dev stop's outcome.
            post_mortem.records_root(create=True).joinpath("provenance.json").unlink(missing_ok=True)
            raise RuntimeError("resume refused")
        monkeypatch.setattr(docker, "resume_agent_docker", failed_resume)
    if outcome in {"running", "stopped", "unclear"} or site == "resume-dev":
        with pytest.raises((GitSSHError, RuntimeError, subprocess.TimeoutExpired)) as caught:
            observe_ticks(
                owner, binary, monkeypatch, transition, ticks=3 if site == "resume-dev" else 2
            )
        if outcome == "stopped":
            assert caught.value is error
        else:
            assert str(caught.value) == (
                "resume refused"
                if outcome in {"success", "already-stopped"} else "operation refused"
            )
    else:
        observe_ticks(owner, binary, monkeypatch, transition)
    assert cleanup_calls == [True]
    assert marker_roots == ([] if outcome == "already-stopped" else [supplied_root])
    assert markers() == expected
    if outcome == "already-stopped":
        assert argv not in calls(log)
    else:
        assert argv in calls(log)


@pytest.mark.parametrize("episode", ["djinn-stopped", "externally-restarted"])
def test_detached_observer_uses_argument_root_and_episode(fake_owner, monkeypatch, episode):
    config, binary, objects, log = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
    supplied_root = post_mortem.records_root(create=False)
    resolved = []
    def resolve(*, create):
        assert create is False
        resolved.append(create)
        return supplied_root
    monkeypatch.setattr(post_mortem, "records_root", resolve)
    with host_runtime.git_runtime(config, "djinn") as owner:
        helper = dev(owner, True, "helper-id")
        helper["Config"]["Labels"]["com.docker.compose.service"] = "audio-helper"
        owner.register("audio-helper", helper)
        objects.write_text(json.dumps({"djinn": dev(owner), "helper-id": helper}))
        owner.retain()
        assert owner.observer.args[-1] == str(supplied_root)
        data = json.loads(objects.read_text())
        data["djinn"]["State"]["Running"] = False
        objects.write_text(json.dumps(data))
        wait_for(lambda: supplied_root.joinpath("provenance.json").exists())
        assert json.loads(supplied_root.joinpath("provenance.json").read_text()) == {
            "helper-id": "run-a",
        }
        data = json.loads(objects.read_text())
        assert data["helper-id"]["State"]["Running"] is False
        del data["djinn"]
        if episode == "externally-restarted":
            data["helper-id"]["State"].update(StartedAt="run-b", FinishedAt="finish-b")
            behavior(fake_owner, record={
                "trigger": "observer-cleanup", "generation": owner.generation, "ids": ["helper-id"],
            })
        else:
            behavior(fake_owner, no_record=True)
        objects.write_text(json.dumps(data))
        wait_for(lambda: not (owner.root / "state.json").exists())
        owner.observer.wait(timeout=5)
        assert owner.observer.returncode == 0, owner.root.joinpath("observer.log").read_text()
    assert resolved == [False]
    assert ["rm", "-f", "helper-id"] in calls(log)
    saved = list(supplied_root.glob("*Z-*.json"))
    assert bool(saved) == (episode == "externally-restarted")
    if saved:
        assert json.loads(saved[0].read_text())["trigger"] == "observer-cleanup"


def test_close_records_failed_detached_creation_without_observer(fake_owner, monkeypatch):
    config, _, objects, log = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
    spawn = subprocess.Popen
    def unavailable(argv, **kwargs):
        if "djinn_in_a_box.core.host_runtime" in argv:
            raise OSError("observer unavailable")
        return spawn(argv, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", unavailable)
    for name in ("get_shell_mount_args", "get_sops_age_key_mount_args"):
        monkeypatch.setattr(docker, name, lambda *a: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *a: ([], ()))
    monkeypatch.setattr(docker, "_prepare_companions", lambda *a: None)
    monkeypatch.setattr(docker, "_guard_dev_creation", lambda *a: None)
    def failed_up(*args, **kwargs):
        state = host_runtime.read_state(host_runtime.runtime_root())
        owner = SimpleNamespace(generation=state["generation"])
        objects.write_text(json.dumps({"djinn": dev(owner, False)}))
        behavior(fake_owner, record={
            "trigger": "runtime-close", "generation": owner.generation, "ids": ["dev-id"],
        })
        return docker.RunResult(17, stderr="creation failed")
    monkeypatch.setattr(docker, "_run_compose", failed_up)
    result = docker.compose_up_detached(config, docker.ContainerOptions())
    assert result.returncode == 17 and result.stderr == "creation failed"
    assert ["rm", "-f", "dev-id"] in calls(log)
    assert records()[0]["trigger"] == "runtime-close"


@pytest.mark.parametrize("incident", [None, "probe"])
def test_cleanup_pins_record_candidates(fake_owner, monkeypatch, incident):
    _, binary, _, _ = fake_owner
    owner = cleanup_inventory(fake_owner, dev_running=False)
    observed = []
    supplied_root = post_mortem.records_root(create=False)
    def record(trigger, **kwargs):
        observed.append((trigger, kwargs))
    monkeypatch.setattr(post_mortem, "record", record)
    assert host_runtime.cleanup_owned(
        owner.root, "mine", str(binary), terminate_dev=True,
        incident=incident, records_root=supplied_root,
    )
    if incident is None:
        assert observed == []
    else:
        assert observed == [("probe", {
            "docker_path": str(binary), "runtime_generation": "mine", "root": supplied_root,
            "candidates": [
                post_mortem.Candidate("djinn", True, "dev-id"),
                post_mortem.Candidate("helper-id", True, "helper-id"),
            ],
        })]


@pytest.mark.parametrize("snapshot", ["unavailable", "replacement", "missing-started"])
def test_dev_marker_inspection_is_best_effort(fake_owner, monkeypatch, snapshot):
    _, binary, _, _ = fake_owner
    owner = cleanup_inventory(fake_owner, dev_running=True, helper_running=True)
    behavior(fake_owner, failure={"argv": ["rm", "-f", "dev-id"], "running": False})
    attempts = []
    mark_terminated = post_mortem.mark_terminated
    def mark(*args, **kwargs):
        attempts.append(args)
        return mark_terminated(*args, **kwargs)
    monkeypatch.setattr(post_mortem, "mark_terminated", mark)
    inspect = host_runtime.inspect_object
    def inspect_dev_run(name, *args, **kwargs):
        if name == "dev-id":
            if snapshot == "unavailable":
                raise GitSSHError("diagnostic inspection failed")
            if snapshot == "replacement":
                return dev(owner, True, "replacement")
            result = dev(owner, True)
            result["State"]["StartedAt"] = ""
            return result
        return inspect(name, *args, **kwargs)
    monkeypatch.setattr(host_runtime, "inspect_object", inspect_dev_run)
    with pytest.raises(GitSSHError, match="^operation refused$"):
        host_runtime.cleanup_owned(owner.root, "mine", str(binary), terminate_dev=True)
    assert markers() == {}
    assert attempts == []


@pytest.mark.parametrize("route", ["runtime-close", "observer-cleanup"])
def test_record_exception_preserves_cleanup_callers(fake_owner, monkeypatch, route):
    _, binary, _, log = fake_owner
    owner = cleanup_inventory(fake_owner)
    def broken(*args, **kwargs):
        raise RuntimeError("recorder bug")
    monkeypatch.setattr(post_mortem, "record", broken)
    if route == "runtime-close":
        owner.close()
    else:
        observe_ticks(owner, binary, monkeypatch, lambda tick: None, ticks=0)
    assert ["rm", "-f", "helper-id"] in calls(log)
    assert not (owner.root / "state.json").exists()


@pytest.mark.parametrize("command", ["start", "run"])
def test_reclaim_removal_timeout_reaches_cli(fake_owner, monkeypatch, command):
    from typer.testing import CliRunner

    from djinn_in_a_box.cli.djinn import app
    from djinn_in_a_box.commands import agent, container

    config, binary, _, _ = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
    cleanup_inventory(fake_owner, dev_running=False)
    behavior(fake_owner, record={
        "trigger": "start-reclaim", "generation": "mine", "ids": ["dev-id", "helper-id"],
    })
    execute = subprocess.run
    def run(argv, **kwargs):
        if argv[1:] == ["rm", "-f", "dev-id"]:
            assert kwargs["timeout"] == 60
            # Let the executable assert ordering, but preserve the object on timeout.
            behavior(fake_owner, record={
                "trigger": "start-reclaim", "generation": "mine", "ids": ["dev-id", "helper-id"],
            }, failure={"argv": argv[1:]})
            execute(argv, **kwargs)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return execute(argv, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    module = container if command == "start" else agent
    monkeypatch.setattr(module, "load_config", lambda: config)
    monkeypatch.setattr(
        module, "prepare_config_workflow", lambda *a, **k: SimpleNamespace(success=True)
    )
    def create(*args, **kwargs):
        with host_runtime.git_runtime(config, "djinn"):
            pytest.fail("timed-out reclaim authorized creation")
    if command == "start":
        monkeypatch.setattr(container, "preflight", lambda *a, **k: None)
        monkeypatch.setattr(container, "ensure_network", lambda: True)
        monkeypatch.setattr(container, "get_shell_mount_args", lambda *a: [])
        monkeypatch.setattr(container, "compose_run", create)
        args = ["start"]
    else:
        monkeypatch.setattr(agent, "workflow_image_compatible", lambda: None)
        monkeypatch.setattr(agent, "run_headless_agent", create)
        args = ["run", "claude", "test"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    output = " ".join(result.output.split())
    assert (
        "Internal runtime mount construction failed: Docker is still removing container dev-id "
        "after 60 s; the daemon may finish it in the background. Retrying is safe."
    ) in output
    assert records()[0]["trigger"] == "start-reclaim"
    assert binary.is_file()


def test_failed_dev_rm_never_marks_unreached_companion(fake_owner):
    _, binary, _, log = fake_owner
    owner = cleanup_inventory(
        fake_owner, service="agent-docker", dev_running=True, helper_running=True
    )
    behavior(fake_owner, failure={"argv": ["rm", "-f", "dev-id"]})
    with pytest.raises(GitSSHError, match="^operation refused$"):
        host_runtime.cleanup_owned(owner.root, "mine", str(binary), terminate_dev=True)
    assert markers() == {}
    assert ["stop", "-t", "20", "helper-id"] not in calls(log)
    assert ["rm", "-f", "helper-id"] not in calls(log)


@pytest.mark.parametrize("expected", ["recorded", "inspected", "unknown"])
def test_record_dev_candidate_cannot_widen_ownership(fake_owner, monkeypatch, expected):
    _, binary, objects, _ = fake_owner
    owner = cleanup_inventory(
        fake_owner, dev_running=False if expected == "inspected" else None, helper_running=True
    )
    if expected != "recorded":
        state = host_runtime.read_state(owner.root)
        state.pop("dev_id")
        host_runtime.save_state(owner.root, state)
    candidates = []
    record = post_mortem.record
    def replace_dev(trigger, **kwargs):
        candidates.extend(kwargs["candidates"])
        data = json.loads(objects.read_text())
        data["djinn"] = dev(owner, False, "replacement-id")
        objects.write_text(json.dumps(data))
        return record(trigger, **kwargs)
    monkeypatch.setattr(post_mortem, "record", replace_dev)
    behavior(fake_owner, no_record=True)
    assert host_runtime.cleanup_owned(owner.root, "mine", str(binary), incident="probe")
    assert candidates[0] == post_mortem.Candidate(
        "djinn", expected != "unknown", "dev-id" if expected != "unknown" else None
    )
    assert records() == []
    assert json.loads(objects.read_text())["djinn"]["Id"] == "replacement-id"


@pytest.fixture
def clean_owner(fake_owner, monkeypatch):
    monkeypatch.setattr(docker, "is_own_container", lambda *args: False)
    monkeypatch.setattr(
        docker, "_run_compose",
        lambda *a, **kw: docker._run_captured([str(fake_owner[1]), "compose-down"], timeout=15),
    )
    return fake_owner


@pytest.mark.parametrize("layout", ["state", "no-state", "stale-state", "oneoff", "running"])
@pytest.mark.parametrize("service", ["audio-helper", "agent-docker"])
def test_clean_records_before_mutation(clean_owner, layout, service):
    _, _, objects, log = clean_owner
    owner = cleanup_inventory(clean_owner, dev_running=layout == "running",
                              helper_running=layout == "running", service=service)
    data = json.loads(objects.read_text())
    if layout == "stale-state":
        data["djinn"]["Config"]["Labels"].pop(host_runtime.GENERATION_LABEL)
    if layout == "oneoff":
        data["djinn"]["Config"]["Labels"]["com.docker.compose.oneoff"] = "True"
        data["djinn"]["HostConfig"] = {"AutoRemove": True}
    objects.write_text(json.dumps(data))
    if layout == "no-state":
        host_runtime.clear_state(owner.root, owner.generation)
    ids = ["helper-id"] if layout == "oneoff" else ["dev-id", "helper-id"]
    behavior(clean_owner, **({"no_record": True} if layout == "running" else {
        "record": {"trigger": "clean", "generation": None if layout == "no-state" else "mine",
                   "ids": ids},
    }))
    assert docker.compose_down().success
    assert ["ps", "-aq", "--no-trunc", "--filter",
            "label=com.docker.compose.project=djinn-in-a-box"] in calls(log)
    assert bool(records()) == (layout != "running")
    assert markers() == {}  # Successful removals need no provenance.


def test_clean_discovery_excludes_foreign_project(clean_owner):
    _, _, objects, _ = clean_owner
    owner = cleanup_inventory(clean_owner)
    host_runtime.clear_state(owner.root, owner.generation)
    data = json.loads(objects.read_text())
    data["foreign"] = dev(owner, False, "foreign-id")
    data["foreign"]["Config"]["Labels"]["com.docker.compose.project"] = "other-project"
    objects.write_text(json.dumps(data))
    behavior(clean_owner, record={"trigger": "clean", "generation": None, "ids": ["helper-id"]})
    assert docker.compose_down().success
    assert "foreign" in json.loads(objects.read_text())


def test_clean_candidates_follow_removal_boundary(clean_owner, monkeypatch):
    _, _, objects, log = clean_owner
    owner = cleanup_inventory(clean_owner, dev_running=False)
    state = host_runtime.read_state(owner.root)
    state["dev_id"] = "expected-old-dev"
    host_runtime.save_state(owner.root, state)
    captured = []
    original = post_mortem.record

    def record(trigger, **kwargs):
        captured.extend(kwargs["candidates"])
        return original(trigger, **kwargs)

    monkeypatch.setattr(post_mortem, "record", record)
    behavior(
        clean_owner,
        record={"trigger": "clean", "generation": "mine", "ids": ["dev-id", "helper-id"]},
    )
    assert docker.compose_down().success
    # A stale runtime dev_id never filters out the stopped dev that clean removes.
    assert post_mortem.Candidate("dev-id", True, "dev-id") in captured
    assert ["rm", "-f", "dev-id"] in calls(log)  # Removal boundary stays as before.

    owner = cleanup_inventory(clean_owner)
    data = json.loads(objects.read_text())
    data["helper-id"]["Config"]["Labels"][host_runtime.GENERATION_LABEL] = "foreign"
    objects.write_text(json.dumps(data))
    captured.clear()
    assert not docker.compose_down().success
    assert captured == []


@pytest.mark.parametrize("outcome", ["running", "stopped", "unclear", "different-run"])
@pytest.mark.parametrize("site", ["dev", "compose"])
def test_clean_verified_failure_retry(clean_owner, monkeypatch, site, outcome):
    _, binary, objects, log = clean_owner
    cleanup_inventory(clean_owner, service="agent-docker", dev_running=True, helper_running=True)
    target = "dev-id" if site == "dev" else "helper-id"
    argv = ["rm", "-f", target] if site == "dev" else ["compose-down"]
    failure = {"argv": argv, "target": target}
    if outcome == "stopped":
        failure["running"] = False
    elif outcome == "unclear":
        failure["unclear"] = True
    elif outcome == "different-run":
        failure.update(running=False, new_started="run-b")
    behavior(clean_owner, no_record=True, failure=failure)
    execute = subprocess.run

    def run(command, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        result = execute(command, **kwargs)
        if command == [str(binary), *argv]:
            if site == "dev":
                assert kwargs["timeout"] == 60
            if outcome == "stopped":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return result

    monkeypatch.setattr(subprocess, "run", run)
    result = docker.compose_down()
    assert not result.success
    if outcome == "stopped":
        assert result.returncode == 124
        if site == "dev":
            assert result.stderr == (
                "Docker is still removing container dev-id after 60 s; the daemon may finish "
                "it in the background. Retrying is safe."
            )
    assert markers() == ({target: "run-a"} if outcome == "stopped" else {})
    if site == "dev":
        assert ["compose-down"] not in calls(log)
        assert "helper-id" not in markers()
    data = json.loads(objects.read_text())
    for obj in data.values():
        obj.pop("InspectionError", None)
        obj["State"]["Running"] = False  # Later external failure, including unreached companion.
    objects.write_text(json.dumps(data))
    expected = [obj["Id"] for obj in data.values() if obj["Id"] not in markers()]
    behavior(clean_owner, **({"record": {"trigger": "clean", "generation": "mine", "ids": expected}}
                             if expected else {"no_record": True}))
    monkeypatch.setattr(subprocess, "run", execute)
    assert docker.compose_down().success
    assert {s["id"] for doc in records() for s in doc["subjects"]} == set(expected)


@pytest.mark.parametrize("fault", ["record", "mark_terminated"])
def test_clean_diagnostics_failure_keeps_result(clean_owner, monkeypatch, fault):
    cleanup_inventory(clean_owner, dev_running=True)
    behavior(clean_owner, failure={"argv": ["rm", "-f", "dev-id"], "running": False})

    def broken(*a, **kw):
        raise RuntimeError("diagnostics failure")

    monkeypatch.setattr(post_mortem, fault, broken)
    result = docker.compose_down()
    assert result.returncode == 1 and result.stderr.strip() == "operation refused"


def test_clean_records_before_hostctl_stop(clean_owner):
    _, _, objects, _ = clean_owner
    cleanup_inventory(clean_owner, dev_running=False)
    data = json.loads(objects.read_text())
    data["djinn-hostctl"] = {
        "Id": "hostctl-id", "State": {"Running": True, "StartedAt": "hostctl-run"},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "hostctl-generation"}},
    }
    objects.write_text(json.dumps(data))
    behavior(clean_owner, record={"trigger": "clean", "generation": "mine",
                                 "ids": ["dev-id", "helper-id"]})
    assert docker.compose_down().success


def test_clean_includes_state_resources_outside_compose_label(clean_owner):
    _, _, objects, _ = clean_owner
    owner = cleanup_inventory(clean_owner)
    data = json.loads(objects.read_text())
    data["helper-id"]["Config"]["Labels"]["com.docker.compose.project"] = "other-project"
    objects.write_text(json.dumps(data))
    state = host_runtime.read_state(owner.root)
    state["resources"]["audio-helper"]["project"] = "other-project"
    host_runtime.save_state(owner.root, state)
    behavior(clean_owner, record={"trigger": "clean", "generation": "mine", "ids": ["helper-id"]})
    assert docker.compose_down().success


def test_clean_discovery_is_bounded_and_captured(clean_owner, monkeypatch):
    cleanup_inventory(clean_owner)
    execute = subprocess.run
    discoveries = []

    def run(argv, **kwargs):
        if argv[1] == "ps" and "-aq" in argv and argv.count("--filter") == 1:
            assert kwargs["stdin"] is subprocess.DEVNULL
            assert kwargs["timeout"] == 5
            assert kwargs["capture_output"] is True and kwargs["text"] is True
            discoveries.append(argv)
        return execute(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert docker.compose_down().success
    assert discoveries


def test_clean_shared_cleanup_has_no_incident(clean_owner, monkeypatch):
    cleanup_inventory(clean_owner)
    cleanup = host_runtime.cleanup_owned
    seen = []

    def checked(*args, **kwargs):
        assert kwargs.get("incident") is None
        seen.append(True)
        return cleanup(*args, **kwargs)

    monkeypatch.setattr(host_runtime, "cleanup_owned", checked)
    assert docker.compose_down().success
    assert seen == [True]


def test_clean_already_stopped_failure_never_marks(clean_owner):
    cleanup_inventory(clean_owner, dev_running=False)
    behavior(clean_owner, record={"trigger": "clean", "generation": "mine",
                                 "ids": ["dev-id", "helper-id"]},
             failure={"argv": ["rm", "-f", "dev-id"]})
    assert not docker.compose_down().success
    assert markers() == {}


def test_clean_unreached_state_resource_records_on_retry(clean_owner, monkeypatch):
    _, _, objects, log = clean_owner
    owner = cleanup_inventory(clean_owner, dev_running=True, helper_running=True)
    state = host_runtime.read_state(owner.root)
    state["resources"]["audio-helper"]["project"] = "other-project"
    host_runtime.save_state(owner.root, state)
    data = json.loads(objects.read_text())
    data["helper-id"]["Config"]["Labels"]["com.docker.compose.project"] = "other-project"
    objects.write_text(json.dumps(data))
    down = docker._run_compose

    def failed_down(*args, **kwargs):
        data = json.loads(objects.read_text())
        data["helper-id"]["State"]["Running"] = False  # External exit; cleanup is never reached.
        objects.write_text(json.dumps(data))
        return docker.RunResult(1, stderr="compose failed")

    monkeypatch.setattr(docker, "_run_compose", failed_down)
    result = docker.compose_down()
    assert result.returncode == 1 and result.stderr == "compose failed"
    assert ["rm", "-f", "helper-id"] not in calls(log)
    assert markers() == {}
    monkeypatch.setattr(docker, "_run_compose", down)
    behavior(clean_owner, record={"trigger": "clean", "generation": "mine", "ids": ["helper-id"]})
    assert docker.compose_down().success
    assert json.loads(objects.read_text()) == {}
    assert {s["id"] for doc in records() for s in doc["subjects"]} == {"helper-id"}


@pytest.mark.parametrize("change", ["stopped", "restarted"])
def test_clean_compose_uses_immediate_running_snapshot(clean_owner, monkeypatch, change):
    _, _, objects, log = clean_owner
    cleanup_inventory(clean_owner, dev_running=True, helper_running=True)
    record = post_mortem.record
    run = docker._run_captured

    def recorded(trigger, **kwargs):
        snapshot = record(trigger, **kwargs)
        assert snapshot is not None and snapshot.running["helper-id"] == "run-a"
        return snapshot

    def removed_dev(argv, **kwargs):
        result = run(argv, **kwargs)
        if argv[1:] == ["rm", "-f", "dev-id"]:
            data = json.loads(objects.read_text())
            data["helper-id"]["State"]["Running"] = change == "restarted"
            if change == "restarted":
                data["helper-id"]["State"]["StartedAt"] = "run-b"
            objects.write_text(json.dumps(data))
        return result

    monkeypatch.setattr(post_mortem, "record", recorded)
    monkeypatch.setattr(docker, "_run_captured", removed_dev)
    behavior(clean_owner, no_record=True, failure={
        "argv": ["compose-down"], "target": "helper-id", "running": False,
    })
    assert docker.compose_down().returncode == 1
    assert ["compose-down"] in calls(log)
    assert markers() == ({"helper-id": "run-b"} if change == "restarted" else {})
    assert records() == []
    if change == "stopped":
        monkeypatch.setattr(post_mortem, "record", record)
        behavior(clean_owner, record={"trigger": "clean", "generation": "mine",
                                     "ids": ["helper-id"]})
        assert docker.compose_down().success
        assert json.loads(objects.read_text()) == {}
        assert {s["id"] for doc in records() for s in doc["subjects"]} == {"helper-id"}


@pytest.mark.parametrize("fault", ["none", "exception"])
@pytest.mark.parametrize("returncode", [0, 1])
def test_clean_compose_markers_independent_of_record(clean_owner, monkeypatch, fault, returncode):
    _, _, objects, _ = clean_owner
    cleanup_inventory(clean_owner, dev_running=True, helper_running=True)

    def failed_record(*args, **kwargs):
        if fault == "exception":
            raise OSError("private recorder failure")
        return None

    def down(*args, **kwargs):
        data = json.loads(objects.read_text())
        data["helper-id"]["State"]["Running"] = False
        objects.write_text(json.dumps(data))
        return docker.RunResult(returncode)

    monkeypatch.setattr(post_mortem, "record", failed_record)
    monkeypatch.setattr(docker, "_run_compose", down)
    assert docker.compose_down().returncode == returncode
    assert markers() == {"helper-id": "run-a"}


@pytest.mark.parametrize("fault", ["error", "timeout", "absent", "unclear"])
def test_clean_compose_preinspect_is_best_effort(clean_owner, monkeypatch, capsys, fault):
    _, _, objects, _ = clean_owner
    cleanup_inventory(clean_owner, dev_running=True, helper_running=True)
    inspect = host_runtime.inspect_object
    timeouts = []
    marker_calls = []

    def inspected(identity, *args, **kwargs):
        if identity == "helper-id" and "djinn" not in json.loads(objects.read_text()):
            timeouts.append(kwargs.get("timeout"))
            if fault == "error":
                raise OSError("private pre-operation inspect failure")
            if fault == "timeout":
                raise subprocess.TimeoutExpired("private inspect", kwargs["timeout"])
            if fault == "absent":
                return None
            return {"Id": identity, "State": {"StartedAt": "run-a", "Running": None}}
        return inspect(identity, *args, **kwargs)

    def down(*args, **kwargs):
        assert len(timeouts) == 1
        data = json.loads(objects.read_text())
        data["helper-id"]["State"]["Running"] = False
        objects.write_text(json.dumps(data))
        return docker.RunResult(1)

    monkeypatch.setattr(host_runtime, "inspect_object", inspected)
    monkeypatch.setattr(docker, "_run_compose", down)
    monkeypatch.setattr(post_mortem, "mark_terminated",
                        lambda *a, **kw: marker_calls.append((a, kw)))
    assert docker.compose_down().returncode == 1
    assert timeouts and all(value is not None and 0 < value <= 5 for value in timeouts)
    assert all(args[1] != "helper-id" for args, _ in marker_calls)
    assert markers() == {}
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        "Post-mortem record failed (inspect)\n" if fault in {"error", "timeout"} else ""
    )


def test_clean_success_verifies_surviving_target(clean_owner, monkeypatch):
    _, _, objects, _ = clean_owner
    cleanup_inventory(clean_owner, dev_running=True, helper_running=True)
    cleanup = host_runtime.cleanup_owned

    def down(*args, **kwargs):
        data = json.loads(objects.read_text())
        data["helper-id"]["State"]["Running"] = False
        objects.write_text(json.dumps(data))
        return docker.RunResult(0)

    def checked_cleanup(*args, **kwargs):
        assert markers() == {"helper-id": "run-a"}
        return cleanup(*args, **kwargs)

    monkeypatch.setattr(docker, "_run_compose", down)
    monkeypatch.setattr(host_runtime, "cleanup_owned", checked_cleanup)
    assert docker.compose_down().success
    assert records() == []


@pytest.mark.parametrize("fault", [1, 124, "exception"])
@pytest.mark.parametrize("with_state", [False, True])
def test_clean_discovery_failure_falls_back(clean_owner, monkeypatch, capsys, fault, with_state):
    _, _, objects, log = clean_owner
    owner = cleanup_inventory(clean_owner, dev_running=False, helper_running=False)
    if not with_state:
        host_runtime.clear_state(owner.root, owner.generation)
    run = docker._run_captured
    record = post_mortem.record
    candidates = []

    def failed_discovery(argv, **kwargs):
        if argv[1] == "ps" and "--no-trunc" in argv and argv.count("--filter") == 1:
            if fault == "exception":
                raise OSError("private-discovery-exception")
            return docker.RunResult(fault, stderr="private-discovery-stderr")
        return run(argv, **kwargs)

    def recorded(trigger, **kwargs):
        candidates.extend(kwargs["candidates"])
        return record(trigger, **kwargs)

    monkeypatch.setattr(docker, "_run_captured", failed_discovery)
    monkeypatch.setattr(post_mortem, "record", recorded)
    expected = ["dev-id", "helper-id"] if with_state else ["dev-id"]
    behavior(clean_owner, record={"trigger": "clean", "generation": "mine" if with_state else None,
                                 "ids": expected})
    result = docker.compose_down()
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(objects.read_text()) == {}
    assert ["rm", "-f", "dev-id"] in calls(log)
    assert post_mortem.Candidate("djinn", True) in candidates  # Fixed name, deliberately unpinned.
    assert {s["id"] for doc in records() for s in doc["subjects"]} == set(expected)
    output = capsys.readouterr()
    assert "Post-mortem record failed (inspect)\n" in output.err
    assert "private-discovery" not in output.out + output.err


def test_clean_discovery_fallback_requires_project_label(clean_owner, monkeypatch, capsys):
    _, _, objects, _ = clean_owner
    owner = cleanup_inventory(clean_owner, dev_running=False)
    host_runtime.clear_state(owner.root, owner.generation)
    data = json.loads(objects.read_text())
    data["djinn"]["Config"]["Labels"]["com.docker.compose.project"] = "foreign"
    objects.write_text(json.dumps(data))
    run = docker._run_captured

    def failed_discovery(argv, **kwargs):
        if argv[1] == "ps" and "--no-trunc" in argv and argv.count("--filter") == 1:
            return docker.RunResult(124)
        return run(argv, **kwargs)

    monkeypatch.setattr(docker, "_run_captured", failed_discovery)
    result = docker.compose_down()
    assert result.returncode == 1
    assert result.stderr == "dev ownership is unknown; preserving resources"
    assert records() == []
    assert markers() == {}
    assert "Post-mortem record failed (inspect)" in capsys.readouterr().err


def test_stopped_companion_skips_failing_stop_and_removes(fake_owner):
    _, binary, objects, log = fake_owner
    owner = cleanup_inventory(fake_owner, service="agent-docker", helper_running=False)
    behavior(fake_owner, failure={"argv": ["stop", "-t", "20", "helper-id"]})
    assert host_runtime.cleanup_owned(owner.root, "mine", str(binary))
    assert ["stop", "-t", "20", "helper-id"] not in calls(log)
    assert ["rm", "-f", "helper-id"] in calls(log)
    assert json.loads(objects.read_text()) == {}
    assert markers() == {}


def test_stopped_resource_failed_rm_records_before_retry(fake_owner, monkeypatch):
    _, binary, objects, log = fake_owner
    owner = cleanup_inventory(fake_owner, helper_running=False)
    behavior(fake_owner, failure={"argv": ["rm", "-f", "helper-id"]})
    remove = host_runtime.remove_runtime_object
    errors = []

    def removed(*args):
        try:
            return remove(*args)
        except GitSSHError as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(host_runtime, "remove_runtime_object", removed)
    with pytest.raises(GitSSHError, match="^operation refused$") as caught:
        host_runtime.cleanup_owned(owner.root, "mine", str(binary))
    assert caught.value is errors[0]
    assert markers() == {}
    assert records() == []
    assert "helper-id" in json.loads(objects.read_text())
    behavior(fake_owner, record={"trigger": "retry", "generation": "mine", "ids": ["helper-id"]})
    assert host_runtime.cleanup_owned(owner.root, "mine", str(binary), incident="retry")
    assert calls(log).count(["rm", "-f", "helper-id"]) == 2
    assert {s["id"] for doc in records() for s in doc["subjects"]} == {"helper-id"}
    assert json.loads(objects.read_text()) == {}
    assert markers() == {}
