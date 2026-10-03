from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import stat
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
    RUNTIME_MANIFEST_NAME,
    PublishedFile,
    WorkflowView,
    publish_workflow_view,
)

_FORMS = ("canonical-lean", "runtime-state")
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
    (project / "config/claude/AGENTS.md").write_text("Shared instructions.\n")
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


def _runtime_view() -> WorkflowView:
    return WorkflowView(
        "claude",
        (PublishedFile(PurePosixPath("AGENTS.md"), b"Shared instructions.\n"),),
        target_tool="claude",
    )


def _duplicate_json_payload() -> bytes:
    return b'{"source":"claude","source":"claude","items":[]}'


def _corrupt(form: str, corruption: str, raw: bytes) -> bytes:
    if corruption == "duplicate-json-key":
        return _duplicate_json_payload()
    data = _objects(json.loads(raw))
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


def _runtime_audit(target: Path) -> DriftClass:
    try:
        publisher_module._load_manifest(
            target,
            PurePosixPath(RUNTIME_MANIFEST_NAME),
            canonical_target=False,
            target_tool="claude",
        )
    except publisher_module.PublishError as error:
        return error.drift_class
    return DriftClass.CLEAN


def _runtime_preflight(target: Path) -> DriftClass:
    try:
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
        manifest_path.write_bytes(_corrupt(form, corruption, manifest_path.read_bytes()))
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
    assert publish_workflow_view(
        view, canonical, target, target / RUNTIME_MANIFEST_NAME
    ).success
    manifest_path = target / RUNTIME_MANIFEST_NAME
    raw = manifest_path.read_bytes()
    manifest_path.write_bytes(_corrupt(form, corruption, raw))
    before = _tree(target)

    outcomes = (
        _runtime_audit(target),
        _runtime_preflight(target),
        publish_workflow_view(view, canonical, target, target / RUNTIME_MANIFEST_NAME).drift_class,
    )

    assert outcomes == (DriftClass.INVALID_OR_SEMANTIC,) * 3
    assert _tree(target) == before
