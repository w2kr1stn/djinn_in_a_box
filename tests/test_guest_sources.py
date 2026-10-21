"""The guest window boundary checks the sources Docker will actually bind."""

from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_desktop_runtime import calls
from typer.testing import CliRunner

from djinn_in_a_box.config.ssh import GitIdentity
from djinn_in_a_box.core import docker, host_runtime, host_sealing, hostctl, post_mortem
from djinn_in_a_box.core.ssh_delivery import GitSSHError

VALID = {
    "services": {"dev": {"networks": {"default": None}, "volumes": []}},
    "networks": {"default": {"name": "djinn-in-a-box_default"}},
}


@pytest.fixture
def boundary(mock_app_config, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DOCKER_CONFIG", str(home / ".docker"))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    runtime = home / ".local/state/djinn/runtime/git-agent"
    (runtime / "private").mkdir(parents=True)
    (runtime / "private/agent.sock").touch()
    monkeypatch.setattr(host_runtime, "runtime_root", lambda **kw: runtime)
    data, backing = tmp_path / "docker-data", tmp_path / "helper-backing"
    data.mkdir()
    backing.mkdir()
    monkeypatch.setattr(host_sealing, "docker_storage_roots", lambda **kw: (data, backing))
    key = home / "key"
    key.touch()
    mock_app_config.git.identities["guest-test"] = GitIdentity(
        hostname="git.example.test",
        user="git",
        key_file=key,
        public_key_file=home / "key.pub",
    )
    mock_app_config.config_root.mkdir()
    return SimpleNamespace(
        config=mock_app_config,
        home=home,
        runtime=runtime,
        data=data,
        backing=backing,
        key=key,
    )


def check_source(
    boundary, source, *, target="/review", read_only=False, mode=docker.DockerMode.NONE
):
    options = docker.ContainerOptions(
        docker_mode=mode,
        mounts=(docker.ContainerMount(Path(source), Path(target), read_only),),
    )
    return docker._guest_source_check(boundary.config, options)


@pytest.mark.parametrize("read_only", [False, True], ids=["rw", "ro"])
@pytest.mark.parametrize(
    "which",
    [
        "runtime",
        "hostctl",
        "serve",
        "post-mortem",
        "data",
        "backing",
        "key",
        "docker-socket",
    ],
)
def test_every_inventory_entry(boundary, monkeypatch, tmp_path, which, read_only):
    roots = {
        "runtime": boundary.runtime,
        "hostctl": hostctl.state_root(create=False),
        "serve": hostctl.state_root(create=False).parent / "serve",
        "post-mortem": post_mortem.records_root(create=False),
        "data": boundary.data,
        "backing": boundary.backing,
        "key": boundary.key,
        "docker-socket": tmp_path / "listed-docker.sock",
    }
    monkeypatch.setattr(host_sealing, "docker_sockets", lambda: (roots["docker-socket"],))
    source = roots[which]
    if not source.exists():
        source.mkdir(parents=True)
    with pytest.raises(docker.RunRefusedError, match="protected host path") as error:
        check_source(boundary, source, read_only=read_only)
    assert f"{source} → /review" in str(error.value)
    assert "change --mount, the declaration or code_dir" in str(error.value)


@pytest.mark.parametrize("read_only", [False, True])
def test_private_socket_symlink_alias(boundary, tmp_path, read_only):
    alias = tmp_path / "innocent-alias"
    alias.symlink_to(boundary.runtime / "private/agent.sock")
    with pytest.raises(docker.RunRefusedError, match="host runtime"):
        check_source(boundary, alias, read_only=read_only)


def test_key_inode_alias_is_protected(boundary, tmp_path):
    alias = tmp_path / "key-hardlink"
    alias.hardlink_to(boundary.key)
    with pytest.raises(docker.RunRefusedError, match="key file"):
        check_source(boundary, alias, read_only=True)


def test_inventory_is_lazy_and_shared(boundary, monkeypatch):
    original = host_sealing.protected_host_roots

    def inventory(identities, *, timeout):
        assert tuple(identities) == (boundary.key,)
        return original((boundary.key,), timeout=timeout)

    roots = Mock(side_effect=inventory)
    monkeypatch.setattr(host_sealing, "protected_host_roots", roots)
    check = docker._guest_source_check(boundary.config, docker.ContainerOptions())
    roots.assert_not_called()
    for target in ("/first", "/second"):
        assert check(boundary.config.code_dir, Path(target)) == boundary.config.code_dir
    roots.assert_called_once()
    assert roots.call_args.kwargs == {"timeout": 30}


@pytest.mark.skipif(os.geteuid() == 0, reason="requires real EACCES")
def test_nonstorage_protected_root_is_strict(boundary, tmp_path, monkeypatch):
    parent = tmp_path / "unreadable"
    parent.mkdir()
    protected = parent / "private"
    protected.mkdir()
    roots = host_sealing.protected_host_roots

    def inventory(*args, **kwargs):
        return {**roots(*args, **kwargs), "unreadable private state": protected}

    monkeypatch.setattr(host_sealing, "protected_host_roots", inventory)
    parent.chmod(0)
    try:
        with pytest.raises(docker.RunRefusedError, match="cannot verify"):
            check_source(boundary, boundary.config.code_dir)
    finally:
        parent.chmod(0o700)


def test_home_ancestor_message_precedes_execution_input(boundary, monkeypatch):
    monkeypatch.setattr(
        host_sealing,
        "execution_paths",
        Mock(side_effect=AssertionError("execution check first")),
    )
    with pytest.raises(docker.RunRefusedError) as error:
        docker.compose_run(
            boundary.config,
            docker.ContainerOptions(
                mounts=(docker.ContainerMount(boundary.home, Path("/home/dev/workspace")),)
            ),
            interactive=False,
        )
    assert (
        "start from a subdirectory, or use `--mount <dir>:ro` for a directory "
        "outside the private state"
    ) in str(error.value)


def test_unlisted_socket_is_refused(boundary, monkeypatch):
    monkeypatch.setattr(host_sealing, "docker_sockets", lambda: ())
    with tempfile.TemporaryDirectory(prefix="djinn-s3-") as directory:
        source = Path(directory) / "other.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(source))
            with pytest.raises(docker.RunRefusedError, match="resolved source is a socket"):
                check_source(boundary, source, read_only=True)


@pytest.mark.skipif(os.geteuid() == 0, reason="requires real EACCES")
def test_storage_roots_are_lexical_below_unreadable_parent(boundary, tmp_path, monkeypatch):
    parent = tmp_path / "unreadable"
    parent.mkdir()
    data = parent / "data"
    data.mkdir()
    monkeypatch.setattr(host_sealing, "docker_storage_roots", lambda **kw: (data, data / "volume"))
    parent.chmod(0)
    try:
        check = check_source(boundary, boundary.config.code_dir)
        assert check(boundary.config.code_dir, Path("/review")) == boundary.config.code_dir
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize("relation", ["ancestor", "descendant"])
def test_storage_overlap_in_both_directions(boundary, relation):
    source = boundary.data.parent if relation == "ancestor" else boundary.data / "child"
    source.mkdir(exist_ok=True)
    with pytest.raises(docker.RunRefusedError, match="Docker data root"):
        check_source(boundary, source, read_only=True)


def test_absent_helper_volume_uses_data_root(fake_owner):
    config, _, _, log = fake_owner
    data, backing = host_sealing.docker_storage_roots(timeout=30)
    assert data == backing
    data.mkdir()
    with pytest.raises(docker.RunRefusedError, match="Docker data root"):
        docker._guest_source_check(
            config, docker.ContainerOptions(mounts=(docker.ContainerMount(data, Path("/review")),))
        )
    assert ["volume", "inspect", hostctl.HOSTCTL_STATE_VOLUME] in calls(log)


@pytest.fixture
def compose_guest(boundary, monkeypatch):
    resolved = copy.deepcopy(VALID)
    invocations, fragments, launches = [], [], []

    def compose(args, **kwargs):
        invocations.append((args, kwargs))
        override = Path(args[-4])
        fragments.append(json.loads(override.read_text()))
        return docker.RunResult(0, stdout=json.dumps(resolved))

    def execute(args, **kwargs):
        launches.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(host_sealing, "execution_paths", lambda: {})
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda *a: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *a: ([], ()))
    monkeypatch.setattr(docker, "_run_compose", compose)
    monkeypatch.setattr(docker, "_remove_run", lambda *a: True)
    monkeypatch.setattr(subprocess, "run", execute)
    return SimpleNamespace(
        **vars(boundary),
        resolved=resolved,
        invocations=invocations,
        fragments=fragments,
        launches=launches,
    )


@pytest.mark.parametrize("family", ["shell", "sops", "zone", "declared"])
def test_phase_one_managed_and_declared_sources_precede_execution_check(
    compose_guest,
    monkeypatch,
    family,
):
    g = compose_guest
    source = g.runtime / "private/agent.sock"
    spec = ["-v", f"{source}:/review:ro"]
    if family == "declared":
        g.config.mounts["review"] = {"source": str(source), "target": "/review", "read_only": True}
    elif family == "zone":
        monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *a: (spec, ()))
    else:
        builder = "get_shell_mount_args" if family == "shell" else "get_sops_age_key_mount_args"
        monkeypatch.setattr(docker, builder, lambda *a: spec)
    monkeypatch.setattr(
        host_sealing,
        "execution_paths",
        Mock(side_effect=AssertionError("execution check first")),
    )
    with pytest.raises(docker.RunRefusedError, match="host runtime"):
        docker.compose_run(g.config, docker.ContainerOptions(), interactive=False)
    assert not g.invocations and not g.launches


@pytest.mark.parametrize(
    "family",
    [
        "code_dir",
        "config_root",
        "config-override",
        "config-child",
        "sessions",
        "gitconfig",
    ],
)
def test_structural_base_binds(compose_guest, monkeypatch, family):
    g = compose_guest
    private = g.runtime / "private"
    aliases = {
        "code_dir": (g.config.code_dir, "/home/dev/projects"),
        "config_root": (g.config.config_root, "/home/dev/.djinn/config"),
        "config-override": (g.home / "override", "/home/dev/.djinn/config"),
        "config-child": (g.config.config_root / "codex", "/home/dev/.codex"),
        "sessions": (g.home / ".djinn/sessions", "/home/dev/.djinn/sessions"),
        "gitconfig": (g.home / ".gitconfig", "/home/dev/.gitconfig"),
    }
    source, target = aliases[family]
    if source.is_dir():
        source.rmdir()
    source.parent.mkdir(parents=True, exist_ok=True)
    source.symlink_to(g.key if family == "gitconfig" else private)
    if family == "config-override":
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(source))
        assert docker.get_config_root(g.config) == private
    g.resolved["services"]["dev"]["volumes"] = [
        {"type": "volume", "source": "named", "target": "/named"},
        {"type": "bind", "source": str(source), "target": target, "read_only": True},
    ]
    with pytest.raises(docker.RunRefusedError, match="protected host path") as error:
        docker.compose_run(g.config, docker.ContainerOptions(), interactive=False)
    assert f"{source} → {target}" in str(error.value)
    assert len(g.invocations) == 1 and not g.launches


@pytest.mark.parametrize("flag", ["-v", "--volume", "--volume="])
def test_structural_command_volume_forms(boundary, flag):
    spec = f"{boundary.runtime}:/review:ro"
    args = [flag + spec] if flag.endswith("=") else [flag, spec]
    with pytest.raises(docker.RunRefusedError, match="host runtime"):
        docker._guest_source_check(
            boundary.config,
            docker.ContainerOptions(),
            resolved=VALID,
            mount_args=args,
        )


def test_command_line_dollar_source_is_checked_literally(boundary, tmp_path):
    alias = tmp_path / "alias$$name"
    alias.symlink_to(boundary.runtime)
    with pytest.raises(docker.RunRefusedError, match="host runtime"):
        docker._guest_source_check(
            boundary.config,
            docker.ContainerOptions(),
            resolved=VALID,
            mount_args=["-v", f"{alias}:/review:ro"],
        )


@pytest.mark.parametrize("mode", [docker.DockerMode.NONE, docker.DockerMode.DIRECT])
@pytest.mark.parametrize("phase", ["user", "resolved", "command"])
@pytest.mark.parametrize(
    "pair",
    ["exact", "canonical-source", "different-target", "different-source", "other-socket"],
)
def test_direct_socket_exception_is_exact(boundary, monkeypatch, mode, pair, phase):
    monkeypatch.setattr(host_sealing, "docker_sockets", lambda: (Path("/var/run/docker.sock"),))
    with tempfile.TemporaryDirectory(prefix="djinn-s3-") as directory:
        other = Path(directory) / "other.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(other))
            source, target = {
                "exact": ("/var/run/docker.sock", "/var/run/docker.sock"),
                "canonical-source": ("/run/docker.sock", "/var/run/docker.sock"),
                "different-target": ("/var/run/docker.sock", "/other.sock"),
                "different-source": (str(other), "/var/run/docker.sock"),
                "other-socket": (str(other), str(other)),
            }[pair]

            def invoke():
                if phase == "user":
                    check_source(boundary, source, target=target, mode=mode)
                    return
                resolved = copy.deepcopy(VALID)
                args = []
                if phase == "resolved":
                    resolved["services"]["dev"]["volumes"] = [
                        {
                            "type": "bind",
                            "source": source,
                            "target": target,
                        }
                    ]
                else:
                    args = ["-v", f"{source}:{target}"]
                docker._guest_source_check(
                    boundary.config,
                    docker.ContainerOptions(docker_mode=mode),
                    resolved=resolved,
                    mount_args=args,
                )

            if mode is docker.DockerMode.DIRECT and pair == "exact":
                invoke()
            else:
                with pytest.raises(docker.RunRefusedError):
                    invoke()


def test_new_compose_bind_is_checked_automatically(compose_guest, tmp_path, monkeypatch):
    g = compose_guest
    extra = tmp_path / "extra-compose.yml"
    # JSON is also a Compose YAML document. The fake resolver consumes this new file.
    extra.write_text(
        json.dumps(
            {
                "services": {
                    "dev": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(g.runtime),
                                "target": "/new-feature",
                                "read_only": True,
                            }
                        ]
                    }
                }
            }
        )
    )
    files = docker.get_compose_files
    monkeypatch.setattr(
        docker, "get_compose_files", lambda *a, **kw: [*files(*a, **kw), "-f", str(extra)]
    )
    resolve = docker._run_compose

    def compose(args, **kwargs):
        assert args[args.index(str(extra)) - 1] == "-f"
        g.resolved["services"]["dev"]["volumes"] = json.loads(extra.read_text())["services"]["dev"][
            "volumes"
        ]
        return resolve(args, **kwargs)

    monkeypatch.setattr(docker, "_run_compose", compose)
    with pytest.raises(docker.RunRefusedError, match="/new-feature"):
        docker.compose_run(g.config, docker.ContainerOptions(), interactive=False)
    assert not g.launches


def test_base_bind_alias_is_checked_without_rewriting(compose_guest, tmp_path):
    source = tmp_path / "base-alias"
    source.symlink_to(compose_guest.config.code_dir)
    row = {"type": "bind", "source": str(source), "target": "/base", "read_only": True}
    compose_guest.resolved["services"]["dev"]["volumes"] = [row]
    docker._guest_source_check(
        compose_guest.config,
        docker.ContainerOptions(),
        resolved=compose_guest.resolved,
    )
    assert docker.compose_run(
        compose_guest.config,
        docker.ContainerOptions(),
        interactive=False,
    ).success
    assert row["source"] == str(source)


@pytest.mark.parametrize("volumes", [None, [{"type": "bind"}], [{"source": "/x"}], [None]])
def test_resolved_bind_schema_errors_are_refusals(compose_guest, volumes):
    compose_guest.resolved["services"]["dev"]["volumes"] = volumes
    with pytest.raises(docker.RunRefusedError, match="cannot verify .*; refusing"):
        docker.compose_run(compose_guest.config, docker.ContainerOptions(), interactive=False)
    assert not compose_guest.launches


@pytest.mark.parametrize("family", ["user", "declared", "shell", "sops", "zone"])
@pytest.mark.parametrize("dollars", [False, True], ids=["plain", "dollar"])
def test_resolved_sources_targets_and_single_escape(
    compose_guest, tmp_path, monkeypatch, family, dollars
):
    g = compose_guest
    real = tmp_path / ("real$source" if dollars else "real-source")
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    target = Path("/review")
    options = docker.ContainerOptions()
    if family == "user":
        # Assign a target from the alias name before the S3 source substitution.
        options = docker.ContainerOptions(
            mounts=(docker.ContainerMount(alias, Path("/home/dev/mount/alias")),)
        )
        target = options.mounts[0].target
    elif family == "declared":
        g.config.mounts["review"] = {"source": str(alias), "target": str(target), "read_only": True}
    elif family == "zone":
        monkeypatch.setattr(
            docker,
            "_zone_overlay_mount_args_and_targets",
            lambda *a: (["-v", f"{alias}:{target}:ro"], ()),
        )
    else:
        builder = "get_shell_mount_args" if family == "shell" else "get_sops_age_key_mount_args"
        monkeypatch.setattr(docker, builder, lambda *a: ["--volume=" + f"{alias}:{target}:ro"])
    assert docker.compose_run(g.config, options, interactive=False).success
    escaped = str(real).replace("$", "$$")
    if family == "declared":
        row = g.fragments[0]["services"]["dev"]["volumes"][0]
        assert row["source"] == escaped and row["target"] == str(target)
    else:
        specs = docker._volume_specs_from_mount_args(g.launches[0][0])
        assert f"{real}:{target}" + ("" if family == "user" else ":ro") in specs


@pytest.mark.parametrize("family", ["user", "managed"])
def test_cli_target_with_dollar_stays_unchanged(compose_guest, family):
    source = compose_guest.config.code_dir
    target = Path("/review$target")
    options = docker.ContainerOptions()
    kwargs = {}
    if family == "user":
        options = docker.ContainerOptions(mounts=(docker.ContainerMount(source, target),))
    else:
        kwargs["shell_mount_args"] = ["-v", f"{source}:{target}"]
    assert docker.compose_run(compose_guest.config, options, interactive=False, **kwargs).success
    specs = docker._volume_specs_from_mount_args(compose_guest.launches[0][0])
    assert f"{source}:{target}" in specs


def test_resolved_colon_is_refused(boundary, tmp_path):
    real = tmp_path / "real:source"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    with pytest.raises(docker.RunRefusedError, match="resolved source contains ':'"):
        check_source(boundary, alias)


@pytest.mark.parametrize("problem", ["loop", "eacces", "meta"])
def test_real_check_failures_are_refusals(boundary, tmp_path, problem):
    source = tmp_path / "source"
    source.mkdir()
    if problem == "loop":
        source.rmdir()
        source.symlink_to(source)
    elif problem == "eacces":
        if os.geteuid() == 0:
            pytest.skip("requires real EACCES")
        source = source / "child"
        source.mkdir()
        source.parent.chmod(0)
    else:
        meta = boundary.home / ".docker/contexts/meta/context/meta.json"
        meta.parent.mkdir(parents=True)
        meta.write_text("{broken")
    try:
        with pytest.raises(docker.RunRefusedError, match="cannot verify .*; refusing"):
            check_source(boundary, source)
    finally:
        if problem == "eacces":
            source.parent.chmod(0o700)


ERRORS = [
    OSError("spawn failed"),
    ValueError("invalid"),
    KeyError("missing"),
    TypeError("schema"),
    RuntimeError("loop"),
    GitSSHError("ssh"),
    hostctl.HostctlError("helper"),
    subprocess.TimeoutExpired("docker", 30),
    subprocess.SubprocessError("spawn"),
    UnicodeDecodeError("utf8", b"\xff", 0, 1, "invalid"),
    json.JSONDecodeError("bad", "x", 0),
]


@pytest.mark.parametrize("exc", ERRORS, ids=lambda exc: type(exc).__name__)
@pytest.mark.parametrize("phase", ["user", "resolved", "managed", "direct"])
def test_check_error_conversion(boundary, compose_guest, monkeypatch, exc, phase):
    fail = Mock(side_effect=exc)
    if phase == "direct":
        monkeypatch.setattr(hostctl, "inspect_helper", fail)
        invoke = docker._guest_direct_check
    elif phase == "managed":
        monkeypatch.setattr(docker, "get_shell_mount_args", fail)

        def invoke():
            return docker.compose_run(boundary.config, docker.ContainerOptions(), interactive=False)
    else:
        monkeypatch.setattr(host_sealing, "protected_host_roots", fail)
        if phase == "user":

            def invoke():
                return check_source(boundary, boundary.config.code_dir)
        else:
            compose_guest.resolved["services"]["dev"]["volumes"] = [
                {
                    "type": "bind",
                    "source": str(boundary.config.code_dir),
                    "target": "/review",
                }
            ]

            def invoke():
                return docker.compose_run(
                    boundary.config, docker.ContainerOptions(), interactive=False
                )

    with pytest.raises(docker.RunRefusedError, match="cannot verify .*; refusing"):
        invoke()


@pytest.mark.parametrize("running", [True, False, None])
def test_direct_helper_state_and_guard(compose_guest, monkeypatch, running):
    held = False

    @contextmanager
    def guard():
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    def inspect():
        assert held, "helper inspection must hold control_guard"
        return None if running is None else {"State": {"Running": running}}

    monkeypatch.setattr(hostctl, "control_guard", guard)
    inspected = Mock(side_effect=inspect)
    monkeypatch.setattr(hostctl, "inspect_helper", inspected)
    options = docker.ContainerOptions(docker_mode=docker.DockerMode.DIRECT)
    if running:
        with pytest.raises(
            docker.RunRefusedError, match="djinn hostctl off.*without --docker-direct"
        ):
            docker.compose_run(compose_guest.config, options, interactive=False)
        assert not compose_guest.launches
    else:
        assert docker.compose_run(compose_guest.config, options, interactive=False).success
    inspected.assert_called_once()
    assert not held


def test_direct_control_lock_permission_error(compose_guest, monkeypatch):
    monkeypatch.setattr(hostctl, "control_guard", Mock(side_effect=PermissionError("control lock")))
    with pytest.raises(docker.RunRefusedError, match="cannot verify hostctl helper.*control lock"):
        docker.compose_run(
            compose_guest.config,
            docker.ContainerOptions(
                docker_mode=docker.DockerMode.DIRECT,
            ),
            interactive=False,
        )
    assert not compose_guest.launches


def test_plain_guest_never_checks_helper(compose_guest, monkeypatch):
    monkeypatch.setattr(hostctl, "control_guard", Mock(side_effect=AssertionError("plain guest")))
    assert docker.compose_run(
        compose_guest.config, docker.ContainerOptions(), interactive=False
    ).success


@pytest.mark.parametrize("phase", ["user", "resolved", "managed", "direct"])
def test_keyboard_interrupt_passes_through(boundary, compose_guest, monkeypatch, phase):
    fail = Mock(side_effect=KeyboardInterrupt)
    if phase == "direct":
        monkeypatch.setattr(hostctl, "inspect_helper", fail)
        invoke = docker._guest_direct_check
    elif phase == "managed":
        monkeypatch.setattr(docker, "get_shell_mount_args", fail)

        def invoke():
            return docker.compose_run(boundary.config, docker.ContainerOptions(), interactive=False)
    else:
        monkeypatch.setattr(host_sealing, "protected_host_roots", fail)

        def invoke():
            return check_source(boundary, boundary.config.code_dir)

        if phase == "resolved":
            resolved = copy.deepcopy(VALID)
            resolved["services"]["dev"]["volumes"] = [
                {
                    "type": "bind",
                    "source": str(boundary.config.code_dir),
                    "target": "/review",
                }
            ]

            def invoke():
                return docker._guest_source_check(
                    boundary.config,
                    docker.ContainerOptions(),
                    resolved=resolved,
                )

    with pytest.raises(KeyboardInterrupt):
        invoke()


def test_real_cli_home_with_installation_and_private_state(boundary, tmp_path, monkeypatch):
    from djinn_in_a_box.cli.djinn import app
    from djinn_in_a_box.commands import agent
    from djinn_in_a_box.core import paths

    install = boundary.home / "djinn-install"
    install.mkdir()
    (install / "docker-compose.yml").touch()
    monkeypatch.setattr(paths, "get_project_root", lambda: install)
    monkeypatch.setattr(docker, "get_project_root", lambda: install)
    monkeypatch.setattr(host_sealing.host_paths, "get_project_root", lambda: install)
    monkeypatch.setattr(host_sealing, "get_project_root", lambda: install)
    monkeypatch.setattr(agent, "get_project_root", lambda: install)
    monkeypatch.setattr(agent, "load_config", lambda: boundary.config)
    monkeypatch.setattr(
        agent, "prepare_config_workflow", lambda *a, **kw: SimpleNamespace(success=True, notices=())
    )
    monkeypatch.setattr(agent, "workflow_image_compatible", lambda: True)
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda *a: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *a: ([], ()))
    monkeypatch.chdir(boundary.home)
    assert "Djinn installation/build context" in docker._execution_input_check()(boundary.home)
    result = CliRunner().invoke(app, ["run", "codex", "inspect"])
    assert result.exit_code == 1, result.output
    assert (
        "start from a subdirectory, or use `--mount <dir>:ro` for a directory "
        "outside the private state"
    ) in result.output
    assert "overlaps host execution input" not in result.output
