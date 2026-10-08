"""Desktop boundary regressions. All Docker objects are supplied disposable fixtures."""

from __future__ import annotations

import json
import socket
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import desktop, docker, host_runtime
from djinn_in_a_box.core.exceptions import RuntimeMountSpecificationError

ROOT = Path(__file__).resolve().parents[1]


def endpoint(channel="dbus", available=True):
    return desktop.DesktopEndpoint(
        channel,
        Path("/host/runtime/bus" if channel == "dbus" else "/host/runtime/pulse/native"),
        available,
    )


def objects(e):
    source = f"/docker/volumes/{e.volume}/_data"
    output = {
        "Type": "volume",
        "Name": e.volume,
        "Source": source,
        "Destination": e.target,
        "RW": False,
    }
    helper = {
        "Id": e.service + "-id",
        "Image": "sha256:trusted",
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Config": {
            "Cmd": ["python3", "-I", "/etc/djinn/helper.py", e.channel],
            "Entrypoint": None,
            "User": "",
            "WorkingDir": "/",
            "Labels": {
                "com.docker.compose.project": "djinn-in-a-box",
                "com.docker.compose.service": e.service,
                host_runtime.GENERATION_LABEL: "generation",
            },
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "NetworkMode": "none",
            "CapDrop": ["ALL"],
            # As Compose 5 reports them; the plain Docker CLI omits the CAP_ prefix.
            "CapAdd": ["CAP_CHOWN", "CAP_SETGID", "CAP_SETPCAP", "CAP_SETUID"],
            "SecurityOpt": ["no-new-privileges:true"],
        },
        "Mounts": [
            {**output, "Destination": "/out", "RW": True},
            {
                "Type": "bind",
                "Source": str(e.upstream if e.channel == "dbus" else e.upstream.parent),
                "Destination": "/upstream/bus" if e.channel == "dbus" else "/upstream/pulse",
                "RW": False,
            },
        ],
    }
    dev = {
        "Id": "dev-id",
        "State": {"Running": True},
        "Config": {
            "Env": [f"{k}={v}" for k, v in e.environment.items()],
            "Labels": {host_runtime.GENERATION_LABEL: "generation"},
        },
        "Mounts": [output],
    }
    evidence = {
        **desktop.image_policy(),
        "health_ok": True,
        "version": "0.1.6-1+deb13u3",
        "floor": desktop.PROXY_VERSION_FLOOR,
        "floor_ok": True,
    }
    return dev, helper, {"Id": "sha256:trusted"}, evidence


def inspect(e, dev, helper, image, evidence):
    return desktop.inspect_desktop_endpoints(
        dev, {e.service: helper}, {desktop.HELPER_IMAGE: image}, (e,), {e.service: evidence}
    )


@pytest.mark.parametrize("prefix", ["CAP_", ""])
def test_verified_endpoints(prefix):
    for channel, expected in (("dbus", "filtered"), ("audio", "locked")):
        e = endpoint(channel)
        dev, helper, image, evidence = objects(e)
        caps = helper["HostConfig"]["CapAdd"]
        helper["HostConfig"]["CapAdd"] = [prefix + c.removeprefix("CAP_") for c in caps]
        result = inspect(e, dev, helper, image, evidence)
        assert result.sealed_ok is True
        assert result.channels[0].state == expected
        assert result.raw_verified


FAULTS = [
    "raw-leaf",
    "raw-ancestor",
    "raw-alias",
    "output-state-alias",
    "rw-output",
    "wrong-volume",
    "wrong-source",
    "listener-shadow",
    "var-run-shadow",
    "wrong-env",
    "no-cookie-env",
    "unhealthy",
    "wrong-image",
    "wrong-service",
    "wrong-project",
    "wrong-command",
    "writable-root",
    "extra-cap",
    "no-cap-drop",
    "network",
    "extra-mount",
    "rw-upstream",
    "wrong-policy",
    "no-version",
    "old-version",
    "no-health",
    "wrong-relay",
    "wrong-daemon",
]


@pytest.mark.parametrize("fault", FAULTS)
def test_inspector_rejects_unverified_delivery(fault, tmp_path):
    e = endpoint("audio" if fault in {"no-cookie-env", "wrong-relay", "wrong-daemon"} else "dbus")
    dev, helper, image, evidence = objects(e)
    if fault.startswith("raw-"):
        source = str(e.upstream) if fault == "raw-leaf" else "/host"
        if fault == "raw-alias":
            alias = tmp_path / "alias"
            alias.symlink_to(source)
            source = str(alias)
        dev["Mounts"].append({"Type": "bind", "Source": source, "Destination": "/alternate"})
    elif fault == "output-state-alias":
        dev["Mounts"].append(
            {"Type": "bind", "Source": dev["Mounts"][0]["Source"], "Destination": "/alias"}
        )
    elif fault == "rw-output":
        dev["Mounts"][0]["RW"] = True
    elif fault == "wrong-volume":
        dev["Mounts"][0]["Name"] = "untrusted"
    elif fault == "wrong-source":
        dev["Mounts"][0]["Source"] = "/other"
    elif fault in {"listener-shadow", "var-run-shadow"}:
        dev["Mounts"].append(
            {
                "Type": "volume",
                "Name": "untrusted",
                "Source": "/other",
                "Destination": (e.target + "/bus").replace(
                    "/run/", "/var/run/" if fault == "var-run-shadow" else "/run/"
                ),
            }
        )
    elif fault == "wrong-env":
        dev["Config"]["Env"] = ["DBUS_SESSION_BUS_ADDRESS=unix:path=/alternate"]
    elif fault == "no-cookie-env":
        dev["Config"]["Env"] = [
            v for v in dev["Config"]["Env"] if not v.startswith("PULSE_COOKIE=")
        ]
    elif fault == "unhealthy":
        helper["State"]["Health"]["Status"] = "unhealthy"
    elif fault == "wrong-image":
        image["Id"] = "sha256:other"
    elif fault in {"wrong-service", "wrong-project"}:
        helper["Config"]["Labels"]["com.docker.compose." + fault.removeprefix("wrong-")] = "other"
    elif fault == "wrong-command":
        helper["Config"]["Cmd"].remove("-I")
    elif fault == "writable-root":
        helper["HostConfig"]["ReadonlyRootfs"] = False
    elif fault == "extra-cap":
        helper["HostConfig"]["CapAdd"].append("CAP_SYS_ADMIN")
    elif fault == "no-cap-drop":
        helper["HostConfig"]["CapDrop"] = None
    elif fault == "network":
        helper["HostConfig"]["NetworkMode"] = "host"
    elif fault == "extra-mount":
        helper["Mounts"].append({"Type": "bind", "Source": "/host/config", "Destination": "/etc"})
    elif fault == "rw-upstream":
        helper["Mounts"][1]["RW"] = True
    elif fault == "wrong-policy":
        evidence["policy"]["dbus"].append("--talk=*")
    elif fault == "no-version":
        evidence.pop("version")
    elif fault == "old-version":
        evidence["version"] = "0.1.6-1+deb13u2"
    elif fault == "no-health":
        evidence["health_ok"] = False
    elif fault == "wrong-relay":
        evidence["relay"] += "load-module module-cli\n"
    elif fault == "wrong-daemon":
        evidence["daemon"] = "allow-module-loading = yes\n"
    result = inspect(e, dev, helper, image, evidence)
    assert result.sealed_ok is False
    assert result.channels[0].reasons


@pytest.mark.parametrize("case", ["off", "missing", "unknown-host", "no-dev", "no-mount-evidence"])
def test_absence_and_unknown_are_distinct(case):
    e = endpoint(available=False if case == "off" else None if case == "unknown-host" else True)
    dev = {"State": {"Running": True}, "Config": {"Env": []}, "Mounts": []}
    if case == "no-dev":
        dev = None
    elif case == "no-mount-evidence":
        dev.pop("Mounts")
    result = inspect(e, dev, None, None, {})
    row = result.channels[0]
    assert row.state == (
        "unknown"
        if case.startswith("no-")
        else "missing"
        if case in {"missing", "unknown-host"}
        else "off"
    )
    assert result.sealed_ok is (None if case.startswith("no-") else True)


@pytest.mark.parametrize("channel", ["dbus", "audio"])
@pytest.mark.parametrize("kind", ["socket", "stale", "absent", "error"])
def test_discovery_only_standard_sockets(channel, kind, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/ignored")
    monkeypatch.setenv("PULSE_SERVER", "unix:/ignored")
    leaf = tmp_path / ("bus" if channel == "dbus" else "pulse/native")
    leaf.parent.mkdir(exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if kind == "socket":
        server.bind(str(leaf))
    elif kind == "stale":
        leaf.touch()
    elif kind == "error":
        original = Path.stat

        def failed(path, *args, **kwargs):
            if path == leaf:
                raise PermissionError("unreadable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", failed)
    try:
        e = desktop.discover_desktop_endpoints()[0 if channel == "dbus" else 1]
        assert e.upstream == leaf
        assert e.available is (None if kind == "error" else kind == "socket")
        assert bool(e.error) == (kind == "error")
    finally:
        server.close()


@pytest.mark.parametrize("creator", ["interactive", "headless", "detached"])
@pytest.mark.parametrize("key", sorted(desktop.MANAGED_ENV))
def test_caller_cannot_override_endpoint(creator, key, tmp_path, monkeypatch):
    monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
    monkeypatch.setattr(
        docker, "git_runtime", lambda *args: pytest.fail("guard entered before validation")
    )
    config = AppConfig(code_dir=tmp_path)
    with pytest.raises(RuntimeMountSpecificationError, match="managed by Djinn"):
        if creator == "detached":
            docker.compose_up_detached(config, docker.ContainerOptions(), env={key: "untrusted"})
        else:
            docker.compose_run(
                config,
                docker.ContainerOptions(),
                env={key: "untrusted"},
                interactive=creator == "interactive",
            )


@pytest.mark.parametrize("creator", ["interactive", "headless", "detached"])
@pytest.mark.parametrize(
    "outcome",
    [
        "healthy",
        "absent",
        "audio-only",
        "both",
        "independent",
        "missing-image",
        "unhealthy",
        "auth-failure",
    ],
)
@pytest.mark.parametrize("mode", list(docker.DockerMode))
def test_creator_delivery_and_degradation(creator, outcome, mode, tmp_path, monkeypatch, capsys):
    config = AppConfig(code_dir=tmp_path)
    e = endpoint()
    audio_delivered = outcome in {"audio-only", "both", "independent"}
    other = endpoint("audio", audio_delivered)
    dev, helper, image, evidence = objects(e)
    audio_objects = objects(other)
    proxy = {
        "Id": "proxy-id",
        "Config": {
            "Labels": {
                host_runtime.GENERATION_LABEL: "generation",
                "com.docker.compose.project": "djinn-in-a-box",
                "com.docker.compose.service": "agent-docker",
            }
        },
    }
    services = {"dbus-helper": helper, "audio-helper": audio_objects[1], "agent-docker": proxy}
    events, fragments = [], []
    owner = SimpleNamespace(
        root=tmp_path,
        generation="generation",
        resources={},
        observer=MagicMock(),
        docker_path=docker.DOCKER_EXECUTABLE,
        detached=False,
    )
    owner.observer.poll.return_value = None
    owner.add_to_fragment = lambda f: events.append("guard")
    owner.register = lambda service, obj, volume=None: owner.resources.update(
        {service: {"id": obj["Id"], "service": service}}
    )
    owner.register_volume = lambda *args, **kwargs: None
    owner.discard_volume = lambda *args: None
    owner.forget = lambda service: owner.resources.pop(service, None)
    owner.begin_creation = lambda: None
    owner.retain = lambda: setattr(owner, "detached", True)

    @contextmanager
    def lease(*args):
        yield owner
        events.append("retain" if owner.detached else "close")

    monkeypatch.setattr(docker, "_prepare_agent_docker", lambda *args: None)
    monkeypatch.setattr(docker, "git_runtime", lease)
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda *args: [])
    monkeypatch.setattr(docker, "get_sops_age_key_mount_args", lambda *args: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *args: ([], ()))
    monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
    monkeypatch.setattr(host_runtime, "CONTAINER_USER_UID", __import__("os").getuid())
    monkeypatch.setattr(
        desktop,
        "discover_desktop_endpoints",
        lambda: (endpoint(available=False) if outcome in {"absent", "audio-only"} else e, other),
    )
    calls = {}

    def service(name, *args):
        calls[name] = calls.get(name, 0) + 1
        return None if calls[name] == 1 else services[name]

    monkeypatch.setattr(docker, "_service_inspect", service)
    if outcome in {"unhealthy", "independent"}:
        helper["State"]["Health"]["Status"] = "unhealthy"

    def actual(name, path, resource="container", **kwargs):
        if name == "djinn-hostctl" and resource == "container":
            return None
        if resource == "volume":
            return None
        if resource == "image":
            return None if outcome == "missing-image" else image
        return next(obj for obj in services.values() if obj["Id"] == name)

    monkeypatch.setattr(host_runtime, "inspect_object", actual)
    monkeypatch.setattr(docker, "_helper_evidence", lambda *args: evidence)

    def probe(*args):
        events.append("probe")
        if outcome == "auth-failure":
            raise RuntimeError("credentials refused")

    monkeypatch.setattr(docker, "_downstream_probe", probe)

    def execute(cmd, **kwargs):
        assert events[0] == "guard"
        if "-f" in cmd:
            paths = [Path(cmd[i + 1]) for i, word in enumerate(cmd) if word == "-f"]
            fragments.append(json.loads(paths[-1].read_text()))
        assert not desktop.MANAGED_ENV.intersection(kwargs.get("env", {}))
        return SimpleNamespace(returncode=0, stdout="agent-output", stderr="")

    monkeypatch.setattr(docker.subprocess, "run", execute)
    monkeypatch.setenv("PULSE_SERVER", "unix:/raw-inherited")
    options = docker.ContainerOptions(docker_mode=mode)
    if creator == "detached":
        result = docker.compose_up_detached(config, options)
    else:
        result = docker.compose_run(
            config, options, interactive=creator == "interactive", command="true"
        )
    assert result.returncode == 0
    if creator == "headless":
        assert result.stdout == "agent-output"
    delivery = fragments[-1]["services"]["dev"]
    output = [
        m
        for m in delivery.get("volumes", [])
        if isinstance(m, dict) and m.get("target") == e.target
    ]
    assert bool(output) == (outcome in {"healthy", "both"})
    assert delivery["environment"].get("DBUS_SESSION_BUS_ADDRESS") == (
        e.environment["DBUS_SESSION_BUS_ADDRESS"] if outcome in {"healthy", "both"} else None
    )
    assert delivery["environment"].get("PULSE_COOKIE") == (
        other.environment["PULSE_COOKIE"] if audio_delivered else None
    )
    audio_output = [
        m
        for m in delivery.get("volumes", [])
        if isinstance(m, dict) and m.get("target") == other.target
    ]
    assert bool(audio_output) == audio_delivered
    if audio_output:
        assert audio_output[0]["read_only"] is True
    if output:
        assert output[0]["read_only"] is True
    assert all(
        "/upstream" not in str(m) and "/host/runtime" not in str(m)
        for m in delivery.get("volumes", [])
    )
    warning = capsys.readouterr().err
    assert ("Desktop dbus missing" in warning) == (
        outcome not in {"healthy", "absent", "both", "audio-only"}
    )
    assert events[-1] == ("retain" if creator == "detached" else "close")


@pytest.mark.parametrize(
    "case",
    [
        "filter",
        "wildcard",
        "own",
        "lock",
        "cli",
        "source",
        "health-uid",
        "isolated",
        "digest",
        "floor",
        "readonly",
        "clients",
    ],
)
def test_packaged_boundary_policy(case):
    policy = json.loads((ROOT / "helpers/desktop/policy.json").read_text())
    relay = (ROOT / "helpers/desktop/relay.pa").read_text()
    health = (ROOT / "helpers/desktop/health.py").read_text()
    compose = (ROOT / "docker-compose.desktop.yml").read_text()
    dockerfile = (ROOT / "helpers/desktop/Dockerfile").read_text()
    if case == "filter":
        assert "--filter" in policy["dbus"]
    elif case == "wildcard":
        # Only the Notifications interface on its object path, not the owner's other interfaces.
        name = "org.freedesktop.Notifications"
        rule = f"{name}={name}.*@/org/freedesktop/Notifications"
        assert policy["dbus"][3:] == ["--filter", f"--call={rule}", f"--broadcast={rule}"]
    elif case == "own":
        assert not any(a.startswith("--own") for a in policy["dbus"])
    elif case == "lock":
        assert "--disallow-module-loading=yes" in policy["audio"]
    elif case == "cli":
        assert len(relay.splitlines()) == 3
        assert "module-cli" not in relay and "default.pa" not in policy["audio"]
    elif case == "source":
        assert "module-tunnel-source-new" in relay
        assert "get-default-source" in health
    elif case == "health-uid":
        assert "--reuid=1000" in health and "--regid=1000" in health
        assert "--clear-groups" in health
    elif case == "isolated":
        import yaml

        services = yaml.safe_load(compose)["services"]
        for channel in ("dbus", "audio"):
            assert services[f"{channel}-helper"]["command"] == [
                "python3",
                "-I",
                "/etc/djinn/helper.py",
                channel,
            ]
        assert "working_dir: /" in compose
    elif case == "digest":
        assert __import__("re").search(r"FROM debian:trixie-slim@sha256:[0-9a-f]{64}", dockerfile)
    elif case == "floor":
        assert "dpkg --compare-versions" in dockerfile and "ge 0.1.6-1+deb13u3" in dockerfile
    elif case == "readonly":
        assert "read_only: true" in compose and "network_mode: none" in compose
    elif case == "clients":
        dev = (ROOT / "Dockerfile").read_text()
        assert "libnotify-bin dbus-bin" in dev


@pytest.mark.parametrize(
    "version,expected", [("0.1.6-1+deb13u2", False), ("0.1.6-1+deb13u3", True), ("0.1.9-1", True)]
)
def test_build_version_assertion(version, expected, tmp_path):
    import os
    import re
    import subprocess

    dockerfile = (ROOT / "helpers/desktop/Dockerfile").read_text()
    assertion = re.search(r"    && (dpkg --compare-versions [^\n]+) \\\n", dockerfile).group(1)
    query = tmp_path / "dpkg-query"
    query.write_text(f"#!/bin/sh\nprintf '%s' '{version}'\n")
    query.chmod(0o700)
    result = subprocess.run(
        ["sh", "-c", assertion],
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
    )
    assert (result.returncode == 0) is expected


def test_downstream_probe_uses_supported_compose_run_flags(tmp_path, monkeypatch):
    # Fakes accept any flag; the real Compose CLI rejects unknown ones (e.g. run --no-build).
    import re
    import shutil
    import subprocess

    docker_cli = shutil.which("docker")
    if docker_cli is None:
        pytest.skip("Docker CLI unavailable")
    help_text = subprocess.run(
        [docker_cli, "compose", "run", "--help"], capture_output=True, text=True, check=False
    )
    if help_text.returncode:
        pytest.skip("docker compose unavailable")
    captured = []
    monkeypatch.setattr(
        host_runtime,
        "inspect_object",
        lambda name, path, resource="container", **kwargs: (
            {"Id": "sha256:dev"} if resource == "image" else None
        ),
    )
    monkeypatch.setattr(
        docker,
        "_run_compose",
        lambda args, **kwargs: captured.append(args) or SimpleNamespace(success=True, stderr=""),
    )
    docker._downstream_probe(
        AppConfig(code_dir=tmp_path),
        docker.ContainerOptions(),
        endpoint(),
        SimpleNamespace(generation="generation"),
    )
    args = captured[0]
    flags = [a for a in args[args.index("run") + 1 :] if a.startswith("-")]
    assert flags
    for flag in flags:
        assert re.search(rf"(^|\s){re.escape(flag)}(,|\s)", help_text.stdout, re.M), flag


def test_downstream_probe_declares_the_helper_volume(tmp_path, monkeypatch):
    # A differing volume definition makes Compose ask whether to recreate the volume.
    fragments = []

    def run(args, **kwargs):
        fragments.append(json.loads(Path(args[args.index("run") - 1]).read_text()))
        return SimpleNamespace(success=True, stderr="")

    monkeypatch.setattr(
        host_runtime,
        "inspect_object",
        lambda name, path, resource="container", **kwargs: (
            {"Id": "sha256:dev"} if resource == "image" else None
        ),
    )
    monkeypatch.setattr(docker, "_run_compose", run)
    docker._downstream_probe(
        AppConfig(code_dir=tmp_path),
        docker.ContainerOptions(),
        endpoint(),
        SimpleNamespace(generation="generation"),
    )
    assert fragments[0]["volumes"] == {
        "desktop-dbus": {
            "name": "djinn-desktop-dbus",
            "labels": {host_runtime.GENERATION_LABEL: "generation"},
        }
    }


def test_root_bootstrap_only_hands_over_directories():
    # Without CAP_FOWNER/CAP_DAC_OVERRIDE root can neither chmod nor write a directory
    # it has chowned to UID 1000, so all preparation runs after the privilege drop.
    import ast

    tree = ast.parse((ROOT / "helpers/desktop/helper.py").read_text())
    initialize = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "initialize"
    )
    calls = {
        n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
        for n in ast.walk(initialize)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute | ast.Name)
    }
    assert calls <= {"Path", "mkdir", "chown", "update", "execvp"}
    # Docker recreates the tmpfs root-owned on restart; it is handed over after its children.
    loop = next(n for n in ast.walk(initialize) if isinstance(n, ast.For))
    assert isinstance(loop.iter, ast.Tuple)
    assert [e.value for e in loop.iter.elts if isinstance(e, ast.Constant)][-1] == "/runtime"


@pytest.mark.parametrize("state", ["off", "filtered", "locked", "missing", "raw", "unknown"])
def test_doctor_consumes_read_only_inspector(state, monkeypatch):
    from djinn_in_a_box.commands import doctor

    row = desktop.DesktopChannelInspection(
        "dbus",
        state,
        state in {"off", "filtered", "locked"},
        ("supplied evidence",),
        True,
        state != "off",
        "0.1.6-1+deb13u3",
    )
    inspection = desktop.DesktopInspection(
        (row,), ("/raw -> /alternate",) if state == "raw" else (), state != "unknown"
    )
    monkeypatch.setattr(doctor.docker_core, "inspect_running_desktop", lambda: inspection)
    for name in (
        "_docker_installed",
        "docker_daemon_ok",
        "_docker_socket_ok",
        "compose_v2_ok",
        "buildx_ok",
        "_image_built",
        "network_exists",
    ):
        monkeypatch.setattr(doctor, name, lambda *args: False)
    monkeypatch.setattr(doctor, "get_project_root", lambda: ROOT)
    checks = doctor.run_checks(None)
    bus = next(c for c in checks if c.name == "D-Bus session")
    raw = next(c for c in checks if c.name == "Desktop raw sockets")
    assert bus.status.value == (
        "fail" if state == "raw" else "pass" if state in {"off", "filtered", "locked"} else "warn"
    )
    assert "0.1.6-1+deb13u3" in bus.detail
    assert raw.status.value == (
        "fail" if state == "raw" else "warn" if state == "unknown" else "pass"
    )
