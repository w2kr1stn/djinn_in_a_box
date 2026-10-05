from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from djinn_in_a_box.core import docker
from djinn_in_a_box.core.exceptions import DeclarationSpecificationError
from djinn_in_a_box.core.host_runtime import GitRuntime
from djinn_in_a_box.core.ssh_delivery import GIT_ENVIRONMENT, MANAGED_SSH_TARGETS


@pytest.mark.parametrize("kind", ["interactive", "headless", "detached"])
@pytest.mark.parametrize("outcome", ["success", "failure", "exception", "interrupt", "timeout"])
def test_every_creator_public_fragment_and_lifetime(git_inputs, monkeypatch, kind, outcome):
    events, fragments = [], []
    root = Path("/owner-only/djinn/git-agent")

    @contextmanager
    def runtime(config, name):
        assert config is git_inputs
        assert name == "djinn"
        lease = GitRuntime(root, "generation")
        lease.begin_creation = lambda: None
        lease.retain = lambda: setattr(lease, "detached", True)
        events.append("acquired")
        try:
            yield lease
        finally:
            events.append("retained" if lease.detached else "released")

    def execute(command, **kwargs):
        assert events == ["acquired"]
        paths = [Path(command[i + 1]) for i, arg in enumerate(command) if arg == "-f"]
        fragments.append(json.loads(paths[-1].read_text()))
        if outcome == "exception":
            raise PermissionError("denied")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 0 if outcome == "success" else 1, "", "")

    for name in (
        "get_shell_mount_args",


        "get_sops_age_key_mount_args",
    ):
        monkeypatch.setattr(docker, name, lambda *args: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *args: ([], ()))
    monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
    monkeypatch.setattr(docker, "git_runtime", runtime)
    monkeypatch.setattr(docker.subprocess, "run", execute)
    invocation = docker.ContainerOptions()

    def create():
        if kind == "detached":
            return docker.compose_up_detached(git_inputs, invocation)
        return docker.compose_run(
            git_inputs, invocation, interactive=kind == "interactive", timeout=1
        )

    if outcome == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            create()
    elif outcome == "timeout" and kind == "detached":
        assert create().returncode == 124
    else:
        result = create()
        assert result.success == (outcome == "success")
    service = fragments[0]["services"]["dev"]
    binds = service["volumes"]
    assert [(bind["source"], bind["target"]) for bind in binds] == [
        (str(root / "public"), "/home/dev/.ssh"),
        (str(root / "export"), "/run/djinn-git-agent"),
    ]
    assert all(bind["read_only"] and not bind["bind"]["create_host_path"] for bind in binds)
    assert all(str(Path.home() / ".ssh") not in bind["source"] for bind in binds)
    assert all(bind["source"] not in {str(root), str(root / "private")} for bind in binds)
    assert GIT_ENVIRONMENT.items() <= service["environment"].items()
    assert events == [
        "acquired",
        "retained" if kind == "detached" and outcome == "success" else "released",
    ]


@pytest.mark.parametrize(
    "target",
    [
        "/home/dev/.ssh",
        "/home/dev/.ssh/work_git.pub",
        "/home/dev/.gitconfig_local",
        "/run/djinn-git-agent",
        "/run/djinn-git-agent/auth.sock",
        "/var/run/djinn-git-agent",
        "/var/run/djinn-git-agent/auth.sock",
    ],
)
@pytest.mark.parametrize("producer", ["invocation", "declaration"])
def test_managed_delivery_and_children_cannot_be_overridden(
    git_inputs, monkeypatch, target, producer
):
    for name in (
        "get_shell_mount_args",


        "get_sops_age_key_mount_args",
    ):
        monkeypatch.setattr(docker, name, lambda *args: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda *args: ([], ()))
    if producer == "invocation":
        with pytest.raises(docker.MountCollisionError):
            docker.validate_container_mounts(
                (docker.ContainerMount(git_inputs.code_dir, Path(target)),),
                git_inputs,
                docker.DockerMode.NONE,
            )
    else:
        config = git_inputs.model_copy(
            update={
                "mounts": {
                    "override": docker.BindDeclaration(
                        source=str(git_inputs.code_dir), target=target
                    ),
                }
            }
        )
        resolved = docker.resolve_declared_entries(
            config,
            docker.ContainerOptions(),
            runtime_targets=docker._reserved_mount_targets(config, docker.DockerMode.NONE),
            caller_env=None,
        )
        with pytest.raises(DeclarationSpecificationError):
            resolved.require_valid()


@pytest.mark.parametrize("key", ["SSH_AUTH_SOCK", "DJINN_GIT_MANIFEST"])
def test_git_environment_cannot_be_overridden(git_inputs, key):
    from pydantic import ValidationError

    from djinn_in_a_box.config.models import AppConfig

    with pytest.raises(ValidationError):
        AppConfig.model_validate({**git_inputs.model_dump(), "environment": {key: "override"}})
    with pytest.raises(DeclarationSpecificationError):
        docker.resolve_declared_entries(
            git_inputs, docker.ContainerOptions(), runtime_targets=[], caller_env={key: "override"}
        ).require_valid()


def test_runtime_mount_and_environment_producers_match_registries(git_inputs):
    from djinn_in_a_box.config.declarations import RESERVED_ENVIRONMENT

    fragment = {"services": {"dev": {}}}
    GitRuntime(Path("/runtime"), "generation").add_to_fragment(fragment)
    service = fragment["services"]["dev"]
    produced = {Path(volume["target"]) for volume in service["volumes"]}
    assert produced == set(MANAGED_SSH_TARGETS) - {Path("/home/dev/.gitconfig_local")}
    assert set(service["environment"]) == set(GIT_ENVIRONMENT)
    assert set(service["environment"]) <= RESERVED_ENVIRONMENT.keys()
