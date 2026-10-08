"""P2 admission contracts using the anonymized, measured P1 inspect shapes."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_hostctl_sealing import ground as sealing_ground
from test_hostctl_sealing import trust

from djinn_in_a_box.commands.doctor import Status, agent_docker_checks, hostctl_boundary_checks
from djinn_in_a_box.config.models import AppConfig, HostctlConfig
from djinn_in_a_box.core import agent_docker, docker, host_runtime, host_sealing, hostctl

ground = sealing_ground


@pytest.fixture
def managed(ground, monkeypatch):
    fixtures = Path(__file__).parent / "fixtures"
    companion = json.loads((fixtures / "agent_docker_inspect.json").read_text())
    manifest = json.loads((fixtures / "agent_docker_manifest.json").read_text())
    image = json.loads((fixtures / "agent_docker_image.json").read_text())[0]
    endpoint = json.loads((fixtures / "agent_docker_endpoint.json").read_text())[0]
    for row in manifest["mounts"]:
        if row[0] == "bind":
            source = ground.paths["bus"].parent / "workspace" / Path(row[1]).name
            source.mkdir(parents=True, exist_ok=True)
            observed = next(m for m in companion["Mounts"] if m["Destination"] == row[2])
            observed["Source"] = row[1] = str(source)
    cache = {
        "Name": manifest["cache_volume"],
        "Driver": "local",
        "Options": None,
        "Mountpoint": str(ground.paths["data"] / "volumes/cache/_data"),
    }
    for volume in (endpoint, cache):
        for mount in companion["Mounts"]:
            if mount.get("Name") == volume["Name"]:
                mount["Source"] = volume["Mountpoint"]
    workspace = [
        copy.deepcopy(m)
        for m in companion["Mounts"]
        if m["Destination"] not in (agent_docker.ENDPOINT, agent_docker.DATA_ROOT)
    ]
    dev_endpoint = copy.deepcopy(
        next(m for m in companion["Mounts"] if m["Destination"] == agent_docker.ENDPOINT)
    )
    dev_endpoint.update(Destination=agent_docker.DEV_ENDPOINT, RW=False, Mode="ro")
    dev = {
        "Id": "a" * 64,
        "Name": "/djinn-test-dev",
        "Image": manifest["dev_image_id"],
        "State": {"Running": True},
        "Mounts": [*workspace, dev_endpoint],
        "Config": {
            "Env": [
                "DOCKER_HOST=" + agent_docker.DOCKER_HOST,
                "DOCKER_CONTEXT",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
            ],
            "Labels": {
                host_runtime.GENERATION_LABEL: endpoint["Labels"][host_runtime.GENERATION_LABEL],
                "com.docker.compose.project": "djinn-test",
                "com.docker.compose.service": "dev",
            },
            "Image": "dev:1",
        },
        "HostConfig": {"NetworkMode": manifest["network"]},
        "NetworkSettings": copy.deepcopy(companion["NetworkSettings"]),
    }
    state = {
        "generation": endpoint["Labels"][host_runtime.GENERATION_LABEL],
        "dev_id": dev["Id"],
        "container_name": "djinn-test-dev",
        "resources": {
            "agent-docker": {
                "id": companion["Id"],
                "project": "djinn-test",
                "service": "agent-docker",
            }
        },
        "volumes": [endpoint["Name"]],
        "agent_docker": manifest,
    }
    network = {"Id": manifest["network_id"], "Name": manifest["network"]}
    objects = ground.objects
    objects.update(
        {
            dev["Id"]: dev,
            "djinn-test-dev": dev,
            companion["Id"]: companion,
            manifest["image"]: image,
            endpoint["Name"]: endpoint,
            cache["Name"]: cache,
            network["Id"]: network,
        }
    )
    for mount in workspace:
        if mount["Type"] == "volume":
            objects[mount["Name"]] = {
                "Name": mount["Name"],
                "Driver": "local",
                "Options": None,
                "Mountpoint": mount["Source"],
            }
    users = [dev["Id"], companion["Id"]]
    peers = [dev["Id"], companion["Id"]]
    calls = []

    def run(argv, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert 0 < kwargs["timeout"] <= 5
        calls.append(argv[1:])
        args = argv[1:]
        if len(args) > 1 and args[1] == "inspect":
            obj = objects.get(args[2])
            if obj is None:
                error = (
                    "Error response from daemon: get " + args[2] + ": no such volume"
                    if args[0] == "volume"
                    else "Error response from daemon: No such " + args[0] + ": " + args[2]
                )
                return subprocess.CompletedProcess(argv, 1, "[]\n", error + "\n")
            output = json.dumps([obj])
        elif args[0] == "ps":
            if any(a.startswith("volume=") for a in args):
                output = "\n".join(users)
            elif "--filter" in args:
                output = companion["Id"]
            else:
                output = "\n".join(peers)
        elif args[:2] == ["volume", "ls"]:
            output = endpoint["Name"]
        elif args[0] == "info":
            output = json.dumps({"DockerRootDir": str(ground.paths["data"])})
        else:
            pytest.fail(f"unexpected or mutating Docker call: {args}")
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(subprocess, "run", run)
    # Undo ground's inspector/command substitutes so real captured wrappers run.
    monkeypatch.setattr(host_runtime, "inspect_object", _INSPECT)
    monkeypatch.setattr(hostctl, "command", _COMMAND)
    monkeypatch.setattr(docker, "DOCKER_EXECUTABLE", "/isolated/docker")
    monkeypatch.setattr(hostctl, "DOCKER_EXECUTABLE", "/isolated/docker")
    monkeypatch.setattr(hostctl, "HELPER_NAME", "djinn-hostctl")
    monkeypatch.setitem(docker._SERVICE_CONTAINER_NAMES, "dev", "djinn-test-dev")
    host_runtime.save_state(ground.paths["runtime"], state)
    return SimpleNamespace(
        dev=dev,
        companion=companion,
        manifest=manifest,
        state=state,
        image=image,
        endpoint=endpoint,
        cache=cache,
        network=network,
        users=users,
        peers=peers,
        calls=calls,
        objects=objects,
        ground=ground,
    )


_INSPECT = host_runtime.inspect_object
_COMMAND = hostctl.command


def pure(m, **kwargs):
    return agent_docker.verify_endpoint(
        m.dev, m.state, m.companion, m.image, m.endpoint, m.cache, m.network, m.users, **kwargs
    )


def readonly_workspace(m):
    observed = next(mount for mount in m.dev["Mounts"] if mount["Type"] == "bind")
    config = AppConfig(code_dir=m.ground.paths["bus"], mounts={
        "ro": {
            "source": observed["Source"], "target": observed["Destination"], "read_only": True,
        },
    })
    declarations = docker.resolve_declared_entries(
        config, docker.ContainerOptions(), runtime_targets=[], caller_env=None,
    )
    declarations.require_valid()
    row = declarations.compose_fragment()["services"]["dev"]["volumes"][0]
    assert row["read_only"] is True
    companion = next(
        mount for mount in m.companion["Mounts"] if mount["Destination"] == row["target"]
    )
    expected = next(mount for mount in m.manifest["mounts"] if mount[2] == row["target"])
    observed["RW"] = companion["RW"] = expected[3] = not row["read_only"]
    host_runtime.save_state(m.ground.paths["runtime"], m.state)
    return observed, companion


def test_verified_agent_removes_only_its_docker_causes(managed, monkeypatch):
    m = managed
    assert pure(m).kind == "agent"
    actual = host_sealing.assess(m.dev)
    assert actual.state == "sealed" and actual.agent == pure(m).evidence, actual
    # A second Docker peer must remain visible.
    peer = copy.deepcopy(m.companion)
    peer.update(Id="b" * 64, Name="/foreign-proxy", Mounts=[])
    m.objects[peer["Id"]] = peer
    m.peers.append(peer["Id"])
    result = host_sealing.assess(m.dev)
    assert result.state == "unsealed" and len(result.causes) == 1
    assert "foreign-proxy" in result.causes[0]
    assert not any("djinn-test-agent-docker" in c for c in result.causes)
    m.peers.remove(peer["Id"])
    m.dev["Mounts"].append({"Type": "bind", "Source": "/", "Destination": "/host", "RW": False})
    assert any("host root bind" in c for c in host_sealing.assess(m.dev).causes)
    # The pure verifier cannot issue inspections or commands.
    monkeypatch.setattr(
        host_runtime, "inspect_object", lambda *a, **k: pytest.fail("pure verifier IO")
    )
    assert pure(m).kind == "agent"


@pytest.mark.parametrize(
    "key",
    list(agent_docker.HOST_PROFILE) + ["NanoCpus", "Memory", "MemoryReservation", "MemorySwap"],
)
def test_exact_host_profile(managed, key):
    managed.companion["HostConfig"][key] = "tampered"
    result = host_sealing.assess(managed.dev)
    assert result.state == "unknown"
    with pytest.raises(hostctl.HostctlError):
        result.require(allow_unsealed=True)


@pytest.mark.parametrize(
    "case",
    [
        "env-extra",
        "env-path",
        "env-duplicate",
        "env-missing",
        "command",
        "entrypoint",
        "user",
        "healthcheck",
        "image",
        "generation",
        "dev-id",
        "companion-id",
        "companion-absent",
        "state-absent",
        "network-id",
        "extra-network",
        "dev-network",
        "endpoint-driver",
        "endpoint-options",
        "cache-options",
        "workspace-readonly",
        "workspace-readonly-dev-flip",
        "workspace-readonly-companion-flip",
        "workspace-extra",
        "workspace-dev-lacks",
        "workspace-volume-subpath",
        "endpoint-writable",
        "endpoint-alias",
        "endpoint-shadow",
        "endpoint-backing",
        "foreign-consumer",
        "unhealthy",
        "stopped",
        "context",
        "inspect-error",
        "malformed",
        "dev-env-malformed",
        "mount-mode-malformed",
        "companion-propagation",
        "exposed-ports",
    ],
)
def test_agent_evidence_never_fails_open(managed, monkeypatch, case):
    m = managed
    if case.startswith("workspace-readonly"):
        observed, companion = readonly_workspace(m)
        assert observed["RW"] is False and companion["RW"] is False
        assert pure(m).kind == "agent"
        docker.require_agent_profile(m.companion, m.manifest)
        if case == "workspace-readonly":
            assert host_sealing.assess(m.dev).state == "sealed"
            assert all(row.status is Status.PASS for row in agent_docker_checks(daemon=True))
            return
        if case.endswith("dev-flip"):
            observed["RW"] = True
        else:
            companion["RW"] = True
            with pytest.raises(RuntimeError, match="profile differs"):
                docker.require_agent_profile(m.companion, m.manifest)
        assert pure(m).kind == "unknown"
    elif case.startswith("env-"):
        env = m.companion["Config"]["Env"]
        if case == "env-extra":
            env.append("DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--net=host")
        elif case == "env-path":
            env[2] = "PATH=/workspace/bin"
        elif case == "env-duplicate":
            env.append(env[0])
        else:
            env.pop()
    elif case in {"command", "entrypoint", "user", "healthcheck"}:
        key = {
            "command": "Cmd",
            "entrypoint": "Entrypoint",
            "user": "User",
            "healthcheck": "Healthcheck",
        }[case]
        m.companion["Config"][key] = "tampered"
    elif case == "image":
        m.companion["Image"] = "sha256:foreign"
    elif case == "generation":
        m.dev["Config"]["Labels"][host_runtime.GENERATION_LABEL] = "foreign"
    elif case == "dev-id":
        m.dev["Id"] = "foreign"
    elif case == "companion-id":
        m.companion["Id"] = "foreign"
    elif case == "companion-absent":
        m.objects.pop(m.companion["Id"])
    elif case == "state-absent":
        (m.ground.paths["runtime"] / "state.json").unlink()
    elif case == "network-id":
        m.companion["NetworkSettings"]["Networks"][m.manifest["network"]]["NetworkID"] = "foreign"
    elif case == "extra-network":
        m.companion["NetworkSettings"]["Networks"]["foreign"] = {}
    elif case == "dev-network":
        m.dev["NetworkSettings"]["Networks"][m.manifest["network"]]["NetworkID"] = "foreign"
    elif case == "endpoint-driver":
        m.endpoint["Driver"] = "foreign"
    elif case == "endpoint-options":
        m.endpoint["Options"] = {"o": "bind", "device": "/"}
    elif case == "cache-options":
        m.cache["Options"] = {"o": "bind", "device": "/"}
    elif case == "workspace-extra":
        m.companion["Mounts"].append(copy.deepcopy(m.companion["Mounts"][0]))
    elif case == "workspace-dev-lacks":
        m.dev["Mounts"].pop(0)
    elif case == "workspace-volume-subpath":
        next(mount for mount in m.dev["Mounts"] if mount.get("Name") == "djinn-test-inner-data")[
            "Source"
        ] += "/subpath"
    elif case == "endpoint-writable":
        m.dev["Mounts"][-1]["RW"] = True
    elif case == "endpoint-alias":
        alias = copy.deepcopy(m.dev["Mounts"][-1])
        alias["Destination"] = "/alias"
        m.dev["Mounts"].append(alias)
    elif case == "endpoint-shadow":
        alias = copy.deepcopy(m.dev["Mounts"][0])
        alias["Destination"] = agent_docker.DEV_ENDPOINT + "/socket"
        m.dev["Mounts"].append(alias)
    elif case == "endpoint-backing":
        m.dev["Mounts"][-1]["Source"] = "/foreign"
    elif case == "foreign-consumer":
        m.users.append("foreign")
    elif case == "unhealthy":
        m.companion["State"]["Health"]["Status"] = "unhealthy"
    elif case == "stopped":
        m.companion["State"]["Running"] = False
    elif case == "context":
        m.dev["Config"]["Env"].append("DOCKER_CONTEXT=foreign")
    elif case == "dev-env-malformed":
        m.dev["Config"]["Env"] = [5]
    elif case == "mount-mode-malformed":
        m.dev["Mounts"][-1]["RW"] = 0
    elif case == "exposed-ports":
        m.companion["Config"]["ExposedPorts"]["8888/tcp"] = {}
    elif case == "companion-propagation":
        m.companion["Mounts"][0]["Propagation"] = "rshared"
    elif case == "inspect-error":
        monkeypatch.setattr(
            host_runtime,
            "inspect_object",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("Docker inspection failed; runtime ownership is unknown")
            ),
        )
    else:
        m.companion.pop("HostConfig")
    result = host_sealing.assess(m.dev)
    assert result.state == "unknown", result
    with pytest.raises(hostctl.HostctlError):
        result.require(allow_unsealed=True)
    rows = agent_docker_checks(daemon=True)
    assert rows[0].status is not Status.PASS


@pytest.mark.parametrize("mode", ["agent", "none", "direct", "foreign", "foreign-direct", "absent"])
def test_doctor_reports_observed_endpoint_without_repair(managed, mode):
    m = managed
    if mode in {"none", "direct", "foreign", "foreign-direct"}:
        m.dev["Mounts"] = []
        m.dev["Config"]["Env"] = (
            []
            if mode == "none"
            else [
                "DOCKER_HOST="
                + (
                    "unix:///var/run/docker.sock"
                    if mode == "direct"
                    else "tcp://user:secret@foreign:2375"
                )
            ]
        )
    elif mode == "absent":
        m.objects.pop("djinn-test-dev")
    if mode == "foreign-direct":
        m.dev["Mounts"] = [
            {
                "Type": "bind",
                "Source": str(m.ground.socket),
                "Destination": "/var/run/docker.sock",
                "RW": True,
            }
        ]
    rows = agent_docker_checks(daemon=True)
    expected = {
        "agent": "agent",
        "none": "none",
        "direct": "host-direct",
        "foreign": "unknown",
        "foreign-direct": "unknown",
        "absent": "none",
    }[mode]
    assert rows[0].detail.startswith(expected + ";")
    assert "secret" not in " ".join(r.detail for r in rows)
    if mode == "agent":
        assert all(r.status is Status.PASS for r in rows)
        assert m.companion["Id"] in rows[1].detail
        assert "29.8.2" in rows[1].detail and "healthy" in rows[2].detail
        assert m.cache["Name"] in rows[3].detail and "overlay2" in rows[3].detail
    else:
        assert "not in use" in rows[0].detail
        assert all(r.detail == "not in use" for r in rows[1:4])
        assert rows[4].status is Status.WARN and m.companion["Id"] in rows[4].detail
    assert all(call[0] in {"container", "image", "volume", "network", "ps"} for call in m.calls)


def test_planned_assessment_carries_prepared_companion(managed, monkeypatch):
    m = managed
    planned = copy.deepcopy(m.dev)
    planned["Id"] = "planned"
    m.users.remove(m.dev["Id"])
    m.state.pop("dev_id")
    host_runtime.save_state(m.ground.paths["runtime"], m.state)
    checked = host_sealing.assess(planned, planned_generation=m.state["generation"])
    assert checked.state == "sealed" and checked.agent, checked
    helper = {
        "Id": "helper-id",
        "State": {"Running": True},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "gen-a"}},
    }
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: helper)
    original = hostctl.command
    monkeypatch.setattr(
        hostctl,
        "command",
        lambda *a, **k: json.dumps({"generation": "gen-a", "paused": True})
        if a[0] == "exec"
        else original(*a, **k),
    )
    hostctl.save_private("opening.json", {"generation": "gen-a", "dev_id": None})
    hostctl.guard_dev_start(planned, m.state["generation"])
    opening = json.loads((hostctl.state_root() / "opening.json").read_text())
    assert opening["creator"] == m.state["generation"] and opening["agent"] == checked.agent
    m.users.append(m.dev["Id"])
    m.state["dev_id"] = m.dev["Id"]
    host_runtime.save_state(m.ground.paths["runtime"], m.state)
    assert host_sealing.assess(m.dev).agent == checked.agent


def test_planned_delivery_keeps_workspace_backing(managed):
    m = managed
    observed, _ = readonly_workspace(m)
    m.objects[m.network["Name"]] = m.network
    delivery = {
        "services": {
            "dev": {
                "image": "dev:1",
                "labels": m.dev["Config"]["Labels"],
                "environment": {"DOCKER_HOST": agent_docker.DOCKER_HOST},
                "networks": {"managed": {}},
                "volumes": [
                    {
                        "type": mount["Type"],
                        "source": mount.get("Name", mount["Source"]),
                        "target": mount["Destination"],
                        "read_only": not mount["RW"],
                    }
                    for mount in m.dev["Mounts"]
                ],
            }
        },
        "networks": {"managed": {"name": m.network["Name"]}},
        "volumes": {
            mount["Name"]: {"name": mount["Name"]}
            for mount in m.dev["Mounts"]
            if mount["Type"] == "volume"
        },
    }
    planned = docker.planned_dev_inspection(delivery)
    assert next(
        mount for mount in planned["Mounts"] if mount["Destination"] == observed["Destination"]
    )["RW"] is False
    checked = host_sealing.assess(planned, planned_generation=m.state["generation"])
    assert checked.state == "sealed" and checked.agent, checked
    assert checked.agent == host_sealing.assess(m.dev).agent


@pytest.mark.parametrize("namespace", ["dev", "agent"])
def test_admission_requires_all_addresses_blocked_in_each_namespace(
    managed, monkeypatch, namespace
):
    m = managed
    agent = host_sealing.assess(m.dev).agent
    hostctl.save_private(
        "opening.json", {"generation": "gen-a", "dev_id": m.dev["Id"], "agent": agent}
    )
    calls = []

    def probe(identity, snapshot):
        calls.append(identity)
        return [
            {
                "namespace": identity,
                "host": "host-a",
                "address": "100.64.0.1",
                "state": "reached"
                if identity == (m.dev["Id"] if namespace == "dev" else m.companion["Id"])
                else "blocked",
            }
        ]

    monkeypatch.setattr(host_sealing, "run_probe", probe)
    with pytest.raises(hostctl.HostctlError, match="Direct route refused"):
        hostctl.admission_assessment(trust())
    assert calls == [m.dev["Id"], m.companion["Id"]]
    config = AppConfig(
        code_dir=m.ground.paths["runtime"],
        hostctl=HostctlConfig.model_validate(
            {"hosts": {"host-a": {"address": "host-a.example.ts.net", "user": "dev"}}}
        ),
    )
    rows = hostctl_boundary_checks(
        {"dev_id": m.dev["Id"], "agent": agent, "trust": trust(), "sealing": "sealed"}, config
    )
    assert len([r for r in rows if r.name.startswith("Hostctl direct probe ")]) == 2
    assert any(r.status is Status.FAIL for r in rows)
    assert any(m.companion["Id"] in r.name for r in rows)


@pytest.mark.parametrize("drift", ["replacement", "profile", "network", "storage"])
def test_commit_reinspects_companion_and_preserves_replacements(managed, monkeypatch, drift):
    m = managed
    agent = host_sealing.assess(m.dev).agent
    checked = {"generation": "gen-a", "dev_id": m.dev["Id"], "creator": None, "agent": agent}
    hostctl.save_private(
        "opening.json", {"generation": "gen-a", "dev_id": m.dev["Id"], "agent": agent}
    )
    hostctl.commit_assessment(checked)
    if drift == "replacement":
        m.objects.pop(m.companion["Id"])
    elif drift == "profile":
        m.companion["Config"]["Env"].append("DOCKER_OPTS=--host=tcp://0.0.0.0:2375")
    elif drift == "network":
        m.network["Id"] = "foreign"
    else:
        m.endpoint["Options"] = {}
    before = copy.deepcopy(m.objects)
    with pytest.raises(hostctl.HostctlError, match="companion changed"):
        hostctl.commit_assessment(checked)
    assert m.objects == before


@pytest.mark.parametrize("drift", ["replacement", "profile"])
def test_open_window_observer_closes_on_companion_drift(managed, monkeypatch, drift):
    m = managed
    agent = host_sealing.assess(m.dev).agent
    helper = {
        "Id": "helper-id",
        "State": {"Running": True},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "gen-a"}},
    }
    hostctl.save_private(
        "opening.json", {"generation": "gen-a", "dev_id": m.dev["Id"], "agent": agent}
    )
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: helper)
    monkeypatch.setattr(hostctl, "node_status", lambda h: {"BackendState": "Running"})
    monkeypatch.setattr(hostctl, "prepare_trust", lambda *a: trust())
    monkeypatch.setattr(hostctl, "admit_locked", lambda *a, **k: None)
    monkeypatch.setattr(host_sealing, "run_probe", lambda *a: [])
    closed = []
    monkeypatch.setattr(hostctl, "stop_helper_locked", lambda: closed.append("closed"))

    class Enrollment:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(host_runtime.subprocess, "Popen", lambda *a, **k: Enrollment())
    turns = 0

    def advance(_):
        nonlocal turns
        turns += 1
        if turns == 1:
            if drift == "replacement":
                m.objects.pop(m.companion["Id"])
            else:
                m.companion["Config"]["Env"].append("PATH=/foreign")
        if turns == 2:
            helper["State"]["Running"] = False

    monkeypatch.setattr(host_runtime.time, "sleep", advance)
    # The observer's production dev name is persisted in its runtime state.
    original_inspect_dev = host_runtime.inspect_dev
    monkeypatch.setattr(
        host_runtime, "inspect_dev", lambda name, *a: original_inspect_dev("djinn-test-dev", *a)
    )
    host_runtime.observe_hostctl("helper-id", "gen-a", "/isolated/docker")
    assert closed == ["closed"]
    assert turns == 1


@pytest.mark.parametrize("tool", ["docker_daemon_ok", "compose_v2_ok", "buildx_ok"])
@pytest.mark.parametrize("timeout", [False, True])
def test_host_docker_tool_rows_are_bounded_with_closed_stdin(monkeypatch, tool, timeout):
    from djinn_in_a_box.commands import doctor

    monkeypatch.setattr(doctor, "DOCKER_EXECUTABLE", "/isolated/docker")
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        assert argv[0] == "/isolated/docker"
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 5
        if timeout:
            raise subprocess.TimeoutExpired(argv, 5)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert getattr(doctor, tool)() is (not timeout)
    assert commands
