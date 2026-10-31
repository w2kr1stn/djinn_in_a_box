"""Assistant contract tests, using real Docker result shapes and closed capture stdin."""

from __future__ import annotations

import csv
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import agent_versions, paths
from djinn_in_a_box.core import assistant as a
from djinn_in_a_box.core import docker as docker_module
from djinn_in_a_box.core import opencode_credentials as oc
from djinn_in_a_box.core.exceptions import ConfigValidationError, ZoneConfigurationError

AUDIT_CONFIG = a.audit_config
LOCAL_DAEMON = a.local_daemon


class Docker:
    def __init__(self):
        self.calls = []
        self.image = None
        self.containers = {}
        self.status = 0
        self.build_status = 0
        self.network = True
        self.network_failure = False
        self.daemon_failure = False
        self.keep = False
        self.fail_remove = False
        self.interrupt = False
        self.exports = []
        self.stubborn = False
        self.on_run = None
        self.removing = 0
        self.stop_error = ""

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        args = argv[1:]
        if args[0] == "run":
            assert "stdin" not in kwargs and "capture_output" not in kwargs
            if self.on_run is not None:
                self.on_run(argv)
            name = args[args.index("--name") + 1]
            obj = {
                "Id": "a" * 64,
                "Config": {"Labels": {a.SESSION_LABEL: name}},
                "State": {"Running": True},
            }
            if self.keep or self.interrupt:
                self.containers[name] = obj
            if self.interrupt:
                raise KeyboardInterrupt
            return subprocess.CompletedProcess(argv, self.status)
        assert kwargs["stdin"] is subprocess.DEVNULL
        if args[:2] == ["buildx", "build"]:
            if self.build_status == 0:
                content = args[args.index("--label") + 1].split("=", 1)[1]
                self.image = {
                    "Id": "sha256:" + content,
                    "Config": {"Labels": {a.CONTENT_LABEL: content}},
                }
            return subprocess.CompletedProcess(argv, self.build_status)
        if self.daemon_failure:
            return subprocess.CompletedProcess(argv, 1, "", "Cannot connect to the Docker daemon")
        if args[:2] == ["image", "inspect"]:
            if self.image is None:
                return subprocess.CompletedProcess(argv, 1, "[]", f"No such image: {args[2]}")
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.image]), "")
        if args[:2] == ["container", "inspect"]:
            if args[2] in self.containers and self.removing:
                # An auto-removal in progress: the container vanishes after some inspects.
                self.removing -= 1
                if not self.removing:
                    del self.containers[args[2]]
            if args[2] not in self.containers:
                return subprocess.CompletedProcess(argv, 1, "[]", f"No such container: {args[2]}")
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.containers[args[2]]]), "")
        if args[0] == "stop" and self.stop_error:
            return subprocess.CompletedProcess(argv, 1, "", self.stop_error)
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(
                argv,
                0 if self.network else 1,
                json.dumps([{"Name": args[2]}]) if self.network else "[]",
                "" if self.network else f"network {args[2]} not found",
            )
        if args[:2] == ["network", "create"]:
            self.network = not self.network_failure
            return subprocess.CompletedProcess(
                argv,
                int(self.network_failure),
                "network-id",
                "permission denied" if self.network_failure else "",
            )
        if args[0] == "ps" and f"label={docker_module.CREDENTIAL_EXPORT_LABEL}=" in args[-1]:
            return subprocess.CompletedProcess(argv, 0, "".join(f"{i}\n" for i in self.exports), "")
        if args[0] == "rm" and args[-1] in self.exports:
            if not self.stubborn:
                self.exports = [i for i in self.exports if i not in args[2:]]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[0] == "rm":
            if self.fail_remove:
                return subprocess.CompletedProcess(argv, 1, "", "removal failed")
            if self.removing:
                message = f"Error response from daemon: removal of container {args[-1]}"
                return subprocess.CompletedProcess(argv, 1, "", f"{message} is already in progress")
            self.containers = {
                name: obj for name, obj in self.containers.items() if obj["Id"] != args[2]
            }
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def installation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    project = tmp_path / 'project, "quoted"'
    project.mkdir()
    real = Path(__file__).resolve().parents[2]
    (project / "Dockerfile").write_text(
        "ARG CLAUDE_CODE_VERSION=2.1.288\nARG CODEX_VERSION=0.160.0\n"
        "ARG OPENCODE_VERSION=1.18.34\nARG DOCKER_VERSION=27.4.1\n"
    )
    (project / "assistant").mkdir()
    for name in (
        "Dockerfile", "entrypoint.sh", "install-opencode.sh", "install-claude.sh",
        "audit-briefing.md",
    ):
        (project / "assistant" / name).write_bytes((real / "assistant" / name).read_bytes())
    sock = socket.socket(socket.AF_UNIX)
    sock_path = tmp_path / "docker.sock"
    sock.bind(str(sock_path))
    monkeypatch.setattr(a, "local_daemon", lambda: (sock_path, "linux/amd64"))
    monkeypatch.setattr(a, "get_project_root", lambda: project)
    config_dir = home / ".config/djinn_in_a_box"
    monkeypatch.setattr(a, "CONFIG_DIR", config_dir)
    config = AppConfig(code_dir=tmp_path, config_root=home / ".djinn/config")
    monkeypatch.setattr(a, "audit_config", lambda: (config, ""))
    monkeypatch.setattr(
        a,
        "sys",
        SimpleNamespace(
            platform="linux",
            stdin=SimpleNamespace(isatty=lambda: True),
            stdout=SimpleNamespace(isatty=lambda: True),
        ),
    )
    monkeypatch.setattr(a, "is_background_process_group", lambda: False)
    fake = Docker()
    monkeypatch.setattr(subprocess, "run", fake.run)
    yield SimpleNamespace(
        home=home,
        project=project,
        config=config,
        directory=config_dir,
        socket=sock_path,
        docker=fake,
    )
    sock.close()


def run_call(fixture):
    return next(argv for argv, _ in fixture.docker.calls if argv[1] == "run")


@pytest.mark.parametrize("agent", ("claude", "codex", "opencode"))
def test_launch_contract(installation, agent, monkeypatch):
    f = installation
    monkeypatch.setenv("SECRET_KEY", "must-not-forward")
    monkeypatch.setenv("TERM", "xterm")
    prompt = '-failure\n"quotes" `echo forbidden` $(touch forbidden); tail'
    assert a.run_audit(prompt, agent) == 0
    argv = run_call(f)
    assert all(flag in argv for flag in ("--rm", "--interactive", "--tty"))
    assert argv[argv.index("--network") + 1] == a.DJINN_NETWORK
    assert argv[argv.index("--workdir") + 1] == str(f.project)
    assert argv[argv.index("--user") + 1] == f"{a.os.getuid()}:{a.os.getgid()}"
    assert argv[argv.index("--group-add") + 1] == str(f.socket.stat().st_gid)
    assert argv[-1].endswith(prompt)
    assert "Djinn installation audit" in argv[-1]
    assert not (f.project / "forbidden").exists()
    assert "TERM=xterm" in argv and not any("SECRET_KEY" in v for v in argv)
    assert f.docker.image["Id"] in argv and a.ASSISTANT_IMAGE not in argv
    expected = {
        "claude": ["claude", "--permission-mode", "manual", "--safe-mode"],
        "codex": ["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "on-request"],
        "opencode": ["opencode", "--standalone", "--prompt"],
    }
    assert argv[-len(expected[agent]) - 1 : -1] == expected[agent]
    assert (
        'OPENCODE_CONFIG_CONTENT={"permissions":[{"action":"edit","resource":"*","effect":"ask"},'
        '{"action":"shell","resource":"*","effect":"ask"}]}'
    ) in argv
    assert "OPENCODE_DISABLE_PROJECT_CONFIG=true" in argv


def test_config_selection_general_and_override(installation, monkeypatch):
    f = installation
    config = AppConfig.model_validate({**f.config.model_dump(), "assistant": {"agent": "opencode"}})
    monkeypatch.setattr(a, "audit_config", lambda: (config, ""))
    a.run_audit(None)
    assert run_call(f)[-4:-1] == ["opencode", "--standalone", "--prompt"]
    assert "general installation health check" in run_call(f)[-1]
    f.docker.calls.clear()
    a.run_audit(None, "codex")
    assert "codex" in run_call(f) and config.assistant.agent == "opencode"


def test_invalid_agent_before_docker(installation):
    with pytest.raises(ValueError):
        a.run_audit(None, "unknown")
    assert installation.docker.calls == []


def test_exact_mounts_and_credentials(installation, monkeypatch):
    f = installation
    custom = f.home / "external-creds/codex"
    custom.mkdir(parents=True)
    (custom / "auth.json").write_text('{"token":"fixture"}')
    (custom / "config.toml").write_text('approval_policy = "never"')
    monkeypatch.setenv("DJINN_CONFIG_ROOT", str(custom.parent))
    a.run_audit(None, "codex")
    argv = run_call(f)
    mounts = [next(csv.reader([argv[i + 1]])) for i, flag in enumerate(argv) if flag == "--mount"]
    assert mounts == [
        ["type=bind", f"src={f.project}", f"dst={f.project}"],
        ["type=bind", f"src={f.directory}", f"dst={f.directory}"],
        ["type=bind", f"src={f.home}/.djinn", f"dst={f.home}/.djinn"],
        ["type=bind", f"src={f.socket}", "dst=/var/run/docker.sock"],
        ["type=bind", f"src={custom}/auth.json", "dst=/run/djinn-credentials/auth.json"],
    ]


def test_redirected_credentials_refused(installation):
    f = installation
    root = f.config.config_root / "codex"
    root.mkdir(parents=True)
    (root / "auth.json").symlink_to(f.home / "missing")
    with pytest.raises(a.AssistantError, match="regular file"):
        a.run_audit(None, "codex")
    assert f.docker.calls == []


@pytest.mark.parametrize("broken", ("TOML", "zones", "code_dir"))
def test_broken_config_loads_default_session(installation, monkeypatch, broken):
    f = installation
    agent_versions.save_versions({"CLAUDE_CODE_VERSION": "2.1.300"})
    # Exercise the actual recovery, not a stub returning a hand-built fallback.
    monkeypatch.setattr(a, "audit_config", AUDIT_CONFIG)

    def load():
        if broken == "zones":
            return f.config
        raise ConfigValidationError(f"Invalid {broken}: fixture failure")

    monkeypatch.setattr(a, "load_config", load)

    def zones(config):
        raise ZoneConfigurationError("Invalid zones: fixture failure")

    monkeypatch.setattr(a, "load_zone_assignments", zones)
    assert a.run_audit("investigate") == 0
    argv = run_call(f)
    assert "claude" in argv
    assert f"Invalid {broken}" in argv[-1]
    assert "investigate" in argv[-1]
    assert len([flag for flag in argv if flag == "--mount"]) == 4
    build = next(argv for argv, _ in f.docker.calls if argv[1] == "buildx")
    assert "AGENT_VERSION=2.1.300" in build


def test_cache_rebuilds_only_changed_inputs(installation):
    f = installation
    first = a.ensure_image(f.project, "claude", "linux/amd64", "default")
    assert a.ensure_image(f.project, "claude", "linux/amd64", "default") == first

    def builds():
        return sum(argv[1:3] == ["buildx", "build"] for argv, _ in f.docker.calls)

    assert builds() == 1
    other = a.ensure_image(f.project, "codex", "linux/amd64", "default")
    assert other != first and builds() == 2
    path = f.project / "assistant/Dockerfile"
    path.write_text(path.read_text() + "\n# change\n")
    assert a.ensure_image(f.project, "codex", "linux/amd64", "default") != other
    assert builds() == 3
    pin = f.project / "Dockerfile"
    pin.write_text(pin.read_text().replace("0.160.0", "0.161.0"))
    a.ensure_image(f.project, "codex", "linux/amd64", "default")
    assert builds() == 4


@pytest.mark.parametrize("network", ("default", "host"))
def test_build_flags_and_pins(installation, network):
    f = installation
    a.ensure_image(f.project, "opencode", "linux/amd64", network)
    argv = next(argv for argv, _ in f.docker.calls if argv[1] == "buildx")
    assert argv[argv.index("--network") + 1] == network
    assert "--load" in argv and argv[argv.index("--platform") + 1] == "linux/amd64"
    assert "AGENT=opencode" in argv and "AGENT_VERSION=1.18.34" in argv
    assert "DOCKER_VERSION=27.4.1" in argv
    assert ("--allow" in argv) == (network == "host")
    assert argv[-1] == str(f.project / "assistant")


@pytest.mark.parametrize("pin", ("duplicate", "malformed", "missing"))
@pytest.mark.parametrize("name", ("CODEX_VERSION", "DOCKER_VERSION"))
def test_invalid_pins_abort(installation, pin, name):
    f = installation
    path = f.project / "Dockerfile"
    default = "0.160.0" if name == "CODEX_VERSION" else "27.4.1"
    value = {
        "duplicate": path.read_text() + f"ARG {name}={default}\n",
        "malformed": path.read_text().replace(default, "latest"),
        "missing": path.read_text().replace(f"ARG {name}", f"# ARG {name}"),
    }[pin]
    path.write_text(value)
    with pytest.raises(a.AssistantError, match="numeric x.y.z ARG") as exc:
        a.ensure_image(f.project, "codex", "linux/amd64", "default")
    assert str(path) in str(exc.value) and name in str(exc.value)
    assert f.docker.calls == []


@pytest.mark.parametrize(("agent", "arg", "new_version"), [
    ("claude", "CLAUDE_CODE_VERSION", "2.1.300"),
    ("codex", "CODEX_VERSION", "0.161.0"),
    ("opencode", "OPENCODE_VERSION", "1.18.40"),
])
def test_record_version_reaches_agent_arg_and_fingerprint(installation, agent, arg, new_version):
    f = installation
    first = a.ensure_image(f.project, agent, "linux/amd64", "default")
    first_fingerprint = a.label(f.docker.image, a.CONTENT_LABEL)
    agent_versions.save_versions({arg: new_version})
    updated = a.ensure_image(f.project, agent, "linux/amd64", "default")
    assert updated != first
    assert a.label(f.docker.image, a.CONTENT_LABEL) != first_fingerprint
    builds = [argv for argv, _ in f.docker.calls if argv[1:3] == ["buildx", "build"]]
    assert len(builds) == 2
    assert f"AGENT_VERSION={new_version}" in builds[-1]
    assert "DOCKER_VERSION=27.4.1" in builds[-1]
    assert a.ensure_image(f.project, agent, "linux/amd64", "default") == updated
    assert len([argv for argv, _ in f.docker.calls if argv[1] == "buildx"]) == 2


@pytest.mark.parametrize(("agent", "arg", "default"), [
    ("claude", "CLAUDE_CODE_VERSION", "2.1.288"),
    ("codex", "CODEX_VERSION", "0.160.0"),
    ("opencode", "OPENCODE_VERSION", "1.18.34"),
])
@pytest.mark.parametrize("local", ["equal", "lower"])
def test_inert_record_keeps_agent_arg_and_fingerprint(installation, agent, arg, default, local):
    f = installation
    first = a.ensure_image(f.project, agent, "linux/amd64", "default")
    fingerprint = a.label(f.docker.image, a.CONTENT_LABEL)
    another = next(key for key in agent_versions.KNOWN_AGENT_ARGS if key != arg)
    agent_versions.save_versions({arg: default if local == "equal" else "0.0.1", another: "99.0.0"})
    assert a.ensure_image(f.project, agent, "linux/amd64", "default") == first
    assert a.label(f.docker.image, a.CONTENT_LABEL) == fingerprint
    agent_versions.save_versions({another: "100.0.0"})
    assert a.ensure_image(f.project, agent, "linux/amd64", "default") == first
    builds = [argv for argv, _ in f.docker.calls if argv[1] == "buildx"]
    assert len(builds) == 1
    assert f"AGENT_VERSION={default}" in builds[0]


@pytest.mark.parametrize("failure", ["toml", "unknown", "value", "unselected", "read"])
def test_invalid_record_aborts_assistant_before_docker(installation, monkeypatch, failure):
    record = paths.AGENT_VERSIONS_FILE
    record.parent.mkdir(parents=True)
    record.write_text({"toml": "[", "unknown": 'DOCKER_VERSION = "29.0.0"',
                       "value": 'CLAUDE_CODE_VERSION = "beta"',
                       "unselected": 'OPENCODE_VERSION = "1.2.3-beta"', "read": ""}[failure])
    if failure == "read":
        original = Path.open

        def opened(self, *args, **kwargs):
            if self == record:
                raise OSError("record permission denied")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(a.AssistantError) as exc:
        a.ensure_image(installation.project, "claude", "linux/amd64", "default")
    assert str(record) in str(exc.value)
    assert installation.docker.calls == []


def test_failed_rebuild_never_launches(installation):
    f = installation
    a.ensure_image(f.project, "claude", "linux/amd64", "default")
    f.docker.build_status = 7
    with pytest.raises(a.AssistantError, match="build failed"):
        a.run_audit(None, "codex")
    assert not any(argv[1] == "run" for argv, _ in f.docker.calls)


def test_daemon_failure_not_missing_image(installation):
    installation.docker.daemon_failure = True
    with pytest.raises(a.AssistantError, match="Cannot connect"):
        a.run_audit(None)
    assert not any(argv[1] == "buildx" for argv, _ in installation.docker.calls)


def test_malformed_inspect_refused(installation, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, '[{"Config":null}]', ""),
    )
    with pytest.raises(a.AssistantError, match="Invalid Docker"):
        a.inspect("image", "fixture")


@pytest.mark.parametrize("failed", (False, True))
def test_network_creation(installation, failed):
    f = installation
    f.docker.network = False
    f.docker.network_failure = failed
    if failed:
        with pytest.raises(a.AssistantError, match="Could not ensure"):
            a.run_audit(None)
        assert not any(argv[1] == "run" for argv, _ in f.docker.calls)
    else:
        assert a.run_audit(None) == 0
    assert any(argv[1:3] == ["network", "create"] for argv, _ in f.docker.calls)


@pytest.mark.parametrize("status", (0, 19, 130))
def test_exit_cleanup_only_owned_container(installation, status):
    f = installation
    f.docker.keep = True
    f.docker.status = status
    f.docker.interrupt = status == 130
    peer = {
        "Id": "b" * 64,
        "Config": {"Labels": {a.SESSION_LABEL: "other"}},
        "State": {"Running": True},
    }
    f.docker.containers["other"] = peer
    assert a.run_audit(None) == status
    assert f.docker.containers == {"other": peer}
    assert any(argv[1:] == ["rm", "--force", "a" * 64] for argv, _ in f.docker.calls)


def test_cleanup_failure_is_visible(installation):
    f = installation
    f.docker.keep = f.docker.fail_remove = True
    with pytest.raises(a.AssistantError, match="removal failed"):
        a.run_audit(None)


def test_cleanup_refuses_foreign_label(installation):
    f = installation
    f.docker.containers["fixture"] = {
        "Id": "c" * 64,
        "Config": {"Labels": {}},
        "State": {"Running": True},
    }
    with pytest.raises(a.AssistantError, match="unknown ownership"):
        a.cleanup("fixture")
    assert not any(argv[1] == "rm" for argv, _ in f.docker.calls)


def _owned(f, name):
    f.docker.containers[name] = {
        "Id": "d" * 64,
        "Config": {"Labels": {a.SESSION_LABEL: name}},
        "State": {"Running": False},
    }


@pytest.fixture
def removal_clock(monkeypatch):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(a, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    return now


def test_cleanup_waits_for_an_auto_removal_already_in_progress(installation, removal_clock):
    f = installation
    _owned(f, "fixture")
    f.docker.removing = 4
    a.cleanup("fixture")
    assert removal_clock[0] > 0
    assert "fixture" not in f.docker.containers


def test_cleanup_fails_visibly_when_an_auto_removal_never_ends(installation, removal_clock):
    f = installation
    _owned(f, "fixture")
    f.docker.removing = 10**9
    with pytest.raises(a.AssistantError, match="cleanup failed"):
        a.cleanup("fixture")
    assert removal_clock[0] >= a.REMOVAL_WAIT_SECONDS


@pytest.mark.parametrize(
    "stderr", ("No such container: fixture", "removal of container x is already in progress")
)
def test_stop_tolerates_a_container_that_is_gone_or_being_removed(installation, stderr):
    installation.docker.stop_error = f"Error response from daemon: {stderr}"
    a.stop_container("fixture")


def test_stop_reports_other_failures(installation):
    installation.docker.stop_error = "Error response from daemon: cannot stop"
    with pytest.raises(a.AssistantError, match="cannot stop"):
        a.stop_container("fixture")


def test_terminal_required_before_docker(installation, monkeypatch):
    monkeypatch.setattr(a.sys.stdin, "isatty", lambda: False)
    with pytest.raises(a.AssistantError, match="foreground terminal"):
        a.run_audit(None)
    assert installation.docker.calls == []


def test_remote_context_refused(installation, monkeypatch):
    # Restore the real endpoint resolver for this isolated CLI response.
    monkeypatch.setattr(a, "local_daemon", LOCAL_DAEMON)
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")
    monkeypatch.setenv("DOCKER_HOST", "unix:///should-not-win.sock")
    monkeypatch.setattr(
        a,
        "capture",
        lambda *args: json.dumps([{"Endpoints": {"docker": {"Host": "ssh://remote"}}}]),
    )
    with pytest.raises(a.AssistantError, match="TCP/SSH"):
        a.local_daemon()


def test_local_socket_and_platform(installation, monkeypatch):
    monkeypatch.setattr(a, "local_daemon", LOCAL_DAEMON)
    monkeypatch.setenv("DOCKER_HOST", "unix://" + str(installation.socket))
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(a, "capture", lambda *args: "linux/x86_64\n")
    assert a.local_daemon() == (installation.socket, "linux/amd64")


@pytest.mark.parametrize("agent", ["opencode", "claude"])
def test_opencode_installer_change_invalidates_assistant_image(installation, agent):
    f = installation
    first = a.ensure_image(f.project, agent, "linux/amd64", "default")
    script = f.project / f"assistant/install-{agent}.sh"
    script.write_text(script.read_text() + "\n# changed installer\n")
    second = a.ensure_image(f.project, agent, "linux/amd64", "default")
    assert first != second


def test_audit_image_owns_the_session_bind_targets_before_switching_user():
    dockerfile = (Path(__file__).parents[2] / "assistant" / "Dockerfile").read_text()
    created = dockerfile.index("RUN install -d -o dev -g dev /home/dev/.local ")
    end = dockerfile.index("\n", dockerfile.index("/home/dev/.cache", created))
    command = dockerfile[created:end]
    for target in ("/home/dev/.local/share/opencode", "/home/dev/.local/state", "/home/dev/.cache"):
        assert target in command
    assert dockerfile.index("useradd") < created < dockerfile.index("\nUSER dev")


# --- One-off credential export adapter ----------------------------------------------------------

DATA_TARGET = "/home/dev/.local/share/opencode"
REAL_POPEN = subprocess.Popen


def child_script(body):
    return [sys.executable, "-c", "import sys, time\n" + body]


@pytest.fixture
def export_child(installation, monkeypatch):
    """Replace the Docker client process by a real child; records how it was started."""
    started = []
    script = {"body": "sys.stdout.write('[]\\n')"}

    def popen(argv, **kwargs):
        started.append((argv, kwargs))
        return REAL_POPEN(child_script(script["body"]), **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    installation.started = started
    installation.script = script
    return installation


def export_argv(f):
    return f.started[0][0]


def option(argv, flag):
    return [argv[i + 1] for i, item in enumerate(argv) if item == flag]


def mount_of(argv):
    return next(csv.reader([option(argv, "--mount")[0]]))


def test_export_argv_contract_bind(export_child, tmp_path):
    f = export_child
    source = tmp_path / 'data, "dir"'
    source.mkdir()
    out = docker_module.export_opencode_credentials(
        image="sha256:fixture", source=str(source), kind="bind"
    )
    assert out == b"[]\n"
    argv = export_argv(f)
    assert argv[1:3] == ["run", "--rm"]
    name = option(argv, "--name")[0]
    token = name.removeprefix("djinn-credexport-")
    assert name != token and len(token) >= 16
    assert option(argv, "--label") == [f"djinn.credential-export={token}"]
    assert option(argv, "--network") == ["none"]
    assert option(argv, "--log-driver") == ["none"]
    assert option(argv, "--cap-drop") == ["ALL"]
    assert option(argv, "--security-opt") == ["no-new-privileges"]
    assert int(option(argv, "--pids-limit")[0]) > 0
    assert option(argv, "--memory")[0]
    assert option(argv, "--user") == [f"{os.getuid()}:{os.getgid()}"]
    assert option(argv, "--entrypoint") == ["opencode"]
    assert argv[-4:] == ["sha256:fixture", "auth", "export", "--standalone"]
    # Read-write on purpose: a read-only data directory makes OpenCode exit before it is ready.
    assert mount_of(argv) == ["type=bind", f"src={source}", f"dst={DATA_TARGET}"]
    assert len(option(argv, "--mount")) == 1
    assert not any("docker.sock" in item or "readonly" in item for item in argv)
    assert "--volume" not in argv and "-v" not in argv and "--privileged" not in argv
    env = dict(item.split("=", 1) for item in option(argv, "--env"))
    config_home = env["XDG_CONFIG_HOME"]
    assert option(argv, "--workdir") == [config_home]
    assert option(argv, "--tmpfs")[0].startswith(config_home + ":")
    assert {k: v for k, v in env.items() if k != "XDG_CONFIG_HOME"} == {
        "HOME": "/home/dev",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
        "DO_NOT_TRACK": "1",
    }
    kwargs = f.started[0][1]
    assert kwargs["stdout"] is subprocess.PIPE and kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["stdin"] is subprocess.DEVNULL and "env" not in kwargs


def test_export_argv_contract_volume_runs_as_image_user(export_child):
    docker_module.export_opencode_credentials(
        image="sha256:fixture", source="djinn-opencode-data", kind="volume"
    )
    argv = export_argv(export_child)
    assert "--user" not in argv
    assert mount_of(argv) == ["type=volume", "src=djinn-opencode-data", f"dst={DATA_TARGET}"]
    assert option(argv, "--network") == ["none"] and option(argv, "--log-driver") == ["none"]


@pytest.mark.parametrize(
    ("image", "source", "kind"),
    (("", "/x", "bind"), ("img", "", "bind"), ("img", "relative", "bind"), ("img", "/x", "other")),
)
def test_export_refuses_invalid_request_before_docker(export_child, image, source, kind):
    with pytest.raises(docker_module.CredentialExportError, match="invalid export request"):
        docker_module.export_opencode_credentials(image=image, source=source, kind=kind)
    assert export_child.started == [] and export_child.docker.calls == []


def test_export_requires_local_daemon(export_child, monkeypatch):
    def remote():
        raise a.AssistantError("TCP/SSH contexts are unsupported")

    monkeypatch.setattr(a, "local_daemon", remote)
    with pytest.raises(docker_module.CredentialExportError, match="local Docker daemon"):
        docker_module.export_opencode_credentials(image="img", source="v", kind="volume")
    assert export_child.started == []


def test_export_output_is_bounded(export_child):
    limit = oc.EXPORT_LIMIT
    export_child.script["body"] = f"sys.stdout.buffer.write(b'x' * {limit})"
    exported = docker_module.export_opencode_credentials(
        image="img", source="v", kind="volume", timeout=30
    )
    assert len(exported) == limit
    export_child.script["body"] = f"sys.stdout.buffer.write(b'x' * {limit + 1})"
    with pytest.raises(docker_module.CredentialExportError, match="size limit"):
        docker_module.export_opencode_credentials(
            image="img", source="v", kind="volume", timeout=30
        )


def test_export_failure_names_only_the_status(export_child):
    sentinel = "sentinel-" + secrets.token_hex(8)
    export_child.script["body"] = f"sys.stdout.write({sentinel!r}); sys.exit(7)"
    with pytest.raises(docker_module.CredentialExportError) as failure:
        docker_module.export_opencode_credentials(image="img", source="v", kind="volume")
    assert str(failure.value) == "export exited with status 7"
    assert sentinel not in repr(failure.value) and sentinel not in repr(failure.value.__cause__)


def test_export_sentinel_never_reaches_argv_env_or_messages(export_child):
    sentinel = "sentinel-" + secrets.token_hex(8)
    export_child.script["body"] = f"sys.stdout.write({sentinel!r})"
    out = docker_module.export_opencode_credentials(image="img", source="v", kind="volume")
    # Positive control: the value is visible where it is allowed to be, and the check sees it.
    assert sentinel.encode() in out
    argv, kwargs = export_child.started[0]
    surfaces = "\0".join([*argv, *map(repr, kwargs.values())])
    assert sentinel in "\0".join([surfaces, sentinel]) and sentinel not in surfaces
    export_child.script["body"] = f"sys.stdout.write({sentinel!r}); sys.exit(3)"
    with pytest.raises(docker_module.CredentialExportError) as failure:
        docker_module.export_opencode_credentials(image="img", source="v", kind="volume")
    assert sentinel not in str(failure.value)
    assert all(sentinel not in "\0".join(c[0]) for c in export_child.docker.calls)


def test_export_removes_by_label_after_success(export_child):
    export_child.docker.exports = ["e" * 64]
    docker_module.export_opencode_credentials(image="img", source="v", kind="volume")
    token = option(export_argv(export_child), "--label")[0].split("=", 1)[1]
    queries = [argv for argv, _ in export_child.docker.calls if argv[1] == "ps"]
    assert queries and all(f"label=djinn.credential-export={token}" in argv for argv in queries)
    assert any(argv[1:3] == ["rm", "--force"] for argv, _ in export_child.docker.calls)
    assert export_child.docker.exports == []


def test_export_timeout_removes_by_label_and_fails(export_child):
    export_child.script["body"] = "time.sleep(30)"
    export_child.docker.exports = ["e" * 64]
    started = time.monotonic()
    with pytest.raises(docker_module.CredentialExportError, match="timed out"):
        docker_module.export_opencode_credentials(
            image="img", source="v", kind="volume", timeout=0.5
        )
    assert time.monotonic() - started < 10
    assert export_child.docker.exports == []
    assert any(argv[1:3] == ["rm", "--force"] for argv, _ in export_child.docker.calls)


def test_export_abort_check_fails_visibly_when_a_container_is_left(export_child, monkeypatch):
    monkeypatch.setattr(docker_module, "_EXPORT_CLEANUP_PAUSE", 0.0)
    export_child.docker.exports = ["e" * 64]
    export_child.docker.stubborn = True
    with pytest.raises(docker_module.CredentialExportError, match="could not be removed"):
        docker_module.export_opencode_credentials(image="img", source="v", kind="volume")
    # The abort check looked again after the removal attempt.
    assert len([c for c, _ in export_child.docker.calls if c[1] == "ps"]) >= 2


def test_export_unverifiable_state_fails(export_child):
    export_child.docker.daemon_failure = True
    with pytest.raises(docker_module.CredentialExportError, match="could not be verified"):
        docker_module.export_opencode_credentials(image="img", source="v", kind="volume")


def test_export_interrupt_still_removes_by_label(export_child, monkeypatch):
    export_child.docker.exports = ["e" * 64]

    def interrupted(process, output, deadline):
        raise KeyboardInterrupt

    monkeypatch.setattr(docker_module, "_read_export", interrupted)
    with pytest.raises(KeyboardInterrupt):
        docker_module.export_opencode_credentials(image="img", source="v", kind="volume")
    assert export_child.docker.exports == []


# --- OpenCode audit sessions ----------------------------------------------------------------------


def credential(identity, integration="prov_a"):
    return {
        "id": identity,
        "integrationID": integration,
        "label": "fixture",
        "active": True,
        "value": {"type": "key", "key": secrets.token_hex(8)},
    }


def audit_root(f):
    return f.home / ".local/state/djinn/opencode-audit"


def carrier_dir(f):
    path = f.config.config_root / "opencode"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_carrier(f, entries):
    path = carrier_dir(f) / "credentials.json"
    path.write_text(json.dumps(entries))
    path.chmod(0o600)


def carrier_ids(f):
    path = carrier_dir(f) / "credentials.json"
    return {item["id"] for item in json.loads(path.read_text())} if path.exists() else set()


def mounts_of(argv):
    return [next(csv.reader([argv[i + 1]])) for i, flag in enumerate(argv) if flag == "--mount"]


@pytest.fixture
def warnings_seen(monkeypatch):
    seen = []
    monkeypatch.setattr(a, "warning", seen.append)
    return seen


@pytest.fixture
def at_run(installation):
    """What the host side looks like at the moment `docker run` starts."""
    seen = {}

    def on_run(argv):
        seen["argv"] = argv
        seen["mounts"] = mounts_of(argv)
        for mount in seen["mounts"]:
            if mount[2] == "dst=/run/djinn-credentials":
                seed = Path(mount[1].removeprefix("src=")) / "seed.json"
                seen["seed"] = seed.read_bytes() if seed.exists() else None
                seen["seed_mode"] = stat.S_IMODE(seed.stat().st_mode) if seed.exists() else None
        seen["calls_before"] = len(installation.docker.calls)

    installation.docker.on_run = on_run
    installation.seen = seen
    return installation


def test_credentials_table(installation):
    assert a.CREDENTIALS == {
        "claude": (".credentials.json", "claude.json"),
        "codex": ("auth.json",),
        "opencode": (),
    }


def test_opencode_audit_mounts_exactly(at_run, warnings_seen):
    f = at_run
    write_carrier(f, [credential("cred_one")])
    assert a.run_audit(None, "opencode") == 0
    argv = f.seen["argv"]
    name = argv[argv.index("--name") + 1]
    session = audit_root(f) / name.removeprefix("djinn-assistant-")
    assert f.seen["mounts"] == [
        ["type=bind", f"src={f.project}", f"dst={f.project}"],
        ["type=bind", f"src={f.directory}", f"dst={f.directory}"],
        ["type=bind", f"src={f.home}/.djinn", f"dst={f.home}/.djinn"],
        ["type=bind", f"src={f.socket}", "dst=/var/run/docker.sock"],
        ["type=bind", f"src={session}/io", "dst=/run/djinn-credentials"],
        ["type=bind", f"src={session}/data", f"dst={DATA_TARGET}"],
    ]
    assert argv[argv.index("--label") + 1] == f"{a.SESSION_LABEL}={name}"
    assert len(name.removeprefix("djinn-assistant-")) == 32
    assert warnings_seen == []


def test_opencode_seed_is_written_before_docker_run(at_run):
    f = at_run
    write_carrier(f, [credential("cred_one"), credential("cred_two", "prov_b")])
    a.run_audit(None, "opencode")
    seed = json.loads(f.seen["seed"])
    assert {item["id"] for item in seed} == {"cred_one", "cred_two"}
    assert f.seen["seed_mode"] == 0o600


def test_opencode_seed_leaves_out_tombstoned_ids(at_run):
    f = at_run
    write_carrier(f, [credential("cred_one"), credential("cred_two", "prov_b")])
    removed = carrier_dir(f) / "credentials.removed.json"
    removed.write_text(json.dumps(["cred_two"]))
    removed.chmod(0o600)
    a.run_audit(None, "opencode")
    assert {item["id"] for item in json.loads(f.seen["seed"])} == {"cred_one"}


def test_invalid_carrier_warns_and_audit_runs_without_seed(at_run, warnings_seen):
    f = at_run
    path = carrier_dir(f) / "credentials.json"
    path.write_text("this is not json")
    path.chmod(0o600)
    assert a.run_audit(None, "opencode") == 0
    assert f.seen["seed"] is None
    assert any("without a login seed" in message for message in warnings_seen)
    assert path.read_text() == "this is not json"


def test_missing_carrier_directory_is_created_and_audit_runs(at_run, warnings_seen):
    f = at_run
    assert not (f.config.config_root / "opencode").exists()
    assert a.run_audit(None, "opencode") == 0
    assert json.loads(f.seen["seed"]) == []
    assert stat.S_IMODE((f.config.config_root / "opencode").stat().st_mode) == 0o700


@pytest.mark.parametrize("agent", ("claude", "codex"))
def test_other_agents_get_no_session(at_run, agent):
    f = at_run
    write_carrier(f, [credential("cred_one")])
    assert a.run_audit(None, agent) == 0
    assert not audit_root(f).exists()
    assert not any("djinn-credentials" in m[2] for m in f.seen["mounts"])
    assert "seed" not in f.seen
    argv = f.seen["argv"]
    assert argv[argv.index("--name") + 1].startswith("djinn-assistant-")


def test_claude_mounts_unchanged(installation):
    f = installation
    root = f.config.config_root / "claude"
    root.mkdir(parents=True)
    for name in (".credentials.json", "claude.json"):
        (root / name).write_text("{}")
    a.run_audit(None, "claude")
    mounts = mounts_of(run_call(f))
    assert mounts[4:] == [
        [
            "type=bind",
            f"src={root}/.credentials.json",
            "dst=/run/djinn-credentials/.credentials.json",
        ],
        ["type=bind", f"src={root}/claude.json", "dst=/run/djinn-credentials/claude.json"],
    ]


def test_opencode_credential_files_are_no_longer_bound(installation):
    f = installation
    root = f.config.config_root / "opencode"
    root.mkdir(parents=True)
    for name in ("auth.json", "mcp-auth.json"):
        (root / name).write_text("{}")
    a.run_audit(None, "opencode")
    destinations = [m[2] for m in mounts_of(run_call(f))]
    assert not any(d.endswith(("auth.json", "mcp-auth.json")) for d in destinations)


class Events:
    """Orders docker calls and the recovery function in one list."""

    def __init__(self, f, monkeypatch):
        self.f = f
        self.recoveries = []
        real = oc.recover_audit_sessions

        def spy(*args, **kwargs):
            self.recoveries.append((args, kwargs))
            f.docker.calls.append((["docker", "RECOVER"], {}))
            return real(*args, **kwargs)

        monkeypatch.setattr(oc, "recover_audit_sessions", spy)

    def order(self):
        return [argv[1] for argv, _ in self.f.docker.calls if argv[1] in {"stop", "rm", "RECOVER"}]


@pytest.mark.parametrize("status", (0, 19, 130))
def test_finalization_order_stop_cleanup_recovery(installation, monkeypatch, status):
    f = installation
    events = Events(f, monkeypatch)
    f.docker.keep = True
    f.docker.status = status
    f.docker.interrupt = status == 130
    assert a.run_audit(None, "opencode") == status
    assert events.order() == ["RECOVER", "stop", "rm", "RECOVER"]
    assert f.docker.containers == {}
    stop = next(argv for argv, _ in f.docker.calls if argv[1] == "stop")
    assert stop[2:4] == ["--time", str(a.STOP_GRACE_SECONDS)]
    start_call, end_call = events.recoveries
    assert start_call[0][4] is None
    own = end_call[0][4]
    assert stop[-1] == own.container_name and not own.held
    assert end_call[1]["stop_container"] is a.stop_container
    # The empty session is gone after its merge-less recovery; nothing else is left behind.
    assert list(audit_root(f).iterdir()) == []


@pytest.mark.parametrize("agent", ("claude", "codex"))
@pytest.mark.parametrize("status", (0, 130))
def test_every_audit_ends_with_a_recovery(installation, monkeypatch, agent, status):
    f = installation
    events = Events(f, monkeypatch)
    f.docker.keep = True
    f.docker.interrupt = status == 130
    assert a.run_audit(None, agent) == status
    assert events.order() == ["RECOVER", "rm", "RECOVER"]
    assert all(call[0][4] is None for call in events.recoveries)


def test_the_end_recovery_runs_even_when_the_cleanup_fails(installation, monkeypatch):
    f = installation
    events = Events(f, monkeypatch)
    f.docker.keep = f.docker.fail_remove = True
    with pytest.raises(a.AssistantError, match="removal failed"):
        a.run_audit(None, "claude")
    assert events.order() == ["RECOVER", "rm", "RECOVER"]


def test_finalization_runs_after_a_failed_stop(installation, monkeypatch, warnings_seen):
    f = installation
    events = Events(f, monkeypatch)
    f.docker.keep = True

    def broken_stop(name):
        raise a.AssistantError("stop failed")

    monkeypatch.setattr(a, "stop_container", broken_stop)
    with pytest.raises(a.AssistantError, match="stop failed"):
        a.run_audit(None, "opencode")
    assert events.order() == ["RECOVER", "rm", "RECOVER"]
    assert f.docker.containers == {}


def test_ctrl_c_keeps_status_130_when_the_finalization_fails(
    installation, monkeypatch, warnings_seen
):
    f = installation
    events = Events(f, monkeypatch)
    f.docker.interrupt = True

    def broken_stop(name):
        raise a.AssistantError("stop failed")

    monkeypatch.setattr(a, "stop_container", broken_stop)
    assert a.run_audit(None, "opencode") == 130
    assert events.order() == ["RECOVER", "rm", "RECOVER"]
    assert any("finalization failed: stop failed" in str(item) for item in warnings_seen)


def test_prelaunch_failure_still_releases_the_session(installation, monkeypatch):
    f = installation
    events = Events(f, monkeypatch)
    f.docker.build_status = 7
    with pytest.raises(a.AssistantError, match="build failed"):
        a.run_audit(None, "opencode")
    # Never launched: no stop and no container cleanup, but the session is merged and released.
    assert events.order() == ["RECOVER", "RECOVER"]
    assert list(audit_root(f).iterdir()) == []


def leftover(f, *, final=None, database=False):
    with oc.create_audit_session() as session:
        identity = session.id
    base = audit_root(f) / identity
    if final is not None:
        target = base / "io" / "final.json"
        target.write_text(json.dumps(final))
        target.chmod(0o600)
    if database:
        (base / "data" / "opencode.db").write_bytes(b"db-" + secrets.token_hex(4).encode())
    return identity


@pytest.mark.parametrize("agent", ("claude", "opencode"))
def test_leftover_session_is_merged_at_audit_start(installation, agent):
    f = installation
    write_carrier(f, [credential("cred_one")])
    identity = leftover(f, final=[credential("cred_late", "prov_b")])
    a.run_audit(None, agent)
    assert carrier_ids(f) == {"cred_one", "cred_late"}
    assert not (audit_root(f) / identity).exists()


@pytest.mark.parametrize("agent", ("claude", "codex", "opencode"))
def test_recovery_image_is_the_opencode_image_whatever_the_agent(installation, monkeypatch, agent):
    f = installation
    leftover(f, database=True)
    exported = []

    def export(**kwargs):
        exported.append(kwargs)
        return b"[]\n"

    monkeypatch.setattr(a, "export_opencode_credentials", export)
    a.run_audit(None, agent)
    builds = [argv for argv, _ in f.docker.calls if argv[1:3] == ["buildx", "build"]]
    assert "AGENT=opencode" in builds[0]
    assert len(exported) == 1 and exported[0]["kind"] == "bind"
    assert exported[0]["source"].endswith("/data")
    assert exported[0]["image"].startswith("sha256:")


def test_recovery_builds_nothing_without_a_database(installation):
    f = installation
    leftover(f)
    a.run_audit(None, "claude")
    assert len([1 for argv, _ in f.docker.calls if argv[1] == "buildx"]) == 1


def test_kept_sessions_are_named_in_a_warning(installation, warnings_seen):
    f = installation
    identity = leftover(f)
    name = f"djinn-assistant-{identity}"
    f.docker.containers[name] = {
        "Id": "d" * 64,
        "Config": {"Labels": {a.SESSION_LABEL: name}},
        "State": {"Running": True},
    }
    a.run_audit(None, "claude")
    assert any(identity in message and "container exists" in message for message in warnings_seen)
    assert any(argv[1] == "stop" and argv[-1] == name for argv, _ in f.docker.calls)
    assert (audit_root(f) / identity).exists()


def test_failed_export_keeps_the_session_and_says_why(installation, monkeypatch, warnings_seen):
    f = installation
    identity = leftover(f, database=True)

    def export(**kwargs):
        raise docker_module.CredentialExportError("export exited with status 3")

    monkeypatch.setattr(a, "export_opencode_credentials", export)
    assert a.run_audit(None, "claude") == 0
    assert any("status 3" in message for message in warnings_seen)
    assert any(identity in message and "export failed" in message for message in warnings_seen)
    assert (audit_root(f) / identity / "data" / "opencode.db").exists()


def test_recovery_never_raises(installation, monkeypatch, warnings_seen):
    def broken(*args, **kwargs):
        raise oc.Refused("an ancestor is writable by group or others")

    monkeypatch.setattr(oc, "recover_audit_sessions", broken)
    a.recover_leftover_sessions(installation.project, installation.config)
    assert warnings_seen and "recovery failed" in warnings_seen[0]


def test_container_exists_checks_name_and_session_label(installation):
    f = installation
    assert a.container_exists("djinn-assistant-x") is False
    ps = [argv for argv, _ in f.docker.calls if argv[1] == "ps"]
    assert ps and f"label={a.SESSION_LABEL}=djinn-assistant-x" in ps[0]
    f.docker.containers["djinn-assistant-x"] = {
        "Id": "d" * 64,
        "Config": {"Labels": {}},
        "State": {"Running": True},
    }
    assert a.container_exists("djinn-assistant-x") is True


def test_stop_container_tolerates_a_missing_container(installation, monkeypatch):
    def gone(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "Error: No such container: x")

    monkeypatch.setattr(subprocess, "run", gone)
    a.stop_container("x")

    def broken(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "permission denied")

    monkeypatch.setattr(subprocess, "run", broken)
    with pytest.raises(a.AssistantError, match="permission denied"):
        a.stop_container("x")
