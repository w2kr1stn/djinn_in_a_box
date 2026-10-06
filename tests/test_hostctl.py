from __future__ import annotations

import json
import shutil
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import hostctl as cli
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.ssh import HostctlConfig, duration_minutes
from djinn_in_a_box.config.volumes import HOSTCTL_STATE_VOLUME, PROTECTED_INTERNAL_VOLUMES
from djinn_in_a_box.core import docker, host_runtime, hostctl


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = hostctl.state_root()
    (root / "bin").mkdir(mode=0o700)
    binary = root / "bin/supervisor"
    binary.write_bytes(b"static supervisor")
    binary.chmod(0o500)
    return AppConfig(
        code_dir=tmp_path,
        hostctl=HostctlConfig(
            hosts={
                "host-a": {"address": "host-a.example.ts.net", "user": "operator"},
            }
        ),
    )


def helper(*, running=True, gen="generation-a", identity="helper-a"):
    return {
        "Id": identity,
        "State": {"Running": running},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: gen}},
    }


@pytest.mark.parametrize("value,minutes", [("1m", 1), ("2h", 120), ("24h", 1440), ("1440m", 1440)])
def test_duration_grammar(value, minutes):
    assert duration_minutes(value) == minutes


@pytest.mark.parametrize(
    "value",
    ["0m", "-1h", "1", "1.5h", "1H", " 1h", "01m", "25h", "1441m", "1d", "99999999999999999m"],
)
def test_duration_rejects_unbounded_or_invalid(value):
    with pytest.raises(ValueError):
        duration_minutes(value)
    with pytest.raises(ValidationError):
        HostctlConfig(default_duration=value)


def test_explicit_empty_duration_does_not_use_default(inputs, fake_transitions):
    with pytest.raises(ValueError, match="duration"):
        hostctl.open_window(inputs, "")


def test_status_lists_protected_state(inputs, monkeypatch):
    from djinn_in_a_box.commands import container

    monkeypatch.setattr(container, "volume_exists", lambda name: name == HOSTCTL_STATE_VOLUME)
    monkeypatch.setattr(container, "get_existing_volumes_by_category", lambda *args: [])
    assert container._list_existing_volumes(inputs)["protected (only clean all)"] == [
        HOSTCTL_STATE_VOLUME
    ]


def test_config_roundtrip_and_scalar_preserve_hosts(inputs, tmp_path):
    from djinn_in_a_box.commands.config import _set_config_value

    path = tmp_path / "config.toml"
    save_config(inputs, path)
    loaded = load_config(path)
    assert loaded.hostctl == inputs.hostctl
    changed = _set_config_value(loaded, "hostctl.default_duration", "20m")
    assert changed.hostctl.default_duration == "20m"
    assert changed.hostctl.hosts == loaded.hostctl.hosts
    assert HostctlConfig().default_duration == "2h"


@pytest.mark.parametrize(
    "table",
    [
        {"*": {"address": "host-a", "user": "operator"}},
        {"host-a": {"address": "-oProxyCommand=bad", "user": "operator"}},
        {"host-a": {"address": "host-a\nHost *", "user": "operator"}},
        {"host-a": {"address": "host-a", "user": "user name"}},
        {"host-a": {"address": "host-a", "user": "operator", "authkey": "bad"}},
        {"host-a": {"address": "host-a"}},
    ],
)
def test_invalid_hosts(table):
    with pytest.raises(ValidationError):
        HostctlConfig(hosts=table)


def test_on_requires_hosts_and_supervisor(inputs, monkeypatch):
    empty = AppConfig(code_dir=inputs.code_dir)
    with pytest.raises(hostctl.HostctlError, match="Declare at least"):
        hostctl.open_window(empty)
    hostctl.supervisor_path().chmod(0o755)
    with pytest.raises(hostctl.HostctlError, match="owner-only"):
        hostctl.open_window(inputs)


@pytest.fixture
def fake_transitions(inputs, monkeypatch):
    state = {"helper": None, "calls": [], "network": None}
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: state["helper"])

    def ensure(name):
        state["calls"].append(("ensure", name))
        state["network"] = {"Name": name}
        return True

    monkeypatch.setattr(docker, "ensure_network", ensure)
    monkeypatch.setattr(host_runtime, "inspect_object", lambda *args: state["network"])

    def command(*args, **kwargs):
        state["calls"].append(args)
        if args[0] == "run":
            gen = args[args.index("--generation") + 1]
            state["helper"] = helper(gen=gen)
        elif args[0] == "stop":
            state["helper"]["State"]["Running"] = False
        elif args[0] == "rm":
            state["helper"] = None
        return ""

    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(host_runtime, "start_hostctl_observer", lambda *args: None)
    return state


@pytest.mark.parametrize("duration,expected", [(None, 120), ("3m", 3), ("24h", 1440)])
def test_on_network_before_helper_and_login_charged(inputs, fake_transitions, duration, expected):
    before = datetime.now(UTC)
    gen = hostctl.open_window(inputs, duration, allow_unsealed=True)
    assert fake_transitions["calls"][0] == ("ensure", hostctl.HELPER_NETWORK)
    argv = next(c for c in fake_transitions["calls"] if c[0] == "run")
    deadline = datetime.fromisoformat(argv[argv.index("--deadline") + 1])
    assert (
        before + timedelta(minutes=expected)
        <= deadline
        < before + timedelta(minutes=expected, seconds=2)
    )
    assert fake_transitions["helper"]["Config"]["Labels"][hostctl.GENERATION_LABEL] == gen
    with pytest.raises(hostctl.HostctlError, match="already open.*limit"):
        hostctl.open_window(inputs)
    assert len([c for c in fake_transitions["calls"] if c[0] == "run"]) == 1


@pytest.mark.parametrize("failure", ["create", "inspect"])
def test_network_failure_prevents_helper(inputs, fake_transitions, monkeypatch, failure):
    if failure == "create":
        # A later inspection may succeed after another Docker client creates it;
        # the explicit failure still must abort this attempt before helper creation.
        fake_transitions["network"] = {"Name": hostctl.HELPER_NETWORK}
        monkeypatch.setattr(docker, "ensure_network", lambda name: False)
    else:
        monkeypatch.setattr(host_runtime, "inspect_object", lambda *args: None)
    with pytest.raises(hostctl.HostctlError, match="network"):
        hostctl.open_window(inputs)
    assert not any(c[0] == "run" for c in fake_transitions["calls"])


def test_packaging_and_volume_producer_registry(inputs):
    import yaml

    from djinn_in_a_box.config.defaults import VOLUME_CATEGORIES

    argv = hostctl.run_argv(hostctl.supervisor_path(), "gen", datetime.now(UTC), 1)
    assert hostctl.HELPER_IMAGE in argv
    assert ":v1.102.5@sha256:" in hostctl.HELPER_IMAGE
    assert argv[argv.index("--restart") + 1] == "no"
    assert f"type=volume,src={HOSTCTL_STATE_VOLUME},dst=/var/lib/tailscale" in argv
    assert any(str(hostctl.supervisor_path()) in arg and arg.endswith(",readonly") for arg in argv)
    assert not ({"--rm", "--publish", "-p", "--device", "--cap-add", "--privileged"} & set(argv))
    assert not any("authkey" in arg.lower() or "/dev/net/tun" in arg for arg in argv)
    root = Path(__file__).parents[1]
    compose_names = set()
    for path in root.glob("docker-compose*.yml"):
        compose = yaml.safe_load(path.read_text())
        compose_names.update(value["name"] for value in (compose.get("volumes") or {}).values())
    category_names = {name for names in VOLUME_CATEGORIES.values() for name in names}
    helper_names = {
        arg.split("src=", 1)[1].split(",", 1)[0] for arg in argv if arg.startswith("type=volume,")
    }
    assert compose_names == category_names
    assert compose_names | helper_names == category_names | PROTECTED_INTERNAL_VOLUMES
    assert not category_names & PROTECTED_INTERNAL_VOLUMES


def test_emitted_flags_exist_in_installed_docker_help(inputs):
    executable = shutil.which("docker")
    if executable is None:
        pytest.skip("Docker CLI is not installed")
    argv = hostctl.run_argv(hostctl.supervisor_path(), "gen", datetime.now(UTC), 1)
    image_index = argv.index(hostctl.HELPER_IMAGE)
    flags = {arg for arg in argv[2:image_index] if arg.startswith("--")}
    for subcommand, emitted in [("run", flags), ("exec", set()), ("stop", {"-t"})]:
        output = subprocess.run(
            [executable, subcommand, "--help"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=True,
        ).stdout
        assert all(flag in output for flag in emitted)


def test_limit_acknowledgement_and_closed_errors(inputs, fake_transitions, monkeypatch):
    with pytest.raises(hostctl.HostctlError, match="closed"):
        hostctl.limit_window(1)
    fake_transitions["helper"] = helper()
    seen = []
    acknowledgement = {
        "generation": "generation-a",
        "closed": False,
        "deadline": datetime.now(UTC).isoformat(),
        "boottime_deadline_ns": 1,
    }

    def command(*args, **kwargs):
        seen.append(args)
        return json.dumps(acknowledgement)

    monkeypatch.setattr(hostctl, "command", command)
    assert hostctl.limit_window(2) == acknowledgement
    assert seen == [("exec", "helper-a", hostctl.SUPERVISOR, "set-deadline", "generation-a", "2")]
    for bad in [{**acknowledgement, "generation": "old"}, {}, []]:
        monkeypatch.setattr(hostctl, "command", lambda *args, bad=bad: json.dumps(bad))
        with pytest.raises(hostctl.HostctlError, match="acknowledge"):
            hostctl.limit_window(1)


@pytest.mark.parametrize("minutes", [0, -1, 1441])
def test_limit_bounds(minutes, monkeypatch):
    result = CliRunner().invoke(app, ["hostctl", "limit", str(minutes)])
    assert result.exit_code != 0
    # The core API bounds callers that bypass the CLI, before any Docker call.
    monkeypatch.setattr(hostctl, "command", lambda *a, **k: pytest.fail("Docker was called"))
    with pytest.raises(hostctl.HostctlError, match="up to 1440"):
        hostctl.limit_window(minutes)


def test_off_config_free_idempotent_and_failed_stop(inputs, fake_transitions, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: pytest.fail("off loaded config"))
    assert CliRunner().invoke(app, ["hostctl", "off"]).exit_code == 0
    fake_transitions["helper"] = helper()
    monkeypatch.setattr(
        hostctl,
        "command",
        lambda *a, **k: (_ for _ in ()).throw(hostctl.HostctlError("stop failed")),
    )
    result = CliRunner().invoke(app, ["hostctl", "off"])
    assert result.exit_code == 1
    assert "Window closed" not in result.output


def test_journal_failure_refuses_and_rotation(inputs, fake_transitions, monkeypatch):
    journal = hostctl.state_root() / "journal.jsonl"
    journal.write_text("old\n")
    journal.chmod(0o600)
    monkeypatch.setattr(hostctl, "JOURNAL_BYTES", 1)
    for _ in range(6):
        hostctl.journal("test", generation="generic")
    assert len(list(journal.parent.glob("journal.jsonl*"))) == 4
    assert journal.stat().st_mode & 0o777 == 0o600
    journal.chmod(0o644)
    monkeypatch.setattr(hostctl, "JOURNAL_BYTES", 10**6)
    with pytest.raises(hostctl.HostctlError, match="owner-only"):
        hostctl.open_window(inputs)
    assert not any(c[0] == "run" for c in fake_transitions["calls"])


def test_journal_failure_while_open_requests_closure(inputs, fake_transitions, monkeypatch):
    fake_transitions["helper"] = helper()
    monkeypatch.setattr(
        hostctl,
        "journal",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("journal write failed")),
    )
    with pytest.raises(OSError, match="journal write failed"):
        hostctl.limit_window(1)
    assert fake_transitions["helper"]["State"]["Running"] is False


def test_expiry_reconciled_once_and_no_invented_events(inputs, fake_transitions, monkeypatch):
    h = helper(running=False)
    expiry = json.dumps(
        {
            "event": "expiry",
            "generation": "generation-a",
            "time": "2026-01-01T00:00:00Z",
            "deadline": "2026-01-01T00:00:00Z",
        }
    )
    monkeypatch.setattr(hostctl, "command", lambda *args, **kw: expiry)
    with hostctl.control_guard():
        hostctl.reconcile(h)
        (hostctl.state_root() / "observed-expiry.json").unlink()
        hostctl.reconcile(h)
    rows = [
        json.loads(line)
        for line in (hostctl.state_root() / "journal.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1 and rows[0]["helper_time"] == "2026-01-01T00:00:00Z"
    monkeypatch.setattr(hostctl, "command", lambda *args, **kw: "forced kill, no expiry event")
    hostctl.reconcile(helper(running=False, gen="generation-b"))
    assert len((hostctl.state_root() / "journal.jsonl").read_text().splitlines()) == 1


def test_captured_commands_have_closed_stdin(inputs, monkeypatch):
    def run(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] is not None
        return subprocess.CompletedProcess(argv, 0, "ack", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert hostctl.command("exec", "generic", "status") == "ack"


def test_control_lock_off_limit_clean_serialization(inputs):
    reached = threading.Event()

    def other():
        with hostctl.control_guard():
            reached.set()

    with hostctl.control_guard():
        thread = threading.Thread(target=other)
        thread.start()
        assert not reached.wait(0.1)
    thread.join(timeout=2)
    assert reached.is_set()


def test_async_observer_spawn_does_not_wait_for_enrollment(inputs, monkeypatch):
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return object()  # Any wait/poll by the creator would fail this case.

    monkeypatch.setattr(subprocess, "Popen", popen)
    host_runtime.start_hostctl_observer("helper-a", "generation-a", "/usr/bin/docker")
    assert calls[0][0][-4:] == ["--hostctl", "helper-a", "generation-a", "/usr/bin/docker"]
    assert calls[0][1]["start_new_session"]
    assert calls[0][1]["stdin"] == subprocess.DEVNULL


def test_cleanup_checks_identity_and_retains_state(inputs, fake_transitions, monkeypatch):
    fake_transitions["helper"] = helper()
    with hostctl.control_guard():
        hostctl.stop_helper_locked(remove=True)
    calls = fake_transitions["calls"]
    assert calls[0] == ("stop", "-t", "3", "helper-a")
    assert ("rm", "helper-a") in calls
    assert not any(call[:2] == ("volume", "rm") for call in calls)
    values = iter([helper(), helper(running=False, identity="replacement")])
    monkeypatch.setattr(hostctl, "command", lambda *args, **kwargs: "")
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: next(values))
    with pytest.raises(hostctl.HostctlError, match="changed"):
        hostctl.stop_helper_locked(remove=True)


def test_internal_volume_backup_restore_and_declaration_refused(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(
        docker,
        "_run_captured",
        lambda *args, **kwargs: pytest.fail("protected volume reached Docker"),
    )
    from djinn_in_a_box.config.defaults import volume_categories

    assert not docker.backup_volume(HOSTCTL_STATE_VOLUME, tmp_path).success
    assert not docker.restore_volume(HOSTCTL_STATE_VOLUME, tmp_path).success
    assert not docker.delete_volume(HOSTCTL_STATE_VOLUME)
    config = AppConfig.model_validate(
        {
            **inputs.model_dump(),
            "mounts": {"hostctl-state": {"volume": True, "target": "/generic", "backup": "data"}},
        }
    )
    with pytest.raises(Exception, match="conflicts"):
        volume_categories(config)
    result = CliRunner().invoke(app, ["clean", "volumes", HOSTCTL_STATE_VOLUME])
    assert result.exit_code == 1 and "only djinn clean all" in result.output


@pytest.mark.parametrize("fail", [False, True])
def test_all_clean_lock_covers_teardown_state_and_network_without_config(inputs, monkeypatch, fail):
    from djinn_in_a_box.commands import container

    calls = []
    runtime = inputs.code_dir / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(host_runtime, "runtime_root", lambda **kwargs: runtime)
    monkeypatch.setattr(host_runtime, "inspect_dev", lambda *args: None)
    monkeypatch.setattr(container, "_load_optional_config", lambda: None)
    monkeypatch.setattr(container, "volume_categories", lambda config: {})
    monkeypatch.setattr(container, "SYNC_PATHS", {})

    def down():
        assert hostctl._held.active
        calls.append("down")
        return docker.RunResult(0)

    def delete_state():
        assert hostctl._held.active
        calls.append("state")
        if fail:
            raise OSError("protected state deletion failed")

    def delete_network(name):
        assert hostctl._held.active
        calls.append("network")
        return True

    monkeypatch.setattr(container, "compose_down", down)
    monkeypatch.setattr(hostctl, "delete_state_locked", delete_state)
    monkeypatch.setattr(container, "delete_volumes", lambda names: {})
    monkeypatch.setattr(container, "network_exists", lambda name: True)
    monkeypatch.setattr(container, "delete_network", delete_network)
    result = CliRunner().invoke(app, ["clean", "all", "--force"])
    assert result.exit_code == (1 if fail else 0)
    assert calls == (["down", "state"] if fail else ["down", "state", "network"])
    if fail:
        assert "Cleanup complete" not in result.output


def test_clean_all_delete_state_verifies_stop_and_deletion(inputs, fake_transitions, monkeypatch):
    fake_transitions["helper"] = helper()
    volume = {"Name": HOSTCTL_STATE_VOLUME}
    volumes = iter([volume, None])
    monkeypatch.setattr(host_runtime, "inspect_object", lambda *args: next(volumes))
    with hostctl.control_guard():
        hostctl.delete_state_locked()
    calls = fake_transitions["calls"]
    assert calls.index(("rm", "helper-a")) < calls.index(("volume", "rm", HOSTCTL_STATE_VOLUME))
    monkeypatch.setattr(host_runtime, "inspect_object", lambda *args: volume)
    with pytest.raises(hostctl.HostctlError, match="deletion could not"):
        hostctl.delete_state_locked()


@pytest.mark.parametrize(
    "resource,message",
    [
        ("container", "Error response from daemon: No such container: generic"),
        ("volume", "Error response from daemon: get generic: no such volume"),
    ],
)
def test_missing_daemon_shapes_and_closed_inspect_stdin(monkeypatch, resource, message):
    def run(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 1, "[]", message)

    monkeypatch.setattr(subprocess, "run", run)
    assert host_runtime.inspect_object("generic", "/usr/bin/docker", resource) is None


def test_no_git_host_alias_collision(inputs):
    data = inputs.model_dump()
    data["git"] = {
        "identities": {
            "host-a": {
                "hostname": "git.example.com",
                "user": "git",
                "key_file": "/generic/key",
                "public_key_file": "/generic/key.pub",
            }
        }
    }
    with pytest.raises(ValidationError, match="aliases must be distinct"):
        AppConfig.model_validate(data)


def test_build_and_published_go_gates_agree():
    root = Path(__file__).parents[1]
    assert "FROM golang:1.27.1-alpine" in (root / "Dockerfile.hostctl-helper").read_text()
    assert "go 1.27.1" in (root / "helper/hostctl/go.mod").read_text()
    assert "go test ./... && CGO_ENABLED=0 go build ./..." in (root / "CONTRIBUTING.md").read_text()


def test_helper_window_generation_and_malformed_state(inputs, monkeypatch):
    import io
    import tarfile

    h = helper()
    value = {
        "generation": "generation-a",
        "deadline": "2026-01-01T00:00:00Z",
        "boottime_deadline_ns": 1,
    }

    def run(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        data = json.dumps(value).encode()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            member = tarfile.TarInfo("window.json")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        return subprocess.CompletedProcess(argv, 0, stream.getvalue(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert hostctl.read_window(h) == value
    value["generation"] = "old"
    with pytest.raises(hostctl.HostctlError, match="malformed"):
        hostctl.read_window(h)


@pytest.mark.parametrize("labels", [None, {}, {"foreign": "owner"}])
def test_real_null_or_foreign_labels_refuse_ownership(monkeypatch, labels):
    monkeypatch.setattr(
        host_runtime,
        "inspect_object",
        lambda *args: {
            "Id": "foreign-helper",
            "Config": {"Labels": labels},
            "State": {"Running": True},
        },
    )
    with pytest.raises(hostctl.HostctlError, match="ownership is unknown"):
        hostctl.inspect_helper()


@pytest.mark.parametrize("operation", ["control", "runtime-volume"])
def test_shared_host_runtime_captured_calls_close_stdin(monkeypatch, operation):
    def run(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        host_runtime,
        "inspect_object",
        lambda *args: {
            "Name": "disposable",
            "Labels": {
                host_runtime.GENERATION_LABEL: "generation-a",
                "com.docker.compose.project": "djinn-in-a-box",
            },
        },
    )
    if operation == "control":
        host_runtime._command("/usr/bin/docker", "stop", "disposable")
    else:
        host_runtime.remove_runtime_volume("disposable", "/usr/bin/docker")
