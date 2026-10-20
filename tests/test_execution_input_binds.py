"""Writable declared binds must not expose the creating CLI's execution inputs."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import agent, container, doctor
from djinn_in_a_box.config.models import AgentConfig, AppConfig
from djinn_in_a_box.core import agent_runner, docker, host_sealing
from djinn_in_a_box.core.config_workflow import WorkflowPreparationResult
from djinn_in_a_box.core.exceptions import DeclarationSpecificationError, MountSpecificationError


@pytest.fixture
def execution_ground(tmp_path, monkeypatch):
    inputs = {}
    for name, directory in (
        ("Djinn installation/build context", "install"),
        ("Python environment", "python"),
        ("host PATH directory", "bin"),
    ):
        path = tmp_path / directory
        path.mkdir()
        inputs[name] = (path, "execution input", name)
    inventory = Mock(return_value=inputs)
    monkeypatch.setattr(host_sealing, "execution_paths", inventory)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    return inputs, inventory, unrelated


@pytest.mark.parametrize("direction", ["equal", "child", "parent", "symlink", "unrelated"])
def test_execution_overlap_directions(execution_ground, tmp_path, direction):
    inputs, _inventory, unrelated = execution_ground
    name, (path, *_rest) = next(iter(inputs.items()))
    source = path
    if direction == "child":
        source = path / "child"
        source.mkdir()
    elif direction == "parent":
        source = path.parent
    elif direction == "symlink":
        source = tmp_path / "alias"
        source.symlink_to(path, target_is_directory=True)
    elif direction == "unrelated":
        source = unrelated
    assert host_sealing.execution_overlap(source, inputs) == (
        None if direction == "unrelated" else (name, path)
    )


def test_execution_overlap_inode_alias(execution_ground, monkeypatch):
    inputs, _inventory, unrelated = execution_ground
    name, (path, *_rest) = next(iter(inputs.items()))
    samefile = Path.samefile
    monkeypatch.setattr(
        Path, "samefile", lambda a, b: (a == unrelated and b == path) or samefile(a, b)
    )
    assert host_sealing.execution_overlap(unrelated, inputs) == (name, path)


def test_execution_overlap_first_input(execution_ground, tmp_path):
    inputs, _inventory, _unrelated = execution_ground
    name, (path, *_rest) = next(iter(inputs.items()))
    assert host_sealing.execution_overlap(tmp_path, inputs) == (name, path)


@pytest.mark.parametrize("source", ["relative", "dangling"])
def test_execution_overlap_canonical_failure(execution_ground, tmp_path, source):
    inputs, _inventory, _unrelated = execution_ground
    if source == "dangling":
        source = tmp_path / "dangling"
        source.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="non-absolute host path|dangling host alias"):
        host_sealing.execution_overlap(source, inputs)


@pytest.mark.parametrize("contents", ["[]", "null", '"config"', "42"])
def test_non_object_docker_config(tmp_path, monkeypatch, contents):
    (tmp_path / "config.json").write_text(contents)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    with pytest.raises(ValueError, match="must be a JSON object"):
        host_sealing.execution_paths()


def resolve(source, *, read_only=False, target="/review", extra=None):
    entry = {"source": str(source), "target": target}
    if read_only is not None:
        entry["read_only"] = read_only
    config = AppConfig(code_dir=source).model_copy(
        update={"mounts": {"review": entry, **(extra or {})}}
    )
    return docker.resolve_declared_entries(
        config, docker.ContainerOptions(), runtime_targets=[], caller_env=None
    )


def validate(mounts, source):
    docker.validate_container_mounts(
        mounts,
        AppConfig(code_dir=source),
        docker.DockerMode.NONE,
        shell_args=[],
        sops_args=[],
        zone_overlay_targets=(),
    )


@pytest.mark.parametrize(
    "input_name",
    [
        "Djinn installation/build context",
        "Python environment",
        "host PATH directory",
    ],
)
@pytest.mark.parametrize("read_only", [False, None, True], ids=["rw", "default", "ro"])
def test_declaration_matrix(execution_ground, input_name, read_only):
    inputs, inventory, _unrelated = execution_ground
    path = inputs[input_name][0]
    result = resolve(path, read_only=read_only)
    if read_only:
        result.require_valid()
        inventory.assert_not_called()
    else:
        expected = (
            f"Declared mount 'review': writable source '{path}' overlaps host execution input "
            f"{input_name} ({path}); set read_only = true or remove the mount."
        )
        assert result.diagnostics[0].error == expected
        with pytest.raises(DeclarationSpecificationError) as exc:
            result.require_valid()
        assert str(exc.value) == expected


@pytest.mark.parametrize(
    "input_name",
    [
        "Djinn installation/build context",
        "Python environment",
        "host PATH directory",
    ],
)
@pytest.mark.parametrize("mode", ["plain", "rw", "ro", "here", "synthetic-ro-workspace"])
def test_invocation_matrix(execution_ground, monkeypatch, input_name, mode):
    inputs, inventory, _unrelated = execution_ground
    path = inputs[input_name][0]
    if mode in {"here", "synthetic-ro-workspace"}:
        monkeypatch.chdir(path)
        mounts = docker.resolve_container_mounts((), here=True)
        if mode == "synthetic-ro-workspace":
            mounts = (docker.ContainerMount(path, Path("/home/dev/workspace"), True),)
    else:
        suffix = "" if mode == "plain" else f":{mode}"
        mounts = docker.resolve_container_mounts((f"{path}{suffix}",))
    if mode in {"ro", "synthetic-ro-workspace"}:
        validate(mounts, path)
        inventory.assert_not_called()
        return
    with pytest.raises(MountSpecificationError) as exc:
        validate(mounts, path)
    overlap = f"overlaps host execution input {input_name} ({path})"
    if mode == "here":
        assert str(exc.value) == (
            f"--here mounts {path} writable, which {overlap}; start from a directory outside it "
            f"or mount it read-only with --mount {path}:ro"
        )
        readonly = docker.resolve_container_mounts((f"{path}:ro",))
        validate(readonly, path)
    else:
        assert str(exc.value) == (
            f"Mount {path} -> {mounts[0].target} is writable and {overlap}; "
            "append :ro or remove the mount"
        )


@pytest.mark.parametrize("channel", ["declaration", "invocation"])
@pytest.mark.parametrize("direction", ["child", "parent", "symlink", "unrelated"])
def test_validator_overlap_directions(execution_ground, tmp_path, channel, direction):
    inputs, _inventory, unrelated = execution_ground
    name, (path, *_rest) = next(iter(inputs.items()))
    source = unrelated
    if direction == "child":
        source = path / "child"
        source.mkdir()
    elif direction == "parent":
        source = path.parent
    elif direction == "symlink":
        source = tmp_path / "alias"
        source.symlink_to(path, target_is_directory=True)
    if channel == "declaration":
        result = resolve(source)
        if direction == "unrelated":
            result.require_valid()
        else:
            assert f"{name} ({path})" in result.diagnostics[0].error
    elif direction == "unrelated":
        validate((docker.ContainerMount(source, Path("/review")),), source)
    else:
        with pytest.raises(MountSpecificationError) as exc:
            validate((docker.ContainerMount(source, Path("/review")),), source)
        assert f"{name} ({path})" in str(exc.value)


@pytest.mark.parametrize("channel", ["declaration", "invocation"])
def test_duplicate_ro_rw_source(execution_ground, channel):
    inputs, _inventory, _unrelated = execution_ground
    path = next(iter(inputs.values()))[0]
    if channel == "declaration":
        result = resolve(
            path,
            read_only=True,
            extra={
                "writable": {"source": str(path), "target": "/writable"},
            },
        )
        assert result.diagnostics[0].error is None
        assert "writable source" in result.diagnostics[1].error
    else:
        with pytest.raises(MountSpecificationError, match="/writable is writable"):
            validate(
                (
                    docker.ContainerMount(path, Path("/review"), True),
                    docker.ContainerMount(path, Path("/writable")),
                ),
                path,
            )


@pytest.mark.parametrize("channel", ["declaration", "invocation", "here"])
@pytest.mark.parametrize("site", ["inventory", "canonical", "samefile"])
@pytest.mark.parametrize("exception", [OSError, ValueError, KeyError, TypeError, RuntimeError])
def test_fail_closed(execution_ground, monkeypatch, channel, site, exception):
    _inputs, inventory, source = execution_ground
    failure = exception("input unavailable")
    if site == "inventory":
        inventory.side_effect = failure
    elif site == "canonical":
        monkeypatch.setattr(host_sealing, "canonical", Mock(side_effect=failure))
    else:
        monkeypatch.setattr(Path, "samefile", Mock(side_effect=failure))
    cause = f"cannot be checked against host execution inputs ({failure})"
    if channel == "declaration":
        result = resolve(source)
        assert result.diagnostics[0].error == (
            f"Declared mount 'review': writable source '{source}' {cause}; "
            "set read_only = true or remove the mount."
        )
        with pytest.raises(DeclarationSpecificationError):
            result.require_valid()
    else:
        target = Path("/home/dev/workspace" if channel == "here" else "/review")
        with pytest.raises(MountSpecificationError) as exc:
            validate((docker.ContainerMount(source, target),), source)
        expected = (
            f"--here mounts {source} writable, which {cause}; start from a directory outside it "
            "or mount it read-only "
            f"with --mount {source}:ro"
            if channel == "here"
            else f"Mount {source} -> {target} is writable and {cause}; "
            "append :ro or remove the mount"
        )
        assert str(exc.value) == expected


@pytest.mark.parametrize("channel", ["declaration", "invocation"])
@pytest.mark.parametrize("case", ["empty", "ro", "volume"])
def test_inventory_is_lazy(execution_ground, channel, case):
    _inputs, inventory, source = execution_ground
    inventory.side_effect = RuntimeError("must not evaluate")
    if channel == "declaration":
        mounts = (
            {}
            if case == "empty"
            else {
                "review": {"source": str(source), "target": "/review", "read_only": True}
                if case == "ro"
                else {"volume": True, "target": "/review", "backup": "none"}
            }
        )
        config = AppConfig(code_dir=source, mounts=mounts)
        docker.resolve_declared_entries(
            config,
            docker.ContainerOptions(),
            runtime_targets=[],
            caller_env=None,
        ).require_valid()
    else:
        mounts = (docker.ContainerMount(source, Path("/review"), True),) if case == "ro" else ()
        validate(mounts, source)
    inventory.assert_not_called()


@pytest.mark.parametrize("channel", ["declaration", "invocation"])
@pytest.mark.parametrize("failure", [False, True])
def test_inventory_evaluated_once(execution_ground, channel, failure):
    _inputs, inventory, source = execution_ground
    if failure:
        inventory.side_effect = ValueError("bad inventory")
    if channel == "declaration":
        result = resolve(source, extra={"second": {"source": str(source), "target": "/second"}})
        if failure:
            assert len(result.diagnostics) == 2
            assert all("bad inventory" in d.error for d in result.diagnostics)
        else:
            result.require_valid()
    elif failure:
        with pytest.raises(MountSpecificationError, match="bad inventory"):
            validate((docker.ContainerMount(source, Path("/review")),), source)
    else:
        validate(
            (
                docker.ContainerMount(source, Path("/review")),
                docker.ContainerMount(source, Path("/second")),
            ),
            source,
        )
    inventory.assert_called_once_with()


def test_target_error_precedes_execution_overlap(execution_ground):
    inputs, inventory, _source = execution_ground
    path = next(iter(inputs.values()))[0]
    result = resolve(path, target="/home/dev/.cache/uv/child")
    assert result.diagnostics[0].error == (
        "Declared mount 'review': target '/home/dev/.cache/uv/child' conflicts with built-in "
        "mount '/home/dev/.cache/uv' (Djinn-managed volume root)."
    )
    inventory.assert_not_called()
    with pytest.raises(docker.MountCollisionError, match="Djinn-managed path"):
        validate((docker.ContainerMount(path, Path("/home/dev/.cache/uv/child")),), path)
    inventory.assert_not_called()


def test_later_target_error_precedes_first_execution_overlap(execution_ground):
    inputs, inventory, unrelated = execution_ground
    path = next(iter(inputs.values()))[0]
    mounts = (
        docker.ContainerMount(path, Path("/review")),
        docker.ContainerMount(unrelated, Path("/home/dev/.cache/uv/child")),
    )
    with pytest.raises(docker.MountCollisionError, match="Djinn-managed path"):
        validate(mounts, unrelated)
    inventory.assert_not_called()


@pytest.fixture
def creation_context(execution_ground, djinn_named_project_root, monkeypatch):
    _inputs, _inventory, source = execution_ground
    monkeypatch.setattr(docker, "get_project_root", lambda: djinn_named_project_root)
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda config: [])
    monkeypatch.setattr(docker, "get_sops_age_key_mount_args", lambda config: [])
    monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda config: ([], ()))
    monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
    return AppConfig(code_dir=source)


@pytest.mark.parametrize(
    "input_name",
    [
        "Djinn installation/build context",
        "Python environment",
        "host PATH directory",
    ],
)
@pytest.mark.parametrize("read_only", [False, True])
def test_doctor_execution_bind(creation_context, execution_ground, input_name, read_only):
    inputs, _inventory, _unrelated = execution_ground
    path = inputs[input_name][0]
    config = creation_context.model_copy(
        update={
            "mounts": {
                "review": {"source": str(path), "target": "/review", "read_only": read_only},
            }
        }
    )
    (row,) = doctor.declaration_checks(config)
    assert row.name == "mounts.review"
    if read_only:
        assert row.status is doctor.Status.PASS
        assert row.detail == "valid declaration (read-only)"
    else:
        assert row.status is doctor.Status.FAIL
        assert row.detail == (
            f"Declared mount 'review': writable source '{path}' overlaps host execution input "
            f"{input_name} ({path}); set read_only = true or remove the mount."
        )


@pytest.mark.parametrize("site", ["inventory", "canonical", "samefile"])
def test_doctor_fail_closed(creation_context, execution_ground, monkeypatch, site):
    _inputs, inventory, source = execution_ground
    failure = OSError("input unavailable")
    if site == "inventory":
        inventory.side_effect = failure
    elif site == "canonical":
        monkeypatch.setattr(host_sealing, "canonical", Mock(side_effect=failure))
    else:
        monkeypatch.setattr(Path, "samefile", Mock(side_effect=failure))
    config = creation_context.model_copy(
        update={
            "mounts": {
                "review": {"source": str(source), "target": "/review"},
            }
        }
    )
    (row,) = doctor.declaration_checks(config)
    assert row.status is doctor.Status.FAIL
    assert row.detail == (
        f"Declared mount 'review': writable source '{source}' cannot be checked against host "
        "execution inputs (input unavailable); set read_only = true or remove the mount."
    )


@pytest.mark.parametrize(("creator", "mode"), [
    (creator, mode)
    for creator in ("interactive", "headless", "detached")
    for mode in (docker.DockerMode.NONE, docker.DockerMode.AGENT)
    if creator != "headless" or mode is not docker.DockerMode.AGENT
])
@pytest.mark.parametrize("channel", ["declaration", "mount", "here"])
def test_execution_refusal_precedes_creation(
    creation_context,
    execution_ground,
    monkeypatch,
    creator,
    mode,
    channel,
):
    monkeypatch.setattr(docker, "validate_managed_bind_sources", lambda config: None)
    inputs, _inventory, _source = execution_ground
    path = next(iter(inputs.values()))[0]
    config = creation_context
    mounts = ()
    if channel == "declaration":
        config = config.model_copy(
            update={
                "mounts": {
                    "review": {"source": str(path), "target": "/review"},
                }
            }
        )
    else:
        target = Path("/home/dev/workspace" if channel == "here" else "/review")
        mounts = (docker.ContainerMount(path, target),)
    untouched = []
    for obj, name in (
        (docker.subprocess, "run"),
        (docker.subprocess, "Popen"),
        (docker.tempfile, "mkstemp"),
        (docker, "_prepare_workspace"),
        (docker, "_prepare_companions"),
    ):
        spy = Mock(side_effect=AssertionError(f"unexpected {name}"))
        monkeypatch.setattr(obj, name, spy)
        untouched.append(spy)
    options = docker.ContainerOptions(docker_mode=mode, mounts=mounts)
    with pytest.raises(MountSpecificationError, match="overlaps host execution input"):
        if creator == "detached":
            docker.compose_up_detached(config, options)
        elif creator == "headless":
            monkeypatch.setattr(
                agent_runner,
                "load_agents",
                lambda: {
                    "example": AgentConfig(
                        binary="example",
                        description="Example agent",
                    )
                },
            )
            agent_runner.run_headless_agent(
                "example",
                "test prompt",
                docker_mode=mode,
                resolved_mounts=mounts,
                app_config=config,
            )
        else:
            docker.compose_run(config, options)
    for spy in untouched:
        spy.assert_not_called()


@pytest.mark.parametrize("here", [False, True])
def test_detached_literal_paths(
    creation_context,
    execution_ground,
    tmp_path,
    monkeypatch,
    generated_overrides,
    here,
):
    inputs, _inventory, _source = execution_ground
    source = tmp_path / "$NAME"
    source.mkdir()
    input_path = next(iter(inputs.values()))[0]
    monkeypatch.setenv("NAME", input_path.name)
    assert str(source).replace("$NAME", input_path.name) == str(input_path)
    monkeypatch.chdir(source)
    mounts = docker.resolve_container_mounts(
        () if here else (f"{source}:/review/$NAME",),
        here=here,
    )
    fragments, paths = [], []

    def capture(cmd, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        (override,) = generated_overrides(cmd)
        fragments.append(json.loads(override.read_text()))
        paths.append(override)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(docker.subprocess, "run", capture)
    result = docker.compose_up_detached(
        creation_context,
        docker.ContainerOptions(mounts=mounts),
    )
    assert result.success
    service = fragments[0]["services"]["dev"]
    target = "/home/dev/workspace" if here else "/review/$$NAME"
    assert service["volumes"] == [f"{tmp_path}/$$NAME:{target}"]
    assert service["working_dir"] == target
    assert source.is_dir()
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("command", ["start", "detached", "run"])
@pytest.mark.parametrize("channel", ["declaration", "mount", "here"])
@pytest.mark.parametrize("failure", ["overlap", "inventory", "canonical", "samefile"])
def test_cli_execution_refusal(
    creation_context,
    execution_ground,
    monkeypatch,
    command,
    channel,
    failure,
):
    for module in (docker, container, agent):
        monkeypatch.setattr(module, "validate_managed_bind_sources", lambda config: None)
    inputs, inventory, unrelated = execution_ground
    name, (input_path, *_rest) = next(iter(inputs.items()))
    source = input_path if failure == "overlap" else unrelated
    if failure == "inventory":
        inventory.side_effect = ValueError("input unavailable")
    elif failure == "canonical":
        monkeypatch.setattr(
            host_sealing, "canonical", Mock(side_effect=OSError("input unavailable"))
        )
    elif failure == "samefile":
        monkeypatch.setattr(Path, "samefile", Mock(side_effect=OSError("input unavailable")))
    config = creation_context
    flags = []
    if channel == "declaration":
        config = config.model_copy(
            update={
                "mounts": {
                    "review": {"source": str(source), "target": "/review"},
                }
            }
        )
        if command == "run":
            flags = ["--mount", f"{unrelated}:ro"]
    elif channel == "mount":
        flags = ["--mount", f"{source}:/review"]
    else:
        monkeypatch.chdir(source)
        flags = [] if command == "run" else ["--here"]
    monkeypatch.setattr(container, "load_config", lambda: config)
    monkeypatch.setattr(container, "preflight", lambda *a, **kw: None)
    monkeypatch.setattr(container, "ensure_network", lambda: True)
    monkeypatch.setattr(container, "is_container_running", lambda *a: False)
    monkeypatch.setattr(container, "get_shell_mount_args", lambda config: [])
    monkeypatch.setattr(
        container, "prepare_config_workflow", lambda *a, **kw: WorkflowPreparationResult(True)
    )
    monkeypatch.setattr(agent, "load_config", lambda: config)
    monkeypatch.setattr(
        agent, "prepare_config_workflow", lambda *a, **kw: WorkflowPreparationResult(True)
    )
    monkeypatch.setattr(
        agent, "workflow_image_compatible", lambda: docker.WorkflowImageCompatibility.COMPATIBLE
    )
    monkeypatch.setattr(
        agent_runner,
        "load_agents",
        lambda: {
            "example": AgentConfig(
                binary="example",
                description="Example agent",
            )
        },
    )
    monkeypatch.setattr(
        "djinn_in_a_box.core.console.err_console", Console(width=1000, no_color=True)
    )
    untouched = []
    for obj, attr in (
        (docker.subprocess, "run"),
        (docker.subprocess, "Popen"),
        (docker.tempfile, "mkstemp"),
        (docker, "_prepare_workspace"),
    ):
        spy = Mock(side_effect=AssertionError(f"unexpected {attr}"))
        monkeypatch.setattr(obj, attr, spy)
        untouched.append(spy)
    argv = ["run", "example", "test prompt"] if command == "run" else ["start"]
    if command == "detached":
        argv.append("--detach")
    result = CliRunner().invoke(app, [*argv, *flags])
    assert result.exit_code == 1, result.output
    cause = (
        f"overlaps host execution input {name} ({input_path})"
        if failure == "overlap"
        else "cannot be checked against host execution inputs (input unavailable)"
    )
    if channel == "declaration":
        expected = (
            f"Declared mount 'review': writable source '{source}' {cause}; "
            "set read_only = true or remove the mount."
        )
    elif channel == "mount":
        expected = (
            f"Mount {source} -> /review is writable and {cause}; append :ro or remove the mount"
        )
    else:
        identity = f"--here mounts {source} writable, which {cause}"
        expected = (
            f"{identity}; start from a directory outside it or mount it read-only "
            f"with --mount {source}:ro"
        )
    assert expected in result.output, result.output
    for spy in untouched:
        spy.assert_not_called()


@pytest.fixture
def real_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setenv("PATH", "/opt/tools")
    monkeypatch.setattr(sys, "path", [])
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "problem",
    [
        "relative-path", "non-object", "plugin-null", "plugin-relative", "plugin-string",
        "plugin-object", "plugin-object-keys", "plugin-number",
    ],
)
@pytest.mark.parametrize("channel", ["declaration", "invocation"])
def test_real_inventory_fail_closed(real_inventory, monkeypatch, problem, channel):
    source = real_inventory / "source"
    source.mkdir()
    if problem == "relative-path":
        monkeypatch.setenv("PATH", "relative")
    else:
        contents = {
            "non-object": "[]",
            "plugin-null": '{"cliPluginsExtraDirs": null}',
            "plugin-relative": '{"cliPluginsExtraDirs": ["relative"]}',
            "plugin-string": '{"cliPluginsExtraDirs": ""}',
            "plugin-object": '{"cliPluginsExtraDirs": {}}',
            "plugin-object-keys": '{"cliPluginsExtraDirs": {"/opt/plugins": 1}}',
            "plugin-number": '{"cliPluginsExtraDirs": [1]}',
        }[problem]
        (real_inventory / "config.json").write_text(contents)
    if channel == "declaration":
        result = resolve(source)
        assert "cannot be checked against host execution inputs (" in result.diagnostics[0].error
        with pytest.raises(DeclarationSpecificationError):
            result.require_valid()
    else:
        with pytest.raises(
            MountSpecificationError, match=r"cannot be checked against host execution inputs \("
        ):
            validate((docker.ContainerMount(source, Path("/review")),), source)


def test_real_inventory_empty_path_and_check_time(real_inventory, monkeypatch):
    first = host_sealing.execution_paths()
    assert "host PATH directory /opt/tools" in first
    monkeypatch.setenv("PATH", "")
    second = host_sealing.execution_paths()
    name = f"host PATH directory {real_inventory}"
    assert second[name][0] == real_inventory
    assert "host PATH directory /opt/tools" not in second
    assert host_sealing.execution_overlap(real_inventory, second) is not None
    with pytest.raises(MountSpecificationError, match="overlaps host execution input"):
        validate(
            (docker.ContainerMount(real_inventory, Path("/home/dev/workspace")),), real_inventory
        )


@pytest.fixture
def inspection_inventory(real_inventory, monkeypatch):
    source = real_inventory / "source"
    source.mkdir()
    config_dir = real_inventory / "docker-config"
    config_dir.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(config_dir))
    return source, config_dir


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
@pytest.mark.parametrize("input_kind", ["docker-config", "path-alias"])
@pytest.mark.parametrize("channel", ["declaration", "invocation", "here", "doctor"])
@pytest.mark.parametrize("mode", ["rw", "ro", "empty"])
def test_inaccessible_execution_input(inspection_inventory, monkeypatch, input_kind, channel, mode):
    source, config_dir = inspection_inventory
    if input_kind == "docker-config":
        input_path = config_dir / "config.json"
        input_path.write_text(json.dumps({"cliPluginsExtraDirs": [str(source)]}))
        hidden = config_dir
        name = f"Docker plugin directory {source}"
    else:
        hidden = source.parent / "hidden"
        hidden.mkdir()
        input_path = hidden / "bin"
        input_path.symlink_to(source, target_is_directory=True)
        monkeypatch.setenv("PATH", str(input_path))
        name = f"host PATH directory {input_path}"
    assert host_sealing.execution_overlap(source, host_sealing.execution_paths()) == (name, source)
    original_mode = hidden.stat().st_mode
    hidden.chmod(0)
    try:
        with pytest.raises(PermissionError) as failure:
            if input_kind == "docker-config":
                input_path.read_text()
            else:
                input_path.stat()
        # Inventory failure must be observable before any overlap comparison.
        with pytest.raises(PermissionError) as inventory_failure:
            host_sealing.execution_paths()
        assert str(inventory_failure.value) == str(failure.value)
        cause = f"cannot be checked against host execution inputs ({failure.value})"
        read_only = mode == "ro"
        if channel in ("declaration", "doctor"):
            mounts = {} if mode == "empty" else {
                "review": {"source": str(source), "target": "/review", "read_only": read_only}
            }
            config = AppConfig(code_dir=source, mounts=mounts)
            if channel == "doctor":
                rows = doctor.declaration_checks(config)
                if mode == "empty":
                    assert not rows
                else:
                    (row,) = rows
                    assert row.name == "mounts.review"
                    expected_status = doctor.Status.FAIL if mode == "rw" else doctor.Status.PASS
                    assert row.status is expected_status
                    if mode == "rw":
                        assert cause in row.detail
            else:
                result = docker.resolve_declared_entries(
                    config, docker.ContainerOptions(), runtime_targets=[], caller_env=None
                )
                if mode == "rw":
                    assert result.diagnostics[0].error == (
                        f"Declared mount 'review': writable source '{source}' {cause}; "
                        "set read_only = true or remove the mount."
                    )
                    with pytest.raises(DeclarationSpecificationError):
                        result.require_valid()
                else:
                    result.require_valid()
        else:
            monkeypatch.chdir(source)
            if mode == "empty":
                mounts = ()
            elif channel == "here" and mode == "rw":
                mounts = docker.resolve_container_mounts((), here=True)
            else:
                mounts = docker.resolve_container_mounts((f"{source}:/review:{mode}",))
            if mode == "rw":
                with pytest.raises(MountSpecificationError) as error:
                    validate(mounts, source)
                assert cause in str(error.value)
                identity = "--here mounts" if channel == "here" else f"Mount {source} -> /review"
                assert identity in str(error.value)
            else:
                validate(mounts, source)
    finally:
        hidden.chmod(original_mode)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_execution_source_is_strict_and_sealing_defaults_are_lenient(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    alias = hidden / "bin"
    alias.symlink_to(source, target_is_directory=True)
    original_mode = hidden.stat().st_mode
    hidden.chmod(0)
    try:
        with pytest.raises(PermissionError):
            alias.stat()
        assert host_sealing.canonical(alias) == alias
        assert not host_sealing.protected_overlap(alias, source)
        with pytest.raises(PermissionError):
            host_sealing.canonical(alias, strict=True)
        with pytest.raises(PermissionError):
            host_sealing.protected_overlap(alias, source, strict=True)
        with pytest.raises(PermissionError):
            host_sealing.execution_overlap(alias, {})
    finally:
        hidden.chmod(original_mode)


@pytest.mark.parametrize("channel", ["declaration", "invocation", "here"])
@pytest.mark.parametrize("failure_type", [PermissionError, OSError])
def test_strict_inode_presence_failure(execution_ground, monkeypatch, channel, failure_type):
    inputs, _inventory, source = execution_ground
    protected = next(iter(inputs.values()))[0]
    original_stat = Path.stat
    original_samefile = Path.samefile
    samefile_calls = []
    failure = failure_type("comparison stat unavailable")

    def stat(path, *args, **kwargs):
        if path == protected:
            raise failure
        return original_stat(path, *args, **kwargs)

    def samefile(a, b):
        samefile_calls.append((a, b))
        return (a == source and b == protected) or original_samefile(a, b)

    monkeypatch.setattr(Path, "samefile", samefile)
    assert host_sealing.execution_overlap(source, inputs) is not None
    samefile_calls.clear()
    monkeypatch.setattr(Path, "stat", stat)
    if channel == "declaration":
        result = resolve(source)
        cause = result.diagnostics[0].error
        with pytest.raises(DeclarationSpecificationError):
            result.require_valid()
    else:
        target = Path("/home/dev/workspace" if channel == "here" else "/review")
        with pytest.raises(MountSpecificationError) as error:
            validate((docker.ContainerMount(source, target),), source)
        cause = str(error.value)
    assert f"cannot be checked against host execution inputs ({failure})" in cause
    assert not samefile_calls


@pytest.mark.parametrize("side", ["source", "protected"])
def test_strict_inode_presence_checks_both_sides(tmp_path, monkeypatch, side):
    source, protected = tmp_path / "source", tmp_path / "protected"
    source.mkdir()
    protected.mkdir()
    original_stat = Path.stat
    failing_path = source if side == "source" else protected

    def stat(path, *args, **kwargs):
        if path == failing_path:
            raise PermissionError("comparison stat unavailable")
        return original_stat(path, *args, **kwargs)

    samefile = Mock(return_value=False)
    monkeypatch.setattr(Path, "samefile", samefile)
    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(PermissionError, match="comparison stat unavailable"):
        host_sealing.protected_overlap(source, protected, strict=True)
    samefile.assert_not_called()


@pytest.mark.parametrize("absence", ["missing", "not-directory"])
def test_strict_presence_accepts_proven_absence(tmp_path, absence):
    assert host_sealing._present(tmp_path)
    parent = tmp_path / absence
    if absence == "not-directory":
        parent.touch()
    path = parent / "child"
    assert not host_sealing._present(path)
    assert host_sealing.canonical(path, strict=True) == path
    assert not host_sealing.protected_overlap(path, tmp_path / "other", strict=True)


@pytest.mark.parametrize(
    "helper,args",
    [
        (host_sealing.canonical, (Path("/opt/tools"),)),
        (host_sealing.protected_overlap, (Path("/opt/tools"), Path("/opt/tools"))),
    ],
)
def test_strict_option_is_keyword_only(helper, args):
    with pytest.raises(TypeError):
        helper(*args, True)


@pytest.mark.parametrize("absence", ["missing", "not-directory"])
def test_missing_docker_config_is_accepted(inspection_inventory, monkeypatch, absence):
    source, config_dir = inspection_inventory
    if absence == "not-directory":
        config_dir.rmdir()
        config_dir.touch()
    inputs = host_sealing.execution_paths()
    assert host_sealing.execution_overlap(source, inputs) is None
    resolve(source).require_valid()
    validate((docker.ContainerMount(source, Path("/review")),), source)


def _path_through_link(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    (tools / "bin").mkdir(parents=True)
    state = tmp_path / "state"
    (state / "multishells").mkdir(parents=True)
    hop = state / "multishells" / "123"
    hop.symlink_to(tools, target_is_directory=True)
    monkeypatch.setenv("PATH", f"{hop}/bin")
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "docker-config"))
    return state, hop, tools


@pytest.mark.parametrize("channel", ["declaration", "invocation"])
@pytest.mark.parametrize("read_only", [False, True])
def test_link_location_on_execution_path_is_protected(
    real_inventory, monkeypatch, channel, read_only
):
    state, hop, tools = _path_through_link(real_inventory, monkeypatch)
    inventory = host_sealing.execution_paths()
    name = f"host PATH directory {hop}/bin via link {hop}"
    assert inventory[name] == (hop, "writable PATH directory", f"{hop}/bin")
    assert inventory[f"host PATH directory {hop}/bin"][0] == tools / "bin"
    cause = f"overlaps host execution input {name} ({hop})"
    if channel == "declaration":
        error = resolve(state, read_only=read_only).diagnostics[0].error
        assert error is None if read_only else cause in error
    elif read_only:
        validate((docker.ContainerMount(state, Path("/review"), True),), state)
    else:
        with pytest.raises(MountSpecificationError, match=re.escape(cause)):
            validate((docker.ContainerMount(state, Path("/review")),), state)


def test_link_locations_follow_nested_relative_and_missing_paths(tmp_path):
    root = tmp_path.resolve()
    (root / "real" / "x").mkdir(parents=True)
    (root / "a").mkdir()
    (root / "b").mkdir()
    (root / "b" / "link2").symlink_to(root / "real", target_is_directory=True)
    (root / "a" / "link1").symlink_to("../b/link2", target_is_directory=True)
    assert host_sealing._link_locations(root / "a" / "link1" / "x") == [
        root / "a" / "link1",
        root / "b" / "link2",
    ]
    assert host_sealing._link_locations(root / "a" / "missing" / "x") == []
    (root / "loop1").symlink_to(root / "loop2")
    (root / "loop2").symlink_to(root / "loop1")
    with pytest.raises(ValueError, match="too many host aliases"):
        host_sealing._link_locations(root / "loop1" / "bin")


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("target", ["/review", "/home/dev/workspace"])
def test_invocation_source_with_colon_is_refused(tmp_path, read_only, target):
    source = tmp_path / "input:x"
    source.mkdir()
    with pytest.raises(MountSpecificationError, match="must not contain ':'"):
        validate((docker.ContainerMount(source, Path(target), read_only),), tmp_path)


@pytest.mark.parametrize("channel", ["declaration", "invocation"])
@pytest.mark.parametrize("bind", ["project-in-link-target", "link-directory", "path-entry-parent"])
def test_link_entry_protects_its_location_not_its_target(
    real_inventory, monkeypatch, channel, bind
):
    disk_home = real_inventory / "disk" / "u"
    (disk_home / ".local" / "bin").mkdir(parents=True)
    (disk_home / "projects" / "foo").mkdir(parents=True)
    (real_inventory / "home").mkdir()
    link = real_inventory / "home" / "u"
    link.symlink_to(disk_home, target_is_directory=True)
    monkeypatch.setenv("PATH", f"{link}/.local/bin")
    monkeypatch.setenv("DOCKER_CONFIG", str(real_inventory / "docker-config"))
    source = {
        "project-in-link-target": disk_home / "projects" / "foo",
        "link-directory": real_inventory / "home",
        "path-entry-parent": disk_home / ".local",
    }[bind]
    refused = bind != "project-in-link-target"
    if channel == "declaration":
        error = resolve(source).diagnostics[0].error
        assert (error is not None and "overlaps host execution input" in error) is refused
    elif refused:
        with pytest.raises(MountSpecificationError, match="overlaps host execution input"):
            validate((docker.ContainerMount(source, Path("/review")),), source)
    else:
        validate((docker.ContainerMount(source, Path("/review")),), source)


def test_link_entry_detects_an_alias_of_its_directory(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / "target").mkdir()
    (root / "state").mkdir()
    link = root / "state" / "link"
    link.symlink_to(root / "target", target_is_directory=True)
    alias = root / "alias"
    alias.mkdir()
    samefile = Path.samefile
    monkeypatch.setattr(
        Path, "samefile", lambda a, b: (a == alias and b == root / "state") or samefile(a, b)
    )
    inputs = {"host PATH directory via link": (link, "writable PATH directory", "item")}
    assert host_sealing.execution_overlap(alias, inputs) == ("host PATH directory via link", link)
