# Djinn in a Box Implementation Guide

> **Version**: 0.1.0
> **Architecture**: Python Typer CLI, Pydantic v2 config, Docker Compose runtime
> **License**: MIT

This is the developer reference for the current implementation. It is written
from the source tree, Compose files, scripts, Dockerfile, seed templates, and
tests, and it is intended to explain the whole system from this single document.

## Product Model

Djinn ships the mechanism:

- a Python CLI named `djinn`
- a companion `mcpgateway` CLI
- a Docker image and Compose stack
- host-side setup, validation, and repair flows
- neutral seed templates under `templates/seed/`
- container startup merge and reverse-sync scripts

Djinn does not ship a user's working configuration. The root-level `config/`
directory is local-only and gitignored. It is created from neutral templates on
first run, then owned by the user. Package lists, agent settings, credential
stores, and local command choices remain outside the published source.

## Repository Layout

```text
.
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── docker-compose.docker.yml
├── docker-compose.docker-direct.yml
├── docs/
│   └── design/
│       └── CLI_DESIGN_SYSTEM.md
├── src/djinn_in_a_box/
│   ├── cli/
│   │   ├── djinn.py
│   │   └── mcpgateway.py
│   ├── commands/
│   │   ├── agent.py
│   │   ├── backup.py
│   │   ├── config.py
│   │   ├── container.py
│   │   ├── doctor.py
│   │   ├── mcp.py
│   │   └── session.py
│   ├── config/
│   │   ├── defaults.py
│   │   ├── loader.py
│   │   └── models.py
│   └── core/
│       ├── __init__.py
│       ├── banner.py
│       ├── console.py
│       ├── decorators.py
│       ├── docker.py
│       ├── exceptions.py
│       ├── hostinfo.py
│       ├── paths.py
│       ├── seeding.py
│       ├── session.py
│       └── theme.py
├── scripts/
│   ├── entrypoint.sh
│   ├── output-lib.sh
│   ├── seed-lib.sh
│   ├── mcp-register.sh
│   ├── init-firewall.sh
│   └── update-agents.sh
├── tools/
│   └── install.sh
├── templates/seed/
│   ├── config/
│   ├── packages.txt
│   └── tools.txt
└── tests/
```

`src/djinn_in_a_box/core/paths.py` defines the persistent host paths:

- `CONFIG_DIR`: `~/.config/djinn_in_a_box/`
- `CONFIG_FILE`: `~/.config/djinn_in_a_box/config.toml`
- `AGENTS_FILE`: `~/.config/djinn_in_a_box/agents.toml`
- `BACKUPS_DIR`: `~/.djinn/backups/`

The project root is discovered by `get_project_root()` by walking upward from
the package until it finds `docker-compose.yml`.

## Runtime Architecture

```text
user
  |
  v
djinn CLI (Typer)
  |
  +-- commands/config.py     init, config show/path/set/edit
  +-- commands/container.py  build, start, auth, status, clean, audit, update, enter
  +-- commands/doctor.py     doctor, doctor --fix, preflight
  +-- commands/agent.py      djinn run, djinn agents
  +-- commands/session.py    djinn session
  +-- commands/backup.py     backup, restore
  |
  v
core + config
  |
  +-- config/models.py       AppConfig, ResourceLimits, ShellConfig, AgentConfig
  +-- config/loader.py       TOML load/save, agent default fallback
  +-- core/docker.py         Compose env bridge, Docker operations, backup helpers
  +-- core/seeding.py        host-side first-run seed repair/copy
  +-- core/session.py        docker exec and host-mode session runner
  |
  v
Docker Compose + container entrypoint
```

`mcpgateway` is a separate Typer app in `cli/mcpgateway.py` that delegates to
`commands/mcp.py`.

## CLI Output System

The implementation rationale and visual contract live in
`docs/design/CLI_DESIGN_SYSTEM.md`. The runtime source of truth is split by
producer:

- `core/theme.py` owns Python Rich styling. It defines eight branded hex
  palette constants (`PRIMARY`, `SECONDARY`, `SUCCESS`, `ERROR`, `WARNING`,
  `PATH`, `MUTED`, `BORDER`) plus `INFO = "blue"`, deliberately using
  terminal-adaptive ANSI blue color 4 for informational output. `DJINN_THEME`
  exposes semantic roles including `success`, `error`, `warning`, `info`,
  `info.bold`, `path`, `primary`, `secondary`, `muted`, and `border`. Derived
  roles map back to the palette: `header`, `table.title`, and `table.header`
  use bold `primary`; `table.category` uses `secondary`; `table.value` uses
  `muted`; `status.enabled`, `status.disabled`, and `status.error` use
  `success`, `warning`, and `error`.
- `tests/test_core/test_theme.py` pins the palette and derived-role mapping. It
  also gates command and CLI modules against literal Rich color usage: palette
  hex values must appear once in `theme.py`, and command/CLI Python files must
  not introduce literal hex colors or named Rich color style literals.
- `core/console.py` defines `console` for stdout and `err_console` for stderr.
  Operational UI helpers (`success()`, `error()`, `warning()`, `info()`,
  `status_line()`, `blank()`, `header()`, and `rule()`) print through
  `err_console`. `rule()` owns section spacing by writing one leading blank
  line, then a border-styled rule with an optional `primary.bold` title.
  `status_line(..., value_style=None)` supports styling values independently,
  with `value_style="path"` used for filesystem paths such as Projects,
  Workspace, CODE_DIR, and sync roots.
- `core/banner.py` renders the `djinn start` banner to `err_console`. Full mode
  shows the Braille djinn logo with a `PRIMARY` to `SECONDARY` vertical gradient
  next to the block wordmark. Wordmark mode keeps the wordmark without Braille.
  Plain mode prints `Djinn in a Box`. The degradation predicates are:
  plain-required output (`NO_COLOR`/Rich `no_color` or dumb terminal),
  non-UTF-8 output, and insufficient full-banner capability (no color or width
  below 70 columns), which degrades from full to wordmark after UTF-8 passes.
- `scripts/output-lib.sh` owns container startup shell UI. It is sourceable from
  both zsh and bash, guarded by `_DJINN_OUTPUT_LIB_LOADED` so repeated sourcing
  does not re-declare readonly constants. With `COLORTERM=truecolor` or `24bit`
  it emits exact RGB escapes matching the Python palette; otherwise it uses
  ANSI-256 fallbacks, while `info` intentionally remains basic ANSI blue in
  both tiers. Per no-color.org, `NO_COLOR` disables color only when present with
  a non-empty value. `DJINN_TERM_WIDTH` takes precedence for shell rule width,
  followed by `COLUMNS`, `tput cols`, `stty size`, and finally 80 columns.
  Public helpers are `ui_section`, `ui_ok`, `ui_warn`, `ui_err`, `ui_info`,
  `ui_item`, and `ui_boxed`.

Shell UI consumers include `scripts/entrypoint.sh`, `scripts/mcp-register.sh`,
`scripts/seed-lib.sh` marker output, `scripts/init-firewall.sh`,
`tools/install.sh`, and `scripts/update-agents.sh`.

## Configuration Model

`config/models.py` is the schema source of truth.

`AppConfig` fields:

- `code_dir: Path`: required project directory mounted as `/home/dev/projects`
- `timezone: str`: IANA timezone, default `UTC`
- `config_root: Path`: local credential/config bind-mount root, default
  `~/.djinn/config`
- `resources: ResourceLimits`
- `shell: ShellConfig`

`ResourceLimits` defaults are:

- `cpu_limit = 4`
- `memory_limit = "8G"`
- `cpu_reservation = 1`
- `memory_reservation = "2G"`

Memory values are validated by `validate_memory_format()` and normalized to an
uppercase suffix. Reservations cannot exceed limits.

`ShellConfig` controls host shell mounts:

- `skip_mounts = False`
- `omp_theme_path = None`

`AgentConfig` defines CLI agent invocation shape: binary, headless flags,
read-only flags, write flags, JSON flags, model flag, and prompt template.

## Config File Loading

`config/loader.py` loads `~/.config/djinn_in_a_box/config.toml`.

The TOML layout stores top-level application fields under `[general]`, while
`resources` and `shell` remain structured sections. `load_config()` flattens
`[general]` into the `AppConfig` constructor and raises:

- `ConfigNotFoundError` when the file is absent
- `ConfigValidationError` for invalid TOML or Pydantic validation failures

`save_config()` serializes back to nested TOML and writes atomically with
`tempfile.mkstemp()` plus `os.replace()`.

Agent definitions are loaded by `load_agents()` with this priority:

1. explicit file path, if supplied
2. `~/.config/djinn_in_a_box/agents.toml`
3. `DEFAULT_AGENTS` from `config/defaults.py`

The shipped defaults cover `claude`, `gemini`, `codex`, and `opencode`.

## Config Commands

`commands/config.py` implements:

- `init_config()` exposed as `djinn init`
- `config_show()` exposed as `djinn config show`
- `config_path()` exposed as `djinn config path`
- `config_set()` exposed as `djinn config set`
- `config_edit()` exposed as `djinn config edit`

`djinn init` is the entry point. It creates the app config directory, prompts
for the projects directory and timezone, then uses progressive disclosure for
advanced resource and shell settings. The simple path accepts suggested
resources from `core/hostinfo.py`; advanced prompts allow explicit CPU, memory,
and shell-mount choices.

`core/hostinfo.detect_timezone()` reads `/etc/localtime` when it is an IANA
timezone symlink and falls back to `UTC`. `suggest_resources()` reads
`/proc/meminfo`, uses half the host memory and half the CPU count, clamps CPU to
the `ResourceLimits` bounds, and falls back to model defaults on probe failure.

`ALLOWED_CONFIG_KEYS` controls `djinn config set`:

- `general.code_dir`
- `general.timezone`
- `general.config_root`
- `resources.cpu_limit`
- `resources.memory_limit`
- `resources.cpu_reservation`
- `resources.memory_reservation`
- `shell.skip_mounts`
- `shell.omp_theme_path`

`config_edit()` runs `$EDITOR` or `vi`, then reloads and validates the file.

## Config Root and Compose Environment Bridge

`AppConfig.config_root` is the configuration root source of truth for
credential/config bind mounts. `core/docker.py` resolves it through
`get_config_root(config)`:

1. `DJINN_CONFIG_ROOT` in the host environment, when set
2. `config.config_root`, when an `AppConfig` is available
3. default `~/.djinn/config`

The Compose files use host-side interpolation variables such as
`${CODE_DIR}`, `${DJINN_CONFIG_ROOT}`, `${TZ}`, and resource variables. Those are
not the same as `docker compose run -e` container variables. Djinn centralizes
host interpolation through:

- `build_compose_env(config)` renders Compose variables from `AppConfig`
- `_compose_host_env(config)` overlays them onto `os.environ`
- `_run_compose(args, config, cwd)` is the captured `docker compose` choke-point

Captured Compose calls such as `compose_build()`, `compose_up()`,
`compose_down()`, and Docker proxy cleanup route through `_run_compose()`.
`compose_run()` is the sanctioned interactive/headless run site; it also builds
`host_env = _compose_host_env(config)` before calling `subprocess.run()`.
When stdout or stderr is a TTY, `build_compose_env()` also renders
`DJINN_TERM_WIDTH` from `shutil.get_terminal_size().columns`; otherwise that
variable is left to inherited host environment or Compose defaults.

`ensure_host_env(config)` provisions bind-mount sources before Compose runs, so
the Docker daemon does not auto-create missing paths as root-owned directories.
It creates credential subdirectories from `SYNC_PATHS["credentials"]`,
`~/.djinn/sessions`, `~/.djinn/backups`, `~/.ssh`, and `~/.gitconfig`.

## Host-Side Seeding

`core/seeding.py` copies neutral seed templates from `templates/seed/` into the
local root-level `config/`, `packages.txt`, and `tools/tools.txt` locations.

`SEED_MANIFEST` defines every seed source, target, and kind:

- `config/claude/CLAUDE.md`
- `config/claude/settings.json`
- `config/claude/skills`
- `config/claude/commands`
- `config/claude/agents`
- `config/claude/context`
- `config/claude/scripts`
- `config/gemini`
- `config/opencode`
- `config/mcp-servers.json`
- `config/agents.toml.example`
- `tools.txt` copied to `tools/tools.txt`
- `packages.txt`

`seed_config(project_root)` is copy-if-absent. Existing targets of the correct
type are never overwritten. Wrong-type targets are repaired by `_repair_wrong_type()`.
Dangling symlinks are treated as existing targets because `Path.exists()` would
otherwise miss them.

Copies are atomic:

- file seeds copy to `.<name>.seed-tmp`, then `os.replace()`
- directory seeds copy to a temporary directory, then `os.replace()`
- `.gitkeep` files are ignored by `_ignore_gitkeep()`

Blocking ancestors are handled explicitly. `_blocking_ancestor()` detects the
nearest non-directory or dangling symlink in the parent chain and raises a
`SeedingError` with a removal remedy. Permission failures name the existing
ancestor and provide an ownership or removal remedy. Missing seed sources also
raise `SeedingError` with a reinstall or reclone remedy.

## Container-Side Seed and Merge

`scripts/entrypoint.sh` sources `/home/dev/seed-lib.sh` from `scripts/seed-lib.sh`
inside the image. Startup then performs runtime reconciliation between host
seed mounts and persistent container locations.

`scripts/seed-lib.sh` provides:

- `merge_settings(base, overlay, output)`: deep-merges JSON with overlay wins.
  `enabledPlugins` and `extraKnownMarketplaces` are replacement keys rather than
  recursive merge keys, so stale plugin entries do not persist.
- `sync_seed(label, seed_dir, target_dir, config_file)`: clean-syncs managed
  seed directories and root files into a persistent target, records a
  `.seed-manifest`, deletes stale manifest-tracked files, and deep-merges
  `settings.json` when requested.
- `claude_settings_merge(seed_dir, target_settings_file)`: merges the tracked
  Claude settings baseline with optional `settings.local.json`. It has a
  minimal-seed guard: if `CLAUDE.md` or `settings.json` is missing, it prints a
  repair hint and skips the merge rather than writing incomplete state.
- `reverse_sync_file(volume_file, seed_file)`: best-effort copy from container
  state back to writable seed mounts on shell exit.

`entrypoint.sh` applies those helpers as follows:

```text
container start
  |
  +-- volume ownership repair for cache/workspace paths
  +-- source seed-lib.sh
  +-- restore ~/.claude.json from the Claude volume when present
  +-- claude_settings_merge ~/.claude_seed -> ~/.claude/settings.json
  +-- sync_seed gemini   ~/.gemini_seed   -> ~/.gemini
  +-- sync_seed opencode ~/.opencode/seed -> ~/.config/opencode
  +-- source mcp-register.sh and register MCP servers
  +-- install optional cached tools
  +-- print security summary, including firewall, Docker access, and MCP state
  +-- run interactive zsh
  +-- reverse-sync selected settings files on exit
```

Shell-side startup output is sectioned through `scripts/output-lib.sh`:
`Seed & Config`, `MCP`, `Tools`, and `Security`. `mcp-register.sh` captures
third-party CLI output from MCP add/remove commands and passes non-empty output
through `ui_boxed`, so external tool chatter stays visibly nested under the MCP
section while remaining on stderr.

For Claude, `docker-compose.yml` mounts selected directories and files from
root-level `config/claude` directly into the live `~/.claude` tree. Only
settings are merged. In-session settings changes are reverse-synced to
`config/claude/settings.local.json`, not to the tracked baseline template.

## Docker Compose Runtime

`docker-compose.yml` defines a stable project name and two services:

- `dev`: normal development container on `djinn-network`
- `dev-auth`: auth profile using host networking for OAuth-style callbacks

Common mounts include:

- `${DJINN_CONFIG_ROOT}/claude` to `/home/dev/.claude`
- `${DJINN_CONFIG_ROOT}/gemini` to `/home/dev/.gemini`
- `${DJINN_CONFIG_ROOT}/codex` to `/home/dev/.codex`
- `${DJINN_CONFIG_ROOT}/opencode` to `/home/dev/.opencode`
- `${DJINN_CONFIG_ROOT}/gh` to `/home/dev/.config/gh`
- named volumes for caches, OpenCode data, VS Code server state, and workspace
  metadata
- read-only `~/.ssh` and `~/.gitconfig`
- root-level `config/` seed mounts
- `${CODE_DIR}` to `/home/dev/projects`
- `${HOME}/.djinn/sessions` to `/home/dev/sessions`

The base Compose environment sets `TZ`, `NO_COLOR`, `DJINN_TERM_WIDTH`,
`UV_LINK_MODE=copy`, `LOCAL_ENDPOINT`, and `GEMINI_FORCE_FILE_STORAGE=true`.
`NO_COLOR` and `DJINN_TERM_WIDTH` propagate the host's plain-output and terminal
width decisions into the container shell UI. Resource limits use the Compose
variables rendered by `build_compose_env()`.

`docker-compose.docker.yml` adds a Docker socket proxy service and sets
`DOCKER_HOST=tcp://docker-proxy:2375` for the dev container. The proxy allows
selected read and lifecycle operations and blocks higher-risk Docker APIs.

`docker-compose.docker-direct.yml` mounts `/var/run/docker.sock` directly and
sets `DOCKER_DIRECT=true`. The CLI warns that this gives the container full
Docker control.

`core/docker.py` also auto-detects optional host mounts:

- `get_audio_mount_args()` mounts the PulseAudio/PipeWire socket when present
- `get_dbus_mount_args()` mounts the session bus only when the host path is an
  actual Unix socket
- `get_shell_mount_args(config)` mounts `.zshrc`, an explicit Oh My Posh theme,
  and the host shell custom directory unless `shell.skip_mounts` is true

## Image Build

`Dockerfile` builds from `debian:bookworm-slim`. It installs base packages,
audio client support, optional packages from `packages.txt`, Docker CLI,
Compose plugin, GitHub CLI, uv, a non-root `dev` user, zsh setup, Node via fnm,
and the supported coding agent CLIs.

The image locale is `C.UTF-8`. Runtime Docker access is disabled unless the user
starts with proxy or direct Docker options.

Optional runtime tool installers are copied from `tools/`, with cache locations
backed by named volumes.

## Container Lifecycle Commands

`commands/container.py` implements:

- `build()`: loads config, runs `preflight(config)`, refreshes build-time
  local-only files via `_sync_build_files()`, then calls `compose_build()`.
- `start()`: resolves Docker mode, preflights, ensures `djinn-network`, resolves
  `--here` or `--mount`, prints the banner plus `Environment` and `Container`
  rules on stderr, then calls `compose_run()` for `dev`.
- `auth()`: starts the `dev-auth` profile with host networking; proxy mode
  starts `docker-proxy` separately because the auth container uses host network.
- `status()`: reports config, containers, known volumes, config-root paths,
  networks, Docker proxy, and MCP Gateway status.
- `clean_default()`: `djinn clean` stops and removes containers with
  `compose_down(config=None)`, using best-effort placeholders.
- `clean_volumes()`: lists or deletes named volume categories and clears
  config-root sync paths by category.
- `clean_all()`: stops containers, deletes all known named volumes, clears all
  sync paths, and deletes the network.
- `audit()`: prints Docker proxy logs.
- `update()`: runs `scripts/update-agents.sh`.
- `enter()`: opens a zsh shell in the first running Djinn container.

`_sync_build_files(config)` copies `packages.txt` and `tools.txt` from
`get_config_root(config)/repo-dotfiles` into the build context when those local
files exist. This is a build-context refresh helper, not a Compose bind-mount.
During `djinn build`, the loaded `AppConfig` is threaded through, so
`general.config_root` from `config.toml` is honored unless `DJINN_CONFIG_ROOT`
is exported in the host environment, which still takes precedence.

## Doctor and Preflight

`commands/doctor.py` has two levels:

- `doctor(fix=False)`: full diagnostic report
- `preflight(config)`: fast critical path used before `build`, `start`, and
  `auth`

`run_checks(config, config_error)` reports Docker installation, daemon reach,
socket permission, Compose v2, configuration, projects directory, config root,
image, network, optional Docker MCP plugin, D-Bus session availability, and seed
config presence.

`doctor --fix` calls `_doctor_fix(config)`, which attempts:

- `ensure_host_env(config)`
- `seed_config(project_root)`
- `ensure_network()`

It exits non-zero when hard checks or repairs fail.

`preflight(config)` first verifies Docker is installed and the daemon is
reachable. Only after Docker is usable does it provision host directories with
`ensure_host_env(config)` and reseed local config with `seed_config(project_root)`.
This keeps Docker-down failures from creating unrelated host artifacts.

## Agent Commands

`commands/agent.py` implements headless one-shot agent runs.

`build_agent_command(agent_config, write, json_output, model)` assembles a shell
command string from `AgentConfig`. It appends the prompt template, which expands
`$AGENT_PROMPT` inside the container.

`run()` loads app config and agent config, validates the requested agent, ensures
the Docker network, mounts the current directory by default, and calls
`compose_run(..., interactive=False, env={"AGENT_PROMPT": prompt})`.

`agents()` lists configured agents, with verbose and JSON modes.

## Session Workspace Contract

`commands/session.py` exposes `djinn session`.

Host workspaces live under:

```text
~/.djinn/sessions/<project>/
```

The `project` name must match `^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`, enforced by
`core/session.py::SessionManager.__init__()`.

The command resolves both the sessions root and requested workspace and rejects
paths that escape the sessions root. `--create` may create a missing workspace,
but only after that containment check. Existing non-directories are rejected.
Without `--create`, the workspace must already exist.

`SessionManager` runs in container mode when a running `djinn` container exists.
It maps host paths under `~/.djinn/sessions` to `/home/dev/sessions/...` and uses
`docker exec` with `TERM=xterm-256color` and `COLORTERM=truecolor`. Each session
workspace is initialized as a git repository if needed.

If no container is running, `SessionManager.preflight_check()` allows host mode
only when `claude` is available on `PATH`. It does not check the requested
agent's binary during preflight; selecting another host agent can still fail at
invocation with `Agent binary not found: <binary>`. Host-mode interactive and
headless commands run directly in the host workspace.

## Backup and Restore

`commands/backup.py` handles backup and restore for Docker named volumes and
config-root paths.

Default backup categories are:

- `credentials`
- `repo-dotfiles`
- `data`

Category definitions come from `config/defaults.py`:

- `VOLUME_CATEGORIES["cache"]`: `djinn-uv-cache`, `djinn-tools-cache`,
  `djinn-vscode-server`
- `VOLUME_CATEGORIES["data"]`: `djinn-opencode-data`,
  `djinn-vscode-workspaces`
- `SYNC_PATHS["credentials"]`: `claude`, `gemini`, `codex`, `opencode`, `gh`
- `SYNC_PATHS["repo-dotfiles"]`: `repo-dotfiles`

`backup()` refuses to run while Djinn containers are active. It stages one
archive per selected named volume or config-root subdirectory, then writes a
single dated `djinn-backup-YYYY-MM-DD.tar.gz` under `~/.djinn/backups/` and
removes older backups.

`restore()` also refuses to run while containers are active, extracts the newest
backup archive, restores config-root path archives by filename prefix, and
restores named volumes by validated volume name.

Cache volumes are intentionally excluded from default backups because they are
large and rebuildable.

## MCP Gateway

`commands/mcp.py` implements the separate `mcpgateway` CLI.

It requires Docker and the Docker MCP CLI plugin for server management. It uses
`mcp/docker-compose.yml`, the shared `djinn-network`, and a gateway container
named `mcp-gateway`.

Commands include:

- `start`, `stop`, `restart`
- `status`, `logs`
- `enable`, `disable`, `servers`, `catalog`
- `test`
- `clean`

The main dev container receives `MCP_GATEWAY_URL` pointing at the gateway over
the Docker network. The auth container uses a host endpoint because it runs with
host networking.

## Data Flow Diagrams

First run:

```text
djinn init
  |
  +-- prompt for code_dir and timezone
  +-- optionally prompt for resources and shell mounts
  +-- save ~/.config/djinn_in_a_box/config.toml atomically
  +-- ensure_host_env(config)
  +-- seed_config(project_root)
  v
local config is ready; build/start can run
```

Compose run:

```text
load_config()
  |
  v
AppConfig
  |
  +-- build_compose_env(config)
  +-- _compose_host_env(config)
  v
docker compose parses ${CODE_DIR}, ${DJINN_CONFIG_ROOT}, TZ, NO_COLOR,
DJINN_TERM_WIDTH, resources
  |
  v
container receives bind mounts, named volumes, and selected -e variables
```

Container startup:

```text
djinn start
  |
  +-- banner()
  +-- Environment rule: Projects, Docker, Firewall, Workspace, Shell, Audio
  +-- Container rule
  v
entrypoint.sh
  |
  +-- optional pre-seed firewall initialization
  +-- repair writable volume ownership
  +-- Seed & Config: merge/copy seed config
  +-- MCP: register MCP servers and box third-party CLI output
  +-- Tools: install optional tools
  +-- Security: summarize firewall, Docker access, and MCP gateway state
  +-- run interactive shell
  +-- reverse-sync selected settings on exit
```

Backup:

```text
djinn backup
  |
  +-- refuse if containers are running
  +-- collect selected named volumes
  +-- collect existing config-root paths
  +-- stage per-item tar.gz files
  +-- write ~/.djinn/backups/djinn-backup-YYYY-MM-DD.tar.gz
```

Session:

```text
djinn session --project name [--create]
  |
  +-- validate project name
  +-- contain workspace under ~/.djinn/sessions
  +-- require or create workspace
  +-- prefer docker exec into running djinn container
  +-- otherwise allow host mode only when claude is on PATH
  +-- selected non-claude host agents may still fail at invocation
```

## Error Handling

Configuration commands use `@handle_config_errors` from `core/decorators.py` to
turn config exceptions into user-facing CLI exits. Command modules catch
expected `OSError`, `PermissionError`, validation, and subprocess failures near
the command boundary and raise `typer.Exit` with a specific status.

Docker helpers return `RunResult` objects with `returncode`, `stdout`, `stderr`,
and a `success` property. This keeps subprocess details out of command control
flow until the command decides how to report them.

Seeding errors use `SeedingError` when the condition needs a precise user
remedy. The caller prints the remedy and exits cleanly.

## Test Layout

Tests live under `tests/` with command-specific tests in `tests/test_commands/`
and session core tests in `tests/test_core/`.

Coverage areas include:

- CLI registration and behavior
- config loading, saving, validation, defaults, and paths
- Docker Compose environment injection and Docker helper behavior
- hostinfo detection and resource suggestions
- host-side seed copying, repair, and template completeness
- `scripts/seed-lib.sh` and entrypoint MCP behavior
- backup/restore command behavior
- session command containment and `SessionManager`
- doctor/preflight behavior

`tests/conftest.py` removes `FORCE_COLOR` before importing CLI modules. Rich
reads color-forcing variables from the live process environment at print time,
so scrubbing that variable keeps substring assertions stable across shells.

Shared fixtures include:

- `mock_home`: isolated fake home directory
- `mock_app_config`: an `AppConfig` with temporary `code_dir` and default
  resource/shell models

The expected verification command for this docs-only change is:

```bash
uv run pytest -q
```
