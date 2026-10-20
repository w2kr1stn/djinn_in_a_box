"""Managed writable sources use the execution-input boundary before creation."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import agent, container, doctor
from djinn_in_a_box.config import loader, zones
from djinn_in_a_box.config.defaults import SYNC_PATHS
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import docker, git_diagnostics, host_sealing
from djinn_in_a_box.core.exceptions import ConfigValidationError, MountSpecificationError

LABELS = {
    "workspace": "Workspace root",
    "config root": "Config-root tool directory",
    "zones": "Zone overlay source",
    "sessions": "Sessions directory",
}
DESCRIPTIONS = {
    "workspace": "general.code_dir",
    "config root": "general.config_root (or DJINN_CONFIG_ROOT)",
    "zones": "general.shared_root / general.local_root",
    "sessions": "~/.djinn/sessions",
}
REMEDIES = {
    "workspace": "move that input out of the workspace root or set "
    "general.code_dir to another directory",
    "config root": "move that input out of the config root or set general.config_root "
    "(or DJINN_CONFIG_ROOT) to another directory",
    "zones": "move that input out of the zone roots or set "
    "general.shared_root / general.local_root to another directory",
    "sessions": "move that input out of ~/.djinn/sessions",
}


@pytest.fixture
def real_execution_paths():
    return host_sealing.execution_paths


@pytest.fixture
def ground(tmp_path, monkeypatch, real_execution_paths):
    home = tmp_path / "home"
    home.mkdir()
    code = tmp_path / "code"
    code.mkdir()
    install = tmp_path / "install"
    install.mkdir()
    inputs = {"installation": (install, "execution input", "installation")}
    inventory = Mock(return_value=inputs)
    monkeypatch.setattr(host_sealing, "execution_paths", inventory)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
    monkeypatch.setattr(
        zones,
        "DEFAULT_ZONES",
        {name: {"local": [], "shared": []} for name in zones.ZONE_CONTAINER_TARGETS},
    )
    zone_file = tmp_path / "zones.toml"
    zone_file.write_text(
        "".join(
            f"[zones.{name}]\nlocal = []\nshared = []\n"
            for name in ("codex", "opencode", "gh", "age")
        )
        + '[zones.claude]\nlocal = ["state"]\nshared = ["notes"]\n'
    )
    monkeypatch.setattr(zones, "ZONES_FILE", zone_file)
    return AppConfig(code_dir=code, config_root=tmp_path / "config"), inputs, inventory


def protect(inputs, path):
    inputs.clear()
    inputs["protected"] = (path, "execution input", "protected")


def expected(family, source, path):
    return (
        f"{LABELS[family]} {source} is mounted writable and "
        f"overlaps host execution input protected ({path}); {REMEDIES[family]}"
    )


def expected_construction(family, cause):
    return (
        f"{LABELS[family]} ({DESCRIPTIONS[family]}) cannot be checked against "
        f"host execution inputs ({cause}); fix that setting or file and retry"
    )


def set_sessions_home(ground, monkeypatch, real_execution_paths, kind="missing-dotdot"):
    config, _, _ = ground
    actual_home = Path.home()
    missing = config.code_dir.parent / "missing"
    path_input = actual_home / ".djinn/sessions/bin"
    path_input.mkdir(parents=True)
    if kind == "missing-dotdot":
        home = missing / ".." / actual_home.name
    else:
        home = actual_home.with_name("linked-home")
        home.symlink_to(actual_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(path_input))
    monkeypatch.setenv("DOCKER_CONFIG", str(config.code_dir.parent / "docker-config"))
    monkeypatch.setattr(host_sealing, "execution_paths", real_execution_paths)
    message = (
        f"Sessions directory {actual_home / '.djinn/sessions'} is mounted writable and "
        f"overlaps host execution input host PATH directory {path_input} ({path_input}); "
        f"{REMEDIES['sessions']}"
    )
    return missing, message


@pytest.mark.parametrize("kind", ["missing-dotdot", "symlink"])
def test_sessions_home_checks_actual_path(ground, monkeypatch, real_execution_paths, kind):
    config, _, _ = ground
    missing, message = set_sessions_home(ground, monkeypatch, real_execution_paths, kind)
    assert docker.managed_bind_problems(config) == [message]
    assert not missing.exists()


def test_relative_home_fails_closed(ground, monkeypatch):
    config, _, _ = ground
    monkeypatch.setenv("HOME", "relative-home")
    source = Path("relative-home/.djinn/sessions")
    assert docker.managed_bind_problems(config) == [
        f"Sessions directory {source} is mounted writable and cannot be checked against "
        f"host execution inputs (non-absolute host path: {source}); {REMEDIES['sessions']}"
    ]


@pytest.mark.parametrize(
    "relation", ["equal", "inside", "contains", "inode", "symlink", "unrelated"]
)
def test_managed_relation_matrix(ground, tmp_path, monkeypatch, relation):
    config, inputs, _ = ground
    source = config.code_dir
    path = source
    if relation == "inside":
        path = tmp_path
    elif relation == "contains":
        path = source / "future" / "executable"
    elif relation == "inode":
        path = tmp_path / "alias"
        path.mkdir()
        original = Path.samefile
        monkeypatch.setattr(
            Path, "samefile", lambda a, b: (a == source and b == path) or original(a, b)
        )
    elif relation == "symlink":
        source = tmp_path / "link"
        source.symlink_to(config.code_dir, target_is_directory=True)
        config = config.model_copy(update={"code_dir": source})
    elif relation == "unrelated":
        path = tmp_path / "elsewhere"
    protect(inputs, path)
    problems = docker.managed_bind_problems(config)
    if relation == "unrelated":
        assert problems == []
    else:
        assert expected("workspace", source, path) in problems


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_all_managed_sources_and_order(ground, tmp_path, monkeypatch, explicit, override):
    config, inputs, inventory = ground
    if override:
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(tmp_path / "override"))
    root = tmp_path / ("override" if override else "config")
    if explicit:
        config = config.model_copy(
            update={
                "shared_root": tmp_path / "shared",
                "local_root": tmp_path / "local",
            }
        )
    local = config.local_root or Path(f"{root}.local")
    shared = config.shared_root or Path(f"{root}.shared")
    sources = [
        ("workspace", config.code_dir),
        *(("config root", root / name) for name in SYNC_PATHS["credentials"]),
        ("zones", local / "claude/state"),
        ("zones", shared / "claude/notes"),
        ("sessions", Path.home() / ".djinn/sessions"),
    ]
    assert docker.managed_bind_problems(config) == []
    for family, source in sources:
        protect(inputs, source / "future-input")
        assert docker.managed_bind_problems(config) == [
            expected(family, source, source / "future-input")
        ]
    protect(inputs, tmp_path)
    assert docker.managed_bind_problems(config) == [
        expected(family, source, tmp_path) for family, source in sources
    ]
    assert inventory.call_count == len(sources) + 2
    with pytest.raises(MountSpecificationError) as exc:
        docker.validate_managed_bind_sources(config)
    assert str(exc.value) == expected("workspace", config.code_dir, tmp_path)
    assert not root.exists()
    assert not local.exists()
    assert not shared.exists()
    assert not (Path.home() / ".djinn").exists()


@pytest.mark.parametrize("error_type", [OSError, ValueError, KeyError, TypeError, RuntimeError])
def test_inventory_failure_is_cached_and_closed(ground, error_type):
    config, _, inventory = ground
    inventory.side_effect = error_type("inventory unavailable")
    problems = docker.managed_bind_problems(config)
    assert len(problems) == 9
    assert all("cannot be checked against host execution inputs" in p for p in problems)
    inventory.assert_called_once_with()


@pytest.mark.parametrize("failure", ["relative", "inspection"])
def test_source_failure_is_closed(ground, monkeypatch, failure):
    config, _, _ = ground
    if failure == "relative":
        config = config.model_copy(update={"code_dir": Path("relative")})
    else:
        original = host_sealing.canonical

        def canonical(path, **kwargs):
            if path == config.code_dir:
                raise PermissionError("source unreadable")
            return original(path, **kwargs)

        monkeypatch.setattr(host_sealing, "canonical", canonical)
    problems = docker.managed_bind_problems(config)
    assert len(problems) == 1
    cause = (
        f"non-absolute host path: {config.code_dir}"
        if failure == "relative"
        else "source unreadable"
    )
    assert problems[0] == (
        f"{LABELS['workspace']} {config.code_dir} is mounted writable and cannot be checked "
        f"against host execution inputs ({cause}); {REMEDIES['workspace']}"
    )


@pytest.mark.parametrize("family", ["workspace", "config root", "zones", "sessions"])
@pytest.mark.parametrize(
    "error_type", [OSError, ValueError, KeyError, TypeError, RuntimeError, ConfigValidationError]
)
def test_family_construction_failures_are_contained(ground, monkeypatch, family, error_type):
    config, _, _ = ground
    broken = Mock(side_effect=error_type("construction unavailable"))
    if family == "workspace":
        monkeypatch.setattr(Path, "expanduser", broken)
    elif family == "config root":
        monkeypatch.setattr(docker, "get_config_root", broken)
    elif family == "zones":
        monkeypatch.setattr(docker, "resolve_zone_roots", broken)
    else:
        monkeypatch.setattr(Path, "home", broken)
    problems = docker.managed_bind_problems(config)
    assert expected_construction(family, str(error_type("construction unavailable"))) in problems
    assert all("construction unavailable" in p for p in problems)


@pytest.fixture
def creators(ground, monkeypatch, djinn_named_project_root):
    monkeypatch.setattr(docker, "get_project_root", lambda: djinn_named_project_root)
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda config: [])
    monkeypatch.setattr(docker, "get_sops_age_key_mount_args", lambda config: [])
    monkeypatch.setattr(docker, "is_background_process_group", lambda: False)
    monkeypatch.setattr(docker, "_guard_dev_creation", lambda *a: None)
    return ground


def create(kind, config, mode):
    options = docker.ContainerOptions(docker_mode=mode)
    if kind == "detached":
        return docker.compose_up_detached(config, options)
    return docker.compose_run(config, options, interactive=kind == "attached")


@pytest.mark.parametrize("kind", ["attached", "headless", "detached"])
@pytest.mark.parametrize("mode", list(docker.DockerMode))
@pytest.mark.parametrize("failure", ["overlap", "zones", "sessions"])
def test_managed_refusal_precedes_all_creation(
    creators, monkeypatch, real_execution_paths, kind, mode, failure
):
    config, inputs, inventory = creators
    if failure == "overlap":
        protect(inputs, config.code_dir)
    elif failure == "sessions":
        missing, message = set_sessions_home(creators, monkeypatch, real_execution_paths)
    else:
        zones.ZONES_FILE.write_text("[invalid")
    spies = []
    for obj, name in (
        (docker, "is_background_process_group"),
        (docker, "_validate_desktop_env"),
        (docker, "get_project_root"),
        (docker, "get_compose_files"),
        (docker.subprocess, "run"),
        (docker.subprocess, "Popen"),
        (docker.tempfile, "mkstemp"),
        (docker, "_prepare_workspace"),
        (docker, "_prepare_companions"),
        (docker, "git_runtime"),
        (docker, "get_shell_mount_args"),
        (docker, "get_sops_age_key_mount_args"),
        (docker, "_zone_overlay_mount_args_and_targets"),
        (docker, "resolve_declared_entries"),
        (docker, "validate_container_mounts"),
    ):
        spy = Mock(side_effect=AssertionError(f"unexpected {name}"))
        monkeypatch.setattr(obj, name, spy)
        spies.append(spy)
    with pytest.raises(MountSpecificationError) as exc:
        create(kind, config, mode)
    if failure == "overlap":
        assert str(exc.value) == expected("workspace", config.code_dir, config.code_dir)
    elif failure == "sessions":
        assert str(exc.value) == message
        assert not missing.exists()
    else:
        assert str(exc.value).startswith(f"{LABELS['zones']} ")
        assert "cannot be checked against host execution inputs (Invalid TOML" in str(exc.value)
    if failure != "sessions":
        inventory.assert_called_once_with()
    for spy in spies:
        spy.assert_not_called()


@pytest.mark.parametrize("kind", ["attached", "headless", "detached"])
@pytest.mark.parametrize("mode", list(docker.DockerMode))
def test_literal_delivery_and_seed_exclusion(
    creators,
    monkeypatch,
    tmp_path,
    generated_overrides,
    kind,
    mode,
):
    config, inputs, inventory = creators
    literal = tmp_path / "$NAME" / "${NAME}"
    config = config.model_copy(update={"local_root": literal, "shared_root": tmp_path / "shared"})
    zones.ZONES_FILE.write_text('[zones.claude]\nlocal = ["state/$NAME"]\nshared = ["notes"]\n')
    source = literal / "claude/state/$NAME"
    source.mkdir(parents=True)
    runtime = tmp_path / "$NAME" / "shell.sock"
    runtime.touch()
    sops = tmp_path / "${NAME}" / "key"
    sops.parent.mkdir()
    sops.touch()
    monkeypatch.setenv("NAME", "install")
    protected = tmp_path / "install"
    protect(inputs, protected)
    inputs["installation"] = (docker.get_project_root(), "execution input", "installation")
    assert Path(
        str(literal).replace("$NAME", "install").replace("${NAME}", "install")
    ).is_relative_to(protected)
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda c: ["-v", f"{runtime}:/shell:ro"])
    monkeypatch.setattr(docker, "get_sops_age_key_mount_args", lambda c: ["-v", f"{sops}:/sops:ro"])
    captures = []
    guard_specs = []
    monkeypatch.setattr(docker, "_guard_dev_creation", lambda *a: guard_specs.extend(a[4:5]))

    def execute(cmd, **kwargs):
        fragments = [json.loads(p.read_text()) for p in generated_overrides(cmd)]
        captures.append((cmd, kwargs["env"], fragments))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(docker.subprocess, "run", execute)
    assert create(kind, config, mode).success
    cmd, env, fragments = captures[-1]
    assert env["HOME"] == str(Path.home())
    assert env["CODE_DIR"] == str(config.code_dir)
    raw = [f"{source}:/home/dev/.claude/state/$NAME", f"{runtime}:/shell:ro", f"{sops}:/sops:ro"]
    service = fragments[-1]["services"]["dev"]
    if kind == "detached":
        assert all(spec.replace("$", "$$") in service["volumes"] for spec in raw)
    else:
        assert docker._volume_specs_from_mount_args(cmd) == raw
        assert guard_specs == [raw]
    if mode is docker.DockerMode.AGENT:
        companion = fragments[-1]["services"]["agent-docker"]
        for path in (config.code_dir, Path.home() / ".djinn/sessions"):
            for delivered in (service, companion):
                assert any(
                    v.get("source") == str(path)
                    for v in delivered["volumes"]
                    if isinstance(v, dict)
                )
    # No invocation/declared writable mounts: only the new managed inventory runs.
    inventory.assert_called_once_with()


def test_home_unset_is_pinned_to_checked_sessions(ground, monkeypatch):
    config, _, _ = ground
    monkeypatch.delenv("HOME")
    monkeypatch.setattr(
        Path, "home", classmethod(lambda cls: config.code_dir.parent / "passwd-home")
    )
    checked = []
    monkeypatch.setattr(
        host_sealing, "execution_overlap", lambda path, inputs: checked.append(path)
    )
    assert docker.managed_bind_problems(config) == []
    env = docker._compose_host_env(config)
    assert Path(env["HOME"]) / ".djinn/sessions" == checked[-1]
    assert "HOME" not in docker.build_compose_env(config)
    fragment = {"services": {"dev": {}}}
    docker._prepare_workspace(
        config,
        docker.ContainerOptions(docker_mode=docker.DockerMode.AGENT),
        docker.ResolvedDeclarations([], {}, []),
        (),
        fragment,
    )
    for service in ("dev", "agent-docker"):
        assert any(
            volume["source"] == str(checked[-1])
            for volume in fragment["services"][service]["volumes"]
        )


@pytest.fixture
def quiet_doctor(monkeypatch):
    for name in (
        "_docker_installed",
        "docker_daemon_ok",
        "_docker_socket_ok",
        "compose_v2_ok",
        "buildx_ok",
        "_image_built",
        "network_exists",
    ):
        monkeypatch.setattr(doctor, name, lambda *a: True)
    monkeypatch.setattr(doctor, "agent_docker_checks", lambda **kw: [])
    monkeypatch.setattr(doctor, "hostctl_checks", lambda *a, **kw: [])
    monkeypatch.setattr(git_diagnostics, "git_diagnostics", lambda c: [])
    monkeypatch.setattr(
        docker,
        "inspect_running_desktop",
        lambda: Mock(channels=(), raw_sources=(), raw_verified=True),
    )


@pytest.mark.parametrize("overlap", [False, True])
def test_doctor_reports_all_managed_problems_without_provisioning(ground, quiet_doctor, overlap):
    config, inputs, _ = ground
    before = sorted(config.code_dir.parent.rglob("*"))
    if overlap:
        protect(inputs, config.code_dir.parent)
    problems = docker.managed_bind_problems(config)
    rows = [r for r in doctor.run_checks(config) if r.name == "Managed bind sources"]
    assert len(rows) == 1
    assert rows[0].status is (doctor.Status.FAIL if overlap else doctor.Status.PASS)
    assert rows[0].detail == (
        "; ".join(problems) if overlap else "no overlap with host execution inputs"
    )
    assert sorted(config.code_dir.parent.rglob("*")) == before
    assert bool(problems) is overlap


def test_doctor_sessions_dotdot_is_fail_without_provisioning(
    ground, quiet_doctor, monkeypatch, real_execution_paths
):
    config, _, _ = ground
    missing, message = set_sessions_home(ground, monkeypatch, real_execution_paths)
    before = sorted(config.code_dir.parent.rglob("*"))
    row = next(r for r in doctor.run_checks(config) if r.name == "Managed bind sources")
    assert row.status is doctor.Status.FAIL
    assert row.detail == message
    monkeypatch.setattr(loader, "load_config", lambda: config)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert any(
        "Managed bind sources" in line and "FAIL" in line for line in result.output.splitlines()
    )
    assert sorted(config.code_dir.parent.rglob("*")) == before
    assert not missing.exists()


def test_doctor_invalid_utf8_zones_renders_both_rows(ground, quiet_doctor, monkeypatch):
    config, _, _ = ground
    zones.ZONES_FILE.write_bytes(b"\xff")
    message = docker.managed_bind_problems(config)[0]
    assert message == expected_construction(
        "zones",
        f"Invalid TOML syntax in {zones.ZONES_FILE}: "
        "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
    )
    before = sorted(config.code_dir.parent.rglob("*"))
    monkeypatch.setattr(loader, "load_config", lambda: config)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert any(
        "Managed bind sources" in line and "FAIL" in line for line in result.output.splitlines()
    )
    assert "Zone configuration" in result.output
    assert f"Invalid TOML syntax in {zones.ZONES_FILE}" in result.output
    assert "fix that setting or file and retry" in result.output
    assert sorted(config.code_dir.parent.rglob("*")) == before


def test_doctor_contains_unexpandable_root(ground, quiet_doctor, monkeypatch):
    config, _, _ = ground
    monkeypatch.setenv("DJINN_CONFIG_ROOT", "~djinn154-user-does-not-exist/config")
    for name in ("_sops_age_key_check", "loose_credential_dirs", "_zone_diagnostic_checks"):
        monkeypatch.setattr(doctor, name, Mock(side_effect=AssertionError("dependent row reached")))
    rows = doctor.run_checks(config)
    assert next(r for r in rows if r.name == "Config root").status is doctor.Status.FAIL
    managed = next(r for r in rows if r.name == "Managed bind sources")
    assert managed.status is doctor.Status.FAIL
    assert "cannot be checked" in managed.detail
    assert not any(r.name in {"Credential and zone dir modes", "Zone configuration"} for r in rows)
    monkeypatch.setattr(loader, "load_config", lambda: config)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "Managed bind sources" in result.output
    assert "Config root" in result.output


@pytest.mark.parametrize("mode_args", [[], ["--docker"], ["--docker-direct"]])
@pytest.mark.parametrize("argv", [["start"], ["start", "--detach"], ["run", "claude", "test"]])
@pytest.mark.parametrize(
    "failure", ["overlap", "malformed", "unreadable", "root", "sessions", "utf8"]
)
def test_cli_managed_gate_before_real_workflow(
    ground, monkeypatch, real_execution_paths, argv, failure, mode_args
):
    config, inputs, _ = ground
    if failure == "overlap":
        protect(inputs, config.code_dir)
    elif failure == "sessions":
        missing, message = set_sessions_home(ground, monkeypatch, real_execution_paths)
        assert docker.managed_bind_problems(config) == [message]
    elif failure == "utf8":
        zones.ZONES_FILE.write_bytes(b"\xff")
    elif failure == "malformed":
        zones.ZONES_FILE.write_text("[broken")
    elif failure == "unreadable":
        original = Path.open

        def open_path(path, *a, **kw):
            if path == zones.ZONES_FILE:
                raise PermissionError("zones unreadable")
            return original(path, *a, **kw)

        monkeypatch.setattr(Path, "open", open_path)
    else:
        monkeypatch.setenv("DJINN_CONFIG_ROOT", "~djinn154-user-does-not-exist/config")
    message = docker.managed_bind_problems(config)[0]
    monkeypatch.setattr(container, "load_config", lambda: config)
    monkeypatch.setattr(agent, "load_config", lambda: config)
    monkeypatch.setattr(container, "is_container_running", lambda *a: False)
    spies = []
    for obj, name in (
        (container, "preflight"),
        (container, "compose_run"),
        (container, "compose_up_detached"),
        (agent, "run_headless_agent"),
        (agent, "workflow_image_compatible"),
        (docker.subprocess, "run"),
        (docker.subprocess, "Popen"),
    ):
        spy = Mock(side_effect=AssertionError(f"unexpected {name}"))
        monkeypatch.setattr(obj, name, spy)
        spies.append(spy)
    before = sorted(config.code_dir.parent.rglob("*"))
    result = CliRunner().invoke(app, [*argv, *mode_args])
    assert result.exit_code == 1, result.output
    assert message in result.output, result.output
    assert isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output
    if failure == "sessions":
        assert not missing.exists()
    assert sorted(config.code_dir.parent.rglob("*")) == before
    for spy in spies:
        spy.assert_not_called()


def test_workspace_expansion_and_execution_symlink_location(ground, tmp_path):
    config, inputs, _ = ground
    config = config.model_copy(update={"code_dir": Path("~/work")})
    source = Path.home() / "work"
    source.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    link = source / "tool"
    link.symlink_to(external, target_is_directory=True)
    protect(inputs, link)
    assert docker.managed_bind_problems(config) == [expected("workspace", source, link)]


def test_every_agent_and_zone_assignment_is_enumerated(ground):
    config, _, _ = ground
    zones.ZONES_FILE.write_text(
        "".join(
            f'[zones.{name}]\nlocal = ["private/nested"]\nshared = ["shared/nested"]\n'
            for name in zones.ZONE_CONTAINER_TARGETS
        )
    )
    roots = docker.resolve_zone_roots(config)
    assert docker._zone_overlay_sources(config) == [
        (root / name / relative, target / relative)
        for name, target in zones.ZONE_CONTAINER_TARGETS.items()
        for root, relative in (
            (roots.local_root, Path("private/nested")),
            (roots.shared_root, Path("shared/nested")),
        )
    ]


def test_family_construction_failure_keeps_other_problems(ground, monkeypatch):
    config, inputs, _ = ground
    protect(inputs, config.code_dir.parent)
    monkeypatch.setattr(docker, "get_config_root", Mock(side_effect=RuntimeError("root failed")))
    problems = docker.managed_bind_problems(config)
    assert len(problems) == 4
    assert problems[0] == expected("workspace", config.code_dir, config.code_dir.parent)
    for family, problem in zip(("config root", "zones"), problems[1:3], strict=True):
        assert problem == expected_construction(family, "root failed")
    assert problems[-1] == expected(
        "sessions", Path.home() / ".djinn/sessions", config.code_dir.parent
    )


def test_construction_and_source_failures_use_distinct_remedies(ground, monkeypatch):
    config, _, _ = ground
    monkeypatch.setattr(docker, "get_config_root", Mock(side_effect=RuntimeError("root failed")))
    original = host_sealing.canonical

    def canonical(path, **kwargs):
        if path == config.code_dir:
            raise PermissionError("source unreadable")
        return original(path, **kwargs)

    monkeypatch.setattr(host_sealing, "canonical", canonical)
    assert docker.managed_bind_problems(config) == [
        f"Workspace root {config.code_dir} is mounted writable and cannot be checked against "
        f"host execution inputs (source unreadable); {REMEDIES['workspace']}",
        expected_construction("config root", "root failed"),
        expected_construction("zones", "root failed"),
    ]


@pytest.mark.parametrize("kind", ["missing", "directory", "file", "symlink"])
def test_overlay_builder_preserves_targets_and_source_refusal(ground, monkeypatch, tmp_path, kind):
    config, _, _ = ground
    source = tmp_path / "source"
    target = Path("/home/dev/.claude/custom")
    if kind == "directory":
        source.mkdir()
    elif kind == "file":
        source.touch()
    elif kind == "symlink":
        source.symlink_to(config.code_dir, target_is_directory=True)
    monkeypatch.setattr(docker, "_zone_overlay_sources", lambda c: [(source, target)])
    if kind in {"file", "symlink"}:
        with pytest.raises(
            ConfigValidationError, match=f"Zone overlay source is not a directory: {source}"
        ):
            docker._zone_overlay_mount_args_and_targets(config)
    else:
        args, targets = docker._zone_overlay_mount_args_and_targets(config)
        assert targets == (target,)
        assert args == (["-v", f"{source}:{target}"] if kind == "directory" else [])


@pytest.mark.parametrize("error_type", [OSError, ValueError, KeyError, TypeError, RuntimeError])
def test_doctor_root_construction_errors_are_contained(
    ground, quiet_doctor, monkeypatch, error_type
):
    config, _, _ = ground
    monkeypatch.setattr(doctor, "get_config_root", Mock(side_effect=error_type("root unavailable")))
    rows = doctor.run_checks(config)
    root = next(row for row in rows if row.name == "Config root")
    assert root.status is doctor.Status.FAIL
    assert "root unavailable" in root.detail
    assert (
        next(row for row in rows if row.name == "Managed bind sources").status is doctor.Status.PASS
    )
