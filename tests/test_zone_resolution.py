from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import djinn_in_a_box.config.zones as zones_mod
import djinn_in_a_box.core.docker as docker_mod
from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.config.defaults import DEFAULT_ZONES, SYNC_PATHS
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.config.zones import (
    ZONE_CONTAINER_TARGETS,
    ZoneConfigurationError,
    load_zone_assignments,
)
from djinn_in_a_box.core.docker import (
    build_compose_env,
    ensure_host_env,
    ensure_zone_roots,
    get_config_root,
    resolve_zone_roots,
)
from djinn_in_a_box.core.exceptions import ZoneRootValidationError

runner = CliRunner()


def _config(
    tmp_path: Path,
    *,
    shared_root: Path | None = None,
    local_root: Path | None = None,
) -> AppConfig:
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    return AppConfig(
        code_dir=projects,
        config_root=tmp_path / "config",
        shared_root=shared_root,
        local_root=local_root,
    )


def _paths(values: list[str]) -> tuple[Path, ...]:
    return tuple(Path(value) for value in values)


def test_default_zones_match_the_frozen_assignment_table() -> None:
    assert DEFAULT_ZONES == {
        "claude": {
            "local": [
                "jobs",
                "cache",
                "file-history",
                "ide",
                "paste-cache",
                "session-env",
                "shell-snapshots",
                "tasks",
                "telemetry",
                "work",
                "sessions",
                "daemon",
                "plugins/marketplaces",
                "plugins/cache",
                "state",
                "feedback",
            ],
            "shared": ["projects", "transcripts"],
        },
        "codex": {
            "local": [
                ".tmp",
                "tmp",
                "cache",
                "log",
                "mcp-oauth-locks",
                "shell_snapshots",
                "plugins/cache",
                "packages",
                "app-server-daemon",
                "app-server-control",
                "thread-writer-locks",
                "tui-thread-reference-capabilities",
            ],
            "shared": ["sessions"],
        },
        "opencode": {"local": ["node_modules", "native"], "shared": []},
        "gh": {"local": [], "shared": []},
        "age": {"local": [], "shared": []},
    }


@pytest.mark.parametrize("use_environment", [False, True])
def test_zone_roots_follow_the_effective_config_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_environment: bool
) -> None:
    config = _config(tmp_path)
    if use_environment:
        effective_root = tmp_path / "from-environment"
        monkeypatch.setenv("DJINN_CONFIG_ROOT", str(effective_root))
    else:
        effective_root = config.config_root
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)

    roots = resolve_zone_roots(config)
    compose_env = build_compose_env(config)

    assert get_config_root(config) == effective_root
    assert roots.config_root == effective_root
    assert roots.shared_root == Path(f"{effective_root}.shared")
    assert roots.local_root == Path(f"{effective_root}.local")
    assert Path(compose_env["DJINN_CONFIG_ROOT"]) == roots.config_root


def test_explicit_zone_roots_survive_an_unrelated_config_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(
        tmp_path,
        shared_root=tmp_path / "shared-zone",
        local_root=tmp_path / "local-zone",
    )

    config_file = tmp_path / "config.toml"
    save_config(config, config_file)
    monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

    result = runner.invoke(app, ["config", "set", "general.timezone", "Europe/Berlin"])

    assert result.exit_code == 0
    persisted = load_config(config_file)

    assert persisted.shared_root == (tmp_path / "shared-zone").resolve()
    assert persisted.local_root == (tmp_path / "local-zone").resolve()


def test_config_set_and_show_carry_explicit_zone_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    save_config(_config(tmp_path), config_file)
    monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)
    shared_root = tmp_path / "shared-zone"
    local_root = tmp_path / "local-zone"

    shared_result = runner.invoke(app, ["config", "set", "general.shared_root", str(shared_root)])
    local_result = runner.invoke(app, ["config", "set", "general.local_root", str(local_root)])
    show_result = runner.invoke(app, ["config", "show"])

    assert shared_result.exit_code == 0
    assert local_result.exit_code == 0
    assert show_result.exit_code == 0
    persisted = load_config(config_file)
    assert persisted.shared_root == shared_root.resolve()
    assert persisted.local_root == local_root.resolve()
    unwrapped = "".join(show_result.output.split())
    assert str(shared_root) in unwrapped
    assert str(local_root) in unwrapped


@pytest.mark.parametrize(
    "roots",
    [
        {"shared_root": Path("config")},
        {"shared_root": Path("config/child")},
        {"shared_root": Path("shared"), "local_root": Path("shared/child")},
    ],
)
def test_zone_root_resolution_rejects_equal_and_nested_roots(
    tmp_path: Path, roots: dict[str, Path]
) -> None:
    config_root = tmp_path / "config"
    projects = tmp_path / "projects"
    projects.mkdir()
    config = AppConfig(
        code_dir=projects,
        config_root=config_root,
        shared_root=tmp_path / roots["shared_root"] if "shared_root" in roots else None,
        local_root=tmp_path / roots["local_root"] if "local_root" in roots else None,
    )

    assert config.config_root == config_root
    with pytest.raises(ZoneRootValidationError):
        resolve_zone_roots(config)


def _provisioning_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(docker_mod, "get_project_root", lambda: tmp_path / "project")
    zones_file = tmp_path / "zones.toml"
    zones_file.write_text(
        '[zones.claude]\nlocal = ["custom/scratch/nested"]\n'
        'shared = ["custom/transcripts/nested"]\n'
    )
    monkeypatch.setattr(zones_mod, "ZONES_FILE", zones_file)
    return _config(tmp_path)


def _compose_nested_bind_targets(workspace: str = "projects") -> dict[tuple[str, Path], str]:
    project = Path(__file__).parents[1]
    compose = yaml.safe_load((project / "docker-compose.yml").read_text())
    mounts: list[tuple[str, Path]] = []
    for volume in compose["services"]["dev"]["volumes"]:
        volume = volume.replace(
            "${DJINN_WORKSPACE_TARGET:-/home/dev/projects}", f"/home/dev/{workspace}"
        )
        match = re.fullmatch(r"(.+):(/[^:]+)(?::(?:ro|rw))?", volume)
        assert match is not None, f"Unexpected Compose mount: {volume}"
        assert "$" not in match.group(2), f"Unresolved Compose target: {volume}"
        mounts.append((match.group(1), Path(match.group(2))))
    agent_roots = {
        source.rsplit("/", 1)[1]: target
        for source, target in mounts
        if source.startswith("${DJINN_CONFIG_ROOT")
    }
    assert agent_roots == ZONE_CONTAINER_TARGETS
    nested: dict[tuple[str, Path], str] = {}
    for agent, agent_root in agent_roots.items():
        for source, target in mounts:
            if target == agent_root or not target.is_relative_to(agent_root):
                continue
            assert source.startswith("./"), f"Expected a nested bind mount: {source}"
            source_path = project / source
            if source.startswith("./config/"):
                source_path = project / "templates" / "seed" / source
            assert source_path.is_file() or source_path.is_dir(), source_path
            nested[agent, target.relative_to(agent_root)] = (
                "file" if source_path.is_file() else "directory"
            )
    return nested


@pytest.mark.parametrize("workspace", ["projects", "aios"])
def test_nested_bind_target_definitions_match_compose_sources_and_kinds(workspace: str) -> None:
    defined = {
        (agent, target.relative_to(agent_root)): docker_mod._COMPOSE_DEV_MOUNT_TARGETS[target]
        for agent, agent_root in ZONE_CONTAINER_TARGETS.items()
        for target in docker_mod.repo_owned_submount_targets(agent_root)
    }

    assert defined == _compose_nested_bind_targets(workspace)


def _nested_bind_targets(config: AppConfig) -> dict[Path, str]:
    root = resolve_zone_roots(config).config_root
    targets = {
        root / agent / relative_path: kind
        for (agent, relative_path), kind in _compose_nested_bind_targets().items()
    }
    for agent, by_zone in load_zone_assignments(config).by_agent.items():
        for relative_paths in by_zone.values():
            targets.update(
                dict.fromkeys((root / agent / path for path in relative_paths), "directory")
            )
    return targets


def _overlay_directories(config: AppConfig) -> set[Path]:
    roots = resolve_zone_roots(config)
    directories: set[Path] = set()
    for agent, by_zone in load_zone_assignments(config).by_agent.items():
        for zone, root in (("local", roots.local_root), ("shared", roots.shared_root)):
            for relative_path in by_zone[zone]:
                directory = root
                for component in (agent, *relative_path.parts):
                    directory /= component
                    directories.add(directory)
    return directories


def _host_tree(root: Path) -> dict[Path, tuple[int, int, int, int, bytes | None]]:
    return {
        path.relative_to(root): (
            path.stat().st_ino,
            path.stat().st_uid,
            path.stat().st_gid,
            path.stat().st_mode,
            path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


def test_host_provisioning_creates_every_overlay_and_nested_bind_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _provisioning_config(tmp_path, monkeypatch)
    expected = _overlay_directories(config)
    expected_targets = _nested_bind_targets(config)

    ensure_host_env(config)

    roots = resolve_zone_roots(config)
    actual = set(roots.local_root.rglob("*")) | set(roots.shared_root.rglob("*"))
    assert actual == expected
    for directory in expected:
        metadata = directory.lstat()
        assert stat.S_ISDIR(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o700
        assert (metadata.st_uid, metadata.st_gid) == (os.getuid(), os.getgid())
    for target, kind in expected_targets.items():
        metadata = target.lstat()
        assert (metadata.st_uid, metadata.st_gid) == (os.getuid(), os.getgid())
        if kind == "file":
            assert stat.S_ISREG(metadata.st_mode)
            assert stat.S_IMODE(metadata.st_mode) == 0o600
            assert target.read_bytes() == b""
        else:
            assert stat.S_ISDIR(metadata.st_mode)
            assert stat.S_IMODE(metadata.st_mode) == 0o700
        for parent in target.parents:
            if parent == roots.config_root:
                break
            metadata = parent.lstat()
            assert stat.S_ISDIR(metadata.st_mode)
            assert stat.S_IMODE(metadata.st_mode) == 0o700
            assert (metadata.st_uid, metadata.st_gid) == (os.getuid(), os.getgid())
    for agent, by_zone in load_zone_assignments(config).by_agent.items():
        for zone, root in (("local", roots.local_root), ("shared", roots.shared_root)):
            for relative_path in by_zone[zone]:
                assert list((root / agent / relative_path).iterdir()) == []


def test_host_provisioning_preserves_existing_zone_and_config_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _provisioning_config(tmp_path, monkeypatch)
    roots = ensure_zone_roots(config)
    for directory in sorted(_overlay_directories(config)):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for agent in SYNC_PATHS["credentials"]:
        (roots.config_root / agent).mkdir(mode=0o700)
    for target, kind in _nested_bind_targets(config).items():
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if kind == "file":
            target.write_bytes(b"existing instruction content\n")
            target.chmod(0o640)
        else:
            target.mkdir(exist_ok=True)
            target.chmod(0o755)
            (target / "sentinel.bin").write_bytes(b"existing target data")
    home = Path.home()
    for directory in (home / ".djinn" / "sessions", home / ".djinn" / "backups", home / ".ssh"):
        directory.mkdir(parents=True, mode=0o700)
    (home / ".gitconfig").write_bytes(b"[user]\nname = Operator\n")
    base = roots.config_root / "claude" / "jobs"
    preserved_target = roots.config_root / "claude" / "custom" / "scratch" / "nested"
    preserved_target.chmod(0o755)
    (preserved_target / "sentinel.bin").write_bytes(b"existing target data")
    for directory, content in (
        (base, b"config data\x00"),
        (roots.local_root / "claude" / "custom" / "scratch" / "nested", b"local data\xff"),
        (roots.shared_root / "claude" / "custom" / "transcripts" / "nested", b"shared data\n"),
        (roots.config_root, b"unrelated data"),
    ):
        (directory / "sentinel.bin").write_bytes(content)
    before = _host_tree(tmp_path)

    ensure_host_env(config)

    assert _host_tree(tmp_path) == before
    assert stat.S_IMODE(preserved_target.lstat().st_mode) == 0o755
    assert (preserved_target / "sentinel.bin").read_bytes() == b"existing target data"


@pytest.mark.parametrize("invalid_entry", ("wrong-kind", "symlink", "dangling-symlink"))
@pytest.mark.parametrize(
    ("agent", "relative_path", "kind"),
    [(agent, path, kind) for (agent, path), kind in _compose_nested_bind_targets().items()],
)
def test_host_provisioning_refuses_invalid_nested_bind_targets_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    relative_path: Path,
    kind: str,
    invalid_entry: str,
) -> None:
    config = _provisioning_config(tmp_path, monkeypatch)
    target = resolve_zone_roots(config).config_root / agent / relative_path
    target.parent.mkdir(parents=True)
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    outside = outside_root / "target"
    if invalid_entry == "wrong-kind":
        if kind == "file":
            target.mkdir(mode=0o755)
            (target / "sentinel.bin").write_bytes(b"untouched")
        else:
            target.write_bytes(b"untouched")
            target.chmod(0o640)
    else:
        if invalid_entry == "symlink":
            if kind == "file":
                outside.write_bytes(b"untouched")
                outside.chmod(0o640)
            else:
                outside.mkdir(mode=0o755)
                (outside / "sentinel.bin").write_bytes(b"untouched")
        target.symlink_to(outside, target_is_directory=kind == "directory")
    before = target.lstat()
    outside_before = _host_tree(outside_root)
    expected_error = kind if invalid_entry == "wrong-kind" else "symlink"

    with pytest.raises(ZoneRootValidationError, match=expected_error):
        ensure_host_env(config)

    after = target.lstat()
    assert (after.st_ino, after.st_uid, after.st_gid, after.st_mode) == (
        before.st_ino, before.st_uid, before.st_gid, before.st_mode,
    )
    assert _host_tree(outside_root) == outside_before
    if invalid_entry == "wrong-kind":
        content = target / "sentinel.bin" if kind == "file" else target
        assert content.read_bytes() == b"untouched"
    else:
        assert target.is_symlink()
        assert target.readlink() == outside


def test_host_provisioning_is_idempotent_for_all_overlay_assignments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _provisioning_config(tmp_path, monkeypatch)
    ensure_host_env(config)
    roots = resolve_zone_roots(config)
    (roots.local_root / "claude" / "jobs" / "state.bin").write_bytes(b"persistent state")
    before = _host_tree(tmp_path)

    ensure_host_env(config)

    assert _host_tree(tmp_path) == before


def test_host_provisioning_creates_zone_roots_and_agent_roots_with_0700_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(docker_mod, "get_project_root", lambda: tmp_path / "project")
    roots = resolve_zone_roots(config)
    for root in (roots.config_root, roots.shared_root, roots.local_root):
        root.mkdir(parents=True)
        root.chmod(0o755)
    agent_root = roots.config_root / "claude"
    agent_root.mkdir()
    agent_root.chmod(0o755)

    ensure_host_env(config)

    for root in (roots.config_root, roots.shared_root, roots.local_root):
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for agent in SYNC_PATHS["credentials"]:
        assert stat.S_IMODE((roots.config_root / agent).stat().st_mode) == 0o700


def test_ensure_zone_roots_rejects_a_derived_symlink_without_chmodding_its_target(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    shared_target = tmp_path / "large-disk"
    shared_target.mkdir()
    shared_target.chmod(0o755)
    Path(f"{config.config_root}.shared").symlink_to(shared_target, target_is_directory=True)

    with pytest.raises(ZoneRootValidationError, match="symlink"):
        ensure_zone_roots(config)

    assert stat.S_IMODE(shared_target.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    ("zone", "component", "target_side"),
    [
        ("local", "", False),
        ("local", "claude", False),
        ("local", "claude/custom", False),
        ("local", "claude/custom/nested", False),
        ("shared", "", False),
        ("shared", "claude", False),
        ("shared", "claude/custom", False),
        ("shared", "claude/custom/nested", False),
        ("local", "claude/custom", True),
        ("local", "claude/custom/nested", True),
    ],
)
def test_ensure_zone_roots_translates_a_regular_file_to_a_named_zone_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zone: str,
    component: str,
    target_side: bool,
) -> None:
    config = _provisioning_config(tmp_path, monkeypatch)
    zones_mod.ZONES_FILE.write_text(f'[zones.claude]\n{zone} = ["custom/nested"]\n')
    assignments = load_zone_assignments(config) if target_side else None
    roots = resolve_zone_roots(config)
    root = (
        roots.config_root
        if target_side
        else roots.local_root if zone == "local" else roots.shared_root
    )
    file_path = root / component if component else config.config_root
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"not a directory")
    before = file_path.stat()

    if target_side:
        assert assignments is not None
        monkeypatch.setattr(zones_mod, "load_zone_assignments", lambda _config: assignments)
        with pytest.raises(ZoneRootValidationError, match="directory"):
            ensure_host_env(config)
    else:
        with pytest.raises(
            (ZoneRootValidationError, ZoneConfigurationError), match="directory|file"
        ):
            ensure_host_env(config)

    assert file_path.read_bytes() == b"not a directory"
    after = file_path.stat()
    assert (after.st_ino, after.st_uid, after.st_gid, after.st_mode) == (
        before.st_ino, before.st_uid, before.st_gid, before.st_mode,
    )
    if component and not target_side:
        assert set(root.rglob("*")) == {
            file_path,
            *file_path.parents[: len(Path(component).parts) - 1],
        }


def test_zone_assignments_merge_defaults_for_every_container_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zones_file = tmp_path / "zones.toml"
    monkeypatch.setattr(zones_mod, "ZONES_FILE", zones_file)

    assignments = load_zone_assignments(_config(tmp_path))

    assert tuple(assignments.by_agent) == tuple(ZONE_CONTAINER_TARGETS)
    assert assignments.skipped_defaults == ()
    for agent in ZONE_CONTAINER_TARGETS:
        assert assignments.by_agent[agent]["local"] == _paths(DEFAULT_ZONES[agent]["local"])
        assert assignments.by_agent[agent]["shared"] == _paths(DEFAULT_ZONES[agent]["shared"])


def test_zone_assignments_add_user_paths_without_replacing_defaults(tmp_path: Path) -> None:
    zones_file = tmp_path / "zones.toml"
    zones_file.write_text('[zones.claude]\nlocal = ["custom-cache"]\n')

    assignments = load_zone_assignments(_config(tmp_path), path=zones_file)

    assert assignments.by_agent["claude"]["local"] == (
        *_paths(DEFAULT_ZONES["claude"]["local"]),
        Path("custom-cache"),
    )
    assert assignments.by_agent["claude"]["shared"] == _paths(DEFAULT_ZONES["claude"]["shared"])


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ('[zones.claude]\nlocal = ["projects"]\n', "both"),
        ('[zones.claude]\nlocal = ["plugins"]\n', "overlap"),
    ],
)
def test_zone_assignments_reject_cross_zone_and_nested_conflicts(
    tmp_path: Path, contents: str, expected: str
) -> None:
    zones_file = tmp_path / "zones.toml"
    zones_file.write_text(contents)

    with pytest.raises(ZoneConfigurationError, match=expected):
        load_zone_assignments(_config(tmp_path), path=zones_file)


@pytest.mark.parametrize(
    "path",
    ["/absolute", ".", "..", "cache/../outside"],
)
def test_zone_assignments_reject_absolute_and_traversal_paths(tmp_path: Path, path: str) -> None:
    zones_file = tmp_path / "zones.toml"
    zones_file.write_text(f'[zones.gh]\nlocal = ["{path}"]\n')

    with pytest.raises(ZoneConfigurationError):
        load_zone_assignments(_config(tmp_path), path=zones_file)


def test_zone_assignments_reject_symlinked_components(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    agent_root = config.config_root / "claude"
    agent_root.mkdir(parents=True)
    (agent_root / "link").symlink_to(target)
    zones_file = tmp_path / "zones.toml"
    zones_file.write_text('[zones.claude]\nlocal = ["link/cache"]\n')

    with pytest.raises(ZoneConfigurationError, match="symlinked"):
        load_zone_assignments(config, path=zones_file)


@pytest.mark.parametrize("zone", ("local", "shared"))
@pytest.mark.parametrize("dangling", (False, True))
@pytest.mark.parametrize(
    ("component", "target_side"),
    [
        ("claude", False),
        ("claude/custom", False),
        ("claude/custom/nested", False),
        ("claude/custom", True),
        ("claude/custom/nested", True),
    ],
)
def test_zone_assignments_reject_symlinked_destination_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zone: str,
    component: str,
    dangling: bool,
    target_side: bool,
) -> None:
    config = _provisioning_config(tmp_path, monkeypatch)
    zones_mod.ZONES_FILE.write_text(f'[zones.claude]\n{zone} = ["custom/nested"]\n')
    assignments = load_zone_assignments(config) if target_side else None
    outside = tmp_path / "outside"
    if not dangling:
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"untouched")
    roots = resolve_zone_roots(config)
    root = (
        roots.config_root
        if target_side
        else roots.local_root if zone == "local" else roots.shared_root
    )
    link = root / component
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    target_before = _host_tree(outside) if not dangling else None

    if target_side:
        assert assignments is not None
        monkeypatch.setattr(zones_mod, "load_zone_assignments", lambda _config: assignments)
        with pytest.raises(ZoneRootValidationError, match="symlink"):
            ensure_host_env(config)
    else:
        with pytest.raises(ZoneConfigurationError, match="symlinked"):
            ensure_host_env(config)

    assert link.is_symlink()
    assert link.readlink() == outside
    assert (_host_tree(outside) if outside.exists() else None) == target_before
    if not target_side:
        assert set(root.rglob("*")) == {link, *link.parents[: len(Path(component).parts) - 1]}


def test_zone_assignments_reject_a_user_path_that_is_a_regular_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    file_path = config.config_root / "gh" / "hosts.yml"
    file_path.parent.mkdir(parents=True)
    file_path.touch()
    zones_file = tmp_path / "zones.toml"
    zones_file.write_text('[zones.gh]\nlocal = ["hosts.yml"]\n')

    with pytest.raises(ZoneConfigurationError, match="regular file"):
        load_zone_assignments(config, path=zones_file)


def test_regular_file_at_a_default_assignment_is_skipped(tmp_path: Path) -> None:
    config = _config(tmp_path)
    jobs = config.config_root / "claude" / "jobs"
    jobs.parent.mkdir(parents=True)
    jobs.touch()
    zones_file = tmp_path / "zones.toml"
    zones_file.touch()

    assignments = load_zone_assignments(config, path=zones_file)

    assert Path("jobs") not in assignments.by_agent["claude"]["local"]
    assert assignments.skipped_defaults[0].relative_path == Path("jobs")


def test_symlinked_component_at_a_default_assignment_is_skipped(tmp_path: Path) -> None:
    config = _config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    plugins = config.config_root / "claude" / "plugins"
    plugins.parent.mkdir(parents=True)
    plugins.symlink_to(outside, target_is_directory=True)
    zones_file = tmp_path / "zones.toml"
    zones_file.touch()

    assignments = load_zone_assignments(config, path=zones_file)

    assert Path("plugins/marketplaces") not in assignments.by_agent["claude"]["local"]
    assert any(
        skipped.relative_path == Path("plugins/marketplaces")
        for skipped in assignments.skipped_defaults
    )


@pytest.mark.parametrize("key", ("general.shared_root", "general.local_root"))
def test_config_set_rejects_nested_zone_roots_before_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    config_file = tmp_path / "config.toml"
    config = _config(tmp_path)
    save_config(config, config_file)
    monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

    result = runner.invoke(app, ["config", "set", key, str(config.config_root / "nested")])

    assert result.exit_code == 1
    assert load_config(config_file).shared_root is None
    assert load_config(config_file).local_root is None


@pytest.mark.parametrize("key", ("general.shared_root", "general.local_root"))
def test_config_set_warns_that_moved_zone_data_is_not_relocated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    config_file = tmp_path / "config.toml"
    config = _config(tmp_path)
    save_config(config, config_file)
    monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

    result = runner.invoke(app, ["config", "set", key, str(tmp_path / "moved-zone")])

    assert result.exit_code == 0
    assert "Zone data does not follow" in result.output


@pytest.mark.parametrize(
    ("agent", "path"),
    [
        ("repo-dotfiles", "cache"),
        ("claude", "skills/cache"),
        ("opencode", "seed"),
    ],
)
def test_zone_assignments_reject_entries_without_a_safe_container_target(
    tmp_path: Path, agent: str, path: str
) -> None:
    zones_file = tmp_path / "zones.toml"
    zones_file.write_text(f'[zones.{agent}]\nlocal = ["{path}"]\n')

    with pytest.raises(ZoneConfigurationError):
        load_zone_assignments(_config(tmp_path), path=zones_file)


@pytest.mark.parametrize(
    "contents",
    [
        "[zones.claude\nlocal = [\"cache\"]\n",
        '[zones.claude]\nunknown = ["cache"]\n',
    ],
)
def test_malformed_or_schema_invalid_zones_file_raises_a_named_error(
    tmp_path: Path, contents: str
) -> None:
    zones_file = tmp_path / "zones.toml"
    zones_file.write_text(contents)

    with pytest.raises(ZoneConfigurationError):
        load_zone_assignments(_config(tmp_path), path=zones_file)
