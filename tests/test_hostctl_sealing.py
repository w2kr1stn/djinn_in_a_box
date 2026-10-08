from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from djinn_in_a_box.commands.doctor import Status, hostctl_boundary_checks
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import desktop, docker, host_runtime, hostctl
from djinn_in_a_box.core import host_sealing as sealing


def dev(mounts=(), *, identity="dev-a", env=()):
    return {
        "Id": identity,
        "State": {"Running": True},
        "Mounts": list(mounts),
        "Config": {"Env": list(env), "Labels": {}, "Image": "dev:1"},
        "HostConfig": {"NetworkMode": "bridge"},
        "NetworkSettings": {"Networks": {"djinn-network": {"IPAddress": "172.20.0.2"}}},
    }


def bind(path, *, rw=False):
    return {"Type": "bind", "Source": str(path), "Destination": "/delivery", "RW": rw}


@pytest.fixture
def ground(tmp_path, monkeypatch):
    paths = {
        name: tmp_path / name
        for name in (
            "home",
            "data",
            "install",
            "djinn-config",
            "python",
            "path",
            "docker-config",
            "plugin",
            "runtime",
            "bus",
            "pulse",
        )
    }
    for path in paths.values():
        path.mkdir()
    backing = paths["data"] / "volumes/state/_data"
    backing.mkdir(parents=True)
    docker_socket = tmp_path / "docker.sock"
    docker_socket.touch()
    monkeypatch.setattr(
        sealing.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(paths["home"]))
    )
    monkeypatch.setattr(host_runtime, "runtime_root", lambda **kw: paths["runtime"])
    monkeypatch.setattr(
        sealing,
        "execution_paths",
        lambda: {
            "Djinn installation/build context": paths["install"],
            "Djinn configuration": paths["djinn-config"],
            "Python environment": paths["python"],
            "host PATH directory": paths["path"],
            "Docker configuration": paths["docker-config"],
            "Docker plugin directory": paths["plugin"],
        },
    )
    monkeypatch.setattr(sealing, "docker_sockets", lambda: (docker_socket,))
    objects = {
        hostctl.HOSTCTL_STATE_VOLUME: {
            "Name": hostctl.HOSTCTL_STATE_VOLUME,
            "Mountpoint": str(backing),
        }
    }
    monkeypatch.setattr(host_runtime, "inspect_object", lambda name, *a, **kw: objects.get(name))
    monkeypatch.setattr(
        hostctl,
        "command",
        lambda *a, **kw: json.dumps({"DockerRootDir": str(paths["data"])})
        if a[0] == "info"
        else "",
    )
    endpoints = (
        desktop.DesktopEndpoint("dbus", paths["bus"] / "bus", False),
        desktop.DesktopEndpoint("audio", paths["pulse"] / "native", False),
    )
    monkeypatch.setattr(
        sealing,
        "_desktop",
        lambda actual: desktop.inspect_desktop_endpoints(actual, {}, {}, endpoints, {}),
    )
    return SimpleNamespace(paths=paths, backing=backing, socket=docker_socket, objects=objects)


@pytest.mark.parametrize(
    "kind",
    [
        "root",
        "home",
        "ancestor",
        "socket",
        "socket-alias",
        "data",
        "backing",
        "backing-child",
        "install",
        "djinn-config",
        "python",
        "path",
        "docker-config",
        "plugin",
        "controller",
        "agent",
        "bus",
        "pulse",
    ],
)
def test_each_sealing_cause(ground, kind):
    paths = ground.paths
    source = paths.get(kind)
    writable = kind in {"install", "djinn-config", "python", "path", "docker-config", "plugin"}
    if kind == "root":
        source = Path("/")
    elif kind == "ancestor":
        source = paths["home"].parent
    elif kind.startswith("socket"):
        source = ground.socket
        if kind == "socket-alias":
            source = ground.socket.with_name("renamed.sock")
            source.hardlink_to(ground.socket)
    elif kind.startswith("backing"):
        source = ground.backing
        if kind == "backing-child":
            source = source / "secret"
            source.touch()
    elif kind == "controller":
        source = hostctl.state_root()
    elif kind == "agent":
        source = paths["runtime"] / "private"
        source.mkdir()
    result = sealing.assess(dev([bind(source, rw=writable)]))
    expected = {
        "root": "host root bind",
        "home": "user home",
        "ancestor": "user home",
        "socket": "Docker socket",
        "socket-alias": "Docker socket",
        "data": "Docker data root",
        "backing": "helper backing storage",
        "backing-child": "helper backing storage",
        "controller": "host controller/journal",
        "agent": "private agent",
        "bus": "raw host desktop endpoint",
        "pulse": "raw host desktop endpoint",
        "djinn-config": "writable Djinn configuration",
    }.get(kind, "writable")
    assert any(expected in cause for cause in result.causes), result
    assert result.state == "unsealed", result
    with pytest.raises(hostctl.HostctlError, match="Sealing refused"):
        result.require()
    result.require(allow_unsealed=True)


@pytest.mark.parametrize("read_only", [True, False, None], ids=["ro", "rw", "default"])
def test_clean_and_readonly_execution_binds_pass(ground, read_only):
    result = sealing.assess(dev([bind(ground.paths["install"])]))
    assert result.state == "sealed" and not result.causes and not result.errors
    assert sealing.assess(None).state == "deferred"
    config = AppConfig(code_dir=ground.paths["bus"], mounts={
        "execution": {
            "source": str(ground.paths["install"]), "target": "/execution",
            **({"read_only": read_only} if read_only is not None else {}),
        },
    })
    declarations = docker.resolve_declared_entries(
        config, docker.ContainerOptions(), runtime_targets=[], caller_env=None,
    )
    declarations.require_valid()
    delivery = declarations.compose_fragment()
    delivery["services"]["dev"]["image"] = "dev:1"
    planned = docker.planned_dev_inspection(delivery)
    assert planned["Mounts"][0]["RW"] is (read_only is not True)
    result = sealing.assess(planned)
    writable = [cause for cause in result.causes if "writable" in cause]
    if read_only is True:
        assert result.state == "sealed" and not writable and not result.errors
    else:
        assert result.state == "unsealed" and writable
        assert any("Djinn installation/build context" in cause for cause in writable)


def test_alias_and_relocated_socket(ground, monkeypatch):
    alias = ground.paths["home"].with_name("home-alias")
    alias.symlink_to(ground.paths["home"], target_is_directory=True)
    assert sealing.canonical(alias) == ground.paths["home"]
    assert any("user home" in c for c in sealing.assess(dev([bind(alias)])).causes)
    with socket.socket(socket.AF_UNIX) as server:
        path = ground.socket.with_name("unusual")
        server.bind(str(path))
        monkeypatch.setattr(sealing, "_socket_is_docker", lambda p: p == path)
        assert any("relocated Docker socket" in c for c in sealing.assess(dev([bind(path)])).causes)


def test_leftover_proxy_and_environment(ground, monkeypatch):
    proxy = dev()
    proxy["Name"] = "/leftover-proxy"
    proxy["Config"]["Image"] = "tecnativa/docker-socket-proxy:latest"
    ground.objects["proxy-id"] = proxy
    monkeypatch.setattr(
        hostctl,
        "command",
        lambda *a, **kw: "proxy-id"
        if a[0] == "ps"
        else json.dumps({"DockerRootDir": str(ground.paths["data"])}),
    )
    result = sealing.assess(dev(env=["DOCKER_HOST=tcp://leftover-proxy:2375"]))
    assert any("leftover-proxy" in c and "djinn-network" in c for c in result.causes)
    assert any("environment" in c for c in result.causes)


@pytest.mark.parametrize("response", [b"HTTP/1.0 401 Unauthorized", b"unidentified"])
def test_unknown_socket_response_cannot_certify_sealing(ground, monkeypatch, response):
    path = ground.socket.with_name("unknown-endpoint")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        client = MagicMock()
        client.__enter__.return_value = client
        client.recv.return_value = response
        monkeypatch.setattr(socket, "socket", lambda *a, **kw: client)
        result = sealing.assess(dev([bind(path)]))
    assert result.state == "unknown"
    assert any("socket provenance unknown" in e for e in result.errors)
    with pytest.raises(hostctl.HostctlError):
        result.require(allow_unsealed=True)


@pytest.mark.parametrize("route", ["host-published", "relocated", "ancestor"])
def test_proxy_routes_and_socket_aliases(ground, monkeypatch, request, route):
    proxy = dev(identity="proxy-id")
    proxy["Name"] = "/socket-service"
    proxy["Config"]["ExposedPorts"] = {"8888/tcp": {}}
    if route == "host-published":
        proxy["Config"]["Image"] = "tecnativa/docker-socket-proxy:latest"
        proxy["NetworkSettings"] = {
            "Networks": {"other-network": {"IPAddress": "172.21.0.2"}},
            "Ports": {"8888/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32375"}]},
        }
    elif route == "ancestor":
        proxy["Mounts"] = [bind(ground.socket.parent)]
    else:
        path = ground.socket.with_name("relocated-api")
        server = socket.socket(socket.AF_UNIX)
        request.addfinalizer(server.close)
        server.bind(str(path))
        proxy["Mounts"] = [bind(path)]
        monkeypatch.setattr(sealing, "_socket_is_docker", lambda source: source == path)
    ground.objects["proxy-id"] = proxy
    monkeypatch.setattr(
        hostctl,
        "command",
        lambda *a, **kw: "proxy-id"
        if a[0] == "ps"
        else json.dumps({"DockerRootDir": str(ground.paths["data"])}),
    )
    result = sealing.assess(dev())
    assert any("reachable Docker proxy /socket-service" in c for c in result.causes), result
    assert not result.errors


@pytest.mark.parametrize("field", ["Mounts", "Config", "NetworkSettings"])
def test_uncertain_inspection_cannot_be_overridden(ground, field):
    actual = dev()
    actual.pop(field)
    result = sealing.assess(actual)
    assert result.state == "unknown" and result.errors
    with pytest.raises(hostctl.HostctlError):
        result.require(allow_unsealed=True)


def trust():
    return {
        "generation": "gen-a",
        "peers": {
            "host-a": {
                "address": "host-a.example.ts.net",
                "ips": ["100.64.0.1", "fd7a:115c:a1e0::1"],
            }
        },
    }


@pytest.mark.parametrize(
    "outcome", ["blocked", "reached", "unknown", "omitted", "reordered", "timeout", "cancel"]
)
def test_probe_namespace_output_and_cleanup(monkeypatch, outcome):
    calls = []
    remaining = {"exists": False}
    monkeypatch.setattr(
        host_runtime,
        "inspect_object",
        lambda *a, **kw: {"Id": "probe"} if remaining["exists"] else None,
    )

    def command(*args, **kwargs):
        calls.append(args)
        if args[0] == "rm":
            remaining["exists"] = False
            return ""
        remaining["exists"] = True
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args, 30)
        if outcome == "cancel":
            raise KeyboardInterrupt
        rows = [
            {
                "host": h,
                "address": ip,
                "state": outcome if outcome in {"blocked", "reached", "unknown"} else "blocked",
            }
            for h, ip in json.loads(args[-1])
        ]
        if outcome == "omitted":
            rows.pop()
        if outcome == "reordered":
            rows.reverse()
        return json.dumps(rows)

    monkeypatch.setattr(hostctl, "command", command)
    expected = {
        "timeout": subprocess.TimeoutExpired,
        "cancel": KeyboardInterrupt,
        "omitted": ValueError,
        "reordered": ValueError,
    }.get(outcome)
    if expected:
        with pytest.raises(expected):
            sealing.run_probe("dev-a", trust())
    else:
        rows = sealing.run_probe("dev-a", trust())
        assert len(rows) == 2
        if outcome == "blocked":
            sealing.require_blocked(rows)
        else:
            with pytest.raises(hostctl.HostctlError, match="no forwarding"):
                sealing.require_blocked(rows)
    run = calls[0]
    assert run[run.index("--network") + 1] == "container:dev-a"
    assert sealing.PROBE_IMAGE in run and "@sha256:" in sealing.PROBE_IMAGE
    assert "-I" in run and sealing.PROBE_CODE in run
    assert not any(arg in run for arg in ("--env", "-e", "--mount", "-v", "exec"))
    assert calls[-1][:2] == ("rm", "-f") and not remaining["exists"]


def test_execution_chain_determined_at_check_time(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    extra = tmp_path / "extra-plugins"
    (config / "config.json").write_text(json.dumps({"cliPluginsExtraDirs": [str(extra)]}))
    monkeypatch.setenv("DOCKER_CONFIG", str(config))
    monkeypatch.setenv("PATH", str(tmp_path / "first"))
    first = sealing.execution_paths()
    monkeypatch.setenv("PATH", str(tmp_path / "second"))
    second = sealing.execution_paths()
    assert extra in first.values() and config in first.values()
    assert tmp_path / "first" in first.values() and tmp_path / "second" in second.values()
    assert tmp_path / "first" not in second.values()
    assert any(key == "Python environment" for key in second)
    assert second["Djinn configuration"] == sealing.canonical(sealing.host_paths.CONFIG_DIR)
    assert second["Djinn configuration file"] == sealing.canonical(sealing.host_paths.CONFIG_FILE)


def test_guard_closes_verified_before_unsealed_launch(ground, monkeypatch, capsys):
    calls = []
    helper = {
        "Id": "helper-a",
        "State": {"Running": True},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "gen-a"}},
    }
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: helper)

    def close():
        calls.append("stop")
        helper["State"]["Running"] = False

    monkeypatch.setattr(hostctl, "stop_helper_locked", close)
    hostctl.guard_dev_start(dev([bind(ground.paths["home"])]), "creator-a")
    calls.append("launch")
    assert calls == ["stop", "launch"] and helper["State"]["Running"] is False
    captured = capsys.readouterr()
    assert "closed before unsealed dev start" in captured.out + captured.err

    def fail():
        raise hostctl.HostctlError("termination could not be verified")

    helper["State"]["Running"] = True
    monkeypatch.setattr(hostctl, "stop_helper_locked", fail)
    with pytest.raises(hostctl.HostctlError, match="verified"):
        hostctl.guard_dev_start(dev([bind(ground.paths["home"])]), "creator-a")


def test_sealed_start_pauses_no_override_and_checks_actual_before_resume(ground, monkeypatch):
    helper = {
        "Id": "helper-a",
        "State": {"Running": True},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "gen-a"}},
    }
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: helper)
    hostctl.save_private(
        "opening.json", {"generation": "gen-a", "allow_unsealed": True, "dev_id": None}
    )
    calls = []

    def command(*args, **kw):
        if args[0] == "exec":
            calls.append(args[-2])
            return json.dumps(
                {"generation": "gen-a", "paused": args[-2] == "pause", "closed": False}
            )
        return json.dumps({"DockerRootDir": str(ground.paths["data"])}) if args[0] == "info" else ""

    monkeypatch.setattr(hostctl, "command", command)
    hostctl.guard_dev_start(dev(), "creator-a")
    opening = json.loads((hostctl.state_root() / "opening.json").read_text())
    assert opening["allow_unsealed"] is False and opening["creator"] == "creator-a"
    monkeypatch.setattr(hostctl, "host_runtime_dev", lambda: None)
    monkeypatch.setattr(sealing, "inspect_assessment", lambda: sealing.Assessment(None))
    assert hostctl.admission_assessment(trust()) is None
    monkeypatch.setattr(hostctl, "host_runtime_dev", lambda: ("dev-a", True, "creator-a"))
    monkeypatch.setattr(sealing, "inspect_assessment", lambda: sealing.Assessment("dev-a"))
    monkeypatch.setattr(sealing, "run_probe", lambda *a: calls.append("probe") or [])
    checked = hostctl.admission_assessment(trust())
    assert checked is not None
    monkeypatch.setattr(host_runtime, "inspect_dev", lambda *a: ("dev-a", True, "creator-a"))
    hostctl.commit_assessment(checked)
    hostctl.resume_locked(helper)
    assert calls == ["pause", "probe", "resume"]
    monkeypatch.setattr(host_runtime, "inspect_dev", lambda *a: ("replacement", True, "creator-a"))
    with pytest.raises(hostctl.HostctlError, match="removed or replaced"):
        hostctl.commit_assessment(checked)


@pytest.mark.parametrize("state", ["reached", "blocked", "unknown", "error", "deferred"])
def test_doctor_each_address_and_cause_no_helper_start(tmp_path, monkeypatch, state):
    config = AppConfig(
        code_dir=tmp_path,
        hostctl={"hosts": {"host-a": {"address": "host-a.example.ts.net", "user": "operator"}}},
    )
    monkeypatch.setattr(
        hostctl, "open_window", lambda *a, **kw: pytest.fail("doctor started helper")
    )
    monkeypatch.setattr(hostctl, "verify_dev", lambda *a: None)

    def probe(*a):
        if state == "error":
            raise hostctl.HostctlError("probe execution failed")
        return [
            {"host": h, "address": ip, "state": state} for h, ip in sealing.probe_addresses(trust())
        ]

    monkeypatch.setattr(sealing, "run_probe", probe)
    rows = hostctl_boundary_checks(
        {
            "dev_id": "dev-a",
            "sealing_causes": ["root bind", "Docker socket"],
            "trust": None if state == "deferred" else trust(),
        },
        config,
    )
    assert [r.detail for r in rows[:2]] == ["root bind", "Docker socket"]
    assert all(r.status is Status.FAIL for r in rows[:2])
    if state == "deferred":
        assert "deferred" in rows[-1].detail
    else:
        assert len(rows[2:]) == 2
        expected = (
            Status.FAIL
            if state == "reached"
            else (Status.PASS if state == "blocked" else Status.WARN)
        )
        assert all(r.status is expected for r in rows[2:])
        assert "100.64.0.1" in rows[2].name and "fd7a:" in rows[3].name


@pytest.mark.parametrize("mode", ["interactive", "headless", "detached"])
def test_every_creator_guards_before_launch(tmp_path, monkeypatch, mode):
    config = AppConfig(code_dir=tmp_path)
    calls = []
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda *a: [])
    monkeypatch.setattr(docker, "get_project_root", lambda: tmp_path)
    monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
    monkeypatch.setattr(docker, "_guard_dev_creation", lambda *a: calls.append("guard"))

    def launch(*a, **kw):
        calls.append("launch")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docker.subprocess, "run", launch)
    if mode == "detached":
        docker.compose_up_detached(config, docker.ContainerOptions())
    else:
        docker.compose_run(config, docker.ContainerOptions(), interactive=mode == "interactive")
    assert calls == ["guard", "launch"]


def test_resolved_creator_delivery_includes_all_producers(tmp_path, monkeypatch):
    config = AppConfig(code_dir=tmp_path)
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: {"State": {"Running": True}})
    monkeypatch.setattr(
        host_runtime,
        "inspect_object",
        lambda *a, **kw: {"Id": "network-id", "Mountpoint": "/store/output"},
    )
    delivery = {
        "services": {
            "dev": {
                "image": "dev:1",
                "volumes": [
                    {
                        "type": "bind", "source": "/declared", "target": "/declared",
                        "read_only": True,
                    },
                    {"type": "volume", "source": "output", "target": "/output", "read_only": True},
                ],
                "environment": {"DOCKER_HOST": "tcp://proxy:2375"},
                "networks": {"devnet": {}},
                "labels": {host_runtime.GENERATION_LABEL: "creator"},
            }
        },
        "networks": {"devnet": {"name": "djinn-network"}},
        "volumes": {"output": {"name": "actual-output"}},
    }
    calls = []
    monkeypatch.setattr(
        docker,
        "_run_compose",
        lambda *a, **kw: calls.append(a[0]) or docker.RunResult(0, stdout=json.dumps(delivery)),
    )
    captured = []
    monkeypatch.setattr(hostctl, "guard_dev_start", lambda *a: captured.append(a))
    docker._guard_dev_creation(
        config,
        ["-f", "base.yml"],
        tmp_path / "override.json",
        SimpleNamespace(generation="creator"),
        ["/invocation:/workspace:ro"],
        {"CALLER": "value"},
    )
    planned, creator = captured[0]
    assert calls[0][-3:] == ["config", "--format", "json"]
    assert {row["Source"] for row in planned["Mounts"]} == {
        "/declared",
        "/store/output",
        "/invocation",
    }
    assert {row["Destination"]: row["RW"] for row in planned["Mounts"]}["/workspace"] is False
    assert {row["Destination"]: row["RW"] for row in planned["Mounts"]}["/declared"] is False
    assert planned["NetworkSettings"]["Networks"] == {"djinn-network": {"NetworkID": "network-id"}}
    assert "CALLER=value" in planned["Config"]["Env"] and creator == "creator"


def test_on_override_journals_causes_but_never_overrides_probe(ground, monkeypatch):
    monkeypatch.setattr(
        sealing,
        "inspect_assessment",
        lambda: sealing.Assessment("dev-a", ("root bind", "Docker socket")),
    )
    monkeypatch.setattr(hostctl, "verify_dev", lambda *a: None)
    monkeypatch.setattr(hostctl, "supervisor_path", lambda: ground.socket)
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: None)
    # Stop after the explicit override journal, before helper creation.
    monkeypatch.setattr(docker, "ensure_network", lambda *a: False)
    config = AppConfig(
        code_dir=ground.paths["home"],
        hostctl={"hosts": {"host-a": {"address": "host-a.example.ts.net", "user": "operator"}}},
    )
    with pytest.raises(hostctl.HostctlError, match="root bind.*Docker socket"):
        hostctl.open_window(config)
    with pytest.raises(hostctl.HostctlError, match="network"):
        hostctl.open_window(config, allow_unsealed=True)
    journal = [
        json.loads(line)
        for line in (hostctl.state_root() / "journal.jsonl").read_text().splitlines()
    ]
    override = next(row for row in journal if row["event"] == "unsealed-override")
    assert (
        override["causes"] == ["root bind", "Docker socket"]
        and "host authority" in override["boundary"]
    )
    hostctl.save_private(
        "opening.json", {"generation": "gen-a", "allow_unsealed": True, "dev_id": "dev-a"}
    )
    monkeypatch.setattr(
        sealing,
        "run_probe",
        lambda *a: [{"host": "host-a", "address": "100.64.0.1", "state": "reached"}],
    )
    with pytest.raises(hostctl.HostctlError, match="no forwarding"):
        hostctl.admission_assessment(trust())


def test_nested_relocated_socket_and_incomplete_bind_names_known_causes(ground, monkeypatch):
    source = ground.paths["home"].with_name("innocent")
    source.mkdir()
    with socket.socket(socket.AF_UNIX) as server:
        path = source / "hidden"
        server.bind(str(path))
        monkeypatch.setattr(sealing, "_socket_is_docker", lambda p: p == path)
        result = sealing.assess(dev([bind(source)]))
        assert any("relocated Docker socket" in c for c in result.causes)
    result = sealing.assess(dev([{"Type": "bind"}, bind(Path("/")), bind(ground.socket)]))
    assert result.errors
    assert any("host root bind" in c for c in result.causes)
    assert any("Docker socket" in c for c in result.causes)


def test_trusted_probe_code_raw_bounded_and_all_families(monkeypatch, capsys):
    import sys

    calls = []

    class RawSocket:
        def __init__(self, family):
            self.family = family

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            assert value == 1.5

        def connect(self, destination):
            assert destination[1] == 22
            calls.append((self.family, destination))
            if self.family == socket.AF_INET6:
                raise TimeoutError

    monkeypatch.setattr(socket, "socket", RawSocket)
    monkeypatch.setattr(sys, "argv", ["probe", json.dumps(sealing.probe_addresses(trust()))])
    exec(sealing.PROBE_CODE, {})
    rows = json.loads(capsys.readouterr().out)
    assert [r["state"] for r in rows] == ["reached", "blocked"]
    assert {family for family, _ in calls} == {socket.AF_INET, socket.AF_INET6}


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("kind", ["Docker CLI", "journal"])
def test_protected_file_inode_aliases(ground, monkeypatch, kind, directory):
    if kind == "journal":
        protected = hostctl.state_root() / "journal.jsonl"
    else:
        protected = ground.paths["path"] / "docker"
        paths = sealing.execution_paths()
        paths["Docker CLI"] = protected
        monkeypatch.setattr(sealing, "execution_paths", lambda: paths)
    protected.write_text("host input")
    source = ground.paths["home"].with_name("innocent")
    if directory:
        source.mkdir()
    alias = source / "file" if directory else source
    alias.hardlink_to(protected)
    result = sealing.assess(dev([bind(source, rw=kind != "journal")]))
    assert any(kind in cause for cause in result.causes), result
    result.require(allow_unsealed=True)


@pytest.mark.parametrize("kind", ["root", "bus", "pulse"])
def test_named_volume_host_bind_alias(ground, kind):
    device = "/" if kind == "root" else str(ground.paths[kind])
    ground.objects["root-alias"] = {
        "Name": "root-alias",
        "Driver": "local",
        "Mountpoint": str(ground.paths["data"] / "alias/_data"),
        "Options": {"type": "none", "o": "bind", "device": device},
    }
    result = sealing.assess(
        dev(
            [
                {
                    "Type": "volume",
                    "Name": "root-alias",
                    "Source": ground.objects["root-alias"]["Mountpoint"],
                    "Destination": "/delivery",
                    "RW": False,
                }
            ]
        )
    )
    expected = "host root bind" if kind == "root" else "raw host desktop endpoint"
    assert any(expected in cause for cause in result.causes), result
    result.require(allow_unsealed=True)
