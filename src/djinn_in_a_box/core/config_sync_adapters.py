"""Closed native workflow adapters used by canonical config synchronization."""

from __future__ import annotations

import json
import re
import stat
import tomllib
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import NamedTuple, cast

import tomli_w

from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core.workflow_publisher import (
    NATIVE_ONLY_SPEC_MATRIX,
    ManifestError,
    load_strict_json,
)

_p = PurePosixPath
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SKILL = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_AGENT_FIELDS = frozenset({"name", "description", "developer_instructions"})
_CLAUDE_REVIEW_FIELDS = frozenset({"description", "argument-hint", "allowed-tools"})


class ArtifactKind(StrEnum):
    INSTRUCTIONS = "instructions"
    AGENT = "agent"
    SKILL = "skill"
    COMMAND = "command"
    CONTEXT = "context"
    HOOK = "hook"


class WorkflowArtifact(NamedTuple):
    kind: ArtifactKind
    name: str
    source_tool: ConfigSyncSource
    source_path: PurePosixPath
    content: bytes
    body: bytes | None = None
    description: str = ""
    nonportable_metadata: tuple[str, ...] = ()
    executable: bool = False
    native_only_for: ConfigSyncSource | None = None

    @property
    def identifier(self) -> str:
        return f"{self.kind}:{self.name}:{self.source_path.as_posix()}"


class RenderedFile(NamedTuple):
    relative_path: PurePosixPath
    content: bytes
    artifact_id: str
    executable: bool = False


class SettingsFragment(NamedTuple):
    carrier_path: PurePosixPath
    key_path: tuple[str, ...]
    value_json: bytes
    artifact_id: str


class UnresolvedItem(NamedTuple):
    identifier: str
    reason: str
    source_path: PurePosixPath
    source_bytes: bytes
    target_tool: ConfigSyncSource | None = None
    executable: bool = False


class ValidationIssue(NamedTuple):
    identifier: str
    message: str
    relative_path: PurePosixPath | None = None


class AdapterReadResult(NamedTuple):
    tool: ConfigSyncSource
    artifacts: tuple[WorkflowArtifact, ...]
    unresolved: tuple[UnresolvedItem, ...]
    validation_issues: tuple[ValidationIssue, ...]


class AdapterRenderResult(NamedTuple):
    source_tool: ConfigSyncSource
    target_tool: ConfigSyncSource
    files: tuple[RenderedFile, ...]
    settings_fragments: tuple[SettingsFragment, ...]
    unresolved: tuple[UnresolvedItem, ...]
    validation_issues: tuple[ValidationIssue, ...]


class NativeOnlySpec(NamedTuple):
    kind: ArtifactKind
    name: str
    script_path: PurePosixPath
    carrier_path: PurePosixPath | None = None
    event: str | None = None


class ToolOwnership(NamedTuple):
    instruction_path: PurePosixPath
    instruction_companion: PurePosixPath
    agent_suffix: str
    native_only: tuple[NativeOnlySpec, ...]
    provisioning_placeholders: tuple[PurePosixPath, ...] = ()

    @property
    def hooks(self) -> tuple[NativeOnlySpec, ...]:
        return tuple(item for item in self.native_only if item.kind is ArtifactKind.HOOK)


def _native_specs(tool: ConfigSyncSource) -> tuple[NativeOnlySpec, ...]:
    return tuple(
        NativeOnlySpec(
            ArtifactKind(item.kind), item.name, item.script_path, item.carrier_path, item.event
        )
        for item in NATIVE_ONLY_SPEC_MATRIX[tool]
    )


OWNERSHIP_MATRIX: Mapping[ConfigSyncSource, ToolOwnership] = {
    "claude": ToolOwnership(
        _p("CLAUDE.md"),
        _p("AGENTS.md"),
        ".md",
        _native_specs("claude"),
        (_p("AGENTS.md"),),
    ),
    "codex": ToolOwnership(
        _p("AGENTS.md"),
        _p("CLAUDE.md"),
        ".toml",
        _native_specs("codex"),
    ),
    "opencode": ToolOwnership(
        _p("AGENTS.md"),
        _p("CLAUDE.md"),
        ".md",
        _native_specs("opencode"),
    ),
}
_PLUGIN_PATHS = frozenset(hook.script_path for hook in OWNERSHIP_MATRIX["opencode"].hooks)
type _File = tuple[PurePosixPath, bytes, bool]


class NativeOnlyReadResult(NamedTuple):
    artifacts: tuple[WorkflowArtifact, ...]
    settings_fragments: tuple[SettingsFragment, ...]
    validation_issues: tuple[ValidationIssue, ...]


def read_native_workflow(root: Path, tool: ConfigSyncSource) -> AdapterReadResult:
    root, owned = root.resolve(), OWNERSHIP_MATRIX[tool]
    artifacts: list[WorkflowArtifact] = []
    unresolved: list[UnresolvedItem] = []
    issues: list[ValidationIssue] = []
    native_only = read_native_only_workflow(root, tool)
    artifacts.extend(native_only.artifacts)
    issues.extend(native_only.validation_issues)
    instruction = _read(root, owned.instruction_path, issues)
    if instruction is None:
        issues.append(
            _issue(
                "instructions:missing", "Missing native root instructions.", owned.instruction_path
            )
        )
    else:
        artifacts.append(_artifact(tool, ArtifactKind.INSTRUCTIONS, "global", instruction))
    for item in _scan(root, _p("agents"), issues):
        if len(item[0].parts) == 2 and item[0].suffix == owned.agent_suffix:
            artifact, issue = _agent(tool, item)
            if artifact is not None:
                artifacts.append(artifact)
            if issue is not None:
                issues.append(issue)
    skills = _scan(root, _p("skills"), issues)
    for name, items in _group_skills(skills).items():
        if tool == "codex" and name.startswith("command-"):
            command = _command(tool, name.removeprefix("command-"), _entrypoint(items))
            _append(command, artifacts, unresolved, issues)
        elif _valid_skill(name, _entrypoint(items)):
            artifacts.extend(_artifact(tool, ArtifactKind.SKILL, name, item) for item in items)
        else:
            issues.append(
                _issue(
                    f"skill-entrypoint:{name}", "Skill entrypoint is invalid.", _p("skills") / name
                )
            )
    if tool != "codex":
        for item in _scan(root, _p("commands"), issues):
            if (
                len(item[0].parts) == 2
                and item[0].suffix == ".md"
                and not native_only_file_is_owned(tool, item[0])
            ):
                _append(_command(tool, item[0].stem, item), artifacts, unresolved, issues)
    for prefix in (_p("context"), _p("scripts")):
        artifacts.extend(
            _artifact(tool, ArtifactKind.CONTEXT, prefix.name, item)
            for item in _scan(root, prefix, issues)
            if not native_only_file_is_owned(tool, item[0])
        )
    unresolved.extend(_blocked(item) for item in artifacts if item.nonportable_metadata)
    return AdapterReadResult(
        tool,
        tuple(sorted(artifacts, key=lambda item: item.source_path)),
        _unique(unresolved),
        tuple(sorted(issues)),
    )


def read_native_only_workflow(root: Path, tool: ConfigSyncSource) -> NativeOnlyReadResult:
    root = root.resolve()
    artifacts: list[WorkflowArtifact] = []
    fragments: list[SettingsFragment] = []
    issues: list[ValidationIssue] = []
    carriers: dict[PurePosixPath, Mapping[str, object] | None] = {}
    for item in OWNERSHIP_MATRIX[tool].native_only:
        if item.kind is ArtifactKind.COMMAND:
            command = _read(root, item.script_path, issues)
            if command is not None:
                _append(_command(tool, item.name, command), artifacts, [], issues)
            continue
        script = _read(root, item.script_path, issues)
        if script is not None and item.script_path in _PLUGIN_PATHS and not _plugin(script[1]):
            issues.append(
                _issue(
                    f"plugin-export:{item.name}",
                    "OpenCode plugin export marker is missing.",
                    item.script_path,
                )
            )
        if item.carrier_path is None:
            if script is not None:
                artifacts.append(_artifact(tool, item.kind, item.name, script, native=tool))
            continue
        carriers.setdefault(item.carrier_path, _json(root, item.carrier_path, issues))
        value = _nested(carriers[item.carrier_path], item.event)
        if script is None and value is None:
            continue
        if script is None or value is None:
            issues.append(
                _issue(
                    f"hook-incomplete:{item.name}",
                    "Hook script and registration must coexist.",
                    item.script_path,
                )
            )
            continue
        artifact = _artifact(tool, item.kind, item.name, script, native=tool)
        artifacts.append(artifact)
        assert item.event is not None
        fragments.append(
            SettingsFragment(
                item.carrier_path,
                ("hooks", item.event),
                _dump(value),
                artifact.identifier,
            )
        )
    return NativeOnlyReadResult(
        tuple(sorted(artifacts, key=lambda item: item.source_path)),
        tuple(sorted(fragments)),
        tuple(sorted(issues)),
    )


def render_native_workflow(
    source: AdapterReadResult, target: ConfigSyncSource
) -> AdapterRenderResult:
    if source.tool == target:
        raise ValueError("Source and target tools must differ.")
    owned = OWNERSHIP_MATRIX[target]
    files: list[RenderedFile] = []
    fragments: list[SettingsFragment] = []
    unresolved = [_retarget(item, target) for item in source.unresolved]
    for artifact in source.artifacts:
        if artifact.native_only_for is not None:
            continue
        if artifact.kind == ArtifactKind.INSTRUCTIONS:
            for path in (owned.instruction_path, owned.instruction_companion):
                _append_portable_file(
                    files, unresolved, _rendered(path, artifact), artifact, target
                )
        elif artifact.kind == ArtifactKind.AGENT:
            rendered = _render_agent(artifact, target)
            if rendered is None:
                unresolved.append(_blocked(artifact, target))
            else:
                _append_portable_file(files, unresolved, rendered, artifact, target)
        elif artifact.kind == ArtifactKind.SKILL:
            _append_portable_file(
                files,
                unresolved,
                _rendered(
                    _p("skills") / artifact.name / _p(*artifact.source_path.parts[2:]), artifact
                ),
                artifact,
                target,
            )
        elif artifact.kind == ArtifactKind.COMMAND:
            rendered = _render_command(artifact, target)
            if rendered is None:
                unresolved.append(_blocked(artifact, target))
            else:
                _append_portable_file(files, unresolved, rendered, artifact, target)
        elif artifact.kind == ArtifactKind.CONTEXT:
            _append_portable_file(
                files, unresolved, _rendered(artifact.source_path, artifact), artifact, target
            )
    if target == "codex":
        _append_portable_fragment(
            fragments,
            unresolved,
            SettingsFragment(
                _p("config.toml"), ("project_doc_fallback_filenames",), b'["CLAUDE.md"]', "bridge"
            ),
            target,
        )
    files_tuple = tuple(sorted(files))
    fragments_tuple = tuple(sorted(fragments))
    return AdapterRenderResult(
        source.tool,
        target,
        files_tuple,
        fragments_tuple,
        _unique(unresolved),
        validate_rendered_workflow(target, files_tuple, fragments_tuple),
    )


def validate_rendered_workflow(
    tool: ConfigSyncSource, files: Iterable[RenderedFile], fragments: Iterable[SettingsFragment]
) -> tuple[ValidationIssue, ...]:
    files, fragments = tuple(files), tuple(fragments)
    issues: list[ValidationIssue] = []
    paths: set[PurePosixPath] = set()
    for item in files:
        path = item.relative_path
        if not is_safe_relative_path(path) or not path_is_owned(tool, path):
            issues.append(
                _issue(f"unowned-path:{item.artifact_id}", "Rendered path is not owned.", path)
            )
        if path in paths:
            issues.append(_issue(f"duplicate-path:{path}", "Rendered path is duplicated.", path))
        paths.add(path)
        issues.extend(_file_issues(tool, item))
    for path in (
        OWNERSHIP_MATRIX[tool].instruction_path,
        OWNERSHIP_MATRIX[tool].instruction_companion,
    ):
        if path not in paths:
            issues.append(
                _issue(
                    f"instructions:missing:{path}", "Rendered instruction form is missing.", path
                )
            )
    seen: set[tuple[PurePosixPath, tuple[str, ...]]] = set()
    for item in fragments:
        key = item.carrier_path, item.key_path
        if key in seen or not fragment_is_owned(tool, *key):
            issues.append(
                _issue(f"invalid-fragment:{item.artifact_id}", "Settings fragment is not owned.")
            )
        seen.add(key)
        try:
            load_strict_json(item.value_json)
        except ManifestError:
            issues.append(
                _issue(f"invalid-fragment:{item.artifact_id}", "Settings fragment is invalid JSON.")
            )
    return tuple(sorted(issues))


def is_safe_relative_path(path: PurePosixPath) -> bool:
    return (
        not path.is_absolute()
        and bool(path.parts)
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def path_is_owned(tool: ConfigSyncSource, path: PurePosixPath) -> bool:
    owned = OWNERSHIP_MATRIX[tool]
    return is_safe_relative_path(path) and (
        path in {owned.instruction_path, owned.instruction_companion}
        or len(path.parts) == 2
        and path.parts[0] == "agents"
        and path.suffix == owned.agent_suffix
        or len(path.parts) >= 3
        and path.parts[0] == "skills"
        or tool != "codex"
        and len(path.parts) == 2
        and path.parts[0] == "commands"
        and path.suffix == ".md"
        and (tool == "claude" or path.stem != "codex-review")
        or len(path.parts) >= 2
        and path.parts[0] in {"context", "scripts"}
        or path in {item.script_path for item in owned.native_only}
    )


def fragment_is_owned(tool: ConfigSyncSource, path: PurePosixPath, keys: tuple[str, ...]) -> bool:
    return (
        native_only_fragment_is_owned(tool, path, keys)
        or tool == "codex"
        and (path, keys) == (_p("config.toml"), ("project_doc_fallback_filenames",))
    )


def native_only_file_is_owned(tool: ConfigSyncSource, path: PurePosixPath) -> bool:
    return path in {
        item.script_path for item in OWNERSHIP_MATRIX[tool].native_only
    }


def native_only_input_paths(tool: ConfigSyncSource) -> frozenset[PurePosixPath]:
    """Return every canonical path whose native bytes can enter a runtime view."""
    return frozenset(
        path
        for item in OWNERSHIP_MATRIX[tool].native_only
        for path in (item.script_path, item.carrier_path)
        if path is not None
    )


def provisioning_placeholder_paths(tool: ConfigSyncSource) -> frozenset[PurePosixPath]:
    """Return mount-source placeholders declared by the ownership matrix."""
    return frozenset(OWNERSHIP_MATRIX[tool].provisioning_placeholders)


def native_only_fragment_is_owned(
    tool: ConfigSyncSource, path: PurePosixPath, keys: tuple[str, ...]
) -> bool:
    return (path, keys) in {
        (item.carrier_path, ("hooks", item.event))
        for item in OWNERSHIP_MATRIX[tool].native_only
        if item.carrier_path and item.event
    }


def _append_portable_file(
    files: list[RenderedFile],
    unresolved: list[UnresolvedItem],
    rendered: RenderedFile,
    artifact: WorkflowArtifact,
    target: ConfigSyncSource,
) -> None:
    if native_only_file_is_owned(target, rendered.relative_path):
        unresolved.append(_blocked(artifact, target))
        return
    files.append(rendered)


def _append_portable_fragment(
    fragments: list[SettingsFragment],
    unresolved: list[UnresolvedItem],
    rendered: SettingsFragment,
    target: ConfigSyncSource,
) -> None:
    if native_only_fragment_is_owned(target, rendered.carrier_path, rendered.key_path):
        from djinn_in_a_box.core.config_sync import CANONICAL_REMEDY

        unresolved.append(
            UnresolvedItem(
                rendered.artifact_id,
                CANONICAL_REMEDY,
                rendered.carrier_path,
                rendered.value_json,
                target,
            )
        )
        return
    fragments.append(rendered)


def _read(root: Path, path: PurePosixPath, issues: list[ValidationIssue]) -> _File | None:
    candidate = root.joinpath(*path.parts)
    if not candidate.exists() and not candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise OSError
        content = resolved.read_bytes()
        content.decode()
        return path, content, bool(resolved.stat().st_mode & stat.S_IXUSR)
    except UnicodeDecodeError:
        issues.append(_issue(f"invalid-utf8:{path}", "Workflow files must be UTF-8.", path))
    except (OSError, RuntimeError):
        issues.append(
            _issue(f"external-path:{path}", "Path is unreadable or escapes its root.", path)
        )
    return None


def _scan(root: Path, prefix: PurePosixPath, issues: list[ValidationIssue]) -> tuple[_File, ...]:
    directory = root.joinpath(*prefix.parts)
    if not directory.exists() and not directory.is_symlink():
        return ()
    try:
        if not directory.resolve(strict=True).is_relative_to(root):
            raise OSError
        paths = sorted(path for path in directory.rglob("*") if not path.is_dir())
    except (OSError, RuntimeError):
        issues.append(_issue(f"invalid-tree:{prefix}", "Allowlisted tree is invalid.", prefix))
        return ()
    return tuple(
        item
        for path in paths
        if (item := _read(root, _p(path.relative_to(root).as_posix()), issues)) is not None
    )


def _agent(
    tool: ConfigSyncSource, item: _File
) -> tuple[WorkflowArtifact | None, ValidationIssue | None]:
    path, content, _executable = item
    if not _NAME.fullmatch(path.stem):
        return None, _issue(f"agent-name:{path.stem}", "Agent name is unsafe.", path)
    if tool == "codex":
        try:
            data = _mapping(tomllib.loads(content.decode()))
        except (tomllib.TOMLDecodeError, ValueError):
            return None, _issue(f"agent-toml:{path.stem}", "Codex agent TOML is invalid.", path)
        if not _valid_agent(data, path.stem):
            return None, _issue(
                f"agent-fields:{path.stem}", "Codex agent fields are invalid.", path
            )
        return _artifact(
            tool,
            ArtifactKind.AGENT,
            path.stem,
            item,
            cast(str, data["developer_instructions"]).encode(),
            cast(str, data["description"]),
            tuple(sorted(set(data) - _AGENT_FIELDS)),
        ), None
    parsed = _frontmatter(content)
    if (
        parsed is None
        or parsed[0].get("name", path.stem) != path.stem
        or not parsed[0].get("description")
        or not parsed[1].strip()
    ):
        return None, _issue(f"agent-fields:{path.stem}", "Markdown agent fields are invalid.", path)
    return _artifact(
        tool,
        ArtifactKind.AGENT,
        path.stem,
        item,
        parsed[1],
        parsed[0]["description"],
        tuple(sorted(set(parsed[0]) - {"name", "description"})),
    ), None


def _command(
    tool: ConfigSyncSource, name: str, item: _File | None
) -> tuple[WorkflowArtifact | None, UnresolvedItem | None, ValidationIssue | None]:
    if item is None or not _NAME.fullmatch(name) or (parsed := _frontmatter(item[1])) is None:
        return (
            None,
            None,
            _issue(
                f"command-fields:{name}",
                "Command frontmatter is invalid.",
                item[0] if item else None,
            ),
        )
    metadata, body = parsed
    expected = f"command-{name}" if tool == "codex" else None
    if (
        not metadata.get("description")
        or not body.strip()
        or expected
        and metadata.get("name") != expected
    ):
        return None, None, _issue(f"command-fields:{name}", "Command fields are invalid.", item[0])
    native = native_only_file_is_owned(tool, item[0])
    allowed = (
        _CLAUDE_REVIEW_FIELDS
        if native
        else {"name", "description"}
        if tool == "codex"
        else {"description"}
    )
    if native and set(metadata) - allowed:
        return (
            None,
            None,
            _issue(f"command-fields:{name}", "Claude-only command fields are invalid.", item[0]),
        )
    artifact = _artifact(
        tool,
        ArtifactKind.COMMAND,
        name,
        item,
        body,
        metadata["description"],
        tuple(sorted(set(metadata) - allowed)),
        tool if native else None,
    )
    return artifact, _blocked(artifact) if name == "codex-review" and not native else None, None


def _append(
    value: tuple[WorkflowArtifact | None, UnresolvedItem | None, ValidationIssue | None],
    artifacts: list[WorkflowArtifact],
    unresolved: list[UnresolvedItem],
    issues: list[ValidationIssue],
) -> None:
    artifact, blocked, issue = value
    if artifact:
        artifacts.append(artifact)
    if blocked:
        unresolved.append(blocked)
    if issue:
        issues.append(issue)


def _group_skills(files: tuple[_File, ...]) -> dict[str, list[_File]]:
    groups: dict[str, list[_File]] = {}
    for item in files:
        if len(item[0].parts) >= 3:
            groups.setdefault(item[0].parts[1], []).append(item)
    return groups


def _entrypoint(items: list[_File]) -> _File | None:
    return next((item for item in items if item[0].name == "SKILL.md"), None)


def _valid_skill(name: str, item: _File | None) -> bool:
    return (
        item is not None
        and _SKILL.fullmatch(name) is not None
        and (parsed := _frontmatter(item[1])) is not None
        and parsed[0].get("name") == name
        and bool(parsed[0].get("description", "").strip())
        and bool(parsed[1].strip())
    )


def _json(
    root: Path, path: PurePosixPath, issues: list[ValidationIssue]
) -> Mapping[str, object] | None:
    item = _read(root, path, issues)
    if item is None:
        return None
    try:
        return _mapping(load_strict_json(item[1]))
    except (ManifestError, ValueError):
        issues.append(_issue(f"invalid-json:{path}", "Settings carrier is invalid JSON.", path))
        return None


def _file_issues(tool: ConfigSyncSource, item: RenderedFile) -> tuple[ValidationIssue, ...]:
    try:
        text = item.content.decode()
    except UnicodeDecodeError:
        return (
            _issue(
                f"invalid-utf8:{item.artifact_id}",
                "Workflow files must be UTF-8.",
                item.relative_path,
            ),
        )
    try:
        if item.relative_path.suffix == ".json":
            load_strict_json(item.content)
        if item.relative_path.suffix == ".toml":
            tomllib.loads(text)
    except (ManifestError, tomllib.TOMLDecodeError):
        return (
            _issue(
                f"invalid-data:{item.artifact_id}",
                "Workflow data file is invalid.",
                item.relative_path,
            ),
        )
    if len(item.relative_path.parts) == 2 and item.relative_path.parts[0] == "agents":
        if tool == "codex":
            try:
                valid = _valid_agent(_mapping(tomllib.loads(text)), item.relative_path.stem)
            except ValueError:
                valid = False
        else:
            parsed = _frontmatter(item.content)
            valid = (
                parsed is not None
                and parsed[0].get("name", item.relative_path.stem) == item.relative_path.stem
                and bool(parsed[0].get("description"))
                and not (set(parsed[0]) - {"name", "description"})
                and bool(parsed[1].strip())
            )
        if not valid:
            return (
                _issue(
                    f"invalid-agent:{item.artifact_id}",
                    "Agent fields are invalid.",
                    item.relative_path,
                ),
            )
    if tool == "opencode" and item.relative_path in _PLUGIN_PATHS and not _plugin(item.content):
        return (
            _issue(
                f"invalid-plugin:{item.artifact_id}",
                "OpenCode plugin export marker is missing.",
                item.relative_path,
            ),
        )
    if (
        len(item.relative_path.parts) == 3
        and item.relative_path.parts[0] == "skills"
        and item.relative_path.name == "SKILL.md"
        and not _valid_skill(
            item.relative_path.parts[1], (item.relative_path, item.content, item.executable)
        )
    ):
        return (
            _issue(
                f"invalid-skill:{item.artifact_id}",
                "Skill entrypoint is invalid.",
                item.relative_path,
            ),
        )
    return ()


def _render_agent(item: WorkflowArtifact, target: ConfigSyncSource) -> RenderedFile | None:
    if item.body is None or item.nonportable_metadata:
        return None
    if target == "codex":
        return _rendered(
            _p("agents") / f"{item.name}.toml",
            item,
            tomli_w.dumps(
                {
                    "name": item.name,
                    "description": item.description,
                    "developer_instructions": item.body.decode(),
                },
                multiline_strings=True,
            ).encode(),
        )
    return _rendered(
        _p("agents") / f"{item.name}.md",
        item,
        _markdown((("name", item.name), ("description", item.description)), item.body),
    )


def _render_command(item: WorkflowArtifact, target: ConfigSyncSource) -> RenderedFile | None:
    if item.body is None or item.nonportable_metadata:
        return None
    if target == "codex":
        return _rendered(
            _p("skills") / f"command-{item.name}" / "SKILL.md",
            item,
            _markdown(
                (("name", f"command-{item.name}"), ("description", item.description)), item.body
            ),
        )
    return _rendered(
        _p("commands") / f"{item.name}.md",
        item,
        _markdown((("description", item.description),), item.body),
    )


def _artifact(
    tool: ConfigSyncSource,
    kind: ArtifactKind,
    name: str,
    item: _File,
    body: bytes | None = None,
    description: str = "",
    nonportable: tuple[str, ...] = (),
    native: ConfigSyncSource | None = None,
) -> WorkflowArtifact:
    return WorkflowArtifact(
        kind, name, tool, item[0], item[1], body, description, nonportable, item[2], native
    )


def _rendered(
    path: PurePosixPath, item: WorkflowArtifact, content: bytes | None = None
) -> RenderedFile:
    return RenderedFile(
        path, item.content if content is None else content, item.identifier, item.executable
    )


def _blocked(item: WorkflowArtifact, target: ConfigSyncSource | None = None) -> UnresolvedItem:
    from djinn_in_a_box.core.config_sync import CANONICAL_REMEDY

    return UnresolvedItem(
        item.identifier, CANONICAL_REMEDY, item.source_path, item.content, target, item.executable
    )


def _retarget(item: UnresolvedItem, target: ConfigSyncSource) -> UnresolvedItem:
    return UnresolvedItem(
        item.identifier, item.reason, item.source_path, item.source_bytes, target, item.executable
    )


def _frontmatter(content: bytes) -> tuple[dict[str, str], bytes] | None:
    try:
        lines = content.decode().splitlines(keepends=True)
    except UnicodeDecodeError:
        return None
    end = (
        next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if lines and lines[0].strip() == "---"
        else None
    )
    if end is None:
        return None
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if line[:1].isspace() or ":" not in (text := line.strip()):
            return None
        key, value = text.split(":", 1)
        if key in metadata or not value.strip():
            return None
        metadata[key] = value.strip().strip('"').strip("'")
    return metadata, "".join(lines[end + 1 :]).encode()


def _markdown(metadata: tuple[tuple[str, str], ...], body: bytes) -> bytes:
    return "\n".join(
        ("---", *(f"{key}: {json.dumps(value)}" for key, value in metadata), "---", "")
    ).encode() + body.lstrip(b"\r\n")


def _valid_agent(data: Mapping[str, object], name: str) -> bool:
    return (
        set(data) >= _AGENT_FIELDS
        and data.get("name") == name
        and _NAME.fullmatch(name) is not None
        and all(isinstance(value, str) and value.strip() for value in data.values())
    )


def _plugin(content: bytes) -> bool:
    try:
        return "export" in content.decode()
    except UnicodeDecodeError:
        return False


def _nested(data: Mapping[str, object] | None, event: str | None) -> object | None:
    return (
        cast(Mapping[str, object], data["hooks"]).get(event)
        if data and event and isinstance(data.get("hooks"), Mapping)
        else None
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError
    raw = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ValueError
    return {cast(str, key): item for key, item in raw.items()}


def _dump(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _issue(identifier: str, message: str, path: PurePosixPath | None = None) -> ValidationIssue:
    return ValidationIssue(identifier, message, path)


def _unique(items: Iterable[UnresolvedItem]) -> tuple[UnresolvedItem, ...]:
    return tuple(sorted({(item.identifier, item.target_tool): item for item in items}.values()))
