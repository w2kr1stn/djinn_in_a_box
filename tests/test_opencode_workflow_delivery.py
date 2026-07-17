from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Protocol

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "opencode-workflow-delivery.py"
MANIFEST = ".djinn-workflow-delivery.json"


class _StageLike(Protocol):
    name: str


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "seed"
    destination = tmp_path / "runtime"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    return source, destination


def _run(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source",
            str(source),
            "--destination",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _copy_settings(
    source: Path, destination: Path, *, missing_ok: bool = False
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--copy-settings",
        str(source),
        str(destination),
    ]
    if missing_ok:
        command.append("--missing-ok")
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _write(path: Path, content: bytes = b"content\n", *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("opencode_workflow_delivery", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_exact_allowlist_is_delivered_and_all_unmanaged_neighbors_are_preserved(
    tmp_path: Path,
) -> None:
    source, destination = _roots(tmp_path)
    managed = {
        "AGENTS.md": b"agents\n",
        "CLAUDE.md": b"companion\n",
        "agents/reviewer.md": b"reviewer\n",
        "skills/check/SKILL.md": b"skill\n",
        "skills/check/references/notes.md": b"notes\n",
        "commands/build.md": b"command\n",
        "context/git.md": b"context\n",
        "scripts/status.py": b"print('ok')\n",
        "plugins/session-start-status.js": b"startup\n",
        "plugins/security-reminder.js": b"security\n",
        "plugins/ready-notify.js": b"ready\n",
    }
    for relative, content in managed.items():
        _write(source / relative, content, executable=relative == "scripts/status.py")

    for relative in (
        ".gitkeep",
        "package.json",
        "settings.json",
        ".opencode.json",
        ".seed-manifest",
        "plugins/custom.js",
        "other/private.txt",
    ):
        _write(source / relative, b"canonical-neighbor\n")

    runtime_neighbors = {
        ".opencode.json": b'{"personal":true}\n',
        ".seed-manifest": b"legacy-owned-nothing\n",
        "package.json": b'{"private":true}\n',
        "plugins/custom.js": b"custom-runtime-plugin\n",
        "private/notes.txt": b"keep-runtime-neighbor\n",
    }
    for relative, content in runtime_neighbors.items():
        _write(destination / relative, content)

    result = _run(source, destination)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    for relative, content in managed.items():
        assert (destination / relative).read_bytes() == content
    assert (destination / "scripts/status.py").stat().st_mode & stat.S_IXUSR
    for relative, content in runtime_neighbors.items():
        assert (destination / relative).read_bytes() == content
    assert not (destination / ".gitkeep").exists()
    manifest = json.loads((destination / MANIFEST).read_bytes())
    assert set(manifest["files"]) == set(managed)
    assert manifest["fragments"] == []


def test_optional_companion_may_be_missing_but_agents_is_required(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"agents\n")

    accepted = _run(source, destination)
    assert accepted.returncode == 0, accepted.stderr
    assert not (destination / "CLAUDE.md").exists()

    (source / "AGENTS.md").unlink()
    rejected = _run(source, destination)
    assert rejected.returncode != 0
    assert "source-agents-missing" in rejected.stderr


def test_first_adoption_requires_exact_bytes_and_mode(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"canonical\n")
    _write(destination / "AGENTS.md", b"canonical\n")
    before = (destination / "AGENTS.md").stat().st_mtime_ns

    adopted = _run(source, destination)
    assert adopted.returncode == 0, adopted.stderr
    assert (destination / "AGENTS.md").stat().st_mtime_ns == before

    other_source, other_destination = _roots(tmp_path / "collision")
    _write(other_source / "AGENTS.md", b"canonical\n")
    _write(other_destination / "AGENTS.md", b"private\n")
    collision = _run(other_source, other_destination)
    assert collision.returncode != 0
    assert (other_destination / "AGENTS.md").read_bytes() == b"private\n"
    assert not (other_destination / MANIFEST).exists()


def test_update_and_manifest_owned_stale_removal_are_drift_safe(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"v1\n")
    _write(source / "agents/reviewer.md", b"reviewer\n")
    assert _run(source, destination).returncode == 0

    _write(source / "AGENTS.md", b"v2\n")
    (source / "agents/reviewer.md").unlink()
    _write(destination / "agents/reviewer.md", b"operator-edit\n")
    blocked = _run(source, destination)
    assert blocked.returncode != 0
    assert (destination / "AGENTS.md").read_bytes() == b"v1\n"
    assert (destination / "agents/reviewer.md").read_bytes() == b"operator-edit\n"

    _write(destination / "agents/reviewer.md", b"reviewer\n")
    updated = _run(source, destination)
    assert updated.returncode == 0, updated.stderr
    assert (destination / "AGENTS.md").read_bytes() == b"v2\n"
    assert not (destination / "agents/reviewer.md").exists()


def test_managed_content_and_executable_drift_block_delivery(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"agents\n")
    _write(source / "scripts/status.py", b"print('ok')\n", executable=True)
    assert _run(source, destination).returncode == 0

    script = destination / "scripts/status.py"
    script.chmod(script.stat().st_mode & ~stat.S_IXUSR)
    mode_drift = _run(source, destination)
    assert mode_drift.returncode != 0
    assert "managed-file-drift" in mode_drift.stderr

    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (destination / "AGENTS.md").write_bytes(b"private\n")
    content_drift = _run(source, destination)
    assert content_drift.returncode != 0
    assert (destination / "AGENTS.md").read_bytes() == b"private\n"


@pytest.mark.parametrize("kind", ["root", "target", "parent", "wrong-type"])
def test_destination_symlinks_and_wrong_types_block_before_mutation(
    tmp_path: Path, kind: str
) -> None:
    source = tmp_path / "seed"
    source.mkdir()
    _write(source / "AGENTS.md", b"agents\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "runtime"
    if kind == "root":
        destination.symlink_to(outside, target_is_directory=True)
    else:
        destination.mkdir()
        if kind == "target":
            (destination / "AGENTS.md").symlink_to(outside / "escaped")
        elif kind == "wrong-type":
            (destination / "AGENTS.md").mkdir()
        else:
            _write(source / "scripts/status.py", b"print('ok')\n")
            (destination / "scripts").symlink_to(outside, target_is_directory=True)

    result = _run(source, destination)

    assert result.returncode != 0
    assert not (outside / "escaped").exists()
    assert not (outside / "status.py").exists()
    assert not (destination / MANIFEST).exists()


def test_internal_source_file_symlink_is_dereferenced(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "context/shared.md", b"shared instructions\n")
    (source / "AGENTS.md").symlink_to("context/shared.md")

    result = _run(source, destination)

    assert result.returncode == 0, result.stderr
    assert not (destination / "AGENTS.md").is_symlink()
    assert (destination / "AGENTS.md").read_bytes() == b"shared instructions\n"


def test_source_file_swap_to_external_symlink_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"canonical\n")
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"external-sentinel\n")
    module = _load_module()
    original = module._read_regular_source_entry
    swapped = False

    def swapping_read(
        parent_fd: int,
        name: str,
        expected: object,
        tracked_entries: list[object],
        tracked_path: PurePosixPath,
    ) -> object:
        nonlocal swapped
        if name == "AGENTS.md" and not swapped:
            (source / "AGENTS.md").unlink()
            (source / "AGENTS.md").symlink_to(outside)
            swapped = True
        return original(parent_fd, name, expected, tracked_entries, tracked_path)

    monkeypatch.setattr(module, "_read_regular_source_entry", swapping_read)
    with pytest.raises(module._DeliveryError) as failure:
        module.deliver(source, destination)

    assert swapped
    assert failure.value.retryable
    assert not (destination / "AGENTS.md").exists()
    assert outside.read_bytes() == b"external-sentinel\n"


def test_owned_file_in_place_mutation_after_read_aborts_before_destination_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"first-generation\n")
    _write(destination / ".opencode.json", b'{"neighbor":true}\n')
    module = _load_module()
    original = module._read_owned_source_file
    mutated = False

    def mutating_read(
        root_fd: int,
        source_real: Path,
        path: PurePosixPath,
        entry_parent_fd: int,
        tracked_entries: list[object],
    ) -> object:
        nonlocal mutated
        result = original(root_fd, source_real, path, entry_parent_fd, tracked_entries)
        if path == PurePosixPath("AGENTS.md") and not mutated:
            (source / "AGENTS.md").write_bytes(b"second-generation\n")
            mutated = True
        return result

    monkeypatch.setattr(module, "_read_owned_source_file", mutating_read)
    with pytest.raises(module._DeliveryError) as failure:
        module.deliver(source, destination)

    assert mutated
    assert failure.value.retryable
    assert list(destination.iterdir()) == [destination / ".opencode.json"]
    assert (destination / ".opencode.json").read_bytes() == b'{"neighbor":true}\n'


def test_internal_symlink_target_in_place_mutation_after_read_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "context/shared.md", b"first-generation\n")
    (source / "AGENTS.md").symlink_to("context/shared.md")
    module = _load_module()
    original = module._read_owned_source_file
    mutated = False

    def mutating_read(
        root_fd: int,
        source_real: Path,
        path: PurePosixPath,
        entry_parent_fd: int,
        tracked_entries: list[object],
    ) -> object:
        nonlocal mutated
        result = original(root_fd, source_real, path, entry_parent_fd, tracked_entries)
        if path == PurePosixPath("AGENTS.md") and not mutated:
            (source / "context/shared.md").write_bytes(b"second-generation\n")
            mutated = True
        return result

    monkeypatch.setattr(module, "_read_owned_source_file", mutating_read)
    with pytest.raises(module._DeliveryError) as failure:
        module.deliver(source, destination)

    assert mutated
    assert failure.value.retryable
    assert list(destination.iterdir()) == []


def test_source_root_swap_during_discovery_cannot_publish_false_stale_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"canonical\n")
    _write(source / "context/keep.md", b"must-remain-managed\n")
    assert _run(source, destination).returncode == 0
    before = {
        path.relative_to(destination): (
            path.read_bytes(),
            bool(path.stat().st_mode & stat.S_IXUSR),
        )
        for path in destination.rglob("*")
        if path.is_file()
    }

    module = _load_module()
    original_fwalk = module.os.fwalk
    swapped = False

    def swapping_fwalk(*args: object, **kwargs: object) -> object:
        nonlocal swapped
        if not swapped:
            source.rename(tmp_path / "original-seed")
            source.mkdir()
            _write(source / "AGENTS.md", b"replacement-root\n")
            swapped = True
        yield from original_fwalk(*args, **kwargs)

    monkeypatch.setattr(module.os, "fwalk", swapping_fwalk)
    with pytest.raises(module._DeliveryError) as failure:
        module.deliver(source, destination)

    assert swapped
    assert failure.value.retryable
    after = {
        path.relative_to(destination): (
            path.read_bytes(),
            bool(path.stat().st_mode & stat.S_IXUSR),
        )
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert (destination / "context/keep.md").read_bytes() == b"must-remain-managed\n"


def test_source_subtree_swap_and_restore_aborts_before_destination_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"canonical\n")
    _write(source / "context/keep.md", b"old-generation\n")
    assert _run(source, destination).returncode == 0
    before = {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }

    module = _load_module()
    original = module._read_owned_source_file
    swapped = False

    def swapping_read(
        root_fd: int,
        source_real: Path,
        path: PurePosixPath,
        entry_parent_fd: int,
        tracked_entries: list[object],
    ) -> object:
        nonlocal swapped
        if path == PurePosixPath("context/keep.md") and not swapped:
            backup = tmp_path / "old-context"
            (source / "context").rename(backup)
            _write(source / "context/keep.md", b"new-generation\n")
            result = original(root_fd, source_real, path, entry_parent_fd, tracked_entries)
            shutil.rmtree(source / "context")
            backup.rename(source / "context")
            swapped = True
            return result
        return original(root_fd, source_real, path, entry_parent_fd, tracked_entries)

    monkeypatch.setattr(module, "_read_owned_source_file", swapping_read)
    with pytest.raises(module._DeliveryError) as failure:
        module.deliver(source, destination)

    assert swapped
    assert failure.value.retryable
    after = {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert (destination / "context/keep.md").read_bytes() == b"old-generation\n"


def test_retryable_source_race_output_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    sentinel = "SECRET-SOURCE-BODY"

    def racing_delivery(source: Path, destination: Path) -> None:
        del source, destination
        raise module._DeliveryError("source-file-race", retryable=True)

    monkeypatch.setattr(module, "deliver", racing_delivery)
    result = module.main(
        [
            "--source",
            str(tmp_path / sentinel),
            "--destination",
            str(tmp_path / "runtime"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "opencode workflow delivery failed: source-file-race\n"
    assert sentinel not in captured.err


@pytest.mark.parametrize("kind", ["external-file", "dangling-file", "directory"])
def test_unsafe_allowlisted_source_symlinks_are_blocked(tmp_path: Path, kind: str) -> None:
    source, destination = _roots(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "directory":
        _write(source / "AGENTS.md", b"agents\n")
        (source / "context").symlink_to(outside, target_is_directory=True)
    elif kind == "external-file":
        _write(outside / "instructions.md", b"outside\n")
        (source / "AGENTS.md").symlink_to(outside / "instructions.md")
    else:
        (source / "AGENTS.md").symlink_to(source / "missing.md")

    result = _run(source, destination)

    assert result.returncode != 0
    assert not (destination / MANIFEST).exists()


@pytest.mark.parametrize("schema", [True, 2, "1"])
def test_manifest_schema_is_strict_and_bool_is_rejected(tmp_path: Path, schema: object) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"agents\n")
    (destination / MANIFEST).write_text(
        json.dumps({"schema_version": schema, "tool": "opencode", "files": {}, "fragments": []})
    )

    result = _run(source, destination)

    assert result.returncode != 0
    assert "manifest-malformed" in result.stderr
    assert not (destination / "AGENTS.md").exists()


def test_syntactically_malformed_manifest_blocks_without_adoption(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"agents\n")
    (destination / MANIFEST).write_bytes(b"not-json")

    result = _run(source, destination)

    assert result.returncode != 0
    assert "manifest-malformed" in result.stderr
    assert not (destination / "AGENTS.md").exists()


def test_stage_tamper_never_publishes_bad_bytes_and_clean_retry_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"canonical\n")
    module = _load_module()
    original = module._stage_file
    tampered = False

    def tampering_stage(
        root_fd: int,
        stage: object,
        category: str,
        path: PurePosixPath,
        source_file: object,
    ) -> object:
        nonlocal tampered
        staged = original(root_fd, stage, category, path, source_file)
        if category == "files" and not tampered:
            with module._stage_parent_fd(  # pyright: ignore[reportPrivateUsage]
                stage, staged.relative_path, create=False
            ) as parent_fd:
                descriptor = os.open(
                    staged.relative_path.name,
                    os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                try:
                    os.write(descriptor, b"stage-attacker-sentinel")
                finally:
                    os.close(descriptor)
            tampered = True
        return staged

    monkeypatch.setattr(module, "_stage_file", tampering_stage)
    with pytest.raises(module._DeliveryError):
        module.deliver(source, destination)
    assert tampered
    assert not (destination / "AGENTS.md").exists()
    assert not (destination / MANIFEST).exists()

    monkeypatch.setattr(module, "_stage_file", original)
    module.deliver(source, destination)
    assert (destination / "AGENTS.md").read_bytes() == b"canonical\n"
    assert (destination / MANIFEST).is_file()


@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_stage_root_replacement_cannot_escape_or_delete_unmanaged_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
    timing: str,
) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"agents\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_marker = outside / "keep.txt"
    outside_marker.write_text("unchanged")
    module = _load_module()
    original = module._stage_file
    attacked = False
    parked: Path | None = None
    replacement_path: Path | None = None

    def attack(stage: _StageLike) -> None:
        nonlocal attacked, parked, replacement_path
        stage_path = destination / stage.name
        parked = destination / f"{stage.name}-parked"
        stage_path.rename(parked)
        replacement_path = stage_path
        if replacement == "symlink":
            stage_path.symlink_to(outside, target_is_directory=True)
        else:
            stage_path.mkdir()
            (stage_path / "unmanaged.txt").write_text("retain")
        attacked = True

    def replacing_stage(
        root_fd: int,
        stage: _StageLike,
        category: str,
        path: PurePosixPath,
        source_file: object,
    ) -> object:
        if timing == "before" and not attacked:
            attack(stage)
        staged = original(root_fd, stage, category, path, source_file)
        if timing == "after" and not attacked:
            attack(stage)
        return staged

    monkeypatch.setattr(module, "_stage_file", replacing_stage)

    with pytest.raises(module._DeliveryError) as failure:
        module.deliver(source, destination)

    assert failure.value.retryable is True
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
    assert not (destination / "AGENTS.md").exists()
    assert not (destination / MANIFEST).exists()


def test_new_unmanaged_file_appearing_with_target_parent_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    target = PurePosixPath("skills/demo/SKILL.md")
    _write(source / "AGENTS.md", b"agents\n")
    _write(source / Path(*target.parts), b"managed\n")
    module = _load_module()
    original = module._parent_fd
    attacked = False

    @contextmanager
    def racing_parent(root_fd: int, path: PurePosixPath, *, create: bool) -> Iterator[int]:
        nonlocal attacked
        if path == target and create and not attacked:
            sentinel = destination / Path(*target.parts)
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_bytes(b"UNMANAGED-SENTINEL\n")
            attacked = True
        with original(root_fd, path, create=create) as descriptor:
            yield descriptor

    monkeypatch.setattr(module, "_parent_fd", racing_parent)

    with pytest.raises(module._DeliveryError) as failure:
        module.deliver(source, destination)

    assert attacked is True
    assert failure.value.retryable is True
    assert (destination / Path(*target.parts)).read_bytes() == b"UNMANAGED-SENTINEL\n"


def test_stale_parent_swap_never_unlinks_replacement_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    target = PurePosixPath("skills/demo/SKILL.md")
    _write(source / "AGENTS.md", b"agents\n")
    _write(source / Path(*target.parts), b"managed\n")
    module = _load_module()
    module.deliver(source, destination)
    (source / Path(*target.parts)).unlink()
    original = module._remove
    attacked = False

    def racing_remove(root_fd: int, path: PurePosixPath, expected: object) -> None:
        nonlocal attacked
        if path == target and not attacked:
            live_parent = destination / "skills" / "demo"
            live_parent.rename(destination / "skills" / "demo-parked")
            live_parent.mkdir()
            (live_parent / "SKILL.md").write_bytes(b"UNMANAGED-SENTINEL\n")
            attacked = True
        original(root_fd, path, expected)

    monkeypatch.setattr(module, "_remove", racing_remove)

    with pytest.raises(module._DeliveryError) as failure:
        module.deliver(source, destination)

    assert attacked is True
    assert failure.value.retryable is True
    assert (destination / Path(*target.parts)).read_bytes() == b"UNMANAGED-SENTINEL\n"


def test_atomic_replace_absent_and_unlink_races_preserve_sentinels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"v1\n")
    module = _load_module()
    module.deliver(source, destination)
    manifest_before = (destination / MANIFEST).read_bytes()

    original_read = module._read_at_parent
    reads = 0

    def racing_replace(parent_fd: int, name: str) -> object:
        nonlocal reads
        snapshot = original_read(parent_fd, name)
        if name == "AGENTS.md":
            reads += 1
            if reads == 3:
                (destination / name).write_bytes(b"UNMANAGED-REPLACE\n")
        return snapshot

    monkeypatch.setattr(module, "_read_at_parent", racing_replace)
    _write(source / "AGENTS.md", b"v2\n")
    with pytest.raises(module._DeliveryError) as replace_failure:
        module.deliver(source, destination)
    assert replace_failure.value.retryable
    assert (destination / "AGENTS.md").read_bytes() == b"UNMANAGED-REPLACE\n"
    assert (destination / MANIFEST).read_bytes() == manifest_before

    monkeypatch.undo()
    source, destination = _roots(tmp_path / "absent")
    _write(source / "AGENTS.md", b"managed\n")
    module = _load_module()
    original_rename = module._rename_noreplace
    attacked = False

    def racing_absent(source_fd: int, source_name: str, target_fd: int, target: str) -> None:
        nonlocal attacked
        if target == "AGENTS.md" and not attacked:
            (destination / target).write_bytes(b"UNMANAGED-ABSENT\n")
            attacked = True
        original_rename(source_fd, source_name, target_fd, target)

    monkeypatch.setattr(module, "_rename_noreplace", racing_absent)
    with pytest.raises(module._DeliveryError) as absent_failure:
        module.deliver(source, destination)
    assert absent_failure.value.retryable
    assert (destination / "AGENTS.md").read_bytes() == b"UNMANAGED-ABSENT\n"
    assert not (destination / MANIFEST).exists()

    monkeypatch.undo()
    source, destination = _roots(tmp_path / "unlink")
    target = PurePosixPath("skills/demo/SKILL.md")
    _write(source / "AGENTS.md", b"agents\n")
    _write(source / Path(*target.parts), b"managed\n")
    module = _load_module()
    module.deliver(source, destination)
    manifest_before = (destination / MANIFEST).read_bytes()
    (source / Path(*target.parts)).unlink()
    original_read = module._read_at_parent
    reads = 0

    def racing_unlink(parent_fd: int, name: str) -> object:
        nonlocal reads
        snapshot = original_read(parent_fd, name)
        if name == target.name:
            reads += 1
            if reads == 3:
                (destination / Path(*target.parts)).write_bytes(b"UNMANAGED-UNLINK\n")
        return snapshot

    monkeypatch.setattr(module, "_read_at_parent", racing_unlink)
    with pytest.raises(module._DeliveryError) as unlink_failure:
        module.deliver(source, destination)
    assert unlink_failure.value.retryable
    assert (destination / Path(*target.parts)).read_bytes() == b"UNMANAGED-UNLINK\n"
    assert (destination / MANIFEST).read_bytes() == manifest_before


def test_quarantine_bookkeeping_failure_requires_recovery_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"v1\n")
    module = _load_module()
    module.deliver(source, destination)
    manifest_before = (destination / MANIFEST).read_bytes()
    _write(source / "AGENTS.md", b"v2\n")
    original_read = module._read_at_parent
    original_stat = module.os.stat
    reads = 0
    attacked = False
    failed = False

    def racing_read(parent_fd: int, name: str) -> object:
        nonlocal reads, attacked
        snapshot = original_read(parent_fd, name)
        if name == "AGENTS.md":
            reads += 1
            if reads == 3:
                (destination / name).write_bytes(b"UNIQUE-OPERATOR-EDIT\n")
                attacked = True
        return snapshot

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

    monkeypatch.setattr(module, "_read_at_parent", racing_read)
    monkeypatch.setattr(module.os, "stat", failing_stat)
    with pytest.raises(module._DeliveryError) as failure:
        module.deliver(source, destination)
    monkeypatch.undo()

    assert attacked and failed
    assert failure.value.code == "quarantine-preserved"
    assert failure.value.retryable
    assert not (destination / "AGENTS.md").exists()
    assert (destination / MANIFEST).read_bytes() == manifest_before
    quarantines = list(destination.glob(".djinn-opencode-stage-*/.quarantine-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b"UNIQUE-OPERATOR-EDIT\n"


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("mkdir", "stage-create-failed"),
        ("open", "stage-create-failed"),
        ("identity", "stage-changed"),
    ],
)
def test_stage_creation_failures_have_exact_cli_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    expected_code: str,
) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"managed\n")
    module = _load_module()
    original_open = module.os.open
    original_stat = module.os.stat

    def failing_mkdir(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("private stage path")

    def failing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if isinstance(path, str) and path.startswith(".djinn-opencode-stage-"):
            raise PermissionError("private stage path")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def failing_identity_stat(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if isinstance(path, str) and path.startswith(".djinn-opencode-stage-"):
            raise OSError("private stage race")
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    if failure == "mkdir":
        monkeypatch.setattr(module.os, "mkdir", failing_mkdir)
    elif failure == "open":
        monkeypatch.setattr(module.os, "open", failing_open)
    else:
        monkeypatch.setattr(module.os, "stat", failing_identity_stat)

    returncode = module.main(["--source", str(source), "--destination", str(destination)])
    captured = capsys.readouterr()

    assert returncode == 1
    assert captured.out == ""
    assert captured.err == f"opencode workflow delivery failed: {expected_code}\n"
    assert "private stage" not in captured.err
    assert not (destination / MANIFEST).exists()


def test_parent_and_root_detach_after_open_cannot_publish_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = _roots(tmp_path)
    target = PurePosixPath("skills/demo/SKILL.md")
    _write(source / "AGENTS.md", b"agents\n")
    _write(source / Path(*target.parts), b"v1\n")
    module = _load_module()
    module.deliver(source, destination)
    manifest_before = (destination / MANIFEST).read_bytes()
    _write(source / Path(*target.parts), b"v2\n")
    original_read = module._read_at_parent
    reads = 0

    def detaching_read(parent_fd: int, name: str) -> object:
        nonlocal reads
        snapshot = original_read(parent_fd, name)
        if name == target.name:
            reads += 1
            if reads == 3:
                parent = destination / "skills/demo"
                parent.rename(destination / "skills/demo-parked")
                parent.mkdir()
                (parent / target.name).write_bytes(b"UNMANAGED-SENTINEL\n")
        return snapshot

    monkeypatch.setattr(module, "_read_at_parent", detaching_read)
    with pytest.raises(module._DeliveryError) as parent_failure:
        module.deliver(source, destination)
    assert parent_failure.value.retryable
    assert (destination / Path(*target.parts)).read_bytes() == b"UNMANAGED-SENTINEL\n"
    assert (destination / MANIFEST).read_bytes() == manifest_before

    monkeypatch.undo()
    source, destination = _roots(tmp_path / "root")
    _write(source / "AGENTS.md", b"v1\n")
    module = _load_module()
    module.deliver(source, destination)
    _write(source / "AGENTS.md", b"v2\n")
    original_verify = module._verify_final_snapshots
    attacked = False

    def detaching_verify(root_fd: int, expected: object) -> None:
        nonlocal attacked
        original_verify(root_fd, expected)
        if not attacked:
            destination.rename(tmp_path / "root/runtime-parked")
            destination.mkdir()
            (destination / "UNMANAGED").write_text("retain")
            attacked = True

    monkeypatch.setattr(module, "_verify_final_snapshots", detaching_verify)
    with pytest.raises(module._DeliveryError) as root_failure:
        module.deliver(source, destination)
    assert root_failure.value.retryable
    assert (destination / "UNMANAGED").read_text() == "retain"
    assert not (destination / MANIFEST).exists()


def test_manifest_is_published_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = _roots(tmp_path)
    _write(source / "AGENTS.md", b"agents\n")
    module = _load_module()
    original = module._replace
    published: list[str] = []

    def recording_replace(
        root_fd: int,
        stage: object,
        staged: object,
        path: PurePosixPath,
        expected: object,
    ) -> None:
        published.append(path.as_posix())
        original(root_fd, stage, staged, path, expected)

    monkeypatch.setattr(module, "_replace", recording_replace)
    module.deliver(source, destination)

    assert published[-1] == MANIFEST


def test_failure_output_and_manifest_never_echo_workflow_bodies(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    sentinel = "WORKFLOW-BODY-SENTINEL"
    _write(source / "AGENTS.md", sentinel.encode())
    _write(destination / "AGENTS.md", b"collision")

    failed = _run(source, destination)
    assert failed.returncode != 0
    assert failed.stdout == ""
    assert sentinel not in failed.stderr

    (destination / "AGENTS.md").unlink()
    succeeded = _run(source, destination)
    assert succeeded.returncode == 0
    assert sentinel.encode() not in (destination / MANIFEST).read_bytes()
    assert str(source) not in (destination / MANIFEST).read_text()


@pytest.mark.parametrize(
    "kind", ["source-symlink", "destination-symlink", "parent-symlink", "wrong-type"]
)
def test_personal_settings_copy_never_follows_links_or_wrong_types(
    tmp_path: Path, kind: str
) -> None:
    source = tmp_path / "personal.json"
    destination_parent = tmp_path / "runtime"
    destination_parent.mkdir()
    destination = destination_parent / ".opencode.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"outside":true}\n')
    if kind == "source-symlink":
        source.symlink_to(outside)
    else:
        source.write_bytes(b'{"personal":true}\n')
    if kind == "destination-symlink":
        destination.symlink_to(outside)
    elif kind == "parent-symlink":
        destination_parent.rmdir()
        destination_parent.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "wrong-type":
        destination.mkdir()

    result = _copy_settings(source, destination)

    assert result.returncode != 0
    assert result.stdout == ""
    assert outside.read_bytes() == b'{"outside":true}\n'


def test_personal_settings_copy_is_atomic_and_missing_reverse_source_is_optional(
    tmp_path: Path,
) -> None:
    source = tmp_path / "runtime" / ".opencode.json"
    destination = tmp_path / "persistent" / ".opencode.json"
    source.parent.mkdir()
    destination.parent.mkdir()
    source.write_bytes(b'{"personal":true}\n')

    copied = _copy_settings(source, destination)
    assert copied.returncode == 0, copied.stderr
    assert destination.read_bytes() == source.read_bytes()
    assert not destination.stat().st_mode & stat.S_IXUSR

    source.unlink()
    skipped = _copy_settings(source, destination, missing_ok=True)
    assert skipped.returncode == 0, skipped.stderr
    assert destination.read_bytes() == b'{"personal":true}\n'


def test_entrypoint_and_image_use_the_narrow_bridge_contract() -> None:
    entrypoint = (ROOT / "scripts" / "entrypoint.sh").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()

    exact = (
        "python3 /home/dev/opencode-workflow-delivery.py \\\n"
        "        --source /home/dev/.opencode/seed \\\n"
        "        --destination /home/dev/.config/opencode"
    )
    assert exact in entrypoint
    assert entrypoint.index(exact) < entrypoint.index('source "$MCP_REGISTER"')
    assert entrypoint.index(exact) < entrypoint.index('/bin/zsh "$@"')
    assert 'sync_seed "opencode"' not in entrypoint
    assert (
        '--copy-settings "$OPENCODE_RUNTIME_SETTINGS" "$OPENCODE_PERSISTENT_SETTINGS"'
    ) in entrypoint
    assert '"$HOME/.opencode/seed/.opencode.json"' not in entrypoint.split("# Interactive Shell")[1]
    assert 'sync_seed "gemini"' in entrypoint
    assert (
        "COPY --chown=dev:dev scripts/opencode-workflow-delivery.py "
        "/home/dev/opencode-workflow-delivery.py"
    ) in dockerfile
    assert "jq python3 build-essential" in dockerfile
    assert (
        'RUN uv tool install ruff\n\n'
        '# Non-interactive processes do not source fnm\'s shell initialization.\n'
        'ENV PATH="/home/${USERNAME}/.local/share/fnm/aliases/default/bin:$PATH"'
    ) in dockerfile
