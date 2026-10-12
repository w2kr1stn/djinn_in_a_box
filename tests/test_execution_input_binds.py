"""Writable declared binds must not expose the creating CLI's execution inputs."""

import json
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


@pytest.mark.parametrize("creator", ["interactive", "headless", "detached"])
@pytest.mark.parametrize("mode", [docker.DockerMode.NONE, docker.DockerMode.AGENT])
@pytest.mark.parametrize("channel", ["declaration", "mount", "here"])
def test_execution_refusal_precedes_creation(
    creation_context,
    execution_ground,
    monkeypatch,
    creator,
    mode,
    channel,
):
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
            monkeypatch.setattr(agent_runner, "ensure_network", lambda: True)
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
    monkeypatch.setattr(agent_runner, "ensure_network", lambda: True)
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
    "problem", ["relative-path", "non-object", "plugin-null", "plugin-relative"]
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
