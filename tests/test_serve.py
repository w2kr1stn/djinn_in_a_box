"""Host serve tests: all daemon/CLI calls are faked with production-shaped replies."""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import serve as command
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import host_runtime, serve
from djinn_in_a_box.core.docker import RunResult
from djinn_in_a_box.core.host_runtime import inspect_object

CID = "a1" * 32
PORT = 49380
runner = CliRunner()


def inspection():
    return {
        "Id": CID,
        "State": {"Running": True},
        "Config": {"Env": ["HOME=/home/dev", "DJINN_DETACHED=true"]},
        "HostConfig": {
            "PortBindings": {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(PORT)}]},
            "NetworkMode": "djinn-network",
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": "djinn-opencode-data",
                "Source": "/var/lib/docker/volumes/djinn-opencode-data/_data",
                "Destination": "/home/dev/.opencode",
            }
        ],
        "NetworkSettings": {"Networks": {"djinn-network": {"NetworkID": "b" * 64}}},
    }


def status_reply(state="running"):
    return {
        "ok": True,
        "pid1_start": "12345",
        "busy": False,
        "servers": {
            "claude": {
                "state": "stopped",
                "reason": None,
                "pid": None,
                "since": None,
                "last_line": None,
            },
            "opencode": {
                "state": state,
                "reason": None,
                "pid": 123,
                "since": "2026-10-20T10:00:00+00:00",
                "last_line": None,
                "listening": state == "running",
                "discovery_pid_ok": state == "running",
                "foreign_listener": False,
                "relay_refused": 0,
                "relay_note": None,
            },
        },
    }


def tailscale_config(target=None, *, key="box.example.ts.net:443", path="/", funnel=False):
    # Tailscale ipn.ServeConfig: documented JSON, not a claim of live tailnet measurement.
    return {
        "TCP": {key.rpartition(":")[2]: {"HTTPS": True}},
        "Web": {key: {"Handlers": {path: {"Proxy": target or f"http://127.0.0.1:{PORT}"}}}},
        "AllowFunnel": {key: funnel},
    }


@pytest.fixture
def host(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "config")
    monkeypatch.setattr(command, "load_config", lambda: config)
    monkeypatch.setattr(serve.shutil, "which", lambda name: "/test/bin/tailscale")
    data = SimpleNamespace(
        inspect=inspection(),
        config=config,
        status=status_reply(),
        tailnet=tailscale_config(),
        network={"Name": "djinn-network", "Driver": "bridge", "Options": {}},
        network_rc=0,
        volume={
            "Driver": "local",
            "Options": None,
            "Mountpoint": "/var/lib/docker/volumes/djinn-opencode-data/_data",
        },
        volume_calls=[],
        engine="29.9.1",
        calls=[],
        direct=[],
        error=None,
        stop_result="not-running",
        start_result="started",
        pair_rc=0,
    )

    def helper(args):
        if data.error:
            return RunResult(1, json.dumps({"ok": False, **data.error}))
        if args[0] == "status":
            reply = data.status
        elif args[0] == "opencode-preflight":
            reply = {"ok": True}
        elif args[0] == "stop":
            reply = {
                "ok": True,
                "results": {name: {"result": data.stop_result, "pid": 123} for name in args[2::2]},
            }
        else:
            name = args[0].removesuffix("-start")
            reply = {
                "ok": True,
                "name": name,
                "result": data.start_result,
                "pid": 123,
                "since": None,
                "log_tail": [],
            }
            if name == "opencode":
                reply["stopped_previous"] = True
        return RunResult(0, json.dumps(reply))

    def captured(argv, **kwargs):
        data.calls.append((argv, kwargs))
        if argv[1] == "inspect":
            return RunResult(0, json.dumps(data.inspect))
        if argv[1] == "network":
            return RunResult(
                data.network_rc,
                json.dumps([data.network]),
                "Error response from daemon: network x not found",
            )
        if argv[1] == "version":
            return RunResult(0, data.engine)
        if argv[1] == "serve":
            return RunResult(0, json.dumps(data.tailnet))
        if argv[1] == "exec":
            return helper(argv[argv.index("-c") + 2 :])
        raise AssertionError(argv)

    def direct(argv, **kwargs):
        if argv[1:3] == ["volume", "inspect"]:
            data.volume_calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, json.dumps([data.volume]), "")
        data.direct.append((argv, kwargs))
        if "pair" in argv:
            return subprocess.CompletedProcess(argv, data.pair_rc)
        reply = helper(argv[argv.index("-c") + 2 :])
        return subprocess.CompletedProcess(argv, reply.returncode, reply.stdout, reply.stderr)

    monkeypatch.setattr(serve, "_run_captured", captured)
    monkeypatch.setattr(serve.subprocess, "run", direct)
    # The legacy Compose autouse fixture replaces this; exercise its bounded parser here.
    monkeypatch.setattr(host_runtime, "inspect_object", inspect_object)
    return data


def test_protocol_enums_match_helper():
    path = Path(serve.__file__).with_name("serve_helper.py")
    spec = importlib.util.spec_from_file_location("serve_protocol_fixture", path)
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    try:
        spec.loader.exec_module(helper)
        for name in (
            "STATE_DIR",
            "NAMES",
            "STATES",
            "START_RESULTS",
            "STOP_RESULTS",
            "ERROR_CODES",
        ):
            assert getattr(serve, name) == getattr(helper, name)
    finally:
        del sys.modules[spec.name]


def test_fresh_cli_import_does_not_import_container_helper():
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; import djinn_in_a_box.cli.djinn; "
            "assert 'djinn_in_a_box.core.serve_helper' not in sys.modules",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize(
    "reply,state",
    [
        (RunResult(1, stderr="Error response from daemon: No such container: djinn"), "ABSENT"),
        (
            RunResult(
                1,
                stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                "Is the docker daemon running?",
            ),
            "UNKNOWN",
        ),
        (RunResult(124, stderr="Docker operation timed out"), "UNKNOWN"),
        (RunResult(127, stderr="Command not found: docker"), "UNKNOWN"),
        (RunResult(0, ""), "UNKNOWN"),
        (RunResult(0, "{broken"), "UNKNOWN"),
        (RunResult(0, "[]"), "UNKNOWN"),
        (RunResult(0, "{}"), "UNKNOWN"),
    ],
)
def test_container_tri_state(monkeypatch, reply, state):
    mock = MagicMock(return_value=reply)
    monkeypatch.setattr(serve, "_run_captured", mock)
    assert serve.detect_container().state == getattr(serve.ContainerState, state)
    argv = mock.call_args.args[0]
    assert argv[1:] == ["inspect", "--type", "container", "djinn", "--format", "{{json .}}"]
    assert mock.call_args.kwargs == {"timeout": 10}


@pytest.mark.parametrize("exception", [FileNotFoundError(), subprocess.TimeoutExpired("docker", 3)])
def test_detection_catches_execution_failures(monkeypatch, exception):
    monkeypatch.setattr(serve, "_run_captured", MagicMock(side_effect=exception))
    assert serve.detect_container().state == serve.ContainerState.UNKNOWN


@pytest.mark.parametrize(
    "field,value",
    [
        ("Id", "short"),
        ("State", {"Running": "true"}),
        ("Config", {"Env": [None]}),
        ("HostConfig", None),
    ],
)
def test_detection_rejects_invalid_schema(host, field, value):
    host.inspect[field] = value
    assert serve.detect_container().state == serve.ContainerState.UNKNOWN


@pytest.mark.parametrize(
    "marker", ["DJINN_DETACHED=false", "DJINN_DETACHED=truex", "XDJINN_DETACHED=true", ""]
)
def test_refuses_container_not_started_detached(host, marker):
    host.inspect["Config"]["Env"] = ["HOME=/home/dev", marker]
    result = runner.invoke(app, ["serve", "claude"])
    assert result.exit_code == 1
    assert "not started with --detach" in result.output
    assert "Recreate it: djinn clean, then djinn start --detach" in result.output
    assert not host.direct


@pytest.mark.parametrize(
    "ports",
    [
        None,
        {},
        {"4096/tcp": []},
        *[{"4096/tcp": [{"HostIp": ip, "HostPort": str(PORT)}]} for ip in ("0.0.0.0", "::")],
        {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49381"}]},
        {
            "4096/tcp": [
                {"HostIp": "127.0.0.1", "HostPort": str(PORT)},
                {"HostIp": "0.0.0.0", "HostPort": "49381"},
            ]
        },
        {"4096/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(PORT)}], "9999/tcp": []},
    ],
)
def test_refuses_wrong_port_binding(host, ports):
    host.inspect["HostConfig"]["PortBindings"] = ports
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == 1
    assert "does not publish the OpenCode port" in result.output
    assert not host.direct


@pytest.mark.parametrize(
    "destination",
    [
        "/home/dev/.config/opencode",
        "/home/dev/.local/state",
        "/home/dev",
        "/",
        "/home/dev/.config/opencode/subdir",
        "/home/dev/.local/state/opencode",
        "//home/dev/.config",
        "//home/dev/.local/state/opencode",
        "//home/dev",
        "/home/dev/./.config/opencode/",
    ],
)
def test_mount_over_opencode_settings_refused(host, destination):
    host.inspect["Mounts"] = [
        {
            "Type": "bind",
            "Source": str(host.config.code_dir / "mount"),
            "Destination": destination,
        }
    ]
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == 1
    expected = (
        "A mount overlaps the serve process-state directory"
        if destination == "/"
        else "A mount covers OpenCode's service settings"
    )
    assert expected in result.output
    assert not host.direct


def test_opencode_settings_allow_unrelated_double_slash_mount(host):
    host.inspect["Mounts"][0]["Destination"] = "//tmp/other"
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == 0, result.output
    assert host.direct


@pytest.mark.parametrize(
    "names", [["claude"], ["opencode"], ["claude", "opencode"], ["opencode", "claude"]]
)
@pytest.mark.parametrize(
    "destination,allowed",
    [
        ("/tmp", False),
        ("/tmp/djinn-serve", False),
        ("/tmp/djinn-serve/opencode.json", False),
        ("/tmp/./djinn-serve/../djinn-serve/", False),
        ("/tmp/other/..", False),
        ("//tmp", False),
        ("/tmp/other", True),
        ("/tmp/djinn-serve-other", True),
    ],
)
def test_process_state_mount_gate(host, names, destination, allowed):
    host.inspect["Mounts"][0]["Destination"] = destination
    result = runner.invoke(app, ["serve", *names])
    assert result.exit_code == (0 if allowed else 1), result.output
    if not allowed:
        assert "A mount overlaps the serve process-state directory" in result.output
        assert "writable layer" in result.output
        assert [argv[1] for argv, _ in host.calls] == ["inspect"]
        assert not host.direct
        assert not serve.password_path().exists()
        with pytest.raises(serve.ServeError) as exc:
            serve.check_process_state(
                serve.ContainerInfo(serve.ContainerState.RUNNING, mounts=host.inspect["Mounts"])
            )
        assert exc.value.code == "gate"


@pytest.mark.parametrize("name", ["claude", "opencode"])
@pytest.mark.parametrize(
    "mounts",
    [
        None,
        {},
        [None],
        [{}],
        [{"Destination": None}],
        [{"Destination": 123}],
        [{"Destination": "relative"}],
        [{"Destination": ""}],
    ],
)
def test_process_state_unknown_mounts_refused(host, name, mounts):
    host.inspect["Mounts"] = mounts
    result = runner.invoke(app, ["serve", name])
    assert result.exit_code == 1
    assert "Could not inspect the container's process-state mounts" in result.output
    assert [argv[1] for argv, _ in host.calls] == ["inspect"]
    assert not host.direct
    assert not serve.password_path().exists()


@pytest.mark.parametrize("name", ["claude", "opencode"])
@pytest.mark.parametrize(
    "variable",
    [
        "DJINN_SERVE_TEST_STATE_DIR",
        "DJINN_SERVE_TEST_HOME",
        "DJINN_SERVE_TEST_TIMING",
        "DJINN_SERVE_TEST_FUTURE",
    ],
)
@pytest.mark.parametrize("value", ["", "override"])
def test_process_state_test_overrides_refused(host, name, variable, value):
    host.inspect["Config"]["Env"].append(f"{variable}={value}")
    result = runner.invoke(app, ["serve", name])
    assert result.exit_code == 1
    assert "serve test override" in result.output and variable in result.output
    assert [argv[1] for argv, _ in host.calls] == ["inspect"]
    assert not host.direct
    assert not serve.password_path().exists()


@pytest.mark.parametrize("operation", [["--status"], ["--stop"], ["--pair", "opencode"]])
def test_process_state_gate_only_for_start(host, operation):
    host.inspect["Mounts"] = [{"Destination": "/tmp"}]
    host.inspect["Config"]["Env"].append("DJINN_SERVE_TEST_STATE_DIR=/tmp/override")
    result = runner.invoke(app, ["serve", *operation])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("state", ["absent", "unknown", "not-detached"])
def test_process_state_gate_after_container_gates(host, monkeypatch, state):
    container = (
        serve.ContainerInfo(serve.ContainerState.RUNNING, CID)
        if state == "not-detached"
        else serve.ContainerInfo(
            serve.ContainerState.UNKNOWN if state == "unknown" else serve.ContainerState.ABSENT
        )
    )
    monkeypatch.setattr(serve, "detect_container", lambda: container)
    gate = MagicMock()
    monkeypatch.setattr(serve, "check_process_state", gate)
    result = runner.invoke(app, ["serve", "claude"])
    assert result.exit_code == 1
    gate.assert_not_called()


@pytest.mark.parametrize(
    "entry,allowed",
    [
        ("HOME=/home/dev/", True),
        ("HOME=/home/dev/../dev", True),
        ("HOME=/home/other", False),
        ("XDG_CONFIG_HOME=", False),
        ("XDG_STATE_HOME=/tmp/state", False),
        ("OPENCODE_CONFIG_DIR=/tmp/config", False),
    ],
)
def test_root_selectors_refused(host, entry, allowed):
    host.inspect["Config"]["Env"] = ["DJINN_DETACHED=true", entry]
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == (0 if allowed else 1), result.output
    if not allowed:
        assert "environment relocates OpenCode's settings" in result.output
        assert not host.direct


@pytest.mark.parametrize(
    "mode,driver,options,rc,allowed",
    [
        ("djinn-network", "bridge", {}, 0, True),
        ("djinn-network", "bridge", {"com.docker.network.bridge.enable_icc": "true"}, 0, True),
        *[
            ("djinn-network", "bridge", {key: value}, 0, False)
            for key, values in [
                ("com.docker.network.bridge.trusted_host_interfaces", ["eth0", ""]),
                ("com.docker.network.bridge.gateway_mode_ipv4", ["routed", "nat-unprotected"]),
                ("com.docker.network.bridge.gateway_mode_ipv6", ["routed", "nat-unprotected"]),
            ]
            for value in values
        ],
        ("djinn-network", "bridge", {}, 1, False),
        *[
            (mode, "bridge", {}, 0, False)
            for mode in ("host", "container:abc", "none", "bridge", "")
        ],
        *[
            ("djinn-network", driver, {}, 0, False)
            for driver in ("macvlan", "ipvlan", "overlay", None)
        ],
    ],
)
def test_network_gate(host, mode, driver, options, rc, allowed):
    host.inspect["HostConfig"]["NetworkMode"] = mode
    host.network.update(Driver=driver, Options=options)
    host.network_rc = rc
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == (0 if allowed else 1), result.output
    if not allowed:
        assert "refusing" in result.output.lower()
        assert not host.direct


def test_every_attached_network_inspected(host):
    host.inspect["NetworkSettings"]["Networks"]["second"] = {"NetworkID": "c" * 64}
    assert runner.invoke(app, ["serve", "opencode"]).exit_code == 0
    assert [argv[-1] for argv, _ in host.calls if argv[1] == "network"] == [
        "djinn-network",
        "second",
    ]


@pytest.mark.parametrize("interface", ["all", "default", "eth0", "eth0.1"])
@pytest.mark.parametrize("value", ["1", "2", "-1", "invalid", "", None, 0])
def test_route_localnet_host_sysctls_refused(host, interface, value):
    host.inspect["HostConfig"]["Sysctls"] = {f"net.ipv4.conf.{interface}.route_localnet": value}
    with pytest.raises(serve.ServeError) as caught:
        serve.check_opencode_networks(serve.detect_container())
    assert caught.value.code == "gate"
    assert "Set route_localnet to 0" in str(caught.value)
    assert "recreate the container" in str(caught.value)


@pytest.mark.parametrize("network", ["djinn-network", "second"])
@pytest.mark.parametrize(
    "settings",
    [
        "net.ipv4.conf.IFNAME.route_localnet=1",
        "net.ipv4.conf.all.route_localnet=2",
        "net.ipv4.conf.default.route_localnet=-1",
        "net.ipv4.conf.eth0.route_localnet=invalid",
        "net.ipv4.conf.IFNAME.route_localnet",
        "net.ipv4.conf.IFNAME.route_localnet=",
        "net.ipv4.conf.IFNAME.log_martians=1, net.ipv4.conf.IFNAME.route_localnet = 1 ",
        "net.ipv4.conf.IFNAME.route_localnet=1,net.ipv4.conf.IFNAME.route_localnet=0",
    ],
)
def test_route_localnet_endpoint_sysctls_refused(host, network, settings):
    networks = host.inspect["NetworkSettings"]["Networks"]
    networks.setdefault(network, {"NetworkID": "c" * 64})["DriverOpts"] = {
        "com.docker.network.endpoint.sysctls": settings
    }
    with pytest.raises(serve.ServeError) as caught:
        serve.check_opencode_networks(serve.detect_container())
    assert caught.value.code == "gate"
    assert "route_localnet" in str(caught.value)
    assert "recreate the container" in str(caught.value)


@pytest.mark.parametrize("sysctls", [None, {}, {"net.ipv4.conf.all.route_localnet": "0"}])
@pytest.mark.parametrize(
    "driver_opts",
    [
        None,
        {},
        {"com.docker.network.endpoint.sysctls": "net.ipv4.conf.IFNAME.route_localnet=0"},
        {"com.docker.network.endpoint.sysctls": " net.ipv4.conf.IFNAME.route_localnet = 0 "},
        {
            "com.docker.network.endpoint.sysctls": (
                "net.ipv4.conf.all.route_localnet=0,net.ipv4.conf.IFNAME.log_martians=1"
            )
        },
    ],
)
def test_route_localnet_disabled_and_unrelated_sysctls_allowed(host, sysctls, driver_opts):
    host.inspect["HostConfig"]["Sysctls"] = sysctls
    if isinstance(sysctls, dict):
        sysctls["net.ipv4.ip_forward"] = "1"
    host.inspect["NetworkSettings"]["Networks"]["djinn-network"]["DriverOpts"] = driver_opts
    assert runner.invoke(app, ["serve", "opencode"]).exit_code == 0


@pytest.mark.parametrize("part", ["host", "endpoint", "driver-opts", "endpoint-sysctls"])
@pytest.mark.parametrize("value", ["uninspectable", [], True])
def test_route_localnet_uninspectable_metadata_refused(host, part, value):
    networks = host.inspect["NetworkSettings"]["Networks"]
    if part == "host":
        host.inspect["HostConfig"]["Sysctls"] = value
    elif part == "endpoint":
        networks["djinn-network"] = value
    elif part == "driver-opts":
        networks["djinn-network"]["DriverOpts"] = value
    else:
        if isinstance(value, str):
            value = 1
        networks["djinn-network"]["DriverOpts"] = {"com.docker.network.endpoint.sysctls": value}
    with pytest.raises(serve.ServeError) as caught:
        serve.check_opencode_networks(serve.detect_container())
    assert caught.value.code == "gate"


@pytest.mark.parametrize(
    "operation",
    [
        ["opencode"],
        ["claude", "opencode"],
        ["--pair", "opencode"],
        ["claude"],
        ["--status"],
        ["--stop"],
    ],
)
def test_route_localnet_gate_only_for_opencode_start_and_pair(host, operation):
    host.inspect["HostConfig"]["Sysctls"] = {"net.ipv4.conf.all.route_localnet": "1"}
    result = runner.invoke(app, ["serve", *operation])
    refused = "opencode" in operation
    assert result.exit_code == (1 if refused else 0), result.output
    if refused:
        assert "Set route_localnet to 0" in result.output
        assert not host.direct
        assert not (Path(os.environ["XDG_STATE_HOME"]) / "djinn/serve/opencode-password").exists()
        assert not any(argv[1] == "exec" for argv, _ in host.calls)


@pytest.mark.parametrize(
    "version,rc,allowed",
    [
        *[(v, 0, False) for v in ("27.5.1", "28.2.0", "28.3.2", "abc", "", "28.3")],
        *[(v, 0, True) for v in ("28.3.3", "28.3.3-rc1", "29.0.0+dirty", "29.9.1")],
        ("29.9.1", 1, False),
        ("", 124, False),
    ],
)
def test_engine_version_gate(monkeypatch, version, rc, allowed):
    mock = MagicMock(return_value=RunResult(rc, version))
    monkeypatch.setattr(serve, "_run_captured", mock)
    if allowed:
        serve.engine_supports_loopback_isolation()
    else:
        with pytest.raises(serve.ServeError):
            serve.engine_supports_loopback_isolation()
    assert mock.call_args.args[0][1:] == ["version", "--format", "{{.Server.Version}}"]
    assert mock.call_args.kwargs == {"timeout": 5}


def test_password_travels_only_on_stdin(host, monkeypatch):
    marker = "private-password-marker-123456789"
    monkeypatch.setattr(serve, "ensure_password", lambda *_: marker)
    before = os.environ.copy()
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == 0, result.output
    argv, kwargs = host.direct[0]
    assert argv[:4] == [serve.DOCKER_EXECUTABLE, "exec", "-i", CID]
    assert argv[4:7] == ["python3", "-I", "-c"]
    assert argv[7] == serve.helper_source()
    assert argv[8:] == ["opencode-start", "--port", "4096", "--cors", "https://box.example.ts.net"]
    assert json.loads(kwargs["input"]) == {"password": marker}
    assert kwargs["timeout"] == 60 and kwargs["capture_output"] is True
    assert marker not in repr(argv)
    assert marker not in repr({k: v for k, v in kwargs.items() if k != "input"})
    assert marker not in result.output
    assert "env" not in kwargs and "stdin" not in kwargs
    assert os.environ == before
    # Test-only overrides in the helper source are definitions, not exec env/flags.
    assert not any("DJINN_SERVE_TEST_" in value for value in argv[:7] + argv[8:])


def test_claude_argv_and_slug(host, monkeypatch):
    monkeypatch.setattr(serve.socket, "gethostname", lambda: "My_Host.local")
    assert serve.claude_prefix_slug() == "my-host"
    result = runner.invoke(app, ["serve", "claude"])
    assert result.exit_code == 0
    argv, kwargs = host.calls[-1]
    assert argv[:7] == [
        serve.DOCKER_EXECUTABLE,
        "exec",
        CID,
        "python3",
        "-I",
        "-c",
        serve.helper_source(),
    ]
    assert argv[7:] == ["claude-start", "--prefix", "my-host"]
    assert kwargs == {"timeout": 60}
    assert not host.direct
    monkeypatch.setattr(serve.socket, "gethostname", lambda: "___.local")
    assert serve.claude_prefix_slug() == "djinn-host"


@pytest.mark.parametrize(
    "kind", ["mode", "symlink", "directory", "owner", "empty", "large", "fifo"]
)
def test_password_file_refused(host, monkeypatch, kind):
    path = serve.password_path()
    path.parent.mkdir(parents=True, mode=0o700)
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
        real_fstat = serve.os.fstat

        def plausible_size(fd):
            fields = list(real_fstat(fd))
            fields[6] = 40  # Isolate the regular-file check from the independent size guard.
            return os.stat_result(fields)

        monkeypatch.setattr(serve.os, "fstat", plausible_size)
    else:
        path.write_text("x" * (0 if kind == "empty" else 257 if kind == "large" else 40))
        path.chmod(0o644 if kind == "mode" else 0o600)
        if kind == "symlink":
            target = path.with_name("target")
            path.rename(target)
            path.symlink_to(target)
        if kind == "owner":
            real = serve.os.fstat

            def foreign(fd):
                fields = list(real(fd))
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)

            monkeypatch.setattr(serve.os, "fstat", foreign)
    message = "symbolic link" if kind == "symlink" else "not a private regular file"
    with pytest.raises(serve.ServeError, match=message):
        serve.read_password(host.config, serve.detect_container())


def test_password_stable_and_exclusive(host, monkeypatch):
    real_open = serve.os.open
    opened = []

    def opening(path, flags, *args, **kwargs):
        opened.append((path, flags))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(serve.os, "open", opening)
    first = serve.ensure_password(host.config, serve.detect_container())
    assert len(first) >= 40
    assert serve.ensure_password(host.config, serve.detect_container()) == first
    assert serve.read_password(host.config, serve.detect_container()) == first
    path = serve.password_path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not (path.parent.parent / "hostctl").exists()
    assert list(path.parent.iterdir()) == [path]
    writes = [flags for _, flags in opened if flags & os.O_WRONLY]
    assert len(writes) == 1 and writes[0] & os.O_EXCL and writes[0] & os.O_NOFOLLOW


def test_password_concurrent_creator_reads_winner(host, monkeypatch):
    winner = "winner-password-12345678901234567890"

    def concurrent(tmp, path):
        path.write_text(winner)
        path.chmod(0o600)
        raise FileExistsError()

    monkeypatch.setattr(serve.os, "link", concurrent)
    assert serve.ensure_password(host.config, serve.detect_container()) == winner
    assert list(serve.password_path().parent.iterdir()) == [serve.password_path()]


def test_password_absolute_state_and_parent_permissions(host, monkeypatch):
    state = serve.password_path().parent.parent
    state.mkdir(parents=True, mode=0o700)
    state.chmod(0o777)
    with pytest.raises(serve.ServeError, match="not writable by others"):
        serve.ensure_password(host.config, serve.detect_container())
    monkeypatch.setenv("XDG_STATE_HOME", "relative")
    with pytest.raises(serve.ServeError, match="absolute"):
        serve.password_path()


@pytest.mark.parametrize(
    "operation",
    [["opencode"], ["claude"], ["--pair", "opencode"], ["--status"], ["--stop", "opencode"]],
)
def test_gate_matrix_one_per_command(host, operation):
    host.inspect["HostConfig"]["PortBindings"] = {}
    host.inspect["Mounts"] = [{"Destination": "/home/dev"}]
    host.inspect["Config"]["Env"] += ["XDG_STATE_HOME=/relocated"]
    host.inspect["HostConfig"]["NetworkMode"] = "host"
    host.engine = "27.5.1"
    host.tailnet = tailscale_config(funnel=True)
    result = runner.invoke(app, ["serve", *operation])
    needs_exposure = operation in (["opencode"], ["--pair", "opencode"])
    assert result.exit_code == (1 if needs_exposure else 0), result.output
    if operation == ["--status"]:
        assert "port not published" in result.output
        assert "Funnel" in result.output


@pytest.mark.parametrize(
    "operation", [["opencode"], ["claude"], ["--pair", "opencode"], ["--status"], ["--stop"]]
)
@pytest.mark.parametrize("state", ["absent", "unknown", "not-detached"])
def test_container_gate_per_command(host, monkeypatch, operation, state):
    if state == "not-detached":
        host.inspect["Config"]["Env"] = ["HOME=/home/dev"]
    else:
        message = (
            "Error response from daemon: No such container: djinn"
            if state == "absent"
            else "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
            "Is the docker daemon running?"
        )
        monkeypatch.setattr(serve, "_run_captured", lambda *a, **k: RunResult(1, stderr=message))
    result = runner.invoke(app, ["serve", *operation])
    no_op = state == "absent" and operation in (["--status"], ["--stop"])
    no_op |= state == "not-detached" and operation in (["--status"], ["--stop"])
    assert result.exit_code == (0 if no_op else 1), result.output
    if state == "unknown":
        assert "Restore Docker access" in result.output
    assert not host.direct


@pytest.mark.parametrize(
    "args,text",
    [
        ([], "Nothing to do. Use:"),
        (["codex"], "Unknown server 'codex'"),
        (["--pair", "claude"], "Pairing is only available for opencode."),
        (["--status", "--stop"], "Choose one mode"),
        (["claude", "--status"], "Choose one mode"),
        (["claude", "--pair", "opencode"], "Choose one mode"),
    ],
)
def test_modes(host, args, text):
    result = runner.invoke(app, ["serve", *args])
    assert result.exit_code == 1
    assert text in result.output
    assert not host.calls and not host.direct


def test_all_names_preflight_and_deduplicated(host):
    host.inspect["HostConfig"]["PortBindings"] = {}
    result = runner.invoke(app, ["serve", "claude", "opencode"])
    assert result.exit_code == 1
    assert not any(argv[1] == "exec" for argv, _ in host.calls)
    host.inspect = inspection()
    assert runner.invoke(app, ["serve", "claude", "claude"]).exit_code == 0
    assert sum(argv[1] == "exec" for argv, _ in host.calls) == 1


def test_stop_not_running_is_success(host):
    result = runner.invoke(app, ["serve", "--stop"])
    assert result.exit_code == 0
    assert "claude: not running" in result.output and "opencode: not running" in result.output
    assert sum(argv[1] == "exec" for argv, _ in host.calls) == 2


@pytest.mark.parametrize(
    "outcome,code,text",
    [
        ("stopped", 0, "opencode: stopped"),
        ("survived", 1, "opencode: could not be stopped (pid 123)"),
    ],
)
def test_stop_results(host, outcome, code, text):
    host.stop_result = outcome
    result = runner.invoke(app, ["serve", "--stop", "opencode"])
    assert result.exit_code == code
    assert text in result.output
    if outcome == "stopped":
        assert "loopback default" in result.output


def test_pair_inherits_output_and_stdin_closed(host):
    host.pair_rc = 7
    result = runner.invoke(app, ["serve", "--pair", "opencode"])
    assert result.exit_code == 7
    argv, kwargs = host.direct[0]
    assert argv == [
        serve.DOCKER_EXECUTABLE,
        "exec",
        CID,
        "opencode",
        "pair",
        "--url",
        "https://box.example.ts.net",
    ]
    assert kwargs == {"stdin": subprocess.DEVNULL, "timeout": 30, "check": False}
    assert "--remote" not in argv


@pytest.mark.parametrize(
    "running,url,text", [(False, True, "opencode is not running"), (True, False, "No tailnet URL:")]
)
def test_pair_preconditions(host, running, url, text):
    host.status = status_reply("running" if running else "stopped")
    if not url:
        host.tailnet = {}
    result = runner.invoke(app, ["serve", "--pair", "opencode"])
    assert result.exit_code == 1
    assert text in result.output
    assert not host.direct


@pytest.mark.parametrize(
    "target", [f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}/", f"http://[::1]:{PORT}/"]
)
def test_tailnet_url_selection(host, target):
    host.tailnet = tailscale_config(target, key="box.example.ts.net:8443")
    host.tailnet["Web"]["a.example.ts.net:443"] = {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}, "/other": {"Proxy": target}}
    }
    host.tailnet["TCP"]["443"] = {"HTTPS": False}
    host.tailnet["Web"]["a-http.example.ts.net:8080"] = {"Handlers": {"/": {"Proxy": target}}}
    host.tailnet["TCP"]["8080"] = {"HTTPS": False}
    host.tailnet["Web"]["a-wrong.example.ts.net:8443"] = {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}
    }
    host.tailnet["Web"]["a-path.example.ts.net:8443"] = {"Handlers": {"/nested": {"Proxy": target}}}
    assert serve.tailnet_state(PORT) == ("https://box.example.ts.net:8443", "", False)
    argv, kwargs = host.calls[-1]
    assert argv == ["/test/bin/tailscale", "serve", "status", "--json"]
    assert kwargs == {"timeout": 5}


@pytest.mark.parametrize("key", ["-x:443", "a b:443", "box\x1b:443", "x/evil:443", "x:999999"])
def test_tailnet_url_rejects_malformed_key(host, key):
    host.tailnet = tailscale_config(key=key)
    assert serve.tailnet_state(PORT)[0] is None


@pytest.mark.parametrize("mode", [["opencode"], ["--pair", "opencode"]])
@pytest.mark.parametrize(
    "target",
    [
        f"http://localhost:{PORT}/other",
        f"http://[::1]:{PORT}/x",
        f"https+insecure://other.example:{PORT}/path",
        f"other.example:{PORT}/path",
    ],
)
def test_funnel_refuses_start_and_pair(host, mode, target):
    host.tailnet["Web"]["other.example.ts.net:8443"] = {"Handlers": {"/sub": {"Proxy": target}}}
    host.tailnet["AllowFunnel"]["other.example.ts.net:8443"] = True
    result = runner.invoke(app, ["serve", *mode])
    assert result.exit_code == 1
    assert "Tailscale Funnel is enabled" in result.output
    assert not host.direct


@pytest.mark.parametrize("mode", [["opencode"], ["--pair", "opencode"], ["--status"]])
def test_funnel_in_foreground_layer_refuses(host, mode):
    host.tailnet["Foreground"] = {
        "session": tailscale_config(key="other.example.ts.net:8443", path="/nested", funnel=True)
    }
    result = runner.invoke(app, ["serve", *mode])
    assert result.exit_code == (0 if mode == ["--status"] else 1)
    assert "Funnel is enabled" in result.output


@pytest.mark.parametrize("flag_in_root", [True, False])
def test_funnel_cross_layer(host, flag_in_root):
    handler = tailscale_config()
    flag = {"AllowFunnel": {"box.example.ts.net:443": True}}
    host.tailnet = flag if flag_in_root else handler
    host.tailnet["Foreground"] = {"session": handler if flag_in_root else flag}
    assert serve.tailnet_state(PORT)[2] is True
    assert runner.invoke(app, ["serve", "opencode"]).exit_code == 1


def test_funnel_tcp_forward_and_nested_foreground(host):
    host.tailnet = {
        "Foreground": {
            "outer": {
                "Foreground": {
                    "inner": {
                        "TCP": {"8443": {"TCPForward": f"other.example:{PORT}"}},
                        "AllowFunnel": {"box.example.ts.net:8443": True},
                    }
                }
            }
        }
    }
    assert serve.tailnet_state(PORT)[2] is True
    assert runner.invoke(app, ["serve", "opencode"]).exit_code == 1


@pytest.mark.parametrize("flag", ["true", 1, 0, None, [], {}])
@pytest.mark.parametrize("on_matching_key", [True, False])
def test_non_boolean_funnel_flag_is_unknown(host, flag, on_matching_key):
    # Tailscale writes booleans; any other flag value leaves the Funnel state unknown.
    config = tailscale_config()
    key = "box.example.ts.net:443" if on_matching_key else "other.example.ts.net:443"
    config["AllowFunnel"] = {key: flag}
    host.tailnet = config
    assert serve.tailnet_state(PORT) == (None, "unreadable output", False)


def test_non_boolean_funnel_flag_keeps_positive_evidence(host):
    config = tailscale_config(funnel=True)
    config["AllowFunnel"]["other.example.ts.net:443"] = "true"
    host.tailnet = config
    assert serve.tailnet_state(PORT) == (None, "unreadable output", True)
    assert runner.invoke(app, ["serve", "opencode"]).exit_code == 1


def test_deeply_nested_tailscale_status_is_unknown(host, monkeypatch):
    nested = "[" * 100000 + "]" * 100000
    monkeypatch.setattr(serve, "_run_captured", lambda *_a, **_k: RunResult(0, nested))
    assert serve.tailnet_state(PORT) == (None, "unreadable output", False)


@pytest.mark.parametrize(
    "anomaly",
    [
        "web-key",
        "web-value",
        "handler",
        "handlers",
        "web",
        "tcp-entry",
        "tcp",
        "foreground",
        "layer",
        "funnel-flags",
    ],
)
@pytest.mark.parametrize("position", ["before", "after", "only", "clean"])
def test_funnel_evidence_survives_unrelated_parse_anomaly(host, anomaly, position):
    def entries(good, bad):
        if position == "clean":
            return good
        if position == "only":
            return bad
        return {**bad, **good} if position == "before" else {**good, **bad}

    config = tailscale_config(funnel=True)
    if anomaly in ("web-key", "web-value"):
        bad = {"weird": {}} if anomaly == "web-key" else {"other.example.ts.net:443": None}
        config["Web"] = entries(config["Web"], bad)
    elif anomaly in ("handler", "handlers"):
        key = "box.example.ts.net:443"
        if anomaly == "handler":
            # Sibling handlers on one key must also be evaluated independently.
            config["Web"][key]["Handlers"] = entries(
                config["Web"][key]["Handlers"], {"/broken": None}
            )
        else:
            config["Web"] = entries(config["Web"], {"other.example.ts.net:443": {"Handlers": None}})
    elif anomaly == "tcp-entry":
        config["TCP"] = entries(config["TCP"], {"1": None})
        if position == "only":
            config["Web"] = {}
    else:
        bad = {
            "web": {"Web": None},
            "tcp": {"TCP": None},
            "foreground": {"Foreground": None},
            "layer": None,
            "funnel-flags": {"AllowFunnel": None},
        }[anomaly]
        config = {"Foreground": entries({"good": config}, {"bad": bad})}
    host.tailnet = config
    evidence = position != "only"
    expected = (
        ("https://box.example.ts.net", "", True)
        if position == "clean"
        else (None, "unreadable output", evidence)
    )
    assert serve.tailnet_state(PORT) == expected
    for mode in (["opencode"], ["--pair", "opencode"]):
        result = runner.invoke(app, ["serve", *mode])
        if evidence:
            assert result.exit_code == 1
            assert "Tailscale Funnel is enabled" in result.output
            assert not host.direct
        elif mode == ["opencode"]:
            assert result.exit_code == 0, result.output
            assert "Funnel state unknown (unreadable output)" in result.output
        else:
            assert result.exit_code == 1
            assert "No tailnet URL: unreadable output" in result.output
            assert len(host.direct) == 1  # Only the earlier, allowed start executed.


@pytest.mark.parametrize("anomaly", ["web", "tcp", "tcp-entry", "flags", "layer"])
@pytest.mark.parametrize("position", ["before", "after"])
def test_funnel_tcp_evidence_survives_malformed_siblings(host, anomaly, position):
    good = {"TCP": {"8443": {"TCPForward": f"other.example:{PORT}"}}}
    bad = {"Web": None} if anomaly == "web" else {"TCP": {"1": None}}
    if anomaly == "flags":
        bad = {"AllowFunnel": None}
    elif anomaly == "layer":
        bad = None
    if anomaly == "tcp-entry":
        forward, invalid = good["TCP"], {"1": None}
        good["TCP"] = {**invalid, **forward} if position == "before" else {**forward, **invalid}
        layers = {"good": good}
    else:
        layers = {"bad": bad, "good": good} if position == "before" else {"good": good, "bad": bad}
    host.tailnet = {"Foreground": layers, "AllowFunnel": {"box.example.ts.net:8443": True}}
    assert serve.tailnet_state(PORT) == (None, "unreadable output", True)
    assert runner.invoke(app, ["serve", "opencode"]).exit_code == 1


def test_funnel_flag_for_other_port_does_not_refuse(host):
    host.tailnet = tailscale_config(target=f"http://127.0.0.1:{PORT + 1}", funnel=True)
    assert serve.tailnet_state(PORT) == (None, f"no Serve entry for http://127.0.0.1:{PORT}", False)
    assert runner.invoke(app, ["serve", "opencode"]).exit_code == 0


def test_tailnet_uses_sorted_candidates_across_layers(host):
    host.tailnet = tailscale_config(key="z.example.ts.net:8443")
    host.tailnet["Foreground"] = {"session": tailscale_config(key="a.example.ts.net:443")}
    assert serve.tailnet_state(PORT) == ("https://a.example.ts.net", "", False)


def test_tailnet_anomaly_discards_readable_url_without_funnel(host):
    host.tailnet["Web"]["weird"] = {}
    assert serve.tailnet_state(PORT) == (None, "unreadable output", False)
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == 0
    assert "Funnel state unknown (unreadable output)" in result.output
    assert "--cors" not in host.direct[-1][0]
    result = runner.invoke(app, ["serve", "--pair", "opencode"])
    assert result.exit_code == 1 and "No tailnet URL: unreadable output" in result.output


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("missing", "tailscale not found on PATH"),
        ("failed", "tailscale serve status failed (exit 1)"),
        ("bad", "unreadable output"),
        ("non-object", "unreadable output"),
        ("empty", f"no Serve entry for http://127.0.0.1:{PORT}"),
    ],
)
def test_tailnet_failures(host, monkeypatch, kind, reason):
    if kind == "missing":
        monkeypatch.setattr(serve.shutil, "which", lambda _: None)
    elif kind in ("failed", "bad", "non-object"):
        monkeypatch.setattr(
            serve,
            "_run_captured",
            lambda *a, **k: RunResult(
                1 if kind == "failed" else 0, "[]" if kind == "non-object" else "broken"
            ),
        )
    else:
        host.tailnet = {}
    assert serve.tailnet_state(PORT) == (None, reason, False)


def test_funnel_unknown_is_noted_and_start_allowed(host, monkeypatch):
    monkeypatch.setattr(serve.shutil, "which", lambda _: None)
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == 0
    assert "Funnel state unknown (tailscale not found on PATH)" in result.output
    assert host.direct[0][0][-3:] == ["opencode-start", "--port", "4096"]


@pytest.mark.parametrize("state", ["running", "exited", "degraded", "stopped"])
def test_snapshot_quick_timeouts_and_optional_tailscale(host, state):
    host.status = status_reply(state)
    snapshot = serve.collect_snapshot(None, quick=True)
    assert snapshot.port == PORT and snapshot.servers["opencode"]["state"] == state
    assert [(argv[1], kw["timeout"]) for argv, kw in host.calls] == (
        [("inspect", 3), ("exec", 5), ("serve", 2)]
        if state == "running"
        else [("inspect", 3), ("exec", 5)]
    )


@pytest.mark.parametrize(
    "outcome,code,text",
    [
        ("started", 0, "opencode: started (pid 123)"),
        ("already-running", 0, "opencode: already running (pid 123)"),
        ("restarted", 0, "opencode: restarted (pid 123)"),
        ("exited", 1, "opencode: exited during startup"),
    ],
)
def test_start_results(host, outcome, code, text):
    host.start_result = outcome
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == code
    assert text in result.output
    if outcome != "exited":
        assert "Local URL: http://127.0.0.1:49380" in result.output
        assert "Tailnet URL: https://box.example.ts.net" in result.output
        assert "Password file:" in result.output
        assert "attached clients must reconnect" in result.output


@pytest.mark.parametrize("code", serve.ERROR_CODES)
def test_helper_error_codes_have_messages(host, code):
    host.error = {
        "code": code,
        "message": "service.json contains 'hostname'; "
        "remove it with: opencode service unset hostname",
    }
    result = runner.invoke(app, ["serve", "claude"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Error:" in result.output
    if code == "busy":
        assert "Another djinn serve start or stop" in result.output
    if code == "timeout":
        assert "Check with: djinn serve --status" in result.output


@pytest.mark.parametrize(
    "reply",
    [
        RunResult(0, ""),
        RunResult(0, "[]"),
        RunResult(0, '{"ok":true}'),
        RunResult(0, '{"ok":1}'),
        RunResult(1, '{"ok":false,"code":"unknown","message":"x"}'),
        RunResult(1, '{"ok":false,"code":"busy","message":1}'),
        RunResult(1, stderr="Error response from daemon: No such container: djinn"),
    ],
)
def test_helper_protocol_failure(host, monkeypatch, reply):
    monkeypatch.setattr(serve, "_run_captured", lambda *a, **k: reply)
    with pytest.raises(serve.ServeError) as caught:
        serve.exec_helper(CID, ["status"])
    assert caught.value.code == "protocol"


@pytest.mark.parametrize(
    "field,value",
    [("state", "unknown"), ("pid", True), ("reason", 3), ("last_line", []), ("listening", "true")],
)
def test_helper_rejects_status_schema(host, field, value):
    host.status["servers"]["opencode"][field] = value
    with pytest.raises(serve.ServeError) as caught:
        serve.exec_helper(CID, ["status"])
    assert caught.value.code == "protocol"


@pytest.mark.parametrize(
    "field,value",
    [("relay_refused", value) for value in (True, "0", 1.5, [], {})]
    + [("relay_note", value) for value in (True, 0, [], {})],
)
def test_helper_rejects_relay_status_schema(host, field, value):
    host.status["servers"]["opencode"][field] = value
    with pytest.raises(serve.ServeError) as caught:
        serve.exec_helper(CID, ["status"])
    assert caught.value.code == "protocol"


@pytest.mark.parametrize("field", ["relay_refused", "relay_note"])
def test_helper_requires_relay_status_fields(host, field):
    del host.status["servers"]["opencode"][field]
    with pytest.raises(serve.ServeError) as caught:
        serve.exec_helper(CID, ["status"])
    assert caught.value.code == "protocol"


def test_route_localnet_helper_failure_message(host, monkeypatch):
    # Integration adds the error to the frozen enums; fake its decoded reply here.
    reply = {"ok": False, "code": "route-localnet", "message": "unsafe [red]sysctl\x1b[31m"}
    monkeypatch.setattr(
        serve,
        "exec_helper",
        MagicMock(side_effect=serve.ServeError(reply["message"], code=reply["code"])),
    )
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == 1
    assert (
        "OpenCode requires route_localnet=0 on all container interfaces; refusing." in result.output
    )
    assert "Disable loopback routing in the container and endpoint sysctls" in result.output
    assert "new containers inherit its all/default values" in result.output
    assert "then recreate the container" in result.output
    assert "unsafe" not in result.output


@pytest.mark.parametrize(
    "args,stdin", [(["status"], "secret"), (["opencode-start"], None), (["unknown"], None)]
)
def test_exec_input_restricted(host, args, stdin):
    with pytest.raises(serve.ServeError):
        serve.exec_helper(CID, args, stdin=stdin)
    assert not host.calls and not host.direct


def test_exec_requires_full_container_id(host):
    with pytest.raises(serve.ServeError):
        serve.exec_helper("djinn", ["status"])
    assert not host.calls


@pytest.mark.parametrize("password_input", [False, True])
def test_helper_timeout_message(host, monkeypatch, password_input):
    if password_input:
        monkeypatch.setattr(
            serve.subprocess, "run", MagicMock(side_effect=subprocess.TimeoutExpired("docker", 60))
        )
        args = ["opencode-start"]
    else:
        monkeypatch.setattr(serve, "_run_captured", lambda *a, **k: RunResult(124))
        args = ["status"]
    with pytest.raises(serve.ServeError) as caught:
        serve.exec_helper(CID, args, stdin='{"password":"x"}' if password_input else None)
    command._helper_failure("opencode", caught.value)
    assert caught.value.code == "exec-timeout"


@pytest.mark.parametrize(
    "payload", ["[/x]", "[link=https://example.com]literal[/link]", "\x1b[31mred"]
)
@pytest.mark.parametrize(
    "site",
    [
        "unknown-name",
        "gate",
        "helper-message",
        "helper-stderr",
        "log-tail",
        "service-key",
        "service-message",
        "state-path",
        "tailnet-url",
        "tailnet-reason",
        "pair-reason",
        "password-path",
        "status-last-line",
        "status-reason",
        "status-url",
        "status-tailnet-reason",
        "status-relay-note",
    ],
)
def test_untrusted_text_is_printed_literally(host, monkeypatch, payload, site):
    args = ["serve", "opencode"]
    if site == "unknown-name":
        args = ["serve", payload]
    elif site == "gate":
        monkeypatch.setattr(
            serve, "check_opencode_roots", MagicMock(side_effect=serve.ServeError(payload))
        )
    elif site in (
        "helper-message",
        "helper-stderr",
        "state-path",
        "service-key",
        "service-message",
    ):
        failure = serve.ServeError(payload, code="spawn")
        if site == "helper-stderr":
            failure = serve.ServeError("helper failed", code="protocol", detail=payload)
        elif site == "state-path":
            failure = serve.ServeError(payload, code="state-path")
        elif site.startswith("service-"):
            message = f"service.json contains '{payload}'" if site == "service-key" else payload
            failure = serve.ServeError(message, code="service-config")
        monkeypatch.setattr(serve, "exec_helper", MagicMock(side_effect=failure))
    elif site == "log-tail":
        monkeypatch.setattr(
            serve,
            "exec_helper",
            lambda *a, **k: {"result": "exited", "pid": None, "log_tail": [payload]},
        )
    elif site == "password-path":
        payload = str(Path(payload))
        monkeypatch.setattr(serve, "password_path", lambda: Path(payload))
        monkeypatch.setattr(serve, "ensure_password", lambda *_: "x" * 40)
    elif site in ("tailnet-url", "tailnet-reason", "pair-reason"):
        monkeypatch.setattr(
            serve,
            "tailnet_state",
            lambda *a, **k: (payload, "", False)
            if site == "tailnet-url"
            else (None, payload, False),
        )
        if site == "pair-reason":
            args = ["serve", "--pair", "opencode"]
    else:
        args = ["serve", "--status"]
        row = host.status["servers"]["opencode"]
        if site == "status-last-line":
            row.update(state="exited", last_line=payload)
        elif site == "status-reason":
            row.update(state="degraded", reason=payload)
        elif site == "status-relay-note":
            row.update(relay_refused=None, relay_note=payload)
        else:
            monkeypatch.setattr(
                serve,
                "tailnet_state",
                lambda *a, **k: (payload, "", False)
                if site == "status-url"
                else (None, payload, False),
            )
    result = runner.invoke(app, args)
    assert not result.exception or isinstance(result.exception, SystemExit), result.exception
    from djinn_in_a_box.core.console import escape_nonprintable

    assert escape_nonprintable(payload) in result.output, result.output
    assert "\x1b" not in result.output


@pytest.mark.parametrize("width", [80, 200])
def test_status_endpoint_lines_render_as_lines(host, monkeypatch, width):
    forged = "relay refused since start: 9"
    monkeypatch.setattr(serve, "tailnet_state", lambda *a, **k: (None, "x\n" + forged, False))
    monkeypatch.setenv("COLUMNS", str(width))
    host.status["servers"]["opencode"].update(relay_refused=3, relay_note=None)
    result = runner.invoke(app, ["serve", "--status"])
    assert result.exit_code == 0, result.output
    rows = [line.split("│") for line in result.output.splitlines() if line.count("│") == 6]
    cells = [row[4].strip() for row in rows]
    assert any(cell.startswith("tailnet: unknown") for cell in cells), result.output
    assert any(cell.startswith("relay refused since start: 3") for cell in cells), result.output
    # A newline inside a value stays escaped instead of forging a status line.
    assert forged not in cells, result.output


def test_status_table_uses_text_cells(host, monkeypatch):
    from rich.text import Text

    printed = []
    monkeypatch.setattr(command.console, "print", lambda table: printed.append(table))
    assert runner.invoke(app, ["serve", "--status"]).exit_code == 0
    assert all(isinstance(cell, Text) for column in printed[0].columns for cell in column._cells)


def test_status_busy_and_degraded(host):
    host.status["busy"] = True
    host.status["servers"]["opencode"].update(
        state="degraded", reason="not registered", foreign_listener=True
    )
    result = runner.invoke(app, ["serve", "--status"])
    assert result.exit_code == 0
    assert "busy (start/stop in progress)" in result.output
    assert "degraded (not registered)" in result.output
    assert "port 4096 is used by another process" in result.output
    assert "a djinn serve from before the update — recreate the container" in result.output


@pytest.mark.parametrize(
    "count,note,expected",
    [(0, None, "relay refused since start: 0"), (27, None, "relay refused since start: 27")]
    + [
        (None, note, f"relay refused: unknown ({note})")
        for note in ("missing", "unreadable", "foreign identity", "stale")
    ],
)
def test_status_relay_count_or_unknown_note(host, monkeypatch, count, note, expected):
    host.status["servers"]["opencode"].update(relay_refused=count, relay_note=note)
    printed = []
    monkeypatch.setattr(command.console, "print", lambda table: printed.append(table))
    result = runner.invoke(app, ["serve", "--status"])
    assert result.exit_code == 0, result.output
    endpoints = [cell.plain for cell in printed[0].columns[3]._cells]
    assert expected in endpoints[1]
    assert "relay refused" not in endpoints[0]
    if count is None:
        assert "relay refused since start: 0" not in endpoints[1]


@pytest.mark.parametrize(
    "state,shown",
    [("running", True), ("degraded", True), ("stopped", False), ("exited", False)],
)
def test_status_relay_line_only_while_a_relay_is_expected(host, monkeypatch, state, shown):
    host.status["servers"]["opencode"].update(
        state=state, reason="relay gone", relay_refused=None, relay_note="foreign identity"
    )
    printed = []
    monkeypatch.setattr(command.console, "print", lambda table: printed.append(table))
    assert runner.invoke(app, ["serve", "--status"]).exit_code == 0
    endpoint = [cell.plain for cell in printed[0].columns[3]._cells][1]
    assert ("relay refused" in endpoint) is shown, endpoint


@pytest.mark.parametrize("reason", ["relay gone", "relay listener lost", "opencode gone"])
def test_status_new_degraded_reasons_are_text(host, reason):
    host.status["servers"]["opencode"].update(state="degraded", reason=reason)
    result = runner.invoke(app, ["serve", "--status"])
    assert result.exit_code == 0, result.output
    assert f"degraded ({reason})" in result.output


@pytest.mark.parametrize(
    "part,value",
    [
        ("Mounts", None),
        ("Mounts", [{}]),
        ("NetworkSettings", {}),
        ("NetworkSettings", {"Networks": {}}),
    ],
)
def test_undeterminable_exposure_metadata_refused(host, part, value):
    host.inspect[part] = value
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == 1
    assert not host.direct


@pytest.mark.parametrize(
    "command_args,reply",
    [
        (["stop", "--name", "claude"], {"ok": True, "results": {}}),
        (
            ["stop", "--name", "claude"],
            {"ok": True, "results": {"claude": {"result": "unknown", "pid": None}}},
        ),
        (
            ["claude-start"],
            {
                "ok": True,
                "name": "opencode",
                "result": "started",
                "pid": 3,
                "since": None,
                "log_tail": [],
            },
        ),
        (
            ["claude-start"],
            {
                "ok": True,
                "name": "claude",
                "result": "unknown",
                "pid": 3,
                "since": None,
                "log_tail": [],
            },
        ),
        (
            ["claude-start"],
            {
                "ok": True,
                "name": "claude",
                "result": "started",
                "pid": 3,
                "since": None,
                "log_tail": [3],
            },
        ),
    ],
)
def test_helper_rejects_start_stop_schema(host, monkeypatch, command_args, reply):
    monkeypatch.setattr(serve, "_run_captured", lambda *a, **k: RunResult(0, json.dumps(reply)))
    with pytest.raises(serve.ServeError) as caught:
        serve.exec_helper(CID, command_args)
    assert caught.value.code == "protocol"


def test_helper_source_size_limit(monkeypatch):
    serve.helper_source.cache_clear()
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: "x" * 100_001)
    try:
        with pytest.raises(serve.ServeError, match="size limit"):
            serve.helper_source()
    finally:
        serve.helper_source.cache_clear()


def test_password_parent_leaf_must_be_private(host):
    path = serve.password_path().parent
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o755)
    with pytest.raises(serve.ServeError, match="private OpenCode password"):
        serve.ensure_password(host.config, serve.detect_container())


def test_pair_failure_is_a_refusal(host, monkeypatch):
    monkeypatch.setattr(serve.subprocess, "run", MagicMock(side_effect=FileNotFoundError()))
    result = runner.invoke(app, ["serve", "--pair", "opencode"])
    assert result.exit_code == 1
    assert "opencode: pairing failed" in result.output


def test_home_comparison_does_not_resolve_symlinks(host, monkeypatch):
    host.inspect["Config"]["Env"] = ["DJINN_DETACHED=true", "HOME=/relocated"]
    monkeypatch.setattr(serve.os.path, "realpath", lambda *a, **k: "/home/dev")
    with pytest.raises(serve.ServeError, match="environment relocates"):
        serve.check_opencode_roots(serve.detect_container())


def test_helper_source_read_failure_is_serve_error(monkeypatch):
    serve.helper_source.cache_clear()
    monkeypatch.setattr(Path, "read_text", MagicMock(side_effect=FileNotFoundError()))
    try:
        with pytest.raises(serve.ServeError, match="Could not read the serve helper"):
            serve.helper_source()
    finally:
        serve.helper_source.cache_clear()


def test_helper_symlink_refusal_message(host):
    host.error = {
        "code": "state-path",
        "message": "OpenCode's settings path contains a symbolic link: /home/dev/.config",
    }
    result = runner.invoke(app, ["serve", "opencode"])
    assert result.exit_code == 1
    assert (
        "OpenCode's settings path contains a symbolic link (/home/dev/.config); refusing."
        in result.output
    )


def test_password_is_synced_before_publication(host, monkeypatch):
    events = []
    real_sync, real_link = serve.os.fsync, serve.os.link

    def synced(fd):
        events.append("fsync")
        real_sync(fd)

    def linked(source, target):
        events.append("link")
        return real_link(source, target)

    monkeypatch.setattr(serve.os, "fsync", synced)
    monkeypatch.setattr(serve.os, "link", linked)
    serve.ensure_password(host.config, serve.detect_container())
    assert events == ["fsync", "link"]


def test_helper_failure_diagnostics_are_bounded(host, monkeypatch):
    monkeypatch.setattr(serve, "_run_captured", lambda *a, **k: RunResult(1, stderr="[/x]" * 100))
    with pytest.raises(serve.ServeError) as caught:
        serve.exec_helper(CID, ["status"])
    assert caught.value.detail == "[/x]" * 50


@pytest.mark.parametrize("root_name", ["config", "local", "shared", "backups", "bind"])
@pytest.mark.parametrize("operation", ["create", "read"])
def test_password_excluded_roots_refused_before_io(
    host, monkeypatch, tmp_path, root_name, operation
):
    roots = {name: tmp_path / name for name in ("config", "local", "shared", "backups", "bind")}
    host.config = AppConfig(
        code_dir=tmp_path,
        config_root=roots["config"],
        local_root=roots["local"],
        shared_root=roots["shared"],
    )
    monkeypatch.setattr(serve, "BACKUPS_DIR", roots["backups"])
    host.inspect["Mounts"] = [
        {"Type": "bind", "Source": str(roots["bind"]), "Destination": "/data"}
    ]
    monkeypatch.setenv("XDG_STATE_HOME", str(roots[root_name] / "state"))
    path = serve.password_path()
    if operation == "read":
        path.parent.mkdir(parents=True)
        path.write_text("x" * 40)
        path.chmod(0o600)
    opened = []
    monkeypatch.setattr(serve.os, "open", lambda *a, **k: opened.append(a))
    with pytest.raises(serve.ServeError, match="inside a config, zone, backup, bind source"):
        if operation == "read":
            serve.read_password(host.config, serve.detect_container())
        else:
            serve.ensure_password(host.config, serve.detect_container())
    assert not opened
    assert path.exists() == (operation == "read")


@pytest.fixture(params=["create", "read"])
def password_io(host, monkeypatch, request):
    path = serve.password_path()
    if request.param == "read":
        path.parent.mkdir(parents=True)
        path.write_text("x" * 40)
        path.chmod(0o600)
    opened = MagicMock(wraps=os.open)
    monkeypatch.setattr(serve.os, "open", opened)

    def access():
        container = serve.detect_container()
        if request.param == "read":
            return serve.read_password(host.config, container)
        return serve.ensure_password(host.config, container)

    def assert_untouched():
        opened.assert_not_called()
        if request.param == "read":
            assert path.read_text() == "x" * 40
            assert list(path.parent.iterdir()) == [path]
        else:
            assert not path.parent.parent.exists()

    return SimpleNamespace(access=access, assert_untouched=assert_untouched)


@pytest.mark.parametrize("storage", ["source", "bind", "rbind"])
@pytest.mark.parametrize("symlink_root", [False, True], ids=["direct", "resolved"])
def test_password_volume_storage_refused_before_io(
    host, password_io, tmp_path, storage, symlink_root
):
    root = serve.password_path().parents[2]
    if symlink_root:
        alias = tmp_path / "volume-alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    mount = host.inspect["Mounts"][0]
    if storage == "source":
        mount["Source"] = str(root)
        host.volume["Mountpoint"] = str(root)
    else:
        host.volume["Options"] = {"type": "none", "o": storage, "device": str(root)}
    with pytest.raises(serve.ServeError, match="inside a config.*volume host storage"):
        password_io.access()
    password_io.assert_untouched()


@pytest.mark.parametrize("options", [None, {}], ids=["null-options", "empty-options"])
@pytest.mark.parametrize("source", ["mountpoint", "", None, 42])
def test_password_plain_local_volume_accepted(host, password_io, options, source):
    host.volume["Options"] = options
    host.inspect["Mounts"][0]["Source"] = (
        host.volume["Mountpoint"] if source == "mountpoint" else source
    )
    assert len(password_io.access()) >= 40
    assert host.volume_calls
    for argv, kwargs in host.volume_calls:
        assert argv[1:] == ["volume", "inspect", "djinn-opencode-data"]
        assert kwargs["timeout"] == 5
        assert kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("storage", ["source", "bind", "rbind"])
def test_password_volume_storage_must_be_absolute(host, password_io, storage):
    if storage == "source":
        host.inspect["Mounts"][0]["Source"] = "relative-storage"
    else:
        host.volume["Options"] = {"type": "none", "o": storage, "device": "relative-storage"}
    with pytest.raises(serve.ServeError, match="paths must be absolute; refusing"):
        password_io.access()
    password_io.assert_untouched()


@pytest.mark.parametrize(
    "driver,options",
    [
        ("remote", None),
        ("local", {"type": "nfs", "o": "addr=192.0.2.1,rw", "device": ":/data"}),
        ("local", {"type": "none", "o": "rw", "device": "/data"}),
        ("local", {"type": "none", "o": "bind"}),
    ],
    ids=["non-local", "nfs-options", "unknown-options", "malformed-options"],
)
def test_password_unknown_volume_storage_refused(host, password_io, driver, options):
    host.volume.update(Driver=driver, Options=options)
    with pytest.raises(
        serve.ServeError,
        match="^Could not determine a password volume's host storage; refusing\\.$",
    ):
        password_io.access()
    password_io.assert_untouched()


@pytest.mark.parametrize(
    "failure", ["missing-volume", "daemon-unavailable", "invalid-json", "timeout", "missing-binary"]
)
def test_password_volume_inspection_failure_refused(host, password_io, monkeypatch, failure):
    def failed(argv, **kwargs):
        assert argv[1:] == ["volume", "inspect", "djinn-opencode-data"]
        assert kwargs["timeout"] == 5
        assert kwargs["stdin"] == subprocess.DEVNULL
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == "missing-binary":
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        if failure == "missing-volume":
            return subprocess.CompletedProcess(
                argv, 1, "", "Error response from daemon: get djinn-opencode-data: no such volume"
            )
        if failure == "daemon-unavailable":
            return subprocess.CompletedProcess(
                argv,
                1,
                "",
                "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                "Is the docker daemon running?",
            )
        return subprocess.CompletedProcess(argv, 0, "not-json", "")

    monkeypatch.setattr(serve.subprocess, "run", failed)
    with pytest.raises(
        serve.ServeError,
        match="^Could not determine a password volume's host storage; refusing\\.$",
    ):
        password_io.access()
    password_io.assert_untouched()


@pytest.mark.parametrize(
    "exception", [ValueError, KeyError, OSError, subprocess.SubprocessError, serve.GitSSHError]
)
def test_password_volume_inspection_exceptions_mapped(host, password_io, monkeypatch, exception):
    monkeypatch.setattr(
        host_runtime, "inspect_object", MagicMock(side_effect=exception("volume inspection failed"))
    )
    with pytest.raises(
        serve.ServeError,
        match="^Could not determine a password volume's host storage; refusing\\.$",
    ):
        password_io.access()
    password_io.assert_untouched()


@pytest.mark.parametrize("name", ["missing", None, "", 42])
def test_password_volume_missing_name_refused(host, password_io, name):
    if name == "missing":
        del host.inspect["Mounts"][0]["Name"]
    else:
        host.inspect["Mounts"][0]["Name"] = name
    with pytest.raises(
        serve.ServeError,
        match="^Could not determine a password volume's host storage; refusing\\.$",
    ):
        password_io.access()
    password_io.assert_untouched()
    assert not host.volume_calls


def test_password_tmpfs_has_no_host_storage(host, password_io):
    host.inspect["Mounts"] = [
        {"Type": "tmpfs", "Source": str(serve.password_path().parents[2]), "Destination": "/data"}
    ]
    assert len(password_io.access()) >= 40
    assert not host.volume_calls


@pytest.mark.parametrize("component", ["state", "djinn", "serve", "opencode-password"])
def test_password_symlink_component_refused(host, tmp_path, component):
    path = serve.password_path()
    link = (
        path
        if component == "opencode-password"
        else next(p for p in path.parents if p.name == component)
    )
    link.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "target"
    target.mkdir()
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(serve.ServeError, match="symbolic link"):
        serve.ensure_password(host.config, serve.detect_container())
    assert path.is_symlink() if component == "opencode-password" else not path.exists()


@pytest.mark.parametrize(
    "mounts",
    [
        None,
        [None],
        [{}],
        [{"Type": "unknown"}],
        [{"Type": "bind"}],
        [{"Type": "bind", "Source": 1}],
        [{"Type": "bind", "Source": "relative"}],
    ],
)
def test_password_unknown_bind_sources_refused(host, mounts):
    host.inspect["Mounts"] = mounts
    with pytest.raises(serve.ServeError, match="refusing"):
        serve.ensure_password(host.config, serve.detect_container())
    assert not serve.password_path().exists()


@pytest.mark.parametrize("exception", [PermissionError, ValueError])
def test_password_uninspectable_component_refused(host, monkeypatch, exception):
    path = serve.password_path()
    blocked = path.parent
    real = Path.lstat

    def refusing(self, *args, **kwargs):
        if self == blocked:
            raise exception("cannot inspect")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", refusing)
    # The untraversable-root exception never applies to the password path itself.
    with pytest.raises(serve.ServeError, match="Could not inspect the password location"):
        serve.ensure_password(host.config, serve.detect_container())
    assert not path.exists()


@pytest.mark.parametrize(
    "via,target,refused",
    [
        ("bind", "dotfile", False),
        ("bind", "elsewhere", False),
        ("bind", "state", True),
        ("bind", "state/djinn", True),
        ("config-env", "elsewhere", False),
        ("config-env", "state", True),
    ],
)
def test_password_symlinked_excluded_roots_are_resolved(
    host, monkeypatch, tmp_path, via, target, refused
):
    # Djinn binds dotfiles such as ~/.zshrc; a symlinked root counts where it really lies.
    destination = tmp_path / target
    if target == "dotfile":
        destination.write_text("")
    else:
        destination.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(destination)
    if via == "bind":
        host.inspect["Mounts"] = [{"Type": "bind", "Source": str(alias), "Destination": "/data"}]
    else:
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(alias))
    if refused:
        with pytest.raises(serve.ServeError, match="inside a config"):
            serve.ensure_password(host.config, serve.detect_container())
        assert not serve.password_path().exists()
    else:
        assert len(serve.ensure_password(host.config, serve.detect_container())) >= 40


@pytest.mark.parametrize("kind", ["loop", "nul"])
def test_password_unresolvable_excluded_root_refused(host, tmp_path, kind):
    source = tmp_path / "loop"
    if kind == "loop":
        source.symlink_to(source)
    host.inspect["Mounts"] = [
        {"Type": "bind", "Source": f"{source}\0" if kind == "nul" else str(source)}
    ]
    with pytest.raises(serve.ServeError, match="Could not inspect an excluded password root"):
        serve.ensure_password(host.config, serve.detect_container())
    assert not serve.password_path().exists()


def _untraversable(monkeypatch, prefix, code=errno.EACCES):
    # Rootful Docker's data directory is not traversable for the user: resolving it raises EACCES.
    real = os.path.realpath

    def realpath(path, *args, **kwargs):
        if str(path).startswith(str(prefix)):
            raise OSError(code, os.strerror(code), str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(serve.os.path, "realpath", realpath)


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM])
@pytest.mark.parametrize("kind", ["volume", "bind"])
def test_password_untraversable_root_counts_lexically(host, monkeypatch, tmp_path, kind, code):
    data = tmp_path / "docker-data"
    mount = {"Type": kind, "Source": str(data / "volumes/data/_data"), "Destination": "/data"}
    if kind == "volume":
        mount["Name"] = "data"
        monkeypatch.setattr(serve, "volume_bind_source", lambda _mount: None)
    host.inspect["Mounts"] = [mount]
    _untraversable(monkeypatch, data, code)
    assert len(serve.ensure_password(host.config, serve.detect_container())) >= 40


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOTDIR, errno.ENAMETOOLONG])
def test_password_other_root_resolution_errors_refused(host, monkeypatch, tmp_path, code):
    data = tmp_path / "docker-data"
    host.inspect["Mounts"] = [{"Type": "bind", "Source": str(data), "Destination": "/data"}]
    _untraversable(monkeypatch, data, code)
    with pytest.raises(serve.ServeError, match="Could not inspect an excluded password root"):
        serve.ensure_password(host.config, serve.detect_container())
    assert not serve.password_path().exists()


def test_password_lexically_beneath_untraversable_root_refused(host, monkeypatch, tmp_path):
    state = tmp_path / "state"
    host.inspect["Mounts"] = [{"Type": "bind", "Source": str(state), "Destination": "/data"}]
    _untraversable(monkeypatch, state)
    with pytest.raises(serve.ServeError, match="inside a config, zone, backup"):
        serve.ensure_password(host.config, serve.detect_container())
    assert not serve.password_path().exists()


def test_password_safe_sibling_and_nofollow_walk(host, monkeypatch, tmp_path):
    root = tmp_path / "state"
    host.config = AppConfig(code_dir=tmp_path, config_root=root)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-local"))
    assert len(serve.ensure_password(host.config, serve.detect_container())) >= 40
    # An alias must be inspected before '..' removes it from the lexical path.
    (tmp_path / "alias").symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "alias" / ".." / "other-state"))
    with pytest.raises(serve.ServeError, match="symbolic link"):
        serve.ensure_password(host.config, serve.detect_container())


@pytest.mark.parametrize("names", [["claude", "opencode"], ["opencode", "claude"]])
@pytest.mark.parametrize("code", ["env-root", "state-path", "remote-config", "service-config"])
def test_all_names_helper_preflight_before_password_or_start(host, names, code):
    host.error = {"code": code, "message": "preflight refusal"}
    result = runner.invoke(app, ["serve", *names])
    assert result.exit_code == 1
    calls = [a[a.index("-c") + 2 :] for a, _ in host.calls if a[1] == "exec"]
    assert calls == [["opencode-preflight"]]
    assert not host.direct
    assert not serve.password_path().exists()


@pytest.mark.parametrize("names", [["claude", "opencode"], ["opencode", "claude"]])
@pytest.mark.parametrize("failure", ["error", "exited"])
def test_start_stops_at_first_failure(host, monkeypatch, names, failure):
    calls = []

    def helper(_cid, args, **_kwargs):
        calls.append(args[0])
        if args[0] == "opencode-preflight":
            return {"ok": True}
        if failure == "error":
            raise serve.ServeError("cannot spawn", code="spawn")
        return {"result": "exited", "pid": None, "log_tail": []}

    monkeypatch.setattr(serve, "exec_helper", helper)
    result = runner.invoke(app, ["serve", *names])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert calls == ["opencode-preflight", f"{names[0]}-start"]


@pytest.mark.parametrize("reply", [{"ok": True, "extra": 1}, {"ok": True, "result": "started"}])
def test_helper_rejects_preflight_schema(host, monkeypatch, reply):
    monkeypatch.setattr(serve, "_run_captured", lambda *a, **k: RunResult(0, json.dumps(reply)))
    with pytest.raises(serve.ServeError, match="helper failed"):
        serve.exec_helper(CID, ["opencode-preflight"])


def test_password_double_slash_cannot_bypass_excluded_root(host, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/" + str(host.config.config_root / "state"))
    with pytest.raises(serve.ServeError, match="inside a config"):
        serve.ensure_password(host.config, serve.detect_container())
    assert not serve.password_path().exists()


def test_password_unknown_container_refused(host):
    container = serve.ContainerInfo(serve.ContainerState.UNKNOWN, mounts=[])
    with pytest.raises(serve.ServeError, match="Could not inspect password bind sources"):
        serve.ensure_password(host.config, container)
    assert not serve.password_path().exists()


def test_password_unknown_zone_roots_refused(host, monkeypatch):
    from djinn_in_a_box.core.exceptions import ZoneConfigurationError

    def unknown(_config):
        raise ZoneConfigurationError("cannot determine roots")

    monkeypatch.setattr(serve, "resolve_zone_roots", unknown)
    with pytest.raises(serve.ServeError, match="Could not determine excluded password roots"):
        serve.ensure_password(host.config, serve.detect_container())
    assert not serve.password_path().exists()


def test_password_config_root_environment_precedence(host, monkeypatch, tmp_path):
    root = tmp_path / "relocated-config"
    monkeypatch.setenv("DJINN_CONFIG_ROOT", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    with pytest.raises(serve.ServeError, match="inside a config"):
        serve.ensure_password(host.config, serve.detect_container())
    assert not serve.password_path().exists()
