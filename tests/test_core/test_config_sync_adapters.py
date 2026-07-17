from __future__ import annotations

import json
import stat
import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig, ConfigSyncConfig, ConfigSyncSource
from djinn_in_a_box.core.config_sync import (
    CANONICAL_REMEDY,
    MANIFEST_NAME,
    DriftClass,
    audit_config_sync,
    sync_config,
)
from djinn_in_a_box.core.config_sync_adapters import (
    OWNERSHIP_MATRIX,
    read_native_workflow,
    render_native_workflow,
)


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
def test_full_surface_projection_preserves_modes_carriers_and_plugins(
    tmp_path: Path, source: ConfigSyncSource, target: ConfigSyncSource
) -> None:
    project, config_path = _workspace(tmp_path, source)
    hook_contents = _full_source(project / "config" / source, source)

    result = sync_config(project, config_path=config_path)

    assert result.success
    target_root = project / "config" / target
    owned = OWNERSHIP_MATRIX[target]
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
        assert plugin.read_bytes() == hook_contents[hook.name]
        if target == "opencode":
            assert plugin.read_bytes().decode()
            assert b"export" in plugin.read_bytes()
    if target == "claude":
        assert isinstance(json.loads((target_root / "settings.json").read_bytes()), dict)
    if target == "codex":
        assert isinstance(json.loads((target_root / "hooks.json").read_bytes()), dict)
        assert tomllib.loads((target_root / "config.toml").read_text())
    manifest = json.loads((project / "config" / MANIFEST_NAME).read_text())
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


def test_claude_source_only_command_survives_a_round_trip_without_projection(
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

    assert sync_config(project, config_path=config_path).success
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
