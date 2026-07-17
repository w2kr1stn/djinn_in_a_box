from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core.config_sync_adapters import (
    OWNERSHIP_MATRIX,
    AdapterRenderResult,
    RenderedFile,
    SettingsFragment,
    UnresolvedItem,
    allowed_outputs_for_unresolved,
    fragment_is_owned,
    is_safe_relative_path,
    native_only_path_is_owned,
    path_is_owned,
    read_native_workflow,
    render_native_workflow,
    validate_rendered_workflow,
)


def _write(root: Path, relative: str, content: str = "content\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _markdown(name: str, description: str, body: str, extra: str = "") -> str:
    return f'---\nname: "{name}"\ndescription: "{description}"\n{extra}---\n\n{body}\n'


def _source(root: Path, tool: ConfigSyncSource, *, reverse_creation: bool = False) -> None:
    instruction = OWNERSHIP_MATRIX[tool].instruction_path.as_posix()
    entries = [
        (instruction, "Shared instructions.\n"),
        (
            "skills/check/SKILL.md",
            "---\nname: check\ndescription: Check safely\n---\n\nCheck safely.\n",
        ),
        ("context/policy.md", "Policy.\n"),
    ]
    if tool == "codex":
        entries.extend(
            (
                (
                    "agents/reviewer.toml",
                    'name = "reviewer"\ndescription = "Reviews"\n'
                    'developer_instructions = "Review carefully."\n',
                ),
                (
                    "skills/command-ship/SKILL.md",
                    '---\nname: "command-ship"\ndescription: "Ship"\n---\n\nShip safely.\n',
                ),
            )
        )
    else:
        entries.extend(
            (
                ("agents/reviewer.md", _markdown("reviewer", "Reviews", "Review carefully.")),
                ("commands/ship.md", '---\ndescription: "Ship"\n---\n\nShip safely.\n'),
            )
        )
    for relative, content in reversed(entries) if reverse_creation else entries:
        _write(root, relative, content)


def _paths(result: AdapterRenderResult) -> set[str]:
    return {item.relative_path.as_posix() for item in result.files}


@pytest.mark.parametrize(
    ("source_tool", "target_tool"),
    [
        ("claude", "codex"),
        ("claude", "opencode"),
        ("codex", "claude"),
        ("codex", "opencode"),
        ("opencode", "claude"),
        ("opencode", "codex"),
    ],
)
def test_all_six_static_directions(
    tmp_path: Path, source_tool: ConfigSyncSource, target_tool: ConfigSyncSource
) -> None:
    _source(tmp_path, source_tool)

    source = read_native_workflow(tmp_path, source_tool)
    result = render_native_workflow(source, target_tool)

    assert source.validation_issues == ()
    assert source.unresolved == ()
    assert result.validation_issues == ()
    assert result.unresolved == ()
    paths = _paths(result)
    target = OWNERSHIP_MATRIX[target_tool]
    assert {target.instruction_path.as_posix(), target.instruction_companion.as_posix()} <= paths
    assert "skills/check/SKILL.md" in paths
    assert "context/policy.md" in paths
    assert ("agents/reviewer.toml" if target_tool == "codex" else "agents/reviewer.md") in paths
    assert (
        "skills/command-ship/SKILL.md" if target_tool == "codex" else "commands/ship.md"
    ) in paths


def test_markdown_to_markdown_preserves_bytes_and_complex_metadata_is_unresolved(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "CLAUDE.md", "Instructions.\n")
    raw = (
        "---\nname: reviewer\ndescription: Review\ntools:\n  Read: true\n"
        "provider:\n  model: sonnet\n---\n\nPrompt.\n"
    )
    _write(tmp_path, "agents/reviewer.md", raw)

    source = read_native_workflow(tmp_path, "claude")
    result = render_native_workflow(source, "opencode")

    unresolved = next(item for item in result.unresolved if item.identifier.startswith("agent:"))
    assert unresolved.source_bytes == raw.encode()
    assert unresolved.metadata == raw.encode()
    rendered = next(
        item for item in result.files if item.relative_path == PurePosixPath("agents/reviewer.md")
    )
    assert rendered.content == raw.encode()


def test_unknown_metadata_value_is_retained_and_blocks_lossy_conversion(tmp_path: Path) -> None:
    _write(tmp_path, "CLAUDE.md", "Instructions.\n")
    raw = _markdown("reviewer", "Review", "Prompt.", 'model: "custom/model-v9"\n')
    _write(tmp_path, "agents/reviewer.md", raw)

    result = render_native_workflow(read_native_workflow(tmp_path, "claude"), "codex")

    unresolved = next(item for item in result.unresolved if item.identifier.startswith("agent:"))
    assert unresolved.source_bytes == raw.encode()
    assert b"custom/model-v9" in (unresolved.metadata or b"")
    assert "agents/reviewer.toml" not in _paths(result)


def test_explicit_runtime_variants_are_never_blindly_projected(tmp_path: Path) -> None:
    _write(tmp_path, "CLAUDE.md", "Instructions.\n")
    skill = _write(
        tmp_path,
        "skills/convergence-loop/SKILL.md",
        "---\nname: convergence-loop\ndescription: Converge changes\n---\n\n"
        "Claude-native runtime.\n",
    ).read_bytes()
    command = _write(
        tmp_path,
        "commands/codex-review.md",
        '---\ndescription: "Claude review"\n'
        "argument-hint: <target> [--scope=<paths>]\n"
        "allowed-tools: Bash, Read\n---\n\n# /codex-review\n\nReview.\n",
    ).read_bytes()

    source = read_native_workflow(tmp_path, "claude")
    result = render_native_workflow(source, "opencode")

    assert source.validation_issues == ()
    assert source.unresolved == ()
    assert "skills/convergence-loop/SKILL.md" not in _paths(result)
    variant = next(item for item in result.unresolved if "skill variant" in item.reason)
    assert variant.source_bytes == skill
    assert "commands/codex-review.md" not in _paths(result)
    assert all(item.source_bytes != command for item in result.unresolved)


def test_misplaced_claude_only_command_is_unresolved_with_raw_bytes(tmp_path: Path) -> None:
    _write(tmp_path, "AGENTS.md", "Instructions.\n")
    raw = '---\ndescription: "Review"\n---\n\nReview.\n'
    _write(tmp_path, "commands/codex-review.md", raw)

    source = read_native_workflow(tmp_path, "opencode")

    item = next(entry for entry in source.unresolved if "native to claude" in entry.reason)
    assert item.source_bytes == raw.encode()
    assert item.metadata is not None


def test_noncanonical_hook_registration_retains_actual_registration(tmp_path: Path) -> None:
    _write(tmp_path, "CLAUDE.md", "Instructions.\n")
    script = _write(tmp_path, "ready_notify_hook.py", "print('ready')\n").read_bytes()
    actual = [{"matcher": "custom", "hooks": [{"type": "command", "command": "custom"}]}]
    _write(tmp_path, "settings.json", json.dumps({"hooks": {"Stop": actual}}))

    source = read_native_workflow(tmp_path, "claude")

    unresolved = next(item for item in source.unresolved if "Noncanonical" in item.reason)
    assert unresolved.source_bytes == script
    assert json.loads(unresolved.metadata or b"") == actual


def test_python_hooks_are_syntax_validated_and_cross_tool_copy_is_never_claimed_valid(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "CLAUDE.md", "Instructions.\n")
    _write(tmp_path, "ready_notify_hook.py", "def broken(:\n")
    registration = [
        {
            "matcher": "",
            "hooks": [
                {"type": "command", "command": "uv run python3 ~/.claude_seed/ready_notify_hook.py"}
            ],
        }
    ]
    _write(tmp_path, "settings.json", json.dumps({"hooks": {"Stop": registration}}))

    source = read_native_workflow(tmp_path, "claude")
    opencode = render_native_workflow(source, "opencode")

    assert any(issue.identifier == "hook-python:ready" for issue in source.validation_issues)
    assert "plugins/ready-notify.js" not in _paths(opencode)
    assert any("Cross-tool hook" in item.reason for item in opencode.unresolved)


def test_same_language_cross_tool_hook_is_semantic_and_never_byte_copied(tmp_path: Path) -> None:
    _write(tmp_path, "CLAUDE.md", "Instructions.\n")
    script = _write(tmp_path, "security_reminder_hook.py", "print('claude semantics')\n")
    registration = [
        {
            "matcher": "Edit|Write",
            "hooks": [
                {
                    "type": "command",
                    "command": "uv run python3 ~/.claude_seed/security_reminder_hook.py",
                }
            ],
        }
    ]
    _write(tmp_path, "settings.json", json.dumps({"hooks": {"PreToolUse": registration}}))

    result = render_native_workflow(read_native_workflow(tmp_path, "claude"), "codex")

    assert "hooks/security_guard.py" not in _paths(result)
    unresolved = next(item for item in result.unresolved if item.identifier.startswith("hook:"))
    assert unresolved.source_bytes == script.read_bytes()
    assert "Cross-tool hook" in unresolved.reason
    assert any(
        fragment.carrier_path == PurePosixPath("hooks.json")
        and fragment.key_path == ("hooks", "PreToolUse")
        for fragment in result.settings_fragments
    )


def test_cross_language_hook_keeps_registration_deterministic_and_only_script_unresolved(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "AGENTS.md", "Instructions.\n")
    script = _write(
        tmp_path,
        "plugins/ready-notify.js",
        "export const Ready = async () => ({ event: async () => {} });\n",
    )
    script.chmod(0o755)

    result = render_native_workflow(read_native_workflow(tmp_path, "opencode"), "claude")

    unresolved = next(item for item in result.unresolved if item.identifier.startswith("hook:"))
    fragment = next(
        item
        for item in result.settings_fragments
        if item.carrier_path == PurePosixPath("settings.json")
        and item.key_path == ("hooks", "Stop")
    )
    assert unresolved.executable is True
    assert allowed_outputs_for_unresolved(unresolved).settings_fragments == ()
    assert json.loads(fragment.value_json) == [
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


@pytest.mark.parametrize(
    ("source_name", "target", "target_name"),
    [
        ("convergence-loop", "codex", "convergence-loop"),
        ("claude-md-management", "codex", "agents-md-management"),
        ("update-claude-md", "opencode", "update-agents-md"),
        ("session-bootstrap", "codex", "session-bootstrap"),
        ("agents-md-management", "claude", "claude-md-management"),
        ("update-agents-md", "claude", "update-claude-md"),
    ],
)
def test_runtime_skill_families_have_explicit_native_target_names(
    source_name: str, target: ConfigSyncSource, target_name: str
) -> None:
    item = UnresolvedItem(
        f"skill:{source_name}:skills/{source_name}/SKILL.md",
        "Runtime-specific skill variant needs adaptation.",
        PurePosixPath(f"skills/{source_name}/SKILL.md"),
        b"source",
        target_tool=target,
    )

    contract = allowed_outputs_for_unresolved(item)

    assert contract.file_paths == (PurePosixPath(f"skills/{target_name}/SKILL.md"),)


def test_symlink_directory_cycle_and_file_alias_are_blocked_deterministically(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "CLAUDE.md", "Instructions.\n")
    target = _write(tmp_path, "context/a.md", "inside\n")
    (tmp_path / "context/b.md").symlink_to(target)
    (tmp_path / "context/cycle").symlink_to(tmp_path / "context", target_is_directory=True)

    source = read_native_workflow(tmp_path, "claude")

    identifiers = [issue.identifier for issue in source.validation_issues]
    assert identifiers == sorted(identifiers)
    assert "symlink-alias:context/b.md" in identifiers
    assert "symlink-directory:context/cycle" in identifiers
    paths = {item.source_path.as_posix() for item in source.artifacts}
    assert "context/a.md" in paths
    assert "context/b.md" not in paths


def test_discovery_and_render_order_ignore_creation_order(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _source(left, "claude")
    _source(right, "claude", reverse_creation=True)

    assert read_native_workflow(left, "claude") == read_native_workflow(right, "claude")
    assert render_native_workflow(
        read_native_workflow(left, "claude"), "codex"
    ) == render_native_workflow(read_native_workflow(right, "claude"), "codex")


def test_duplicate_paths_and_fragments_fail_validation() -> None:
    files = (
        RenderedFile(PurePosixPath("CLAUDE.md"), b"a", "instructions"),
        RenderedFile(PurePosixPath("AGENTS.md"), b"a", "companion"),
        RenderedFile(PurePosixPath("AGENTS.md"), b"b", "duplicate"),
    )
    fragment = SettingsFragment(PurePosixPath("settings.json"), ("hooks", "Stop"), b"[]", "hook")

    issues = validate_rendered_workflow("claude", files, (fragment, fragment))

    identifiers = {item.identifier for item in issues}
    assert "duplicate-path:AGENTS.md" in identifiers
    assert "duplicate-fragment:hook" in identifiers


@pytest.mark.parametrize(
    "content",
    [
        b"prompt without frontmatter\n",
        b"---\nname: other\ndescription: Reviews\n---\n\nPrompt.\n",
        b"---\nname: reviewer\nunknown: value\ndescription: Reviews\n---\n\nPrompt.\n",
        b"---\nname: reviewer\ndescription: Reviews\n---\n",
        b"---\nname: reviewer\ndescription: Reviews\n---\n\n\xff",
    ],
)
def test_markdown_agent_native_validation_rejects_invalid_frontmatter_and_body(
    content: bytes,
) -> None:
    files = (
        RenderedFile(PurePosixPath("CLAUDE.md"), b"instructions", "instructions"),
        RenderedFile(PurePosixPath("AGENTS.md"), b"instructions", "companion"),
        RenderedFile(PurePosixPath("agents/reviewer.md"), content, "agent:reviewer"),
    )

    issues = validate_rendered_workflow("claude", files, ())

    assert issues


@pytest.mark.parametrize(
    "content",
    [
        b'foo = "bar"\n',
        b'name = "other"\ndescription = "Reviews"\ndeveloper_instructions = "Review carefully."\n',
        b'name = "reviewer"\ndescription = ""\ndeveloper_instructions = "Review carefully."\n',
        b'name = "reviewer"\ndescription = "Reviews"\ndeveloper_instructions = ""\n',
        b'name = "reviewer"\ndescription = "Reviews"\n'
        b'developer_instructions = "Review carefully."\nunknown = "value"\n',
    ],
)
def test_codex_agent_native_validation_rejects_missing_mismatched_and_unknown_fields(
    content: bytes,
) -> None:
    files = (
        RenderedFile(PurePosixPath("AGENTS.md"), b"instructions", "instructions"),
        RenderedFile(PurePosixPath("CLAUDE.md"), b"instructions", "companion"),
        RenderedFile(PurePosixPath("agents/reviewer.toml"), content, "agent:reviewer"),
    )

    issues = validate_rendered_workflow("codex", files, ())

    assert any(item.identifier == "invalid-agent:agent:reviewer" for item in issues)


def test_codex_agent_native_validation_accepts_bounded_runtime_fields() -> None:
    content = (
        b'name = "reviewer"\ndescription = "Reviews"\n'
        b'model = "gpt-5.6-terra"\nmodel_reasoning_effort = "xhigh"\n'
        b'sandbox_mode = "danger-full-access"\n'
        b'developer_instructions = "Review carefully."\n'
    )
    files = (
        RenderedFile(PurePosixPath("AGENTS.md"), b"instructions", "instructions"),
        RenderedFile(PurePosixPath("CLAUDE.md"), b"instructions", "companion"),
        RenderedFile(PurePosixPath("agents/reviewer.toml"), content, "agent:reviewer"),
    )

    assert validate_rendered_workflow("codex", files, ()) == ()


@pytest.mark.parametrize(
    "content",
    [
        b"Skill without frontmatter.\n",
        b"---\nname: other\ndescription: Check safely\n---\n\nBody.\n",
        b"---\nname: check\n---\n\nBody.\n",
        b"---\nname: check\ndescription: Check safely\n---\n",
    ],
)
def test_skill_native_validation_rejects_invalid_entrypoint(content: bytes) -> None:
    files = (
        RenderedFile(PurePosixPath("AGENTS.md"), b"instructions", "instructions"),
        RenderedFile(PurePosixPath("CLAUDE.md"), b"instructions", "companion"),
        RenderedFile(PurePosixPath("skills/check/SKILL.md"), content, "skill:check"),
    )

    issues = validate_rendered_workflow("opencode", files, ())

    assert any(item.identifier == "invalid-skill:skill:check" for item in issues)


@pytest.mark.parametrize(
    "content",
    [
        b"export {\n",
        b"export const Ready = {};\n",
        b"export const Ready = async () => ({ event: async () => {} );\n",
        b"export const Ready = async () => ({ event: });\n",
        b"export const Ready = async () => ({ event: /unterminated });\n",
        b'export const Ready = async () => 42; const claim = "return {";\n',
        b"export const Ready = async () => ({});\n",
        b"export const Ready = async () => ({ unrelated: async () => {} });\n",
        b"export const Ready = async () => ({ event: true });\n",
    ],
)
def test_opencode_plugin_native_validation_rejects_invalid_module_structure(
    content: bytes,
) -> None:
    files = (
        RenderedFile(PurePosixPath("AGENTS.md"), b"instructions", "instructions"),
        RenderedFile(PurePosixPath("CLAUDE.md"), b"instructions", "companion"),
        RenderedFile(PurePosixPath("plugins/ready-notify.js"), content, "hook:ready"),
    )

    issues = validate_rendered_workflow("opencode", files, ())

    assert any(item.identifier == "invalid-hook:hook:ready" for item in issues)


def test_opencode_source_reader_rejects_invalid_plugin_structure(tmp_path: Path) -> None:
    _write(tmp_path, "AGENTS.md", "Instructions.\n")
    _write(tmp_path, "plugins/ready-notify.js", "export {\n")

    source = read_native_workflow(tmp_path, "opencode")

    assert any(item.identifier == "hook-javascript:ready" for item in source.validation_issues)


@pytest.mark.parametrize(
    ("name", "path", "expected_key", "swapped_key"),
    [
        ("startup", "plugins/session-start-status.js", "event", '"tool.execute.before"'),
        ("security", "plugins/security-reminder.js", '"tool.execute.before"', "event"),
        ("ready", "plugins/ready-notify.js", "event", '"tool.execute.before"'),
    ],
)
def test_opencode_source_reader_requires_hook_key_for_owned_plugin_path(
    tmp_path: Path,
    name: str,
    path: str,
    expected_key: str,
    swapped_key: str,
) -> None:
    _write(tmp_path, "AGENTS.md", "Instructions.\n")
    plugin = _write(
        tmp_path,
        path,
        f"export const Plugin = async () => ({{ {expected_key}: async () => {{}} }});\n",
    )

    valid = read_native_workflow(tmp_path, "opencode")

    assert not any(
        item.identifier == f"hook-javascript:{name}" for item in valid.validation_issues
    )

    plugin.write_text(
        f"export const Plugin = async () => ({{ {swapped_key}: async () => {{}} }});\n"
    )
    invalid = read_native_workflow(tmp_path, "opencode")

    assert any(
        item.identifier == f"hook-javascript:{name}" for item in invalid.validation_issues
    )


@pytest.mark.parametrize(
    ("name", "path", "expected_key", "swapped_key"),
    [
        ("startup", "plugins/session-start-status.js", "event", '"tool.execute.before"'),
        ("security", "plugins/security-reminder.js", '"tool.execute.before"', "event"),
        ("ready", "plugins/ready-notify.js", "event", '"tool.execute.before"'),
    ],
)
def test_opencode_native_validation_requires_hook_key_for_owned_plugin_path(
    name: str,
    path: str,
    expected_key: str,
    swapped_key: str,
) -> None:
    base = (
        RenderedFile(PurePosixPath("AGENTS.md"), b"instructions", "instructions"),
        RenderedFile(PurePosixPath("CLAUDE.md"), b"instructions", "companion"),
    )
    valid_plugin = RenderedFile(
        PurePosixPath(path),
        f"export const Plugin = async () => ({{ {expected_key}: async () => {{}} }});\n".encode(),
        f"hook:{name}",
    )
    swapped_plugin = RenderedFile(
        PurePosixPath(path),
        f"export const Plugin = async () => ({{ {swapped_key}: async () => {{}} }});\n".encode(),
        f"hook:{name}",
    )

    assert validate_rendered_workflow("opencode", (*base, valid_plugin), ()) == ()
    issues = validate_rendered_workflow("opencode", (*base, swapped_plugin), ())

    assert any(item.identifier == f"invalid-hook:hook:{name}" for item in issues)


def test_opencode_plugin_native_validation_accepts_regex_and_single_parameter_arrow() -> None:
    files = (
        RenderedFile(PurePosixPath("AGENTS.md"), b"instructions", "instructions"),
        RenderedFile(PurePosixPath("CLAUDE.md"), b"instructions", "companion"),
        RenderedFile(
            PurePosixPath("plugins/security-reminder.js"),
            (
                b"export const Security = async context => ({\n"
                b'  "tool.execute.before": async () => { const deny = /}/; return context; },\n'
                b"});\n"
            ),
            "hook:security",
        ),
    )

    assert validate_rendered_workflow("opencode", files, ()) == ()


def test_ownership_queries_share_the_closed_matrix() -> None:
    assert path_is_owned("codex", PurePosixPath("skills/command-ship/SKILL.md"))
    assert not path_is_owned("codex", PurePosixPath("commands/ship.md"))
    assert fragment_is_owned("claude", PurePosixPath("settings.json"), ("hooks", "Stop"))
    assert not fragment_is_owned("claude", PurePosixPath("settings.json"), ("statusLine",))
    assert native_only_path_is_owned("claude", PurePosixPath("commands/codex-review.md"))
    assert not native_only_path_is_owned("opencode", PurePosixPath("commands/codex-review.md"))
    assert not native_only_path_is_owned("claude", PurePosixPath("agents/reviewer.md"))


@pytest.mark.parametrize(
    ("value", "safe"),
    [("context/file.md", True), ("../outside", False), ("/absolute", False), (".", False)],
)
def test_relative_path_validation(value: str, *, safe: bool) -> None:
    assert is_safe_relative_path(PurePosixPath(value)) is safe
