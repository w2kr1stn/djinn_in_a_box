"""Pure declaration validation and diagnostics shared by config and runtime."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator

COMPOSE_ENV_KEYS = frozenset(
    [
        "TZ",
        "NO_COLOR",
        "DJINN_TERM_WIDTH",
        "UV_LINK_MODE",
        "LOCAL_ENDPOINT",
        "ENABLE_FIREWALL",
        "DJINN_DETACHED",
        "CONTAINERS",
        "IMAGES",
        "NETWORKS",
        "VOLUMES",
        "INFO",
        "VERSION",
        "POST",
        "ALLOW_START",
        "ALLOW_STOP",
        "ALLOW_RESTARTS",
        "BUILD",
        "COMMIT",
        "EXEC",
        "SWARM",
        "SECRETS",
        "CONFIGS",
        "PLUGINS",
        "SERVICES",
        "TASKS",
        "NODES",
        "AUTH",
        "LOG_LEVEL",
        "DOCKER_HOST",
        "DOCKER_ENABLED",
        "DOCKER_DIRECT",
    ]
)
DJINN_ENV_KEYS = frozenset(
    [
        "CODE_DIR",
        "DJINN_WORKSPACE_TARGET",
        "DJINN_CONFIG_ROOT",
        "CPU_LIMIT",
        "MEMORY_LIMIT",
        "CPU_RESERVATION",
        "MEMORY_RESERVATION",
        "DJINN_BUILD_NETWORK",
        "PULSE_SERVER",
        "PULSE_COOKIE",
        "PULSE_CLIENTCONFIG",
        "PULSE_CONFIG",
        "HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "SOPS_AGE_KEY_FILE",
        "SSH_AUTH_SOCK",
        "DJINN_GIT_MANIFEST",
        "GIT_CONFIG_HELPER",
        "AGENT_PROMPT",
        "TERM",
        "COLORTERM",
        "ZSH_THEME",
        "DJINN_DECLARED_VOLUME_TARGETS",
        "OWNERSHIP_REPAIR_HELPER",
        "SEED_LIB",
        "OUTPUT_LIB",
        "OPENCODE_RUNTIME_ROOT",
        "OPENCODE_RUNTIME_SETTINGS",
        "OPENCODE_PERSISTENT_SETTINGS",
        "SETTINGS_COPY_HELPER",
        "OPENCODE_CREDENTIALS_HELPER",
        "WORKFLOW_PUBLISHER",
        "DJINN_CANONICAL_ROOT",
        "CANONICAL_CONFIG_ROOT",
        "OPENCODE_WORKFLOW_VIEW",
        "MCP_REGISTER",
        "TOOLS_DIR",
        "TOOLS_BIN",
        "TOOLS_LIB",
        "RUSTUP_HOME",
        "CARGO_HOME",
        "DEBIAN_FRONTEND",
        "PATH",
        "LD_LIBRARY_PATH",
        "ZSH",
        "UV_PROJECT_ENVIRONMENT",
        "EDITOR",
        "VISUAL",
        "LANG",
        "LC_ALL",
        "SHELL",
        "DOCKER_VERSION",
        "COMPOSE_VERSION",
        "GH_VERSION",
        "USERNAME",
        "USER_UID",
        "USER_GID",
        "CLAUDE_CODE_VERSION",
        "CODEX_VERSION",
        "OPENCODE_VERSION",
        "NPM_CONFIG_FETCH_RETRIES",
        "NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT",
        "SCRIPT_DIR",
        "DOCKERFILE",
        "UI_COLOR_SUCCESS",
        "UI_COLOR_ERROR",
        "UI_COLOR_WARNING",
        "UI_COLOR_INFO",
        "PACKAGES",
        "INSTALL_DIR",
        "CURL_GUARDS",
        "SOPS_VERSION",
        "JUST_VERSION",
        "BUN_VERSION",
        "TMP_DIR",
        "INSTALL_BIN",
        "INSTALL_TOOLS",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "RUST_TOOLCHAIN",
        "COMPONENTS",
        "RUSTUP",
        "PULUMI_VERSION",
        "INSTALL_ROOT",
        "INSTALL_LIB",
        "PG_VERSION",
        "PG_BIN_DIR",
        "TOOLS_FILE",
        "CACHE_DIR",
        "INSTALLERS_DIR",
        "BUILD_TS",
        "CACHE_TS",
        "IFS",
        "CODEX_CONFIG",
        "MCP_SERVERS_CONFIG",
        "VENV_DIR",
        "BROWSERS_DIR",
        "WRAPPER",
        "REAL_BIN",
        "DEFAULT",
        "TARGET",
        "UI_COLOR_PRIMARY",
        "UI_COLOR_SECONDARY",
        "UI_COLOR_PATH",
        "UI_COLOR_MUTED",
        "UI_COLOR_BORDER",
        "DJINN_FORCE_UI_COLOR",
        "COLUMNS",
        "ALLOWED_DOMAINS",
        "DOCKER_NETWORKS",
        "SIGNING_KEY",
        "SOCK_GID",
        "DJINN_SHELL_PID",
        "EXIT_CODE",
    ]
)
RESERVED_ENVIRONMENT = {
    **dict.fromkeys(DJINN_ENV_KEYS, "Djinn"),
    **dict.fromkeys(COMPOSE_ENV_KEYS, "Compose"),
}


def _string(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain NUL")
    return value


class BindDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: StrictStr
    target: StrictStr
    marker: StrictStr | None = None

    @field_validator("source", "target")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        _string(value)
        if not PurePosixPath(value).is_absolute():
            raise ValueError("path must be absolute")
        return value

    @field_validator("source")
    @classmethod
    def colon_free(cls, value: str) -> str:
        if ":" in value:
            raise ValueError("source must not contain ':'")
        return value

    @field_validator("marker")
    @classmethod
    def marker_filename(cls, value: str | None) -> str | None:
        if value is not None:
            _string(value)
            if value in ("", ".", "..") or "/" in value or "\\" in value:
                raise ValueError("marker must be a single file name")
        return value


class VolumeDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    volume: Literal[True]
    target: StrictStr
    backup: Literal["data", "cache", "none"]

    @field_validator("volume", mode="before")
    @classmethod
    def literal_true(cls, value: object) -> object:
        if value is not True:
            raise ValueError("volume must be true")
        return value

    @field_validator("target")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        _string(value)
        if not PurePosixPath(value).is_absolute():
            raise ValueError("path must be absolute")
        return value


MountDeclaration = BindDeclaration | VolumeDeclaration


@dataclass(frozen=True, slots=True)
class DeclarationDiagnostic:
    collection: Literal["mounts", "environment"]
    name: str
    error: str | None = None

    @property
    def identity(self) -> str:
        return f"{self.collection}.{self.name}"


def declaration_error(collection: str, name: str, cause: str) -> str:
    kind = "mount" if collection == "mounts" else "environment"
    return f"Declared {kind} '{name}': {cause}."


def validate_mount(name: str, value: object) -> MountDeclaration:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name):
        raise ValueError("name must match [a-z0-9][a-z0-9_.-]*")
    if isinstance(value, (BindDeclaration, VolumeDeclaration)):
        value = value.model_dump(exclude_none=True)
    if not isinstance(value, dict):
        raise ValueError("invalid entry: expected a mount table")
    value = cast(dict[str, object], value)
    if "volume" in value and value["volume"] is not True:
        raise ValueError("invalid volume: volume must be true")
    model = VolumeDeclaration if value.get("volume") is True else BindDeclaration
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        error = exc.errors()[0]
        reason = {
            "missing": "required field is missing",
            "extra_forbidden": "field is not supported for this mount kind",
            "string_type": "value must be a string",
        }.get(error["type"], error["msg"].removeprefix("Value error, "))
        raise ValueError(f"invalid {error['loc'][0]}: {reason}") from exc


def validate_environment(key: str, value: object) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError("key must match [A-Za-z_][A-Za-z0-9_]*")
    if not isinstance(value, str):
        raise ValueError("value must be a string")
    _string(value)
    if key in RESERVED_ENVIRONMENT:
        raise ValueError(f"key is reserved by {RESERVED_ENVIRONMENT[key]}")
    return value


@dataclass(slots=True)
class DeclarationSet:
    mounts: dict[str, MountDeclaration] = field(default_factory=dict[str, MountDeclaration])
    environment: dict[str, str] = field(default_factory=dict[str, str])
    diagnostics: list[DeclarationDiagnostic] = field(default_factory=list[DeclarationDiagnostic])


def inspect_declarations(mounts: object, environment: object) -> DeclarationSet:
    """Retain every entry's outcome without accepting a partially valid config."""
    result = DeclarationSet()
    collections: tuple[tuple[Literal["mounts", "environment"], object], ...] = (
        ("mounts", mounts),
        ("environment", environment),
    )
    for collection, raw in collections:
        if not isinstance(raw, dict):
            result.diagnostics.append(
                DeclarationDiagnostic(
                    collection,
                    "<table>",
                    declaration_error(collection, "<table>", "expected a table"),
                )
            )
            continue
        for name, value in cast(dict[str, object], raw).items():
            error = None
            try:
                if collection == "mounts":
                    result.mounts[name] = validate_mount(name, value)
                else:
                    result.environment[name] = validate_environment(name, value)
            except ValueError as exc:
                error = declaration_error(collection, name, str(exc))
            result.diagnostics.append(DeclarationDiagnostic(collection, name, error))
    return result
