from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from djinn_in_a_box.config.defaults import DEFAULT_ZONES
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.docker import (
    ZoneRoots,
    repo_owned_submount_targets,
    resolve_zone_roots,
)
from djinn_in_a_box.core.exceptions import (
    ConfigNotFoundError,
    ZoneConfigurationError,
)
from djinn_in_a_box.core.paths import ZONES_FILE

type ZoneName = Literal["local", "shared"]
_ZONE_NAMES: Final[tuple[ZoneName, ...]] = ("local", "shared")
MIGRATING_ZONE_PREFIX: Final[str] = ".djinn-migrating-"
"""Reserved directory prefix for an in-progress cross-filesystem migration."""

ZONE_CONTAINER_TARGETS: Final[dict[str, Path]] = {
    "claude": Path("/home/dev/.claude"),
    "codex": Path("/home/dev/.codex"),
    "opencode": Path("/home/dev/.opencode"),
    "gemini": Path("/home/dev/.gemini"),
    "gh": Path("/home/dev/.config/gh"),
    "age": Path("/home/dev/.config/age"),
}


class _ZoneLists(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    local: tuple[str, ...] = ()
    shared: tuple[str, ...] = ()


class _ZoneFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    zones: dict[str, _ZoneLists] = Field(default_factory=dict)


@dataclass(frozen=True)
class ZoneAssignment:
    agent: str
    zone: ZoneName
    relative_path: Path


@dataclass(frozen=True)
class ZoneAssignments:
    by_agent: dict[str, dict[ZoneName, tuple[Path, ...]]]
    skipped_defaults: tuple[ZoneAssignment, ...]


@dataclass(frozen=True)
class _Candidate:
    zone: ZoneName
    raw_path: str
    is_default: bool


def _load_zone_overrides(path: Path) -> dict[str, _ZoneLists]:
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        msg = f"Invalid TOML syntax in {path}: {error}"
        raise ZoneConfigurationError(msg) from error

    try:
        return _ZoneFile.model_validate(data).zones
    except ValidationError as error:
        details = "\n".join(
            f"  - {'.'.join(str(item) for item in entry['loc'])}: {entry['msg']}"
            for entry in error.errors()
        )
        msg = f"Invalid zone configuration in {path}:\n{details}"
        raise ZoneConfigurationError(msg) from error


def _validate_agent(agent: str) -> None:
    if agent not in ZONE_CONTAINER_TARGETS:
        msg = f"Zone assignment for {agent!r} has no container mount target"
        raise ZoneConfigurationError(msg)


def _validate_relative_path(agent: str, zone: ZoneName, raw_path: str) -> Path:
    parts = raw_path.split("/")
    path = Path(raw_path)
    if path.is_absolute():
        msg = f"Zone assignment for {agent}.{zone} must be relative: {raw_path!r}"
        raise ZoneConfigurationError(msg)
    if not raw_path or any(part in {"", ".", ".."} for part in parts):
        msg = f"Zone assignment for {agent}.{zone} contains an invalid path component: {raw_path!r}"
        raise ZoneConfigurationError(msg)
    if any(part.startswith(MIGRATING_ZONE_PREFIX) for part in parts):
        msg = f"Zone assignment for {agent}.{zone} uses a reserved migration path: {raw_path!r}"
        raise ZoneConfigurationError(msg)
    return path


def _repo_owned_target(agent: str, relative_path: Path) -> Path | None:
    agent_target = ZONE_CONTAINER_TARGETS[agent]
    target = agent_target / relative_path
    for mount_target in repo_owned_submount_targets(agent_target):
        if target == mount_target or target.is_relative_to(mount_target):
            return mount_target
    return None


def _source_has_symlink_or_file(source: Path, relative_path: Path) -> tuple[bool, bool]:
    current = source
    if current.is_symlink():
        return True, False
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            return True, False
        if current.is_file():
            return False, True
    return False, False


def _validate_assignment(
    roots: ZoneRoots,
    agent: str,
    candidate: _Candidate,
) -> Path | None:
    relative_path = _validate_relative_path(agent, candidate.zone, candidate.raw_path)
    repo_owned_target = _repo_owned_target(agent, relative_path)
    if repo_owned_target is not None:
        msg = (
            f"Zone assignment for {agent}.{candidate.zone} is under repo-owned mount "
            f"{repo_owned_target}: {candidate.raw_path!r}"
        )
        raise ZoneConfigurationError(msg)

    source = roots.config_root
    source_path = Path(agent) / relative_path
    has_symlink, has_file = _source_has_symlink_or_file(source, source_path)
    if has_symlink:
        if candidate.is_default:
            return None
        msg = (
            f"Zone assignment for {agent}.{candidate.zone} has a symlinked component: "
            f"{candidate.raw_path!r}"
        )
        raise ZoneConfigurationError(msg)
    if has_file:
        if candidate.is_default:
            return None
        msg = (
            f"Zone assignment for {agent}.{candidate.zone} resolves to a regular file: "
            f"{candidate.raw_path!r}"
        )
        raise ZoneConfigurationError(msg)

    destination_root = roots.local_root if candidate.zone == "local" else roots.shared_root
    destination_symlink, destination_file = _source_has_symlink_or_file(
        destination_root,
        source_path,
    )
    if destination_symlink:
        msg = (
            f"Zone assignment for {agent}.{candidate.zone} has a symlinked destination "
            f"component: {candidate.raw_path!r}"
        )
        raise ZoneConfigurationError(msg)
    if destination_file:
        msg = (
            f"Zone assignment for {agent}.{candidate.zone} has a destination regular file: "
            f"{candidate.raw_path!r}"
        )
        raise ZoneConfigurationError(msg)
    return relative_path


def _check_overlaps(agent: str, assignments: list[ZoneAssignment]) -> None:
    for index, assignment in enumerate(assignments):
        for other in assignments[index + 1 :]:
            if assignment.relative_path == other.relative_path and assignment.zone != other.zone:
                msg = (
                    f"Zone assignment {assignment.relative_path!s} is in both "
                    f"{assignment.zone} and {other.zone} for {agent}"
                )
                raise ZoneConfigurationError(msg)
            first_is_nested = assignment.relative_path.is_relative_to(other.relative_path)
            second_is_nested = other.relative_path.is_relative_to(assignment.relative_path)
            if first_is_nested or second_is_nested:
                msg = (
                    f"Zone assignments overlap for {agent}: "
                    f"{assignment.relative_path!s} and {other.relative_path!s}"
                )
                raise ZoneConfigurationError(msg)


def _agent_candidates(agent: str, override: _ZoneLists | None) -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    for zone in _ZONE_NAMES:
        candidates.extend(
            _Candidate(zone, path, True) for path in DEFAULT_ZONES[agent][zone]
        )
        if override is not None:
            candidates.extend(_Candidate(zone, path, False) for path in getattr(override, zone))
    return tuple(candidates)


def load_zone_assignments(
    config: AppConfig | None = None,
    *,
    path: Path | None = None,
) -> ZoneAssignments:
    roots = resolve_zone_roots(config)
    zones_path = path or ZONES_FILE
    if path is not None and not zones_path.exists():
        raise ConfigNotFoundError(zones_path)
    overrides = _load_zone_overrides(zones_path) if zones_path.exists() else {}

    for agent in overrides:
        _validate_agent(agent)

    by_agent: dict[str, dict[ZoneName, tuple[Path, ...]]] = {}
    skipped_defaults: list[ZoneAssignment] = []
    for agent in ZONE_CONTAINER_TARGETS:
        paths_by_zone: dict[ZoneName, list[Path]] = {"local": [], "shared": []}
        assignments: list[ZoneAssignment] = []
        seen_by_zone: dict[ZoneName, set[Path]] = {"local": set(), "shared": set()}
        for candidate in _agent_candidates(agent, overrides.get(agent)):
            relative_path = _validate_assignment(roots, agent, candidate)
            if relative_path is None:
                skipped_defaults.append(
                    ZoneAssignment(agent, candidate.zone, Path(candidate.raw_path))
                )
                continue
            if relative_path in seen_by_zone[candidate.zone]:
                continue
            seen_by_zone[candidate.zone].add(relative_path)
            paths_by_zone[candidate.zone].append(relative_path)
            assignments.append(ZoneAssignment(agent, candidate.zone, relative_path))
        _check_overlaps(agent, assignments)
        by_agent[agent] = {
            "local": tuple(paths_by_zone["local"]),
            "shared": tuple(paths_by_zone["shared"]),
        }

    return ZoneAssignments(by_agent, tuple(skipped_defaults))
