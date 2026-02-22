# Djinn in a Box CLI - Complete Implementation Guide

> **Version**: 0.1.0
> **Architecture**: Python Typer CLI with Pydantic Configuration
> **License**: MIT

This document provides a comprehensive technical reference for the Djinn in a Box CLI application. After reading this guide, you will understand every component, their interactions, data flows, and implementation patterns used throughout the codebase.

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

Djinn in a Box CLI is a Python application that manages Docker-based development environments for AI coding agents. It replaces the original Bash script implementation (`dev.sh`, `agent_runner.py`, `mcp.sh`) with a type-safe, maintainable Python codebase.

### 1.2 Key Capabilities

| Capability | Description |
|------------|-------------|
| **Container Lifecycle** | Build, start, stop, enter development containers |
| **Agent Execution** | Run AI agents (Claude, Gemini, Codex, OpenCode) in headless mode |
| **MCP Gateway** | Manage Model Context Protocol servers for AI tool access |
| **Configuration** | TOML-based configuration with Pydantic validation |
| **Volume Management** | Clean up Docker volumes by category |

### 1.3 Two CLI Entry Points

```
djinn    # Main CLI for container and agent management
mcpgateway   # Dedicated CLI for MCP Gateway operations
```

---

## 2. Architecture Overview

### 2.1 Layer Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLI Layer                               │
│   cli/djinn.py              cli/mcpgateway.py               │
│   (Typer apps, command          (Typer app, command             │
│    registration, callbacks)      registration)                  │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Commands Layer                            │
│   commands/agent.py      commands/container.py   commands/mcp.py│
│   (Agent execution)      (Container lifecycle)   (MCP gateway)  │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Configuration Layer                          │
│   config/models.py       config/loader.py     config/defaults.py│
│   (Pydantic models)      (Load/save TOML)     (Default values)  │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                            Core Layer                           │
│core/docker.py core/console.py  core/paths.py  core/decorators.py│
│ (Docker ops)   (Rich output)    (Path utils)   (Error handling) │
└─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                        External Systems                         │
│              Docker Engine          File System                 │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Dependency Flow

```
cli/djinn.py ──────┬──► commands/agent.py ──────┬──► config/loader.py
                       │                            │
                       ├──► commands/container.py ──┤──► config/models.py
                       │                            │
cli/mcpgateway.py ─────┴──► commands/mcp.py ────────┴──► core/docker.py
                                                    │
                                                    └──► core/console.py
                                                    │
                                                    └──► core/paths.py
```

### 2.3 Design Principles

1. **Strict Layer Separation**: Each layer only depends on layers below it
2. **No Shell=True**: All subprocess calls use explicit argument lists
3. **Type Safety**: Pyright strict mode, Pydantic validation
4. **Fail-Fast**: ConfigNotFoundError raised early, explicit error handling
5. **Decorator Patterns**: Reusable error handling via decorators

---

## 3. Package Structure

```
djinn-in-a-box/
├── pyproject.toml                    # Package configuration, entry points
├── src/
│   └── djinn_in_a_box/
│       ├── __init__.py               # Package version
│       ├── py.typed                  # PEP 561 marker
│       │
│       ├── cli/                      # CLI entry points
│       │   ├── __init__.py
│       │   ├── djinn.py          # Main CLI application
│       │   └── mcpgateway.py         # MCP Gateway CLI
│       │
│       ├── commands/                 # Command implementations
│       │   ├── __init__.py
│       │   ├── agent.py              # Agent execution commands
│       │   ├── container.py          # Container lifecycle commands
│       │   └── mcp.py                # MCP Gateway commands
│       │
│       ├── config/                   # Configuration system
│       │   ├── __init__.py           # Re-exports
│       │   ├── models.py             # Pydantic models
│       │   ├── loader.py             # TOML loading/saving
│       │   └── defaults.py           # Default values
│       │
│       └── core/                     # Core infrastructure
│           ├── __init__.py
│           ├── docker.py             # Docker operations
│           ├── theme.py              # TodAI Design Theme (colors, styles, icons)
│           ├── console.py            # Rich console output (uses theme.py)
│           ├── paths.py              # Path utilities
│           └── decorators.py         # Error handling decorators
│
└── tests/                            # Test suite (328 tests)
    ├── conftest.py
    ├── test_cli_*.py
    ├── test_commands_*.py
    ├── test_config_*.py
    └── test_core_*.py
```

---

## 4. Entry Points & CLI Layer

### 4.1 pyproject.toml Entry Points

```toml
[project.scripts]
djinn = "djinn_in_a_box.cli.djinn:app"
mcpgateway = "djinn_in_a_box.cli.mcpgateway:app"
```

When installed via `pip install .` or `uv pip install .`, these create executable commands:
- `djinn` → Invokes `djinn_in_a_box.cli.djinn:app`
- `mcpgateway` → Invokes `djinn_in_a_box.cli.mcpgateway:app`

### 4.2 cli/djinn.py - Main CLI

**File**: `src/djinn_in_a_box/cli/djinn.py` (~300 lines)

#### 4.2.1 Application Structure

```python
import typer

app = typer.Typer(
    name="djinn",
    help="Djinn in a Box CLI - Manage AI coding agent containers",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
```

#### 4.2.2 Command Registration

Commands are registered from the commands layer:

```python
from djinn_in_a_box.commands import agent, container

# Direct command registration
app.command(name="run")(agent.run)
app.command(name="agents")(agent.agents)
app.command(name="build")(container.build)
app.command(name="start")(container.start)
app.command(name="auth")(container.auth)
app.command(name="status")(container.status)
app.command(name="enter")(container.enter)
app.command(name="audit")(container.audit)
app.command(name="update")(container.update)

# Subcommand group for clean operations
app.add_typer(container.clean_app, name="clean")
```

#### 4.2.3 Config Subcommand Group

```python
config_app = typer.Typer(help="Configuration management commands")
app.add_typer(config_app, name="config")

@config_app.command(name="show")
def config_show() -> None:
    """Display current configuration."""
    # Loads and displays config.toml

@config_app.command(name="path")
def config_path() -> None:
    """Show configuration file path."""
    console.print(str(CONFIG_FILE))
```

#### 4.2.4 Init Command with Interactive Setup

```python
@app.command()
def init(
    code_dir: Annotated[Path | None, typer.Option(...)] = None,
    force: Annotated[bool, typer.Option(...)] = False,
) -> None:
    """Initialize Djinn in a Box configuration."""
```

The `init` command:
1. Checks if config already exists (unless `--force`)
2. Prompts for `code_dir` if not provided
3. Attempts migration from legacy `.env` file
4. Creates `config.toml` and `agents.toml` with defaults
5. Displays success message with next steps

#### 4.2.5 Version Callback

```python
def version_callback(value: bool) -> None:
    if value:
        console.print(f"Djinn in a Box CLI v{__version__}")
        raise typer.Exit()

@app.callback()
def main(
    version: Annotated[bool | None, typer.Option("--version", "-v", ...)] = None,
) -> None:
    """Djinn in a Box CLI - Manage AI coding agent containers."""
    if version:
        version_callback(version)
```

### 4.3 cli/mcpgateway.py - MCP Gateway CLI

**File**: `src/djinn_in_a_box/cli/mcpgateway.py` (~50 lines)

A thin wrapper that registers commands from `commands/mcp.py`:

```python
import typer
from djinn_in_a_box.commands import mcp

app = typer.Typer(
    name="mcpgateway",
    help="MCP Gateway CLI - Manage Model Context Protocol servers",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Gateway lifecycle
app.command(name="start")(mcp.start)
app.command(name="stop")(mcp.stop)
app.command(name="restart")(mcp.restart)
app.command(name="status")(mcp.status)
app.command(name="logs")(mcp.logs)

# Server management
app.command(name="enable")(mcp.enable)
app.command(name="disable")(mcp.disable)
app.command(name="servers")(mcp.servers)
app.command(name="catalog")(mcp.catalog)

# Diagnostics
app.command(name="test")(mcp.test)
app.command(name="clean")(mcp.clean)
```

---

## 5. Commands Layer

### 5.1 commands/agent.py - Agent Execution

**File**: `src/djinn_in_a_box/commands/agent.py` (~337 lines)

#### 5.1.1 build_agent_command()

Constructs the shell command for agent execution:

```python
def build_agent_command(
    agent_config: AgentConfig,
    *,
    write: bool = False,
    json_output: bool = False,
    model: str | None = None,
) -> str:
    """Build shell command string for agent execution."""
    parts: list[str] = [agent_config.binary]
    parts.extend(agent_config.headless_flags)

    if model:
        parts.extend([agent_config.model_flag, model])

    if write:
        parts.extend(agent_config.write_flags)
    else:
        parts.extend(agent_config.read_only_flags)

    if json_output:
        parts.extend(agent_config.json_flags)

    # Append prompt template (uses $AGENT_PROMPT env var)
    parts.append(agent_config.prompt_template)

    return " ".join(parts)
```

**Example Output**:
```
claude -p --model sonnet --dangerously-skip-permissions "$AGENT_PROMPT"
```

#### 5.1.2 run() Command

```python
@handle_config_errors
def run(
    agent: Annotated[str, typer.Argument(...)],
    prompt: Annotated[str, typer.Argument(...)],
    write: Annotated[bool, typer.Option("--write", "-w", ...)] = False,
    json_output: Annotated[bool, typer.Option("--json", "-j", ...)] = False,
    model: Annotated[str | None, typer.Option("--model", "-m", ...)] = None,
    docker: Annotated[bool, typer.Option("--docker", "-d", ...)] = False,
    firewall: Annotated[bool, typer.Option("--firewall", "-f", ...)] = False,
    mount: Annotated[Path | None, typer.Option("--mount", ...)] = None,
    timeout: Annotated[int | None, typer.Option("--timeout", "-t", ...)] = None,
) -> None:
```

**Process Flow**:
1. Load configuration via `load_config()` and `load_agents()`
2. Validate agent name exists in config
3. Ensure Docker network exists
4. Determine workspace path (default: cwd)
5. Print status info to stderr
6. Build agent command string
7. Configure `ContainerOptions`
8. Execute via `compose_run()`
9. Output stdout/stderr
10. Cleanup docker proxy
11. Exit with agent's return code

#### 5.1.3 agents() Command

Lists available agents with optional verbosity:

```python
def agents(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", ...)] = False,
    json_output: Annotated[bool, typer.Option("--json", "-j", ...)] = False,
) -> None:
```

### 5.2 commands/container.py - Container Lifecycle

**File**: `src/djinn_in_a_box/commands/container.py` (~700 lines)

#### 5.2.1 Commands Overview

| Command | Description | Decorator |
|---------|-------------|-----------|
| `build` | Build the development container image | `@handle_config_errors` |
| `start` | Start interactive container session | `@handle_config_errors` |
| `auth` | Authenticate with AI provider services | `@handle_config_errors` |
| `status` | Show container and network status | - |
| `enter` | Enter running container with new shell | - |
| `audit` | Run Trivy security scan | - |
| `update` | Pull latest base images | - |

#### 5.2.2 build() Command

```python
@handle_config_errors
def build(
    no_cache: Annotated[bool, typer.Option(...)] = False,
    pull: Annotated[bool, typer.Option(...)] = True,
    progress: Annotated[str, typer.Option(...)] = "auto",
) -> None:
```

Uses `compose_build()` from core/docker.py with build arguments.

#### 5.2.3 start() Command

```python
@handle_config_errors
def start(
    mount: Annotated[Path | None, typer.Option(...)] = None,
    docker: Annotated[bool, typer.Option("--docker", "-d", ...)] = False,
    firewall: Annotated[bool, typer.Option("--firewall", "-f", ...)] = False,
    cmd: Annotated[str | None, typer.Option("--cmd", "-c", ...)] = None,
) -> None:
```

**Process Flow**:
1. Load config, validate mount path
2. Ensure Docker network exists (with error handling)
3. Print status information
4. Configure `ContainerOptions`
5. Execute via `compose_run()` in interactive mode
6. Exit with container's return code

#### 5.2.4 clean Subcommand Group

```python
clean_app = typer.Typer(
    name="clean",
    help="Clean up Docker resources",
    no_args_is_help=True,
)

@clean_app.command(name="volumes")
def clean_volumes(
    category: Annotated[str | None, typer.Option(...)] = None,
    all_volumes: Annotated[bool, typer.Option("--all", "-a", ...)] = False,
    force: Annotated[bool, typer.Option("--force", "-f", ...)] = False,
) -> None:
```

Volume categories from `defaults.py`:
- `credentials`: `djinn-*-credentials`
- `tools`: `djinn-*-tools`, `djinn-*-tools-cache`
- `cache`: `djinn-*-cache`, `djinn-*-uv-cache`
- `data`: `djinn-*-data`, `djinn-*-share`

### 5.3 commands/mcp.py - MCP Gateway

**File**: `src/djinn_in_a_box/commands/mcp.py` (~595 lines)

#### 5.3.1 Constants

```python
GATEWAY_CONTAINER: str = "mcp-gateway"
GATEWAY_ENDPOINT_CONTAINER: str = "http://mcp-gateway:8811"
GATEWAY_ENDPOINT_HOST: str = "http://localhost:8811"
AI_DEV_NETWORK: str = "djinn-network"
```

#### 5.3.2 Helper Functions

```python
def check_mcp_cli() -> None:
    """Verify docker mcp CLI plugin is installed."""
    result = subprocess.run(
        ["docker", "mcp", "--help"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise MCPCliNotFoundError(msg)

def require_running() -> None:
    """Ensure MCP Gateway container is running."""
    if not is_container_running(GATEWAY_CONTAINER):
        error("MCP Gateway is not running")
        raise typer.Exit(1)
```

#### 5.3.3 @require_mcp_cli Decorator

```python
def require_mcp_cli(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator to check for MCP CLI before executing."""
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            check_mcp_cli()
        except MCPCliNotFoundError as e:
            error(str(e))
            raise typer.Exit(1) from None
        return func(*args, **kwargs)
    return wrapper
```

#### 5.3.4 Gateway Lifecycle Commands

| Command | Description |
|---------|-------------|
| `start()` | Start gateway via docker compose, wait for ready |
| `stop()` | Stop gateway via docker compose down |
| `restart()` | Restart gateway via docker compose restart |
| `status()` | Show gateway status, enabled servers, running containers |
| `logs()` | Show gateway logs with follow/tail options |

#### 5.3.5 Server Management Commands

| Command | Decorator | Description |
|---------|-----------|-------------|
| `enable(server)` | `@require_mcp_cli` | Enable an MCP server |
| `disable(server)` | `@require_mcp_cli` | Disable an MCP server |
| `servers()` | `@require_mcp_cli` | List enabled servers |
| `catalog()` | `@require_mcp_cli` | Show available server catalog |

#### 5.3.6 Diagnostic Commands

**test()**: Comprehensive connectivity test
```
Container status:           Running/Not running
Localhost endpoint:         OK/Not responding
Container endpoint:         OK/Not responding
Docker socket access:       OK/Failed
docker mcp CLI plugin:      Installed/Not installed
```

**clean()**: Full reset with confirmation
- Stops gateway
- Removes djinn-network
- Removes ~/.docker/mcp directory

---

## 6. Configuration System

### 6.1 config/models.py - Pydantic Models

**File**: `src/djinn_in_a_box/config/models.py` (~346 lines)

#### 6.1.1 AgentConfig

```python
class AgentConfig(BaseModel):
    """Configuration for a CLI coding agent."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
    )

    binary: Annotated[str, Field(min_length=1)]
    description: str = ""
    headless_flags: list[str] = Field(default_factory=list)
    read_only_flags: list[str] = Field(default_factory=list)
    write_flags: list[str] = Field(default_factory=list)
    json_flags: list[str] = Field(default_factory=list)
    model_flag: str = "--model"
    prompt_template: str = '"$AGENT_PROMPT"'
```

#### 6.1.2 ResourceLimits with Cross-Field Validation

```python
class ResourceLimits(BaseModel):
    """Docker resource limits configuration."""

    cpu_limit: Annotated[int, Field(ge=1, le=128)] = 6
    memory_limit: str = "12G"
    cpu_reservation: Annotated[int, Field(ge=1, le=128)] = 2
    memory_reservation: str = "4G"

    @field_validator("memory_limit", "memory_reservation", mode="after")
    @classmethod
    def validate_memory(cls, value: str) -> str:
        return validate_memory_format(value)

    @model_validator(mode="after")
    def validate_reservations(self) -> ResourceLimits:
        """Ensure cpu_reservation does not exceed cpu_limit."""
        if self.cpu_reservation > self.cpu_limit:
            raise ValueError(
                f"cpu_reservation ({self.cpu_reservation}) cannot exceed "
                f"cpu_limit ({self.cpu_limit})"
            )
        return self
```

**Memory Format Validation**:
```python
def validate_memory_format(value: str) -> str:
    """Validate Docker memory format string (e.g., '12G', '4096M')."""
    pattern = r"^\d+[GMKgmk]$"
    if not re.match(pattern, value):
        raise ValueError(f"Invalid memory format: '{value}'")
    return value[:-1] + value[-1].upper()  # Normalize to uppercase
```

#### 6.1.3 ShellConfig

```python
class ShellConfig(BaseModel):
    """Shell mounting configuration."""

    skip_mounts: bool = False
    omp_theme_path: Path | None = None

    @field_validator("omp_theme_path", mode="before")
    @classmethod
    def expand_omp_theme_path(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        path = Path(value).expanduser() if isinstance(value, str) else value.expanduser()
        return path
```

#### 6.1.4 AppConfig (Root Configuration)

```python
class AppConfig(BaseModel):
    """Main application configuration."""

    code_dir: Path
    timezone: str = "Europe/Berlin"
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    shell: ShellConfig = Field(default_factory=ShellConfig)

    @field_validator("code_dir", mode="before")
    @classmethod
    def expand_code_dir(cls, value: str | Path) -> Path:
        return Path(value).expanduser() if isinstance(value, str) else value.expanduser()

    @field_validator("code_dir", mode="after")
    @classmethod
    def validate_code_dir(cls, value: Path) -> Path:
        if not value.exists():
            raise ValueError(f"code_dir does not exist: {value}")
        if not value.is_dir():
            raise ValueError(f"code_dir is not a directory: {value}")
        return value

    @field_validator("timezone", mode="after")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        # Validates IANA format like 'Europe/Berlin'
        ...
```

#### 6.1.5 AgentsConfig Container

```python
class AgentsConfig(BaseModel):
    """Container for all agent configurations."""

    agents: dict[str, AgentConfig] = Field(default_factory=dict)

    def get_agent(self, name: str) -> AgentConfig:
        if name not in self.agents:
            raise KeyError(f"Unknown agent '{name}'")
        return self.agents[name]

    def list_agents(self) -> list[str]:
        return sorted(self.agents.keys())
```

### 6.2 config/loader.py - TOML Operations

**File**: `src/djinn_in_a_box/config/loader.py` (~250 lines)

#### 6.2.1 Custom Exceptions

```python
class ConfigNotFoundError(Exception):
    """Raised when configuration file is not found."""
    pass

class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""
    pass
```

#### 6.2.2 Loading Functions

```python
def load_config() -> AppConfig:
    """Load application configuration from TOML file."""
    if not CONFIG_FILE.exists():
        raise ConfigNotFoundError(
            f"Configuration not found: {CONFIG_FILE}\n"
            "Run 'djinn init' to create it."
        )

    try:
        with CONFIG_FILE.open("rb") as f:
            data = tomllib.load(f)
        return AppConfig(**data)
    except tomllib.TOMLDecodeError as e:
        raise ConfigValidationError(f"Invalid TOML: {e}") from e
    except ValidationError as e:
        raise ConfigValidationError(f"Validation failed: {e}") from e


def load_agents() -> dict[str, AgentConfig]:
    """Load agent configurations from TOML file."""
    if not AGENTS_FILE.exists():
        # Return defaults if no custom agents file
        return DEFAULT_AGENTS.copy()

    with AGENTS_FILE.open("rb") as f:
        data = tomllib.load(f)

    agents_config = AgentsConfig(agents={
        name: AgentConfig(**cfg)
        for name, cfg in data.get("agents", {}).items()
    })
    return agents_config.agents
```

#### 6.2.3 Saving Functions

```python
def save_config(config: AppConfig) -> None:
    """Save application configuration to TOML file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    data = {
        "code_dir": str(config.code_dir),
        "timezone": config.timezone,
        "resources": {
            "cpu_limit": config.resources.cpu_limit,
            "memory_limit": config.resources.memory_limit,
            "cpu_reservation": config.resources.cpu_reservation,
            "memory_reservation": config.resources.memory_reservation,
        },
        "shell": {
            "skip_mounts": config.shell.skip_mounts,
        },
    }

    if config.shell.omp_theme_path:
        data["shell"]["omp_theme_path"] = str(config.shell.omp_theme_path)

    with CONFIG_FILE.open("wb") as f:
        tomli_w.dump(data, f)


def save_agents(agents: dict[str, AgentConfig]) -> None:
    """Save agent configurations to TOML file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    data = {
        "agents": {
            name: {
                "binary": cfg.binary,
                "description": cfg.description,
                "headless_flags": cfg.headless_flags,
                "read_only_flags": cfg.read_only_flags,
                "write_flags": cfg.write_flags,
                "json_flags": cfg.json_flags,
                "model_flag": cfg.model_flag,
                "prompt_template": cfg.prompt_template,
            }
            for name, cfg in agents.items()
        }
    }

    with AGENTS_FILE.open("wb") as f:
        tomli_w.dump(data, f)
```

#### 6.2.4 Legacy Migration

```python
def migrate_from_env() -> AppConfig | None:
    """Attempt to migrate configuration from legacy .env file."""
    env_file = get_project_root() / ".env"
    if not env_file.exists():
        return None

    # Parse .env file for CODE_DIR
    with env_file.open() as f:
        for line in f:
            line = line.strip()
            if line.startswith("CODE_DIR="):
                code_dir = line.split("=", 1)[1].strip('"\'')
                return AppConfig(code_dir=Path(code_dir).expanduser())

    return None
```

### 6.3 config/defaults.py - Default Values

**File**: `src/djinn_in_a_box/config/defaults.py` (~150 lines)

#### 6.3.1 Volume Categories

```python
VOLUME_CATEGORIES: dict[str, list[str]] = {
    "credentials": [
        "djinn-claude-credentials",
        "djinn-gemini-credentials",
        "djinn-opencode-credentials",
    ],
    "tools": [
        "djinn-claude-tools",
        "djinn-claude-tools-cache",
        "djinn-gemini-tools",
        "djinn-gemini-tools-cache",
        "djinn-opencode-tools",
        "djinn-opencode-tools-cache",
    ],
    "cache": [
        "djinn-npm-cache",
        "djinn-uv-cache",
        "djinn-pip-cache",
    ],
    "data": [
        "djinn-share",
        "djinn-data",
    ],
}
```

#### 6.3.2 Default Agent Configurations

```python
DEFAULT_AGENTS: dict[str, AgentConfig] = {
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
        headless_flags=[],
        read_only_flags=["--sandbox"],
        write_flags=[],
        json_flags=[],
        model_flag="--model",
    ),
    "codex": AgentConfig(
        binary="codex",
        description="OpenAI Codex CLI",
        headless_flags=["--quiet"],
        read_only_flags=["--approval-mode", "suggest"],
        write_flags=["--approval-mode", "full-auto"],
        json_flags=[],
        model_flag="--model",
    ),
    "opencode": AgentConfig(
        binary="opencode",
        description="OpenCode CLI",
        headless_flags=["--non-interactive"],
        read_only_flags=[],
        write_flags=[],
        json_flags=["--json"],
        model_flag="-m",
    ),
}
```

#### 6.3.3 Default Resources

```python
DEFAULT_RESOURCES = ResourceLimits(
    cpu_limit=6,
    memory_limit="12G",
    cpu_reservation=2,
    memory_reservation="4G",
)
```

### 6.4 Configuration File Locations

Following XDG Base Directory Specification:

```
~/.config/djinn-in-a-box/
├── config.toml      # Main application configuration
└── agents.toml      # Agent configurations (optional)
```

#### 6.4.1 Example config.toml

```toml
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

#### 6.4.2 Example agents.toml

```toml
[agents.claude]
binary = "claude"
description = "Anthropic Claude Code CLI"
headless_flags = ["-p"]
read_only_flags = ["--permission-mode", "plan"]
write_flags = ["--dangerously-skip-permissions"]
json_flags = ["--output-format", "json"]
model_flag = "--model"
prompt_template = '"$AGENT_PROMPT"'

[agents.gemini]
binary = "gemini"
description = "Google Gemini CLI"
# ... etc
```

---

## 7. Core Infrastructure

### 7.1 core/docker.py - Docker Operations

**File**: `src/djinn_in_a_box/core/docker.py` (~780 lines)

#### 7.1.1 Data Classes

```python
@dataclass
class ContainerOptions:
    """Options for container execution."""
    docker_enabled: bool = False
    firewall_enabled: bool = False
    mount_path: Path | None = None
    shell_mounts: bool = True


@dataclass
class RunResult:
    """Result of a container run operation."""
    returncode: int
    stdout: str
    stderr: str
```

#### 7.1.2 Network Operations

```python
AI_DEV_NETWORK = "djinn-network"

def network_exists(name: str = AI_DEV_NETWORK) -> bool:
    """Check if Docker network exists."""
    result = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def ensure_network(name: str = AI_DEV_NETWORK) -> bool:
    """Create Docker network if it doesn't exist."""
    if network_exists(name):
        return True

    result = subprocess.run(
        ["docker", "network", "create", name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0
```

#### 7.1.3 Container Status

```python
def is_container_running(name: str) -> bool:
    """Check if a container is running."""
    result = subprocess.run(
        [
            "docker", "ps",
            "--filter", f"name={name}",
            "--filter", "status=running",
            "--format", "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return name in result.stdout.split()
```

#### 7.1.4 Compose Operations

```python
def compose_build(
    config: AppConfig,
    *,
    no_cache: bool = False,
    pull: bool = True,
    progress: str = "auto",
) -> int:
    """Build container image via docker compose."""
    cmd = ["docker", "compose", "build"]

    if no_cache:
        cmd.append("--no-cache")
    if pull:
        cmd.append("--pull")
    cmd.extend(["--progress", progress])

    env = _build_compose_env(config)
    result = subprocess.run(cmd, env=env, cwd=get_project_root(), check=False)
    return result.returncode


def compose_run(
    config: AppConfig,
    options: ContainerOptions,
    *,
    command: str | None = None,
    interactive: bool = True,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> RunResult:
    """Run container via docker compose."""
    cmd = ["docker", "compose", "run", "--rm"]

    if not interactive:
        cmd.append("-T")  # Disable pseudo-TTY

    # Add environment variables
    run_env = _build_compose_env(config, options)
    if env:
        for key, value in env.items():
            cmd.extend(["-e", f"{key}={value}"])

    cmd.append("dev")  # Service name

    if command:
        cmd.extend(["zsh", "-c", command])

    if interactive:
        result = subprocess.run(
            cmd, env=run_env, cwd=get_project_root(), check=False
        )
        return RunResult(result.returncode, "", "")
    else:
        result = subprocess.run(
            cmd,
            env=run_env,
            cwd=get_project_root(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return RunResult(result.returncode, result.stdout, result.stderr)
```

#### 7.1.5 Volume Operations

```python
def list_volumes(pattern: str = "djinn-*") -> list[str]:
    """List Docker volumes matching pattern."""
    result = subprocess.run(
        ["docker", "volume", "ls", "--filter", f"name={pattern}", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [v.strip() for v in result.stdout.strip().split("\n") if v.strip()]


def delete_volume(name: str) -> bool:
    """Delete a Docker volume."""
    result = subprocess.run(
        ["docker", "volume", "rm", name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def volume_exists(name: str) -> bool:
    """Check if a Docker volume exists."""
    result = subprocess.run(
        ["docker", "volume", "inspect", name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0
```

#### 7.1.6 Docker Proxy Cleanup

```python
def cleanup_docker_proxy(was_enabled: bool) -> bool:
    """Clean up docker-proxy container if it was started."""
    if not was_enabled:
        return True

    if not is_container_running("docker-proxy"):
        return True

    result = subprocess.run(
        ["docker", "stop", "docker-proxy"],
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        warning("Failed to stop docker-proxy container")
        return False

    return True
```

### 7.2 core/console.py - Rich Output

**File**: `src/djinn_in_a_box/core/console.py` (~250 lines)

Provides consistent, styled terminal output using the TodAI Design System.
Status messages go to stderr to keep stdout clean for agent output.

#### 7.2.1 Console Instances with Theme

```python
from rich.console import Console
from djinn_in_a_box.core.theme import ICONS, TODAI_THEME

console: Console = Console(theme=TODAI_THEME)        # stdout
err_console: Console = Console(stderr=True, theme=TODAI_THEME)  # stderr
```

#### 7.2.2 Output Functions (with TodAI Theme)

```python
def error(message: str) -> None:
    """Print error message with error styling and icon."""
    err_console.print(f"[error]{ICONS['error']} Error: {message}[/error]")


def success(message: str) -> None:
    """Print success message with success styling and icon."""
    err_console.print(f"[success]{ICONS['success']} {message}[/success]")


def info(message: str) -> None:
    """Print info message with info styling and icon."""
    err_console.print(f"[info]{ICONS['info']} {message}[/info]")


def warning(message: str) -> None:
    """Print warning message with warning styling and icon."""
    err_console.print(f"[warning]{ICONS['warning']} Warning: {message}[/warning]")


def status_line(label: str, value: str, style: str = "status.enabled") -> None:
    """Print a labeled status line with semantic styling."""
    padding = max(0, 10 - len(label))
    err_console.print(f"   [{style}]{label}:[/{style}]{' ' * padding} {value}")


def header(title: str) -> None:
    """Print a section header with header styling."""
    err_console.print(f"[header]{title}:[/header]")


def blank() -> None:
    """Print a blank line."""
    err_console.print()
```

#### 7.2.3 Volume Table with Theme Styling

```python
from rich.table import Table

def create_volume_table(volumes: dict[str, list[str]]) -> Table:
    """Create a Rich table for volume listing with theme styling."""
    table = Table(
        title="Djinn Volumes",
        title_style="table.title",
        show_header=True,
        header_style="table.header",
    )
    table.add_column("Category", style="table.category", width=15)
    table.add_column("Volume", style="table.value")
    # ... populate table
    return table
```

#### 7.2.4 Style Reference

| Style Name | Color | Usage |
|------------|-------|-------|
| `[error]` | #9c0136 (red) | Error messages |
| `[success]` | #03b971 (green) | Success messages |
| `[warning]` | #f5b332 (orange) | Warnings |
| `[info]` | #0e8ac8 (blue) | Information |
| `[header]` | #0e8ac8 bold | Section headers |
| `[status.enabled]` | #03b971 | Enabled status |
| `[status.disabled]` | #f5b332 | Disabled status |
| `[muted]` | #b2bec3 | Secondary text |
| `[primary]` | #69B9A1 | Labels, emphasis |

### 7.3 core/paths.py - Path Utilities

**File**: `src/djinn_in_a_box/core/paths.py` (~100 lines)

#### 7.3.1 Configuration Paths

```python
from pathlib import Path

CONFIG_DIR: Path = Path.home() / ".config" / "djinn-in-a-box"
CONFIG_FILE: Path = CONFIG_DIR / "config.toml"
AGENTS_FILE: Path = CONFIG_DIR / "agents.toml"
```

#### 7.3.2 Project Root Detection

```python
def get_project_root() -> Path:
    """Get the Djinn in a Box project root directory.

    Searches upward from the package location for docker-compose.yml.
    """
    # Start from this file's directory
    current = Path(__file__).parent

    # Search upward for docker-compose.yml
    while current != current.parent:
        if (current / "docker-compose.yml").exists():
            return current
        current = current.parent

    # Fallback to package parent directory
    return Path(__file__).parent.parent.parent.parent
```

#### 7.3.3 Mount Path Validation

```python
def resolve_mount_path(path: Path | None, code_dir: Path) -> Path:
    """Resolve and validate mount path.

    Args:
        path: Optional explicit mount path
        code_dir: Configured code_dir from config

    Returns:
        Resolved absolute path

    Raises:
        ValueError: If path doesn't exist or isn't a directory
    """
    if path is None:
        return Path.cwd()

    resolved = path.resolve()

    if not resolved.exists():
        raise ValueError(f"Mount path does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"Mount path is not a directory: {resolved}")

    return resolved
```

### 7.4 core/decorators.py - Error Handling

**File**: `src/djinn_in_a_box/core/decorators.py` (~50 lines)

```python
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import typer

P = ParamSpec("P")
R = TypeVar("R")


def handle_config_errors(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator to handle ConfigNotFoundError gracefully.

    Wraps command functions to catch ConfigNotFoundError and display
    a user-friendly error message before exiting with code 1.

    Example:
        >>> @handle_config_errors
        ... def my_command():
        ...     config = load_config()  # May raise ConfigNotFoundError
        ...     # ... rest of command
    """
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        from djinn_in_a_box.config import ConfigNotFoundError
        from djinn_in_a_box.core.console import error

        try:
            return func(*args, **kwargs)
        except ConfigNotFoundError as e:
            error(str(e))
            raise typer.Exit(1) from None

    return wrapper
```

**Type Parameters**:
- `P = ParamSpec("P")`: Captures the parameter specification of the decorated function
- `R = TypeVar("R")`: Captures the return type of the decorated function

This preserves full type safety through the decorator, allowing Pyright to verify:
- Argument types passed to decorated functions
- Return types of decorated functions

### 7.5 core/theme.py - TodAI Design Theme

**File**: `src/djinn_in_a_box/core/theme.py` (~187 lines)

Defines the central theme system for consistent CLI output based on the TodAI Design System.

#### 7.5.1 Color Token Constants

```python
# Primary Palette
PRIMARY: str = "#69B9A1"        # Teal - Active elements, focus, labels
PRIMARY_DARK_1: str = "#226666" # Deep Teal - Secondary actions
PRIMARY_DARK_2: str = "#29526d" # Ocean - Borders, dividers
PRIMARY_DARK_3: str = "#333676" # Indigo - Inactive elements

# Surface Palette
SURFACE: str = "#212121"        # Background color
SURFACE_LIGHT_1: str = "#2a2a2a" # Elevated surface
SURFACE_LIGHT_2: str = "#333333" # Hover state

# Text Palette
TEXT: str = "#ffffff"           # Primary text
TEXT_MUTED: str = "#b2bec3"     # Secondary text, hints

# Status Colors
SUCCESS: str = "#03b971"        # Green - Success, enabled
SUCCESS_LIGHT: str = "#c1ff62"  # Light green - Highlights
INFO: str = "#0e8ac8"           # Blue - Information, headers
INFO_LIGHT: str = "#0ec1c8"     # Cyan - Secondary info
WARNING: str = "#f5b332"        # Orange - Warnings, disabled
WARNING_LIGHT: str = "#faf870"  # Yellow - Caution
ERROR: str = "#9c0136"          # Red - Errors, critical
SPECIAL: str = "#8608b8"        # Purple - Special elements
```

#### 7.5.2 TODAI_THEME Definition

```python
from rich.style import Style
from rich.theme import Theme

TODAI_THEME: Theme = Theme({
    # Semantic Message Styles
    "success": Style(color=SUCCESS),
    "success.bold": Style(color=SUCCESS, bold=True),
    "error": Style(color=ERROR, bold=True),
    "warning": Style(color=WARNING),
    "info": Style(color=INFO),
    "info.bold": Style(color=INFO, bold=True),

    # Primary/Accent Styles
    "primary": Style(color=PRIMARY),
    "primary.bold": Style(color=PRIMARY, bold=True),
    "secondary": Style(color=PRIMARY_DARK_1),
    "accent": Style(color=PRIMARY_DARK_2),

    # Text Styles
    "muted": Style(color=TEXT_MUTED),
    "muted.italic": Style(color=TEXT_MUTED, italic=True),
    "highlight": Style(color=SUCCESS_LIGHT),

    # Header/Label Styles
    "header": Style(color=INFO, bold=True),
    "label": Style(color=PRIMARY),

    # Status Indicator Styles
    "status.enabled": Style(color=SUCCESS),
    "status.disabled": Style(color=WARNING),
    "status.error": Style(color=ERROR),
    "status.active": Style(color=PRIMARY, bold=True),

    # Table Styles
    "table.title": Style(color=INFO, bold=True),
    "table.header": Style(bold=True),
    "table.category": Style(color=WARNING),
    "table.value": Style(color=TEXT_MUTED),

    # Special
    "special": Style(color=SPECIAL),
})
```

#### 7.5.3 Icon Constants

```python
ICONS: dict[str, str] = {
    "success": "✓",   # U+2713
    "error": "✗",     # U+2717
    "warning": "⚠",   # U+26A0
    "info": "ℹ",      # U+2139
    "active": "●",    # U+25CF
    "inactive": "○",  # U+25CB
    "arrow": "→",     # U+2192
}
```

#### 7.5.4 Usage

```python
from djinn_in_a_box.core.theme import TODAI_THEME, ICONS
from rich.console import Console

console = Console(theme=TODAI_THEME)
console.print(f"[success]{ICONS['success']} Build complete[/success]")
console.print("[primary.bold]Next steps:[/primary.bold]")
console.print("  [muted]1.[/muted] Run tests")
```

---

## 8. Process Flows

### 8.1 First-Time Setup (djinn init)

```
User runs: djinn init
           │
           ▼
┌──────────────────────────────┐
│ Check if config.toml exists  │
└──────────────────────────────┘
           │
    ┌──────┴──────┐
    │             │
    ▼             ▼
 Exists        Missing
    │             │
    ▼             ▼
Error if       Proceed
no --force        │
    │             ▼
    │    ┌────────────────────┐
    │    │ Prompt for code_dir│
    │    │ (if not provided)  │
    │    └────────────────────┘
    │             │
    │             ▼
    │    ┌────────────────────┐
    │    │ Try migrate from   │
    │    │ legacy .env file   │
    │    └────────────────────┘
    │             │
    │             ▼
    │    ┌────────────────────┐
    │    │ Create AppConfig   │
    │    │ with validation    │
    │    └────────────────────┘
    │             │
    │             ▼
    │    ┌────────────────────┐
    │    │ save_config()      │
    │    │ save_agents()      │
    │    └────────────────────┘
    │             │
    └──────┬──────┘
           │
           ▼
┌──────────────────────────────┐
│ Display success + next steps │
└──────────────────────────────┘
```

### 8.2 Running an Agent (djinn run)

```
User runs: djinn run claude "Fix the bug" --write
           │
           ▼
┌──────────────────────────────┐
│ @handle_config_errors        │
│ catches ConfigNotFoundError  │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ load_config()                │
│ load_agents()                │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ Validate agent name          │
│ (claude ∈ agent_configs?)    │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ ensure_network()             │
│ Create djinn-network        │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ Determine workspace path     │
│ (--mount or cwd)             │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ Print status to stderr       │
│ (Agent, Workspace, Mode...)  │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ build_agent_command()        │
│ → "claude -p --dangerously-  │
│    skip-permissions          │
│    \"$AGENT_PROMPT\""        │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ ContainerOptions(            │
│   docker_enabled=False,      │
│   mount_path=workspace,      │
│   shell_mounts=True,         │
│ )                            │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ compose_run(                 │
│   config, options,           │
│   command=agent_cmd,         │
│   interactive=False,         │
│   env={"AGENT_PROMPT": ...}, │
│ )                            │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ Docker container executes:   │
│   zsh -c 'claude -p ... \    │
│      "$AGENT_PROMPT"'        │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ Output stdout to console     │
│ Output stderr to err_console │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ cleanup_docker_proxy()       │
│ Exit with agent returncode   │
└──────────────────────────────┘
```

### 8.3 MCP Gateway Start (mcpgateway start)

```
User runs: mcpgateway start
           │
           ▼
┌──────────────────────────────┐
│ check_mcp_cli()              │
│ Verify docker mcp plugin     │
└──────────────────────────────┘
           │
    ┌──────┴──────┐
    │             │
    ▼             ▼
 Installed    Not Installed
    │             │
    │             ▼
    │         MCPCliNotFoundError
    │         → Exit(1)
    │
    ▼
┌──────────────────────────────┐
│ensure_network(AI_DEV_NETWORK)│
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ docker compose up -d         │
│ (cwd: mcp/ directory)        │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ time.sleep(3)                │
│ Wait for container ready     │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ is_container_running(        │
│   "mcp-gateway")?            │
└──────────────────────────────┘
           │
    ┌──────┴──────┐
    │             │
    ▼             ▼
 Running      Not Running
    │             │
    ▼             ▼
 success()    docker compose logs
 Display      → Exit(1)
 endpoints
```

### 8.4 Container Start (djinn start)

```
User runs: djinn start --docker --mount /path/to/project
           │
           ▼
┌──────────────────────────────┐
│ @handle_config_errors        │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ load_config()                │
│ → AppConfig with validated   │
│   code_dir, resources, etc.  │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ resolve_mount_path()         │
│ Validate --mount exists      │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ ensure_network()             │
│ → Returns bool               │
└──────────────────────────────┘
           │
    ┌──────┴──────┐
    │             │
    ▼             ▼
  True         False
    │             │
    │             ▼
    │         error("Failed to create network")
    │         → Exit(1)
    │
    ▼
┌──────────────────────────────┐
│ Print status info            │
│ (Mode, Workspace, Docker...) │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ ContainerOptions(            │
│   docker_enabled=True,       │
│   mount_path=resolved_path,  │
│ )                            │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ compose_run(                 │
│   config, options,           │
│   interactive=True,          │
│ )                            │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ Interactive zsh session      │
│ User interacts with container│
│ User types 'exit'            │
└──────────────────────────────┘
           │
           ▼
┌──────────────────────────────┐
│ Exit with container code     │
└──────────────────────────────┘
```

---

## 9. Error Handling Strategy

### 9.1 Exception Hierarchy

```
Exception
├── ConfigNotFoundError      # config/loader.py - Config file missing
├── ConfigValidationError    # config/loader.py - Invalid config values
├── MCPCliNotFoundError      # commands/mcp.py - docker mcp not installed
└── ValidationError          # Pydantic - Model validation failed
```

### 9.2 Error Handling Patterns

#### 9.2.1 Decorator Pattern (@handle_config_errors)

```python
@handle_config_errors
def run(...) -> None:
    config = load_config()  # May raise ConfigNotFoundError
    # If raised, decorator catches it, prints error, exits with code 1
```

**Used in**: `run()`, `start()`, `auth()`, `build()`, and other config-dependent commands

#### 9.2.2 Decorator Pattern (@require_mcp_cli)

```python
@require_mcp_cli
def enable(server: str) -> None:
    # docker mcp command here
    # If MCP CLI not installed, decorator catches, prints instructions, exits
```

**Used in**: `enable()`, `disable()`, `servers()`, `catalog()`

#### 9.2.3 Explicit Check Pattern

```python
def start(...) -> None:
    # ...
    if not network_exists() and not ensure_network():
        error("Failed to create Docker network 'djinn-network'")
        raise typer.Exit(1)
```

#### 9.2.4 Return Code Propagation

```python
def stop() -> None:
    result = subprocess.run(["docker", "compose", "down"], ...)
    if result.returncode != 0:
        error("Failed to stop MCP Gateway")
        raise typer.Exit(result.returncode)
```

### 9.3 Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error (config not found, validation failed, etc.) |
| >1 | Subprocess exit code (propagated from docker, docker compose, etc.) |

### 9.4 Error Messages

Errors are printed via `error()` from `core/console.py`:

```python
error("Configuration not found: ~/.config/djinn-in-a-box/config.toml")
# Output: Error: Configuration not found: ~/.config/djinn-in-a-box/config.toml
```

The `error()` function writes to stderr via `err_console`, keeping stdout clean for agent output.

---

## 10. Type System & Validation

### 10.1 Pydantic v2 Configuration

All models use consistent `ConfigDict`:

```python
model_config = ConfigDict(
    extra="forbid",          # Reject unknown fields
    frozen=False,            # Allow mutation
    validate_assignment=True,  # Validate on attribute assignment
)
```

### 10.2 Field-Level Validation

**Constrained Types**:
```python
cpu_limit: Annotated[int, Field(ge=1, le=128)] = 6
binary: Annotated[str, Field(min_length=1)]
```

**Custom Validators**:
```python
@field_validator("memory_limit", "memory_reservation", mode="after")
@classmethod
def validate_memory(cls, value: str) -> str:
    return validate_memory_format(value)  # e.g., "12G" → "12G"
```

### 10.3 Cross-Field Validation

```python
@model_validator(mode="after")
def validate_reservations(self) -> ResourceLimits:
    if self.cpu_reservation > self.cpu_limit:
        raise ValueError(
            f"cpu_reservation ({self.cpu_reservation}) cannot exceed "
            f"cpu_limit ({self.cpu_limit})"
        )
    return self
```

### 10.4 Type-Safe Decorators

Using `ParamSpec` and `TypeVar` for full type preservation:

```python
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

def handle_config_errors(func: Callable[P, R]) -> Callable[P, R]:
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # ...
    return wrapper
```

This allows Pyright to verify that decorated functions maintain their original signatures.

### 10.5 Typer Annotations

```python
def run(
    agent: Annotated[str, typer.Argument(help="Agent to run")],
    write: Annotated[bool, typer.Option("--write", "-w", help="Allow modifications")] = False,
) -> None:
```

`Annotated` combines the type hint with Typer metadata, preserving type safety while adding CLI documentation.

---

## 11. Testing Architecture

### 11.1 Test Organization

```
tests/
├── conftest.py              # Shared fixtures
├── test_cli_djinn.py    # CLI entry point tests
├── test_cli_mcpgateway.py   # MCP CLI tests
├── test_commands_agent.py   # Agent command tests
├── test_commands_container.py  # Container command tests
├── test_commands_mcp.py     # MCP command tests
├── test_config_models.py    # Pydantic model tests
├── test_config_loader.py    # Config loading tests
├── test_config_defaults.py  # Default values tests
├── test_core_docker.py      # Docker operations tests
├── test_core_console.py     # Console output tests
├── test_core_paths.py       # Path utility tests
└── test_core_decorators.py  # Decorator tests
```

### 11.2 Key Fixtures (conftest.py)

```python
@pytest.fixture
def temp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create temporary config directory."""
    config_dir = tmp_path / ".config" / "djinn-in-a-box"
    config_dir.mkdir(parents=True)
    monkeypatch.setattr("djinn_in_a_box.core.paths.CONFIG_DIR", config_dir)
    monkeypatch.setattr("djinn_in_a_box.core.paths.CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr("djinn_in_a_box.core.paths.AGENTS_FILE", config_dir / "agents.toml")
    return config_dir


@pytest.fixture
def sample_config(tmp_path: Path) -> AppConfig:
    """Create sample AppConfig with valid code_dir."""
    code_dir = tmp_path / "projects"
    code_dir.mkdir()
    return AppConfig(code_dir=code_dir)


@pytest.fixture
def mock_subprocess(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock subprocess.run for Docker command tests."""
    mock = MagicMock()
    mock.return_value.returncode = 0
    mock.return_value.stdout = ""
    mock.return_value.stderr = ""
    monkeypatch.setattr("subprocess.run", mock)
    return mock
```

### 11.3 Testing Patterns

**Model Validation Tests**:
```python
def test_resource_limits_cpu_reservation_exceeds_limit():
    with pytest.raises(ValidationError, match="cpu_reservation.*cannot exceed"):
        ResourceLimits(cpu_limit=2, cpu_reservation=4)
```

**CLI Command Tests** (using Typer's CliRunner):
```python
from typer.testing import CliRunner

def test_config_show_displays_config(temp_config_dir, sample_config):
    save_config(sample_config)
    runner = CliRunner()
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "code_dir" in result.output
```

**Decorator Tests**:
```python
def test_handle_config_errors_catches_exception():
    @handle_config_errors
    def failing_func():
        raise ConfigNotFoundError("Test error")

    with pytest.raises(SystemExit) as exc_info:
        failing_func()
    assert exc_info.value.code == 1
```

### 11.4 Coverage

- **328 tests** total
- **89% code coverage**
- All critical paths covered including error handling

---

## 12. Extension Points

### 12.1 Adding a New Agent

1. **Edit `~/.config/djinn-in-a-box/agents.toml`**:
   ```toml
   [agents.myagent]
   binary = "myagent"
   description = "My Custom Agent"
   headless_flags = ["--headless"]
   read_only_flags = ["--read-only"]
   write_flags = ["--write"]
   json_flags = ["--json"]
   model_flag = "--model"
   prompt_template = '"$AGENT_PROMPT"'
   ```

2. **Or modify `defaults.py`** for built-in support:
   ```python
   DEFAULT_AGENTS["myagent"] = AgentConfig(
       binary="myagent",
       description="My Custom Agent",
       # ...
   )
   ```

### 12.2 Adding a New Command

1. **Create command function in appropriate module**:
   ```python
   # commands/container.py
   def my_new_command(
       arg: Annotated[str, typer.Argument(help="Description")],
   ) -> None:
       """Command docstring becomes help text."""
       # Implementation
   ```

2. **Register in CLI**:
   ```python
   # cli/djinn.py
   app.command(name="my-command")(container.my_new_command)
   ```

### 12.3 Adding a New Volume Category

```python
# config/defaults.py
VOLUME_CATEGORIES["my-category"] = [
    "djinn-my-volume-1",
    "djinn-my-volume-2",
]
```

### 12.4 Adding Configuration Options

1. **Add field to appropriate model**:
   ```python
   # config/models.py
   class AppConfig(BaseModel):
       # ... existing fields
       my_option: str = "default"
   ```

2. **Update loader to serialize**:
   ```python
   # config/loader.py
   def save_config(config: AppConfig) -> None:
       data = {
           # ... existing fields
           "my_option": config.my_option,
       }
   ```

### 12.5 Adding a New Decorator

```python
# core/decorators.py
def require_docker(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator to ensure Docker is available."""
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        result = subprocess.run(["docker", "info"], capture_output=True)
        if result.returncode != 0:
            error("Docker is not running")
            raise typer.Exit(1)
        return func(*args, **kwargs)
    return wrapper
```

---

## Appendix A: Command Reference

### djinn

```
djinn --help
djinn --version

djinn init [--code-dir PATH] [--force]
djinn config show
djinn config path

djinn build [--no-cache] [--pull/--no-pull] [--progress auto|plain|tty]
djinn start [--mount PATH] [--docker] [--firewall] [--cmd COMMAND]
djinn enter
djinn status
djinn auth [claude|gemini|opencode|all]
djinn audit
djinn update

djinn run AGENT PROMPT [--write] [--json] [--model MODEL] [--docker] [--firewall] [--mount PATH] [--timeout SECONDS]
djinn agents [--verbose] [--json]

djinn clean volumes [--category CATEGORY] [--all] [--force]
djinn clean all [--force]
```

### mcpgateway

```
mcpgateway --help

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

## Appendix B: Environment Variables

| Variable | Set By | Purpose |
|----------|--------|---------|
| `AGENT_PROMPT` | compose_run() | Prompt text for agent execution |
| `CODE_DIR` | _build_compose_env() | Projects directory mount |
| `TZ` | _build_compose_env() | Container timezone |
| `CPU_LIMIT` | _build_compose_env() | Docker CPU limit |
| `MEMORY_LIMIT` | _build_compose_env() | Docker memory limit |
| `DOCKER_ENABLED` | _build_compose_env() | Enable docker socket proxy |
| `FIREWALL_ENABLED` | _build_compose_env() | Enable network firewall |

---

## Appendix C: File Dependencies Graph

```
pyproject.toml
    └── defines entry points → cli/djinn.py, cli/mcpgateway.py

cli/djinn.py
    ├── imports → commands/agent.py
    ├── imports → commands/container.py
    ├── imports → config/__init__.py (re-exports)
    └── imports → core/console.py

cli/mcpgateway.py
    └── imports → commands/mcp.py

commands/agent.py
    ├── imports → config/loader.py
    ├── imports → config/models.py (TYPE_CHECKING)
    ├── imports → core/console.py
    ├── imports → core/docker.py
    └── imports → core/decorators.py

commands/container.py
    ├── imports → config/loader.py
    ├── imports → config/defaults.py
    ├── imports → core/console.py
    ├── imports → core/docker.py
    ├── imports → core/paths.py
    └── imports → core/decorators.py

commands/mcp.py
    ├── imports → core/console.py
    ├── imports → core/docker.py
    └── imports → core/paths.py

config/__init__.py
    └── re-exports → config/loader.py, config/models.py, config/defaults.py

config/loader.py
    ├── imports → config/models.py
    ├── imports → config/defaults.py
    └── imports → core/paths.py

config/models.py
    └── (no internal dependencies)

config/defaults.py
    └── imports → config/models.py

core/docker.py
    ├── imports → core/paths.py
    └── imports → core/console.py

core/theme.py
    └── (no internal dependencies, uses rich.style, rich.theme)

core/console.py
    └── imports → core/theme.py (TODAI_THEME, ICONS)

core/paths.py
    └── (no internal dependencies)

core/decorators.py
    └── imports (deferred) → config/loader.py, core/console.py
```

---

*Last updated: 2026-01-30*
*Architecture Version: 1.1.0*
*CLI Version: 1.0.0*
*Theme System: TodAI Design v1.0.0*
