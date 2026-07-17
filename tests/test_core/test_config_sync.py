from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import stat
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

import djinn_in_a_box.core.config_sync as sync_module
import djinn_in_a_box.core.workflow_publisher as publisher_module
from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig, ConfigSyncSource
from djinn_in_a_box.core.config_sync import (
    MANIFEST_NAME,
    DriftClass,
    audit_config_sync,
    load_canonical_delivery_view,
    sync_config,
)
from djinn_in_a_box.core.workflow_publisher import CanonicalLockLease, canonical_lock

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REMOVED_ENGINE_TOKENS = (
    re.compile(r"\bdirectory_is_attached\b"),
    re.compile(r"\brename_noreplace\b"),
    re.compile(r"\bquarantine-preserved\b"),
    re.compile(r"\bstage-changed\b"),
    re.compile(r"\bstage-create-failed\b"),
    re.compile(r"(?<![A-Za-z0-9_])DELIVERY_MANIFEST_NAME(?![A-Za-z0-9_])"),
    re.compile(r"\b_open_real_directory\b"),
    re.compile(r"\b_reopen_real_directory\b"),
    re.compile(r"\b_provision_real_directory\b"),
    re.compile(r"\b_canonical_changed_failure\b"),
    re.compile(r"\b_config_changed_failure\b"),
    re.compile(r"\b_OPENCODE_DELIVERY_CODES\b"),
    re.compile(r"\b_OPENCODE_RETRY_CODES\b"),
    re.compile(r"\b_opencode_refresh_error\b"),
    re.compile(r"\bhook-python\b"),
    re.compile(r"\bast\.parse\b"),
)


def test_removed_workflow_engine_tokens_are_absent_from_product_code() -> None:
    matches: list[str] = []
    for root in (_PROJECT_ROOT / "src", _PROJECT_ROOT / "scripts"):
        for path in sorted(
            candidate
            for candidate in root.rglob("*")
            if candidate.is_file() and "__pycache__" not in candidate.parts
        ):
            source = path.read_text(encoding="utf-8")
            for token in _REMOVED_ENGINE_TOKENS:
                if token.search(source):
                    matches.append(f"{path.relative_to(_PROJECT_ROOT)}: {token.pattern}")

    assert not matches


def _workspace(tmp_path: Path, *, source: ConfigSyncSource = "claude") -> tuple[Path, Path]:
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


def _tree(root: Path) -> dict[PurePosixPath, tuple[bytes, int]]:
    return {
        PurePosixPath(path.relative_to(root).as_posix()): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _hash(path: Path) -> dict[str, object]:
    return {
        "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        "executable": bool(path.stat().st_mode & stat.S_IXUSR),
    }


def _objects(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _legacy_manifest(
    project: Path, *, semantic: list[dict[str, object]] | None = None
) -> dict[str, object]:
    config = project / "config"
    source_files = {
        path.relative_to(config / "claude").as_posix(): _hash(path)
        for path in (config / "claude").rglob("*")
        if path.is_file()
    }
    managed: dict[str, object] = {}
    for tool in ("claude", "codex", "opencode"):
        managed[tool] = {"files": {}, "native_only": {}, "fragments": []}
    return {
        "schema_version": 1,
        "adapter_revision": 3,
        "active_source": "claude",
        "source_hash": "0" * 64,
        "source_files": source_files,
        "managed": managed,
        "semantic": [] if semantic is None else semantic,
    }


def _hold_lock(root: Path, ready: ProcessEvent, release: ProcessEvent) -> None:
    with canonical_lock(root, exclusive=True):
        ready.set()
        release.wait(5)


def test_representative_sync_publishes_all_views_and_lean_manifest(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)

    result = sync_config(project, config_path=config_path)
    manifest = json.loads((project / "config" / MANIFEST_NAME).read_text())

    assert result.success
    assert audit_config_sync(project, config_path=config_path).clean
    assert (project / "config/codex/AGENTS.md").read_text() == "Shared instructions.\n"
    assert (project / "config/opencode/CLAUDE.md").read_text() == "Shared instructions.\n"
    assert set(manifest) == {"source", "items"}
    assert manifest["source"] == "claude"
    assert manifest["items"]
    assert all(
        set(item)
        in (
            {"path", "content_hash", "executable"},
            {"path", "key_path", "content_hash", "executable"},
        )
        for item in manifest["items"]
    )


def _change_source(project: Path) -> None:
    (project / "config/claude/CLAUDE.md").write_text("changed\n")


def _change_target(project: Path) -> None:
    (project / "config/codex/AGENTS.md").write_text("edited\n")


_DRIFT_CASES: list[tuple[DriftClass, Callable[[Path], None]]] = [
    (DriftClass.SOURCE_CHANGED, _change_source),
    (DriftClass.TARGET_DRIFT, _change_target),
]


@pytest.mark.parametrize(("drift", "prepare"), _DRIFT_CASES)
def test_sync_audit_classifies_source_and_managed_drift(
    tmp_path: Path,
    drift: DriftClass,
    prepare: Callable[[Path], None],
) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    prepare(project)

    audit = audit_config_sync(project, config_path=config_path)
    result = sync_config(project, config_path=config_path)

    assert drift in audit.drift_classes
    assert result.success is (drift is DriftClass.SOURCE_CHANGED)


def test_collision_and_invalid_artifacts_block_without_mutation(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    collision = project / "config/codex/AGENTS.md"
    collision.write_text("operator file\n")
    before = _tree(project / "config")

    blocked = sync_config(project, config_path=config_path)

    assert blocked.audit.drift_classes == (DriftClass.COLLISION,)
    assert _tree(project / "config") == before
    collision.unlink()
    agent = project / "config/claude/agents/reviewer.md"
    agent.parent.mkdir()
    agent.write_text("---\nname: reviewer\ndescription: Review\nmodel: native\n---\n\nReview.\n")
    before = _tree(project / "config")

    invalid = sync_config(project, config_path=config_path)

    assert invalid.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before


def test_snapshot_before_commit_blocks_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    before = _tree(project / "config")
    original = sync_module._build_views  # pyright: ignore[reportPrivateUsage]
    changed = False

    def race(*args: object, **kwargs: object) -> object:
        nonlocal changed
        result = original(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        if not changed:
            changed = True
            (project / "config/claude/CLAUDE.md").write_text("operator change\n")
        return result

    monkeypatch.setattr(sync_module, "_build_views", race)
    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.SOURCE_CHANGED,)
    assert _tree(project / "config") != before
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_snapshot_after_commit_finishes_frozen_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    changed = False

    def edit_source(count: int) -> None:
        nonlocal changed
        if count == 1 and not changed:
            changed = True
            (project / "config/claude/CLAUDE.md").write_text("later change\n")

    monkeypatch.setattr(publisher_module, "_after_target_mutation", edit_source)
    result = sync_config(project, config_path=config_path)

    assert result.success
    assert (project / "config/codex/AGENTS.md").read_text() == "Shared instructions.\n"
    assert (
        DriftClass.SOURCE_CHANGED
        in audit_config_sync(project, config_path=config_path).drift_classes
    )


def test_status_audit_uses_shared_canonical_lock(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    root = project / "config"
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_lock, args=(root, ready, release))
    holder.start()
    assert ready.wait(5)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(audit_config_sync, project, config_path=config_path)
            assert not future.done()
            release.set()
            audit = future.result(5)
    finally:
        release.set()
        holder.join(5)

    assert holder.exitcode == 0
    assert DriftClass.SOURCE_CHANGED in audit.drift_classes


def test_sync_uses_config_source_read_under_canonical_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    (project / "config/claude/CLAUDE.md").unlink()
    (project / "config/codex/AGENTS.md").write_text("Codex source.\n")
    original_lock = sync_module.canonical_lock
    source_switched = False

    @contextmanager
    def switch_source_after_lock(root: Path, *, exclusive: bool) -> Iterator[CanonicalLockLease]:
        nonlocal source_switched
        with original_lock(root, exclusive=exclusive) as lease:
            if not source_switched:
                source_switched = True
                save_config(
                    AppConfig(
                        code_dir=tmp_path / "code",
                        config_root=tmp_path / "runtime",
                        config_sync=ConfigSyncConfig(source="codex"),
                    ),
                    config_path,
                )
            yield lease

    monkeypatch.setattr(sync_module, "canonical_lock", switch_source_after_lock)
    result = sync_config(project, config_path=config_path)

    assert result.success
    assert result.audit.clean
    assert result.audit.configured_source == "codex"
    assert (project / "config/claude/CLAUDE.md").read_text() == "Codex source.\n"


def test_legacy_unowned_file_is_rejected_without_deleting_operator_file(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    operator_file = project / "config/codex/operator-private.txt"
    operator_file.write_bytes(b"operator-owned\n")
    legacy = _legacy_manifest(project)
    managed = _objects(legacy["managed"])
    codex = _objects(managed["codex"])
    files = _objects(codex["files"])
    files["operator-private.txt"] = _hash(operator_file)
    codex["files"] = files
    managed["codex"] = codex
    legacy["managed"] = managed
    manifest = project / "config" / MANIFEST_NAME
    manifest.write_text(json.dumps(legacy, sort_keys=True))
    before_manifest = manifest.read_bytes()

    result = sync_config(project, config_path=config_path)

    assert not result.success
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert manifest.read_bytes() == before_manifest
    assert operator_file.read_bytes() == b"operator-owned\n"


def test_legacy_manifest_migrates_atomically_and_removes_stale_item(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    stale = project / "config/opencode/context/stale.md"
    stale.parent.mkdir()
    stale.write_text("old\n")
    legacy = _legacy_manifest(project)
    managed = legacy["managed"]
    managed_data = _objects(managed)
    opencode = managed_data["opencode"]
    opencode_data = _objects(opencode)
    files = opencode_data["files"]
    file_data = _objects(files)
    file_data["context/stale.md"] = _hash(stale)
    opencode_data["files"] = file_data
    managed_data["opencode"] = opencode_data
    legacy["managed"] = managed_data
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_path.write_text(json.dumps(legacy))

    result = sync_config(project, config_path=config_path)

    assert result.success
    assert not stale.exists()
    assert set(json.loads(manifest_path.read_text())) == {"source", "items"}


def test_legacy_semantic_record_still_nonportable_is_not_migrated(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    agent = project / "config/claude/agents/reviewer.md"
    agent.parent.mkdir()
    agent.write_text("---\nname: reviewer\ndescription: Review\nmodel: native\n---\n\nReview.\n")
    output = project / "config/codex/agents/reviewer.toml"
    output.parent.mkdir()
    output.write_text(
        'name = "reviewer"\ndescription = "Review"\ndeveloper_instructions = "Review."\n'
    )
    legacy = _legacy_manifest(project, semantic=[_semantic_record(output)])
    _add_legacy_semantic_output(legacy, output)
    manifest = project / "config" / MANIFEST_NAME
    manifest.write_text(json.dumps(legacy, sort_keys=True))
    before = manifest.read_bytes()

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert manifest.read_bytes() == before


def test_legacy_semantic_record_made_portable_migrates(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    agent = project / "config/claude/agents/reviewer.md"
    agent.parent.mkdir()
    agent.write_text("---\nname: reviewer\ndescription: Review\n---\n\nReview.\n")
    assert sync_config(project, config_path=config_path).success
    manifest = project / "config" / MANIFEST_NAME
    output = project / "config/codex/agents/reviewer.toml"
    legacy = _legacy_manifest(project, semantic=[_semantic_record(output)])
    _add_legacy_semantic_output(legacy, output)
    manifest.write_text(json.dumps(legacy))

    result = sync_config(project, config_path=config_path)

    assert result.success
    assert set(json.loads(manifest.read_text())) == {"source", "items"}


def test_malformed_legacy_manifest_fails_closed(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    manifest = project / "config" / MANIFEST_NAME
    manifest.write_text('{"schema_version": 99}')
    before = _tree(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before


def test_switch_is_allowed_only_without_edited_managed_targets(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    source = project / "config/codex/AGENTS.md"
    source.write_text("Codex source.\n")
    save_config(
        AppConfig(
            code_dir=tmp_path / "code",
            config_root=tmp_path / "runtime",
            config_sync=ConfigSyncConfig(source="codex"),
        ),
        config_path,
    )

    assert sync_config(project, config_path=config_path).success
    assert (project / "config/claude/CLAUDE.md").read_text() == "Codex source.\n"
    (project / "config/opencode/AGENTS.md").write_text("operator edit\n")
    save_config(
        AppConfig(
            code_dir=tmp_path / "code",
            config_root=tmp_path / "runtime",
            config_sync=ConfigSyncConfig(source="claude"),
        ),
        config_path,
    )

    blocked = sync_config(project, config_path=config_path)

    assert blocked.success is False
    assert DriftClass.TARGET_DRIFT in blocked.audit.drift_classes


def test_canonical_delivery_view_returns_publisher_view(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success

    loaded = load_canonical_delivery_view(project, "codex", config_path=config_path)

    assert loaded.success and loaded.view is not None and loaded.revision is not None
    assert {item.relative_path for item in loaded.view.files} >= {
        PurePosixPath("AGENTS.md"),
        PurePosixPath("CLAUDE.md"),
    }


def _add_legacy_semantic_output(legacy: dict[str, object], output: Path) -> None:
    managed = _objects(legacy["managed"])
    codex = _objects(managed["codex"])
    files = _objects(codex["files"])
    files["agents/reviewer.toml"] = _hash(output)
    codex["files"] = files
    managed["codex"] = codex
    legacy["managed"] = managed


def _semantic_record(output: Path) -> dict[str, object]:
    return {
        "fingerprint": "0" * 64,
        "adapter_revision": 3,
        "source_tool": "claude",
        "target_tool": "codex",
        "artifact_id": "agent:reviewer:agents/reviewer.md",
        "source_path": "agents/reviewer.md",
        "files": [
            {
                "path": "agents/reviewer.toml",
                **_hash(output),
            }
        ],
        "fragments": [],
    }
