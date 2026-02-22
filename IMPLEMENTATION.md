# Djinn in a Box — Implementation Guide

> **Version**: 1.0.0
> **Architecture**: Python Typer CLI with Pydantic v2 Configuration
> **License**: MIT

Complete technical reference for the Djinn in a Box CLI application. Covers every component, their interactions, data flows, and implementation patterns.

---

## Table of Contents

1. [Executive Overview](#1-executive-overview)
2. [Architecture Overview](#2-architecture-overview)
3. [Package Structure](#3-package-structure)
4. [Entry Points & CLI Layer](#4-entry-points--cli-layer)
5. [Commands Layer](#5-commands-layer)
6. [Configuration System](#6-configuration-system)
7. [Core Infrastructure](#7-core-infrastructure)
8. [Process Flows](#8-process-flows)
9. [Error Handling Strategy](#9-error-handling-strategy)
10. [Type System & Validation](#10-type-system--validation)
11. [Testing Architecture](#11-testing-architecture)
12. [Extension Points](#12-extension-points)

---

## 1. Executive Overview

### 1.1 Purpose

Djinn in a Box is a Python CLI application that manages Docker-based development environments for AI coding agents. It provides container lifecycle management, headless agent execution, and MCP Gateway integration.

### 1.2 Key Capabilities

| Capability | Description |
|------------|-------------|
| **Container Lifecycle** | Build, start, stop, enter development containers |
| **Agent Execution** | Run AI agents (Claude, Gemini, Codex, OpenCode) in headless mode |
| **MCP Gateway** | Manage Model Context Protocol servers for AI tool access |
| **Configuration** | TOML-based configuration with Pydantic v2 validation |
| **Volume Management** | Clean up Docker volumes by category |

### 1.3 Two CLI Entry Points

```
djinn         # Main CLI for container and agent management
mcpgateway    # Dedicated CLI for MCP Gateway operations
```

---

## 2. Architecture Overview

### 2.1 Layer Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLI Layer                               │
│   cli/djinn.py                cli/mcpgateway.py                 │
│   (Typer app, command             (Typer app, command            │
│    registration, callbacks)        registration)                 │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Commands Layer                            │
│   commands/container.py   commands/agent.py   commands/mcp.py   │
│   commands/config.py                                            │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                 Configuration + Core Layers                     │
│   config/models.py   config/loader.py   config/defaults.py      │
│   core/docker.py     core/console.py    core/paths.py           │
│   core/decorators.py core/exceptions.py core/theme.py           │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                        External Systems                         │
│              Docker Engine          File System                  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Dependency Flow

```
cli/djinn.py ──────┬──► commands/agent.py ──────┬──► config/loader.py
                   │                            │
                   ├──► commands/container.py ──┤──► config/models.py
                   │                            │
                   ├──► commands/config.py ─────┤──► core/docker.py
                   │                            │
cli/mcpgateway.py ─┴──► commands/mcp.py ────────┴──► core/console.py
                                                │
                                                └──► core/paths.py
```

### 2.3 Design Principles

1. **Strict Layer Separation**: Each layer only depends on layers below it
2. **No Shell=True**: All subprocess calls use explicit argument lists
3. **Type Safety**: Pyright strict mode, Pydantic v2 validation, frozen models
4. **Fail-Fast**: ConfigNotFoundError raised early, explicit error handling
5. **Decorator Patterns**: Reusable error handling via `@handle_config_errors`

---

## 3. Package Structure

```
djinn-in-a-box/
├── pyproject.toml                    # Package configuration, entry points
├── devops.py                         # Task runner (fmt, test, clean)
├── src/
│   └── djinn_in_a_box/
│       ├── __init__.py               # Package version via importlib.metadata
│       ├── py.typed                  # PEP 561 marker
│       │
│       ├── cli/                      # CLI entry points (thin wrappers)
│       │   ├── __init__.py
│       │   ├── djinn.py              # Main CLI: Typer app + command registration
│       │   └── mcpgateway.py         # MCP Gateway CLI: Typer app + registration
│       │
│       ├── commands/                 # Business logic (one module per domain)
│       │   ├── __init__.py
│       │   ├── agent.py              # Agent execution: build_agent_command, run, agents
│       │   ├── config.py             # Config commands: init, show, path
│       │   ├── container.py          # Container lifecycle: build, start, auth, clean, ...
│       │   └── mcp.py                # MCP Gateway: start, stop, enable, disable, ...
│       │
│       ├── config/                   # Configuration system
│       │   ├── __init__.py           # Re-exports
│       │   ├── models.py             # Pydantic v2 models (AppConfig, AgentConfig, ...)
│       │   ├── loader.py             # TOML loading/saving with atomic writes
│       │   └── defaults.py           # Volume categories, default agent configs
│       │
│       └── core/                     # Shared infrastructure
│           ├── __init__.py
│           ├── docker.py             # Docker/compose wrappers, volume/network ops
│           ├── console.py            # Rich console output (error/info/success/warning)
│           ├── theme.py              # TodAI Design Theme (colors, styles, icons)
│           ├── paths.py              # XDG paths, project root detection
│           ├── decorators.py         # @handle_config_errors
│           └── exceptions.py         # ConfigNotFoundError, ConfigValidationError
│
└── tests/                            # Test suite (139 tests)
    ├── conftest.py                   # Shared fixtures (mock_home, mock_app_config)
    ├── test_cli_djinn.py
    ├── test_config_loader.py
    ├── test_config_models.py
    ├── test_decorators.py
    ├── test_docker.py
    ├── test_paths.py
    └── test_commands/
        ├── __init__.py
        ├── test_agent.py
        ├── test_container.py
        └── test_mcp.py
```

---

## 4. Entry Points & CLI Layer

### 4.1 pyproject.toml Entry Points

```toml
[project.scripts]
djinn = "djinn_in_a_box.cli.djinn:app"
mcpgateway = "djinn_in_a_box.cli.mcpgateway:app"
```

### 4.2 cli/djinn.py — Main CLI (93 lines)

Thin registration layer — all logic lives in `commands/`.

```python
app = typer.Typer(
    name="djinn",
    help="Djinn in a Box CLI - Manage AI development containers",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Config subcommand group
config_app = typer.Typer(help="Manage configuration files.", no_args_is_help=True)
app.command("init")(init_config)
config_app.command("show")(config_show)
config_app.command("path")(config_path)
app.add_typer(config_app, name="config")

# Container lifecycle
app.command()(build)
app.command()(start)
app.command()(auth)
app.command()(status)
app.command()(audit)
app.command()(update)
app.command()(enter)
app.add_typer(clean_app, name="clean")

# Agent commands
app.command()(run)
app.command()(agents)
```

### 4.3 cli/mcpgateway.py — MCP Gateway CLI (68 lines)

```python
app = typer.Typer(
    name="mcpgateway",
    help="MCP Gateway CLI - Manage Model Context Protocol servers",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Gateway lifecycle
app.command("start")(mcp.start)
app.command("stop")(mcp.stop)
app.command("restart")(mcp.restart)
app.command("status")(mcp.status)
app.command("logs")(mcp.logs)

# Server management
app.command("enable")(mcp.enable)
app.command("disable")(mcp.disable)
app.command("servers")(mcp.servers)
app.command("catalog")(mcp.catalog)

# Diagnostics
app.command("test")(mcp.test)
app.command("clean")(mcp.clean)
```

---

## 5. Commands Layer

### 5.1 commands/config.py — Configuration Commands (162 lines)

| Command | Description |
|---------|-------------|
| `init_config` | Interactive first-time setup — prompts for code_dir, timezone, creates config.toml and copies bundled agents.toml |
| `config_show` | Display current configuration (human-readable or `--json`) |
| `config_path` | Print path to config.toml |

### 5.2 commands/agent.py — Agent Execution (283 lines)

#### build_agent_command()

Constructs the shell command for agent execution, using `shlex.quote()` for safety:

```python
def build_agent_command(
    agent_config: AgentConfig,
    *,
    write: bool = False,
    json_output: bool = False,
    model: str | None = None,
) -> str:
```

**Example Output**: `claude -p --dangerously-skip-permissions "$AGENT_PROMPT"`

#### run() Command

```python
@handle_config_errors
def run(
    agent: str,          # Agent name: claude, gemini, codex, opencode
    prompt: str,         # Prompt text
    write: bool,         # Allow file modifications
    json_output: bool,   # JSON output format
    model: str | None,   # Model override
    docker: bool,        # Docker socket access via proxy
    docker_direct: bool, # Direct Docker socket access
    firewall: bool,      # Network firewall
    mount: Path | None,  # Workspace mount path
    timeout: int | None, # Timeout in seconds
) -> None:
```

**Process Flow**: load config → validate agent → ensure network → build command → compose_run (headless) → output stdout/stderr → cleanup proxy → exit with agent's return code

#### agents() Command

Lists available agents with optional `--verbose` or `--json` output.

### 5.3 commands/container.py — Container Lifecycle (622 lines)

| Command | Decorator | Description |
|---------|-----------|-------------|
| `build` | — | Build Docker image (`--no-cache`) |
| `start` | `@handle_config_errors` | Start interactive shell (`--docker`, `--docker-direct`, `--firewall`, `--here`, `--mount`) |
| `auth` | `@handle_config_errors` | OAuth authentication with host network (`--docker`, `--docker-direct`) |
| `status` | — | Show containers, volumes, networks, and services |
| `enter` | — | Open new shell in running container |
| `audit` | — | Show Docker proxy audit log (`--tail`) |
| `update` | — | Run agent version update script |

#### clean Subcommand Group

```python
clean_app = typer.Typer(name="clean", help="Manage containers and volumes")
```

| Subcommand | Description |
|------------|-------------|
| `clean` (default) | `docker compose down` — remove containers only |
| `clean volumes` | List or delete volumes by category (`--credentials`, `--tools`, `--cache`, `--data`) |
| `clean all` | Remove everything: containers, volumes, network (`--force`) |

### 5.4 commands/mcp.py — MCP Gateway (400 lines)

#### Constants

```python
GATEWAY_CONTAINER: str = "mcp-gateway"
GATEWAY_ENDPOINT_CONTAINER: str = "http://mcp-gateway:8811"
GATEWAY_ENDPOINT_HOST: str = "http://localhost:8811"
```

#### Helper Functions (plain functions, not decorators)

```python
def _require_mcp_cli() -> None:
    """Check for MCP CLI plugin and exit with error if not installed."""

def _require_running() -> None:
    """Exit with error if MCP Gateway is not running."""

def _run_mcp_compose(args: list[str], error_msg: str) -> None:
    """Run docker compose in the MCP directory. Raises typer.Exit on failure."""
```

#### enable/disable via shared _toggle_server()

```python
def _toggle_server(server: str, action: str) -> None:
    """Enable or disable an MCP server by action name."""
    _require_mcp_cli()
    _require_running()
    # subprocess.run(["docker", "mcp", "server", action, server])
```

#### test() — Connectivity Diagnostics

```
Container status:           Running/Not running
Localhost endpoint:         OK/Not responding
Container endpoint:         OK/Not responding
Docker socket access:       OK/Failed
docker mcp CLI plugin:      Installed/Not installed
```

---

## 6. Configuration System

### 6.1 config/models.py — Pydantic Models (177 lines)

All models use `ConfigDict(extra="forbid", frozen=True)`.

#### AgentConfig

```python
class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binary: Annotated[str, Field(min_length=1)]
    description: str = ""
    headless_flags: list[str] = Field(default_factory=list)
    read_only_flags: list[str] = Field(default_factory=list)
    write_flags: list[str] = Field(default_factory=list)
    json_flags: list[str] = Field(default_factory=list)
    model_flag: str = "--model"
    prompt_template: str = '"$AGENT_PROMPT"'
```

#### ResourceLimits with Cross-Field Validation

```python
class ResourceLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_limit: Annotated[int, Field(ge=1, le=128)] = 6
    memory_limit: str = "12G"
    cpu_reservation: Annotated[int, Field(ge=1, le=128)] = 2
    memory_reservation: str = "4G"

    @model_validator(mode="after")
    def validate_reservations(self) -> ResourceLimits:
        """Ensure cpu_reservation ≤ cpu_limit and memory_reservation ≤ memory_limit."""
```

#### AppConfig (Root)

```python
class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code_dir: Path            # Required, must exist as directory
    timezone: str = "Europe/Berlin"
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    shell: ShellConfig = Field(default_factory=ShellConfig)
```

### 6.2 config/loader.py — TOML Operations (184 lines)

#### load_config()

```python
def load_config(path: Path | None = None) -> AppConfig:
    """Load config from TOML. Defaults to ~/.config/djinn-in-a-box/config.toml."""
```

Transforms nested TOML `[general]` section to flat Pydantic model. Uses atomic writes via `tempfile.mkstemp()` + `os.replace()`.

#### load_agents()

```python
def load_agents(path: Path | None = None) -> dict[str, AgentConfig]:
```

Priority chain: explicit path → `~/.config/djinn-in-a-box/agents.toml` → bundled `config/agents.toml` → `DEFAULT_AGENTS`

#### save_config()

```python
def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Atomic write: tempfile + os.replace to avoid corruption on interrupt."""
```

### 6.3 config/defaults.py — Default Values (68 lines)

#### Volume Categories

```python
VOLUME_CATEGORIES: Final[dict[str, list[str]]] = {
    "credentials": [
        "djinn-claude-config", "djinn-gemini-config",
        "djinn-codex-config", "djinn-opencode-config", "djinn-gh-config",
    ],
    "tools": [
        "djinn-azure-config", "djinn-pulumi-config", "djinn-tools-cache",
    ],
    "cache": [
        "djinn-uv-cache", "djinn-vscode-server",
    ],
    "data": [
        "djinn-opencode-data", "djinn-vscode-workspaces",
    ],
}
```

#### Default Agent Configurations

```python
DEFAULT_AGENTS: Final[dict[str, AgentConfig]] = {
    "claude": AgentConfig(
        binary="claude",
        description="Anthropic Claude Code CLI",
        headless_flags=["-p"],
        read_only_flags=["--permission-mode", "plan"],
        write_flags=["--dangerously-skip-permissions"],
        json_flags=["--output-format", "json"],
        model_flag="--model",
    ),
    "gemini": AgentConfig(
        binary="gemini",
        description="Google Gemini CLI",
        headless_flags=["-p"],
        json_flags=["--output-format", "json"],
        model_flag="-m",
    ),
    "codex": AgentConfig(
        binary="codex",
        description="OpenAI Codex CLI",
        headless_flags=["exec"],
        write_flags=["--full-auto"],
        json_flags=["--json"],
        model_flag="--model",
    ),
    "opencode": AgentConfig(
        binary="opencode",
        description="Anomaly OpenCode CLI",
        headless_flags=["run"],
        read_only_flags=["--agent", "plan"],
        json_flags=["--format", "json"],
        model_flag="-m",
    ),
}
```

### 6.4 Configuration File Locations

XDG-compliant: `~/.config/djinn-in-a-box/`

#### config.toml

```toml
[general]
code_dir = "/home/user/projects"
timezone = "Europe/Berlin"

[resources]
cpu_limit = 6
memory_limit = "12G"
cpu_reservation = 2
memory_reservation = "4G"

[shell]
skip_mounts = false
```

---

## 7. Core Infrastructure

### 7.1 core/docker.py — Docker Operations (412 lines)

#### Data Classes

```python
@dataclass
class ContainerOptions:
    docker_enabled: bool = False    # Docker socket via proxy
    docker_direct: bool = False     # Direct Docker socket (no proxy)
    firewall_enabled: bool = False  # Network firewall
    mount_path: Path | None = None  # Workspace mount

@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def success(self) -> bool:
        return self.returncode == 0
```

#### Network Operations

```python
DJINN_NETWORK: str = "djinn-network"

def network_exists(name: str = DJINN_NETWORK) -> bool: ...
def ensure_network(name: str = DJINN_NETWORK) -> bool: ...
def delete_network(name: str) -> bool: ...
```

#### Compose Operations

```python
def compose_build(*, no_cache: bool = False) -> RunResult: ...
def compose_run(
    config: AppConfig,
    options: ContainerOptions,
    *,
    command: str | None = None,
    interactive: bool = True,
    env: dict[str, str] | None = None,
    service: str = "dev",
    profile: str | None = None,
    timeout: int | None = None,
) -> RunResult: ...
def compose_up(services: list[str] | None = None, *, docker_enabled: bool = False) -> RunResult: ...
def compose_down() -> RunResult: ...
```

`compose_run` handles timeouts (returns exit code 124), `FileNotFoundError` (127), and `PermissionError` (126).

#### Container & Volume Operations

```python
def is_container_running(name: str) -> bool: ...
def get_running_containers(prefix: str = "djinn") -> list[str]: ...
def cleanup_docker_proxy(docker_enabled: bool) -> None: ...
def volume_exists(name: str) -> bool: ...
def delete_volume(name: str) -> bool: ...
def delete_volumes(names: list[str]) -> dict[str, bool]: ...
```

#### Internal Helpers

```python
def _docker_inspect(resource: str, name: str) -> bool: ...     # Shared inspect check
def _docker_list(cmd: list[str]) -> list[str]: ...              # Shared list parser
def _warn_subprocess_error(action: str, result: ...) -> None: ...  # Stderr warning
def get_compose_files(docker_enabled, docker_direct) -> list[str]: ...
def get_shell_mount_args(config: AppConfig) -> list[str]: ...
```

### 7.2 core/console.py — Rich Output (51 lines)

```python
console: Console = Console(theme=TODAI_THEME)        # stdout
err_console: Console = Console(stderr=True, theme=TODAI_THEME)  # stderr

def error(message: str) -> None: ...
def success(message: str) -> None: ...
def info(message: str) -> None: ...
def warning(message: str) -> None: ...
def status_line(label: str, value: str, style: str = "status.enabled") -> None: ...
def header(title: str) -> None: ...
def blank() -> None: ...
```

### 7.3 core/theme.py — TodAI Design Theme (38 lines)

```python
TODAI_THEME: Theme = Theme({
    # Semantic
    "success": Style(color="#03b971"),
    "error": Style(color="#9c0136", bold=True),
    "warning": Style(color="#f5b332"),
    "info": Style(color="#0e8ac8"),
    "info.bold": Style(color="#0e8ac8", bold=True),
    # Primary
    "primary": Style(color="#69B9A1"),
    "primary.bold": Style(color="#69B9A1", bold=True),
    # Text
    "muted": Style(color="#b2bec3"),
    # Header
    "header": Style(color="#0e8ac8", bold=True),
    # Status
    "status.enabled": Style(color="#03b971"),
    "status.disabled": Style(color="#f5b332"),
    "status.error": Style(color="#9c0136"),
    # Table
    "table.title": Style(color="#0e8ac8", bold=True),
    "table.header": Style(bold=True),
    "table.category": Style(color="#f5b332"),
    "table.value": Style(color="#b2bec3"),
})

ICONS: dict[str, str] = {
    "success": "✓",
    "error": "✗",
    "warning": "⚠",
    "info": "ℹ",
}
```

### 7.4 core/paths.py — Path Utilities (70 lines)

```python
CONFIG_DIR: Path = Path.home() / ".config" / "djinn-in-a-box"
CONFIG_FILE: Path = CONFIG_DIR / "config.toml"
AGENTS_FILE: Path = CONFIG_DIR / "agents.toml"

@functools.cache
def get_project_root() -> Path:
    """Search upward for docker-compose.yml. Raises FileNotFoundError."""

def resolve_mount_path(path: str | Path) -> Path:
    """Resolve, expand, validate. Raises FileNotFoundError or NotADirectoryError."""
```

### 7.5 core/exceptions.py — Exception Types (17 lines)

```python
class ConfigNotFoundError(FileNotFoundError):
    """Raised when config file is missing. Stores path attribute."""
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"Configuration not found: {path}\nRun 'djinn init' to create configuration.")

class ConfigValidationError(ValueError):
    """Raised when config validation fails."""
```

### 7.6 core/decorators.py — Error Handling (33 lines)

```python
def handle_config_errors(func: Callable[P, R]) -> Callable[P, R]:
    """Catch ConfigNotFoundError and ConfigValidationError, exit with code 1."""
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except (ConfigNotFoundError, ConfigValidationError) as e:
            error(str(e))
            raise typer.Exit(1) from None
    return wrapper
```

---

## 8. Process Flows

### 8.1 First-Time Setup (djinn init)

```
djinn init
  │
  ▼
Check config.toml exists? ──yes──► Prompt "Overwrite?" (unless --force)
  │no
  ▼
Prompt for code_dir and timezone
  │
  ▼
Validate code_dir (offer to create if missing)
  │
  ▼
AppConfig(...) → save_config() (atomic write)
  │
  ▼
Copy bundled agents.toml → ~/.config/djinn-in-a-box/agents.toml
  │
  ▼
Display next steps: build → auth → start
```

### 8.2 Running an Agent (djinn run claude "Fix the bug" --write)

```
@handle_config_errors
  │
  ▼
load_config() + load_agents()
  │
  ▼
Validate agent name ∈ agent_configs
  │
  ▼
ensure_network(DJINN_NETWORK)
  │
  ▼
Resolve workspace (--mount or cwd)
  │
  ▼
Print status to stderr
  │
  ▼
build_agent_command() → "claude -p --dangerously-skip-permissions \"$AGENT_PROMPT\""
  │
  ▼
compose_run(config, options, command=..., interactive=False, env={"AGENT_PROMPT": prompt})
  │
  ▼
Output stdout/stderr → cleanup_docker_proxy() → Exit(returncode)
```

### 8.3 Container Start (djinn start --docker --here)

```
@handle_config_errors
  │
  ▼
Validate --docker and --docker-direct are mutually exclusive
  │
  ▼
load_config() → ensure_network()
  │
  ▼
Resolve mount_path (--here = cwd, --mount = explicit path)
  │
  ▼
Print status lines to stderr (Projects, Docker, Firewall, Workspace, Shell)
  │
  ▼
compose_run(config, options, interactive=True)
  │
  ▼
Interactive zsh session → user types 'exit'
  │
  ▼
cleanup_docker_proxy() → Exit(returncode)
```

### 8.4 MCP Gateway Start (mcpgateway start)

```
_require_mcp_cli() → Exit(1) if "docker mcp" not installed
  │
  ▼
ensure_network(DJINN_NETWORK)
  │
  ▼
_run_mcp_compose(["up", "-d"], ...)
  │
  ▼
time.sleep(3) → Wait for container
  │
  ▼
is_container_running("mcp-gateway")?
  ├─yes─► success() + display endpoints
  └─no──► docker compose logs → Exit(1)
```

---

## 9. Error Handling Strategy

### 9.1 Exception Hierarchy

```
FileNotFoundError
└── ConfigNotFoundError      # core/exceptions.py — config file missing

ValueError
└── ConfigValidationError    # core/exceptions.py — invalid config values

ValidationError              # Pydantic — model validation failed
```

### 9.2 Error Handling Patterns

**Decorator pattern** (`@handle_config_errors`): Used on `run`, `start`, `auth`, `build`, `agents`, `config_show`. Catches both `ConfigNotFoundError` and `ConfigValidationError`.

**Guard function pattern** (`_require_mcp_cli()`, `_require_running()`): Called at the top of MCP commands. Checks a precondition and raises `typer.Exit(1)` on failure.

**Explicit check pattern**: Network creation, Docker availability checks.

**Return code propagation**: Subprocess exit codes propagated via `raise typer.Exit(result.returncode)`.

### 9.3 Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error (config not found, validation, etc.) |
| 124 | Timeout (compose_run) |
| 126 | Permission denied (compose_run) |
| 127 | Command not found (compose_run) |
| >1 | Subprocess exit code (propagated from Docker) |

---

## 10. Type System & Validation

### 10.1 Pydantic v2 Configuration

All models: `ConfigDict(extra="forbid", frozen=True)`.

### 10.2 Field-Level Validation

```python
cpu_limit: Annotated[int, Field(ge=1, le=128)] = 6
binary: Annotated[str, Field(min_length=1)]
```

### 10.3 Cross-Field Validation

```python
@model_validator(mode="after")
def validate_reservations(self) -> ResourceLimits:
    # cpu_reservation ≤ cpu_limit
    # memory_reservation ≤ memory_limit (bytes comparison)
```

### 10.4 Type-Safe Decorators

```python
P = ParamSpec("P")
R = TypeVar("R")

def handle_config_errors(func: Callable[P, R]) -> Callable[P, R]:
```

Preserves full type safety — Pyright verifies argument and return types through the decorator.

---

## 11. Testing Architecture

### 11.1 Test Organization

```
tests/
├── conftest.py              # Shared fixtures
├── test_cli_djinn.py        # CLI entry point tests
├── test_config_loader.py    # Config loading tests
├── test_config_models.py    # Pydantic model tests
├── test_decorators.py       # Decorator tests
├── test_docker.py           # Docker operations tests
├── test_paths.py            # Path utility tests
└── test_commands/
    ├── __init__.py
    ├── test_agent.py        # Agent command tests
    ├── test_container.py    # Container command tests
    └── test_mcp.py          # MCP command tests
```

### 11.2 Key Fixtures (conftest.py)

```python
@pytest.fixture
def mock_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mock the home directory for testing XDG paths."""

@pytest.fixture
def mock_app_config(tmp_path: Path) -> AppConfig:
    """Provide mock app configuration with temporary projects dir."""
```

### 11.3 Testing Patterns

- **Model validation**: `pytest.raises(ValidationError, match=...)` for constraint violations
- **Docker operations**: `subprocess.run` mocked via `unittest.mock.patch`
- **CLI commands**: Direct function calls with mocked dependencies
- **Config loading**: `tmp_path` fixtures with real TOML files

### 11.4 Coverage

- **139 tests** total
- All critical paths covered including error handling

---

## 12. Extension Points

### 12.1 Adding a New Agent

Edit `~/.config/djinn-in-a-box/agents.toml` or add to `DEFAULT_AGENTS` in `config/defaults.py`.

### 12.2 Adding a New Command

1. Create function in appropriate `commands/` module
2. Register in `cli/djinn.py`: `app.command()(my_command)`

### 12.3 Adding a New Volume Category

```python
# config/defaults.py
VOLUME_CATEGORIES["my-category"] = ["djinn-my-volume"]
```

---

## Appendix A: Command Reference

### djinn

```
djinn --version
djinn init [--force]
djinn config show [--json]
djinn config path

djinn build [--no-cache]
djinn start [--docker] [--docker-direct] [--firewall] [--here] [--mount PATH]
djinn auth [--docker] [--docker-direct]
djinn status
djinn enter
djinn audit [--tail N]
djinn update

djinn run AGENT PROMPT [--write] [--json] [--model MODEL] [--docker] [--docker-direct] [--firewall] [--mount PATH] [--timeout SECONDS]
djinn agents [--verbose] [--json]

djinn clean                    # Remove containers only
djinn clean volumes            # List volumes by category
djinn clean volumes --credentials/--tools/--cache/--data
djinn clean volumes VOLUME_NAME
djinn clean all [--force]
```

### mcpgateway

```
mcpgateway --version

mcpgateway start
mcpgateway stop
mcpgateway restart
mcpgateway status
mcpgateway logs [--follow] [--tail N]

mcpgateway enable SERVER
mcpgateway disable SERVER
mcpgateway servers
mcpgateway catalog

mcpgateway test
mcpgateway clean
```

---

## Appendix B: File Dependencies Graph

```
pyproject.toml
    └── entry points → cli/djinn.py, cli/mcpgateway.py

cli/djinn.py
    ├── commands/agent.py (run, agents)
    ├── commands/config.py (init_config, config_show, config_path)
    └── commands/container.py (build, start, auth, status, enter, audit, update, clean_app)

cli/mcpgateway.py
    └── commands/mcp.py (start, stop, restart, status, logs, enable, disable, servers, catalog, test, clean)

commands/agent.py
    ├── config/loader.py (load_config, load_agents)
    ├── core/console.py
    ├── core/docker.py (DJINN_NETWORK, ContainerOptions, compose_run, ...)
    └── core/decorators.py (@handle_config_errors)

commands/container.py
    ├── config/defaults.py (VOLUME_CATEGORIES)
    ├── config/loader.py (load_config)
    ├── core/console.py
    ├── core/docker.py (full API)
    ├── core/exceptions.py (ConfigNotFoundError)
    ├── core/paths.py (get_project_root, resolve_mount_path)
    └── core/decorators.py

commands/config.py
    ├── config/loader.py (load_config, save_config)
    ├── config/models.py (AppConfig)
    ├── core/console.py
    ├── core/decorators.py
    └── core/paths.py (CONFIG_DIR, CONFIG_FILE, AGENTS_FILE, get_project_root)

commands/mcp.py
    ├── core/console.py
    ├── core/docker.py (DJINN_NETWORK, ensure_network, delete_network, is_container_running)
    └── core/paths.py (get_project_root)

config/loader.py
    ├── config/models.py (AgentConfig, AppConfig)
    ├── config/defaults.py (DEFAULT_AGENTS)
    ├── core/console.py (warning)
    ├── core/exceptions.py (ConfigNotFoundError, ConfigValidationError)
    └── core/paths.py (AGENTS_FILE, CONFIG_FILE, get_project_root)

core/docker.py
    ├── core/console.py (warning)
    └── core/paths.py (get_project_root)

core/console.py
    └── core/theme.py (TODAI_THEME, ICONS)

core/decorators.py
    ├── core/console.py (error)
    └── core/exceptions.py (ConfigNotFoundError, ConfigValidationError)

core/theme.py, core/paths.py, core/exceptions.py, config/models.py
    └── (no internal dependencies)
```
