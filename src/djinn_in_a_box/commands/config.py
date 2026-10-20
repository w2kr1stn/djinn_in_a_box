"""Configuration commands — init, show, path, set, and edit."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path, PurePosixPath
from typing import Annotated, cast, get_args

import typer
from pydantic import ValidationError
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from djinn_in_a_box.commands import _lifecycle
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import (
    AppConfig,
    AssistantConfig,
    BuildConfig,
    ConfigSyncConfig,
    ResourceLimits,
    ServeConfig,
    ShellConfig,
    WorkspaceMode,
)
from djinn_in_a_box.core.config_lock import ConfigDirectoryLockError, config_directory_lock
from djinn_in_a_box.core.config_sync import (
    CANONICAL_REMEDY,
    WRITE_PROBLEM_IDENTIFIER,
    ConfigSyncAudit,
    DriftClass,
    DriftEntry,
    workflow_item_label,
    workflow_item_summary,
    workflow_label_component,
    workflow_remedy,
)
from djinn_in_a_box.core.config_sync import (
    audit_config_sync as audit_workflow_config,
)
from djinn_in_a_box.core.config_sync import (
    sync_config as synchronize_workflow_config,
)
from djinn_in_a_box.core.config_workflow import (
    WorkflowDiscardResult,
    discard_workflow_target_drift,
    inspect_workflow_targets,
    inspection_failure,
)
from djinn_in_a_box.core.console import (
    blank,
    console,
    error,
    escape_nonprintable,
    info,
    rule,
    success,
    warning,
)
from djinn_in_a_box.core.decorators import handle_config_errors
from djinn_in_a_box.core.docker import (
    ensure_host_env,
    resolve_zone_roots,
    sops_age_key_file_problem,
)
from djinn_in_a_box.core.exceptions import (
    ConfigNotFoundError,
    ConfigValidationError,
    ZoneConfigurationError,
)
from djinn_in_a_box.core.hostinfo import detect_timezone, suggest_resources
from djinn_in_a_box.core.paths import CONFIG_DIR, CONFIG_FILE, get_project_root
from djinn_in_a_box.core.seeding import SeedingError, seed_config
from djinn_in_a_box.core.workflow_drift_backup import admit_backup_root

ALLOWED_CONFIG_KEYS: tuple[str, ...] = (
    "general.code_dir",
    "general.workspace",
    "general.timezone",
    "general.config_root",
    "general.shared_root",
    "general.local_root",
    "general.sops_age_key_file",
    "resources.cpu_limit",
    "resources.memory_limit",
    "resources.cpu_reservation",
    "resources.memory_reservation",
    "shell.skip_mounts",
    "shell.omp_theme_path",
    "config_sync.source",
    "build.network",
    "serve.opencode_host_port",
    "assistant.agent",
    "git.signing_identity",
    "git.allowed_signers_file",
    "hostctl.default_duration",
)
_LOCK_PROBLEM_IDENTIFIER = "canonical-lock-failed"


class _Unset:
    pass


_UNSET = _Unset()


def _print_config_table(
    title: str,
    rows: list[tuple[str, object]],
    *,
    path_labels: set[str] | None = None,
) -> None:
    rule(title)
    path_labels = path_labels or set()
    table = Table.grid(padding=(0, 2))
    table.add_column("Setting", style="table.category", no_wrap=True)
    table.add_column("Value", style="table.value", overflow="fold")
    for label, value in rows:
        rendered_value = Text(str(value), style="path") if label in path_labels else str(value)
        table.add_row(label, rendered_value)
    console.print(table)


def init_config(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Replace the entire configuration, including mounts and environment.",
        ),
    ] = False,
) -> None:
    """Initialize configuration in ~/.config/djinn_in_a_box/.

    Creates config.toml with user-provided settings through interactive prompts.
    Run this once before using other commands. --force replaces all settings,
    including declared mounts and environment; init asks only for built-in settings.

    [info.bold]Example:[/info.bold]

        djinn init              # Interactive setup

        djinn init --force      # Overwrite existing config
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        error(f"Failed to create configuration directory {CONFIG_DIR}: {e}")
        warning(f"Check that {CONFIG_DIR.parent} is writable, then retry.")
        raise typer.Exit(1) from e

    # Check for existing config
    if CONFIG_FILE.exists() and not force:
        warning(f"Configuration already exists: {CONFIG_FILE}")
        if not typer.confirm("Overwrite existing configuration?"):
            raise typer.Abort()

    # Interactive prompts
    info("Djinn in a Box Configuration Setup")
    console.print()

    workspace_modes = get_args(WorkspaceMode)
    while True:
        answer = typer.prompt("Workspace mode (aios/projects)", default="projects")
        if answer in workspace_modes:
            workspace = cast(WorkspaceMode, answer)
            break
        error(f"{answer!r} is not one of: {', '.join(workspace_modes)}.")
    root_label = "AIOS root" if workspace == "aios" else "Projects directory"
    code_dir = typer.prompt(
        f"{root_label} (mounted as /home/dev/{workspace})",
        default=str(Path.home() / workspace),
    )

    timezone = typer.prompt(
        "Timezone (for container)",
        default=detect_timezone(),
    )

    suggested_resources = suggest_resources()
    if typer.confirm(
        "Configure advanced options (resources, shell mounts)?",
        default=False,
    ):
        try:
            resources = ResourceLimits(
                cpu_limit=typer.prompt(
                    "CPU limit",
                    default=suggested_resources.cpu_limit,
                    type=int,
                ),
                memory_limit=typer.prompt(
                    "Memory limit",
                    default=suggested_resources.memory_limit,
                ),
                cpu_reservation=typer.prompt(
                    "CPU reservation",
                    default=suggested_resources.cpu_reservation,
                    type=int,
                ),
                memory_reservation=typer.prompt(
                    "Memory reservation",
                    default=suggested_resources.memory_reservation,
                ),
            )
        except ValidationError as e:
            for err in e.errors():
                error(str(err.get("msg", e)))
            raise typer.Exit(1) from e
        shell = ShellConfig(
            skip_mounts=typer.confirm(
                "Skip host shell config mounts?",
                default=False,
            )
        )
    else:
        resources = suggested_resources
        shell = ShellConfig()

    # Validate the selected workspace root exists or offer to create it
    code_path = Path(code_dir).expanduser()
    if not code_path.exists():
        if typer.confirm(f"Directory {code_path} does not exist. Create it?"):
            try:
                code_path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                error(f"Failed to create {root_label} {code_path}: {e}")
                warning(f"Check that {code_path.parent} is writable, then retry.")
                raise typer.Exit(1) from e
            success(f"Created directory: {code_path}")
        else:
            error(f"Cannot proceed without a valid {root_label}.")
            raise typer.Exit(1)

    # Create configuration — surface validation errors (e.g. a non-IANA timezone)
    # as a clean message instead of a raw pydantic traceback.
    try:
        config = AppConfig(
            code_dir=code_path,
            workspace=workspace,
            timezone=timezone,
            resources=resources,
            shell=shell,
        )
    except ValidationError as e:
        for err in e.errors():
            error(str(err.get("msg", e)))
        raise typer.Exit(1) from e

    # Save configuration
    try:
        save_config(config)
    except OSError as e:
        error(f"Failed to write configuration to {CONFIG_FILE}: {e}")
        warning(f"Check that {CONFIG_FILE.parent} is writable, then retry.")
        raise typer.Exit(1) from e
    success(f"Configuration written to {CONFIG_FILE}")

    # Outside the try: a missing repo marker is an install problem, not a
    # writability problem — its own FileNotFoundError message must surface as-is.
    project_root = get_project_root()
    try:
        created = seed_config(project_root, source=config.config_sync.source)
    except SeedingError as e:
        error(str(e))
        warning("Follow the remedy above, then retry.")
        raise typer.Exit(1) from e
    except PermissionError as e:
        error(f"Failed to seed default configuration: {e}")
        warning(
            f'Fix ownership with `sudo chown -R "$(id -u):$(id -g)" '
            f"{project_root / 'config'}`, then retry."
        )
        raise typer.Exit(1) from e
    except OSError as e:
        error(f"Failed to seed default configuration: {e}")
        warning("Check that the project config paths are writable, then retry.")
        raise typer.Exit(1) from e
    if created:
        success(f"Seeded {len(created)} default config file(s)")

    try:
        ensure_host_env(config)
    except ZoneConfigurationError as e:
        error(str(e))
        raise typer.Exit(1) from e
    except OSError as e:
        error(f"Failed to provision host directories: {e}")
        warning("Check that your home and config-root paths are writable, then retry.")
        raise typer.Exit(1) from e
    success(f"Host directories provisioned under {config.config_root}")

    rule("Next steps")
    console.print("  [muted]1.[/muted] djinn build    [muted]# Build the Docker image[/muted]")
    console.print("  [muted]2.[/muted] djinn start    [muted]# Start development shell[/muted]")
    blank()
    console.print(
        "  [muted]Sign in to each CLI inside that shell — the tools print a URL and "
        "accept a code pasted back from the browser. Codex needs "
        "`codex login --device-auth`; see \"First Authentication\" in the README.[/muted]"
    )


def _allowed_keys_message() -> str:
    return ", ".join(ALLOWED_CONFIG_KEYS)


def _parse_int_config_value(key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as e:
        error(f"Invalid value for {key}: {value!r}. Expected an integer.")
        raise typer.Exit(1) from e


def _parse_bool_config_value(key: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    error(f"Invalid value for {key}: {value!r}. Expected true or false.")
    raise typer.Exit(1)


def _build_config(
    config: AppConfig,
    *,
    code_dir: Path | None = None,
    workspace: str | None = None,
    timezone: str | None = None,
    config_root: Path | None = None,
    shared_root: Path | None | _Unset = _UNSET,
    local_root: Path | None | _Unset = _UNSET,
    sops_age_key_file: Path | None | _Unset = _UNSET,
    resources: ResourceLimits | None = None,
    shell: ShellConfig | None = None,
    config_sync: ConfigSyncConfig | None = None,
    build: BuildConfig | None = None,
) -> AppConfig:
    return AppConfig.model_validate(
        {
            **config.model_dump(),
            "code_dir": config.code_dir if code_dir is None else code_dir,
            "workspace": config.workspace if workspace is None else workspace,
            "timezone": config.timezone if timezone is None else timezone,
            "config_root": config.config_root if config_root is None else config_root,
            "shared_root": config.shared_root if isinstance(shared_root, _Unset) else shared_root,
            "local_root": config.local_root if isinstance(local_root, _Unset) else local_root,
            "sops_age_key_file": (
                config.sops_age_key_file
                if isinstance(sops_age_key_file, _Unset)
                else sops_age_key_file
            ),
            "resources": config.resources if resources is None else resources,
            "shell": config.shell if shell is None else shell,
            "config_sync": config.config_sync if config_sync is None else config_sync,
            "build": config.build if build is None else build,
        }
    )


def _set_config_value(config: AppConfig, key: str, value: str) -> AppConfig:
    if key == "serve.opencode_host_port":
        serve = ServeConfig.model_validate(
            {"opencode_host_port": _parse_int_config_value(key, value)}
        )
        return AppConfig.model_validate({**config.model_dump(), "serve": serve})
    if key == "assistant.agent":
        assistant = AssistantConfig.model_validate({"agent": value})
        return AppConfig.model_validate({**config.model_dump(), "assistant": assistant})
    if key == "hostctl.default_duration":
        from djinn_in_a_box.config.ssh import HostctlConfig

        hostctl = HostctlConfig.model_validate(
            {**config.hostctl.model_dump(), "default_duration": value}
        )
        return AppConfig.model_validate({**config.model_dump(), "hostctl": hostctl})
    if key in {"git.signing_identity", "git.allowed_signers_file"}:
        from djinn_in_a_box.config.ssh import GitConfig

        selected = None if value.strip().lower() in {"", "none", "null"} else value
        git = GitConfig.model_validate({**config.git.model_dump(), key.split(".")[1]: selected})
        return AppConfig.model_validate({**config.model_dump(), "git": git})
    if key == "general.workspace":
        return _build_config(config, workspace=value)
    if key == "general.code_dir":
        code_dir = Path(value).expanduser()
        if not code_dir.is_dir():
            root_label = "AIOS root" if config.workspace == "aios" else "Projects directory"
            error(f"{root_label} does not exist or is not a directory: {code_dir}")
            warning(f"Create it first: mkdir -p {code_dir}")
            warning("Or run `djinn init` to create it interactively.")
            raise typer.Exit(1)
        return _build_config(config, code_dir=code_dir)
    if key == "general.timezone":
        return _build_config(config, timezone=value)
    if key in {"general.config_root", "general.shared_root", "general.local_root"}:
        if key == "general.config_root":
            updated = _build_config(config, config_root=Path(value).expanduser())
        else:
            normalized = value.strip().lower()
            root = None if normalized in {"", "none", "null"} else Path(value).expanduser()
            if key == "general.shared_root":
                updated = _build_config(config, shared_root=root)
            else:
                updated = _build_config(config, local_root=root)
        old_roots = resolve_zone_roots(config)
        new_roots = resolve_zone_roots(updated)
        if old_roots != new_roots:
            warning(
                "Existing credentials/config remain at "
                f"config={old_roots.config_root}, shared={old_roots.shared_root}, "
                f"local={old_roots.local_root}; new roots are "
                f"config={new_roots.config_root}, shared={new_roots.shared_root}, "
                f"local={new_roots.local_root}. Zone data does not follow. "
                "New empty directories will be provisioned at the configured roots."
            )
        return updated
    if key == "general.sops_age_key_file":
        normalized = value.strip().lower()
        key_file = None if normalized in {"", "none", "null"} else Path(value).expanduser()
        updated = _build_config(config, sops_age_key_file=key_file)
        if updated.sops_age_key_file is not None:
            problem = sops_age_key_file_problem(updated.sops_age_key_file)
            if problem is not None:
                warning(
                    f"{updated.sops_age_key_file} {problem}. `djinn start` refuses to run "
                    "until it is fixed."
                )
        return updated
    if key == "resources.cpu_limit":
        resources = ResourceLimits(
            cpu_limit=_parse_int_config_value(key, value),
            memory_limit=config.resources.memory_limit,
            cpu_reservation=config.resources.cpu_reservation,
            memory_reservation=config.resources.memory_reservation,
        )
        return _build_config(config, resources=resources)
    if key == "resources.memory_limit":
        resources = ResourceLimits(
            cpu_limit=config.resources.cpu_limit,
            memory_limit=value,
            cpu_reservation=config.resources.cpu_reservation,
            memory_reservation=config.resources.memory_reservation,
        )
        return _build_config(config, resources=resources)
    if key == "resources.cpu_reservation":
        resources = ResourceLimits(
            cpu_limit=config.resources.cpu_limit,
            memory_limit=config.resources.memory_limit,
            cpu_reservation=_parse_int_config_value(key, value),
            memory_reservation=config.resources.memory_reservation,
        )
        return _build_config(config, resources=resources)
    if key == "resources.memory_reservation":
        resources = ResourceLimits(
            cpu_limit=config.resources.cpu_limit,
            memory_limit=config.resources.memory_limit,
            cpu_reservation=config.resources.cpu_reservation,
            memory_reservation=value,
        )
        return _build_config(config, resources=resources)
    if key == "shell.skip_mounts":
        shell = ShellConfig(
            skip_mounts=_parse_bool_config_value(key, value),
            omp_theme_path=config.shell.omp_theme_path,
        )
        return _build_config(config, shell=shell)
    if key == "shell.omp_theme_path":
        normalized = value.strip().lower()
        shell = ShellConfig(
            skip_mounts=config.shell.skip_mounts,
            omp_theme_path=None if normalized in {"", "none", "null"} else Path(value).expanduser(),
        )
        return _build_config(config, shell=shell)
    if key == "config_sync.source":
        config_sync = ConfigSyncConfig.model_validate({"source": value})
        return _build_config(config, config_sync=config_sync)
    if key == "build.network":
        build = BuildConfig.model_validate({"network": value.strip().lower()})
        return _build_config(config, build=build)

    error(f"Unknown configuration key: {key}")
    error(f"Allowed keys: {_allowed_keys_message()}")
    raise typer.Exit(1)


def _format_config_value(config: AppConfig, key: str) -> str:
    if key == "serve.opencode_host_port":
        return str(config.serve.opencode_host_port)
    if key == "assistant.agent":
        return config.assistant.agent
    if key == "git.signing_identity":
        return config.git.signing_identity or "unset"
    if key == "git.allowed_signers_file":
        return str(config.git.allowed_signers_file) if config.git.allowed_signers_file else "unset"
    if key == "general.workspace":
        return config.workspace
    if key == "general.code_dir":
        return str(config.code_dir)
    if key == "general.timezone":
        return config.timezone
    if key == "general.config_root":
        return str(config.config_root)
    if key == "general.shared_root":
        return "derived" if config.shared_root is None else str(config.shared_root)
    if key == "general.local_root":
        return "derived" if config.local_root is None else str(config.local_root)
    if key == "general.sops_age_key_file":
        return "unset" if config.sops_age_key_file is None else str(config.sops_age_key_file)
    if key == "resources.cpu_limit":
        return str(config.resources.cpu_limit)
    if key == "resources.memory_limit":
        return config.resources.memory_limit
    if key == "resources.cpu_reservation":
        return str(config.resources.cpu_reservation)
    if key == "resources.memory_reservation":
        return config.resources.memory_reservation
    if key == "shell.skip_mounts":
        return str(config.shell.skip_mounts)
    if key == "shell.omp_theme_path":
        return str(config.shell.omp_theme_path)
    if key == "config_sync.source":
        return config.config_sync.source
    if key == "build.network":
        return config.build.network

    msg = f"Unknown configuration key: {key}"
    raise ValueError(msg)


@handle_config_errors
def config_set(
    key: Annotated[str, typer.Argument(help="Configuration key to update.")],
    value: Annotated[str, typer.Argument(help="New value.")],
) -> None:
    """Set one supported configuration value."""
    if key == "config_sync.source":
        config_dir = get_project_root() / "config"
        value_written = False
        try:
            with config_directory_lock(config_dir, exclusive=True):
                updated = _set_and_save_config_value(key, value)
                value_written = True
        except ConfigDirectoryLockError:
            if value_written:
                warning("Configuration value was written, but releasing its lock failed.")
            raise
        success(f"{key} = {_format_config_value(updated, key)}")
        return

    updated = _set_and_save_config_value(key, value)
    success(f"{key} = {_format_config_value(updated, key)}")


def _set_and_save_config_value(key: str, value: str) -> AppConfig:
    config = load_config()

    try:
        updated = _set_config_value(config, key, value)
    except ValidationError as e:
        for err in e.errors():
            error(str(err.get("msg", e)))
        raise typer.Exit(1) from e

    try:
        save_config(updated)
    except OSError as e:
        error(f"Failed to write configuration to {CONFIG_FILE}: {e}")
        warning(f"Check that {CONFIG_FILE.parent} is writable, then retry.")
        raise typer.Exit(1) from e
    return updated


@handle_config_errors
def config_edit() -> None:
    """Edit configuration and validate syntax/schema; doctor/start check host paths."""
    config_dir = get_project_root() / "config"
    with config_directory_lock(config_dir, exclusive=True):
        _edit_config_file()


def _edit_config_file() -> None:
    editor = os.environ.get("EDITOR", "vi")
    try:
        command = shlex.split(editor)
    except ValueError as e:
        error(f"Cannot run editor {editor!r} ($EDITOR): {e}")
        raise typer.Exit(1) from e
    if not command:
        command = ["vi"]
    try:
        completed = subprocess.run([*command, str(CONFIG_FILE)], check=False)  # noqa: S603
    except (FileNotFoundError, PermissionError) as e:
        error(f"Cannot run editor {editor!r} ($EDITOR): {e}")
        raise typer.Exit(1) from e
    if completed.returncode != 0:
        error(f"Editor exited with status {completed.returncode}: {editor}")
        raise typer.Exit(1)

    try:
        load_config()
    except (ConfigNotFoundError, ConfigValidationError) as e:
        warning(f"Configuration problem after edit: {type(e).__name__}: {e}")


@handle_config_errors
def config_show(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            "-j",
            help="Output configuration as JSON.",
        ),
    ] = False,
) -> None:
    """Show current configuration.

    Displays all settings from ~/.config/djinn_in_a_box/config.toml.

    [info.bold]Example:[/info.bold]

        djinn config show           # Human-readable output

        djinn config show --json    # JSON output for scripting
    """
    config = load_config()

    if json_output:
        # Output as JSON (mode="json" ensures Path objects are serialized as strings)
        output = json.dumps(config.model_dump(mode="json"), indent=2)
        console.print(Text(output), highlight=False, soft_wrap=True)
    else:
        roots = resolve_zone_roots(config)
        # Human-readable output
        rule("Current Configuration")
        console.print(
            Text.assemble("  ", ("Config file:", "muted"), " ", (str(CONFIG_FILE), "path"))
        )
        console.print()

        _print_config_table(
            "General",
            [
                ("code_dir", config.code_dir),
                ("workspace", config.workspace),
                ("timezone", config.timezone),
                ("config_root", roots.config_root),
                ("shared_root", roots.shared_root),
                ("local_root", roots.local_root),
            ],
            path_labels={"code_dir", "config_root", "shared_root", "local_root"},
        )
        console.print()

        _print_config_table(
            "Resources",
            [
                ("cpu_limit", config.resources.cpu_limit),
                ("memory_limit", config.resources.memory_limit),
                ("cpu_reservation", config.resources.cpu_reservation),
                ("memory_reservation", config.resources.memory_reservation),
            ],
        )
        console.print()

        shell_rows: list[tuple[str, object]] = [("skip_mounts", config.shell.skip_mounts)]
        if config.shell.omp_theme_path:
            shell_rows.append(("omp_theme_path", config.shell.omp_theme_path))
        _print_config_table("Shell", shell_rows, path_labels={"omp_theme_path"})
        console.print()

        _print_config_table(
            "Config Sync",
            [("source", config.config_sync.source)],
        )
        console.print()

        _print_config_table("Build", [("network", config.build.network)])
        _print_config_table("Serve", [("opencode_host_port", config.serve.opencode_host_port)])
        _print_config_table("Assistant", [("agent", config.assistant.agent)])

        _print_config_table("Git", [
            ("signing_identity", config.git.signing_identity or "unset"),
            ("allowed_signers_file", config.git.allowed_signers_file or "unset"),
        ])
        for alias, identity in config.git.identities.items():
            console.print(Text(f"  {alias}: {identity.hostname} user={identity.user} "
                               f"key_file={identity.key_file} "
                               f"public_key_file={identity.public_key_file}"))
        _print_config_table("Hostctl", [("default_duration", config.hostctl.default_duration)])
        for alias, host in config.hostctl.hosts.items():
            console.print(Text(f"  {alias}: {host.address} user={host.user}"))
        rule("Mounts")
        from djinn_in_a_box.config.declarations import BindDeclaration

        for name, mount in config.mounts.items():
            if isinstance(mount, BindDeclaration):
                detail = f"bind source={mount.source} target={mount.target}"
                if mount.marker is not None:
                    detail += f" marker={mount.marker}"
                if mount.read_only:
                    detail += " read_only=true"
            else:
                detail = f"volume target={mount.target} backup={mount.backup}"
            console.print(Text(f"  {name}: {detail}"))
        rule("Environment")
        for key, value in config.environment.items():
            console.print(Text(f"  {key}={value}"))


def config_path() -> None:
    """Show configuration file path.

    Outputs the path to the main configuration file.
    Useful for scripting and manual editing.

    [info.bold]Example:[/info.bold]

        djinn config path           # Show path

        vim $(djinn config path)    # Edit config directly
    """
    # Raw path for scripting — no Rich path-highlighting (breaks $(...) consumers
    # when color output is forced).
    console.print(str(CONFIG_FILE), highlight=False)


_status_label = workflow_label_component
_status_location = workflow_item_label


def _print_workflow_audit(audit: ConfigSyncAudit) -> None:
    rule("Agent Workflow Configuration")
    console.print(f"Source: {_status_label(audit.configured_source)}")
    active = audit.manifest_source if audit.manifest_source is not None else "not-initialized"
    console.print(f"Manifest source: {_status_label(active)}")
    console.print(f"State: {'clean' if audit.clean else 'action-required'}")

    for drift in audit.drifts[:5]:
        console.print(
            Text(
                f"Drift: {_status_label(drift.kind.value)} "
                f"({_status_location(drift.tool, drift.relative_path, drift.key_path)})"
            )
        )
    if len(audit.drifts) > 5:
        console.print(f"and {len(audit.drifts) - 5} more")
    for problem in audit.problems[:5]:
        line = (
            f"Problem: {_status_label(problem.identifier)} "
            f"({_status_location(problem.tool, problem.relative_path)})"
        )
        if problem.identifier in {_LOCK_PROBLEM_IDENTIFIER, WRITE_PROBLEM_IDENTIFIER}:
            line += f": {escape_nonprintable(problem.message)}"
        console.print(Text(line))

    if len(audit.problems) > 5:
        console.print(f"and {len(audit.problems) - 5} more")
    if not audit.clean:
        operational_remedy = next(
            (problem.remedy for problem in audit.problems if problem.remedy is not None), None
        )
        if operational_remedy is not None:
            remedy = operational_remedy
        else:
            drift = next(
                (item.kind for item in audit.drifts if item.kind is not DriftClass.CLEAN), None
            )
            remedy = (
                CANONICAL_REMEDY
                if drift is None
                else workflow_remedy(
                    drift, audit.configured_source,
                    items=tuple(DriftEntry(item.kind, item.relative_path or PurePosixPath("."),
                                          item.key_path, reason=item.reason)
                                for item in audit.drifts),
                )
            )
        console.print(Text(f"Remedy: {remedy}"))


@handle_config_errors
def config_status() -> None:
    """Inspect workflow source, drift, and validation without modifying files."""
    audit = audit_workflow_config(get_project_root())
    _print_workflow_audit(audit)
    runtime_clean = _print_runtime_inspections(get_project_root())
    if not audit.clean or not runtime_clean:
        raise typer.Exit(1)


@handle_config_errors
def config_sync(
    discard_target_drift: Annotated[
        bool, typer.Option("--discard-target-drift", help="Back up and discard target edits.")
    ] = False,
) -> None:
    """Explicitly synchronize managed workflow views."""
    try:
        project_root = get_project_root()
        if discard_target_drift:
            config = load_config()
            try:
                admission = admit_backup_root(project_root, config)
            except ValueError as exc:
                # The refusal itself names the component and what to change.
                error(escape(escape_nonprintable(str(exc))))
                raise typer.Exit(1) from exc
            with _lifecycle.lifecycle_guard():
                config = load_config()
                admission = admit_backup_root(project_root, config)
                _lifecycle.guard_no_containers_running("discarding target drift")
                discarded = discard_workflow_target_drift(project_root, config, admission)
            for notice in discarded.notices:
                console.print(Text(escape_nonprintable(notice)))
            for label, entry in discarded.backups:
                location = workflow_item_label(
                    None if label == "config" else label,
                    entry.entry.path, entry.entry.key_path,
                    root=(None if label == "config"
                          else resolve_zone_roots(config).config_root / label),
                )
                action = "was missing" if entry.backup_path is None else "backed up"
                console.print(Text(escape_nonprintable(f"{location}: {action}")))
            if discarded.backup_directory is not None:
                console.print(
                    Text(escape_nonprintable(f"Backed up to {discarded.backup_directory}"))
                )
            elif discarded.failure is None:
                info("no target drift")
            if discarded.failure is not None:
                for problem in discarded.failure.problems:
                    console.print(
                        Text(escape_nonprintable(f"{problem.message} Remedy: {problem.remedy}"))
                    )
            if discarded.failed_phase is not None:
                _print_discard_incomplete(discarded)
                if discarded.failure is None and discarded.sync is not None:
                    _print_workflow_audit(discarded.sync.audit)
                raise typer.Exit(1)
            if discarded.failure is not None:
                raise typer.Exit(1)
            assert discarded.sync is not None
            result = discarded.sync
        else:
            result = synchronize_workflow_config(project_root)
    except (OSError, TypeError, ValueError) as exc:
        error(
            "Configuration synchronization could not start "
            f"({escape(escape_nonprintable(str(exc)))})."
        )
        warning("Run `djinn config status`, correct the reported problem, and retry.")
        raise typer.Exit(1) from exc

    _print_workflow_audit(result.audit)
    if not result.success or not result.audit.clean:
        error("Configuration synchronization is blocked.")
        raise typer.Exit(1)
    success(
        "Configuration synchronized "
        f"({len(result.changed_paths)} changed, {len(result.removed_paths)} removed)."
    )
    _print_runtime_inspections(get_project_root(), after_sync=True)


def _print_discard_incomplete(discarded: WorkflowDiscardResult) -> None:
    phase = escape_nonprintable(str(discarded.failed_phase))
    console.print(Text(f"Discard incomplete: failed during {phase}."))
    if discarded.completed:
        console.print(Text(f"Completed: {', '.join(discarded.completed)}."))
    if discarded.skipped:
        console.print(
            Text(f"Skipped: {', '.join(discarded.skipped)}; runtime repair is incomplete.")
        )
    elif discarded.failed_phase != "backup":
        console.print(Text("Runtime repair may be incomplete."))
    if discarded.backup_directory is not None:
        console.print(
            Text(escape_nonprintable(f"Backups were kept in {discarded.backup_directory}."))
        )
    console.print(
        Text("Fix the cause above, then rerun `djinn config sync --discard-target-drift`.")
    )


def _print_runtime_inspections(project_root: Path, *, after_sync: bool = False) -> bool:
    config = load_config()
    clean = True
    for inspection in inspect_workflow_targets(project_root, config):
        if inspection.clean:
            continue
        clean = False
        failure = inspection_failure(inspection, config).problems[0]
        if after_sync:
            warning(escape(f"{failure.message} Run `djinn config status`"))
        elif (
            inspection.result is not None
            and inspection.result.write_error is None
            and inspection.result.lock_error is None
        ):
            result = inspection.result
            labels = tuple(
                workflow_item_label(
                    inspection.target.tool,
                    item.path,
                    item.key_path,
                    root=inspection.target.destination_root,
                    origin=item.origin,
                )
                for item in result.drift_items
            )
            summary = workflow_item_summary(labels)
            location = f" ({summary})" if summary else ""
            console.print(Text(f"Drift: {result.drift_class.value}{location}"))
            console.print(Text(f"Remedy: {failure.remedy}"))
        else:
            # Operational failure or unchecked runtime: the cause, not a drift class.
            console.print(Text(f"Runtime: {failure.message}"))
            console.print(Text(f"Remedy: {failure.remedy}"))
    return clean
