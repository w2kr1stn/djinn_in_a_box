"""Assistant contract tests, using real Docker result shapes and closed capture stdin."""

from __future__ import annotations

import csv
import json
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import assistant as a
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

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        args = argv[1:]
        if args[0] == "run":
            assert "stdin" not in kwargs and "capture_output" not in kwargs
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
            if args[2] not in self.containers:
                return subprocess.CompletedProcess(argv, 1, "[]", f"No such container: {args[2]}")
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.containers[args[2]]]), "")
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
        if args[0] == "rm":
            if self.fail_remove:
                return subprocess.CompletedProcess(argv, 1, "", "removal failed")
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
    for name in ("Dockerfile", "entrypoint.sh", "audit-briefing.md"):
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
        "opencode": ["opencode", "--pure", "--prompt"],
    }
    assert argv[-len(expected[agent]) - 1 : -1] == expected[agent]
    assert 'OPENCODE_CONFIG_CONTENT={"permission":{"edit":"ask","bash":"ask"}}' in argv
    assert "OPENCODE_DISABLE_PROJECT_CONFIG=true" in argv


def test_config_selection_general_and_override(installation, monkeypatch):
    f = installation
    config = AppConfig.model_validate({**f.config.model_dump(), "assistant": {"agent": "opencode"}})
    monkeypatch.setattr(a, "audit_config", lambda: (config, ""))
    a.run_audit(None)
    assert run_call(f)[-4:-1] == ["opencode", "--pure", "--prompt"]
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
def test_invalid_pins_abort(installation, pin):
    f = installation
    path = f.project / "Dockerfile"
    value = {
        "duplicate": path.read_text() + "ARG CODEX_VERSION=0.160.0\n",
        "malformed": path.read_text().replace("0.160.0", "latest"),
        "missing": path.read_text().replace("ARG CODEX_VERSION", "# ARG CODEX_VERSION"),
    }[pin]
    path.write_text(value)
    with pytest.raises(a.AssistantError, match="numeric ARG"):
        a.ensure_image(f.project, "codex", "linux/amd64", "default")
    assert f.docker.calls == []


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
