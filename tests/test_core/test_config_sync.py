from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import stat
from collections.abc import Callable, Iterator, Mapping
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
from djinn_in_a_box.core.workflow_publisher import (
    CanonicalLockLease,
    canonical_lock,
    decode_lean_manifest,
)

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


@pytest.mark.parametrize("source", ("codex", "opencode"))
def test_non_claude_source_adopts_declared_zero_byte_claude_companion(
    tmp_path: Path, source: ConfigSyncSource
) -> None:
    project, config_path = _workspace(tmp_path, source=source)
    companion = project / "config/claude/AGENTS.md"
    companion.write_bytes(b"")

    result = sync_config(project, config_path=config_path)

    assert result.success
    assert companion.read_text() == "Shared instructions.\n"
    assert audit_config_sync(project, config_path=config_path).clean


def test_nonempty_claude_companion_blocks_non_claude_source_without_mutation(
    tmp_path: Path,
) -> None:
    project, config_path = _workspace(tmp_path, source="codex")
    companion = project / "config/claude/AGENTS.md"
    companion.write_text("operator content\n")
    before = _tree(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.COLLISION,)
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


@pytest.mark.parametrize("foreign_kind", ("file", "fragment"))
def test_lean_manifest_rejects_unowned_items_without_mutation(
    tmp_path: Path, foreign_kind: str
) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    config_root = project / "config"
    manifest_path = config_root / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    if foreign_kind == "file":
        operator_file = config_root / "codex/operator-private.txt"
        operator_file.write_bytes(b"operator-owned\n")
        manifest["items"].append(
            {
                "path": "codex/operator-private.txt",
                "content_hash": hashlib.sha256(operator_file.read_bytes()).hexdigest(),
                "executable": False,
            }
        )
    else:
        carrier = config_root / "claude/settings.json"
        carrier.write_bytes(b'{"operator":{"keep":true}}\n')
        manifest["items"].append(
            {
                "path": "claude/settings.json",
                "key_path": ["operator", "keep"],
                "content_hash": hashlib.sha256(b"true").hexdigest(),
                "executable": False,
            }
        )
    manifest_path.write_text(json.dumps(manifest))
    before = _tree(config_root)

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(config_root) == before


def test_legacy_unowned_carrier_key_is_rejected_without_mutation(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    config_root = project / "config"
    carrier = config_root / "claude/settings.json"
    carrier.write_bytes(b'{"operator":{"keep":true}}\n')
    legacy = _legacy_manifest(project)
    managed = _objects(legacy["managed"])
    claude = _objects(managed["claude"])
    fragments = cast(list[object], claude["fragments"])
    fragments.append(
        {
            "carrier_path": "settings.json",
            "key_path": ["operator", "keep"],
            "value_hash": hashlib.sha256(b"true").hexdigest(),
        }
    )
    claude["fragments"] = fragments
    managed["claude"] = claude
    legacy["managed"] = managed
    manifest_path = config_root / MANIFEST_NAME
    manifest_path.write_text(json.dumps(legacy, sort_keys=True))
    before = _tree(config_root)

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(config_root) == before


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


def test_legacy_migration_releases_edited_native_hook_records_without_deleting_them(
    tmp_path: Path,
) -> None:
    project, config_path = _workspace(tmp_path)
    codex_root = project / "config" / "codex"
    hook = codex_root / "hooks/security_guard.py"
    hook.parent.mkdir()
    hook.write_bytes(b"#!/usr/bin/env python3\nprint('operator native edit')\n")
    registration = [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "python hooks/security_guard.py"}],
        }
    ]
    hooks_path = codex_root / "hooks.json"
    hooks_path.write_bytes(
        json.dumps({"hooks": {"PreToolUse": registration}}, sort_keys=True).encode()
    )
    legacy = _legacy_manifest(project)
    managed = _objects(legacy["managed"])
    codex = _objects(managed["codex"])
    native_only = _objects(codex["native_only"])
    native_only["hooks/security_guard.py"] = {
        "hash": hashlib.sha256(b"#!/usr/bin/env python3\nprint('legacy')\n").hexdigest(),
        "executable": False,
    }
    codex["native_only"] = native_only
    fragments = cast(list[object], codex["fragments"])
    fragments.append(
        {
            "carrier_path": "hooks.json",
            "key_path": ["hooks", "PreToolUse"],
            "value_hash": hashlib.sha256(
                json.dumps(registration, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    )
    codex["fragments"] = fragments
    managed["codex"] = codex
    legacy["managed"] = managed
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_path.write_text(json.dumps(legacy, sort_keys=True))
    before_hook = hook.read_bytes()
    before_hooks = hooks_path.read_bytes()

    result = sync_config(project, config_path=config_path)
    strict = decode_lean_manifest(
        manifest_path.read_bytes(), canonical_target=True, target_tool=None
    )

    assert result.success
    assert hook.read_bytes() == before_hook
    assert hooks_path.read_bytes() == before_hooks
    assert PurePosixPath("codex/hooks/security_guard.py") not in {
        item.path for item in strict.items
    }
    assert (
        PurePosixPath("codex/hooks.json"),
        ("hooks", "PreToolUse"),
    ) not in {(item.path, item.key_path) for item in strict.items if item.key_path is not None}
    assert audit_config_sync(project, config_path=config_path).clean


def test_nonselected_native_carrier_duplicate_key_blocks_without_mutation(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    carrier = project / "config/codex/hooks.json"
    carrier.write_bytes(b'{"hooks":{"Stop":[]},"hooks":{"Stop":[]}}')
    before = _tree(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before


def test_legacy_migration_retry_accepts_missing_stale_file_after_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    stale = project / "config/opencode/context/stale.md"
    stale.parent.mkdir()
    stale.write_text("old\n")
    legacy = _legacy_manifest(project)
    managed = _objects(legacy["managed"])
    opencode = _objects(managed["opencode"])
    files = _objects(opencode["files"])
    files["context/stale.md"] = _hash(stale)
    opencode["files"] = files
    managed["opencode"] = opencode
    legacy["managed"] = managed
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_path.write_text(json.dumps(legacy))

    def crash_after_stale_removal(count: int) -> None:
        if count == 1:
            raise RuntimeError("injected migration crash")

    monkeypatch.setattr(publisher_module, "_after_target_mutation", crash_after_stale_removal)
    with pytest.raises(RuntimeError, match="injected migration crash"):
        sync_config(project, config_path=config_path)

    def no_crash(_count: int) -> None:
        return None

    monkeypatch.setattr(publisher_module, "_after_target_mutation", no_crash)

    assert not stale.exists()
    assert sync_config(project, config_path=config_path).success
    assert audit_config_sync(project, config_path=config_path).clean


def test_legacy_migration_rejects_missing_selected_source_record(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    agent = project / "config/claude/agents/reviewer.md"
    agent.parent.mkdir()
    agent.write_text("---\nname: reviewer\ndescription: Review\n---\n\nReview.\n")
    legacy = _legacy_manifest(project)
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_path.write_bytes(json.dumps(legacy, sort_keys=True).encode())
    agent.unlink()
    before_manifest = manifest_path.read_bytes()

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert manifest_path.read_bytes() == before_manifest


def test_legacy_migration_source_change_keeps_legacy_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_path.write_text(json.dumps(_legacy_manifest(project), sort_keys=True))
    before_manifest = manifest_path.read_bytes()
    original = sync_module._migrate_legacy  # pyright: ignore[reportPrivateUsage]

    def migrate_then_edit(
        config_root: Path,
        legacy: Mapping[str, object],
        selected_source: ConfigSyncSource,
        desired: sync_module.WorkflowView,
    ) -> bytes:
        preflight = original(config_root, legacy, selected_source, desired)
        (project / "config/claude/CLAUDE.md").write_text("operator edit\n")
        return preflight

    monkeypatch.setattr(sync_module, "_migrate_legacy", migrate_then_edit)
    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.SOURCE_CHANGED,)
    assert manifest_path.read_bytes() == before_manifest


def test_legacy_migration_switch_releases_the_new_source_records(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    claude_agent = project / "config/claude/agents/reviewer.md"
    claude_agent.parent.mkdir()
    claude_agent.write_text("---\nname: reviewer\ndescription: Review\n---\n\nReview.\n")
    assert sync_config(project, config_path=config_path).success
    codex_agent = project / "config/codex/agents/reviewer.toml"
    expected_agent = codex_agent.read_bytes()
    legacy = _legacy_manifest(project)
    managed = _objects(legacy["managed"])
    codex = _objects(managed["codex"])
    files = _objects(codex["files"])
    files["agents/reviewer.toml"] = _hash(codex_agent)
    codex["files"] = files
    managed["codex"] = codex
    legacy["managed"] = managed
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_path.write_text(json.dumps(legacy, sort_keys=True))
    save_config(
        AppConfig(
            code_dir=tmp_path / "code",
            config_root=tmp_path / "runtime",
            config_sync=ConfigSyncConfig(source="codex"),
        ),
        config_path,
    )

    result = sync_config(project, config_path=config_path)

    assert result.success
    assert codex_agent.read_bytes() == expected_agent
    assert audit_config_sync(project, config_path=config_path).clean


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fingerprint", "not-a-fingerprint"),
        ("artifact_id", "unknown:reviewer:agents/reviewer.md"),
        ("source_path", "../agents/reviewer.md"),
        ("target_tool", "claude"),
        ("target_tool", "gemini"),
    ],
)
def test_legacy_semantic_record_fields_are_validated_before_migration(
    tmp_path: Path, field: str, value: object
) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    output = project / "config/codex/agents/reviewer.toml"
    output.parent.mkdir(exist_ok=True)
    output.write_text(
        'name = "reviewer"\ndescription = "Review"\ndeveloper_instructions = "Review."\n'
    )
    record = _semantic_record(output)
    record[field] = value
    legacy = _legacy_manifest(project, semantic=[record])
    _add_legacy_semantic_output(legacy, output)
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_path.write_text(json.dumps(legacy, sort_keys=True))
    before = _tree(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before


def test_malformed_legacy_manifest_fails_closed(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    manifest = project / "config" / MANIFEST_NAME
    manifest.write_text('{"schema_version": 99}')
    before = _tree(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 999), ("adapter_revision", 999), ("source_hash", "invalid")],
)
def test_legacy_manifest_metadata_is_validated_before_migration(
    tmp_path: Path, field: str, value: object
) -> None:
    project, config_path = _workspace(tmp_path)
    legacy = _legacy_manifest(project)
    legacy[field] = value
    manifest_path = project / "config" / MANIFEST_NAME
    manifest_path.write_text(json.dumps(legacy, sort_keys=True))
    before = _tree(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before


def test_missing_managed_canonical_file_is_target_drift_without_reconstruction(
    tmp_path: Path,
) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    managed = project / "config/codex/AGENTS.md"
    managed.unlink()
    before = _tree(project / "config")

    audit = audit_config_sync(project, config_path=config_path)
    result = sync_config(project, config_path=config_path)

    assert audit.drift_classes == (DriftClass.TARGET_DRIFT,)
    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.TARGET_DRIFT,)
    assert _tree(project / "config") == before


def test_switch_is_allowed_only_without_edited_managed_targets(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    source = project / "config/codex/AGENTS.md"
    source.write_text("Codex source.\n")
    (project / "config/claude/CLAUDE.md").write_text("Codex source.\n")
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


def test_source_switch_target_drift_keeps_manifest_byte_identical(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success
    (project / "config/codex/AGENTS.md").write_text("Codex source.\n")
    (project / "config/claude/CLAUDE.md").write_text("Codex source.\n")
    (project / "config/opencode/AGENTS.md").write_text("operator edit\n")
    save_config(
        AppConfig(
            code_dir=tmp_path / "code",
            config_root=tmp_path / "runtime",
            config_sync=ConfigSyncConfig(source="codex"),
        ),
        config_path,
    )
    manifest_path = project / "config" / MANIFEST_NAME
    before_manifest = manifest_path.read_bytes()

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.TARGET_DRIFT,)
    assert manifest_path.read_bytes() == before_manifest


def test_source_switch_rejects_untracked_old_source_content(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    reviewer = project / "config/claude/agents/reviewer.md"
    reviewer.parent.mkdir()
    reviewer.write_text("---\nname: reviewer\ndescription: Review\n---\n\nOriginal review.\n")
    assert sync_config(project, config_path=config_path).success
    before_reviewer = reviewer.read_bytes()
    (project / "config/codex/AGENTS.md").write_text("Codex source.\n")
    codex_reviewer = project / "config/codex/agents/reviewer.toml"
    codex_reviewer.parent.mkdir(exist_ok=True)
    codex_reviewer.write_text(
        'name = "reviewer"\ndescription = "Review"\n'
        'developer_instructions = "Replacement review."\n'
    )
    (project / "config/claude/CLAUDE.md").write_text("Codex source.\n")
    save_config(
        AppConfig(
            code_dir=tmp_path / "code",
            config_root=tmp_path / "runtime",
            config_sync=ConfigSyncConfig(source="codex"),
        ),
        config_path,
    )
    manifest_path = project / "config" / MANIFEST_NAME
    before_manifest = manifest_path.read_bytes()

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.COLLISION,)
    assert reviewer.read_bytes() == before_reviewer
    assert manifest_path.read_bytes() == before_manifest


def test_canonical_delivery_view_returns_publisher_view(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path)
    assert sync_config(project, config_path=config_path).success

    loaded = load_canonical_delivery_view(project, "codex", config_path=config_path)

    assert loaded.success and loaded.view is not None and loaded.revision is not None
    assert {item.relative_path for item in loaded.view.files} >= {
        PurePosixPath("AGENTS.md"),
        PurePosixPath("CLAUDE.md"),
    }


def test_canonical_delivery_view_blocks_nonportable_source_added_after_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path)
    agent = project / "config/claude/agents/reviewer.md"
    agent.parent.mkdir()
    agent.write_text("---\nname: reviewer\ndescription: Review\n---\n\nReview.\n")
    assert sync_config(project, config_path=config_path).success
    original_audit = sync_module._audit_locked  # pyright: ignore[reportPrivateUsage]

    def audit_then_make_source_nonportable(
        project_root: Path, source: ConfigSyncSource
    ) -> sync_module.ConfigSyncAudit:
        audit = original_audit(project_root, source)
        agent.write_text(
            "---\nname: reviewer\ndescription: Review\nmodel: native\n---\n\nReview.\n"
        )
        return audit

    monkeypatch.setattr(sync_module, "_audit_locked", audit_then_make_source_nonportable)

    loaded = load_canonical_delivery_view(project, "codex", config_path=config_path)

    assert loaded.success is False
    assert loaded.view is None
    assert loaded.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)


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
