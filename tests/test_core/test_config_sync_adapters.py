from __future__ import annotations

import hashlib
import json
import stat
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

import djinn_in_a_box.core.workflow_publisher as publisher_module
from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig, ConfigSyncSource
from djinn_in_a_box.core.config_sync import (
    CANONICAL_REMEDY,
    MANIFEST_NAME,
    DriftClass,
    audit_config_sync,
    load_canonical_delivery_view,
    sync_config,
)
from djinn_in_a_box.core.config_sync_adapters import (
    OWNERSHIP_MATRIX,
    read_native_workflow,
    render_native_workflow,
)
from djinn_in_a_box.core.workflow_publisher import decode_lean_manifest


def _workspace(tmp_path: Path, source: ConfigSyncSource) -> tuple[Path, Path]:
    project = tmp_path / "project"
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
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


def _write(root: Path, relative: str, content: bytes | str, *, executable: bool = False) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    if executable:
        path.chmod(0o755)
    return path


def _tree(root: Path) -> dict[PurePosixPath, tuple[bytes, int]]:
    return {
        PurePosixPath(path.relative_to(root).as_posix()): (
            path.readlink().as_posix().encode() if path.is_symlink() else path.read_bytes(),
            stat.S_IMODE(path.lstat().st_mode),
        )
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }


def _hook_content(name: str) -> bytes:
    marker = b'"tool.execute.before"' if name == "security" else b"event"
    return b"export const Plugin = async () => ({ " + marker + b": async () => {} });\n"


def _hook_registrations(tool: ConfigSyncSource) -> dict[str, object]:
    if tool == "claude":
        commands = {
            "startup": "uv run python3 ~/.claude/scripts/session-start-status.py",
            "security": "uv run python3 ~/.claude_seed/security_reminder_hook.py",
            "ready": "uv run python3 ~/.claude_seed/ready_notify_hook.py",
        }
        return {
            "hooks": {
                "SessionStart": [
                    {"matcher": "", "hooks": [{"type": "command", "command": commands["startup"]}]}
                ],
                "PreToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [{"type": "command", "command": commands["security"]}],
                    }
                ],
                "Stop": [
                    {"matcher": "", "hooks": [{"type": "command", "command": commands["ready"]}]}
                ],
            }
        }
    commands = {
        "startup": "scripts/session-start-status.py",
        "security": "hooks/security_guard.py",
        "ready": "hooks/ready_notify.py",
    }

    def command(name: str) -> str:
        return f"bash -lc 'uv run python \"${{CODEX_HOME:-$HOME/.codex}}/{commands[name]}\"'"

    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("startup"),
                            "statusMessage": "Checking Codex session status",
                        }
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash|Edit|Write|apply_patch",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("security"),
                            "statusMessage": "Applying Codex security guard",
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("ready"),
                        }
                    ],
                }
            ],
        }
    }


def _native_hooks(root: Path, tool: ConfigSyncSource) -> tuple[dict[str, bytes], bytes | None]:
    contents: dict[str, bytes] = {}
    for hook in OWNERSHIP_MATRIX[tool].hooks:
        content = (
            _hook_content(hook.name)
            if tool == "opencode"
            else f"#!/usr/bin/env python3\nprint({hook.name!r})\n".encode()
        )
        contents[hook.name] = content
        _write(root, hook.script_path.as_posix(), content)
    if tool == "opencode":
        return contents, None
    carrier = "settings.json" if tool == "claude" else "hooks.json"
    carrier_bytes = json.dumps(_hook_registrations(tool), sort_keys=True).encode()
    _write(root, carrier, carrier_bytes)
    return contents, carrier_bytes


def _full_source(
    root: Path, tool: ConfigSyncSource, *, source_only: bool = False
) -> dict[str, bytes]:
    owned = OWNERSHIP_MATRIX[tool]
    _write(root, owned.instruction_path.as_posix(), "Shared instructions.\n")
    if tool == "codex":
        _write(
            root,
            "agents/reviewer.toml",
            (
                'name = "reviewer"\ndescription = "Reviews"\n'
                'developer_instructions = "Review carefully."\n'
            ),
        )
        _write(
            root,
            "skills/command-ship/SKILL.md",
            '---\nname: "command-ship"\ndescription: "Ship"\n---\n\nShip carefully.\n',
        )
    else:
        _write(
            root,
            "agents/reviewer.md",
            '---\nname: "reviewer"\ndescription: "Reviews"\n---\n\nReview carefully.\n',
        )
        _write(root, "commands/ship.md", '---\ndescription: "Ship"\n---\n\nShip carefully.\n')
    _write(
        root,
        "skills/check/SKILL.md",
        "---\nname: check\ndescription: Check\n---\n\nCheck safely.\n",
    )
    _write(root, "skills/check/references/guide.md", "Reference.\n")
    _write(root, "context/policy.md", "Policy.\n")
    _write(root, "context/example.json", '{"safe":true}\n')
    _write(root, "context/example.toml", "safe = true\n")
    _write(root, "scripts/run.sh", "#!/bin/sh\necho safe\n", executable=True)

    hooks: dict[str, bytes] = {}
    for hook in owned.hooks:
        hooks[hook.name] = _hook_content(hook.name)
        _write(root, hook.script_path.as_posix(), hooks[hook.name])
    if tool != "opencode":
        carrier = "settings.json" if tool == "claude" else "hooks.json"
        _write(root, carrier, json.dumps(_hook_registrations(tool)))
    if tool == "codex":
        _write(root, "config.toml", 'project_doc_fallback_filenames = ["CLAUDE.md"]\n')
    if source_only:
        _write(
            root,
            "commands/codex-review.md",
            (
                '---\ndescription: "Review"\nargument-hint: "<target>"\n'
                'allowed-tools: "Bash, Read"\n---\n\nReview.\n'
            ),
        )
    return hooks


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
def test_full_surface_projection_excludes_hooks_and_preserves_native_ones(
    tmp_path: Path, source: ConfigSyncSource, target: ConfigSyncSource
) -> None:
    project, config_path = _workspace(tmp_path, source)
    source_root = project / "config" / source
    _full_source(source_root, source)
    target_root = project / "config" / target
    native_hooks, native_carrier = _native_hooks(target_root, target)
    rendered = render_native_workflow(read_native_workflow(source_root, source), target)
    owned = OWNERSHIP_MATRIX[target]

    assert not {
        item.relative_path for item in rendered.files
    } & {hook.script_path for hook in owned.hooks}
    assert not {
        (item.carrier_path, item.key_path) for item in rendered.settings_fragments
    } & {
        (hook.carrier_path, ("hooks", hook.event))
        for hook in owned.hooks
        if hook.carrier_path and hook.event
    }

    result = sync_config(project, config_path=config_path)

    assert result.success
    assert (target_root / owned.instruction_path).is_file()
    assert (target_root / owned.instruction_companion).is_file()
    assert (
        target_root / ("agents/reviewer.toml" if target == "codex" else "agents/reviewer.md")
    ).is_file()
    assert (
        target_root / ("skills/command-ship/SKILL.md" if target == "codex" else "commands/ship.md")
    ).is_file()
    assert (target_root / "skills/check/SKILL.md").is_file()
    assert (target_root / "skills/check/references/guide.md").is_file()
    assert (target_root / "context/policy.md").is_file()
    assert json.loads((target_root / "context/example.json").read_bytes()) == {"safe": True}
    assert tomllib.loads((target_root / "context/example.toml").read_text()) == {"safe": True}
    script = target_root / "scripts/run.sh"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    for hook in owned.hooks:
        plugin = target_root / hook.script_path
        assert plugin.read_bytes() == native_hooks[hook.name]
        if target == "opencode":
            assert plugin.read_bytes().decode()
            assert b"export" in plugin.read_bytes()
    if target == "claude":
        assert (target_root / "settings.json").read_bytes() == native_carrier
    if target == "codex":
        assert (target_root / "hooks.json").read_bytes() == native_carrier
        assert tomllib.loads((target_root / "config.toml").read_text())
    manifest = json.loads((project / "config" / MANIFEST_NAME).read_text())
    assert not {
        item["path"] for item in manifest["items"]
    } & {f"{target}/{hook.script_path}" for hook in owned.hooks}
    assert not {
        (item["path"], tuple(item["key_path"]))
        for item in manifest["items"]
        if "key_path" in item
    } & {
        (f"{target}/{hook.carrier_path}", ("hooks", hook.event))
        for hook in owned.hooks
        if hook.carrier_path and hook.event
    }
    record = next(item for item in manifest["items"] if item["path"] == f"{target}/scripts/run.sh")
    assert record["executable"] is True

    script.chmod(0o644)

    assert (
        DriftClass.TARGET_DRIFT in audit_config_sync(project, config_path=config_path).drift_classes
    )


def test_nonportable_artifact_uses_the_canonical_remedy_without_mutation(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path, "codex")
    _full_source(project / "config" / "codex", "codex")
    _write(
        project / "config" / "codex",
        "agents/reviewer.toml",
        (
            'name = "reviewer"\ndescription = "Reviews"\nmodel = "native-only"\n'
            'developer_instructions = "Review carefully."\n'
        ),
    )
    before = _tree(project / "config")
    source = read_native_workflow(project / "config" / "codex", "codex")
    projected = render_native_workflow(source, "claude")

    result = sync_config(project, config_path=config_path)

    assert projected.unresolved[0].reason == CANONICAL_REMEDY
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before


@pytest.mark.parametrize("content", [b"const plugin = async () => ({});\n", b"\xff"])
def test_invalid_opencode_plugin_blocks_without_mutating_target_or_manifest(
    tmp_path: Path, content: bytes
) -> None:
    project, config_path = _workspace(tmp_path, "opencode")
    _write(project / "config" / "opencode", "AGENTS.md", "Instructions.\n")
    _write(project / "config" / "opencode", "plugins/ready-notify.js", content)
    before = _tree(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before
    assert not (project / "config" / MANIFEST_NAME).exists()


@pytest.mark.parametrize("content", [b"const plugin = async () => ({});\n", b"\xff"])
def test_invalid_native_target_plugin_blocks_its_delivery_without_mutation(
    tmp_path: Path, content: bytes
) -> None:
    project, config_path = _workspace(tmp_path, "claude")
    _full_source(project / "config" / "claude", "claude")
    plugin = _write(project / "config" / "opencode", "plugins/ready-notify.js", content)
    before = _tree(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.success is False
    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before
    assert plugin.read_bytes() == content
    assert not (project / "config" / MANIFEST_NAME).exists()


def test_real_claude_python_hooks_are_not_projected_and_native_plugin_is_preserved(
    tmp_path: Path,
) -> None:
    project, config_path = _workspace(tmp_path, "claude")
    claude_root = project / "config" / "claude"
    _full_source(claude_root, "claude")
    python_hooks = {
        "startup": b"#!/usr/bin/env python3\nprint('session started')\n",
        "security": b"#!/usr/bin/env python3\nprint('security reminder')\n",
        "ready": b"#!/usr/bin/env python3\nprint('ready notification')\n",
    }
    for hook in OWNERSHIP_MATRIX["claude"].hooks:
        _write(claude_root, hook.script_path.as_posix(), python_hooks[hook.name])
    assert all(b"export" not in content for content in python_hooks.values())

    first = sync_config(project, config_path=config_path)

    assert first.success
    opencode_root = project / "config" / "opencode"
    assert not any(
        (opencode_root / hook.script_path).exists()
        for hook in OWNERSHIP_MATRIX["opencode"].hooks
    )
    claude_view = load_canonical_delivery_view(project, "claude", config_path=config_path)
    assert claude_view.success and claude_view.view is not None
    delivered_claude_files = {item.relative_path: item.content for item in claude_view.view.files}
    for hook in OWNERSHIP_MATRIX["claude"].hooks:
        assert delivered_claude_files[hook.script_path] == python_hooks[hook.name]

    plugin = _write(
        opencode_root,
        "plugins/ready-notify.js",
        b"export const Plugin = async () => ({ event: async () => {} });\n",
    )
    plugin_bytes = plugin.read_bytes()

    second = sync_config(project, config_path=config_path)

    assert second.success
    assert plugin.read_bytes() == plugin_bytes
    opencode_view = load_canonical_delivery_view(project, "opencode", config_path=config_path)
    assert opencode_view.success and opencode_view.view is not None
    assert {
        item.relative_path: item.content for item in opencode_view.view.files
    }[PurePosixPath("plugins/ready-notify.js")] == plugin_bytes


def test_sync_releases_legacy_target_hook_records_without_removing_native_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = _workspace(tmp_path, "claude")
    _full_source(project / "config" / "claude", "claude")
    codex_root = project / "config" / "codex"
    native_hooks, native_carrier = _native_hooks(codex_root, "codex")
    assert native_carrier is not None
    assert sync_config(project, config_path=config_path).success
    manifest_path = project / "config" / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    for hook in OWNERSHIP_MATRIX["codex"].hooks:
        hook_path = codex_root / hook.script_path
        manifest["items"].append(
            {
                "path": f"codex/{hook.script_path}",
                "content_hash": hashlib.sha256(hook_path.read_bytes()).hexdigest(),
                "executable": False,
            }
        )
        assert hook.carrier_path is not None and hook.event is not None
        registration = json.loads((codex_root / hook.carrier_path).read_text())["hooks"][hook.event]
        manifest["items"].append(
            {
                "path": f"codex/{hook.carrier_path}",
                "key_path": ["hooks", hook.event],
                "content_hash": hashlib.sha256(
                    json.dumps(
                        registration,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest(),
                "executable": False,
            }
        )
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    before_hooks = {
        hook.name: (codex_root / hook.script_path).read_bytes()
        for hook in OWNERSHIP_MATRIX["codex"].hooks
    }
    before_carrier = (codex_root / "hooks.json").read_bytes()

    def crash_after_first_mutation(count: int) -> None:
        if count == 1:
            raise RuntimeError("injected native-only release crash")

    def no_crash(_count: int) -> None:
        return None

    monkeypatch.setattr(publisher_module, "_after_target_mutation", crash_after_first_mutation)
    with pytest.raises(RuntimeError, match="injected native-only release crash"):
        sync_config(project, config_path=config_path)
    monkeypatch.setattr(publisher_module, "_after_target_mutation", no_crash)
    assert (codex_root / "hooks.json").read_bytes() == before_carrier
    for hook in OWNERSHIP_MATRIX["codex"].hooks:
        assert (codex_root / hook.script_path).read_bytes() == before_hooks[hook.name]

    result = sync_config(project, config_path=config_path)

    assert result.success
    assert not set(result.removed_paths) & {
        PurePosixPath("codex") / hook.script_path for hook in OWNERSHIP_MATRIX["codex"].hooks
    }
    assert (codex_root / "hooks.json").read_bytes() == before_carrier
    for hook in OWNERSHIP_MATRIX["codex"].hooks:
        assert (codex_root / hook.script_path).read_bytes() == before_hooks[hook.name]
        assert before_hooks[hook.name] == native_hooks[hook.name]
    strict = decode_lean_manifest(
        manifest_path.read_bytes(), canonical_target=True, target_tool=None
    )
    assert not {
        item.path for item in strict.items
    } & {PurePosixPath("codex") / hook.script_path for hook in OWNERSHIP_MATRIX["codex"].hooks}
    assert not {
        (item.path, item.key_path)
        for item in strict.items
        if item.key_path is not None
    } & {
        (PurePosixPath("codex") / hook.carrier_path, ("hooks", hook.event))
        for hook in OWNERSHIP_MATRIX["codex"].hooks
        if hook.carrier_path and hook.event
    }
    assert audit_config_sync(project, config_path=config_path).clean


def test_claude_source_only_command_survives_a_blocked_source_switch_without_projection(
    tmp_path: Path,
) -> None:
    project, config_path = _workspace(tmp_path, "claude")
    claude_root = project / "config" / "claude"
    _full_source(claude_root, "claude", source_only=True)
    command = (claude_root / "commands/codex-review.md").read_bytes()

    assert sync_config(project, config_path=config_path).success
    assert not (project / "config/codex/commands/codex-review.md").exists()
    assert not (project / "config/opencode/commands/codex-review.md").exists()
    save_config(
        AppConfig(
            code_dir=tmp_path / "code",
            config_root=tmp_path / "runtime",
            config_sync=ConfigSyncConfig(source="codex"),
        ),
        config_path,
    )

    blocked = sync_config(project, config_path=config_path)

    assert blocked.success is False
    assert blocked.audit.drift_classes == (DriftClass.COLLISION,)
    assert (claude_root / "commands/codex-review.md").read_bytes() == command
    assert not (project / "config/opencode/commands/codex-review.md").exists()


def test_portable_runtime_named_skill_projects_like_other_skills(tmp_path: Path) -> None:
    project, config_path = _workspace(tmp_path, "claude")
    source = project / "config/claude"
    _write(source, "CLAUDE.md", "Instructions.\n")
    skill = _write(
        source,
        "skills/convergence-loop/SKILL.md",
        "---\nname: convergence-loop\ndescription: Converge\n---\n\nReview fully.\n",
    )

    result = sync_config(project, config_path=config_path)

    assert result.success
    for tool in ("codex", "opencode"):
        assert (project / "config" / tool / "skills/convergence-loop/SKILL.md").read_bytes() == (
            skill.read_bytes()
        )


def _invalid_markdown_agent(root: Path) -> None:
    _write(root, "agents/reviewer.md", "---\nname: reviewer\n---\n\nMissing description.\n")


def _invalid_codex_agent(root: Path) -> None:
    _write(root, "agents/reviewer.toml", 'name = "reviewer"\ndescription =\n')


def _invalid_skill(root: Path) -> None:
    _write(root, "skills/check/SKILL.md", "---\nname: other\ndescription: Check\n---\n\nBody.\n")


@pytest.mark.parametrize(
    ("source", "prepare"),
    [
        ("claude", _invalid_markdown_agent),
        ("codex", _invalid_codex_agent),
        ("opencode", _invalid_markdown_agent),
        ("opencode", _invalid_skill),
    ],
)
def test_invalid_native_artifacts_block_without_target_or_manifest_mutation(
    tmp_path: Path, source: ConfigSyncSource, prepare: Callable[[Path], None]
) -> None:
    project, config_path = _workspace(tmp_path, source)
    root = project / "config" / source
    _write(root, OWNERSHIP_MATRIX[source].instruction_path.as_posix(), "Instructions.\n")
    prepare(root)
    before = _tree(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before
    assert not (project / "config" / MANIFEST_NAME).exists()


@pytest.mark.parametrize("absolute", [False, True])
def test_escaping_paths_block_without_target_or_manifest_mutation(
    tmp_path: Path, absolute: bool
) -> None:
    project, config_path = _workspace(tmp_path, "claude")
    root = project / "config" / "claude"
    _write(root, "CLAUDE.md", "Instructions.\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    escaped = root / "context" / "escaped.txt"
    escaped.parent.mkdir()
    escaped.symlink_to(str(outside) if absolute else "../../../../outside.txt")
    before = _tree(project / "config")

    result = sync_config(project, config_path=config_path)

    assert result.audit.drift_classes == (DriftClass.INVALID_OR_SEMANTIC,)
    assert _tree(project / "config") == before
    assert not (project / "config" / MANIFEST_NAME).exists()
