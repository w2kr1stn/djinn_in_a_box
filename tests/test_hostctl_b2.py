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


def test_doctor_trust_relay_and_unchecked(monkeypatch):
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
    assert "unchecked" in rows["Hostctl helper"].detail
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
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY scripts/hostctl-connect.py /usr/local/bin/djinn-hostctl-connect" in dockerfile
    assert "chmod 755 /usr/local/bin/djinn-hostctl-connect" in dockerfile
    firewall = (ROOT / "scripts/init-firewall.sh").read_text()
    assert '"172.16.0.0/12"' in firewall and '"192.168.0.0/16"' in firewall
    assert '"10.0.0.0/8"' in firewall and '"100.64.0.0/10"' not in firewall


@pytest.mark.parametrize("cancel", [False, True])
def test_observer_freezes_trust_and_rechecks_generation(tmp_path, monkeypatch, cancel):
    # Mutations: drop once-per-opening guard; drop generation recheck before admission.
    value = helper()
    iterations = 0
    captures = []
    root = hostctl.state_root()
    hostctl.save_private(
        "opening.json", {"generation": "generation-a", "config": config().model_dump()}
    )
    monkeypatch.setattr(hostctl, "HELPER_NAME", "helper-a")
    monkeypatch.setattr(hostctl, "DOCKER_EXECUTABLE", "/bin/docker")
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: value)
    monkeypatch.setattr(hostctl, "reconcile", lambda helper: None)
    monkeypatch.setattr(host_runtime, "inspect_dev", lambda *a: None)
    monkeypatch.setattr(hostctl, "node_status", lambda helper: status())

    class Enrollment:
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(host_runtime.subprocess, "Popen", lambda *a, **k: Enrollment())
    prepare = hostctl.prepare_trust

    def snapshot(gen, node):
        captures.append("snapshot")
        trust = prepare(gen, node)
        if cancel:
            value["Config"]["Labels"][hostctl.GENERATION_LABEL] = "replacement"
        return trust

    monkeypatch.setattr(hostctl, "prepare_trust", snapshot)
    monkeypatch.setattr(hostctl, "admit_locked", lambda *a: captures.append("admit"))

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
