"""Host-local agent versions and upstream Dockerfile defaults."""

import contextlib
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

import tomli_w

from djinn_in_a_box.core import paths

KNOWN_AGENT_ARGS = frozenset({"CLAUDE_CODE_VERSION", "CODEX_VERSION", "OPENCODE_VERSION"})
_RESET_HINT = "Delete the file to fall back to the Dockerfile defaults"


class AgentVersionError(RuntimeError):
    """Agent defaults, recorded versions or persistence are invalid."""


def numeric_version(value: str) -> tuple[int, int, int]:
    """Validate numeric x.y.z and return integer components for comparison."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise AgentVersionError(f"Expected numeric x.y.z version, got {value!r}")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def version_pin(project: Path, name: str) -> str:
    """Read exactly one canonical numeric ARG from the upstream Dockerfile.

    Non-canonical declarations count toward uniqueness and are rejected.
    Declarations split over a backslash continuation are not detected; the
    Dockerfile is maintained upstream.
    """
    dockerfile = project / "Dockerfile"
    try:
        text = dockerfile.read_text()
    except (OSError, UnicodeError) as exc:
        raise AgentVersionError(f"Cannot read {dockerfile} for {name}: {exc}") from exc
    declarations = re.findall(
        rf"^[ \t]*(?i:ARG)\b[^\n]*(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])[^\n]*",
        text,
        re.MULTILINE,
    )
    match = (
        re.fullmatch(rf"ARG {re.escape(name)}=([0-9]+\.[0-9]+\.[0-9]+)[ \t]*", declarations[0])
        if len(declarations) == 1
        else None
    )
    if match is None:
        raise AgentVersionError(f"{dockerfile}: expected one numeric x.y.z ARG {name}")
    return match[1]


def _validate_versions(versions: Mapping[str, object]) -> dict[str, str]:
    record = paths.AGENT_VERSIONS_FILE
    if unknown := versions.keys() - KNOWN_AGENT_ARGS:
        raise AgentVersionError(f"{record}: unknown agent keys: {', '.join(sorted(unknown))}")
    validated: dict[str, str] = {}
    for arg, value in versions.items():
        if not isinstance(value, str):
            raise AgentVersionError(f"{record}: {arg} must be a numeric x.y.z string")
        try:
            numeric_version(value)
        except AgentVersionError as exc:
            raise AgentVersionError(f"{record}: {arg}: {exc}") from exc
        validated[arg] = value
    return validated


def load_versions() -> dict[str, str]:
    """Load a validated flat record; a missing file means no local versions."""
    record = paths.AGENT_VERSIONS_FILE
    try:
        with record.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise AgentVersionError(
            f"Cannot read agent versions from {record}: {exc}. {_RESET_HINT}"
        ) from exc
    try:
        return _validate_versions(data)
    except AgentVersionError as exc:
        raise AgentVersionError(f"{exc}. {_RESET_HINT}") from exc


def save_versions(versions: Mapping[str, str]) -> None:
    """Validate and atomically replace the record with sorted TOML keys."""
    validated = _validate_versions(versions)
    record = paths.AGENT_VERSIONS_FILE
    temporary: str | None = None
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=record.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as stream:
            tomli_w.dump(dict(sorted(validated.items())), stream)
        os.replace(temporary, record)
    except OSError as exc:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        raise AgentVersionError(f"Cannot save agent versions to {record}: {exc}") from exc


def effective_version(project: Path, arg: str, versions: Mapping[str, str]) -> str:
    """Choose the numeric maximum, preferring the upstream string on equality."""
    default = version_pin(project, arg)
    local = versions.get(arg)
    if local is not None and numeric_version(local) > numeric_version(default):
        return local
    return default


def bake_overrides(project: Path, versions: Mapping[str, str]) -> dict[str, str]:
    """Return only recorded versions strictly above their upstream defaults."""
    return {
        arg: local
        for arg, local in versions.items()
        if numeric_version(local) > numeric_version(version_pin(project, arg))
    }
