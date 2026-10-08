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
            "Djinn installation/build context": (
                paths["install"],
                "writable Djinn installation and configuration",
                "Djinn installation/build context",
            ),
            "Djinn configuration": (
                paths["djinn-config"],
                "writable Djinn installation and configuration",
                "Djinn configuration",
            ),
            "Python environment": (
                paths["python"],
                "writable Python environment",
                "Python environment",
            ),
            "host PATH directory": (
                paths["path"],
                "writable PATH directory",
                str(paths["path"]),
            ),
            "Docker configuration": (
                paths["docker-config"],
                "writable Docker CLI and plugins",
                "Docker configuration",
            ),
            "Docker plugin directory": (
                paths["plugin"],
                "writable Docker CLI and plugins",
                str(paths["plugin"]),
            ),
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


def empty_desktop(monkeypatch):
    monkeypatch.setattr(
        sealing,
        "_desktop",
        lambda actual: desktop.DesktopInspection(channels=(), raw_sources=(), raw_verified=True),
    )


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
    assert any(expected in cause for cause in result.cause_details), result
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
    assert any("user home" in c for c in sealing.assess(dev([bind(alias)])).cause_details)
    with socket.socket(socket.AF_UNIX) as server:
        path = ground.socket.with_name("unusual")
        server.bind(str(path))
        monkeypatch.setattr(sealing, "_socket_is_docker", lambda p: p == path)
        assert any(
            "relocated Docker socket" in c for c in sealing.assess(dev([bind(path)])).cause_details
        )


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
    assert any("socket provenance unknown" in e for e in result.error_details)
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


@pytest.mark.parametrize("destination", [[], {}], ids=["list", "dict"])
@pytest.mark.parametrize(
    "rw, desktop_failure, expected_state",
    [(False, False, "sealed"), (True, False, "unsealed"), (True, True, "unknown")],
    ids=["readonly", "root", "fallback"],
)
def test_malformed_bind_destination_keeps_base_decision(
    ground, monkeypatch, destination, rw, desktop_failure, expected_state
):
    inspections = []
    if desktop_failure:
        inspect_object = host_runtime.inspect_object

        def inspect(name, *args, **kwargs):
            if name == desktop.HELPER_IMAGE:
                raise RuntimeError(
                    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                    "Is the docker daemon running?"
                )
            return inspect_object(name, *args, **kwargs)

        def inspect_desktop(actual):
            inspection = docker.inspect_running_desktop("dev-a", dev_inspect=actual)
            inspections.append(inspection)
            return inspection

        monkeypatch.setattr(host_runtime, "inspect_object", inspect)
        monkeypatch.setattr(desktop, "discover_desktop_endpoints", lambda: ())
        monkeypatch.setattr(sealing, "_desktop", inspect_desktop)
    else:
        empty_desktop(monkeypatch)

    source = Path("/") if rw else ground.paths["home"].parent / "workspace"
    if not rw:
        source.mkdir()
    actual = dev([{**bind(source, rw=rw), "Destination": destination}])
    result = sealing.assess(actual)
    assert result.state == expected_state
    assert all(f.bind == (str(source), str(destination)) for f in result.cause_findings)
    assert all(isinstance(hash(f), int) for f in (*result.cause_findings, *result.error_findings))
    if rw:
        assert (
            sealing.Finding(
                f"host root bind: / -> {destination}", ("/", str(destination)), "host root"
            )
            in result.cause_findings
        )
    if desktop_failure:
        assert len(inspections) == 1 and inspections[0].raw_verified is False
        assert "raw desktop endpoint inspection unknown" in result.error_details
    for allow_unsealed in (False, True):
        if desktop_failure or (rw and not allow_unsealed):
            with pytest.raises(hostctl.HostctlError, match="Sealing refused"):
                result.require(allow_unsealed=allow_unsealed)
        else:
            result.require(allow_unsealed=allow_unsealed)


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
    assert first["Docker plugins"][0] == config / "cli-plugins"
    assert first[f"Docker plugin directory {extra}"][0] == extra
    assert first[f"host PATH directory {tmp_path / 'first'}"][0] == tmp_path / "first"
    assert second[f"host PATH directory {tmp_path / 'second'}"][0] == tmp_path / "second"
    assert f"host PATH directory {tmp_path / 'first'}" not in second
    assert any(key == "Python environment" for key in second)
    assert second["Djinn configuration"][0] == sealing.canonical(sealing.host_paths.CONFIG_DIR)
    assert second["Djinn configuration file"][0] == sealing.canonical(
        sealing.host_paths.CONFIG_FILE
    )


def test_path_import_directory_item_renders_assessment(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "path", [tmp_path])
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker-config"))
    path, kind, item = sealing.execution_paths()[f"Python import directory {tmp_path}"]
    assert path == tmp_path and isinstance(item, str) and item == str(tmp_path)
    assessment = sealing.Assessment(
        "dev-a", (sealing.Finding("per-item cause", (str(tmp_path), "/delivery"), kind, item),)
    )
    assert assessment.state == "unsealed"
    assert assessment.causes == (
        f"{tmp_path} -> /delivery: writable Python environment {tmp_path}",
    )


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
            "sealing_cause_details": ["root bind", "Docker socket"],
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
        lambda: sealing.Assessment(
            "dev-a", (sealing.Finding("root bind"), sealing.Finding("Docker socket"))
        ),
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
        assert any("relocated Docker socket" in c for c in result.cause_details)
    result = sealing.assess(dev([{"Type": "bind"}, bind(Path("/")), bind(ground.socket)]))
    assert result.errors
    assert any("host root bind" in c for c in result.cause_details)
    assert any("Docker socket" in c for c in result.cause_details)


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
        paths["Docker CLI"] = (
            protected,
            "writable Docker CLI and plugins",
            "Docker CLI",
        )
        monkeypatch.setattr(sealing, "execution_paths", lambda: paths)
    protected.write_text("host input")
    source = ground.paths["home"].with_name("innocent")
    if directory:
        source.mkdir()
    alias = source / "file" if directory else source
    alias.hardlink_to(protected)
    result = sealing.assess(dev([bind(source, rw=kind != "journal")]))
    assert any(kind in cause for cause in result.cause_details), result
    group = (
        "exposed controller/journal/agent state"
        if kind == "journal"
        else "writable Docker CLI and plugins"
    )
    item = f"{kind} inode alias {alias}" if directory else kind
    prefix = "exposed" if kind == "journal" else "writable"
    detail = f"{prefix} {item}: {source} -> /delivery"
    assert sealing.Finding(detail, (str(source), "/delivery"), group, item) in result.cause_findings
    assert result.causes == (f"{source} -> /delivery: {group} {item}",)
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
    assert any(expected in cause for cause in result.cause_details), result
    if kind == "root":
        assert result.cause_findings[0] == sealing.Finding(
            "host root bind: / -> /delivery", ("/", "/delivery"), "host root"
        )
    else:
        assert all(f.bind is None for f in result.cause_findings)
    result.require(allow_unsealed=True)


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
def test_grouped_item_threshold(count):
    pair = ("/workspace", "/delivery")
    findings = tuple(
        sealing.Finding(f"detail {i}", pair, "writable PATH directory", f"/bin/{i}")
        for i in range(count)
    ) or (sealing.Finding("detail", pair, "writable PATH directory"),)
    expected = "writable PATH directory"
    if 1 <= count <= 3:
        expected += " " + ", ".join(f"/bin/{i}" for i in range(count))
    elif count > 3:
        expected += f" ({count} items)"
    assert sealing.Assessment("dev-a", findings).causes == (f"/workspace -> /delivery: {expected}",)


def test_group_order_and_standalones():
    b = ("/workspace: b -> part", "/delivery: b")
    a = ("/workspace: a", "/delivery -> a")
    findings = (
        sealing.Finding("b2", b, "class2", "item: b -> 2"),
        sealing.Finding("raw host desktop endpoint: /display -> /socket"),
        sealing.Finding("a1", a, "class1", "a"),
        sealing.Finding("b1", b, "class1", "b"),
        sealing.Finding("standalone 2"),
        sealing.Finding("a2", a, "class1", "later"),
    )
    expected = (
        "/workspace: b -> part -> /delivery: b: class2 item: b -> 2; class1 b",
        "raw host desktop endpoint: /display -> /socket",
        "/workspace: a -> /delivery -> a: class1 a, later",
        "standalone 2",
    )
    result = sealing.Assessment("dev-a", findings, findings)
    assert result.causes == result.errors == expected
    assert result.cause_details == result.error_details == tuple(f.detail for f in findings)


def test_grouped_item_multiplicity():
    pair = ("/workspace", "/delivery")
    findings = tuple(sealing.Finding(f"detail {i}", pair, "class1", "same") for i in range(4))
    second = sealing.Finding("other class", pair, "class2", "same")
    standalone = sealing.Finding("standalone")
    result = sealing.Assessment("dev-a", (*findings, second, findings[0], standalone, standalone))
    assert result.causes == (
        "/workspace -> /delivery: class1 (4 items); class2 same",
        "standalone",
    )
    assert result.cause_details == tuple(f.detail for f in (*findings, second, standalone))
    assert result.cause_findings[-2:] == (standalone, standalone)


@pytest.mark.parametrize("identity", [None, "dev-a"])
@pytest.mark.parametrize(
    "has_causes,has_errors",
    [(False, False), (True, False), (False, True), (True, True)],
)
@pytest.mark.parametrize("attributed", [False, True])
def test_assessment_contract_and_require(identity, has_causes, has_errors, attributed):
    pair = ("/workspace", "/delivery") if attributed else None
    causes = (sealing.Finding("cause item", pair, "cause class", "cause"),) if has_causes else ()
    errors = (
        (sealing.Finding("unknown item", pair, "unknown class", "unknown"),) if has_errors else ()
    )
    agent = {"dev_id": "dev-a"}
    result = sealing.Assessment(identity, causes, errors, agent)
    assert result.dev_id == identity and result.agent == agent
    assert result.cause_findings == causes and result.error_findings == errors
    assert result.cause_details == (("cause item",) if has_causes else ())
    assert result.error_details == (("unknown item",) if has_errors else ())
    assert bool(result.causes) == bool(result.cause_details) == has_causes
    assert bool(result.errors) == bool(result.error_details) == has_errors
    expected = (
        "unknown"
        if has_errors
        else ("unsealed" if has_causes else ("sealed" if identity else "deferred"))
    )
    assert result.state == expected
    for override in (False, True):
        if has_errors or (has_causes and not override):
            with pytest.raises(hostctl.HostctlError) as exc:
                result.require(allow_unsealed=override)
            assert str(exc.value) == "Sealing refused: " + "; ".join(
                (*result.causes, *result.errors)
            )
        else:
            result.require(allow_unsealed=override)
    assert sealing.Assessment(None).cause_findings == sealing.Assessment(None).error_findings == ()
    assert sealing.Finding("standalone") == ("standalone", None, None, None)


@pytest.mark.parametrize("same_detail", [False, True])
@pytest.mark.parametrize("separator", ["standalone", "other bind"])
def test_colliding_labels_stay_separate(ground, monkeypatch, same_detail, separator):
    empty_desktop(monkeypatch)
    first = ground.paths["home"].with_name("workspace")
    first.mkdir()
    second = Path(f"{first} -> /delivery")
    second.parent.mkdir()
    second.symlink_to(first, target_is_directory=True)
    pairs = ((str(first), "/delivery -> /extra"), (str(second), "/extra"))
    other = ("/other", "/delivery")
    middle = (
        sealing.Finding("standalone")
        if separator == "standalone"
        else sealing.Finding("other", other, "class", "other")
    )
    findings = (
        sealing.Finding("same" if same_detail else "first", pairs[0], "class", "item"),
        middle,
        sealing.Finding("same" if same_detail else "second", pairs[1], "class", "item"),
    )
    label = f"{first} -> /delivery -> /extra"
    expected = (
        f"{label}: class item",
        ("standalone" if separator == "standalone" else "/other -> /delivery: class other"),
        f"{label}: class item",
    )
    result = sealing.Assessment("dev-a", findings, findings)
    assert result.causes == result.errors == expected
    assert (
        result.cause_details
        == result.error_details
        == tuple(dict.fromkeys(f.detail for f in findings))
    )
    different_destination = sealing.Assessment(
        "dev-a",
        (
            sealing.Finding("a", (str(first), "/a"), "class", "item"),
            sealing.Finding("b", (str(first), "/b"), "class", "item"),
        ),
    )
    assert len(different_destination.causes) == 2

    path = first / "input"
    path.touch()
    sibling = first.with_name("other-input")
    sibling.touch()
    monkeypatch.setattr(
        sealing,
        "execution_paths",
        lambda: {
            "Python module example": (path, "writable Python environment", "example"),
            "Python module other": (sibling, "writable Python environment", "other"),
        },
    )
    mounts = [{**bind(first, rw=True), "Destination": pairs[0][1]}]
    if separator == "standalone":
        mounts.append({"Type": "volume", "Name": hostctl.HOSTCTL_STATE_VOLUME})
    else:
        mounts.append(bind(sibling, rw=True))
    mounts.append({**bind(second, rw=True), "Destination": pairs[1][1]})
    actual = sealing.assess(dev(mounts))
    assert [f.bind for f in actual.cause_findings] == [
        pairs[0],
        None if separator == "standalone" else (str(sibling), "/delivery"),
        pairs[1],
    ]
    assert actual.causes[0] == actual.causes[2] == f"{label}: writable Python environment example"
    assert actual.cause_details.count(f"writable Python module example: {label}") == 1

    direct = first.with_name("file")
    direct.hardlink_to(path)
    alias = Path(f"{direct} -> /delivery")
    alias.parent.mkdir()
    alias.hardlink_to(path)
    clean_other = first.with_name("unknown-file")
    clean_other.hardlink_to(path)
    bad_middle = (
        {**bind(clean_other), "Source": "relative"}
        if separator == "standalone"
        else bind(clean_other)
    )
    unknown = sealing.assess(
        dev(
            [
                {**bind(direct), "Destination": pairs[0][1]},
                bad_middle,
                {**bind(alias), "Destination": pairs[1][1]},
            ]
        )
    )
    unknown_label = f"{direct} -> /delivery -> /extra"
    assert [f.bind for f in unknown.error_findings] == [
        (str(direct), pairs[0][1]),
        None if separator == "standalone" else (str(clean_other), "/delivery"),
        (str(alias), pairs[1][1]),
    ]
    assert unknown.errors[0] == f"{unknown_label}: hard-link provenance unknown {direct}"
    assert unknown.errors[2] == f"{unknown_label}: hard-link provenance unknown {alias}"
    assert unknown.error_details.count(f"hard-link provenance unknown: {unknown_label}") == 1


def test_root_bind_groups_execution_inventory(ground, monkeypatch):
    empty_desktop(monkeypatch)
    inventory: dict[str, tuple[Path, str, str]] = {}
    names = (
        (
            "writable Djinn installation and configuration",
            [
                "Djinn installation/build context",
                "Djinn configuration",
                "Djinn configuration file",
                "Djinn agent definitions",
                "Djinn zone definitions",
            ],
        ),
        (
            "writable Python environment",
            ["Python environment", "Python base environment", "Python executable"],
        ),
        (
            "writable Docker CLI and plugins",
            ["Docker CLI", "Docker configuration", "Docker plugins"],
        ),
    )
    for kind, keys in names:
        for name in keys:
            inventory[name] = (ground.paths["install"] / name, kind, name)
    for i in range(2):
        item = str(ground.paths["python"] / f"import{i}")
        inventory[f"Python import directory {item}"] = (
            Path(item),
            "writable Python environment",
            item,
        )
    for i in range(12):
        inventory[f"Python module mod{i}"] = (
            ground.paths["python"] / f"module{max(1, i)}.py",
            "writable Python environment",
            f"mod{i}",
        )
    for prefix, kind, directory in (
        ("host PATH directory", "writable PATH directory", "path"),
        ("Docker plugin directory", "writable Docker CLI and plugins", "plugin"),
    ):
        for i in range(4):
            item = str(ground.paths[directory] / str(i))
            inventory[f"{prefix} {item}"] = (Path(item), kind, item)
    monkeypatch.setattr(sealing, "execution_paths", lambda: inventory)
    result = sealing.assess(dev([bind(Path("/"), rw=True)]))
    assert result.causes == (
        "/ -> /delivery: host root; user home or ancestor; "
        "Docker data root/helper backing storage; "
        f"exposed controller/journal/agent state (5 items); Docker socket {ground.socket}; "
        "writable Djinn installation and configuration (5 items); "
        "writable Python environment (17 items); "
        "writable Docker CLI and plugins (7 items); writable PATH directory (4 items)",
    )
    assert result.cause_details == (
        "host root bind: / -> /delivery",
        "user home or ancestor bind: / -> /delivery",
        "Docker data root/helper backing storage bind: / -> /delivery",
        *(
            f"exposed {name}: / -> /delivery"
            for name in (
                "host controller/journal",
                "journal",
                "private agent",
                "private agent socket",
                "host runtime control",
            )
        ),
        f"Docker socket {ground.socket}: / -> /delivery",
        *(f"writable {name}: / -> /delivery" for name in inventory),
    )
    assert not result.errors and result.state == "unsealed"
    assert all(f.bind == ("/", "/delivery") for f in result.cause_findings)


def test_real_execution_inventory_classes_and_items(tmp_path, monkeypatch):
    import sys
    from types import ModuleType

    config = tmp_path / "config"
    config.mkdir()
    extra = tmp_path / "extra"
    (config / "config.json").write_text(json.dumps({"cliPluginsExtraDirs": [str(extra)]}))
    imported = tmp_path / "import"
    imported.mkdir()
    alias = tmp_path / "import-alias"
    alias.symlink_to(imported, target_is_directory=True)
    module = ModuleType("sample")
    module.__file__ = str(imported / "sample.py")
    monkeypatch.setitem(sys.modules, "sample_one", module)
    monkeypatch.setitem(sys.modules, "sample_two", module)
    monkeypatch.setattr(sys, "path", [str(alias), ""])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_CONFIG", str(config))
    monkeypatch.setenv("PATH", str(alias) + ":")
    result = sealing.execution_paths()
    assert result[f"Python import directory {alias}"] == (
        imported,
        "writable Python environment",
        str(alias),
    )
    assert result[f"Python import directory {tmp_path}"] == (
        tmp_path,
        "writable Python environment",
        str(tmp_path),
    )
    for name in ("sample_one", "sample_two"):
        assert result[f"Python module {name}"] == (
            Path(module.__file__),
            "writable Python environment",
            name,
        )
    assert result[f"host PATH directory {alias}"] == (
        imported,
        "writable PATH directory",
        str(alias),
    )
    assert result[f"host PATH directory {tmp_path}"] == (
        tmp_path,
        "writable PATH directory",
        str(tmp_path),
    )
    for entry in (
        "/usr/local/lib/docker/cli-plugins",
        "/usr/local/libexec/docker/cli-plugins",
        "/usr/lib/docker/cli-plugins",
        "/usr/libexec/docker/cli-plugins",
        str(extra),
    ):
        assert result[f"Docker plugin directory {entry}"] == (
            sealing.canonical(entry),
            "writable Docker CLI and plugins",
            entry,
        )
    singletons = {
        "Djinn installation/build context": sealing.get_project_root(),
        "Djinn configuration": sealing.host_paths.CONFIG_DIR,
        "Djinn configuration file": sealing.host_paths.CONFIG_FILE,
        "Djinn agent definitions": sealing.host_paths.AGENTS_FILE,
        "Djinn zone definitions": sealing.host_paths.ZONES_FILE,
        "Python environment": Path(sys.prefix),
        "Python base environment": Path(sys.base_prefix),
        "Python executable": Path(sys.executable),
        "Docker CLI": Path(hostctl.DOCKER_EXECUTABLE),
        "Docker configuration": config,
        "Docker plugins": config / "cli-plugins",
    }
    for name, path in singletons.items():
        kind = (
            "writable Djinn installation and configuration"
            if name.startswith("Djinn")
            else (
                "writable Python environment"
                if name.startswith("Python")
                else "writable Docker CLI and plugins"
            )
        )
        assert result[name] == (sealing.canonical(path), kind, name)


def test_detail_dedup_preserves_detection_gates(ground, monkeypatch):
    empty_desktop(monkeypatch)
    monkeypatch.setattr(
        sealing, "nested_sensitive_files", lambda source: pytest.fail("root rescanned")
    )
    mount = bind(Path("/"), rw=True)
    single = sealing.assess(dev([mount]))
    repeated = sealing.assess(dev([mount, mount]))
    assert repeated == single
    assert repeated.causes == single.causes and repeated.cause_details == single.cause_details


def test_hardlink_unknowns_group_per_bind(ground, monkeypatch):
    empty_desktop(monkeypatch)
    source = ground.paths["home"].with_name("workspace")
    source.mkdir()
    original = source.with_name("original")
    original.touch()
    files = tuple(source / f"file{i}" for i in range(4))
    for file in files:
        file.hardlink_to(original)
    clean = source.with_name("clean")
    clean.mkdir()
    monkeypatch.setattr(
        sealing,
        "nested_sensitive_files",
        lambda path: list(files) if path == source else [],
    )
    result = sealing.assess(dev([bind(source), bind(clean)]))
    label = f"{source} -> /delivery"
    assert result.error_details == tuple(
        f"hard-link provenance unknown: {file}: {label}" for file in files
    )
    assert result.errors == (f"{label}: hard-link provenance unknown (4 items)",)
    assert result.error_findings == tuple(
        sealing.Finding(
            detail,
            (str(source), "/delivery"),
            "hard-link provenance unknown",
            str(file),
        )
        for detail, file in zip(result.error_details, files, strict=True)
    )
    assert result.state == "unknown" and not result.causes
    repeated = sealing.assess(dev([bind(source), bind(source)]))
    assert repeated.error_findings == result.error_findings
    assert repeated.error_details == result.error_details and repeated.errors == result.errors
    for override in (False, True):
        with pytest.raises(hostctl.HostctlError):
            result.require(allow_unsealed=override)


@pytest.mark.parametrize("kind", ["direct-hardlink", "socket"])
def test_direct_unknown_attribution(ground, monkeypatch, kind):
    empty_desktop(monkeypatch)
    path = ground.paths["home"].with_name("unknown")
    with socket.socket(socket.AF_UNIX) as server:
        if kind == "socket":
            server.bind(str(path))

            def unknown(source):
                raise OSError("unrecognized Unix socket response")

            monkeypatch.setattr(sealing, "_socket_is_docker", unknown)
            name = "socket provenance unknown"
        else:
            path.hardlink_to(ground.socket)
            monkeypatch.setattr(sealing, "docker_sockets", lambda: ())
            name = "hard-link provenance unknown"
        result = sealing.assess(dev([bind(path)]))
    assert result.error_findings == (
        sealing.Finding(f"{name}: {path} -> /delivery", (str(path), "/delivery"), name, str(path)),
    )
    assert result.errors == (f"{path} -> /delivery: {name} {path}",)


def test_desktop_and_inspection_stay_standalone(ground, monkeypatch):
    result = sealing.assess(dev([bind(Path("/")), {"Type": "bind"}, "malformed"]))
    desktop_findings = tuple(
        f
        for f in result.cause_findings
        if f.detail.startswith(("raw host desktop endpoint:", "dbus:", "audio:"))
    )
    assert desktop_findings and all(f.bind is None for f in desktop_findings)
    assert all(f.bind is None for f in result.error_findings)
    assert result.errors == result.error_details
    assert result.causes[1:] == tuple(f.detail for f in desktop_findings)
    monkeypatch.setattr(
        sealing,
        "_desktop",
        lambda actual: desktop.DesktopInspection(channels=(), raw_sources=(), raw_verified=False),
    )
    unknown = sealing.assess(dev())
    assert unknown.error_findings == (sealing.Finding("raw desktop endpoint inspection unknown"),)


@pytest.mark.parametrize(
    "entry",
    [
        "install",
        "djinn-config",
        "python",
        "path",
        "docker-config",
        "plugin",
        "host controller/journal",
        "journal",
        "private agent",
        "private agent socket",
        "host runtime control",
        "socket",
        "socket-alias",
    ],
)
def test_finding_attribution_variants(ground, monkeypatch, entry):
    empty_desktop(monkeypatch)
    monkeypatch.setenv("XDG_STATE_HOME", str(ground.paths["install"].parent / "state"))
    private = {
        "host controller/journal": hostctl.state_root(),
        "journal": hostctl.state_root() / "journal.jsonl",
        "private agent": ground.paths["runtime"] / "private",
        "private agent socket": ground.paths["runtime"] / "private/agent.sock",
        "host runtime control": ground.paths["runtime"] / "runtime.json",
    }
    if entry in private:
        path = private[entry]
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry in {"host controller/journal", "private agent"}:
            path.mkdir(exist_ok=True)
        else:
            path.touch()
        expected = sealing.Finding(
            f"exposed {entry}: {path} -> /delivery",
            (str(path), "/delivery"),
            "exposed controller/journal/agent state",
            entry,
        )
        rw = False
    elif entry.startswith("socket"):
        path = ground.socket
        if entry == "socket-alias":
            path = path.with_name("alias")
            path.hardlink_to(ground.socket)
        expected = sealing.Finding(
            f"Docker socket {ground.socket}: {path} -> /delivery",
            (str(path), "/delivery"),
            "Docker socket",
            str(ground.socket),
        )
        rw = False
    else:
        path = ground.paths[entry]
        name, (_, kind, item) = next(
            (name, triple)
            for name, triple in sealing.execution_paths().items()
            if triple[0] == path
        )
        expected = sealing.Finding(
            f"writable {name}: {path} -> /delivery",
            (str(path), "/delivery"),
            kind,
            item,
        )
        rw = True
    result = sealing.assess(dev([bind(path, rw=rw)]))
    assert expected in result.cause_findings
    items = {
        "host controller/journal": "host controller/journal, journal",
        "journal": "host controller/journal, journal",
        "private agent": "private agent, private agent socket",
        "private agent socket": "private agent, private agent socket",
    }.get(entry, expected.item)
    assert result.causes == (f"{path} -> /delivery: {expected.kind} {items}",)


@pytest.mark.parametrize("nested", [False, True])
def test_relocated_socket_attribution(ground, monkeypatch, nested):
    empty_desktop(monkeypatch)
    source = ground.paths["home"].with_name("workspace")
    if nested:
        source.mkdir()
    path = source / "endpoint" if nested else source
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        monkeypatch.setattr(sealing, "_socket_is_docker", lambda candidate: candidate == path)
        result = sealing.assess(dev([bind(source)]))
    detail = (
        f"relocated Docker socket {path}: {source} -> /delivery"
        if nested
        else f"relocated Docker socket: {source} -> /delivery"
    )
    assert result.cause_findings == (
        sealing.Finding(detail, (str(source), "/delivery"), "relocated Docker socket", str(path)),
    )
    assert result.causes == (f"{source} -> /delivery: relocated Docker socket {path}",)


def test_guard_warning_and_journal_are_grouped(ground, monkeypatch, capsys):
    from djinn_in_a_box.core import console

    monkeypatch.setenv("XDG_STATE_HOME", str(ground.paths["home"].parent / "state"))
    monkeypatch.setattr(console.console, "width", 300)
    monkeypatch.setattr(console.err_console, "width", 300)
    pair = ("/workspace", "/delivery")
    assessment = sealing.Assessment(
        "dev-a",
        (
            sealing.Finding("first detail", pair, "class", "one"),
            sealing.Finding("second detail", pair, "class", "two"),
        ),
        (sealing.Finding("unknown detail", pair, "unknown", "item"),),
    )
    monkeypatch.setattr(sealing, "assess", lambda planned, **kwargs: assessment)
    helper = {
        "Id": "helper-a",
        "State": {"Running": True},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "gen-a"}},
    }
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: helper)
    calls = []

    def stop() -> None:
        calls.append("stop")
        helper["State"]["Running"] = False

    monkeypatch.setattr(hostctl, "stop_helper_locked", stop)
    hostctl.guard_dev_start(dev(), "creator-a")
    assert calls == ["stop"] and helper["State"]["Running"] is False
    reason = "; ".join((*assessment.causes, *assessment.errors))
    rows = [
        json.loads(line)
        for line in (hostctl.state_root() / "journal.jsonl").read_text().splitlines()
    ]
    assert [(row["event"], row["reason"]) for row in rows] == [
        ("dev-start-unsealed", reason),
        ("dev-start-closed", reason),
    ]
    captured = capsys.readouterr()
    assert (
        f"Hostctl window closed before unsealed dev start: {reason}" in captured.out + captured.err
    )
    assert "first detail" not in captured.out + captured.err


@pytest.mark.parametrize(
    "site",
    [
        "helper",
        "environment",
        "proxy",
        "channel-error",
        "volume-error",
        "outer-error",
        "verifier-error",
        "inspect-error",
    ],
)
def test_standalone_producers_keep_details(ground, monkeypatch, site):
    from djinn_in_a_box.core import agent_docker

    empty_desktop(monkeypatch)
    actual = dev()
    expected = ""
    errors = False
    if site == "helper":
        actual = dev(
            [
                {
                    "Type": "volume",
                    "Name": hostctl.HOSTCTL_STATE_VOLUME,
                    "Destination": "/helper",
                }
            ]
        )
        expected = "helper identity volume exposed to dev"
    elif site == "environment":
        actual = dev(env=["DOCKER_HOST=tcp://unverified-proxy:2375"])
        expected = "Unverified Docker endpoint exposed in dev environment"
    elif site == "proxy":
        proxy = dev(identity="proxy-a")
        proxy["Name"] = "/unverified-proxy"
        proxy["Config"]["Image"] = "tecnativa/docker-socket-proxy:latest"
        ground.objects["proxy-a"] = proxy
        monkeypatch.setattr(
            hostctl,
            "command",
            lambda *args, **kwargs: (
                "proxy-a"
                if args[0] == "ps"
                else json.dumps({"DockerRootDir": str(ground.paths["data"])})
            ),
        )
        expected = "reachable Docker proxy /unverified-proxy on djinn-network"
    elif site == "channel-error":
        row = desktop.DesktopChannelInspection(
            "dbus", "unknown", None, ("unavailable",), False, False
        )
        monkeypatch.setattr(
            sealing,
            "_desktop",
            lambda actual: desktop.DesktopInspection((row,), (), True),
        )
        expected = "dbus inspection unknown: unknown; unavailable"
        errors = True
    elif site == "volume-error":
        ground.objects["unverified-volume"] = {"Driver": "unknown"}
        actual = dev(
            [
                {
                    "Type": "volume",
                    "Name": "unverified-volume",
                    "Destination": "/delivery",
                }
            ]
        )
        expected = (
            "volume inspection uncertain: volume storage provenance unknown: unverified-volume"
        )
        errors = True
    elif site == "outer-error":

        def fail_inventory():
            raise ValueError("inventory unavailable")

        monkeypatch.setattr(sealing, "execution_paths", fail_inventory)
        expected = "inspection uncertain: inventory unavailable"
        errors = True
    elif site == "verifier-error":
        monkeypatch.setattr(
            docker,
            "inspect_agent_endpoint",
            lambda *args, **kwargs: agent_docker.EndpointVerification(
                "unknown", "unavailable", errors=("agent inspection unavailable",)
            ),
        )
        expected = "agent inspection unavailable"
        errors = True
    else:

        def fail_inspection(*args, **kwargs):
            raise ValueError("invalid inspect JSON")

        monkeypatch.setattr(host_runtime, "inspect_object", fail_inspection)
        result = sealing.inspect_assessment("dev-a")
        expected = "dev inspection uncertain: invalid inspect JSON"
        assert result.error_findings == (sealing.Finding(expected),)
        assert result.errors == result.error_details == (expected,)
        return
    result = sealing.assess(actual)
    assert (result.error_findings if errors else result.cause_findings) == (
        sealing.Finding(expected),
    )
    assert (result.errors if errors else result.causes) == (expected,)


@pytest.mark.parametrize(
    "kind,key,item",
    [
        (
            "writable Djinn installation and configuration",
            "Djinn configuration file",
            "Djinn configuration file",
        ),
        ("writable Python environment", "Python module example", "example"),
        ("writable PATH directory", "host PATH directory /bin/example", "/bin/example"),
        (
            "writable Docker CLI and plugins",
            "Docker plugin directory /plugins",
            "/plugins",
        ),
    ],
)
def test_execution_inode_alias_uses_short_item(ground, monkeypatch, kind, key, item):
    empty_desktop(monkeypatch)
    protected = ground.paths["python"] / "protected"
    protected.touch()
    source = ground.paths["home"].with_name("workspace")
    source.mkdir()
    candidate = source / "alias"
    candidate.hardlink_to(protected)
    monkeypatch.setattr(sealing, "execution_paths", lambda: {key: (protected, kind, item)})
    result = sealing.assess(dev([bind(source, rw=True)]))
    assert result.cause_findings == (
        sealing.Finding(
            f"writable {key} inode alias {candidate}: {source} -> /delivery",
            (str(source), "/delivery"),
            kind,
            f"{item} inode alias {candidate}",
        ),
    )
    assert result.causes == (f"{source} -> /delivery: {kind} {item} inode alias {candidate}",)
    assert result.state == "unsealed" and not result.errors
