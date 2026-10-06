"""Explicit host inputs for public SSH delivery and the dedicated Git agent."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

PUBLIC_FILENAMES = frozenset(
    {"config", "known_hosts", "tailnet_known_hosts", "allowed_signers", "git.json"}
)


def ssh_token(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
        raise ValueError("must be a literal SSH token (no options, whitespace or wildcards)")
    return value


def duration_minutes(value: str) -> int:
    """The public duration grammar is shared by configuration and hostctl on."""
    if not re.fullmatch(r"[1-9][0-9]*[mh]", value):
        raise ValueError("duration must be a positive integer followed by m or h")
    # Bound the input before int conversion as well as the resulting duration.
    if len(value) > 5:
        raise ValueError("duration must not exceed 24h (1440m)")
    minutes = int(value[:-1]) * (60 if value[-1] == "h" else 1)
    if minutes > 1440:
        raise ValueError("duration must not exceed 24h (1440m)")
    return minutes


class HostctlHost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    address: str
    user: str

    _tokens = field_validator("address", "user")(ssh_token)


class HostctlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_duration: str = "2h"
    hosts: dict[str, HostctlHost] = {}

    @field_validator("default_duration")
    @classmethod
    def duration(cls, value: str) -> str:
        duration_minutes(value)
        return value

    @field_validator("hosts")
    @classmethod
    def aliases(cls, value: dict[str, HostctlHost]) -> dict[str, HostctlHost]:
        for alias in value:
            ssh_token(alias)
        return value


class GitIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hostname: str
    user: str
    key_file: Path
    public_key_file: Path

    _tokens = field_validator("hostname", "user")(ssh_token)

    @field_validator("key_file", "public_key_file", mode="before")
    @classmethod
    def key_path(cls, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute() or any(c in str(path) for c in "\n\r\x00"):
            raise ValueError("key paths must be absolute host paths")
        return path

    @field_validator("public_key_file")
    @classmethod
    def public_filename(cls, value: Path) -> Path:
        if (
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value.name)
            or value.name in PUBLIC_FILENAMES
        ):
            raise ValueError("unsafe or reserved public key filename")
        return value


class GitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signing_identity: str | None = None
    allowed_signers_file: Path | None = None
    identities: dict[str, GitIdentity] = {}

    @field_validator("identities")
    @classmethod
    def aliases(cls, value: dict[str, GitIdentity]) -> dict[str, GitIdentity]:
        for alias in value:
            ssh_token(alias)
        if len({alias.lower() for alias in value}) != len(value):
            raise ValueError("SSH aliases must be distinct ignoring case")
        return value

    @field_validator("allowed_signers_file", mode="before")
    @classmethod
    def signers_path(cls, value: str | Path | None) -> Path | None:
        return None if value is None else GitIdentity.key_path(value)

    @model_validator(mode="after")
    def signing_selection(self) -> Self:
        if self.signing_identity is not None and self.signing_identity not in self.identities:
            raise ValueError("signing_identity must name a declared Git identity")
        return self
