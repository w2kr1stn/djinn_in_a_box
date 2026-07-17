from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
import threading
import tomllib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path, PurePosixPath

import pytest

import djinn_in_a_box.core.config_sync as sync_module
from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig, ConfigSyncSource
from djinn_in_a_box.core.config_delivery import DeliveryResult, DeliveryView
from djinn_in_a_box.core.config_lock import config_directory_lock
from djinn_in_a_box.core.config_sync import (
    MANIFEST_NAME,
    DriftClass,
    audit_config_sync,
    load_canonical_delivery_view,
    sync_config,
)
from djinn_in_a_box.core.config_sync_adapters import (
    ADAPTER_REVISION,
    ArtifactOutputContract,
    RenderedFile,
    UnresolvedItem,
    allowed_outputs_for_unresolved,
)
from djinn_in_a_box.core.config_sync_agent import (
    SemanticAgentResult,
    SemanticFailure,
    semantic_artifact_fingerprint,
)

_REAL_CODEX_SEMANTIC_DELIVERY = sync_module._deliver_codex_semantic_source  # pyright: ignore[reportPrivateUsage]


def _returns[T](value: T) -> Callable[..., T]:
    def _stub(*_args: object, **_kwargs: object) -> T:
        return value

    return _stub


@pytest.fixture(autouse=True)
def _isolate_semantic_runtime_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_module, "ensure_host_env", _returns(None))
    monkeypatch.setattr(sync_module, "seed_config", _returns(list[Path]()))
    monkeypatch.setattr(sync_module, "_deliver_codex_semantic_source", _returns(None))


def _hold_exclusive_lock(config_dir: Path, ready: ProcessEvent, release: ProcessEvent) -> None:
    with config_directory_lock(config_dir, exclusive=True):
        ready.set()
        release.wait()


def _tree_snapshot(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path.relative_to(root): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in root.rglob("*")
        if path.is_file()
    }


def _workspace(
    tmp_path: Path,
    *,
    source: ConfigSyncSource = "claude",
) -> tuple[Path, Path]:
    project = tmp_path / "project"
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
    native = {"claude": "CLAUDE.md", "codex": "AGENTS.md", "opencode": "AGENTS.md"}
    (project / "config" / source / native[source]).write_text("Shared instructions.\n")
    code_dir = tmp_path / "code"
    code_dir.mkdir()
    config_path = tmp_path / "operator.toml"
    save_config(
        AppConfig(
            code_dir=code_dir,
            config_root=tmp_path / "runtime",
            config_sync=ConfigSyncConfig(source=source),
        ),
        config_path,
    )
    return project, config_path


def _select_source(config_path: Path, code_dir: Path, source: ConfigSyncSource) -> None:
    save_config(
        AppConfig(code_dir=code_dir, config_sync=ConfigSyncConfig(source=source)),
        config_path,
    )


class _FakeSemanticResolver:
    def __init__(
        self,
        *,
        failure: SemanticFailure | None = None,
        before_return: Callable[[], None] | None = None,
    ) -> None:
        self.failure = failure
        self.before_return = before_return
        self.calls: list[tuple[ConfigSyncSource, UnresolvedItem, float]] = []

    def __call__(
        self,
        source: ConfigSyncSource,
        item: UnresolvedItem,
        *,
        deadline: float,
    ) -> SemanticAgentResult:
        self.calls.append((source, item, deadline))
        if self.before_return is not None:
            self.before_return()
        fingerprint = semantic_artifact_fingerprint(source, item)
        if self.failure is not None:
            return SemanticAgentResult(fingerprint, failure=self.failure)
        contract = allowed_outputs_for_unresolved(item)
        files = tuple(
            RenderedFile(
                path,
                _semantic_output(item, path),
                item.identifier,
                item.executable,
            )
            for path in contract.file_paths
        )
        return SemanticAgentResult(fingerprint, files=files)


def _semantic_output(item: UnresolvedItem, path: PurePosixPath) -> bytes:
    if item.identifier.startswith("skill:"):
        name = path.parts[1]
        return (
            f"---\nname: {name}\ndescription: Adapted semantic skill\n---\n\n"
            "SEMANTIC-OUTPUT-SENTINEL\n"
        ).encode()
    if path.suffix == ".py":
        return b"print('SEMANTIC-OUTPUT-SENTINEL')\n"
    if path.suffix == ".js":
        hook_key = b'"tool.execute.before"' if path.name == "security-reminder.js" else b"event"
        return (
            b"export const Adapted = async () => "
            b"({ " + hook_key + b": async () => {} }); // SEMANTIC-OUTPUT-SENTINEL\n"
        )
    return b"SEMANTIC-OUTPUT-SENTINEL\n"


def _add_semantic_skill(project: Path, name: str = "convergence-loop") -> Path:
    skill = project / "config/claude/skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        f"---\nname: {name}\ndescription: Semantic source skill\n---\n\n"
        f"SOURCE-BODY-SENTINEL for {name}.\n"
    )
    return skill


def test_first_sync_is_atomic_clean_and_idempotent(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    before = sorted(path.relative_to(project) for path in project.rglob("*"))

    audit = audit_config_sync(project, config_path=config_path)

    assert DriftClass.SOURCE_ONLY in audit.drift_classes
    assert not (project / "config" / MANIFEST_NAME).exists()
    assert sorted(path.relative_to(project) for path in project.rglob("*")) == before

    first = sync_config(project, config_path=config_path)
    second = sync_config(project, config_path=config_path)

    assert first.success is True
    assert (project / "config/codex/AGENTS.md").read_text() == "Shared instructions.\n"
    assert (project / "config/codex/CLAUDE.md").read_text() == "Shared instructions.\n"
    assert (project / "config/opencode/AGENTS.md").read_text() == "Shared instructions.\n"
    assert (project / "config/claude/AGENTS.md").read_text() == "Shared instructions.\n"
    codex_settings = tomllib.loads((project / "config/codex/config.toml").read_text())
    assert codex_settings["project_doc_fallback_filenames"] == ["CLAUDE.md"]
    assert second.success is True
    assert second.changed_paths == ()
    assert audit_config_sync(project, config_path=config_path).clean is True
    assert not list((project / "config").glob(".djinn-config-sync-stage-*"))


def test_canonical_source_view_is_exact_and_preserves_recorded_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    script = project / "config/claude/ready_notify_hook.py"
    script.write_text("print('ready')\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    registration = [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "uv run python3 ~/.claude_seed/ready_notify_hook.py",
                }
            ],
        }
    ]
    (project / "config/claude/settings.json").write_text(
        json.dumps({"hooks": {"Stop": registration}, "private_neighbor": "do-not-copy"})
    )
    (project / "config/claude/private-root-file.txt").write_text("unowned")
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    assert sync_config(project, config_path=config_path, allow_agent=True).success

    loaded = load_canonical_delivery_view(project, "claude", config_path=config_path)

    assert loaded.success is True
    assert loaded.view is not None
    files = {item.relative_path: item for item in loaded.view.files}
    assert set(files) == {
        PurePosixPath("AGENTS.md"),
        PurePosixPath("CLAUDE.md"),
        PurePosixPath("ready_notify_hook.py"),
    }
    assert files[PurePosixPath("ready_notify_hook.py")].executable is True
    assert PurePosixPath("settings.json") not in files
    assert PurePosixPath("private-root-file.txt") not in files
    assert len(loaded.view.settings_fragments) == 1
    fragment = loaded.view.settings_fragments[0]
    assert fragment.carrier_path == PurePosixPath("settings.json")
    assert fragment.key_path == ("hooks", "Stop")
    assert json.loads(fragment.value_json) == registration
    assert "private_neighbor" not in fragment.value_json.decode()


def test_canonical_target_view_contains_cached_semantic_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    assert sync_config(project, config_path=config_path, allow_agent=True).success

    loaded = load_canonical_delivery_view(project, "opencode", config_path=config_path)

    assert loaded.success is True
    assert loaded.view is not None
    semantic = next(
        item
        for item in loaded.view.files
        if item.relative_path == PurePosixPath("skills/convergence-loop/SKILL.md")
    )
    assert b"SEMANTIC-OUTPUT-SENTINEL" in semantic.content


def test_codex_semantic_runtime_is_seeded_and_delivered_before_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path, source="codex")
    skill = project / "config/codex/skills/convergence-loop/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: convergence-loop\ndescription: Converge reviews\n---\n\n"
        "Semantic Codex variant.\n"
    )
    (project / "config/codex/config.toml").write_text(
        'theme = "private-neighbor"\nproject_doc_fallback_filenames = ["CLAUDE.md"]\n'
    )
    events: list[str] = []
    delivered: list[tuple[DeliveryView, Path]] = []
    resolver = _FakeSemanticResolver()

    def _record_host_env(*_args: object, **_kwargs: object) -> None:
        events.append("host-env")

    def _record_seed(_root: Path, *, source: ConfigSyncSource) -> None:
        events.append(f"seed:{source}")

    monkeypatch.setattr(sync_module, "ensure_host_env", _record_host_env)
    monkeypatch.setattr(sync_module, "seed_config", _record_seed)
    monkeypatch.setattr(
        sync_module, "_deliver_codex_semantic_source", _REAL_CODEX_SEMANTIC_DELIVERY
    )

    def capture_delivery(view: DeliveryView, destination: Path) -> DeliveryResult:
        events.append("delivery")
        delivered.append((view, destination))
        return DeliveryResult(True)

    def resolve(
        source: ConfigSyncSource, item: UnresolvedItem, *, deadline: float
    ) -> SemanticAgentResult:
        events.append("resolver")
        return resolver(source, item, deadline=deadline)

    monkeypatch.setattr(sync_module, "deliver_config_view", capture_delivery)
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolve)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is True
    assert events[:4] == ["host-env", "seed:codex", "delivery", "resolver"]
    assert delivered[0][1] == tmp_path / "runtime/codex"
    view = delivered[0][0]
    file_paths = {item.relative_path for item in view.files}
    assert PurePosixPath("AGENTS.md") in file_paths
    assert PurePosixPath("CLAUDE.md") in file_paths
    assert PurePosixPath("skills/convergence-loop/SKILL.md") in file_paths
    assert PurePosixPath("config.toml") not in file_paths
    assert [fragment.key_path for fragment in view.settings_fragments] == [
        ("project_doc_fallback_filenames",)
    ]
    assert "private-neighbor" not in view.settings_fragments[0].value_json.decode()


def test_allow_agent_deterministic_sync_has_no_semantic_host_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    events: list[str] = []

    def record_host(_config: AppConfig) -> None:
        events.append("host-env")

    def record_seed(_root: Path, *, source: ConfigSyncSource) -> None:
        events.append(f"seed:{source}")

    def record_delivery(
        _config: AppConfig,
        _root: Path,
        _desired: sync_module._DesiredState,  # pyright: ignore[reportPrivateUsage]
    ) -> None:
        events.append("delivery")

    def record_resolver(
        _source: ConfigSyncSource,
        _item: UnresolvedItem,
        *,
        deadline: float,
    ) -> SemanticAgentResult:
        del deadline
        events.append("resolver")
        raise AssertionError("deterministic sync invoked semantic resolver")

    monkeypatch.setattr(sync_module, "ensure_host_env", record_host)
    monkeypatch.setattr(sync_module, "seed_config", record_seed)
    monkeypatch.setattr(sync_module, "_deliver_codex_semantic_source", record_delivery)
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", record_resolver)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is True
    assert events == []


def test_allow_agent_cached_sync_has_no_semantic_host_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    assert sync_config(project, config_path=config_path, allow_agent=True).success
    events: list[str] = []

    def record_host(_config: AppConfig) -> None:
        events.append("host-env")

    def record_seed(_root: Path, *, source: ConfigSyncSource) -> None:
        events.append(f"seed:{source}")

    def record_delivery(
        _config: AppConfig,
        _root: Path,
        _desired: sync_module._DesiredState,  # pyright: ignore[reportPrivateUsage]
    ) -> None:
        events.append("delivery")

    def record_resolver(
        _source: ConfigSyncSource,
        _item: UnresolvedItem,
        *,
        deadline: float,
    ) -> SemanticAgentResult:
        del deadline
        events.append("resolver")
        raise AssertionError("cached sync invoked semantic resolver")

    monkeypatch.setattr(sync_module, "ensure_host_env", record_host)
    monkeypatch.setattr(sync_module, "seed_config", record_seed)
    monkeypatch.setattr(sync_module, "_deliver_codex_semantic_source", record_delivery)
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", record_resolver)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is True
    assert events == []


def test_unmanaged_collision_blocks_without_mutation(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    collision = project / "config/opencode/AGENTS.md"
    collision.write_bytes(b"operator bytes\n")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert DriftClass.UNMANAGED_COLLISION in result.audit.drift_classes
    assert collision.read_bytes() == b"operator bytes\n"
    assert not (project / "config/codex/AGENTS.md").exists()
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_visible_collision_blocks_before_semantic_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    (project / "config/opencode/AGENTS.md").write_bytes(b"operator bytes\n")
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is False
    assert DriftClass.UNMANAGED_COLLISION in result.audit.drift_classes
    assert resolver.calls == []
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_unmanaged_semantic_only_path_blocks_before_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    collision = project / "config/opencode/skills/convergence-loop/SKILL.md"
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"operator bytes\n")
    resolver = _FakeSemanticResolver()
    bootstrap_events: list[str] = []

    def record_host(_config: AppConfig) -> None:
        bootstrap_events.append("host-env")

    def record_seed(_root: Path, *, source: ConfigSyncSource) -> None:
        bootstrap_events.append(f"seed:{source}")

    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    monkeypatch.setattr(sync_module, "ensure_host_env", record_host)
    monkeypatch.setattr(sync_module, "seed_config", record_seed)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is False
    assert DriftClass.UNMANAGED_COLLISION in result.audit.drift_classes
    assert resolver.calls == []
    assert bootstrap_events == []
    assert collision.read_bytes() == b"operator bytes\n"
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_explicit_sync_repairs_managed_target_and_preserves_carrier_neighbors(
    tmp_path: Path,
) -> None:
    project, config_path = _workspace(tmp_path)
    carrier = project / "config/codex/config.toml"
    carrier.write_text('theme = "kept"\n')
    assert sync_config(project, config_path=config_path).success
    target = project / "config/opencode/AGENTS.md"
    target.write_bytes(b"drift\n")

    result = sync_config(project, config_path=config_path)

    assert result.success is True
    assert target.read_bytes() == b"Shared instructions.\n"
    settings = tomllib.loads(carrier.read_text())
    assert settings["theme"] == "kept"
    assert settings["project_doc_fallback_filenames"] == ["CLAUDE.md"]


@pytest.mark.parametrize(
    "unsafe",
    [
        "schema",
        "schema-type",
        "path",
        "native-only-path",
        "native-only-shared-path",
        "top-extra",
        "managed-extra",
        "file-state-extra",
        "fragment-extra",
    ],
)
def test_invalid_manifest_blocks_without_mutation(tmp_path: Path, unsafe: str) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    if unsafe == "schema":
        manifest["schema_version"] = 2
    elif unsafe == "schema-type":
        manifest["schema_version"] = True
    elif unsafe == "path":
        files = manifest["managed"]["codex"]["files"]
        files["../outside"] = files.pop("AGENTS.md")
    elif unsafe == "native-only-path":
        state = next(iter(manifest["managed"]["codex"]["files"].values()))
        manifest["managed"]["claude"]["native_only"]["../outside"] = state
    elif unsafe == "native-only-shared-path":
        state = next(iter(manifest["managed"]["codex"]["files"].values()))
        manifest["managed"]["claude"]["native_only"]["agents/shared.md"] = state
    elif unsafe == "top-extra":
        manifest["body"] = "private"
    elif unsafe == "managed-extra":
        manifest["managed"]["codex"]["body"] = "private"
    elif unsafe == "file-state-extra":
        manifest["managed"]["codex"]["files"]["AGENTS.md"]["body"] = "private"
    else:
        manifest["managed"]["codex"]["fragments"][0]["body"] = "private"
    manifest_path.write_text(json.dumps(manifest))
    target = project / "config/opencode/AGENTS.md"
    original = target.read_bytes()

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.MANIFEST_INVALID,)
    assert target.read_bytes() == original


def test_manifest_file_and_native_only_overlap_blocks_before_any_mutation(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    command = project / "config/claude/commands/codex-review.md"
    command.parent.mkdir()
    command.write_text('---\ndescription: "Claude-only review"\n---\n\nReview safely.\n')
    assert sync_config(project, config_path=config_path).success
    _select_source(config_path, tmp_path / "code", "codex")
    assert sync_config(project, config_path=config_path).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    state = manifest["managed"]["claude"]["native_only"]["commands/codex-review.md"]
    manifest["managed"]["claude"]["files"]["commands/codex-review.md"] = state
    manifest_path.write_text(json.dumps(manifest))
    before = _tree_snapshot(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.MANIFEST_INVALID,)
    assert _tree_snapshot(project / "config") == before
    assert command.read_text().endswith("Review safely.\n")


def test_source_change_before_publication_is_retryable_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    original = sync_module._source_fingerprints  # pyright: ignore[reportPrivateUsage]
    calls = 0

    def race(root: Path, source: ConfigSyncSource) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        if calls == 4:
            (project / "config/claude/CLAUDE.md").write_text("changed during sync\n")
        return original(root, source)

    monkeypatch.setattr(sync_module, "_source_fingerprints", race)

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.retryable is True
    assert result.audit.drift_classes == (DriftClass.SOURCE_CHANGED,)
    assert not (project / "config/codex/AGENTS.md").exists()
    assert not (project / "config" / MANIFEST_NAME).exists()


@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_stage_root_replacement_cannot_escape_or_delete_unmanaged_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
    timing: str,
) -> None:
    project, config_path = _workspace(tmp_path)
    config_dir = project / "config"
    outside = tmp_path / "outside-stage-target"
    outside.mkdir()
    outside_marker = outside / "keep.txt"
    outside_marker.write_text("unchanged")
    original = sync_module._stage_bytes  # pyright: ignore[reportPrivateUsage]
    attacked = False
    parked: Path | None = None
    replacement_path: Path | None = None

    def attack(stage: sync_module._Stage) -> None:  # pyright: ignore[reportPrivateUsage]
        nonlocal attacked, parked, replacement_path
        stage_path = config_dir / stage.name
        parked = config_dir / f"{stage.name}-parked"
        stage_path.rename(parked)
        replacement_path = stage_path
        if replacement == "symlink":
            stage_path.symlink_to(outside, target_is_directory=True)
        else:
            stage_path.mkdir()
            (stage_path / "unmanaged.txt").write_text("retain")
        attacked = True

    def replacing_stage(
        config_fd: int,
        stage: sync_module._Stage,  # pyright: ignore[reportPrivateUsage]
        relative: PurePosixPath,
        content: bytes,
        executable: bool,
    ) -> sync_module._StagedFile:  # pyright: ignore[reportPrivateUsage]
        if timing == "before" and not attacked:
            attack(stage)
        staged = original(config_fd, stage, relative, content, executable)
        if timing == "after" and not attacked:
            attack(stage)
        return staged

    monkeypatch.setattr(sync_module, "_stage_bytes", replacing_stage)

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.retryable is True
    assert attacked is True
    assert outside_marker.read_text() == "unchanged"
    assert list(outside.iterdir()) == [outside_marker]
    assert parked is not None and parked.is_dir() and list(parked.iterdir()) == []
    assert replacement_path is not None
    if replacement == "symlink":
        assert replacement_path.is_symlink()
        assert replacement_path.resolve() == outside
    else:
        assert replacement_path.is_dir()
        assert (replacement_path / "unmanaged.txt").read_text() == "retain"
    assert not (project / "config/codex/AGENTS.md").exists()
    assert not (config_dir / MANIFEST_NAME).exists()


def test_external_source_root_symlink_blocks_without_read_or_publish(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    source_root = project / "config/claude"
    source_root.joinpath("CLAUDE.md").unlink()
    source_root.rmdir()
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "CLAUDE.md").write_text("private instructions\n")
    source_root.symlink_to(outside, target_is_directory=True)

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert DriftClass.INVALID_VIEW in result.audit.drift_classes
    assert not (project / "config/codex/AGENTS.md").exists()
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_clean_source_switch_succeeds_but_dirty_candidate_blocks(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    code_dir = tmp_path / "code"
    assert sync_config(project, config_path=config_path).success

    _select_source(config_path, code_dir, "codex")
    switched = sync_config(project, config_path=config_path)

    assert switched.success is True
    assert audit_config_sync(project, config_path=config_path).clean

    _select_source(config_path, code_dir, "claude")
    assert sync_config(project, config_path=config_path).success
    (project / "config/codex/AGENTS.md").write_text("edited candidate\n")
    _select_source(config_path, code_dir, "codex")

    blocked = sync_config(project, config_path=config_path)

    assert blocked.success is False
    assert DriftClass.MANAGED_TARGET in blocked.audit.drift_classes
    assert (project / "config/codex/AGENTS.md").read_text() == "edited candidate\n"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("claude", "codex"),
        ("claude", "opencode"),
        ("codex", "claude"),
        ("codex", "opencode"),
        ("opencode", "claude"),
        ("opencode", "codex"),
    ],
)
def test_semantic_runtime_variant_survives_clean_source_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: ConfigSyncSource,
    target: ConfigSyncSource,
) -> None:
    project, config_path = _workspace(tmp_path, source=source)
    source_name = "claude-md-management" if source == "claude" else "agents-md-management"
    skill = project / "config" / source / "skills" / source_name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {source_name}\ndescription: Manage native instructions\n---\n\n"
        f"{source} runtime semantics.\n"
    )
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    assert sync_config(project, config_path=config_path, allow_agent=True).success
    resolver.calls.clear()
    _select_source(config_path, tmp_path / "code", target)

    switched = sync_config(project, config_path=config_path, allow_agent=True)

    assert switched.success is True
    assert audit_config_sync(project, config_path=config_path).clean
    assert resolver.calls


def test_semantic_source_switch_still_blocks_modified_former_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    skill = _add_semantic_skill(project)
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    assert sync_config(project, config_path=config_path, allow_agent=True).success
    resolver.calls.clear()
    skill.write_text(
        "---\nname: convergence-loop\ndescription: Changed before switch\n---\n\n"
        "Unrecorded former-authority bytes.\n"
    )
    _select_source(config_path, tmp_path / "code", "codex")

    blocked = sync_config(project, config_path=config_path, allow_agent=True)

    assert blocked.success is False
    assert DriftClass.UNMANAGED_COLLISION in blocked.audit.drift_classes
    assert resolver.calls == []
    assert "Unrecorded former-authority bytes." in skill.read_text()


def test_non_byte_identical_source_switch_uses_recorded_candidate_state(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    agent = project / "config/claude/agents/reviewer.md"
    agent.parent.mkdir()
    agent.write_text(
        "---\nname: reviewer\ndescription: 'Reviews carefully'\n---\nReview carefully.\n"
    )
    original = agent.read_bytes()
    assert sync_config(project, config_path=config_path).success
    _select_source(config_path, tmp_path / "code", "codex")

    switched = sync_config(project, config_path=config_path)

    assert switched.success is True
    assert agent.read_bytes() != original
    assert audit_config_sync(project, config_path=config_path).clean


def test_source_switch_detects_executable_mode_drift(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    candidate = project / "config/codex/AGENTS.md"
    candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    _select_source(config_path, tmp_path / "code", "codex")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert DriftClass.MANAGED_TARGET in result.audit.drift_classes
    assert candidate.stat().st_mode & stat.S_IXUSR


def test_mode_only_target_drift_is_audited_and_repaired(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    target = project / "config/opencode/AGENTS.md"
    target.chmod(target.stat().st_mode | stat.S_IXUSR)

    audit = audit_config_sync(project, config_path=config_path)
    repaired = sync_config(project, config_path=config_path)

    assert DriftClass.MANAGED_TARGET in audit.drift_classes
    assert repaired.success is True
    assert not target.stat().st_mode & stat.S_IXUSR


def test_internal_source_file_symlink_is_snapshotted_as_regular_file(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    context = project / "config/claude/context"
    context.mkdir()
    original = context / "original.md"
    original.write_text("Reusable context.\n")
    (context / "alias.md").symlink_to("original.md")

    result = sync_config(project, config_path=config_path)

    assert result.success is True
    for tool in ("codex", "opencode"):
        alias = project / "config" / tool / "context/alias.md"
        assert alias.is_file()
        assert not alias.is_symlink()
        assert alias.read_text() == "Reusable context.\n"
    manifest = json.loads((project / "config" / MANIFEST_NAME).read_text())
    assert manifest["source_files"]["context/alias.md"] == {
        "executable": False,
        "hash": manifest["source_files"]["context/original.md"]["hash"],
    }
    assert audit_config_sync(project, config_path=config_path).clean


def test_source_fingerprints_preserve_legacy_digests_in_one_tree_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "claude"
    context = root / "context"
    context.mkdir(parents=True)
    (root / "CLAUDE.md").write_text("Shared instructions.\n")
    (context / "original.md").write_text("Reusable context.\n")
    (context / "alias.md").symlink_to("original.md")
    original_entries = sync_module._source_entries  # pyright: ignore[reportPrivateUsage]
    traversals = 0

    def counted_entries(source_root: Path, tool: ConfigSyncSource) -> tuple[Path, ...]:
        nonlocal traversals
        traversals += 1
        return original_entries(source_root, tool)

    monkeypatch.setattr(sync_module, "_source_entries", counted_entries)

    identity, material = sync_module._source_fingerprints(  # pyright: ignore[reportPrivateUsage]
        root, "claude"
    )

    assert traversals == 1
    assert identity == "1f36e3531090a535976f6ba2ae2952146b6cc9591da810917bcdc8a1d31f2500"
    assert material == "7f2ce9155ce0dec3820ccd7910c6b9c2f13911fac99f099ba8792ac842dd8055"
    assert identity != material


@pytest.mark.parametrize("failure", ["replace", "remove", "manifest"])
def test_publication_failure_is_retryable_on_next_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    project, config_path = _workspace(tmp_path)
    source_agent = project / "config/claude/agents/reviewer.md"
    source_agent.parent.mkdir()
    source_agent.write_text("---\nname: reviewer\ndescription: Reviews\n---\n\nReview.\n")

    if failure == "remove":
        assert sync_config(project, config_path=config_path).success
        source_agent.unlink()
        original_rename = sync_module.rename_noreplace

        def fail_remove(source_fd: int, source: str, target_fd: int, target: str) -> None:
            if source in {"reviewer.md", "reviewer.toml"} and target.startswith(".quarantine-"):
                raise OSError("injected remove failure")
            original_rename(source_fd, source, target_fd, target)

        monkeypatch.setattr(sync_module, "rename_noreplace", fail_remove)
    elif failure == "replace":
        original_rename = sync_module.rename_noreplace
        calls = 0

        def fail_replace(source_fd: int, source: str, target_fd: int, target: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected replace failure")
            original_rename(source_fd, source, target_fd, target)

        monkeypatch.setattr(sync_module, "rename_noreplace", fail_replace)
    else:
        original_replace_file_at = sync_module._replace_file_at  # pyright: ignore[reportPrivateUsage]

        def fail_manifest(
            config_fd: int,
            stage: sync_module._Stage,  # pyright: ignore[reportPrivateUsage]
            staged: sync_module._StagedFile,  # pyright: ignore[reportPrivateUsage]
            target: PurePosixPath,
            expected: sync_module._FileState | None,  # pyright: ignore[reportPrivateUsage]
        ) -> None:
            if str(target) == MANIFEST_NAME:
                raise OSError("injected pre-manifest failure")
            original_replace_file_at(  # pyright: ignore[reportArgumentType]
                config_fd, stage, staged, target, expected
            )

        monkeypatch.setattr(sync_module, "_replace_file_at", fail_manifest)

    failed = sync_config(project, config_path=config_path)
    monkeypatch.undo()
    retried = sync_config(project, config_path=config_path)

    assert failed.success is False
    assert failed.retryable is True
    assert retried.success is True
    assert audit_config_sync(project, config_path=config_path).clean


def test_stale_file_edit_race_never_unlinks_the_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    source_agent = project / "config/claude/agents/reviewer.md"
    source_agent.parent.mkdir()
    source_agent.write_text("---\nname: reviewer\ndescription: Reviews\n---\n\nReview.\n")
    assert sync_config(project, config_path=config_path).success
    source_agent.unlink()
    raced = project / "config/codex/agents/reviewer.toml"
    original_unlink_file_at = sync_module._unlink_file_at  # pyright: ignore[reportPrivateUsage]
    injected = False

    def edit_before_unlink(config_fd: int, target: object, expected: object) -> bool:
        nonlocal injected
        if not injected and str(target).endswith("agents/reviewer.toml"):
            injected = True
            raced.write_text("operator edit during sync\n")
        return original_unlink_file_at(config_fd, target, expected)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(sync_module, "_unlink_file_at", edit_before_unlink)

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.retryable is True
    assert result.audit.drift_classes == (DriftClass.SOURCE_CHANGED,)
    assert raced.read_text() == "operator edit during sync\n"


def test_new_unmanaged_file_appearing_before_replace_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    target_path = PurePosixPath("codex/AGENTS.md")
    target = project / "config/codex/AGENTS.md"
    original = sync_module._replace_file_at  # pyright: ignore[reportPrivateUsage]
    attacked = False

    def racing_replace(
        config_fd: int,
        stage: sync_module._Stage,  # pyright: ignore[reportPrivateUsage]
        staged: sync_module._StagedFile,  # pyright: ignore[reportPrivateUsage]
        relative: PurePosixPath,
        expected: sync_module._FileState | None,  # pyright: ignore[reportPrivateUsage]
    ) -> None:
        nonlocal attacked
        if relative == target_path and not attacked:
            target.write_bytes(b"UNMANAGED-SENTINEL\n")
            attacked = True
        original(  # pyright: ignore[reportArgumentType]
            config_fd, stage, staged, relative, expected
        )

    monkeypatch.setattr(sync_module, "_replace_file_at", racing_replace)

    result = sync_config(project, config_path=config_path)

    assert attacked is True
    assert result.success is False
    assert result.retryable is True
    assert target.read_bytes() == b"UNMANAGED-SENTINEL\n"


def test_atomic_replace_absent_and_unlink_races_preserve_sentinels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    source_agent = project / "config/claude/agents/reviewer.md"
    source_agent.parent.mkdir()
    source_agent.write_text("---\nname: reviewer\ndescription: Reviews\n---\n\nReview v1.\n")
    assert sync_config(project, config_path=config_path).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    source_agent.write_text("---\nname: reviewer\ndescription: Reviews\n---\n\nReview v2.\n")
    target = project / "config/codex/agents/reviewer.toml"
    original_read = sync_module._read_regular_at  # pyright: ignore[reportPrivateUsage]
    attacked = False

    def racing_replace(parent_fd: int, name: str) -> tuple[bytes, bool] | None:
        nonlocal attacked
        value = original_read(parent_fd, name)
        if name == "reviewer.toml" and not attacked:
            target.write_bytes(b"UNMANAGED-REPLACE\n")
            attacked = True
        return value

    monkeypatch.setattr(sync_module, "_read_regular_at", racing_replace)
    replaced = sync_config(project, config_path=config_path)
    assert attacked and not replaced.success and replaced.retryable
    assert target.read_bytes() == b"UNMANAGED-REPLACE\n"
    assert manifest_path.read_bytes() == manifest_before

    monkeypatch.undo()
    project, config_path = _workspace(tmp_path / "absent")
    target = project / "config/codex/AGENTS.md"
    original_rename = sync_module.rename_noreplace
    attacked = False

    def racing_absent(source_fd: int, source: str, target_fd: int, name: str) -> None:
        nonlocal attacked
        if name == "AGENTS.md" and not attacked:
            target.write_bytes(b"UNMANAGED-ABSENT\n")
            attacked = True
        original_rename(source_fd, source, target_fd, name)

    monkeypatch.setattr(sync_module, "rename_noreplace", racing_absent)
    absent = sync_config(project, config_path=config_path)
    assert attacked and not absent.success and absent.retryable
    assert target.read_bytes() == b"UNMANAGED-ABSENT\n"
    assert not (project / "config" / MANIFEST_NAME).exists()

    monkeypatch.undo()
    project, config_path = _workspace(tmp_path / "unlink")
    source_agent = project / "config/claude/agents/reviewer.md"
    source_agent.parent.mkdir()
    source_agent.write_text("---\nname: reviewer\ndescription: Reviews\n---\n\nReview.\n")
    assert sync_config(project, config_path=config_path).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    source_agent.unlink()
    target = project / "config/codex/agents/reviewer.toml"
    original_read = sync_module._read_regular_at  # pyright: ignore[reportPrivateUsage]
    attacked = False

    def racing_unlink(parent_fd: int, name: str) -> tuple[bytes, bool] | None:
        nonlocal attacked
        value = original_read(parent_fd, name)
        if name == "reviewer.toml" and not attacked:
            target.write_bytes(b"UNMANAGED-UNLINK\n")
            attacked = True
        return value

    monkeypatch.setattr(sync_module, "_read_regular_at", racing_unlink)
    unlinked = sync_config(project, config_path=config_path)
    assert attacked and not unlinked.success and unlinked.retryable
    assert target.read_bytes() == b"UNMANAGED-UNLINK\n"
    assert manifest_path.read_bytes() == manifest_before


def test_quarantine_bookkeeping_failure_requires_recovery_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    (project / "config/claude/CLAUDE.md").write_text("Shared instructions v2.\n")
    target = project / "config/codex/AGENTS.md"
    target_parent = target.parent.stat()
    original_read = sync_module._read_regular_at  # pyright: ignore[reportPrivateUsage]
    original_stat = sync_module.os.stat
    attacked = False
    failed = False

    def racing_read(parent_fd: int, name: str) -> tuple[bytes, bool] | None:
        nonlocal attacked
        value = original_read(parent_fd, name)
        parent = os.fstat(parent_fd)
        if (
            name == target.name
            and (parent.st_dev, parent.st_ino) == (target_parent.st_dev, target_parent.st_ino)
            and not attacked
        ):
            target.write_bytes(b"UNIQUE-OPERATOR-EDIT\n")
            attacked = True
        return value

    def failing_stat(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal failed
        if attacked and isinstance(path, str) and path.startswith(".quarantine-") and not failed:
            failed = True
            raise OSError("injected quarantine bookkeeping failure")
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(sync_module, "_read_regular_at", racing_read)
    monkeypatch.setattr(sync_module.os, "stat", failing_stat)
    result = sync_config(project, config_path=config_path)
    monkeypatch.undo()

    assert attacked and failed
    assert not result.success and result.retryable
    assert {problem.identifier for problem in result.audit.problems} == {"quarantine-preserved"}
    assert not target.exists()
    assert manifest_path.read_bytes() == manifest_before
    quarantines = list((project / "config").glob(".djinn-config-sync-stage-*/.quarantine-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b"UNIQUE-OPERATOR-EDIT\n"


def test_parent_and_root_detach_after_open_cannot_publish_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    source_agent = project / "config/claude/agents/reviewer.md"
    source_agent.parent.mkdir()
    source_agent.write_text("---\nname: reviewer\ndescription: Reviews\n---\n\nReview v1.\n")
    assert sync_config(project, config_path=config_path).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    source_agent.write_text("---\nname: reviewer\ndescription: Reviews\n---\n\nReview v2.\n")
    target = project / "config/codex/agents/reviewer.toml"
    original_read = sync_module._read_regular_at  # pyright: ignore[reportPrivateUsage]
    attacked = False

    def detaching_read(parent_fd: int, name: str) -> tuple[bytes, bool] | None:
        nonlocal attacked
        value = original_read(parent_fd, name)
        if name == target.name and not attacked:
            parent = target.parent
            parent.rename(project / "config/codex/agents-parked")
            parent.mkdir()
            target.write_bytes(b"UNMANAGED-SENTINEL\n")
            attacked = True
        return value

    monkeypatch.setattr(sync_module, "_read_regular_at", detaching_read)
    parent_result = sync_config(project, config_path=config_path)
    assert attacked and not parent_result.success and parent_result.retryable
    assert target.read_bytes() == b"UNMANAGED-SENTINEL\n"
    assert manifest_path.read_bytes() == manifest_before

    monkeypatch.undo()
    project, config_path = _workspace(tmp_path / "root")
    assert sync_config(project, config_path=config_path).success
    source = project / "config/claude/CLAUDE.md"
    source.write_text("Shared instructions v2.\n")
    config_root = project / "config"
    original_guard = sync_module._require_config_root_attached  # pyright: ignore[reportPrivateUsage]
    guards = 0

    def detaching_guard(path: Path, config_fd: int) -> None:
        nonlocal guards
        original_guard(path, config_fd)
        guards += 1
        if guards == 4:
            config_root.rename(project / "config-parked")
            config_root.mkdir()
            (config_root / "UNMANAGED").write_text("retain")

    monkeypatch.setattr(sync_module, "_require_config_root_attached", detaching_guard)
    root_result = sync_config(project, config_path=config_path)
    assert guards >= 4 and not root_result.success and root_result.retryable
    assert (config_root / "UNMANAGED").read_text() == "retain"
    assert not (config_root / MANIFEST_NAME).exists()


def test_stage_name_exhaustion_is_exact_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    monkeypatch.setattr(sync_module.secrets, "token_hex", _returns("fixed"))
    (project / "config/.djinn-config-sync-stage-fixed").mkdir()

    result = sync_config(project, config_path=config_path)

    assert not result.success and result.retryable
    assert result.audit.drifts == ()
    assert {problem.identifier for problem in result.audit.problems} == {"stage-create-failed"}
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_carrier_neighbor_race_is_preserved_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    carrier = project / "config/codex/config.toml"
    carrier.write_text('theme = "before"\n')
    original_replace = sync_module._replace_carrier_at  # pyright: ignore[reportPrivateUsage]
    injected = False

    def race_carrier(
        config_fd: int,
        stage: sync_module._Stage,  # pyright: ignore[reportPrivateUsage]
        staged: sync_module._StagedFile,  # pyright: ignore[reportPrivateUsage]
        target: PurePosixPath,
        expected: sync_module._CarrierState | None,  # pyright: ignore[reportPrivateUsage]
    ) -> None:
        nonlocal injected
        if not injected:
            injected = True
            carrier.write_text('theme = "during"\n')
        original_replace(  # pyright: ignore[reportArgumentType]
            config_fd, stage, staged, target, expected
        )

    monkeypatch.setattr(sync_module, "_replace_carrier_at", race_carrier)

    failed = sync_config(project, config_path=config_path)
    monkeypatch.undo()
    retried = sync_config(project, config_path=config_path)

    assert failed.success is False
    assert failed.retryable is True
    assert tomllib.loads(carrier.read_text())["theme"] == "during"
    assert retried.success is True
    assert tomllib.loads(carrier.read_text())["project_doc_fallback_filenames"] == ["CLAUDE.md"]


def test_audit_waits_for_shared_lock_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    config_dir = project / "config"
    before = _tree_snapshot(config_dir)
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_exclusive_lock, args=(config_dir, ready, release))
    holder.start()
    assert ready.wait(5)
    attempted = threading.Event()
    original_lock = sync_module.config_directory_lock

    @contextmanager
    def observed_lock(path: Path, *, exclusive: bool) -> Iterator[int]:
        attempted.set()
        with original_lock(path, exclusive=exclusive) as descriptor:
            yield descriptor

    monkeypatch.setattr(sync_module, "config_directory_lock", observed_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(audit_config_sync, project, config_path=config_path)
            assert attempted.wait(5)
            assert not future.done()
            release.set()
            audit = future.result(timeout=5)
    finally:
        release.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join()

    assert DriftClass.SOURCE_ONLY in audit.drift_classes
    assert holder.exitcode == 0
    assert _tree_snapshot(config_dir) == before


def test_problem_objects_never_disclose_external_source_sentinel(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    sentinel = "TOP-SECRET-SENTINEL-DO-NOT-DISCLOSE"
    outside = tmp_path / "private.md"
    outside.write_text(sentinel)
    link = project / "config/claude/context/private.md"
    link.parent.mkdir()
    link.symlink_to(outside)

    audit = audit_config_sync(project, config_path=config_path)
    result = sync_config(project, config_path=config_path)
    public_text = repr((audit, result)) + " ".join(
        item.message for item in (*audit.drifts, *audit.problems, *result.audit.drifts)
    )

    assert result.success is False
    assert DriftClass.INVALID_VIEW in audit.drift_classes
    assert sentinel not in public_text
    assert not (project / "config/codex/context/private.md").exists()


def test_dormant_native_only_is_preserved_blocked_and_adopted_by_owner(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    command = project / "config/claude/commands/codex-review.md"
    command.parent.mkdir()
    command.write_text('---\ndescription: "Claude-only review"\n---\n\nReview safely.\n')
    recorded = command.read_bytes()
    assert sync_config(project, config_path=config_path).success
    first_manifest = json.loads((project / "config" / MANIFEST_NAME).read_text())
    assert (
        first_manifest["managed"]["claude"]["native_only"]["commands/codex-review.md"]["hash"]
        == hashlib.sha256(recorded).hexdigest()
    )

    _select_source(config_path, tmp_path / "code", "codex")
    assert sync_config(project, config_path=config_path).success
    assert command.read_bytes() == recorded
    dormant_manifest = json.loads((project / "config" / MANIFEST_NAME).read_text())
    assert (
        dormant_manifest["managed"]["claude"]["native_only"]
        == first_manifest["managed"]["claude"]["native_only"]
    )

    edited = recorded.replace(b"Review safely.", b"Review edited native bytes.")
    command.write_bytes(edited)
    dormant_audit = audit_config_sync(project, config_path=config_path)
    blocked = sync_config(project, config_path=config_path)

    assert DriftClass.MANAGED_TARGET in dormant_audit.drift_classes
    assert any(
        item.identifier.startswith("dormant-native-only:") for item in blocked.audit.problems
    )
    assert blocked.success is False
    assert command.read_bytes() == edited

    _select_source(config_path, tmp_path / "code", "claude")
    adopted = sync_config(project, config_path=config_path)
    adopted_manifest = json.loads((project / "config" / MANIFEST_NAME).read_text())

    assert adopted.success is True
    assert (
        adopted_manifest["managed"]["claude"]["native_only"]["commands/codex-review.md"]["hash"]
        == hashlib.sha256(edited).hexdigest()
    )


@pytest.mark.parametrize("change", ["new", "missing"])
def test_new_or_missing_dormant_native_only_blocks(tmp_path: Path, change: str) -> None:
    project, config_path = _workspace(tmp_path)
    command = project / "config/claude/commands/codex-review.md"
    if change == "missing":
        command.parent.mkdir()
        command.write_text('---\ndescription: "Review"\n---\n\nReview.\n')
    assert sync_config(project, config_path=config_path).success
    _select_source(config_path, tmp_path / "code", "codex")
    assert sync_config(project, config_path=config_path).success
    if change == "new":
        command.parent.mkdir(exist_ok=True)
        command.write_text('---\ndescription: "Review"\n---\n\nReview.\n')
    else:
        command.unlink()

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert DriftClass.MANAGED_TARGET in result.audit.drift_classes
    assert any(item.identifier.startswith("dormant-native-only:") for item in result.audit.problems)


def test_switch_to_native_owner_requires_native_validation(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    command = project / "config/claude/commands/codex-review.md"
    command.parent.mkdir()
    command.write_text('---\ndescription: "Review"\n---\n\nReview.\n')
    assert sync_config(project, config_path=config_path).success
    _select_source(config_path, tmp_path / "code", "codex")
    assert sync_config(project, config_path=config_path).success
    command.write_text(
        '---\ndescription: "Review"\ntools:\n  Read: true\n---\n\nUnvalidated native edit.\n'
    )
    _select_source(config_path, tmp_path / "code", "claude")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert DriftClass.INVALID_VIEW in result.audit.drift_classes


def test_switch_back_blocks_new_dormant_shared_artifact_without_mutation(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    _select_source(config_path, tmp_path / "code", "codex")
    assert sync_config(project, config_path=config_path).success
    agent = project / "config/claude/agents/new.md"
    agent.parent.mkdir(exist_ok=True)
    agent.write_text('---\nname: new\ndescription: "New dormant agent"\n---\n\nDo work.\n')
    before = _tree_snapshot(project / "config")
    _select_source(config_path, tmp_path / "code", "claude")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert DriftClass.MANAGED_TARGET in result.audit.drift_classes
    assert any(
        item.relative_path is not None
        and item.relative_path.as_posix() == "agents/new.md"
        and item.message == "Candidate source contains a new shared artifact."
        for item in result.audit.drifts
    )
    assert _tree_snapshot(project / "config") == before
    manifest = json.loads((project / "config" / MANIFEST_NAME).read_text())
    assert manifest["active_source"] == "codex"


def test_stale_fragment_race_and_followup_never_delete_the_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    original_value = {"operator": "recorded"}
    value_hash = hashlib.sha256(
        json.dumps(original_value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest["managed"]["codex"]["fragments"].append(
        {
            "carrier_path": "hooks.json",
            "key_path": ["hooks", "Stop"],
            "value_hash": value_hash,
        }
    )
    manifest_path.write_text(json.dumps(manifest))
    carrier = project / "config/codex/hooks.json"
    carrier.write_text(json.dumps({"hooks": {"Stop": original_value}, "neighbor": "kept"}))
    (project / "config/claude/CLAUDE.md").write_text("Changed source.\n")
    original_state_at = sync_module._carrier_state_at  # pyright: ignore[reportPrivateUsage]
    raced_value = {"operator": "edited-during-sync"}
    injected = False

    def inject_before_reread(config_fd: int, relative: object) -> object:
        nonlocal injected
        if not injected and str(relative) == "codex/hooks.json":
            injected = True
            carrier.write_text(json.dumps({"hooks": {"Stop": raced_value}, "neighbor": "kept"}))
        return original_state_at(config_fd, relative)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(sync_module, "_carrier_state_at", inject_before_reread)
    raced = sync_config(project, config_path=config_path)
    monkeypatch.undo()
    blocked = sync_config(project, config_path=config_path)

    assert raced.success is False
    assert raced.retryable is True
    assert blocked.success is False
    assert blocked.retryable is False
    assert DriftClass.MANAGED_TARGET in blocked.audit.drift_classes
    assert json.loads(carrier.read_text())["hooks"]["Stop"] == raced_value
    assert json.loads(carrier.read_text())["neighbor"] == "kept"


def test_config_directory_symlink_fails_closed_without_external_writes(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    config_dir = project / "config"
    outside = tmp_path / "external-config"
    config_dir.rename(outside)
    config_dir.symlink_to(outside, target_is_directory=True)
    before = _tree_snapshot(outside)

    audit = audit_config_sync(project, config_path=config_path)
    result = sync_config(project, config_path=config_path)

    assert audit.drift_classes == (DriftClass.INVALID_VIEW,)
    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_VIEW,)
    assert _tree_snapshot(outside) == before
    assert not (outside / MANIFEST_NAME).exists()


@pytest.mark.parametrize("wrong", ["root", "parent-file", "parent-symlink"])
def test_wrong_type_publication_layout_blocks_before_any_output(tmp_path: Path, wrong: str) -> None:
    project, config_path = _workspace(tmp_path)
    source_context = project / "config/claude/context/info.md"
    source_context.parent.mkdir()
    source_context.write_text("Context bytes.\n")
    sentinel = b"DO-NOT-MUTATE"
    if wrong == "root":
        root = project / "config/opencode"
        root.rmdir()
        root.write_bytes(sentinel)
        sentinel_path = root
    else:
        parent = project / "config/codex/context"
        if wrong == "parent-file":
            parent.write_bytes(sentinel)
            sentinel_path = parent
        else:
            real = project / "config/codex/real-context"
            real.mkdir()
            marker = real / "sentinel"
            marker.write_bytes(sentinel)
            parent.symlink_to(real, target_is_directory=True)
            sentinel_path = marker
    before = _tree_snapshot(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert DriftClass.INVALID_VIEW in result.audit.drift_classes
    assert sentinel_path.read_bytes() == sentinel
    assert _tree_snapshot(project / "config") == before
    assert not (project / "config" / MANIFEST_NAME).exists()
    assert not (project / "config/codex/AGENTS.md").exists()


def test_deterministic_sync_never_calls_semantic_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)

    def unexpected_resolver(
        source: ConfigSyncSource,
        item: UnresolvedItem,
        *,
        deadline: float,
    ) -> SemanticAgentResult:
        del source, item, deadline
        raise AssertionError("deterministic sync invoked semantic resolver")

    monkeypatch.setattr(sync_module, "resolve_unresolved_item", unexpected_resolver)

    assert sync_config(project, config_path=config_path, allow_agent=True).success


def test_native_validation_failure_blocks_before_semantic_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    (project / "config/claude/CLAUDE.md").unlink()
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is False
    assert DriftClass.INVALID_VIEW in result.audit.drift_classes
    assert resolver.calls == []


def test_duplicate_semantic_contracts_block_before_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    agents = project / "config/claude/agents"
    agents.mkdir()
    for name in ("first", "second"):
        (agents / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: Reviews\ntools:\n  Read: true\n---\n\nPrompt.\n"
        )
    resolver = _FakeSemanticResolver()
    shared = Path("skills/shared/SKILL.md")

    def shared_contract(item: UnresolvedItem) -> ArtifactOutputContract:
        del item
        return ArtifactOutputContract((PurePosixPath(shared.as_posix()),), ())

    monkeypatch.setattr(sync_module, "allowed_outputs_for_unresolved", shared_contract)
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is False
    assert DriftClass.SEMANTIC_REQUIRED in result.audit.drift_classes
    assert resolver.calls == []
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_semantic_contract_cannot_claim_another_artifacts_static_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    resolver = _FakeSemanticResolver()

    def instruction_contract(item: UnresolvedItem) -> ArtifactOutputContract:
        del item
        return ArtifactOutputContract((PurePosixPath("AGENTS.md"),), ())

    monkeypatch.setattr(sync_module, "allowed_outputs_for_unresolved", instruction_contract)
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is False
    assert DriftClass.SEMANTIC_REQUIRED in result.audit.drift_classes
    assert resolver.calls == []
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_invalid_semantic_markdown_agent_blocks_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path, source="codex")
    agent = project / "config/codex/agents/reviewer.toml"
    agent.parent.mkdir()
    agent.write_text(
        'name = "reviewer"\ndescription = "Reviews"\n'
        'developer_instructions = "Review carefully."\nmodel = "custom"\n'
    )
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is False
    assert DriftClass.INVALID_VIEW in result.audit.drift_classes
    assert len(resolver.calls) == 2
    assert not (project / "config" / MANIFEST_NAME).exists()
    assert not (project / "config/claude/agents/reviewer.md").exists()
    assert not (project / "config/opencode/agents/reviewer.md").exists()


def test_semantic_publish_manifest_cache_and_shared_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    resolver = _FakeSemanticResolver()
    deadline = 9876.5
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    monkeypatch.setattr(sync_module, "start_semantic_deadline", lambda: deadline)

    first = sync_config(project, config_path=config_path, allow_agent=True)
    manifest_text = (project / "config" / MANIFEST_NAME).read_text()
    manifest = json.loads(manifest_text)
    second = sync_config(project, config_path=config_path, allow_agent=True)
    cached_without_agent = sync_config(project, config_path=config_path, allow_agent=False)

    assert first.success is True
    assert second.success is True
    assert second.changed_paths == ()
    assert cached_without_agent.success is True
    assert len(resolver.calls) == 2
    assert {item.target_tool for _source, item, _deadline in resolver.calls} == {
        "codex",
        "opencode",
    }
    assert {value for _source, _item, value in resolver.calls} == {deadline}
    assert len(manifest["semantic"]) == 2
    assert all(record["adapter_revision"] == ADAPTER_REVISION for record in manifest["semantic"])
    assert all(record["files"] for record in manifest["semantic"])
    assert "SOURCE-BODY-SENTINEL" not in manifest_text
    assert "SEMANTIC-OUTPUT-SENTINEL" not in manifest_text
    assert "source_content_base64" not in manifest_text
    assert audit_config_sync(project, config_path=config_path).clean


def test_valid_cache_is_read_only_and_invalid_cache_reruns_only_changed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    assert sync_config(project, config_path=config_path, allow_agent=True).success
    resolver.calls.clear()

    assert sync_config(project, config_path=config_path, allow_agent=False).success
    assert resolver.calls == []
    target = project / "config/codex/skills/convergence-loop/SKILL.md"
    target.write_text("edited cached output\n")
    audit = audit_config_sync(project, config_path=config_path)
    blocked = sync_config(project, config_path=config_path, allow_agent=False)

    assert resolver.calls == []
    assert DriftClass.SEMANTIC_REQUIRED in audit.drift_classes
    assert DriftClass.SEMANTIC_REQUIRED in blocked.audit.drift_classes
    assert blocked.success is False

    repaired = sync_config(project, config_path=config_path, allow_agent=True)

    assert repaired.success is True
    assert len(resolver.calls) == 1
    assert resolver.calls[0][1].target_tool == "codex"
    assert b"SEMANTIC-OUTPUT-SENTINEL" in target.read_bytes()


def test_unrelated_source_edit_reuses_semantic_artifact_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    assert sync_config(project, config_path=config_path, allow_agent=True).success
    resolver.calls.clear()
    (project / "config/claude/CLAUDE.md").write_text("Unrelated instruction edit.\n")

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is True
    assert resolver.calls == []
    assert (project / "config/codex/AGENTS.md").read_text() == "Unrelated instruction edit.\n"


@pytest.mark.parametrize(
    "failure",
    [
        SemanticFailure.EXECUTION_FAILED,
        SemanticFailure.TIMED_OUT,
        SemanticFailure.MALFORMED_RESPONSE,
        SemanticFailure.UNRESOLVED,
    ],
)
def test_semantic_failure_never_publishes_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: SemanticFailure,
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    resolver = _FakeSemanticResolver(failure=failure)
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    before = _tree_snapshot(project / "config")

    result = sync_config(project, config_path=config_path, allow_agent=True)
    public = repr(result)

    assert result.success is False
    assert DriftClass.SEMANTIC_REQUIRED in result.audit.drift_classes
    assert failure.value in public
    assert "SOURCE-BODY-SENTINEL" not in public
    assert "SEMANTIC-OUTPUT-SENTINEL" not in public
    assert _tree_snapshot(project / "config") == before
    assert not (project / "config" / MANIFEST_NAME).exists()


@pytest.mark.parametrize(
    "corruption",
    [
        "revision",
        "path",
        "foreign-path",
        "duplicate-record",
        "duplicate-file",
        "duplicate-json-key",
        "fragment",
        "extra-field",
    ],
)
def test_semantic_manifest_records_are_strict_and_duplicate_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    assert sync_config(project, config_path=config_path, allow_agent=True).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    record = next(item for item in manifest["semantic"] if item["target_tool"] == "codex")
    if corruption == "revision":
        record["adapter_revision"] = 1
    elif corruption == "path":
        record["files"][0]["path"] = "../outside"
    elif corruption == "foreign-path":
        record["files"][0]["path"] = "skills/other/SKILL.md"
    elif corruption == "duplicate-record":
        manifest["semantic"].append(deepcopy(record))
    elif corruption == "duplicate-file":
        record["files"].append(deepcopy(record["files"][0]))
    elif corruption == "duplicate-json-key":
        pass
    elif corruption == "fragment":
        record["fragments"].append(
            {
                "carrier_path": "hooks.json",
                "key_path": ["hooks", "Stop"],
                "value_hash": "0" * 64,
            }
        )
    else:
        record["response"] = "private provider response"
    serialized = json.dumps(manifest)
    if corruption == "duplicate-json-key":
        serialized = serialized.replace(
            '"fingerprint":',
            '"fingerprint":"' + "0" * 64 + '","fingerprint":',
            1,
        )
    manifest_path.write_text(serialized)
    targets_before = {
        tool: _tree_snapshot(project / "config" / tool) for tool in ("claude", "codex", "opencode")
    }

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.MANIFEST_INVALID,)
    assert {
        tool: _tree_snapshot(project / "config" / tool) for tool in ("claude", "codex", "opencode")
    } == targets_before


def test_deterministic_hook_fragments_survive_semantic_script_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path, source="opencode")
    script = project / "config/opencode/plugins/ready-notify.js"
    script.parent.mkdir(parents=True)
    script.write_text("export const Ready = async () => ({ event: async () => {} });\n")
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is True
    assert len(resolver.calls) == 2
    claude = json.loads((project / "config/claude/settings.json").read_text())
    codex = json.loads((project / "config/codex/hooks.json").read_text())
    assert claude["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("ready_notify_hook.py")
    assert codex["hooks"]["Stop"][0]["hooks"][0]["command"].endswith("hooks/ready_notify.py\"'")
    manifest = json.loads((project / "config" / MANIFEST_NAME).read_text())
    assert all(record["fragments"] == [] for record in manifest["semantic"])


def test_changed_hook_reruns_only_that_semantic_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    ready = project / "config/claude/ready_notify_hook.py"
    security = project / "config/claude/security_reminder_hook.py"
    ready.write_text("print('ready one')\n")
    security.write_text("print('security one')\n")
    settings = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "uv run python3 ~/.claude_seed/ready_notify_hook.py",
                        }
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": ("uv run python3 ~/.claude_seed/security_reminder_hook.py"),
                        }
                    ],
                }
            ],
        }
    }
    (project / "config/claude/settings.json").write_text(json.dumps(settings))
    resolver = _FakeSemanticResolver()
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)
    assert sync_config(project, config_path=config_path, allow_agent=True).success
    assert len(resolver.calls) == 4
    resolver.calls.clear()
    ready.write_text("print('ready two')\n")

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is True
    assert len(resolver.calls) == 2
    assert all(call[1].identifier.startswith("hook:ready:") for call in resolver.calls)
    assert {call[1].target_tool for call in resolver.calls} == {"codex", "opencode"}


@pytest.mark.parametrize("mutation", ["source", "config"])
def test_source_change_during_semantic_call_aborts_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    project, config_path = _workspace(tmp_path)
    _add_semantic_skill(project)
    changed = False

    def mutate_source() -> None:
        nonlocal changed
        if not changed:
            changed = True
            if mutation == "source":
                (project / "config/claude/CLAUDE.md").write_text("changed during agent\n")
            else:
                _select_source(config_path, tmp_path / "code", "codex")

    resolver = _FakeSemanticResolver(before_return=mutate_source)
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is False
    assert result.retryable is True
    assert result.audit.drift_classes == (DriftClass.SOURCE_CHANGED,)
    assert len(resolver.calls) == 1
    assert not (project / "config" / MANIFEST_NAME).exists()
    assert not (project / "config/codex/AGENTS.md").exists()
    assert not (project / "config/opencode/AGENTS.md").exists()


def test_target_change_during_semantic_call_preserves_edit_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    _add_semantic_skill(project)
    target = project / "config/opencode/AGENTS.md"

    def mutate_target() -> None:
        target.write_text("operator edit during agent\n")

    resolver = _FakeSemanticResolver(before_return=mutate_target)
    monkeypatch.setattr(sync_module, "resolve_unresolved_item", resolver)

    result = sync_config(project, config_path=config_path, allow_agent=True)

    assert result.success is False
    assert result.retryable is True
    assert target.read_text() == "operator edit during agent\n"
    assert manifest_path.read_bytes() == manifest_before
    assert not (project / "config/codex/skills/convergence-loop/SKILL.md").exists()
    assert not (project / "config/opencode/skills/convergence-loop/SKILL.md").exists()
