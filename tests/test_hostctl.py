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
    from djinn_in_a_box.core import host_sealing

    # B1 transition fixtures describe a window with no dev; B3 tests inspect delivery.
    monkeypatch.setattr(host_sealing, "inspect_assessment", lambda: host_sealing.Assessment(None))
    monkeypatch.setattr(hostctl, "verify_dev", lambda dev_id: None)
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


def test_packaging_and_volume_producer_registry(inputs, compose_yaml_loader):
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
        compose = yaml.load(path.read_text(), Loader=compose_yaml_loader)
        if path.name == "docker-compose.run.yml":
            assert set(compose) == {"services"}
            assert compose["services"] == {"dev": {"networks": ["default"]}}
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
    from djinn_in_a_box.core import post_mortem

    assert calls[0][0][-5:] == ["--hostctl", "helper-a", "generation-a", "/usr/bin/docker",
                              str(post_mortem.records_root(create=False))]
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
        host_runtime.run_runtime_command("/usr/bin/docker", "stop", "disposable")
    else:
        host_runtime.remove_runtime_volume("disposable", "/usr/bin/docker")


@pytest.mark.parametrize("no_cache", [False, True])
def test_supervisor_build_uses_supported_buildx_flags(no_cache, monkeypatch):
    # Fakes accept any flag; the installed buildx rejects unknown ones.
    import re
    import shutil
    import subprocess

    from djinn_in_a_box.core import docker

    captured = {}

    def streamed(cmd, *, cwd=None, env=None):
        captured.update(cmd=cmd, cwd=cwd)
        return docker.RunResult(returncode=0)

    monkeypatch.setattr(docker, "_run_streamed", streamed)
    assert hostctl.build_supervisor(no_cache=no_cache).success
    cmd = captured["cmd"]
    assert cmd[1:3] == ["buildx", "build"] and cmd[-1] == "."
    assert cmd[cmd.index("-f") + 1] == "Dockerfile.hostctl-helper"
    assert cmd[cmd.index("-t") + 1] == hostctl.SUPERVISOR_IMAGE
    assert "--load" in cmd and ("--no-cache" in cmd) is no_cache
    assert (captured["cwd"] / "Dockerfile.hostctl-helper").is_file()
    docker_cli = shutil.which("docker")
    if docker_cli is None:
        pytest.skip("Docker CLI unavailable")
    help_text = subprocess.run(
        [docker_cli, "buildx", "build", "--help"], capture_output=True, text=True, check=False
    )
    # Without the plugin the CLI prints its generic help and still exits 0.
    if help_text.returncode or "docker buildx build" not in help_text.stdout:
        pytest.skip("docker buildx unavailable")
    for flag in [a for a in cmd[3:] if a.startswith("-")]:
        assert re.search(rf"(^|\s){re.escape(flag)}(,|\s|=)", help_text.stdout, re.M), flag


def grouped_assessment(*, unknown=False):
    from djinn_in_a_box.core import host_sealing as sealing

    findings = (
        sealing.Finding(
            "writable Python module one: /workspace -> /delivery",
            ("/workspace", "/delivery"),
            "writable Python environment",
            "one",
        ),
        sealing.Finding(
            "writable Python module two: /workspace -> /delivery",
            ("/workspace", "/delivery"),
            "writable Python environment",
            "two",
        ),
    )
    errors = (
        (
            sealing.Finding(
                "hard-link provenance unknown: /workspace/a: /workspace -> /delivery",
                ("/workspace", "/delivery"),
                "hard-link provenance unknown",
                "/workspace/a",
            ),
            sealing.Finding(
                "hard-link provenance unknown: /workspace/b: /workspace -> /delivery",
                ("/workspace", "/delivery"),
                "hard-link provenance unknown",
                "/workspace/b",
            ),
        )
        if unknown
        else ()
    )
    return sealing.Assessment("dev-a", findings, errors)


def test_snapshot_keeps_grouped_and_detail_fields(inputs, monkeypatch):
    from djinn_in_a_box.core import host_sealing as sealing

    assessment = grouped_assessment(unknown=True)
    monkeypatch.setattr(sealing, "inspect_assessment", lambda: assessment)
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: None)
    monkeypatch.setattr(hostctl, "cached_trust", lambda: None)
    snapshot = hostctl.snapshot()
    assert snapshot["sealing_causes"] == (
        "/workspace -> /delivery: writable Python environment one, two",
    )
    assert snapshot["sealing_errors"] == (
        "/workspace -> /delivery: hard-link provenance unknown /workspace/a, /workspace/b",
    )
    assert snapshot["sealing_cause_details"] == assessment.cause_details
    assert snapshot["sealing_error_details"] == assessment.error_details
    assert snapshot["helper"] == "absent" and snapshot["sealing"] == "unknown"


@pytest.mark.parametrize("unknown", [False, True])
def test_sealing_journal_lines_are_grouped(inputs, fake_transitions, monkeypatch, unknown):
    from djinn_in_a_box.core import host_sealing as sealing

    assessment = grouped_assessment(unknown=unknown)
    monkeypatch.setattr(sealing, "inspect_assessment", lambda: assessment)
    with pytest.raises(hostctl.HostctlError) as exc:
        hostctl.open_window(inputs)
    reason = "Sealing refused: " + "; ".join((*assessment.causes, *assessment.errors))
    assert str(exc.value) == reason
    if unknown:
        with pytest.raises(hostctl.HostctlError) as overridden:
            hostctl.open_window(inputs, allow_unsealed=True)
        assert str(overridden.value) == reason
        assert not fake_transitions["calls"]
    else:
        hostctl.open_window(inputs, allow_unsealed=True)
    rows = [
        json.loads(line)
        for line in (hostctl.state_root() / "journal.jsonl").read_text().splitlines()
    ]
    refused = [row for row in rows if row["event"] == "on-refused"]
    assert len(refused) == (2 if unknown else 1)
    for row in refused:
        assert row["causes"] == list(assessment.causes)
        assert row["errors"] == list(assessment.errors)
        assert "reason" not in row
    if not unknown:
        for event in ("unsealed-override", "on"):
            row = next(row for row in rows if row["event"] == event)
            assert row["causes"] == list(assessment.causes)
        assert (
            "host authority"
            in next(row for row in rows if row["event"] == "unsealed-override")["boundary"]
        )


def test_admission_persists_grouped_causes(inputs, monkeypatch):
    from djinn_in_a_box.core import host_sealing as sealing

    assessment = grouped_assessment()
    monkeypatch.setattr(sealing, "inspect_assessment", lambda: assessment)
    monkeypatch.setattr(hostctl, "verify_agent", lambda *args: None)
    rows = [
        {
            "host": "host-a",
            "address": "100.64.0.1",
            "state": "blocked",
            "namespace": "dev-a",
        }
    ]
    monkeypatch.setattr(sealing, "run_probe", lambda dev_id, trust: rows)
    hostctl.save_private(
        "opening.json",
        {"generation": "generation-a", "dev_id": "dev-a", "allow_unsealed": True},
    )
    checked = hostctl.admission_assessment({"generation": "generation-a"})
    assert checked is not None
    assert checked == {
        "generation": "generation-a",
        "dev_id": "dev-a",
        "creator": None,
        "sealing": "unsealed",
        "causes": assessment.causes,
        "probe": rows,
        "agent": None,
    }
    hostctl.commit_assessment(checked)
    saved = json.loads((hostctl.state_root() / "assessment.json").read_text())
    assert set(saved) == {
        "generation",
        "dev_id",
        "creator",
        "sealing",
        "causes",
        "probe",
        "agent",
    }
    assert saved["causes"] == list(assessment.causes) and saved["probe"] == rows


@pytest.mark.parametrize(
    "case",
    ["empty", "standalone", "single", "merged", "unknown", "mixed", "absent"],
)
def test_status_doctor_hint_when_grouped_view_differs(inputs, monkeypatch, case):
    from djinn_in_a_box.core import host_sealing as sealing

    findings = {
        "empty": (),
        "absent": (),
        "standalone": (sealing.Finding("standalone cause"),),
        "single": (
            sealing.Finding("per-item cause", ("/workspace", "/delivery"), "class", "item"),
        ),
        "merged": (
            sealing.Finding("first detail", ("/workspace", "/delivery"), "class", "one"),
            sealing.Finding("second detail", ("/workspace", "/delivery"), "class", "two"),
        ),
        "unknown": (),
        "mixed": (sealing.Finding("standalone cause"),),
    }[case]
    errors = (
        (sealing.Finding("per-item unknown", ("/workspace", "/delivery"), "unknown", "item"),)
        if case in {"unknown", "mixed"}
        else ()
    )
    assessment = sealing.Assessment("dev-a", findings, errors)
    value = {
        "state": "closed",
        "helper": "absent",
        "sealing": assessment.state,
        "relay": "closed",
        "sealing_causes": assessment.causes,
        "sealing_errors": assessment.errors,
        "sealing_cause_details": assessment.cause_details,
        "sealing_error_details": assessment.error_details,
    }
    if case == "absent":
        for key in (
            "sealing_causes",
            "sealing_errors",
            "sealing_cause_details",
            "sealing_error_details",
        ):
            value.pop(key)
    monkeypatch.setattr(hostctl, "snapshot", lambda: value)
    result = CliRunner().invoke(app, ["hostctl", "status"])
    assert result.exit_code == 0, result.output
    assert f"Sealing: {assessment.state}; relay: closed" in result.output
    assert result.output.count("djinn doctor lists each item.") == (
        0 if case in {"empty", "standalone", "absent"} else 1
    )
    for line in (*assessment.causes, *assessment.errors):
        assert line in result.output
    for detail in (*assessment.cause_details, *assessment.error_details):
        if detail not in (*assessment.causes, *assessment.errors):
            assert detail not in result.output


@pytest.fixture
def postmortem_helper(inputs, tmp_path, monkeypatch):
    import sys

    from djinn_in_a_box.core import post_mortem

    store = tmp_path / "containers.json"
    calls = tmp_path / "docker-calls.jsonl"
    logs = tmp_path / "helper-logs"
    logs.write_text("")
    data = helper(running=False)
    data["State"].update(StartedAt="2026-10-18T10:00:00Z", FinishedAt="2026-10-18T10:01:00Z",
                         OOMKilled=True, ExitCode=137)
    store.write_text(json.dumps(data))
    root = post_mortem.records_root(create=False)
    binary = tmp_path / "docker"
    binary.write_text(f"""#!{sys.executable}
import json, pathlib, sys, time
store = pathlib.Path({str(store)!r})
calls = pathlib.Path({str(calls)!r})
logs = pathlib.Path({str(logs)!r})
records = pathlib.Path({str(root)!r})
args = sys.argv[1:]
with calls.open('a') as out: out.write(json.dumps([time.monotonic(), args])+'\\n')
obj = json.loads(store.read_text())
if args[:2] == ['container', 'inspect']:
    if obj and args[2] in ('djinn-hostctl', 'helper-a', obj['Id']):
        print(json.dumps([obj]))
    else:
        print('Error response from daemon: No such container: '+args[2], file=sys.stderr)
        sys.exit(1)
elif args[:2] == ['network', 'inspect']: print(json.dumps([{{'Name': 'djinn-network'}}]))
elif args[:2] == ['volume', 'inspect']:
    print('Error response from daemon: get '+args[2]+': no such volume', file=sys.stderr)
    sys.exit(1)
elif args[0] == 'ps': pass
elif args[0] == 'logs': print(logs.read_text())
elif args[0] == 'stop':
    obj['State']['Running'] = False
    store.write_text(json.dumps(obj))
elif args[0] == 'rm':
    expectation = store.with_suffix('.expect')
    if expectation.exists():
        docs = [json.loads(p.read_text()) for p in records.glob('*Z-*.json')]
        if expectation.read_text() == 'record':
            assert any(d['trigger'] == 'hostctl-helper'
                       and d['hostctl_generation'] == 'generation-a'
                       and d['subjects'][0]['id'] == 'helper-a' for d in docs), 'missing record'
        else: assert not docs, 'unexpected record'
    store.write_text('null')
elif args[0] == 'run':
    gen = args[args.index('--generation')+1]
    store.write_text(json.dumps({{'Id': 'new-helper', 'State': {{'Running': True}},
                                 'Config': {{'Labels': {{'djinn.hostctl.generation': gen}}}}}}))
else: sys.exit(2)
""")
    binary.chmod(0o700)
    monkeypatch.setattr(hostctl, "DOCKER_EXECUTABLE", str(binary))
    monkeypatch.setattr(docker, "DOCKER_EXECUTABLE", str(binary))
    monkeypatch.setattr(docker, "is_own_container", lambda *a: False)
    monkeypatch.setattr(docker, "_run_compose", lambda *a, **kw: docker.RunResult(0))
    monkeypatch.setattr(docker, "ensure_network", lambda *a: True)
    monkeypatch.setattr(host_runtime, "start_hostctl_observer", lambda *a: None)
    return store, calls, logs, root


@pytest.mark.parametrize("route", ["reopen", "clean", "clean-all"])
@pytest.mark.parametrize("episode", ["oom", "expiry", "marked", "old-expiry", "bad-time",
                                      "wrong-generation", "bad-start", "equal-expiry", "noise"])
def test_hostctl_helper_run_eligibility(inputs, postmortem_helper, route, episode):
    from djinn_in_a_box.core import post_mortem

    store, call_log, logs, root = postmortem_helper
    if episode in {"expiry", "old-expiry", "bad-time", "wrong-generation", "equal-expiry"}:
        stamp = {"old-expiry": "2026-10-18T09:59:00Z", "bad-time": "invalid",
                 "equal-expiry": "2026-10-18T10:00:00Z"}.get(episode, "2026-10-18T10:00:30Z")
        logs.write_text(json.dumps({"event": "expiry", "generation":
                                   "foreign" if episode == "wrong-generation" else "generation-a",
                                   "time": stamp}))
        # Cache deliberately disagrees with current-run eligibility.
        hostctl.state_root().joinpath("observed-expiry.json").write_text('["generation-a"]')
    if episode == "noise":
        logs.write_text('not-json\n[]\n' + json.dumps({
            "event": "startup", "generation": "generation-a", "time": "2026-10-18T10:00:30Z",
        }))
    if episode == "bad-start":
        data = json.loads(store.read_text())
        data["State"]["StartedAt"] = "invalid"
        store.write_text(json.dumps(data))
    if episode == "marked":
        post_mortem.mark_terminated(hostctl.DOCKER_EXECUTABLE, "helper-a", "2026-10-18T10:00:00Z")
    eligible = episode not in {"expiry", "marked", "equal-expiry"}
    store.with_suffix('.expect').write_text("record" if eligible else "none")
    if route == "reopen":
        hostctl.open_window(inputs)
    else:
        assert docker.compose_down().success
        if route == "clean-all":
            with hostctl.control_guard():
                hostctl.delete_state_locked()
    assert bool(list(root.glob("*Z-*.json"))) == eligible
    assert not any(json.loads(line)[1][0] == "stop" for line in call_log.read_text().splitlines())


@pytest.mark.parametrize("site", ["off", "guard", "direct"])
def test_hostctl_marker_after_stop_and_journal(postmortem_helper, monkeypatch, site):
    from djinn_in_a_box.core import host_sealing, post_mortem

    store, calls, _, root = postmortem_helper
    data = json.loads(store.read_text())
    data["State"]["Running"] = True
    store.write_text(json.dumps(data))
    events = []
    journal = hostctl.journal
    mark = post_mortem.mark_terminated
    marker_calls = []

    def journaled(event, **kwargs):
        events.append(event)
        journal(event, **kwargs)

    def marked(path, identity, started_at, **kwargs):
        marker_calls.append((identity, started_at, kwargs))
        assert any(row[1][0] == "stop" for row in map(json.loads, calls.read_text().splitlines()))
        assert not json.loads(store.read_text())["State"]["Running"]
        assert kwargs["blocking"] is False
        assert events[-1] == {"off": "off", "guard": "dev-start-closed",
                              "direct": "direct-before"}[site]
        assert not getattr(hostctl._held, "active", False)
        with hostctl.control_guard():
            pass
        mark(path, identity, started_at, **kwargs)

    monkeypatch.setattr(hostctl, "journal", journaled)
    monkeypatch.setattr(post_mortem, "mark_terminated", marked)
    if site == "off":
        hostctl.close_window()
    elif site == "guard":
        monkeypatch.setattr(host_sealing, "assess", lambda *a, **kw: host_sealing.Assessment(
            "dev-id", (host_sealing.Finding("unsealed"),),
        ))
        hostctl.guard_dev_start({}, "creator")
    else:
        hostctl.journal("direct-before")
        hostctl.stop_helper_locked()
    assert json.loads((root / "provenance.json").read_text()) == {
        "helper-a": "2026-10-18T10:00:00Z",
    }
    assert len(marker_calls) == 1


def test_hostctl_records_lock_never_delays_relay_stop(postmortem_helper):
    import sys
    import time

    from djinn_in_a_box.core import post_mortem

    store, calls, _, root = postmortem_helper
    data = json.loads(store.read_text())
    data["State"]["Running"] = True
    store.write_text(json.dumps(data))
    post_mortem.records_root(create=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl,os,sys; "
         "f=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); "
         "fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.readline()",
         str(root / ".lock")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        before = time.monotonic()
        hostctl.close_window()
        finished = time.monotonic()
        stop_time = next(row[0] for row in map(json.loads, calls.read_text().splitlines())
                         if row[1][0] == "stop")
        assert stop_time - before < 1
        assert finished - before < 1
        assert not (root / "provenance.json").exists()
    finally:
        holder.communicate("release\n", timeout=5)


@pytest.mark.parametrize("outcome", ["running", "stopped", "unclear", "different-run"])
def test_hostctl_stop_failure_retry(postmortem_helper, monkeypatch, outcome):
    store, call_log, _, root = postmortem_helper
    data = json.loads(store.read_text())
    data["State"]["Running"] = True
    store.write_text(json.dumps(data))
    command = hostctl.command
    inspect = host_runtime.inspect_object
    attempted = False

    def failed(*args, **kwargs):
        nonlocal attempted
        if args[0] != "stop":
            return command(*args, **kwargs)
        attempted = True
        if outcome != "running":
            data["State"]["Running"] = False
        if outcome == "different-run":
            data["State"]["StartedAt"] = "2026-10-18T11:00:00Z"
        store.write_text(json.dumps(data))
        raise hostctl.HostctlError("Docker stop failed (exit 124)")

    def inspected(*args, **kwargs):
        if attempted and outcome == "unclear":
            raise RuntimeError("inspection unavailable")
        return inspect(*args, **kwargs)

    monkeypatch.setattr(hostctl, "command", failed)
    monkeypatch.setattr(host_runtime, "inspect_object", inspected)
    with pytest.raises(hostctl.HostctlError, match="stop failed"):
        hostctl.close_window()
    marker = root / "provenance.json"
    assert (json.loads(marker.read_text()) if marker.exists() else {}) == (
        {"helper-a": "2026-10-18T10:00:00Z"} if outcome == "stopped" else {}
    )
    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(host_runtime, "inspect_object", inspect)
    data["State"]["Running"] = False
    store.write_text(json.dumps(data))
    store.with_suffix('.expect').write_text("none" if outcome == "stopped" else "record")
    call_log.write_text("")
    hostctl.stop_helper_locked(remove=True)
    assert bool(list(root.glob("*Z-*.json"))) == (outcome != "stopped")
    assert not any(json.loads(line)[1][0] == "stop" for line in call_log.read_text().splitlines())


@pytest.mark.parametrize("fault", ["record", "mark_terminated"])
def test_hostctl_diagnostic_exception_keeps_closure(postmortem_helper, monkeypatch, fault):
    from djinn_in_a_box.core import post_mortem

    store, _, _, _ = postmortem_helper
    data = json.loads(store.read_text())
    data["State"]["Running"] = fault == "mark_terminated"
    store.write_text(json.dumps(data))

    def broken(*args, **kwargs):
        raise RuntimeError("diagnostic failure")

    monkeypatch.setattr(post_mortem, fault, broken)
    hostctl.stop_helper_locked(remove=True)
    assert json.loads(store.read_text()) is None


def test_hostctl_running_remove_skips_record_without_marker(postmortem_helper, monkeypatch):
    from djinn_in_a_box.core import post_mortem

    store, _, _, _ = postmortem_helper
    data = json.loads(store.read_text())
    data["State"]["Running"] = True
    store.write_text(json.dumps(data))
    store.with_suffix('.expect').write_text("none")
    monkeypatch.setattr(post_mortem, "mark_terminated", lambda *a, **kw: None)
    hostctl.stop_helper_locked(remove=True)


def test_hostctl_deferred_marker_exception_keeps_journal(postmortem_helper, monkeypatch, capsys):
    from djinn_in_a_box.core import post_mortem

    store, _, _, _ = postmortem_helper
    data = json.loads(store.read_text())
    data["State"]["Running"] = True
    store.write_text(json.dumps(data))

    def broken(*args, **kwargs):
        raise RuntimeError("diagnostics marker failure")

    monkeypatch.setattr(post_mortem, "mark_terminated", broken)
    hostctl.close_window()
    assert not json.loads(store.read_text())["State"]["Running"]
    assert '"event": "off"' in (hostctl.state_root() / "journal.jsonl").read_text()
    captured = capsys.readouterr()
    assert "diagnostics marker failure" not in captured.out + captured.err


@pytest.mark.parametrize("retry", [False, True])
def test_hostctl_stopped_helper_never_stops_on_removal(postmortem_helper, monkeypatch, retry):
    store, call_log, _, root = postmortem_helper
    command = hostctl.command
    original_error = hostctl.HostctlError("removal refused")
    failed = False

    def checked(*args, **kwargs):
        nonlocal failed
        assert args[0] != "stop", "already-stopped helper must not receive stop"
        if retry and args[0] == "rm" and not failed:
            failed = True
            raise original_error
        return command(*args, **kwargs)

    monkeypatch.setattr(hostctl, "command", checked)
    store.with_suffix('.expect').write_text("record")
    if retry:
        with pytest.raises(hostctl.HostctlError) as caught:
            hostctl.stop_helper_locked(remove=True)
        assert caught.value is original_error
        assert not (root / "provenance.json").exists()
    hostctl.stop_helper_locked(remove=True)
    assert json.loads(store.read_text()) is None
    assert not any(json.loads(line)[1][0] == "stop" for line in call_log.read_text().splitlines())
    assert not (root / "provenance.json").exists()


def test_compose_loader_only_accepts_merge_tags(compose_yaml_loader):
    import yaml

    assert yaml.load("value: !override [default]", Loader=compose_yaml_loader) == {
        "value": ["default"],
    }
    assert yaml.load("value: !reset []", Loader=compose_yaml_loader) == {"value": []}
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load("value: !unknown []", Loader=compose_yaml_loader)
