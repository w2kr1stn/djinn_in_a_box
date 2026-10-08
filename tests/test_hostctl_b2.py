from __future__ import annotations

import base64
import copy
import importlib.util
import json
import socket
import struct
import subprocess
import threading
from pathlib import Path

import pytest

from djinn_in_a_box.commands.doctor import Status, hostctl_checks
from djinn_in_a_box.config.ssh import GitConfig, HostctlConfig
from djinn_in_a_box.core import host_runtime, hostctl, ssh_delivery

ROOT = Path(__file__).resolve().parents[1]
KEY = (
    "ssh-ed25519 "
    + base64.b64encode(
        struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + b"a" * 32
    ).decode()
)


def config(address="host-a.example.ts.net"):
    return HostctlConfig(hosts={"host-a": {"address": address, "user": "operator"}})


def status():
    return {
        "Version": "1.102.5",
        "BackendState": "Running",
        "Self": {"HostName": "djinn-machine"},
        "Peer": {
            "nodekey:" + "a" * 64: {
                "ID": "peer-a",
                "PublicKey": "nodekey:" + "a" * 64,
                "HostName": "host-a",
                "DNSName": "host-a.example.ts.net.",
                "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"],
                "sshHostKeys": [KEY],
            }
        },
    }


@pytest.mark.parametrize(
    "address", ["host-a", "host-a.example.ts.net", "100.64.0.1", "fd7a:115c:a1e0::1"]
)
def test_authenticated_resolution(address):
    # Mutation: read the Go field name SSH_HostKeys instead of its JSON wire tag.
    value = ssh_delivery.peer_snapshot(config(address), status(), "generation-a")
    peer = value["peers"]["host-a"]
    assert peer["keys"] == [KEY]
    assert peer["ip"] == "100.64.0.1" and len(peer["ips"]) == 2
    assert peer["host_key_alias"] == "host-a.example.ts.net"
    assert value["routes"][address] == "100.64.0.1"


def test_ambiguous_or_missing_peer_refuses():
    # Mutation: accept the first match, including a duplicate declaration address.
    node = status()
    node["Peer"]["nodekey:" + "b" * 64] = copy.deepcopy(next(iter(node["Peer"].values())))
    with pytest.raises(ssh_delivery.GitSSHError, match="exactly one"):
        ssh_delivery.peer_snapshot(config(), node, "generation-a")
    with pytest.raises(ssh_delivery.GitSSHError, match="exactly one"):
        ssh_delivery.peer_snapshot(config("absent"), status(), "generation-a")


def test_missing_or_invalid_keys_refuse():
    # Mutation: replace all host key validation with a fallback placeholder key.
    for keys in [None, [], [""], ["ssh-ed25519 invalid"], [42]]:
        node = status()
        next(iter(node["Peer"].values()))["sshHostKeys"] = keys
        with pytest.raises(ssh_delivery.GitSSHError):
            ssh_delivery.peer_snapshot(config(), node, "generation-a")


def test_non_tailnet_or_unenrolled_refuses():
    # Mutation: trust LAN addresses or ignore enrollment state.
    node = status()
    next(iter(node["Peer"].values()))["TailscaleIPs"] = ["192.168.1.10"]
    with pytest.raises(ssh_delivery.GitSSHError):
        ssh_delivery.peer_snapshot(config(), node, "generation-a")
    node = status()
    node["BackendState"] = "NeedsLogin"
    with pytest.raises(ssh_delivery.GitSSHError):
        ssh_delivery.peer_snapshot(config(), node, "generation-a")


def ssh_options(tmp_path, files, alias):
    for name, value in files.items():
        (tmp_path / name).write_text(value)
    # ssh -G validates the actual option grammar; only the absolute Include is relocated.
    path = tmp_path / "config"
    path.write_text(
        path.read_text().replace("/home/dev/.ssh/tailnet_config", str(tmp_path / "tailnet_config"))
    )
    output = subprocess.run(
        ["ssh", "-G", "-F", str(path), alias], capture_output=True, text=True, check=True
    )
    return dict(line.split(" ", 1) for line in output.stdout.splitlines())


@pytest.mark.parametrize("ready", [False, True])
def test_alias_options_closed_and_snapshot(tmp_path, ready):
    # Mutation: enable forwarding (or omit aliases when there is no current grant).
    trust = ssh_delivery.peer_snapshot(config(), status(), "generation-a") if ready else None
    delivery = ssh_delivery.with_tailnet(
        ssh_delivery.read_public_delivery(GitConfig()), config(), trust
    )
    options = ssh_options(tmp_path, delivery.files, "host-a")
    assert options["hostname"] == ("100.64.0.1" if ready else "host-a.example.ts.net")
    assert options["hostkeyalias"] == "host-a.example.ts.net"
    assert options["user"] == "operator"
    assert options["proxycommand"] == "/usr/local/bin/djinn-hostctl-connect %h %p"
    assert options["stricthostkeychecking"] == "true"
    assert options["updatehostkeys"] == "false"
    assert options["userknownhostsfile"] == "/home/dev/.ssh/tailnet_known_hosts"
    assert options["globalknownhostsfile"] == "/dev/null"
    for name in [
        "forwardagent",
        "forwardx11",
        "forwardx11trusted",
        "permitlocalcommand",
        "pubkeyauthentication",
        "controlpersist",
    ]:
        assert options[name] in ("no", "false")
    assert options["controlmaster"] == "false"
    assert options["clearallforwardings"] == "yes"
    assert options["identityagent"] == "none" and options["identityfile"] == "none"
    assert bool(delivery.files["tailnet_known_hosts"]) is ready


def test_public_refresh_preserves_git(tmp_path):
    # Mutation: prune the shared .ssh directory while refreshing tailnet trust.
    public = tmp_path / "public"
    original = ssh_delivery.PublicDelivery(
        {"config": "Host git-example\n User git\n", "git.json": "{}", "known_hosts": "Git trust"},
        frozenset(),
    )
    combined = ssh_delivery.with_tailnet(original, config(), None)
    ssh_delivery.write_public_delivery(public, combined)
    inode = public.stat().st_ino
    trust = ssh_delivery.peer_snapshot(config(), status(), "generation-a")
    ssh_delivery.write_public_files(public, ssh_delivery.tailnet_files(config(), trust))
    assert public.stat().st_ino == inode
    assert (public / "known_hosts").read_text() == "Git trust"
    assert (public / "config").read_text().endswith(original.files["config"])
    assert (public / "git.json").read_text() == "{}"
    assert KEY in (public / "tailnet_known_hosts").read_text()


def test_host_only_runtime_mounts():
    # Mutation: skip public delivery when Git identities are absent.
    runtime = host_runtime.GitRuntime(Path("/public-runtime"), "generation-a", git_enabled=False)
    fragment = {"services": {"dev": {}}}
    runtime.add_to_fragment(fragment)
    service = fragment["services"]["dev"]
    assert [row["target"] for row in service["volumes"]] == ["/home/dev/.ssh"]
    assert service["volumes"][0]["read_only"] is True
    assert "environment" not in service


def helper():
    return {
        "Id": "helper-a",
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "generation-a"}},
        "State": {"Running": True},
        "NetworkSettings": {"Networks": {"djinn-network": {"IPAddress": "172.20.0.2"}}},
    }


def test_readiness_journal_before_admission(tmp_path, monkeypatch):
    # Mutation: admit before durable relay-ready journal append.
    calls = []
    trust = ssh_delivery.peer_snapshot(config(), status(), "generation-a")
    monkeypatch.setattr(host_runtime, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(host_runtime, "read_state", lambda root: None)
    monkeypatch.setattr(hostctl, "save_private", lambda *a: calls.append("snapshot"))
    monkeypatch.setattr(hostctl, "journal", lambda *a, **k: calls.append("journal"))

    def command(*args):
        calls.append("admit")
        assert args[:4] == ("exec", "helper-a", hostctl.SUPERVISOR, "admit")
        assert json.loads(args[-1]) == trust["routes"]
        return json.dumps({"generation": "generation-a", "admission": True, "closed": False})

    monkeypatch.setattr(hostctl, "command", command)
    hostctl.admit_locked(helper(), trust)
    assert calls == ["snapshot", "journal", "admit"]
    calls.clear()
    monkeypatch.setattr(hostctl, "journal", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        hostctl.admit_locked(helper(), trust)
    assert "admit" not in calls


def test_custom_subnet_refuses_without_allowlist_expansion(monkeypatch):
    # Mutation: bypass the helper bridge IP assessment.
    value = helper()
    value["NetworkSettings"]["Networks"]["djinn-network"]["IPAddress"] = "203.0.113.2"
    monkeypatch.setattr(hostctl, "command", lambda *a: pytest.fail("admitted unsupported subnet"))
    with pytest.raises(hostctl.HostctlError, match="RFC1918"):
        hostctl.admit_locked(value, ssh_delivery.peer_snapshot(config(), status(), "generation-a"))


def test_open_state_requires_helper_admission(monkeypatch):
    # Mutation: infer open from BackendState=Running.
    monkeypatch.setattr(hostctl, "inspect_helper", helper)
    monkeypatch.setattr(hostctl, "read_window", lambda helper: {"deadline": "2026-01-01T00:00:00Z"})
    monkeypatch.setattr(hostctl, "node_status", lambda helper: status())
    monkeypatch.setattr(hostctl, "cached_trust", lambda: None)
    monkeypatch.setattr(
        hostctl,
        "command",
        lambda *a: json.dumps({"generation": "generation-a", "admission": False}),
    )
    assert hostctl.snapshot()["state"] == "opening"
    monkeypatch.setattr(
        hostctl, "command", lambda *a: json.dumps({"generation": "generation-a", "admission": True})
    )
    assert hostctl.snapshot()["state"] == "open"


def test_doctor_trust_relay_and_unknown(monkeypatch):
    # Mutation: claim trust PASS for a different generation.
    value = {
        "state": "open",
        "helper": "helper-a",
        "generation": "generation-a",
        "relay": "open",
        "trust": {"generation": "old", "peers": {"host-a": {}}},
    }
    monkeypatch.setattr(hostctl, "snapshot", lambda: value)
    rows = {row.name: row for row in hostctl_checks(None, daemon=True)}
    assert rows["Hostctl peer trust"].status is Status.WARN
    assert rows["Hostctl relay"].status is Status.PASS
    assert "unknown" in rows["Hostctl sealing"].detail
    value["trust"]["generation"] = "generation-a"
    rows = {row.name: row for row in hostctl_checks(None, daemon=True)}
    assert rows["Hostctl peer trust"].status is Status.PASS


def load_connector():
    spec = importlib.util.spec_from_file_location(
        "hostctl_connect", ROOT / "scripts/hostctl-connect.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_connector_fragmented_handshake_and_refusal(monkeypatch):
    # Mutation: accept a rejected SOCKS CONNECT reply.
    module = load_connector()
    for code in [0, 2]:
        client, server = socket.socketpair()
        monkeypatch.setattr(
            module.socket, "create_connection", lambda *a, _client=client, **k: _client
        )

        def fake_relay(server=server, code=code):
            with server:
                assert module.receive(server, 3) == b"\x05\x01\x00"
                server.sendall(b"\x05")
                server.sendall(b"\x00")
                assert module.receive(server, 10) == b"\x05\x01\x00\x01\x64\x40\x00\x01\x00\x16"
                for value in bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]):
                    try:
                        server.sendall(bytes([value]))
                    except BrokenPipeError:
                        break

        thread = threading.Thread(target=fake_relay)
        thread.start()
        if code:
            with pytest.raises(OSError, match="admission"):
                module.connect("100.64.0.1", 22)
        else:
            module.connect("100.64.0.1", 22).close()
        thread.join(timeout=2)
        assert not thread.is_alive()
    with pytest.raises(ValueError, match="port 22"):
        module.connect("host-a", 23)


def test_connector_packaged_and_firewall_keeps_private_rules():
    # Mutation: drop connector COPY or its executable permission.
    import re

    dockerfile = (ROOT / "Dockerfile").read_text()
    assert (
        "COPY --chmod=755 scripts/hostctl-connect.py /usr/local/bin/djinn-hostctl-connect"
        in dockerfile
    )
    # The build runs as dev after USER; a RUN there cannot change root-owned system paths.
    after_user = dockerfile.split("\nUSER $USERNAME\n", 1)[1]
    runs = re.findall(r"^RUN (.+(?:\\\n.+)*)", after_user, re.M)
    assert runs
    assert not [r for r in runs if re.search(r"(?<![~\w.])/(usr|etc|opt|bin|sbin|lib)/", r)]
    firewall = (ROOT / "scripts/init-firewall.sh").read_text()
    assert '"172.16.0.0/12"' in firewall and '"192.168.0.0/16"' in firewall
    assert '"10.0.0.0/8"' in firewall and '"100.64.0.0/10"' not in firewall


@pytest.fixture
def observer_setup(tmp_path, monkeypatch):
    def setup(*, verify_stdin=False, **opening):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        value = helper()
        stopped = []
        hostctl.save_private(
            "opening.json",
            {"generation": "generation-a", "config": config().model_dump(), **opening},
        )
        monkeypatch.setattr(hostctl, "HELPER_NAME", "helper-a")
        monkeypatch.setattr(hostctl, "DOCKER_EXECUTABLE", "/bin/docker")
        monkeypatch.setattr(hostctl, "inspect_helper", lambda: value)
        monkeypatch.setattr(hostctl, "node_status", lambda helper: status())
        monkeypatch.setattr(host_runtime, "inspect_dev", lambda *args: None)

        class Enrollment:
            returncode = 0

            def poll(self) -> int:
                return 0

        def enroll(*args, **kwargs):
            if verify_stdin:
                assert kwargs["stdin"] == subprocess.DEVNULL
            return Enrollment()

        def stop() -> None:
            stopped.append("stop")
            value["State"]["Running"] = False

        monkeypatch.setattr(host_runtime.subprocess, "Popen", enroll)
        monkeypatch.setattr(hostctl, "stop_helper_locked", stop)
        return value, stopped

    return setup


@pytest.mark.parametrize("cancel", [False, True])
def test_observer_freezes_trust_and_rechecks_generation(observer_setup, monkeypatch, cancel):
    # Mutations: drop once-per-opening guard; drop generation recheck before admission.
    value, _ = observer_setup()
    iterations = 0
    captures = []
    root = hostctl.state_root()
    monkeypatch.setattr(hostctl, "reconcile", lambda helper: None)
    prepare = hostctl.prepare_trust

    def snapshot(gen, node):
        captures.append("snapshot")
        trust = prepare(gen, node)
        if cancel:
            value["Config"]["Labels"][hostctl.GENERATION_LABEL] = "replacement"
        return trust

    monkeypatch.setattr(hostctl, "prepare_trust", snapshot)
    monkeypatch.setattr(hostctl, "admit_locked", lambda *a, **kw: captures.append("admit"))

    def advance(_):
        nonlocal iterations
        iterations += 1
        if iterations == 2:
            value["State"]["Running"] = False

    monkeypatch.setattr(host_runtime.time, "sleep", advance)
    host_runtime.observe_hostctl("helper-a", "generation-a", "/bin/docker")
    assert captures == (["snapshot"] if cancel else ["snapshot", "admit"])
    assert (root / "observation.json").exists() is (not cancel)


def test_unknown_relay_never_claims_closed(monkeypatch):
    # Mutation: label an unavailable helper admission query closed.
    monkeypatch.setattr(hostctl, "inspect_helper", helper)
    monkeypatch.setattr(hostctl, "read_window", lambda helper: {})
    monkeypatch.setattr(hostctl, "node_status", lambda helper: status())
    monkeypatch.setattr(hostctl, "cached_trust", lambda: None)
    monkeypatch.setattr(
        hostctl, "command", lambda *a: (_ for _ in ()).throw(hostctl.HostctlError("unavailable"))
    )
    value = hostctl.snapshot()
    assert value["relay"].startswith("unknown")
    assert value["state"] == "opening" and value["relay_error"] == "unavailable"


def test_host_only_delivery_requires_readable_image_uid(tmp_path, monkeypatch):
    # Mutation: enforce the public delivery UID only for Git sockets.
    from djinn_in_a_box.config.models import AppConfig

    value = AppConfig(code_dir=tmp_path, hostctl=config())
    monkeypatch.setattr(host_runtime.os, "getuid", lambda: host_runtime.CONTAINER_USER_UID + 1)
    with (
        pytest.raises(ssh_delivery.GitSSHError, match="numeric UID"),
        host_runtime.git_runtime(value, "disposable-dev"),
    ):
        pytest.fail("created unreadable host-only SSH delivery")


@pytest.mark.parametrize("unknown", [False, True])
def test_observer_refusal_reason_is_grouped(observer_setup, monkeypatch, unknown):
    from djinn_in_a_box.core import host_sealing as sealing

    value, stopped = observer_setup(dev_id="dev-a")
    pair = ("/workspace", "/delivery")
    assessment = sealing.Assessment(
        "dev-a",
        (
            sealing.Finding("first detail", pair, "cause", "one"),
            sealing.Finding("second detail", pair, "cause", "two"),
        ),
        ((sealing.Finding("unknown detail", pair, "unknown", "item"),) if unknown else ()),
    )
    monkeypatch.setattr(sealing, "inspect_assessment", lambda: assessment)
    monkeypatch.setattr(
        hostctl,
        "command",
        lambda *args, **kwargs: pytest.fail("unexpected Docker command"),
    )
    host_runtime.observe_hostctl("helper-a", "generation-a", "/bin/docker")
    rows = [
        json.loads(line)
        for line in (hostctl.state_root() / "journal.jsonl").read_text().splitlines()
    ]
    refusal = next(row for row in rows if row["event"] == "relay-refused")
    assert refusal["reason"] == "Sealing refused: " + "; ".join(
        (*assessment.causes, *assessment.errors)
    )
    assert stopped == ["stop"] and value["State"]["Running"] is False
    assert not (hostctl.state_root() / "observation.json").exists()
    assert not (hostctl.state_root() / "assessment.json").exists()


@pytest.mark.parametrize("destination", [[], {}], ids=["list", "dict"])
@pytest.mark.parametrize("allow_unsealed", [False, True], ids=["strict", "override"])
@pytest.mark.parametrize(
    "rw, desktop_failure, expected_state",
    [(False, False, "sealed"), (True, False, "unsealed"), (True, True, "unknown")],
    ids=["readonly", "root", "fallback"],
)
def test_observer_malformed_bind_destination_keeps_base_decision(
    observer_setup,
    tmp_path,
    monkeypatch,
    destination,
    allow_unsealed,
    rw,
    desktop_failure,
    expected_state,
):
    from djinn_in_a_box.core import desktop, docker
    from djinn_in_a_box.core import host_sealing as sealing

    value, stopped = observer_setup(
        verify_stdin=True, dev_id="dev-a", allow_unsealed=allow_unsealed
    )
    source = Path("/") if rw else tmp_path / "workspace"
    if not rw:
        source.mkdir()
    actual = {
        "Id": "dev-a",
        "State": {"Running": True},
        "Mounts": [{"Type": "bind", "Source": str(source), "Destination": destination, "RW": rw}],
        "Config": {"Env": [], "Labels": {}, "Image": "dev:1"},
        "HostConfig": {"NetworkMode": "bridge"},
        "NetworkSettings": {"Networks": {"djinn-network": {}}},
    }
    monkeypatch.setattr(host_runtime, "runtime_root", lambda **kwargs: tmp_path / "runtime")
    monkeypatch.setattr(sealing, "execution_paths", lambda: {})
    monkeypatch.setattr(sealing, "docker_sockets", lambda: ())
    monkeypatch.setattr(desktop, "discover_desktop_endpoints", lambda: ())

    def inspect(name, *args, **kwargs):
        if name == desktop.HELPER_IMAGE and desktop_failure:
            raise RuntimeError(
                "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                "Is the docker daemon running?"
            )
        if name == hostctl.HOSTCTL_STATE_VOLUME:
            return {"Mountpoint": str(tmp_path / "data")}
        assert name == docker.service_container_name("dev")
        return actual

    def command(*args, **kwargs):
        if args[0] == "info":
            return json.dumps({"DockerRootDir": str(tmp_path / "data")})
        assert args == ("ps", "-q")
        return ""

    monkeypatch.setattr(host_runtime, "inspect_object", inspect)
    monkeypatch.setattr(hostctl, "command", command)
    if not desktop_failure:
        monkeypatch.setattr(
            sealing,
            "_desktop",
            lambda actual: desktop.DesktopInspection(
                channels=(), raw_sources=(), raw_verified=True
            ),
        )

    assessment = sealing.inspect_assessment()
    assert assessment.state == expected_state
    refused = desktop_failure or (rw and not allow_unsealed)
    admitted = []
    if not refused:
        assessment.require(allow_unsealed=allow_unsealed)
        monkeypatch.setattr(hostctl, "reconcile", lambda helper: None)
        monkeypatch.setattr(sealing, "run_probe", lambda *args: [{"state": "blocked"}])
        monkeypatch.setattr(
            hostctl, "admit_locked", lambda *args, **kwargs: admitted.append("admit")
        )
        monkeypatch.setattr(host_runtime, "inspect_dev", lambda *args: ("dev-a", True, ""))
        monkeypatch.setattr(
            host_runtime.time, "sleep", lambda _: value["State"].update(Running=False)
        )
    else:
        with pytest.raises(hostctl.HostctlError, match="Sealing refused"):
            assessment.require(allow_unsealed=allow_unsealed)
    host_runtime.observe_hostctl("helper-a", "generation-a", "/bin/docker")
    journal = hostctl.state_root() / "journal.jsonl"
    rows = [
        json.loads(line) for line in (journal.read_text().splitlines() if journal.exists() else ())
    ]
    refusals = [row for row in rows if row["event"] == "relay-refused"]
    if refused:
        assert len(refusals) == 1
        assert refusals[0]["reason"] == "Sealing refused: " + "; ".join(
            (*assessment.causes, *assessment.errors)
        )
        if desktop_failure:
            assert "raw desktop endpoint inspection unknown" in refusals[0]["reason"]
        assert stopped == ["stop"] and value["State"]["Running"] is False
        assert not (hostctl.state_root() / "observation.json").exists()
        assert not (hostctl.state_root() / "assessment.json").exists()
    else:
        assert not refusals and not stopped and admitted == ["admit"]
        assert (hostctl.state_root() / "observation.json").exists()
        assert (hostctl.state_root() / "assessment.json").exists()
