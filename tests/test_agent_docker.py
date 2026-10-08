"""Runtime contracts; inspect fixtures are captured from the live daemon, anonymized."""

from __future__ import annotations

import json
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_desktop_runtime import calls, dev
from test_desktop_runtime import fake_owner as daemon_owner_fixture

from djinn_in_a_box.config.defaults import volume_categories
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import agent_docker, docker, host_runtime, hostctl
from djinn_in_a_box.core.exceptions import DeclarationSpecificationError
from djinn_in_a_box.core.host_runtime import GitRuntime

# Reuse the isolated daemon/observer fixture without duplicating a fake executable.
fake_owner = daemon_owner_fixture


def test_cleanup_fixture_isolates_hostctl_docker(fake_owner, monkeypatch):
    _, binary, _, log = fake_owner
    execute = subprocess.run

    def isolated(argv, **kwargs):
        assert argv[0] == str(binary), "test reached a real Docker client"
        assert kwargs["stdin"] is subprocess.DEVNULL
        return execute(argv, **kwargs)

    monkeypatch.setattr(host_runtime.subprocess, "run", isolated)
    assert hostctl.inspect_helper() is None
    assert calls(log) == [["container", "inspect", "djinn-hostctl"]]


@pytest.fixture
def evidence():
    root = Path(__file__).parent / "fixtures"
    actual = json.loads((root / "agent_docker_inspect.json").read_text())
    manifest = json.loads((root / "agent_docker_manifest.json").read_text())
    return actual, manifest


def test_real_profile_and_supported_limits(evidence):
    actual, manifest = evidence
    docker._require_agent_profile(actual, manifest)
    assert actual["HostConfig"]["NanoCpus"] == 2_000_000_000
    assert actual["HostConfig"]["Memory"] == 3 * 1024**3
    assert actual["HostConfig"]["MemoryReservation"] == 512 * 1024**2
    assert manifest["image"] == agent_docker.IMAGE
    assert all(
        m["Type"] != "volume" or m["Name"].startswith("djinn-test-") for m in actual["Mounts"]
    )


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        (None, "Image", "sha256:foreign"),
        ("Config", "User", "0"),
        ("Config", "Cmd", ["dockerd", "--host=tcp://0.0.0.0:2375"]),
        ("Config", "Entrypoint", ["dockerd-entrypoint.sh"]),
        ("Config", "Env", ["DJINN_FIREWALL_GATE=true", "DOCKER_TLS_CERTDIR="]),
        ("HostConfig", "Privileged", True),
        ("HostConfig", "CapAdd", ["CAP_SYS_ADMIN"]),
        ("HostConfig", "Devices", []),
        ("HostConfig", "DeviceRequests", [{"Driver": "nvidia", "Count": -1}]),
        ("HostConfig", "SecurityOpt", ["seccomp=unconfined", "apparmor=unconfined"]),
        ("HostConfig", "MaskedPaths", ["/proc/kcore"]),
        ("HostConfig", "PidMode", "host"),
        ("HostConfig", "IpcMode", "host"),
        ("HostConfig", "CgroupnsMode", "host"),
        ("HostConfig", "NetworkMode", "host"),
        ("HostConfig", "PortBindings", {"2375/tcp": [{"HostPort": "2375"}]}),
        ("HostConfig", "RestartPolicy", {"Name": "always", "MaximumRetryCount": 0}),
        ("HostConfig", "Tmpfs", {}),
        ("HostConfig", "NanoCpus", 4_000_000_000),
        ("HostConfig", "MemoryReservation", 0),
        (None, "Mounts", []),
    ],
)
def test_profile_refuses_untrusted_delivery(evidence, section, key, value):
    actual, manifest = evidence
    (actual if section is None else actual[section])[key] = value
    with pytest.raises(RuntimeError, match="profile differs"):
        docker._require_agent_profile(actual, manifest)


@pytest.mark.parametrize("creator", ["foreground", "headless", "detached"])
def test_creators_share_only_resolved_workspace(tmp_path, monkeypatch, creator):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("code", "here", "ro", "declared"):
        (tmp_path / name).mkdir()
    config = AppConfig(
        code_dir=tmp_path / "code",
        mounts={
            "bind": {"source": str(tmp_path / "declared"), "target": "/declared"},
            "data": {"volume": True, "target": "/data", "backup": "cache"},
        },
    )
    options = docker.ContainerOptions(
        docker_mode=docker.DockerMode.AGENT,
        mounts=(
            docker.ContainerMount(tmp_path / "here", Path("/home/dev/workspace")),
            docker.ContainerMount(tmp_path / "ro", Path("/readonly"), True),
        ),
    )
    fragments = []

    @contextmanager
    def owner(*args):
        yield GitRuntime(None, "fixture", observer=SimpleNamespace(poll=lambda: None))

    def execute(argv, **kwargs):
        if kwargs.get("capture_output"):
            assert kwargs["stdin"] is subprocess.DEVNULL
        files = [Path(argv[i + 1]) for i, word in enumerate(argv) if word == "-f"]
        fragments.append(json.loads(files[-1].read_text()))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(docker, "git_runtime", owner)
    monkeypatch.setattr(docker, "_prepare_companions", lambda *args: None)
    monkeypatch.setattr(docker, "_guard_dev_creation", lambda *args: None)
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda *args: [])
    monkeypatch.setattr(docker, "get_sops_age_key_mount_args", lambda *args: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *args: ([], ()))
    monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
    monkeypatch.setattr(docker.subprocess, "run", execute)
    if creator == "detached":
        assert docker.compose_up_detached(config, options).success
    else:
        assert docker.compose_run(config, options, interactive=creator == "foreground").success
    fragment = fragments[-1]
    workspace = fragment["services"]["agent-docker"]["volumes"]
    assert workspace == fragment["services"]["dev"]["volumes"]
    assert {(m["source"], m["target"], m["read_only"]) for m in workspace} == {
        (str(tmp_path / "code"), "/home/dev/projects", False),
        (str(tmp_path / "home/.djinn/sessions"), "/home/dev/sessions", False),
        (str(tmp_path / "here"), "/home/dev/workspace", False),
        (str(tmp_path / "ro"), "/readonly", True),
        (str(tmp_path / "declared"), "/declared", False),
        ("djinn-data", "/data", False),
    }
    assert all(
        m.get("bind", {}).get("create_host_path") is False for m in workspace if m["type"] == "bind"
    )


@pytest.mark.parametrize("source", ["/", "/home", "/home/dev"])
def test_sensitive_workspace_sources_are_accepted(source):
    mount = agent_docker.WorkspaceMount("bind", source, "/workspace")
    mount.require_safe_target()
    assert mount.compose()["source"] == source


@pytest.mark.parametrize("target", ["/", "/home", "/home/rootless/cache", "/run", "/usr/bin"])
def test_internal_workspace_targets_are_refused(target):
    with pytest.raises(ValueError, match="shadows"):
        agent_docker.WorkspaceMount("bind", "/fixture", target).require_safe_target()


@pytest.mark.parametrize("key", sorted(agent_docker.SELECTORS))
def test_selector_override_refused_before_generation(tmp_path, monkeypatch, key):
    monkeypatch.setattr(docker, "git_runtime", lambda *args: pytest.fail("allocated generation"))
    with pytest.raises(DeclarationSpecificationError, match="reserved"):
        docker.compose_run(
            AppConfig(code_dir=tmp_path),
            docker.ContainerOptions(docker_mode=docker.DockerMode.AGENT),
            env={key: "foreign"},
            interactive=False,
        )


@pytest.mark.parametrize("health", ["unhealthy", "starting", "late-healthy", "observer-dead"])
def test_readiness_refuses_failed_or_late_evidence(evidence, monkeypatch, health):
    actual, _ = evidence
    owner = SimpleNamespace(
        observer=SimpleNamespace(poll=lambda: 1 if health == "observer-dead" else None),
        generation="mine",
        docker_path="/docker",
    )
    actual["State"]["Health"]["Status"] = "healthy" if health == "late-healthy" else health
    ticks = iter([0, 61] if health == "late-healthy" else [0, 0, 61])
    monkeypatch.setattr(docker.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(host_runtime, "inspect_owned_resource", lambda *args, **kw: actual)
    with pytest.raises(RuntimeError, match="observer|health|timeout"):
        docker._wait_agent_healthy({}, owner, 60)


def test_cache_is_not_generation_state_or_default_backup(tmp_path):
    assert agent_docker.CACHE in volume_categories()["cache"]
    assert agent_docker.CACHE not in volume_categories()["data"]
    for name in ("agent-docker", "agent-docker-endpoint-foreign"):
        config = AppConfig(
            code_dir=tmp_path,
            mounts={name: {"volume": True, "target": "/workspace-data", "backup": "cache"}},
        )
        with pytest.raises(DeclarationSpecificationError):
            docker.resolve_declared_entries(
                config, docker.ContainerOptions(), runtime_targets=(), caller_env=None
            ).require_valid()


@pytest.mark.parametrize("git_enabled", [False, True])
def test_observer_stops_and_resumes_daemon_by_id(fake_owner, evidence, git_enabled):
    config, binary, objects, log = fake_owner
    if not git_enabled:
        config = config.model_copy(update={"git": config.git.model_copy(update={"identities": {}})})
    actual, manifest = evidence
    with host_runtime.git_runtime(config, "djinn") as owner:
        actual["Id"] = "agent-id"
        actual["Config"]["Labels"].update(
            {
                host_runtime.GENERATION_LABEL: owner.generation,
                "com.docker.compose.project": "djinn-in-a-box",
                "com.docker.compose.service": "agent-docker",
            }
        )
        owner.register("agent-docker", actual)
        state = host_runtime.read_state(owner.root)
        state["agent_docker"] = manifest
        host_runtime.save_state(owner.root, state)
        objects.write_text(json.dumps({"djinn": dev(owner), "agent-id": actual}))
        owner.retain()
        data = json.loads(objects.read_text())
        data["djinn"]["State"]["Running"] = False
        objects.write_text(json.dumps(data))
        deadline = time.monotonic() + 5
        while ["stop", "-t", "3", "agent-id"] not in calls(log):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        data = json.loads(objects.read_text())
        data["djinn"]["State"]["Running"] = True
        objects.write_text(json.dumps(data))
        while ["start", "agent-id"] not in calls(log):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert json.loads(objects.read_text())["agent-id"]["State"]["Running"]
        objects.write_text(json.dumps({"agent-id": json.loads(objects.read_text())["agent-id"]}))
        owner.observer.wait(timeout=5)
        assert not json.loads(objects.read_text())
        assert not (owner.root / "state.json").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "observer",
        "image",
        "existing",
        "cache-driver",
        "endpoint-options",
        "partial",
        "profile",
        "health",
        "success",
        "literal-dollar",
    ],
)
def test_preparation_fails_closed_and_cleans_partial_resources(
    tmp_path,
    monkeypatch,
    evidence,
    failure,
):
    from unittest.mock import MagicMock

    from djinn_in_a_box.config.models import ResourceLimits

    actual, manifest = evidence
    fixtures = Path(__file__).parent / "fixtures"
    image = json.loads((fixtures / "agent_docker_image.json").read_text())[0]
    endpoint = json.loads((fixtures / "agent_docker_endpoint.json").read_text())[0]
    resolved = json.loads((fixtures / "agent_docker_compose.json").read_text())
    generation = actual["Config"]["Labels"][host_runtime.GENERATION_LABEL]
    if failure == "literal-dollar":
        binding = next(
            m for m in resolved["services"]["agent-docker"]["volumes"] if m["type"] == "bind"
        )
        observed = next(m for m in actual["Mounts"] if m["Destination"] == binding["target"])
        binding["source"] += "/cash$$money"
        binding["target"] = "/cash$$money"
        observed["Source"] += "/cash$money"
        observed["Destination"] = "/cash$money"
    root = host_runtime.private_directory(tmp_path / "owner")
    observer = MagicMock()
    observer.poll.return_value = 0 if failure == "observer" else None
    observer.terminate.side_effect = lambda: setattr(observer.poll, "return_value", 0)
    owner = GitRuntime(
        root, generation, project="djinn-test", owns_generation=True, observer=observer
    )
    host_runtime.save_state(
        root,
        {
            "generation": generation,
            "project": "djinn-test",
            "container_name": "fixture-dev",
            "resources": {},
            "volumes": [],
        },
    )
    config = AppConfig(
        code_dir=tmp_path,
        resources=ResourceLimits(
            cpu_limit=2, memory_limit="3G", cpu_reservation=1, memory_reservation="512M"
        ),
    )
    fragment = {
        "services": {
            "dev": {},
            "agent-docker": {
                "volumes": [
                    m
                    for m in resolved["services"]["agent-docker"]["volumes"]
                    if m["target"] not in (agent_docker.ENDPOINT, agent_docker.DATA_ROOT)
                ]
            },
        },
        "volumes": resolved["volumes"],
    }
    inventory = {}
    created = False
    if failure == "image":
        image["RepoDigests"] = ["docker:29-dind-rootless"]
    if failure == "existing":
        actual["Config"]["Labels"][host_runtime.GENERATION_LABEL] = "foreign"
        inventory[actual["Id"]] = actual

    def inspect(name, binary, resource="container", **kwargs):
        if resource == "image":
            return (
                image if name == agent_docker.IMAGE else {**image, "Id": manifest["dev_image_id"]}
            )
        if resource == "volume":
            if name == "djinn-test-cache":
                return {
                    **endpoint,
                    "Name": name,
                    "Options": {"o": "bind", "device": "/"} if failure == "cache-driver" else None,
                }
            return inventory.get(name)
        return inventory.get(name)

    def captured(argv, **kwargs):
        if argv[1:3] == ["volume", "create"]:
            endpoint["Name"] = argv[-1]
            endpoint["Labels"][host_runtime.GENERATION_LABEL] = generation
            if failure == "endpoint-options":
                endpoint["Options"]["o"] = "mode=0777"
            inventory[argv[-1]] = endpoint
            return docker.RunResult(0, argv[-1] + "\n")
        pytest.fail(f"Unexpected captured Docker call: {argv}")

    def compose(argv, **kwargs):
        nonlocal created
        if "config" in argv:
            resolved["volumes"]["agent-docker-endpoint"]["name"] = endpoint["Name"]
            return docker.RunResult(0, json.dumps(resolved))
        assert "up" in argv and argv[-1] == "agent-docker"
        created = True
        if failure == "profile":
            actual["HostConfig"]["Privileged"] = True
        if failure == "health":
            actual["State"]["Health"]["Status"] = "unhealthy"
        inventory[actual["Id"]] = actual
        return docker.RunResult(
            1 if failure == "partial" else 0,
            stderr="creation failed" if failure == "partial" else "",
        )

    def command(binary, *args):
        if args[0] == "stop":
            inventory[args[-1]]["State"]["Running"] = False
        else:
            assert args[0] in ("rm", "volume")
            inventory.pop(args[-1], None)

    def no_consumers(argv, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert argv[1] == "ps"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(host_runtime, "inspect_object", inspect)
    monkeypatch.setattr(host_runtime, "run_runtime_command", command)
    monkeypatch.setattr(
        docker,
        "_service_inspect",
        lambda *args: actual if created or failure == "existing" else None,
    )
    monkeypatch.setattr(docker, "_run_captured", captured)
    monkeypatch.setattr(docker, "_run_compose", compose)
    monkeypatch.setattr(host_runtime.subprocess, "run", no_consumers)
    try:
        if failure in ("success", "literal-dollar"):
            docker._prepare_agent_docker(
                config,
                docker.ContainerOptions(docker_mode=docker.DockerMode.AGENT),
                fragment,
                owner,
            )
            delivered = fragment["services"]["dev"]["volumes"][-1]
            assert delivered["read_only"] is True
            assert delivered["target"] == agent_docker.DEV_ENDPOINT
            assert manifest["image_id"] == inventory[actual["Id"]]["Image"]
        else:
            with pytest.raises(RuntimeError):
                docker._prepare_agent_docker(
                    config,
                    docker.ContainerOptions(docker_mode=docker.DockerMode.AGENT),
                    fragment,
                    owner,
                )
    finally:
        owner.close()
    if failure == "existing":
        assert list(inventory) == [actual["Id"]]
    else:
        assert inventory == {}
    if failure in ("observer", "image", "existing", "cache-driver", "endpoint-options"):
        assert not created
    assert not (root / "state.json").exists()


@pytest.mark.parametrize("failure", ["foreign", "partial", "profile", "wait", "success"])
def test_firewall_initializer_provenance_and_partial_cleanup(tmp_path, monkeypatch, failure):
    from unittest.mock import MagicMock

    fixtures = Path(__file__).parent / "fixtures"
    actual = json.loads((fixtures / "agent_docker_firewall_inspect.json").read_text())
    generation = actual["Config"]["Labels"][host_runtime.GENERATION_LABEL]
    root = host_runtime.private_directory(tmp_path / "owner")
    owner = GitRuntime(root, generation, project="djinn-test", owns_generation=True)
    parent_id = actual["HostConfig"]["NetworkMode"].split(":", 1)[1]
    owner.resources["agent-docker"] = {
        "id": parent_id,
        "service": "agent-docker",
        "project": owner.project,
    }
    host_runtime.save_state(
        root,
        {
            "generation": generation,
            "container_name": "fixture-dev",
            "resources": owner.resources,
            "volumes": [],
        },
    )
    manifest = {
        "name": actual["Name"].lstrip("/").removesuffix("-firewall"),
        "dev_image_id": actual["Image"],
        "endpoint_volume": actual["Mounts"][0]["Name"],
    }
    inventory = {actual["Id"]: actual} if failure == "foreign" else {}
    name = actual["Name"].lstrip("/")
    commands = []

    def inspect(identity, binary, resource="container", **kwargs):
        if identity == name:
            return inventory.get(actual["Id"])
        return inventory.get(identity)

    def captured(argv, **kwargs):
        if argv[1] == "create":
            inventory[actual["Id"]] = actual
            if failure == "profile":
                actual["HostConfig"]["CapAdd"] = ["CAP_SYS_ADMIN"]
            return docker.RunResult(1 if failure == "partial" else 0, actual["Id"] + "\n")
        if argv[1] == "wait":
            return docker.RunResult(0, "23\n" if failure == "wait" else "0\n")
        assert argv[1] == "logs"
        return docker.RunResult(0, "initializer failed\n")

    def command(binary, *args):
        commands.append(args)
        if args[0] == "rm":
            inventory.pop(args[-1])

    monkeypatch.setattr(host_runtime, "inspect_object", inspect)
    monkeypatch.setattr(host_runtime, "run_runtime_command", command)
    monkeypatch.setattr(docker, "_run_captured", captured)
    # The initializer has no workspace/data volumes; cleanup therefore makes no ps call.
    monkeypatch.setattr(host_runtime.subprocess, "run", MagicMock(side_effect=AssertionError))
    try:
        if failure == "success":
            docker._agent_firewall(owner, manifest)
            assert "agent-docker-firewall" not in owner.resources
            assert commands == [("start", actual["Id"]), ("rm", actual["Id"])]
        else:
            with pytest.raises(RuntimeError):
                docker._agent_firewall(owner, manifest)
    finally:
        owner.close()
    assert bool(inventory) == (failure == "foreign")


@pytest.mark.parametrize("creator", ["foreground", "headless", "detached"])
def test_observer_death_after_preparation_never_admits_dev(tmp_path, monkeypatch, creator):
    @contextmanager
    def owner(*args):
        yield GitRuntime(None, "fixture", observer=SimpleNamespace(poll=lambda: 1))

    executed = []
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(docker, "git_runtime", owner)
    monkeypatch.setattr(docker, "_prepare_companions", lambda *args: None)
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda *args: [])
    monkeypatch.setattr(docker, "get_sops_age_key_mount_args", lambda *args: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *args: ([], ()))
    monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
    monkeypatch.setattr(
        docker.subprocess,
        "run",
        lambda *args, **kw: executed.append(args)
        or subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    with pytest.raises(RuntimeError, match="functioning host observer"):
        config = AppConfig(code_dir=tmp_path)
        options = docker.ContainerOptions(docker_mode=docker.DockerMode.AGENT)
        if creator == "detached":
            docker.compose_up_detached(config, options)
        else:
            docker.compose_run(config, options, interactive=creator == "foreground")
    assert executed == []
