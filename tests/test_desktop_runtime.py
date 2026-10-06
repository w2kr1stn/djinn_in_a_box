"""Real isolated observers against an absolute fake Docker executable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from djinn_in_a_box.config.ssh import GitConfig
from djinn_in_a_box.core import docker, host_runtime
from djinn_in_a_box.core.ssh_delivery import GitSSHError


@pytest.fixture
def fake_owner(git_inputs, tmp_path, monkeypatch):
    directory = tmp_path / "trusted"
    directory.mkdir()
    binary = directory / "docker"
    objects = directory / "objects.json"
    log = directory / "calls.jsonl"
    objects.write_text("{}")
    binary.write_text(f"""#!{sys.executable}
import json, pathlib, sys
store = pathlib.Path({str(objects)!r})
log = pathlib.Path({str(log)!r})
data = json.loads(store.read_text())
args = sys.argv[1:]
# Docker 29 daemon messages for missing objects; volumes use lower case.
MISSING = {{
    'container': 'No such container: %s',
    'image': 'No such image: %s',
    'volume': 'get %s: no such volume',
}}
with log.open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
if len(args) > 1 and args[1] == 'inspect':
    name = args[2]
    if name not in data:
        print('Error response from daemon: ' + MISSING[args[0]] % name, file=sys.stderr)
        sys.exit(1)
    print(json.dumps([data[name]]))
elif args[0] == 'ps':
    pass
elif args[:2] == ['rm', '-f'] or args[:2] == ['volume', 'rm']:
    for key in list(data):
        if key == args[-1] or data[key].get('Id') == args[-1]:
            data.pop(key, None)
    store.write_text(json.dumps(data))
elif args[0] in ('stop', 'start'):
    data[args[1]]['State']['Running'] = args[0] == 'start'
    store.write_text(json.dumps(data))
else:
    sys.exit(2)
""")
    binary.chmod(0o700)
    monkeypatch.setattr(host_runtime, "DOCKER_EXECUTABLE", str(binary))
    monkeypatch.setattr(docker, "DOCKER_EXECUTABLE", str(binary))
    monkeypatch.setattr(host_runtime, "CONTAINER_USER_UID", os.getuid())
    try:
        yield git_inputs, binary, objects, log
    finally:
        metadata = host_runtime.runtime_root() / "state.json"
        if metadata.exists():
            state = json.loads(metadata.read_text())
            if state.get("observer_pid", -1) > 0:
                host_runtime.stop_owned_process(state["observer_pid"], state["observer_token"])
            if state.get("agent_pid", -1) > 0:
                host_runtime.stop_owned_process(state["agent_pid"], state["agent_token"])


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
        "State": {"Running": running},
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
        objects.write_text(json.dumps(data))
        owner.register("audio-helper", helper, "djinn-desktop-audio")
        owner.retain()
        (planted / "docker").write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        (planted / "docker").chmod(0o700)
        data["djinn"] = dev(owner, False)
        objects.write_text(json.dumps(data))
        wait_for(lambda: ["stop", "helper-id"] in calls(log))
        assert json.loads(objects.read_text())["helper-id"]["State"]["Running"] is False
        assert (owner.root / "state.json").exists()
        data = json.loads(objects.read_text())
        data["djinn"] = dev(owner)
        objects.write_text(json.dumps(data))
        wait_for(lambda: ["start", "helper-id"] in calls(log))
        data = json.loads(objects.read_text())
        del data["djinn"]
        objects.write_text(json.dumps(data))
        wait_for(lambda: not (owner.root / "state.json").exists())
        assert "helper-id" not in json.loads(objects.read_text())
        assert "djinn-desktop-audio" not in json.loads(objects.read_text())
        assert ["rm", "-f", "helper-id"] in calls(log)
        assert ["volume", "rm", "djinn-desktop-audio"] in calls(log)
        assert not marker.exists()


@pytest.mark.parametrize("creator", ["foreground", "headless", "detached", "clean"])
def test_empty_identity_pending_guard_refuses_every_path(fake_owner, creator, monkeypatch):
    config, binary, objects, log = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
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
    record = {"id": "proxy-id", "service": "docker-proxy"}
    state = {
        "generation": generation,
        "container_name": "djinn",
        "dev_id": "dev-id",
        "resources": {"docker-proxy": record},
        "volumes": [],
    }
    (root / "state.json").write_text(json.dumps(state))
    obj = {
        "Id": "proxy-id",
        "Config": {
            "Labels": {
                host_runtime.GENERATION_LABEL: generation,
                "com.docker.compose.project": "djinn-in-a-box",
                "com.docker.compose.service": "docker-proxy",
            }
        },
    }
    objects.write_text(json.dumps({"proxy-id": obj}))
    owner = host_runtime.GitRuntime(root, generation, docker_path=str(binary), owns_generation=True)
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
        objects.write_text(
            json.dumps(
                {
                    "proxy-id": obj,
                    "djinn": {
                        "Id": "dev-id",
                        "State": {"Running": True},
                        "Config": {"Labels": {host_runtime.GENERATION_LABEL: generation}},
                    },
                }
            )
        )
    docker.cleanup_docker_proxy(docker.DockerMode.PROXY, owner=owner)
    mutations = [c for c in calls(log) if c[0] in {"rm", "stop", "volume"}]
    assert mutations == ([["rm", "-f", "proxy-id"]] if condition == "owned" else [])
    assert ("proxy-id" in json.loads(objects.read_text())) == (condition != "owned")


def test_clean_removes_dev_before_helpers_and_joins_outside_guard(fake_owner, monkeypatch):
    config, binary, objects, log = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
    monkeypatch.setattr(docker, "is_own_container", lambda *args: False)
    with host_runtime.git_runtime(config, "djinn") as owner:
        objects.write_text(json.dumps({"djinn": dev(owner)}))
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


@pytest.mark.parametrize("caller", ["foreground", "headless"])
@pytest.mark.parametrize("condition", ["refused", "replacement", "owned", "error", "timeout"])
def test_actual_caller_finally_requires_acquired_owner(fake_owner, caller, condition, monkeypatch):
    import typer

    from djinn_in_a_box.commands import container
    from djinn_in_a_box.core import agent_runner
    from djinn_in_a_box.core.config_workflow import WorkflowPreparationResult

    config, binary, objects, log = fake_owner
    config = config.model_copy(update={"git": GitConfig()})
    root = host_runtime.runtime_root(create=True)
    owner = host_runtime.GitRuntime(root, "mine", docker_path=str(binary), owns_generation=True)
    state = {
        "generation": "winner" if condition == "refused" else "mine",
        "container_name": "djinn",
        "resources": {"docker-proxy": {"id": "proxy-id", "service": "docker-proxy"}},
        "volumes": [],
    }
    (root / "state.json").write_text(json.dumps(state))
    objects.write_text(
        json.dumps(
            {
                "proxy-id": {
                    "Id": "proxy-id",
                    "Config": {
                        "Labels": {
                            host_runtime.GENERATION_LABEL: state["generation"],
                            "com.docker.compose.project": "djinn-in-a-box",
                            "com.docker.compose.service": "docker-proxy",
                        }
                    },
                }
            }
        )
    )

    def execute(*args, **kwargs):
        assert args[1].docker_mode is docker.DockerMode.PROXY
        if condition == "refused":
            raise GitSSHError("already owns the runtime")
        if condition == "replacement":
            state["generation"] = "replacement"
            (root / "state.json").write_text(json.dumps(state))
        return docker.RunResult(
            124 if condition == "timeout" else 127 if condition == "error" else 0,
            stdout="agent-output",
            owner=owner,
        )

    if caller == "headless":
        monkeypatch.setattr(agent_runner, "ensure_network", lambda: True)
        monkeypatch.setattr(agent_runner, "compose_run", execute)
        if condition == "refused":
            with pytest.raises(GitSSHError, match="already owns"):
                agent_runner.run_headless_agent(
                    "codex",
                    "fixture",
                    app_config=config,
                    resolved_mounts=(),
                    docker_mode=docker.DockerMode.PROXY,
                )
        else:
            result = agent_runner.run_headless_agent(
                "codex",
                "fixture",
                app_config=config,
                resolved_mounts=(),
                docker_mode=docker.DockerMode.PROXY,
            )
            assert result.stdout == "agent-output"
            assert result.returncode == (
                124 if condition == "timeout" else 127 if condition == "error" else 0
            )
    else:
        monkeypatch.setattr(container, "load_config", lambda: config)
        monkeypatch.setattr(container, "preflight", lambda *args, **kwargs: None)
        monkeypatch.setattr(container, "ensure_network", lambda: True)
        monkeypatch.setattr(
            container,
            "prepare_config_workflow",
            lambda *args, **kwargs: WorkflowPreparationResult(True),
        )
        monkeypatch.setattr(container, "get_shell_mount_args", lambda *args: [])
        monkeypatch.setattr(container, "banner", lambda *args, **kwargs: None)
        monkeypatch.setattr(container, "compose_run", execute)
        with pytest.raises(typer.Exit) as raised:
            container.start(docker=True)
        assert raised.value.exit_code == (
            1
            if condition == "refused"
            else 124
            if condition == "timeout"
            else 127
            if condition == "error"
            else 0
        )
    removals = [c for c in calls(log) if c[0] in {"rm", "stop"}]
    assert removals == (
        [["rm", "-f", "proxy-id"]] if condition in {"owned", "error", "timeout"} else []
    )


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
        objects.write_text(json.dumps({"djinn": dev(owner)}))

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
    state = {"generation": "mine", "dev_id": "dev-id"}
    (root / "state.json").write_text(json.dumps(state))
    objects.write_text(json.dumps({"djinn": dev(owner)}))
    monkeypatch.setattr(
        host_runtime, "_save", lambda *args: pytest.fail("state rewritten after guard release")
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
    objects.write_text(json.dumps({"djinn": dev, "helper-id": helper}))
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
