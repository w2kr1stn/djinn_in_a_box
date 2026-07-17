from __future__ import annotations

# pyright: reportPrivateUsage=false
import hashlib
import json
import stat
from collections.abc import Callable
from itertools import product
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

import djinn_in_a_box.core.workflow_publisher as publisher_module
from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig
from djinn_in_a_box.core.config_sync import (
    MANIFEST_NAME,
    DriftClass,
    audit_config_sync,
    load_canonical_delivery_view,
    sync_config,
)
from djinn_in_a_box.core.workflow_publisher import (
    LEGACY_DELIVERY_MANIFEST_NAME,
    RUNTIME_MANIFEST_NAME,
    PublishedFile,
    WorkflowView,
    publish_workflow_view,
)

_FORMS = ("canonical-lean", "runtime-state", "canonical-legacy", "runtime-legacy")
_CORRUPTIONS = (
    "duplicate-entry",
    "duplicate-json-key",
    "foreign-path",
    "foreign-carrier-key",
    "empty-semantic-record",
    "wrong-type",
    "extra-key",
    "unsafe-path",
)


def _objects(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _values(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def _tree(root: Path) -> dict[PurePosixPath, tuple[bytes, int]]:
    return {
        PurePosixPath(path.relative_to(root).as_posix()): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _workspace(root: Path) -> tuple[Path, Path]:
    project = root / "project"
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
    (project / "config/claude/CLAUDE.md").write_text("Shared instructions.\n")
    config_path = root / "operator.toml"
    code_dir = root / "code"
    code_dir.mkdir()
    save_config(
        AppConfig(
            code_dir=code_dir,
            config_root=root / "runtime",
            config_sync=ConfigSyncConfig(source="claude"),
        ),
        config_path,
    )
    return project, config_path


def _file_state(path: Path) -> dict[str, object]:
    return {
        "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        "executable": bool(path.stat().st_mode & stat.S_IXUSR),
    }


def _canonical_legacy(project: Path) -> dict[str, object]:
    config_root = project / "config"
    return {
        "schema_version": 1,
        "adapter_revision": 3,
        "active_source": "claude",
        "source_hash": "0" * 64,
        "source_files": {"CLAUDE.md": _file_state(config_root / "claude/CLAUDE.md")},
        "managed": {
            tool: {"files": {}, "native_only": {}, "fragments": []}
            for tool in ("claude", "codex", "opencode")
        },
        "semantic": [],
    }


def _runtime_view() -> WorkflowView:
    return WorkflowView(
        "claude",
        (PublishedFile(PurePosixPath("AGENTS.md"), b"Shared instructions.\n"),),
        target_tool="claude",
    )


def _runtime_legacy(target: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "tool": "claude",
        "files": {
            "AGENTS.md": {
                "content_hash": hashlib.sha256((target / "AGENTS.md").read_bytes()).hexdigest(),
                "executable": False,
            }
        },
        "fragments": [],
    }


def _duplicate_json_payload() -> bytes:
    return b'{"source":"claude","source":"claude","items":[]}'


def _empty_semantic_record() -> dict[str, object]:
    return {
        "fingerprint": "0" * 64,
        "adapter_revision": 3,
        "source_tool": "claude",
        "target_tool": "codex",
        "artifact_id": "agent:reviewer:agents/reviewer.md",
        "source_path": "agents/reviewer.md",
        "files": [],
        "fragments": [],
    }


def _fragment() -> dict[str, object]:
    return {
        "carrier_path": "settings.json",
        "key_path": ["hooks", "Stop"],
        "value_hash": hashlib.sha256(b'["ready"]').hexdigest(),
    }


def _corrupt(form: str, corruption: str, raw: bytes) -> bytes:
    if corruption == "duplicate-json-key":
        return _duplicate_json_payload()
    data = _objects(json.loads(raw))
    if form in {"canonical-lean", "runtime-state"}:
        items = _values(data["items"])
        item = _objects(items[0])
        if corruption == "duplicate-entry":
            items.append(dict(item))
        elif corruption == "foreign-path":
            item["path"] = (
                "codex/operator-private.txt"
                if form == "canonical-lean"
                else "operator-private.txt"
            )
        elif corruption == "foreign-carrier-key":
            items[0] = {
                "path": "claude/settings.json" if form == "canonical-lean" else "settings.json",
                "key_path": ["operator", "keep"],
                "content_hash": "0" * 64,
                "executable": False,
            }
        elif corruption == "empty-semantic-record":
            data["semantic"] = []
        elif corruption == "wrong-type":
            data["source"] = 1
        elif corruption == "extra-key":
            data["extra"] = True
        elif corruption == "unsafe-path":
            item["path"] = "../unsafe"
    elif form == "canonical-legacy":
        managed = _objects(data["managed"])
        claude = _objects(managed["claude"])
        source_files = _objects(data["source_files"])
        if corruption == "duplicate-entry":
            files = _objects(claude["files"])
            files["CLAUDE.md"] = dict(_objects(source_files["CLAUDE.md"]))
            claude["files"] = files
        elif corruption == "foreign-path":
            files = _objects(claude["files"])
            files["operator-private.txt"] = {"hash": "0" * 64, "executable": False}
            claude["files"] = files
        elif corruption == "foreign-carrier-key":
            fragments = _values(claude["fragments"])
            fragments.append(
                {
                    "carrier_path": "settings.json",
                    "key_path": ["operator", "keep"],
                    "value_hash": "0" * 64,
                }
            )
            claude["fragments"] = fragments
        elif corruption == "empty-semantic-record":
            data["semantic"] = [_empty_semantic_record()]
        elif corruption == "wrong-type":
            data["schema_version"] = "1"
        elif corruption == "extra-key":
            data["extra"] = True
        elif corruption == "unsafe-path":
            source_files["../unsafe"] = {"hash": "0" * 64, "executable": False}
    else:
        files = _objects(data["files"])
        if corruption == "duplicate-entry":
            fragments = _values(data["fragments"])
            fragments.extend((_fragment(), _fragment()))
            data["fragments"] = fragments
        elif corruption == "foreign-path":
            files["operator-private.txt"] = {"content_hash": "0" * 64, "executable": False}
        elif corruption == "foreign-carrier-key":
            fragments = _values(data["fragments"])
            fragments.append(
                {
                    "carrier_path": "settings.json",
                    "key_path": ["operator", "keep"],
                    "value_hash": "0" * 64,
                }
            )
            data["fragments"] = fragments
        elif corruption == "empty-semantic-record":
            data["semantic"] = []
        elif corruption == "wrong-type":
            data["tool"] = 1
        elif corruption == "extra-key":
            data["extra"] = True
        elif corruption == "unsafe-path":
            files["../unsafe"] = {"content_hash": "0" * 64, "executable": False}
    return json.dumps(data, sort_keys=True).encode()


def _canonical_outcomes(
    project: Path, config_path: Path
) -> tuple[DriftClass, DriftClass, DriftClass]:
    audit = audit_config_sync(project, config_path=config_path)
    published = sync_config(project, config_path=config_path)
    preflight = load_canonical_delivery_view(project, "codex", config_path=config_path)
    return (
        audit.drift_classes[0],
        published.audit.drift_classes[0],
        preflight.audit.drift_classes[0],
    )


def _runtime_audit(target: Path, *, legacy: bool) -> DriftClass:
    try:
        if legacy:
            publisher_module._load_legacy_delivery_manifest(
                target,
                PurePosixPath(LEGACY_DELIVERY_MANIFEST_NAME),
                target_tool="claude",
            )
        else:
            publisher_module._load_manifest(
                target,
                PurePosixPath(RUNTIME_MANIFEST_NAME),
                canonical_target=False,
                target_tool="claude",
            )
    except publisher_module.PublishError as error:
        return error.drift_class
    return DriftClass.CLEAN


def _runtime_preflight(target: Path, *, legacy: bool) -> DriftClass:
    try:
        if legacy:
            prior = publisher_module._load_legacy_delivery_manifest(
                target,
                PurePosixPath(LEGACY_DELIVERY_MANIFEST_NAME),
                target_tool="claude",
            )
            snapshot = None
        else:
            prior, snapshot = publisher_module._load_manifest(
                target,
                PurePosixPath(RUNTIME_MANIFEST_NAME),
                canonical_target=False,
                target_tool="claude",
            )
        publisher_module._preflight(
            target,
            publisher_module._validate_view(_runtime_view()),
            prior,
            snapshot,
            canonical_target=False,
            target_tool="claude",
        )
    except publisher_module.PublishError as error:
        return error.drift_class
    return DriftClass.CLEAN


@pytest.mark.parametrize(("form", "corruption"), product(_FORMS, _CORRUPTIONS))
def test_manifest_corruptions_fail_closed_consistently_and_preserve_the_tree(
    tmp_path: Path, form: str, corruption: str
) -> None:
    if form.startswith("canonical"):
        project, config_path = _workspace(tmp_path)
        config_root = project / "config"
        manifest_path = config_root / MANIFEST_NAME
        assert sync_config(project, config_path=config_path).success
        raw = (
            manifest_path.read_bytes()
            if form == "canonical-lean"
            else json.dumps(_canonical_legacy(project), sort_keys=True).encode()
        )
        manifest_path.write_bytes(_corrupt(form, corruption, raw))
        before = _tree(config_root)

        outcomes = _canonical_outcomes(project, config_path)

        assert outcomes == (DriftClass.INVALID_OR_SEMANTIC,) * 3
        assert _tree(config_root) == before
        return

    canonical = tmp_path / "canonical"
    target = tmp_path / "target"
    canonical.mkdir()
    target.mkdir()
    view = _runtime_view()
    legacy = form == "runtime-legacy"
    if legacy:
        (target / "AGENTS.md").write_bytes(b"Shared instructions.\n")
        (target / "settings.json").write_bytes(b'{"hooks":{"Stop":["ready"]}}\n')
        manifest_path = target / LEGACY_DELIVERY_MANIFEST_NAME
        raw = json.dumps(_runtime_legacy(target), sort_keys=True).encode()
    else:
        assert publish_workflow_view(
            view, canonical, target, target / RUNTIME_MANIFEST_NAME
        ).success
        manifest_path = target / RUNTIME_MANIFEST_NAME
        raw = manifest_path.read_bytes()
    manifest_path.write_bytes(_corrupt(form, corruption, raw))
    before = _tree(target)

    outcomes = (
        _runtime_audit(target, legacy=legacy),
        _runtime_preflight(target, legacy=legacy),
        publish_workflow_view(view, canonical, target, target / RUNTIME_MANIFEST_NAME).drift_class,
    )

    assert outcomes == (DriftClass.INVALID_OR_SEMANTIC,) * 3
    assert _tree(target) == before


def _migration_workspace(root: Path) -> tuple[Path, Path, Path]:
    project, config_path = _workspace(root)
    assert sync_config(project, config_path=config_path).success
    config_root = project / "config"
    stale = config_root / "opencode/context/stale.md"
    stale.parent.mkdir()
    stale.write_text("stale\n")
    legacy = _canonical_legacy(project)
    managed = _objects(legacy["managed"])
    opencode = _objects(managed["opencode"])
    files = _objects(opencode["files"])
    files["context/stale.md"] = _file_state(stale)
    opencode["files"] = files
    managed["opencode"] = opencode
    legacy["managed"] = managed
    (config_root / MANIFEST_NAME).write_text(json.dumps(legacy, sort_keys=True))
    return project, config_path, stale


def _runtime_adoption_workspace(root: Path) -> tuple[Path, Path, WorkflowView]:
    canonical = root / "canonical"
    target = root / "target"
    canonical.mkdir(parents=True)
    target.mkdir()
    (target / "AGENTS.md").write_bytes(b"Shared instructions.\n")
    (target / LEGACY_DELIVERY_MANIFEST_NAME).write_text(
        json.dumps(_runtime_legacy(target), sort_keys=True)
    )
    return canonical, target, _runtime_view()


def _mutation_count(run: Callable[[], object], monkeypatch: pytest.MonkeyPatch) -> int:
    mutations: list[int] = []
    monkeypatch.setattr(publisher_module, "_after_target_mutation", mutations.append)
    run()
    return max(mutations)


def _no_mutation_crash(_count: int) -> None:
    return None


def test_canonical_migration_crash_matrix_converges_after_every_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe, probe_config, _probe_stale = _migration_workspace(tmp_path / "probe")
    steps = _mutation_count(lambda: sync_config(probe, config_path=probe_config), monkeypatch)
    assert steps > 0

    for step in range(1, steps + 1):
        project, config_path, stale = _migration_workspace(tmp_path / f"step-{step}")

        def crash_after_step(count: int, *, wanted: int = step) -> None:
            if count == wanted:
                raise RuntimeError("injected canonical migration crash")

        monkeypatch.setattr(publisher_module, "_after_target_mutation", crash_after_step)
        with pytest.raises(RuntimeError, match="injected canonical migration crash"):
            sync_config(project, config_path=config_path)
        monkeypatch.setattr(publisher_module, "_after_target_mutation", _no_mutation_crash)

        retry = sync_config(project, config_path=config_path)

        assert retry.success
        assert not stale.exists()
        assert audit_config_sync(project, config_path=config_path).clean


def test_runtime_adoption_crash_matrix_converges_after_every_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_canonical, probe_target, probe_view = _runtime_adoption_workspace(tmp_path / "probe")
    steps = _mutation_count(
        lambda: publish_workflow_view(
            probe_view,
            probe_canonical,
            probe_target,
            probe_target / RUNTIME_MANIFEST_NAME,
        ),
        monkeypatch,
    )
    assert steps > 0

    for step in range(1, steps + 1):
        canonical, target, view = _runtime_adoption_workspace(tmp_path / f"step-{step}")

        def crash_after_step(count: int, *, wanted: int = step) -> None:
            if count == wanted:
                raise RuntimeError("injected runtime adoption crash")

        monkeypatch.setattr(publisher_module, "_after_target_mutation", crash_after_step)
        with pytest.raises(RuntimeError, match="injected runtime adoption crash"):
            publish_workflow_view(view, canonical, target, target / RUNTIME_MANIFEST_NAME)
        monkeypatch.setattr(publisher_module, "_after_target_mutation", _no_mutation_crash)

        retry = publish_workflow_view(view, canonical, target, target / RUNTIME_MANIFEST_NAME)

        assert retry.success
        assert (target / RUNTIME_MANIFEST_NAME).is_file()
        assert not (target / LEGACY_DELIVERY_MANIFEST_NAME).exists()
