"""Guest identity, isolation, fail-closed preflight and bounded cleanup contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_desktop_runtime import behavior, calls, write_json

from djinn_in_a_box.core import desktop, docker, host_runtime, hostctl, post_mortem
from djinn_in_a_box.core.exceptions import RuntimeMountSpecificationError

RUN_ID = "012345abcdef"
NAME = f"djinn-run-{RUN_ID}"
DOCKER_ID = "guest-container-id"
VALID = {
    "services": {"dev": {"networks": {"default": None}, "volumes": []}},
    "networks": {"default": {"name": "djinn-in-a-box_default"}},
}


def run_object():
    return {
        "Id": DOCKER_ID, "Name": "/" + NAME,
        "Config": {"Labels": {
            host_runtime.RUN_LABEL: RUN_ID,
            "com.docker.compose.project": docker.COMPOSE_PROJECT,
            "com.docker.compose.service": "dev",
        }},
        "State": {"Running": True},
    }


@pytest.fixture
def guest(fake_owner, monkeypatch):
    config, _, _, _ = fake_owner
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda *args: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *args: ([], ()))
    return fake_owner


@pytest.fixture
def guest_compose(mock_app_config, monkeypatch):
    """Only preflight unit tests replace Compose's resolved configuration."""
    invocations = []
    response = SimpleNamespace(returncode=0, stdout=json.dumps(VALID), stderr="", success=True)

    def compose(args, **kwargs):
        invocations.append((list(args), kwargs))
        return response

    monkeypatch.setattr(docker, "_run_compose", compose)
    return mock_app_config, response, invocations


def tree_hash(root):
    digest = hashlib.sha256()
    for path in [root, *sorted(root.rglob("*"))]:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(str(path.lstat().st_mode).encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout", "exception", "interrupt"])
@pytest.mark.parametrize("dev_running", [False, True])
def test_guest_does_not_touch_dev_runtime(guest, monkeypatch, outcome, dev_running):
    config, _, objects, log = guest
    root = host_runtime.runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "private").mkdir()
    (root / "private/agent.sock").write_bytes(b"runtime sentinel")
    (root / host_runtime.STATE_FILE).write_text(json.dumps({
        "generation": "unchanged", "observer_pid": -1, "dev_running": dev_running,
    }))
    (root / "creator.lock").write_text("unchanged")
    before = tree_hash(root)
    write_json(objects, {"djinn": {
        "Id": "dev-id", "Name": "/djinn", "State": {"Running": dev_running},
    }})
    for module, names in (
        (docker, ["git_runtime", "_prepare_companions", "_require_agent_observer",
                  "_guard_dev_creation"]),
        (host_runtime, ["creation_guard", "read_state", "save_state"]),
        (hostctl, ["guard_dev_start"]),
        (host_runtime.GitRuntime, ["begin_creation", "add_to_fragment"]),
    ):
        for name in names:
            monkeypatch.setattr(module, name, Mock(side_effect=AssertionError(name)))
    behavior(guest, run_rc=9 if outcome == "failure" else 0, run_hang=outcome == "timeout")
    execute = subprocess.run

    def run(argv, **kwargs):
        result = execute(argv, **kwargs)
        if "compose" in argv and "run" in argv:
            if outcome == "exception":
                raise OSError("launch observation failed")
            if outcome == "interrupt":
                raise KeyboardInterrupt
        return result

    monkeypatch.setattr(subprocess, "run", run)
    if outcome in {"exception", "interrupt"}:
        with pytest.raises(OSError if outcome == "exception" else KeyboardInterrupt):
            docker.compose_run(config, docker.ContainerOptions(), interactive=False)
    else:
        result = docker.compose_run(config, docker.ContainerOptions(), interactive=False, timeout=1)
        assert result.returncode == {"success": 0, "failure": 9, "timeout": 124}[outcome]
        assert result.owner is None
    assert tree_hash(root) == before
    assert json.loads(objects.read_text()) == {
        "djinn": {"Id": "dev-id", "Name": "/djinn", "State": {"Running": dev_running}},
    }
    assert not any("djinn-network" in arg for call in calls(log) for arg in call)


def test_identity_fragment_and_frozen_inputs(guest, monkeypatch):
    config, _, _, log = guest
    frozen = docker._compose_host_env(config)
    render = Mock(side_effect=lambda config: {**frozen, "DJINN_TERM_WIDTH": str(render.call_count)})
    monkeypatch.setattr(docker, "_compose_host_env", render)
    invocations, fragments = [], []
    execute = subprocess.run

    def run(argv, **kwargs):
        if "compose" in argv:
            invocations.append((argv, kwargs))
            paths = [Path(argv[i + 1]) for i, value in enumerate(argv) if value == "-f"]
            fragments.append(json.loads(paths[-1].read_text()))
        return execute(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = docker.compose_run(
        config,
        docker.ContainerOptions(docker_mode=docker.DockerMode.DIRECT, firewall_enabled=True),
        interactive=False, command="echo guest", env={"PROMPT": "hello"},
    )
    assert result.owner is None and result.returncode == 0
    assert result.stdout == "guest output\n" and result.stderr == "guest diagnostic\n"
    render.assert_called_once_with(config)
    (pre, pre_kw), (run, run_kw) = invocations
    assert pre[:pre.index("config")] == run[:run.index("run")]
    assert pre_kw["env"] == run_kw["env"] and pre_kw["env"] is run_kw["env"]
    assert pre_kw["cwd"] == run_kw["cwd"] == docker.get_project_root()
    assert pre_kw["timeout"] == 30 and run_kw["stdin"] is subprocess.DEVNULL
    file_names = [Path(run[i + 1]).name for i, value in enumerate(run) if value == "-f"]
    assert file_names[:-1] == [
        "docker-compose.yml", "docker-compose.desktop.yml", "docker-compose.docker-direct.yml",
        "docker-compose.run.yml",
    ]
    name = run[run.index("--name") + 1]
    assert re.fullmatch(r"djinn-run-[0-9a-f]{12}", name)
    assert run[run.index("-l") + 1] == f"djinn.run={name.removeprefix('djinn-run-')}"
    assert "-T" in run and "--rm" in run
    assert "ENABLE_FIREWALL=true" in run and "PROMPT=hello" in run
    service = fragments[0]["services"]["dev"]
    assert all(service["environment"][key] is None for key in desktop.MANAGED_ENV)
    serialized = json.dumps(fragments) + json.dumps(run)
    assert "SSH_AUTH_SOCK" not in serialized and "DJINN_GIT_MANIFEST" not in serialized
    assert host_runtime.GENERATION_LABEL not in serialized
    assert "/run/djinn-git-agent" not in serialized and "/home/dev/.ssh" not in serialized
    identity = "guest-" + name.removeprefix("djinn-run-")
    assert ["stop", "-t", "10", identity] in calls(log)
    assert ["rm", "-f", identity] in calls(log)


def test_egress_guest_preflight_and_launch_share_fragment(guest, monkeypatch):
    from djinn_in_a_box.core.egress import plan_egress

    config, _, _, _ = guest
    plan = plan_egress(["api.example:443"], resolve=lambda _: ["8.8.8.8"])
    invocations = []
    execute = subprocess.run

    def run(argv, **kwargs):
        if "compose" in argv:
            phase = "config" if "config" in argv else "run"
            paths = [Path(argv[i + 1]) for i, value in enumerate(argv) if value == "-f"]
            override = paths[-1]
            fragment = json.loads(override.read_text())
            service = fragment["services"]["dev"]
            assert service["user"] == "0:0"
            assert service["entrypoint"] == ["/usr/local/bin/djinn-egress-init"]
            assert service["extra_hosts"] == ["api.example:8.8.8.8"]
            assert service["environment"]["DJINN_EGRESS_ALLOW"] == "8.8.8.8/32:443"
            invocations.append((phase, override, fragment))
        return execute(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = docker.compose_run(
        config, docker.ContainerOptions(egress=plan, firewall_enabled=True),
        interactive=False, command="agent",
    )
    assert result.returncode == 0
    (preflight, preflight_path, preflight_fragment), (launch, launch_path, launch_fragment) = (
        invocations
    )
    assert (preflight, launch) == ("config", "run")
    assert preflight_path == launch_path
    assert preflight_fragment == launch_fragment
    assert not preflight_path.exists()


@pytest.mark.parametrize("mode", [docker.DockerMode.NONE, docker.DockerMode.DIRECT])
def test_compose_overlay_is_guest_only(mode):
    assert not any("docker-compose.run.yml" in arg for arg in docker.get_compose_files(mode))
    assert docker.get_compose_files(mode, guest=True)[-1].endswith("docker-compose.run.yml")
    overlay = (docker.get_project_root() / "docker-compose.run.yml").read_text()
    assert "networks: !override [default]" in overlay


def test_non_dev_headless_keeps_existing_path(guest, monkeypatch):
    config, _, _, _ = guest
    monkeypatch.setattr(docker, "_run_preflight", Mock(side_effect=AssertionError))
    monkeypatch.setattr(docker, "git_runtime", Mock(side_effect=AssertionError))
    execute = Mock(return_value=subprocess.CompletedProcess([], 0, "other-service", ""))
    monkeypatch.setattr(subprocess, "run", execute)
    result = docker.compose_run(
        config, docker.ContainerOptions(), interactive=False, service="probe",
    )
    argv = execute.call_args.args[0]
    assert argv[argv.index("--name") + 1] == "djinn-probe"
    assert "-l" not in argv and "-T" in argv
    assert not any("docker-compose.run.yml" in arg for arg in argv)
    assert result.stdout == "other-service" and result.owner is None


def test_core_refuses_agent_docker_before_side_effects(mock_app_config, monkeypatch):
    monkeypatch.setattr(docker, "validate_managed_bind_sources", Mock(side_effect=AssertionError))
    with pytest.raises(RuntimeMountSpecificationError, match="agent Docker daemon"):
        docker.compose_run(
            mock_app_config, docker.ContainerOptions(docker_mode=docker.DockerMode.AGENT),
            interactive=False,
        )


@pytest.mark.parametrize("networks", [{"default": None}, ["default"]])
def test_preflight_accepts_only_default(guest_compose, networks):
    config, response, invocations = guest_compose
    model = copy.deepcopy(VALID)
    model["services"]["dev"]["networks"] = networks
    model["networks"]["default"]["ipam"] = {}
    response.stdout = json.dumps(model)
    frozen = {"HOME": "/frozen"}
    assert docker._run_preflight(["-p", "test"], config, Path("/project"), frozen) == model
    assert invocations == [(["-p", "test", "config", "--format", "json"], {
        "config": config, "cwd": Path("/project"), "timeout": 30, "env": frozen,
    })]


@pytest.mark.parametrize("case", [
    "extra-network", "absent-networks", "null-networks", "string-networks", "network-mode",
    "absent-service", "null-service", "absent-default", "null-default", "list-default", "renamed",
    "external", "driver", "driver-opts", "ipam", "null-ipam", "extra-key", "non-json",
])
def test_preflight_refuses_bad_network(guest_compose, case):
    config, response, _ = guest_compose
    model = copy.deepcopy(VALID)
    service = model["services"]["dev"]
    default = model["networks"]["default"]
    if case == "extra-network":
        service["networks"] = ["default", "djinn-network"]
    elif case == "absent-networks":
        service.pop("networks")
    elif case == "null-networks":
        service["networks"] = None
    elif case == "string-networks":
        service["networks"] = "default"
    elif case == "network-mode":
        service["network_mode"] = None
    elif case == "absent-service":
        model["services"].pop("dev")
    elif case == "null-service":
        model["services"]["dev"] = None
    elif case == "absent-default":
        model["networks"].pop("default")
    elif case == "null-default":
        model["networks"]["default"] = None
    elif case == "list-default":
        model["networks"]["default"] = ["name"]
    elif case == "renamed":
        default["name"] = "djinn-network"
    elif case in {"external", "driver", "driver-opts", "extra-key"}:
        default[case.replace("-", "_")] = False if case == "external" else "bridge"
    elif case == "ipam":
        default["ipam"] = {"config": [{"subnet": "10.0.0.0/24"}]}
    elif case == "null-ipam":
        default["ipam"] = None
    response.stdout = "not json" if case == "non-json" else json.dumps(model)
    response.stderr = "first diagnostic\nsecond diagnostic"
    with pytest.raises(docker.RunRefusedError) as caught:
        docker._run_preflight([], config, Path("/project"), {})
    message = str(caught.value)
    assert "older than 2.24.4" in message and "networks or network_mode" in message
    assert "rc=0" in message and "first diagnostic" in message
    assert "second diagnostic" not in message


@pytest.mark.parametrize("rc", [1, 124, 126, 127])
def test_preflight_failed_resolution(guest_compose, rc):
    config, response, _ = guest_compose
    response.returncode = rc
    response.success = False
    with pytest.raises(docker.RunRefusedError) as caught:
        docker._run_preflight([], config, Path("/project"), {})
    assert str(caught.value) == (
        f"Compose could not resolve the run configuration (rc={rc}): "
        "Compose resolution failed; refusing"
    )


def test_preflight_resolution_reports_last_error(guest_compose):
    config, response, _ = guest_compose
    response.returncode = 15
    response.success = False
    diagnostic = 'service "dev" refers to undefined network missing: invalid compose project'
    response.stderr = (
        'WARN[0000] The "X" variable is not set. Defaulting to a blank string.\n'
        f'\n{diagnostic}\n  \n'
    )
    with pytest.raises(docker.RunRefusedError) as caught:
        docker._run_preflight([], config, Path("/project"), {})
    assert str(caught.value) == (
        f"Compose could not resolve the run configuration (rc=15): {diagnostic}; refusing"
    )


@pytest.mark.parametrize("exc", [
    OSError("spawn failed"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
    subprocess.SubprocessError("subprocess failed"),
])
def test_preflight_converts_unexpected_failures(guest_compose, monkeypatch, exc):
    config, _, _ = guest_compose
    monkeypatch.setattr(docker, "_run_compose", Mock(side_effect=exc))
    with pytest.raises(docker.RunRefusedError) as caught:
        docker._run_preflight([], config, Path("/project"), {})
    assert str(caught.value) == (
        f"Compose could not resolve the run configuration: {exc}; refusing"
    )


def test_preflight_refusal_starts_no_container(guest):
    config, _, _, log = guest
    invalid = copy.deepcopy(VALID)
    invalid["services"]["dev"]["networks"] = ["default", "djinn-network"]
    behavior(guest, compose_config=invalid)
    with pytest.raises(docker.RunRefusedError):
        docker.compose_run(config, docker.ContainerOptions(), interactive=False)
    assert not any("run" in call or call[0] in {"stop", "rm"} for call in calls(log))


@pytest.mark.parametrize("case", ["absent", "leftover", "stop-timeout", "already-removing",
                                  "rm-gone", "rm-error", "inspect-error"])
def test_cleanup_steps(guest, monkeypatch, case):
    _, _, objects, log = guest
    if case != "absent":
        write_json(objects, {NAME: run_object()})
    if case == "already-removing":
        behavior(guest, errors={f"rm -f {DOCKER_ID}":
            f"Error response from daemon: removal of container {DOCKER_ID} is already in progress"})
    elif case == "rm-gone":
        behavior(guest, errors={f"rm -f {DOCKER_ID}":
            f"Error response from daemon: No such container: {DOCKER_ID}"})
    elif case == "rm-error":
        behavior(guest, errors={f"rm -f {DOCKER_ID}": "permission denied"})
    elif case == "inspect-error":
        behavior(guest, post_stop_error="inspection unavailable")
    elif case == "stop-timeout":
        behavior(guest, stop_timeout=True)
        execute = subprocess.run

        def bounded(argv, **kwargs):
            if argv[1] == "stop":
                assert kwargs["timeout"] == 15
                kwargs["timeout"] = 0.05
            return execute(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", bounded)
    assert docker._remove_run(RUN_ID, 10) is (case != "rm-error")
    commands = calls(log)
    if case == "absent":
        assert not any(c[0] in {"stop", "rm"} for c in commands)
    else:
        # A killed fake cannot flush its finally block during the stop timeout.
        if case != "stop-timeout":
            assert ["stop", "-t", "10", DOCKER_ID] in commands
        assert ["rm", "-f", DOCKER_ID] in commands


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("message", ["no such file or directory", "no such volume"])
def test_cleanup_strict_absence_wording(guest, monkeypatch, capsys, phase, message):
    _, _, objects, log = guest
    write_json(objects, {NAME: run_object()})
    if phase == "before":
        behavior(guest, errors={f"container inspect {NAME}": message})
    else:
        behavior(guest, post_stop_error=message)
    execute, inspections = subprocess.run, []

    def run(argv, **kwargs):
        if argv[1:3] == ["container", "inspect"]:
            inspections.append((argv[-1], kwargs["timeout"]))
        return execute(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert docker._remove_run(RUN_ID, 10) is (phase == "after")
    if phase == "before":
        assert inspections == [(NAME, 5), (NAME, 15)]
        assert not any(c[0] in {"rm", "stop"} for c in calls(log))
        assert f"{NAME} may still exist" in capsys.readouterr().err
    else:
        assert ["rm", "-f", DOCKER_ID] in calls(log)
        assert not json.loads(objects.read_text())


@pytest.mark.parametrize("field", [
    "name", "label", "project", "service", "docker-id", "empty-id", "null-id",
])
def test_cleanup_rejects_foreign_identity(guest, capsys, field):
    _, _, objects, log = guest
    obj = run_object()
    identifier = RUN_ID
    if field == "name":
        obj["Name"] = "/djinn-run-aaaaaaaaaaaa"
    elif field in {"empty-id", "null-id"}:
        obj["Id"] = "" if field == "empty-id" else None
    elif field == "docker-id":
        identifier = DOCKER_ID
        obj["Id"] = "replacement-id"
    else:
        key = {"label": host_runtime.RUN_LABEL, "project": "com.docker.compose.project",
               "service": "com.docker.compose.service"}[field]
        obj["Config"]["Labels"][key] = "foreign"
    write_json(objects, {identifier if field == "docker-id" else NAME: obj})
    assert docker._remove_run(identifier, 10)
    assert not any(c[0] in {"stop", "rm"} for c in calls(log))
    assert json.loads(objects.read_text())
    message = capsys.readouterr().err
    assert "Skipping" in message
    assert "check it, then remove it or run `djinn clean`" in message


@pytest.mark.parametrize("message", ["[/var/lib/docker]", "[bold]boom"])
def test_cleanup_warnings_preserve_daemon_text(guest, capsys, message):
    _, _, objects, log = guest
    write_json(objects, {NAME: run_object()})
    diagnostic = f"Error response from daemon: {message}"
    behavior(guest, errors={
        f"stop -t 10 {DOCKER_ID}": diagnostic,
        f"rm -f {DOCKER_ID}": diagnostic,
    })
    assert not docker._remove_run(RUN_ID, 10)
    stderr = capsys.readouterr().err
    assert stderr.count(message) == 2
    assert "Could not stop" in stderr and "may still exist" in stderr
    assert NAME in stderr and "djinn clean" in stderr
    assert ["stop", "-t", "10", DOCKER_ID] in calls(log)
    assert ["rm", "-f", DOCKER_ID] in calls(log)


def test_warning_failure_cannot_interrupt_cleanup(guest, monkeypatch):
    _, _, objects, log = guest
    write_json(objects, {NAME: run_object()})
    behavior(guest, errors={f"stop -t 10 {DOCKER_ID}": "stop failed"},
             post_stop_error="inspection unavailable")
    warned = []

    def broken(message):
        assert ["rm", "-f", DOCKER_ID] in calls(log)
        warned.append(message)
        raise OSError("closed terminal")

    monkeypatch.setattr(docker, "warning", broken)
    assert docker._remove_run(RUN_ID, 10)
    assert warned


@pytest.mark.parametrize("rc", [0, 7, 124])
def test_cleanup_warning_preserves_run_result(guest, capsys, rc):
    config, _, _, _ = guest
    behavior(guest, run_rc=rc, errors={})
    original = host_runtime.inspect_object

    def unknown(name, *args, **kwargs):
        if name.startswith("djinn-run-"):
            raise OSError("unknown identity")
        return original(name, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(host_runtime, "inspect_object", unknown)
        result = docker.compose_run(config, docker.ContainerOptions(), interactive=False)
    assert result.returncode == rc
    assert "djinn clean" in capsys.readouterr().err


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP],
                         ids=["int", "term", "hup"])
@pytest.mark.parametrize("phase", ["preflight", "run"])
def test_signal_real_cli_cleans_up(guest, tmp_path, number, phase):
    _, binary, objects, log = guest
    home, state, config = (tmp_path / name for name in ("cli-home", "cli-state", "cli-config"))
    for path in (home, state, config):
        path.mkdir()
    behavior(guest, run_hang=True)
    if phase == "preflight":
        script = binary.read_text()
        marker = "    elif args[0] == 'compose' and 'config' in args:\n"
        assert marker in script
        binary.write_text(script.replace(marker, marker + '''\
        (store.parent / 'preflight-ready').write_text(args[-4])
        with log.open('a') as stream:
            stream.write(json.dumps(args) + '\\n')
        import time
        parent = os.getppid()
        while os.getppid() == parent:
            time.sleep(0.02)
        sys.exit(0)
'''))
    # The real CLI runs in-process in this child; only external preparation is stubbed.
    wrapper = '''
import os, pwd, signal, sys
sys.path = [entry for entry in sys.path if entry not in ("", os.getcwd())]
from pathlib import Path
from types import SimpleNamespace
pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=os.environ['DJINN_TEST_PASSWD_HOME'])
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.commands import agent
from djinn_in_a_box.core import docker
agent.load_config = lambda: AppConfig(code_dir=Path.cwd(),
                                      config_root=Path(os.environ['DJINN_CONFIG_ROOT']))
agent.prepare_config_workflow = lambda *a, **k: SimpleNamespace(success=True, notices=())
agent.workflow_image_compatible = lambda: True
docker.get_shell_mount_args = lambda *a: []
from djinn_in_a_box.cli.djinn import app
signal.signal(signal.SIGINT, signal.default_int_handler)
app()
'''
    environment = {
        **os.environ, "PATH": str(binary.parent) + os.pathsep + "/usr/bin:/bin",
        "HOME": str(home), "XDG_STATE_HOME": str(state), "DJINN_CONFIG_ROOT": str(config),
        "DJINN_TEST_PASSWD_HOME": str(home),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": str(Path(docker.__file__).resolve().parents[2]),
    }
    workspace = tmp_path / "cli-workspace"
    workspace.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", wrapper, "run", "codex", "inspect"],
        env=environment, cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        ready = binary.parent / ("preflight-ready" if phase == "preflight" else "ready")
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), child.communicate(timeout=2)
        overrides = list(tmp_path.glob("djinn-run-*.yml"))
        assert len(overrides) == 1
        if phase == "preflight":
            assert Path(ready.read_text()) == overrides[0]
        child.send_signal(number)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 130, (stdout, stderr)
        assert not overrides[0].exists()
        if phase == "run":
            name = ready.read_text()
            identity = "guest-" + name.removeprefix("djinn-run-")
            assert ["stop", "-t", "10", identity] in calls(log)
            assert ["rm", "-f", identity] in calls(log)
        else:
            assert not any(c[0] in {"stop", "rm"} or "run" in c for c in calls(log))
            assert not any(
                c[:2] == ["container", "inspect"] and c[2].startswith("djinn-run-")
                for c in calls(log)
            )
        assert json.loads(objects.read_text()) == {}
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize("agent_state", [False, True])
def test_clean_removes_runs_before_agent_check(guest, monkeypatch, agent_state):
    _, _, objects, log = guest
    write_json(objects, {NAME: run_object(), "run-network": {
        "Id": "network-id", "Labels": {"com.docker.compose.project": docker.COMPOSE_PROJECT,
                                        "com.docker.compose.network": "default"},
    }})
    data = json.loads(objects.read_text())
    data["djinn-in-a-box_default"] = data.pop("run-network")
    write_json(objects, data)
    state = {"generation": "generation", "resources": {}, "observer_pid": -1,
             "observer_token": "token", "agent_docker": {}} if agent_state else None
    monkeypatch.setattr(host_runtime, "read_state", lambda root: state)
    monkeypatch.setattr(host_runtime, "cleanup_owned", lambda *a: True)
    monkeypatch.setattr(host_runtime, "clear_state", lambda *a: None)
    monkeypatch.setattr(hostctl, "stop_helper_locked", lambda **k: None)
    monkeypatch.setattr(docker, "is_own_container", lambda name: False)
    recorded = []

    def record(*args, **kwargs):
        recorded.append(NAME in json.loads(objects.read_text()))

    monkeypatch.setattr(post_mortem, "record", record)

    def down(*args, **kwargs):
        assert not json.loads(objects.read_text()).get(NAME)
        return docker.RunResult(0)

    monkeypatch.setattr(docker, "_run_compose", down)
    assert docker.compose_down().success
    assert recorded == [True]
    commands = calls(log)
    assert [
        "ps", "-aq", "--no-trunc", "--filter",
        "label=com.docker.compose.project=djinn-in-a-box", "--filter", "label=djinn.run",
    ] in commands
    assert ["stop", "-t", "3", DOCKER_ID] in commands
    assert ["rm", "-f", DOCKER_ID] in commands
    assert ["network", "rm", "network-id"] in commands
    if agent_state:
        listings = [i for i, c in enumerate(commands) if c[0] == "ps"]
        assert commands.index(["rm", "-f", DOCKER_ID]) < listings[-1]


@pytest.mark.parametrize("agent_state", [False, True])
@pytest.mark.parametrize("foreign_label", ["project", "network"])
def test_clean_warns_for_foreign_run_and_network(
    guest, monkeypatch, capsys, agent_state, foreign_label,
):
    _, binary, objects, log = guest
    obj = run_object()
    obj["Config"]["Labels"][host_runtime.RUN_LABEL] = "aaaaaaaaaaaa"
    write_json(objects, {NAME: obj, "djinn-in-a-box_default": {
        "Id": "foreign-network", "Labels": {"com.docker.compose.project": "foreign",
                                             "com.docker.compose.network": "default"},
    }})
    data = json.loads(objects.read_text())
    data["djinn-in-a-box_default"]["Labels"] = {
        "com.docker.compose.project": (
            "foreign" if foreign_label == "project" else docker.COMPOSE_PROJECT
        ),
        "com.docker.compose.network": "foreign" if foreign_label == "network" else "default",
    }
    write_json(objects, data)
    state = {"generation": "generation", "resources": {}, "agent_docker": {}}
    monkeypatch.setattr(host_runtime, "read_state", lambda root: state if agent_state else None)
    monkeypatch.setattr(hostctl, "stop_helper_locked", lambda **k: None)
    monkeypatch.setattr(docker, "is_own_container", lambda name: False)
    down = Mock(side_effect=lambda *a, **k: docker._run_captured([str(binary), "compose-down"]))
    monkeypatch.setattr(docker, "_run_compose", down)
    result = docker.compose_down()
    assert result.success is (not agent_state)
    assert not any(c[0] in {"stop", "rm"} or c[:2] == ["network", "rm"] for c in calls(log))
    assert "Skipping" in capsys.readouterr().err
    if agent_state:
        assert "unknown project resources" in result.stderr
        down.assert_not_called()
        assert NAME in json.loads(objects.read_text())
    else:
        down.assert_called_once()
        assert NAME not in json.loads(objects.read_text())
        assert "djinn-in-a-box_default" in json.loads(objects.read_text())


@pytest.mark.parametrize("fault", ["listing", "removal"])
def test_clean_run_failure_preserves_resources(guest, monkeypatch, fault):
    _, _, objects, log = guest
    write_json(objects, {NAME: run_object()})
    monkeypatch.setattr(host_runtime, "read_state", lambda root: None)
    monkeypatch.setattr(docker, "is_own_container", lambda name: False)
    monkeypatch.setattr(hostctl, "stop_helper_locked", Mock(side_effect=AssertionError))
    if fault == "listing":
        execute = docker._run_captured

        def capture(argv, **kwargs):
            if f"label={host_runtime.RUN_LABEL}" in argv:
                assert kwargs["timeout"] == 5
                return docker.RunResult(124)
            return execute(argv, **kwargs)

        monkeypatch.setattr(docker, "_run_captured", capture)
    else:
        behavior(guest, errors={f"rm -f {DOCKER_ID}": "permission denied"})
    assert docker.compose_down().returncode == 1
    assert json.loads(objects.read_text())
    assert not any(c[0] == "compose-down" for c in calls(log))


def test_network_absence_uses_engine_wording(guest, monkeypatch):
    monkeypatch.setattr(host_runtime, "read_state", lambda root: None)
    monkeypatch.setattr(hostctl, "stop_helper_locked", lambda **k: None)
    monkeypatch.setattr(docker, "is_own_container", lambda name: False)
    monkeypatch.setattr(docker, "_run_compose", lambda *a, **k: docker.RunResult(0))
    assert docker.compose_down().success


def test_fake_storage_roots_and_filters(guest):
    _, binary, objects, _ = guest
    info = subprocess.run([str(binary), "info", "--format", "{{json .}}"],
                          capture_output=True, text=True, check=True)
    assert json.loads(info.stdout)["DockerRootDir"].endswith("docker-data")
    write_json(objects, {"volume": {"Id": "volume-id", "Mountpoint": "/backing"}})
    assert host_runtime.inspect_object("volume", str(binary), "volume")["Mountpoint"] == "/backing"
    assert host_runtime.inspect_object("absent", str(binary), "volume") is None
    unlabelled, foreign = run_object(), run_object()
    unlabelled["Id"] = "unlabelled-id"
    unlabelled["Config"]["Labels"].pop(host_runtime.RUN_LABEL)
    foreign["Id"] = "foreign-id"
    foreign["Config"]["Labels"]["com.docker.compose.project"] = "foreign"
    write_json(objects, {NAME: run_object(), "unlabelled": unlabelled, "foreign": foreign})
    listed = docker._run_captured([
        str(binary), "ps", "-aq", "--no-trunc", "--filter",
        "label=com.docker.compose.project=djinn-in-a-box", "--filter", "label=djinn.run",
    ])
    assert listed.success and listed.stdout.split() == [DOCKER_ID]


def test_preflight_interrupt_is_not_a_refusal(guest_compose, monkeypatch):
    config, _, _ = guest_compose
    monkeypatch.setattr(docker, "_run_compose", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        docker._run_preflight([], config, Path("/project"), {})


def test_cleanup_masks_signals_and_bounds_commands(guest, monkeypatch):
    _, _, objects, _ = guest
    write_json(objects, {NAME: run_object()})
    execute = subprocess.run
    observed = []

    def run(argv, **kwargs):
        handlers = [signal.getsignal(number)
                    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)]
        observed.append((argv, kwargs, handlers))
        return execute(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert docker._remove_run(RUN_ID, 10)
    assert len(observed) == 4
    for argv, kwargs, handlers in observed:
        assert all(handler is signal.SIG_IGN for handler in handlers)
        assert kwargs["timeout"] == (15 if argv[1] in {"stop", "rm"} else 5)


def test_guest_source_hook_precedes_validation_and_receives_resolved_config(guest, monkeypatch):
    config, _, _, _ = guest
    events = []
    validate = docker.validate_managed_bind_sources

    def source(config_arg, options, **kwargs):
        assert config_arg is config
        events.append("resolved" if kwargs else "user")
        if kwargs:
            assert kwargs["resolved"] == VALID
            assert "-T" in kwargs["mount_args"]

    def checked(config_arg):
        events.append("validation")
        return validate(config_arg)

    monkeypatch.setattr(docker, "_guest_source_check", source)
    monkeypatch.setattr(docker, "validate_managed_bind_sources", checked)
    assert docker.compose_run(config, docker.ContainerOptions(), interactive=False).success
    assert events == ["user", "validation", "resolved"]


def test_guest_execution_converts_term_and_hup(guest, monkeypatch):
    config, _, _, _ = guest
    execute = subprocess.run
    before = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
    checked = []

    def run(argv, **kwargs):
        if "compose" in argv and "run" in argv:
            for number in before:
                handler = signal.getsignal(number)
                assert handler is not before[number] and callable(handler)
                with pytest.raises(KeyboardInterrupt):
                    handler(number, None)
            checked.append(True)
        return execute(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert docker.compose_run(config, docker.ContainerOptions(), interactive=False).success
    assert checked == [True]


def test_clean_network_remove_failure(guest, monkeypatch, capsys):
    _, _, objects, _ = guest
    write_json(objects, {"djinn-in-a-box_default": {
        "Id": "network-id", "Labels": {"com.docker.compose.project": docker.COMPOSE_PROJECT,
                                        "com.docker.compose.network": "default"},
    }})
    behavior(guest, errors={"network rm network-id": "permission denied"})
    monkeypatch.setattr(host_runtime, "read_state", lambda root: None)
    monkeypatch.setattr(hostctl, "stop_helper_locked", lambda **k: None)
    monkeypatch.setattr(docker, "is_own_container", lambda name: False)
    monkeypatch.setattr(docker, "_run_compose", lambda *a, **k: docker.RunResult(0))
    assert docker.compose_down().returncode == 1
    assert "Failed to delete network" in capsys.readouterr().err
    assert "djinn-in-a-box_default" in json.loads(objects.read_text())
