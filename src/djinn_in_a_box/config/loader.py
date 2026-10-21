"""Configuration loading for Djinn in a Box.

Provides TOML-based configuration loading with:
- Automatic validation via Pydantic models
- Fallback to bundled defaults for agents
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any, cast

import tomli_w
from pydantic import ValidationError

from djinn_in_a_box.config.declarations import BindDeclaration, inspect_declarations
from djinn_in_a_box.config.defaults import DEFAULT_AGENTS
from djinn_in_a_box.config.models import AgentConfig, AppConfig
from djinn_in_a_box.core.console import escape_nonprintable
from djinn_in_a_box.core.exceptions import ConfigNotFoundError, ConfigValidationError
from djinn_in_a_box.core.paths import AGENTS_FILE, CONFIG_FILE


def _format_validation_errors(e: ValidationError) -> str:
    return "\n".join(
        f"  - {'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()
    )


def load_config(path: Path | None = None) -> AppConfig:
    """Load application config from TOML file.

    Loads configuration from the specified path or the default location
    (~/.config/djinn_in_a_box/config.toml). The TOML structure is flattened
    and validated with Pydantic.

    Args:
        path: Custom config file path. Defaults to CONFIG_FILE.

    Returns:
        Validated AppConfig instance.

    Raises:
        ConfigNotFoundError: If config file does not exist.
        ConfigValidationError: If config is invalid.
    """
    config_path = path or CONFIG_FILE

    if not config_path.exists():
        raise ConfigNotFoundError(config_path)

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigValidationError(f"Invalid TOML syntax in {config_path}: {e}") from e

    # Transform nested TOML structure to flat Pydantic model
    # [general] -> top-level, [shell] -> shell, [resources] -> resources
    general = data.get("general", {})
    config_dict = {**general, **{k: v for k, v in data.items() if k != "general"}}
    try:
        return AppConfig(**config_dict)
    except ValidationError as e:
        declarations = inspect_declarations(
            config_dict.get("mounts", {}), config_dict.get("environment", {})
        )
        reservation_config = None
        # The config error is retained; only a fully valid reservation context is used.
        with contextlib.suppress(ValidationError):
            reservation_config = AppConfig.model_validate(
                {**config_dict, "mounts": {}, "environment": {}}
            )
        raise ConfigValidationError(
            f"Configuration validation failed for {config_path}:\n{_format_validation_errors(e)}",
            declarations=declarations,
            reservation_config=reservation_config,
        ) from e


def load_agents(path: Path | None = None) -> dict[str, AgentConfig]:
    """Load agent configurations with fallback to defaults.

    Priority (first existing wins):
    1. Specified path (if provided)
    2. User's ~/.config/djinn_in_a_box/agents.toml (optional override)
    3. DEFAULT_AGENTS from defaults.py (the sole bundled source)

    Args:
        path: Custom agents file path. Defaults to automatic discovery.

    Returns:
        Dict mapping agent names to AgentConfig.
    """
    agents_path = path if path is not None else AGENTS_FILE
    try:
        os.lstat(agents_path)
    except FileNotFoundError as e:
        if path is not None:
            raise ConfigNotFoundError(agents_path) from e
        return dict(DEFAULT_AGENTS)
    except OSError as e:
        raise _agents_refusal(agents_path, f"entry cannot be inspected: {e}") from e

    return _load_agents_from_toml(agents_path)


def _agents_refusal(path: Path, rule: str) -> ConfigValidationError:
    return ConfigValidationError(
        escape_nonprintable(
            f"Agent definitions {str(path)!r} refused: {rule}. "
            "The host runs these commands, so the file must be a regular file you own "
            "with a single link, not writable by group or others, in a real directory "
            "you own that is not writable by group or others; fix it (for example "
            "`chmod go-w`, or replace a symlink or hard link with a real file) "
            "or remove it to use the built-in agents."
        )
    )


def _agents_content_error(*lines: str) -> ConfigValidationError:
    """A file that passed the host-input checks but does not hold valid agent definitions."""
    return ConfigValidationError("\n".join(escape_nonprintable(line) for line in lines))


def _read_agents_file(path: Path) -> bytes:
    """Check host execution input and read only the descriptor that passed."""
    directory_fd: int | None = None
    file_fd: int | None = None
    operation = "directory open"
    try:
        try:
            directory_fd = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
        except OSError as e:
            if e.errno in (errno.ENOTDIR, errno.ELOOP):
                raise _agents_refusal(
                    path, f"directory is a symlink or not a directory: {e}"
                ) from e
            raise
        operation = "directory metadata check"
        directory = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory.st_mode):
            raise _agents_refusal(path, "containing directory is not a directory")
        if directory.st_uid != os.getuid():
            raise _agents_refusal(path, "directory is not owned by the invoking user")
        if directory.st_mode & 0o022:
            raise _agents_refusal(path, "directory is writable by group or others")

        operation = "file open"
        try:
            file_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
        except OSError as e:
            rules: dict[int | None, str] = {
                errno.ELOOP: "file is a symlink",
                errno.ENXIO: "file is not a regular file",
                errno.EACCES: "file cannot be read",
                errno.ENOENT: "file changed while loading",
            }
            rule = rules.get(e.errno, "file cannot be opened")
            raise _agents_refusal(path, f"{rule}: {e}") from e
        operation = "file metadata check"
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise _agents_refusal(path, "file is not a regular file")
        if info.st_nlink != 1:
            raise _agents_refusal(path, "file must have a single link")
        if info.st_uid != os.getuid():
            raise _agents_refusal(path, "file is not owned by the invoking user")
        if info.st_mode & 0o022:
            raise _agents_refusal(path, "file is writable by group or others")

        operation = "file read"
        chunks: list[bytes] = []
        while chunk := os.read(file_fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as e:
        raise _agents_refusal(path, f"{operation} failed: {e}") from e
    finally:
        try:
            if file_fd is not None:
                os.close(file_fd)
        finally:
            if directory_fd is not None:
                os.close(directory_fd)


def _load_agents_from_toml(path: Path) -> dict[str, AgentConfig]:
    """Load agents from a TOML file.

    Expected format:
        [agents.claude]
        binary = "claude"
        description = "Anthropic Claude Code CLI"
        ...

    Args:
        path: Path to agents.toml file.

    Returns:
        Dict mapping agent names to AgentConfig.

    Raises:
        ConfigValidationError: If TOML is invalid or agents malformed.
    """
    try:
        text = _read_agents_file(path).decode("utf-8")
    except UnicodeDecodeError as e:
        raise _agents_content_error(f"Invalid UTF-8 in {path}: {e}") from e
    try:
        data = tomllib.loads(text)
    except (ValueError, RecursionError) as e:  # TOMLDecodeError and parser limits
        raise _agents_content_error(f"Invalid TOML syntax in {path}: {str(e)[:200]}") from e

    agents_data: object = data.get("agents")
    if (
        not isinstance(agents_data, dict)
        or not agents_data
        or any(
            not isinstance(value, dict)
            for value in cast(dict[str, Any], agents_data).values()
        )
    ):
        raise _agents_content_error(
            f"No agents defined in {path}. Expected [agents.<name>] sections."
        )

    try:
        return {
            name: AgentConfig(**agent_data)
            for name, agent_data in cast(dict[str, Any], agents_data).items()
        }
    except ValidationError as e:
        raise _agents_content_error(
            f"Invalid agent configuration in {path}:", *_format_validation_errors(e).splitlines()
        ) from e


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Save configuration to TOML file.

    Transforms AppConfig back to nested TOML structure and writes it.
    Creates parent directories if they do not exist.

    Args:
        config: AppConfig to save.
        path: Target path. Defaults to CONFIG_FILE.
    """
    config_path = path or CONFIG_FILE

    # Ensure parent directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Transform AppConfig to TOML structure: [general] wraps top-level fields
    data = config.model_dump(mode="json", exclude_none=True)
    for name, mount in config.mounts.items():
        if isinstance(mount, BindDeclaration) and not mount.read_only:
            data["mounts"][name].pop("read_only")
    toml_data = {
        "general": {
            "code_dir": data.pop("code_dir"),
            "workspace": data.pop("workspace"),
            "timezone": data.pop("timezone"),
            "config_root": data.pop("config_root"),
            **({"shared_root": data.pop("shared_root")} if "shared_root" in data else {}),
            **({"local_root": data.pop("local_root")} if "local_root" in data else {}),
            **(
                {"sops_age_key_file": data.pop("sops_age_key_file")}
                if "sops_age_key_file" in data
                else {}
            ),
        },
        **data,
    }

    # Atomic write: write to temp file then rename to avoid corruption on interrupt
    fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(toml_data, f)
        os.replace(tmp_path, config_path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
