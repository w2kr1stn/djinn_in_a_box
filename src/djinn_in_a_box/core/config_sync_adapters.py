from __future__ import annotations

import ast
import json
import os
import re
import stat
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import cast

import tomli_w
import tree_sitter_javascript
from tree_sitter import Language, Node, Parser

from djinn_in_a_box.config.models import ConfigSyncSource

ADAPTER_REVISION = 3


class ArtifactKind(StrEnum):
    INSTRUCTIONS = "instructions"
    AGENT = "agent"
    SKILL = "skill"
    COMMAND = "command"
    CONTEXT = "context"
    HOOK = "hook"


@dataclass(frozen=True)
class WorkflowArtifact:
    kind: ArtifactKind
    name: str
    source_tool: ConfigSyncSource
    source_path: PurePosixPath
    content: bytes
    body: bytes | None = None
    metadata: bytes | None = None
    description: str = ""
    nonportable_metadata: tuple[str, ...] = ()
    executable: bool = False
    native_only_for: ConfigSyncSource | None = None

    @property
    def identifier(self) -> str:
        return f"{self.kind}:{self.name}:{self.source_path.as_posix()}"


@dataclass(frozen=True)
class RenderedFile:
    relative_path: PurePosixPath
    content: bytes
    artifact_id: str
    executable: bool = False


@dataclass(frozen=True)
class SettingsFragment:
    carrier_path: PurePosixPath
    key_path: tuple[str, ...]
    value_json: bytes
    artifact_id: str


@dataclass(frozen=True)
class UnresolvedItem:
    identifier: str
    reason: str
    source_path: PurePosixPath
    source_bytes: bytes
    metadata: bytes | None = None
    target_tool: ConfigSyncSource | None = None
    executable: bool = False


@dataclass(frozen=True)
class AllowedSettingsFragment:
    carrier_path: PurePosixPath
    key_path: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactOutputContract:
    file_paths: tuple[PurePosixPath, ...]
    settings_fragments: tuple[AllowedSettingsFragment, ...]


@dataclass(frozen=True)
class ValidationIssue:
    identifier: str
    message: str
    relative_path: PurePosixPath | None = None


@dataclass(frozen=True)
class AdapterReadResult:
    tool: ConfigSyncSource
    artifacts: tuple[WorkflowArtifact, ...]
    unresolved: tuple[UnresolvedItem, ...]
    validation_issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True)
class AdapterRenderResult:
    source_tool: ConfigSyncSource
    target_tool: ConfigSyncSource
    files: tuple[RenderedFile, ...]
    settings_fragments: tuple[SettingsFragment, ...]
    unresolved: tuple[UnresolvedItem, ...]
    validation_issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True)
class HookSpec:
    name: str
    script_path: PurePosixPath
    carrier_path: PurePosixPath | None = None
    event: str | None = None


@dataclass(frozen=True)
class ToolOwnership:
    instruction_path: PurePosixPath
    instruction_companion: PurePosixPath
    agent_suffix: str
    hooks: tuple[HookSpec, ...]


_p = PurePosixPath
OWNERSHIP_MATRIX: Mapping[ConfigSyncSource, ToolOwnership] = {
    "claude": ToolOwnership(
        _p("CLAUDE.md"),
        _p("AGENTS.md"),
        ".md",
        (
            HookSpec(
                "startup",
                _p("scripts/session-start-status.py"),
                _p("settings.json"),
                "SessionStart",
            ),
            HookSpec(
                "security", _p("security_reminder_hook.py"), _p("settings.json"), "PreToolUse"
            ),
            HookSpec("ready", _p("ready_notify_hook.py"), _p("settings.json"), "Stop"),
        ),
    ),
    "codex": ToolOwnership(
        _p("AGENTS.md"),
        _p("CLAUDE.md"),
        ".toml",
        (
            HookSpec(
                "startup", _p("scripts/session-start-status.py"), _p("hooks.json"), "SessionStart"
            ),
            HookSpec("security", _p("hooks/security_guard.py"), _p("hooks.json"), "PreToolUse"),
            HookSpec("ready", _p("hooks/ready_notify.py"), _p("hooks.json"), "Stop"),
        ),
    ),
    "opencode": ToolOwnership(
        _p("AGENTS.md"),
        _p("CLAUDE.md"),
        ".md",
        (
            HookSpec("startup", _p("plugins/session-start-status.js")),
            HookSpec("security", _p("plugins/security-reminder.js")),
            HookSpec("ready", _p("plugins/ready-notify.js")),
        ),
    ),
}

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MARKDOWN_AGENT_KEYS = frozenset({"name", "description"})
_MARKDOWN_COMMAND_KEYS = frozenset({"description"})
_CODEX_AGENT_PORTABLE_KEYS = frozenset({"name", "description", "developer_instructions"})
_CODEX_AGENT_KEYS = _CODEX_AGENT_PORTABLE_KEYS | frozenset(
    {"model", "model_reasoning_effort", "sandbox_mode"}
)
_CLAUDE_NATIVE_COMMAND_KEYS = frozenset({"description", "argument-hint", "allowed-tools"})
_RUNTIME_SKILL_VARIANTS: tuple[Mapping[ConfigSyncSource, str], ...] = (
    {
        "claude": "convergence-loop",
        "codex": "convergence-loop",
        "opencode": "convergence-loop",
    },
    {
        "claude": "claude-md-management",
        "codex": "agents-md-management",
        "opencode": "agents-md-management",
    },
    {
        "claude": "update-claude-md",
        "codex": "update-agents-md",
        "opencode": "update-agents-md",
    },
    {
        "claude": "session-bootstrap",
        "codex": "session-bootstrap",
        "opencode": "session-bootstrap",
    },
)
_NATIVE_COMMANDS: Mapping[str, ConfigSyncSource] = {"codex-review": "claude"}
_PATH_MARKERS: Mapping[ConfigSyncSource, tuple[bytes, ...]] = {
    "claude": (b"~/.claude/", b"$HOME/.claude/", b"~/.claude_seed/"),
    "codex": (b"~/.codex/", b"$HOME/.codex/", b"${CODEX_HOME"),
    "opencode": (b"~/.config/opencode/", b"$HOME/.config/opencode/"),
}
_JAVASCRIPT_LANGUAGE = Language(tree_sitter_javascript.language())
_OPENCODE_HOOK_PROPERTY_LITERALS: Mapping[PurePosixPath, frozenset[bytes]] = {
    _p("plugins/session-start-status.js"): frozenset({b"event", b'"event"', b"'event'"}),
    _p("plugins/security-reminder.js"): frozenset(
        {b'"tool.execute.before"', b"'tool.execute.before'"}
    ),
    _p("plugins/ready-notify.js"): frozenset({b"event", b'"event"', b"'event'"}),
}


def read_native_workflow(root: Path, tool: ConfigSyncSource) -> AdapterReadResult:
    root = root.resolve()
    owned = OWNERSHIP_MATRIX[tool]
    artifacts: list[WorkflowArtifact] = []
    unresolved: list[UnresolvedItem] = []
    issues: list[ValidationIssue] = []

    instructions = _read_file(root, owned.instruction_path, issues)
    if instructions is None:
        issues.append(
            _issue(
                "instructions:missing", "Missing native root instructions.", owned.instruction_path
            )
        )
    else:
        try:
            instructions.content.decode()
        except UnicodeDecodeError:
            issues.append(
                _issue(
                    "instructions:utf8",
                    "Root instructions must be UTF-8.",
                    instructions.relative_path,
                )
            )
        artifacts.append(_artifact(tool, ArtifactKind.INSTRUCTIONS, "global", instructions))

    for item in _files_below(root, _p("agents"), issues):
        if len(item.relative_path.parts) == 2 and item.relative_path.suffix == owned.agent_suffix:
            artifact = _read_agent(tool, item, issues)
            if artifact is not None:
                artifacts.append(artifact)

    skill_groups: dict[str, list[_ReadFile]] = {}
    for item in _files_below(root, _p("skills"), issues):
        if len(item.relative_path.parts) >= 3:
            skill_groups.setdefault(item.relative_path.parts[1], []).append(item)
    for name, items in sorted(skill_groups.items()):
        if tool == "codex" and name.startswith("command-"):
            command = _read_codex_command(tool, name, items, issues)
            if command is not None:
                artifacts.append(command)
                if len(items) > 1:
                    unresolved.append(
                        _unresolved(command, "Command support assets need adaptation.")
                    )
            continue
        if not _valid_skill_name(name):
            issues.append(
                _issue(f"skill-name:{name}", "Skill name is unsafe.", _p("skills") / name)
            )
            continue
        expected_entrypoint = _p("skills") / name / "SKILL.md"
        entrypoint = next(
            (item for item in items if item.relative_path == expected_entrypoint), None
        )
        if entrypoint is None:
            issues.append(
                _issue(
                    f"skill-entrypoint:{name}", "Skill is missing SKILL.md.", _p("skills") / name
                )
            )
            continue
        if not _valid_skill_content(entrypoint.content, name):
            issues.append(
                _issue(
                    f"skill-fields:{name}",
                    "Skill frontmatter, description, or body is invalid.",
                    entrypoint.relative_path,
                )
            )
            continue
        expected_variant = _runtime_skill_target_name(name, tool)
        if expected_variant is not None and expected_variant != name:
            issues.append(
                _issue(
                    f"skill-variant:{name}",
                    f"Runtime-specific skill must use the native name {expected_variant}.",
                    entrypoint.relative_path,
                )
            )
            continue
        artifacts.extend(_artifact(tool, ArtifactKind.SKILL, name, item) for item in items)

    if tool != "codex":
        for item in _files_below(root, _p("commands"), issues):
            if len(item.relative_path.parts) == 2 and item.relative_path.suffix == ".md":
                command = _read_markdown_command(tool, item, issues)
                if command is not None:
                    artifacts.append(command)

    excluded_hooks = {hook.script_path for hook in owned.hooks}
    for prefix in (_p("context"), _p("scripts")):
        for item in _files_below(root, prefix, issues):
            if item.relative_path not in excluded_hooks:
                artifacts.append(_artifact(tool, ArtifactKind.CONTEXT, prefix.name, item))

    _read_hooks(root, tool, owned, artifacts, unresolved, issues)
    for artifact in artifacts:
        if artifact.nonportable_metadata:
            unresolved.append(
                _unresolved(
                    artifact,
                    f"Nonportable metadata: {', '.join(artifact.nonportable_metadata)}.",
                    metadata=artifact.metadata,
                )
            )
        if (
            artifact.native_only_for is not None
            and artifact.source_tool != artifact.native_only_for
        ):
            unresolved.append(
                _unresolved(
                    artifact,
                    f"Surface is native to {artifact.native_only_for}, not {artifact.source_tool}.",
                    metadata=artifact.metadata,
                )
            )
    return AdapterReadResult(
        tool,
        tuple(sorted(artifacts, key=lambda item: (item.kind, item.name, item.source_path))),
        tuple(sorted(unresolved, key=_unresolved_key)),
        tuple(sorted(issues, key=_issue_key)),
    )


def render_native_workflow(
    source: AdapterReadResult, target_tool: ConfigSyncSource
) -> AdapterRenderResult:
    if source.tool == target_tool:
        raise ValueError("Source and target tools must differ.")
    target = OWNERSHIP_MATRIX[target_tool]
    files: list[RenderedFile] = []
    fragments: list[SettingsFragment] = []
    unresolved = [_retarget(item, target_tool) for item in source.unresolved]

    for artifact in source.artifacts:
        if artifact.native_only_for is not None:
            continue
        target_skill_name = (
            _runtime_skill_target_name(artifact.name, target_tool)
            if artifact.kind is ArtifactKind.SKILL
            else None
        )
        if target_skill_name is not None:
            unresolved.append(
                _unresolved(
                    artifact,
                    f"Runtime-specific skill variant needs adaptation as {target_skill_name}.",
                    target_tool,
                )
            )
            continue
        if artifact.kind is not ArtifactKind.HOOK and any(
            marker in artifact.content for marker in _PATH_MARKERS[source.tool]
        ):
            unresolved.append(
                _unresolved(artifact, "Source-native paths need adaptation.", target_tool)
            )

        if artifact.kind is ArtifactKind.INSTRUCTIONS:
            files.extend(
                (
                    _rendered(target.instruction_path, artifact),
                    _rendered(target.instruction_companion, artifact),
                )
            )
        elif artifact.kind is ArtifactKind.AGENT:
            rendered = _render_agent(artifact, target_tool)
            if rendered is None:
                unresolved.append(
                    _unresolved(artifact, "Agent syntax needs adaptation.", target_tool)
                )
            else:
                files.append(rendered)
        elif artifact.kind is ArtifactKind.SKILL:
            files.append(
                _rendered(
                    _p("skills") / artifact.name / _p(*artifact.source_path.parts[2:]), artifact
                )
            )
        elif artifact.kind is ArtifactKind.COMMAND:
            rendered = _render_command(artifact, target_tool)
            if rendered is None:
                unresolved.append(
                    _unresolved(artifact, "Command syntax needs adaptation.", target_tool)
                )
            else:
                files.append(rendered)
        elif artifact.kind is ArtifactKind.CONTEXT:
            files.append(_rendered(artifact.source_path, artifact))
        elif artifact.kind is ArtifactKind.HOOK:
            hook_file, hook_fragment, hook_gap = _render_hook(artifact, target_tool)
            if hook_file is not None:
                files.append(hook_file)
            if hook_fragment is not None:
                fragments.append(hook_fragment)
            if hook_gap is not None:
                unresolved.append(hook_gap)

    if target_tool == "codex":
        fragments.append(
            SettingsFragment(
                _p("config.toml"), ("project_doc_fallback_filenames",), b'["CLAUDE.md"]', "bridge"
            )
        )
    rendered_files = tuple(sorted(files, key=lambda item: item.relative_path))
    rendered_fragments = tuple(
        sorted(fragments, key=lambda item: (item.carrier_path, item.key_path))
    )
    return AdapterRenderResult(
        source.tool,
        target_tool,
        rendered_files,
        rendered_fragments,
        tuple(sorted(_deduplicate(unresolved), key=_unresolved_key)),
        validate_rendered_workflow(target_tool, rendered_files, rendered_fragments),
    )


def validate_rendered_workflow(
    tool: ConfigSyncSource,
    files: Iterable[RenderedFile],
    settings_fragments: Iterable[SettingsFragment],
) -> tuple[ValidationIssue, ...]:
    owned = OWNERSHIP_MATRIX[tool]
    issues: list[ValidationIssue] = []
    paths: set[PurePosixPath] = set()
    for item in files:
        if not is_safe_relative_path(item.relative_path):
            issues.append(
                _issue(
                    f"unsafe-path:{item.artifact_id}",
                    "Rendered path is unsafe.",
                    item.relative_path,
                )
            )
        if item.relative_path in paths:
            issues.append(
                _issue(
                    f"duplicate-path:{item.relative_path}",
                    "Rendered path is duplicated.",
                    item.relative_path,
                )
            )
        paths.add(item.relative_path)
        if not path_is_owned(tool, item.relative_path):
            issues.append(
                _issue(
                    f"unowned-path:{item.artifact_id}",
                    "Rendered path is not owned.",
                    item.relative_path,
                )
            )
        _validate_native_file(tool, item, issues)
    for path in (owned.instruction_path, owned.instruction_companion):
        if path not in paths:
            issues.append(
                _issue(
                    f"instructions:missing:{path}", "Rendered instruction form is missing.", path
                )
            )

    seen: set[tuple[PurePosixPath, tuple[str, ...]]] = set()
    for fragment in settings_fragments:
        key = (fragment.carrier_path, fragment.key_path)
        if key in seen:
            issues.append(
                _issue(
                    f"duplicate-fragment:{fragment.artifact_id}", "Settings fragment is duplicated."
                )
            )
        seen.add(key)
        if not fragment_is_owned(tool, *key):
            issues.append(
                _issue(
                    f"unowned-fragment:{fragment.artifact_id}", "Settings fragment is not owned."
                )
            )
        try:
            json.loads(fragment.value_json)
        except (json.JSONDecodeError, UnicodeDecodeError):
            issues.append(
                _issue(
                    f"invalid-fragment:{fragment.artifact_id}", "Settings fragment is invalid JSON."
                )
            )
    return tuple(sorted(issues, key=_issue_key))


def is_safe_relative_path(path: PurePosixPath) -> bool:
    return (
        not path.is_absolute()
        and bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


@dataclass(frozen=True)
class _ReadFile:
    relative_path: PurePosixPath
    content: bytes
    executable: bool


def _read_file(root: Path, path: PurePosixPath, issues: list[ValidationIssue]) -> _ReadFile | None:
    if not is_safe_relative_path(path):
        issues.append(_issue(f"unsafe-path:{path}", "Allowlisted path is unsafe.", path))
        return None
    candidate = root.joinpath(*path.parts)
    if not candidate.exists() and not candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as error:
        issues.append(
            _issue(f"unreadable-path:{path}", f"Path cannot resolve: {type(error).__name__}.", path)
        )
        return None
    if not resolved.is_relative_to(root):
        issues.append(_issue(f"external-symlink:{path}", "Path resolves outside its root.", path))
        return None
    if not resolved.is_file():
        return None
    try:
        return _ReadFile(path, resolved.read_bytes(), bool(resolved.stat().st_mode & stat.S_IXUSR))
    except OSError as error:
        issues.append(
            _issue(f"unreadable-file:{path}", f"File cannot be read: {type(error).__name__}.", path)
        )
        return None


def _files_below(
    root: Path, prefix: PurePosixPath, issues: list[ValidationIssue]
) -> tuple[_ReadFile, ...]:
    directory = root.joinpath(*prefix.parts)
    if not directory.exists() and not directory.is_symlink():
        return ()
    try:
        resolved = directory.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        issues.append(
            _issue(f"unreadable-tree:{prefix}", "Allowlisted tree cannot resolve.", prefix)
        )
        return ()
    if not resolved.is_relative_to(root):
        issues.append(
            _issue(f"external-symlink:{prefix}", "Tree resolves outside its root.", prefix)
        )
        return ()
    if not resolved.is_dir():
        return ()

    paths: list[Path] = []
    for current, directory_names, file_names in os.walk(directory, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(directory_names):
            child = current_path / name
            relative = _p(child.relative_to(root).as_posix())
            if child.is_symlink():
                try:
                    target = child.resolve(strict=True)
                except (FileNotFoundError, OSError, RuntimeError):
                    issues.append(
                        _issue(
                            f"unreadable-tree:{relative}",
                            "Symlinked directory cannot resolve.",
                            relative,
                        )
                    )
                else:
                    message = (
                        "Symlinked directory escapes its root."
                        if not target.is_relative_to(root)
                        else "Directory aliases and cycles are unsupported."
                    )
                    issues.append(_issue(f"symlink-directory:{relative}", message, relative))
            else:
                kept.append(name)
        directory_names[:] = kept
        paths.extend(current_path / name for name in file_names)

    items: list[_ReadFile] = []
    targets: dict[Path, PurePosixPath] = {}
    for candidate in sorted(paths, key=lambda path: (path.is_symlink(), path.as_posix())):
        relative = _p(candidate.relative_to(root).as_posix())
        item = _read_file(root, relative, issues)
        if item is None:
            continue
        target = candidate.resolve()
        if target in targets:
            issues.append(
                _issue(f"symlink-alias:{relative}", f"Path aliases {targets[target]}.", relative)
            )
            continue
        targets[target] = relative
        items.append(item)
    return tuple(sorted(items, key=lambda item: item.relative_path))


def _artifact(
    tool: ConfigSyncSource,
    kind: ArtifactKind,
    name: str,
    item: _ReadFile,
    *,
    body: bytes | None = None,
    metadata: bytes | None = None,
    description: str = "",
    nonportable_metadata: tuple[str, ...] = (),
    native_only_for: ConfigSyncSource | None = None,
) -> WorkflowArtifact:
    return WorkflowArtifact(
        kind,
        name,
        tool,
        item.relative_path,
        item.content,
        body,
        metadata,
        description,
        nonportable_metadata,
        item.executable,
        native_only_for,
    )


def _read_agent(
    tool: ConfigSyncSource, item: _ReadFile, issues: list[ValidationIssue]
) -> WorkflowArtifact | None:
    name = item.relative_path.stem
    if not _NAME.fullmatch(name):
        issues.append(_issue(f"agent-name:{name}", "Agent name is unsafe.", item.relative_path))
        return None
    if tool == "codex":
        try:
            data = cast(dict[str, object], tomllib.loads(item.content.decode()))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            issues.append(
                _issue(f"agent-toml:{name}", "Codex agent must be UTF-8 TOML.", item.relative_path)
            )
            return None
        if not _valid_codex_agent(data, name):
            issues.append(
                _issue(
                    f"agent-fields:{name}", "Codex agent fields are invalid.", item.relative_path
                )
            )
            return None
        description = cast(str, data["description"])
        prompt = cast(str, data["developer_instructions"])
        return _artifact(
            tool,
            ArtifactKind.AGENT,
            name,
            item,
            body=prompt.encode(),
            metadata=item.content,
            description=description,
            nonportable_metadata=tuple(sorted(set(data) - _CODEX_AGENT_PORTABLE_KEYS)),
        )
    parsed = _markdown_parts(item.content)
    if parsed is None:
        return _artifact(
            tool,
            ArtifactKind.AGENT,
            name,
            item,
            metadata=item.content,
            nonportable_metadata=("complex-frontmatter",),
        )
    metadata_bytes, metadata, body = parsed
    if metadata.get("name", name) != name or not metadata.get("description"):
        issues.append(
            _issue(f"agent-fields:{name}", "Markdown agent fields are invalid.", item.relative_path)
        )
        return None
    return _artifact(
        tool,
        ArtifactKind.AGENT,
        name,
        item,
        body=body,
        metadata=metadata_bytes,
        description=metadata["description"],
        nonportable_metadata=tuple(sorted(set(metadata) - _MARKDOWN_AGENT_KEYS)),
    )


def _read_markdown_command(
    tool: ConfigSyncSource, item: _ReadFile, issues: list[ValidationIssue]
) -> WorkflowArtifact | None:
    name = item.relative_path.stem
    if not _NAME.fullmatch(name):
        issues.append(_issue(f"command-name:{name}", "Command name is unsafe.", item.relative_path))
        return None
    parsed = _markdown_parts(item.content)
    owner = _NATIVE_COMMANDS.get(name)
    if parsed is None:
        return _artifact(
            tool,
            ArtifactKind.COMMAND,
            name,
            item,
            metadata=item.content,
            nonportable_metadata=("complex-frontmatter",),
            native_only_for=owner,
        )
    metadata_bytes, metadata, body = parsed
    if owner == tool:
        if not _valid_native_command(metadata, body, owner):
            issues.append(
                _issue(
                    f"command-fields:{name}",
                    "Native command frontmatter or body is invalid.",
                    item.relative_path,
                )
            )
            return None
        nonportable_metadata: tuple[str, ...] = ()
    else:
        nonportable_metadata = tuple(sorted(set(metadata) - _MARKDOWN_COMMAND_KEYS))
    return _artifact(
        tool,
        ArtifactKind.COMMAND,
        name,
        item,
        body=body,
        metadata=metadata_bytes,
        description=metadata.get("description", ""),
        nonportable_metadata=nonportable_metadata,
        native_only_for=owner,
    )


def _read_codex_command(
    tool: ConfigSyncSource, directory: str, items: list[_ReadFile], issues: list[ValidationIssue]
) -> WorkflowArtifact | None:
    name = directory.removeprefix("command-")
    entrypoint = next((item for item in items if item.relative_path.name == "SKILL.md"), None)
    if entrypoint is None:
        issues.append(_issue(f"command-entrypoint:{name}", "Command skill is missing SKILL.md."))
        return None
    parsed = _markdown_parts(entrypoint.content)
    if parsed is None:
        return _artifact(
            tool,
            ArtifactKind.COMMAND,
            name,
            entrypoint,
            metadata=entrypoint.content,
            nonportable_metadata=("complex-frontmatter",),
            native_only_for=_NATIVE_COMMANDS.get(name),
        )
    metadata_bytes, metadata, body = parsed
    if metadata.get("name", f"command-{name}") != f"command-{name}":
        issues.append(_issue(f"command-name:{name}", "Command skill name does not match its path."))
        return None
    return _artifact(
        tool,
        ArtifactKind.COMMAND,
        name,
        entrypoint,
        body=body,
        metadata=metadata_bytes,
        description=metadata.get("description", ""),
        nonportable_metadata=tuple(sorted(set(metadata) - {"name", "description"})),
        native_only_for=_NATIVE_COMMANDS.get(name),
    )


def _read_hooks(
    root: Path,
    tool: ConfigSyncSource,
    owned: ToolOwnership,
    artifacts: list[WorkflowArtifact],
    unresolved: list[UnresolvedItem],
    issues: list[ValidationIssue],
) -> None:
    carriers: dict[PurePosixPath, Mapping[str, object] | None] = {}
    for hook in owned.hooks:
        script = _read_file(root, hook.script_path, issues)
        if script is not None and script.relative_path.suffix == ".py":
            try:
                ast.parse(script.content)
            except (SyntaxError, UnicodeDecodeError):
                issues.append(
                    _issue(f"hook-python:{hook.name}", "Python hook is invalid.", hook.script_path)
                )
        if (
            script is not None
            and script.relative_path.suffix == ".js"
            and not _valid_opencode_plugin(
                script.content,
                _OPENCODE_HOOK_PROPERTY_LITERALS[script.relative_path],
            )
        ):
            issues.append(
                _issue(
                    f"hook-javascript:{hook.name}",
                    "OpenCode hook plugin structure is invalid.",
                    hook.script_path,
                )
            )
        if hook.carrier_path is None:
            if script is not None:
                artifacts.append(_artifact(tool, ArtifactKind.HOOK, hook.name, script))
            continue
        if hook.carrier_path not in carriers:
            carriers[hook.carrier_path] = _read_json(root, hook.carrier_path, issues)
        registration = _nested(carriers[hook.carrier_path], "hooks", cast(str, hook.event))
        if script is None and registration is None:
            continue
        if script is None or registration is None:
            issues.append(
                _issue(f"hook-incomplete:{hook.name}", "Hook script and registration must coexist.")
            )
            continue
        artifact = _artifact(tool, ArtifactKind.HOOK, hook.name, script)
        artifacts.append(artifact)
        if registration != _hook_registration(tool, hook.name):
            unresolved.append(
                _unresolved(
                    artifact,
                    "Noncanonical hook registration needs adaptation.",
                    metadata=json.dumps(
                        registration, sort_keys=True, separators=(",", ":")
                    ).encode(),
                )
            )


def _read_json(
    root: Path, path: PurePosixPath, issues: list[ValidationIssue]
) -> Mapping[str, object] | None:
    item = _read_file(root, path, issues)
    if item is None:
        return None
    try:
        value: object = json.loads(item.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        issues.append(_issue(f"invalid-json:{path}", "Settings carrier is invalid JSON.", path))
        return None
    if not isinstance(value, dict):
        issues.append(_issue(f"invalid-json:{path}", "Settings carrier must be an object.", path))
        return None
    return cast(dict[str, object], value)


def _nested(mapping: Mapping[str, object] | None, first: str, second: str) -> object | None:
    if mapping is None:
        return None
    nested = mapping.get(first)
    return cast(dict[str, object], nested).get(second) if isinstance(nested, dict) else None


def _markdown_parts(content: bytes) -> tuple[bytes, dict[str, str], bytes] | None:
    try:
        text = content.decode()
    except UnicodeDecodeError:
        return None
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        return None
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace() or ":" not in stripped:
            return None
        key, value = stripped.split(":", 1)
        if key in metadata or not value.strip():
            return None
        metadata[key] = value.strip().strip('"').strip("'")
    return "".join(lines[: end + 1]).encode(), metadata, "".join(lines[end + 1 :]).encode()


def _valid_skill_name(name: str) -> bool:
    return len(name) <= 64 and _SKILL_NAME.fullmatch(name) is not None


def _valid_skill_content(content: bytes, directory_name: str) -> bool:
    parsed = _markdown_parts(content)
    if parsed is None or not _valid_skill_name(directory_name):
        return False
    _metadata_bytes, metadata, body = parsed
    name = metadata.get("name")
    description = metadata.get("description")
    return (
        name == directory_name
        and description is not None
        and bool(description.strip())
        and bool(body.strip())
    )


def _runtime_skill_target_name(name: str, target: ConfigSyncSource) -> str | None:
    for variants in _RUNTIME_SKILL_VARIANTS:
        if name in variants.values():
            return variants[target]
    return None


def _valid_codex_agent(data: Mapping[str, object], path_name: str) -> bool:
    if set(data) - _CODEX_AGENT_KEYS or not set(data) >= _CODEX_AGENT_PORTABLE_KEYS:
        return False
    for key in data:
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            return False
    return data["name"] == path_name and _NAME.fullmatch(path_name) is not None


def _valid_native_command(
    metadata: Mapping[str, str], body: bytes, owner: ConfigSyncSource | None
) -> bool:
    if owner != "claude" or set(metadata) - _CLAUDE_NATIVE_COMMAND_KEYS:
        return False
    return bool(metadata.get("description", "").strip() and body.strip())


def _valid_opencode_plugin(content: bytes, expected_hook_keys: frozenset[bytes]) -> bool:
    try:
        content.decode()
    except UnicodeDecodeError:
        return False
    try:
        root = Parser(_JAVASCRIPT_LANGUAGE).parse(content).root_node
        if root.has_error:
            return False
    except (TypeError, ValueError):
        return False
    return any(
        _javascript_function_returns_object(function, expected_hook_keys)
        for export in root.named_children
        if export.type == "export_statement"
        for function in _exported_javascript_functions(export)
    )


def _exported_javascript_functions(export: Node) -> tuple[Node, ...]:
    declaration = export.child_by_field_name("declaration")
    if declaration is not None and declaration.type == "function_declaration":
        return (declaration,)
    if declaration is not None and declaration.type in {
        "lexical_declaration",
        "variable_declaration",
    }:
        return tuple(
            value
            for declarator in declaration.named_children
            if declarator.type == "variable_declarator"
            if (value := declarator.child_by_field_name("value")) is not None
            and value.type in {"arrow_function", "function_expression"}
        )
    value = export.child_by_field_name("value")
    if value is not None and value.type in {"arrow_function", "function_expression"}:
        return (value,)
    return ()


def _javascript_function_returns_object(
    function: Node, expected_hook_keys: frozenset[bytes]
) -> bool:
    body = function.child_by_field_name("body")
    if body is None:
        return False
    expression = body
    while expression.type == "parenthesized_expression" and expression.named_child_count == 1:
        expression = expression.named_children[0]
    if expression.type == "object":
        return _javascript_object_has_owned_hook(expression, expected_hook_keys)
    return _javascript_block_returns_object(body, expected_hook_keys)


def _javascript_block_returns_object(node: Node, expected_hook_keys: frozenset[bytes]) -> bool:
    for child in node.named_children:
        if child.type in {"arrow_function", "function_declaration", "function_expression"}:
            continue
        if child.type == "return_statement" and child.named_child_count == 1:
            expression = child.named_children[0]
            while (
                expression.type == "parenthesized_expression" and expression.named_child_count == 1
            ):
                expression = expression.named_children[0]
            if expression.type == "object" and _javascript_object_has_owned_hook(
                expression, expected_hook_keys
            ):
                return True
        if _javascript_block_returns_object(child, expected_hook_keys):
            return True
    return False


def _javascript_object_has_owned_hook(node: Node, expected_hook_keys: frozenset[bytes]) -> bool:
    return any(
        key is not None
        and key.text in expected_hook_keys
        and value is not None
        and value.type in {"arrow_function", "function_expression"}
        for child in node.named_children
        if child.type == "pair"
        for key, value in ((child.child_by_field_name("key"), child.child_by_field_name("value")),)
    )


def _render_agent(artifact: WorkflowArtifact, target: ConfigSyncSource) -> RenderedFile | None:
    if artifact.source_tool != "codex" and target != "codex":
        return _rendered(_p("agents") / f"{artifact.name}.md", artifact)
    if artifact.body is None or artifact.nonportable_metadata:
        return None
    if target == "codex":
        content = tomli_w.dumps(
            {
                "name": artifact.name,
                "description": artifact.description,
                "developer_instructions": artifact.body.decode(),
            },
            multiline_strings=True,
        ).encode()
        return _rendered(_p("agents") / f"{artifact.name}.toml", artifact, content)
    content = _markdown(
        ("name", artifact.name), ("description", artifact.description), body=artifact.body
    )
    return _rendered(_p("agents") / f"{artifact.name}.md", artifact, content)


def _render_command(artifact: WorkflowArtifact, target: ConfigSyncSource) -> RenderedFile | None:
    if artifact.source_tool != "codex" and target != "codex":
        return _rendered(_p("commands") / f"{artifact.name}.md", artifact)
    if artifact.body is None or artifact.nonportable_metadata:
        return None
    if target == "codex":
        content = _markdown(
            ("name", f"command-{artifact.name}"),
            ("description", artifact.description),
            body=artifact.body,
        )
        return _rendered(_p("skills") / f"command-{artifact.name}" / "SKILL.md", artifact, content)
    content = _markdown(("description", artifact.description), body=artifact.body)
    return _rendered(_p("commands") / f"{artifact.name}.md", artifact, content)


def _render_hook(
    artifact: WorkflowArtifact, target: ConfigSyncSource
) -> tuple[RenderedFile | None, SettingsFragment | None, UnresolvedItem | None]:
    hook = next(item for item in OWNERSHIP_MATRIX[target].hooks if item.name == artifact.name)
    fragment: SettingsFragment | None = None
    if hook.carrier_path is not None and hook.event is not None:
        value = json.dumps(
            _hook_registration(target, hook.name), sort_keys=True, separators=(",", ":")
        ).encode()
        fragment = SettingsFragment(
            hook.carrier_path, ("hooks", hook.event), value, artifact.identifier
        )
    return (
        None,
        fragment,
        _unresolved(artifact, "Cross-tool hook script needs semantic adaptation.", target),
    )


def _hook_registration(tool: ConfigSyncSource, name: str) -> object:
    if tool == "claude":
        matcher = "Edit|Write" if name == "security" else ""
        command = {
            "startup": "uv run python3 ~/.claude/scripts/session-start-status.py",
            "security": "uv run python3 ~/.claude_seed/security_reminder_hook.py",
            "ready": "uv run python3 ~/.claude_seed/ready_notify_hook.py",
        }[name]
        return [{"matcher": matcher, "hooks": [{"type": "command", "command": command}]}]
    matcher = {"startup": "startup|resume", "security": "Bash|Edit|Write|apply_patch", "ready": ""}[
        name
    ]
    path = {
        "startup": "scripts/session-start-status.py",
        "security": "hooks/security_guard.py",
        "ready": "hooks/ready_notify.py",
    }[name]
    hook: dict[str, str] = {
        "type": "command",
        "command": f"bash -lc 'uv run python \"${{CODEX_HOME:-$HOME/.codex}}/{path}\"'",
    }
    if name != "ready":
        hook["statusMessage"] = (
            "Checking Codex session status"
            if name == "startup"
            else "Applying Codex security guard"
        )
    return [{"matcher": matcher, "hooks": [hook]}]


def _markdown(*metadata: tuple[str, str], body: bytes) -> bytes:
    header = ["---", *(f"{key}: {json.dumps(value)}" for key, value in metadata), "---", ""]
    return "\n".join(header).encode() + body.lstrip(b"\r\n")


def _rendered(
    path: PurePosixPath, artifact: WorkflowArtifact, content: bytes | None = None
) -> RenderedFile:
    return RenderedFile(
        path,
        artifact.content if content is None else content,
        artifact.identifier,
        artifact.executable,
    )


def path_is_owned(tool: ConfigSyncSource, path: PurePosixPath) -> bool:
    owned = OWNERSHIP_MATRIX[tool]
    return (
        path in {owned.instruction_path, owned.instruction_companion}
        or (
            len(path.parts) == 2 and path.parts[0] == "agents" and path.suffix == owned.agent_suffix
        )
        or (len(path.parts) >= 3 and path.parts[0] == "skills")
        or (
            tool != "codex"
            and len(path.parts) == 2
            and path.parts[0] == "commands"
            and path.suffix == ".md"
        )
        or (len(path.parts) >= 2 and path.parts[0] in {"context", "scripts"})
        or any(path == hook.script_path for hook in owned.hooks)
    )


def native_only_path_is_owned(tool: ConfigSyncSource, path: PurePosixPath) -> bool:
    """Return whether path is the canonical owner path of a static native-only surface."""
    for name, owner in _NATIVE_COMMANDS.items():
        if owner != tool:
            continue
        expected = (
            _p("skills") / f"command-{name}" / "SKILL.md"
            if tool == "codex"
            else _p("commands") / f"{name}.md"
        )
        if path == expected:
            return True
    return False


def _validate_native_file(
    tool: ConfigSyncSource, item: RenderedFile, issues: list[ValidationIssue]
) -> None:
    if item.relative_path.suffix in {".md", ".py", ".js", ".toml"}:
        try:
            item.content.decode()
        except UnicodeDecodeError:
            issues.append(_issue(f"invalid-utf8:{item.artifact_id}", "Native text must be UTF-8."))
            return
    if (
        tool == "codex"
        and len(item.relative_path.parts) == 2
        and item.relative_path.parts[0] == "agents"
    ):
        try:
            data = tomllib.loads(item.content.decode())
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            issues.append(
                _issue(f"invalid-agent:{item.artifact_id}", "Codex agent TOML is invalid.")
            )
        else:
            if not _valid_codex_agent(data, item.relative_path.stem):
                issues.append(
                    _issue(
                        f"invalid-agent:{item.artifact_id}",
                        "Codex agent fields are invalid.",
                        item.relative_path,
                    )
                )
    if (
        tool != "codex"
        and len(item.relative_path.parts) == 2
        and item.relative_path.parts[0] == "agents"
        and item.relative_path.suffix == ".md"
    ):
        name = item.relative_path.stem
        parsed = _markdown_parts(item.content)
        if not _NAME.fullmatch(name) or parsed is None:
            issues.append(
                _issue(
                    f"invalid-agent:{item.artifact_id}",
                    "Markdown agent frontmatter is invalid.",
                    item.relative_path,
                )
            )
        else:
            _metadata_bytes, metadata, body = parsed
            if (
                set(metadata) - _MARKDOWN_AGENT_KEYS
                or metadata.get("name", name) != name
                or not metadata.get("description")
                or not body.strip()
            ):
                issues.append(
                    _issue(
                        f"invalid-agent:{item.artifact_id}",
                        "Markdown agent fields are invalid.",
                        item.relative_path,
                    )
                )
    if (
        len(item.relative_path.parts) == 3
        and item.relative_path.parts[0] == "skills"
        and item.relative_path.name == "SKILL.md"
        and not _valid_skill_content(item.content, item.relative_path.parts[1])
    ):
        issues.append(
            _issue(
                f"invalid-skill:{item.artifact_id}",
                "Skill frontmatter, name, description, or body is invalid.",
                item.relative_path,
            )
        )
    python_hooks = {
        hook.script_path
        for hook in OWNERSHIP_MATRIX[tool].hooks
        if hook.script_path.suffix == ".py"
    }
    if item.relative_path in python_hooks:
        try:
            ast.parse(item.content)
        except (SyntaxError, UnicodeDecodeError):
            issues.append(
                _issue(f"invalid-hook:{item.artifact_id}", "Python hook syntax is invalid.")
            )
    javascript_hooks = {
        hook.script_path: _OPENCODE_HOOK_PROPERTY_LITERALS[hook.script_path]
        for hook in OWNERSHIP_MATRIX[tool].hooks
        if hook.script_path.suffix == ".js"
    }
    if item.relative_path in javascript_hooks and not _valid_opencode_plugin(
        item.content, javascript_hooks[item.relative_path]
    ):
        issues.append(
            _issue(
                f"invalid-hook:{item.artifact_id}",
                "OpenCode hook must export a plugin function that returns hooks.",
                item.relative_path,
            )
        )


def fragment_is_owned(
    tool: ConfigSyncSource, carrier_path: PurePosixPath, key_path: tuple[str, ...]
) -> bool:
    values: set[tuple[PurePosixPath, tuple[str, ...]]] = {
        (hook.carrier_path, ("hooks", hook.event))
        for hook in OWNERSHIP_MATRIX[tool].hooks
        if hook.carrier_path is not None and hook.event is not None
    }
    if tool == "codex":
        values.add((_p("config.toml"), ("project_doc_fallback_filenames",)))
    return (carrier_path, key_path) in values


def allowed_outputs_for_unresolved(item: UnresolvedItem) -> ArtifactOutputContract:
    """Return the closed target surface for one unresolved artifact."""
    target = item.target_tool
    if target is None or not is_safe_relative_path(item.source_path):
        raise ValueError("Unresolved artifact has no safe target contract.")
    try:
        kind_value, name, path_value = item.identifier.split(":", 2)
        kind = ArtifactKind(kind_value)
    except (ValueError, TypeError):
        raise ValueError("Unresolved artifact identifier is invalid.") from None
    if path_value != item.source_path.as_posix():
        raise ValueError("Unresolved artifact identifier does not match its path.")

    owned = OWNERSHIP_MATRIX[target]
    paths: tuple[PurePosixPath, ...]
    fragments: tuple[AllowedSettingsFragment, ...] = ()
    if kind is ArtifactKind.INSTRUCTIONS and name == "global":
        instruction_paths = {ownership.instruction_path for ownership in OWNERSHIP_MATRIX.values()}
        if item.source_path not in instruction_paths:
            raise ValueError("Unresolved instruction path is invalid.")
        paths = (owned.instruction_path, owned.instruction_companion)
    elif kind is ArtifactKind.AGENT and _NAME.fullmatch(name):
        if (
            item.source_path.parent != _p("agents")
            or item.source_path.stem != name
            or item.source_path.suffix not in {".md", ".toml"}
        ):
            raise ValueError("Unresolved agent path is invalid.")
        paths = (_p("agents") / f"{name}{owned.agent_suffix}",)
    elif kind is ArtifactKind.SKILL and _NAME.fullmatch(name):
        if len(item.source_path.parts) < 3 or item.source_path.parts[:2] != ("skills", name):
            raise ValueError("Unresolved skill path is invalid.")
        target_name = _runtime_skill_target_name(name, target) or name
        paths = (_p("skills") / target_name / _p(*item.source_path.parts[2:]),)
    elif kind is ArtifactKind.COMMAND and _NAME.fullmatch(name):
        markdown_path = _p("commands") / f"{name}.md"
        codex_path = _p("skills") / f"command-{name}" / "SKILL.md"
        if item.source_path not in {markdown_path, codex_path}:
            raise ValueError("Unresolved command path is invalid.")
        paths = (codex_path if target == "codex" else markdown_path,)
    elif kind is ArtifactKind.CONTEXT and name in {"context", "scripts"}:
        if item.source_path.parts[0] != name:
            raise ValueError("Unresolved support path is invalid.")
        paths = (item.source_path,)
    elif kind is ArtifactKind.HOOK:
        source_hook_paths = {
            hook.script_path
            for ownership in OWNERSHIP_MATRIX.values()
            for hook in ownership.hooks
            if hook.name == name
        }
        if item.source_path not in source_hook_paths:
            raise ValueError("Unresolved hook path is invalid.")
        try:
            hook = next(candidate for candidate in owned.hooks if candidate.name == name)
        except StopIteration:
            raise ValueError("Unresolved hook name is invalid.") from None
        paths = (hook.script_path,)
    else:
        raise ValueError("Unresolved artifact has no target contract.")

    if any(not is_safe_relative_path(path) or not path_is_owned(target, path) for path in paths):
        raise ValueError("Unresolved artifact file contract is not owned.")
    if any(
        not fragment_is_owned(target, fragment.carrier_path, fragment.key_path)
        for fragment in fragments
    ):
        raise ValueError("Unresolved artifact fragment contract is not owned.")
    return ArtifactOutputContract(
        tuple(sorted(paths)),
        tuple(sorted(fragments, key=lambda value: (value.carrier_path, value.key_path))),
    )


def _unresolved(
    artifact: WorkflowArtifact,
    reason: str,
    target: ConfigSyncSource | None = None,
    *,
    metadata: bytes | None = None,
) -> UnresolvedItem:
    return UnresolvedItem(
        artifact.identifier,
        reason,
        artifact.source_path,
        artifact.content,
        artifact.metadata if metadata is None else metadata,
        target,
        artifact.executable,
    )


def _retarget(item: UnresolvedItem, target: ConfigSyncSource) -> UnresolvedItem:
    return UnresolvedItem(
        item.identifier,
        item.reason,
        item.source_path,
        item.source_bytes,
        item.metadata,
        target,
        item.executable,
    )


def _deduplicate(items: Iterable[UnresolvedItem]) -> tuple[UnresolvedItem, ...]:
    values = {(item.identifier, item.reason, item.target_tool): item for item in items}
    return tuple(values.values())


def _issue(identifier: str, message: str, path: PurePosixPath | None = None) -> ValidationIssue:
    return ValidationIssue(identifier, message, path)


def _unresolved_key(item: UnresolvedItem) -> tuple[str, str, str]:
    return item.identifier, item.target_tool or "", item.reason


def _issue_key(item: ValidationIssue) -> tuple[str, str]:
    return item.identifier, item.relative_path.as_posix() if item.relative_path else ""
