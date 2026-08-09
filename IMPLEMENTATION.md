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
│       ├── agent_runner.py
│       ├── config_lock.py
│       ├── config_sync.py
│       ├── config_sync_adapters.py
│       ├── config_workflow.py
│       ├── workflow_publisher.py
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
│   ├── settings-copy.py
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
  +-- commands/config.py     init, config show/path/set/edit/status/sync
  +-- commands/container.py  build, start, status, clean, audit, update, enter
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
  +-- core/config_sync.py    canonical workflow audit, snapshot, and sync
  +-- core/config_sync_adapters.py  closed native readers/renderers
  +-- core/workflow_publisher.py  stdlib-only shared publisher and CLI
  +-- core/config_workflow.py  shared preflight and runtime publication
  +-- core/config_lock.py    config-setting directory lock
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
- `config_sync: ConfigSyncConfig`

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
read-only flags, write flags, JSON flags, model flag, optional default model,
and prompt template.

`ConfigSyncConfig.source` is one of `claude`, `codex`, or `opencode` and defaults
to `claude`. It selects the native global workflow authority for the deployment;
it does not select an agent for `run` or `session`.

## Config File Loading

`config/loader.py` loads `~/.config/djinn_in_a_box/config.toml`.

The TOML layout stores top-level application fields under `[general]`, while
`resources`, `shell`, and `config_sync` remain structured sections.
`load_config()` flattens
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
- `config_status()` exposed as `djinn config status`
- `config_sync()` exposed as `djinn config sync`

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
- `config_sync.source`

`config_edit()` runs `$EDITOR` or `vi`, then reloads and validates the file.
Changes that may select a different workflow source coordinate through the
exclusive lock on the existing `config/` directory.

## Global Workflow Ownership and Audit

The workflow source is deployment-wide, including the shared demo deployment.
The implementation has no per-tenant source selector. Canonical native roots
remain under the ignored project-local `config/{claude,codex,opencode}` tree.
Only the selected tool's native instruction form is authoritative:

| Category          | Claude Code                                         | Codex                                            | OpenCode                             |
| ----------------- | --------------------------------------------------- | ------------------------------------------------ | ------------------------------------ |
| Root instructions | `CLAUDE.md` plus managed `AGENTS.md`                | `AGENTS.md` plus managed `CLAUDE.md`             | `AGENTS.md` plus managed `CLAUDE.md` |
| Agents            | `agents/*.md`                                       | `agents/*.toml`                                  | `agents/*.md`                        |
| Skills            | `skills/<name>/**`                                  | `skills/<name>/**`                               | `skills/<name>/**`                   |
| Commands          | `commands/*.md`                                     | `skills/command-<name>/**`                       | `commands/*.md`                      |
| Support           | `context/**`, `scripts/**`                          | `context/**`, `scripts/**`                       | `context/**`, `scripts/**`           |
| Native-only hooks | three Python scripts plus `settings.json` fragments | three Python scripts plus `hooks.json` fragments | three named plugin files             |

The known hook fragments are `SessionStart`, `PreToolUse`, and `Stop`; Codex
also owns the `project_doc_fallback_filenames` bridge in `config.toml`. Hooks
and their registrations are native-only, like the Claude-only `/codex-review`
command: a present native item is validated for ownership, UTF-8, and containment
(with the OpenCode export-marker check), but is never cross-tool projected or
stale-removed. Missing native hooks are allowed. Legacy canonical records for
target-view hooks are released on the next sync without deleting the file or
carrier key.
Repository-local instruction files, agents, skills, and commands are outside
this global projection and are not rewritten.

`core/config_sync_adapters.py` holds the closed ownership table, native readers,
renderers, and validation. It produces a transient typed IR; it is never a
persisted user format. Validation covers ownership, UTF-8, containment,
required fields, and JSON/TOML parsing. The three known OpenCode plugins are
copied byte-for-byte after UTF-8 and export-marker checks. A non-portable item
is invalid rather than translated: workflow synchronization contains no
provider-invocation path.

`core/config_sync.py` snapshots the selected source, renders the other two
cross-tool views, reads each tool's native-only artifacts for delivery, audits
the canonical tree, and invokes the publisher in canonical mode. Canonical
projection excludes hooks and hook registrations; runtime delivery retains them
in the complete tool view, so the Claude host-path rewrite and Compose-Claude
settings merge keep their existing inputs. It uses the publisher's content
fingerprint both after snapshot creation and at the commit point. A source
change before the first target mutation returns `source-changed` without a
write; after that point the frozen generation finishes, with the manifest
written last.

`core/workflow_publisher.py` is stdlib-only and is both the shared module API
and the standalone image CLI. It owns the five drift classes, content hashes,
executable modes, atomic replacement, stale managed-item removal, carrier-key
merges, recovery after an interrupted publication, and canonical/runtime locking.
A runtime manifest records the complete delivered native view, including
native-only hooks and OpenCode plugins; the canonical manifest deliberately
does not manage those native artifacts.
A canonical publication holds one exclusive canonical lock. A runtime
publication holds a shared canonical lock plus an exclusive target lock; an
already-held canonical lease is inherited rather than reacquired.

The one manifest schema is `{source, items}`. An item is either a file path or a
carrier path plus key path and records `content_hash` and `executable`. The
canonical instance is `config/.djinn-config-sync.json`; each publisher-managed
runtime root uses `.djinn-workflow-state.json`. Neighboring JSON carrier keys
are preserved semantically. The managed top-level TOML assignment is spliced
while preserving every other byte and then re-parsed. Existing installations
are adopted only after strict verification; an unknown or edited state fails
closed.

The audit result is one of `clean`, `source-changed`, `target-drift`,
`collision`, or `invalid-or-semantic`. `djinn config status` takes a shared
canonical lock, makes no writes, prints only sanitized identifiers and one
remedy, and exits `0` iff clean. `djinn config sync` is the explicit writer.
`commands/doctor.py` performs the same audit once for its read-only `Config
workflow` check. `doctor --fix` may seed a source root, but does not synchronize
workflow views.

Credentials, auth, history, caches, themes, UI policy, MCP, arbitrary plugins,
`PostToolUse`, status-line configuration, and unlisted settings never enter the
managed set. The only non-portable-artifact remedy is: “Author or edit the
artifact natively in the target tool's view, or make the source form portable.”

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

Captured Compose calls such as `compose_build()`,
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
Credential subdirectories and `~/.ssh` are created with mode `0700`. The mode
applies on creation only; directories that already exist are left unchanged by
`ensure_host_env`. `djinn doctor` reports such drift as a `Credential dir modes`
row, and `djinn doctor --fix` tightens the affected directories — only names from
`SYNC_PATHS["credentials"]`, only directly under the config root, and skipping
symlinked names rather than following them.

## Host-Side Seeding

`core/seeding.py` copies neutral seed templates from `templates/seed/` into the
local root-level `config/`, `packages.txt`, and `tools/tools.txt` locations.
It also ensures empty `config/claude`, `config/codex`, and `config/opencode`
workflow roots. The source-aware `seed_config(..., source=...)` entry point only
installs the Claude baseline when Claude is selected and that root is
uninitialized; generated instruction companions are not seed files.
`seed_config()` is called only by `djinn init` and `djinn doctor --fix`, before
`ensure_host_env()`. Status, audit, sync, and workflow preflight never seed or
repair a source root.

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

The publisher, not `sync_seed`, is the only writer for publisher-managed
workflow roots. `sync_seed` remains limited to the separate Gemini seed mount;
its clean-sync behavior is never applied to a mixed or operator-owned workflow
root.

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
inside the image. It keeps personal-settings persistence separate from workflow
publication.

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
  repair hint and skips the merge rather than writing incomplete state. The
  baseline wins for the owned `SessionStart`, `PreToolUse`, and `Stop` hook
  fragments; neighboring settings remain overlay-controlled.
- `reverse_sync_file(volume_file, seed_file)`: best-effort copy from container
  state back to writable seed mounts at shutdown (shell exit or SIGTERM).
- `reverse_sync_claude_settings(volume_file, seed_file)`: persists the personal
  Claude overlay after removing only those three managed hook fragments.

`entrypoint.sh` applies those helpers as follows:

```text
container start
  |
  +-- volume ownership repair for cache/workspace paths
  +-- source seed-lib.sh
  +-- restore ~/.claude.json from the Claude volume when present
  +-- claude_settings_merge ~/.claude_seed -> ~/.claude/settings.json
  +-- sync_seed gemini   ~/.gemini_seed   -> ~/.gemini
  +-- settings-copy.py persists personal OpenCode settings only
  +-- opencode-credentials.sh migrates legacy OpenCode credential files and
      re-establishes volume-to-config-root symlinks on every start
  +-- workflow-publisher.py publishes ~/.opencode/seed -> ~/.config/opencode
      using the read-only /home/dev/.djinn-canonical root and the runtime state manifest
  +-- source mcp-register.sh and register MCP servers
  +-- install optional cached tools
  +-- print security summary, including firewall, Docker access, and MCP state
  +-- run interactive zsh as a background job, waited on by PID 1
  +-- reverse-sync selected settings files on shell exit OR on SIGTERM
```

Both shutdown paths reach the reverse-sync, which matters because a detached
container (`djinn start --detach`) never exits its shell — `docker stop` sends
SIGTERM to PID 1 and that is its only shutdown. `entrypoint.sh` therefore:

- collects the reverse-sync calls in `persist_session_state()`, guarded by
  `_DJINN_STATE_PERSISTED` so the signal path and the normal path cannot both
  run it;
- traps TERM and INT into `_djinn_on_termination_signal`, which persists
  immediately and exits `128 + signal`. It deliberately does not signal the
  shell and wait for it: an interactive zsh ignores SIGTERM, so waiting would
  burn the whole `docker stop` grace period and end in SIGKILL having persisted
  nothing. The agent CLIs write settings as they change, not on exit, so there
  is nothing to flush first;
- runs the shell as a background job and `wait`s on it. As a foreground command
  it would defer every trap until it returned, which under `docker stop` never
  happens — the traps would be dead code. Job control is off in this
  non-interactive script, so the shell starts inside PID 1's process group and
  keeps the terminal's foreground group until it claims the terminal itself;
  interactive behaviour (job control, Ctrl+C) is unchanged.

Shell-side startup output is sectioned through `scripts/output-lib.sh`:
`Seed & Config`, `MCP`, `Tools`, and `Security`. `mcp-register.sh` captures
third-party CLI output from MCP add/remove commands and passes non-empty output
through `ui_boxed`, so external tool chatter stays visibly nested under the MCP
section while remaining on stderr.

For Claude, `docker-compose.yml` mounts selected directories and files from
root-level `config/claude` directly into the live `~/.claude` tree, including
both `CLAUDE.md` and the generated `AGENTS.md` companion. Only settings are
merged. This Compose-Claude runtime is manifestless: the publisher never writes
to `${DJINN_CONFIG_ROOT}/claude`. In-session settings changes are reverse-synced
to `config/claude/settings.local.json`, not to the tracked baseline template.

`core/config_workflow.prepare_config_workflow()` is the common preparation path
for `djinn start`, `djinn run`, and `djinn session`: it verifies image
compatibility for Compose paths, provisions only required runtime roots, audits,
auto-repairs deterministic `source-changed` drift, and publishes only explicit
runtime targets. It never seeds. `target-drift`, `collision`, and
`invalid-or-semantic` stop the command before agent or Compose invocation. Host
fallback publishes the selected Claude/Codex/OpenCode view to its native host
root. A running-container OpenCode session invokes the copied publisher with the
same canonical-root, target, state-manifest, and profile arguments as the
entrypoint.

## Docker Compose Runtime

`docker-compose.yml` defines a stable project name and one service:

- `dev`: normal development container on `djinn-network`

There is no separate authentication service. Every bundled CLI signs in from
inside a normal `dev` session: the tool prints a URL, the user opens it in the
host browser and pastes the returned code back into the container. No loopback
callback is *needed*, so the container requires neither host networking nor a
published port.

Selecting that flow is not uniform. Claude Code and OpenCode prompt for a pasted
code by default; Gemini CLI picks its code-paste path automatically because the
image sets `DEBIAN_FRONTEND=noninteractive` (`Dockerfile`) and no display
variable is present, which suppresses its browser launch. Codex is the
exception: plain `codex login` starts a container-local login server that the
host browser cannot reach, so users must run `codex login --device-auth` (or
choose the remote/headless option in its TUI). README documents this.

Claude Code, Gemini CLI, Codex, GitHub CLI, and OpenCode persist the resulting
credentials in config-root bind mounts. At each container start, the entrypoint
reconciles legacy OpenCode `auth.json` and `mcp-auth.json` files from the
`djinn-opencode-data` volume: it migrates volume-only files, or preserves the
config-root file and sets aside the volume file on conflict. It then
re-establishes the volume paths under `~/.local/share/opencode/` as symlinks to
the config-root files.

Common mounts include:

- `${DJINN_CONFIG_ROOT}/claude` to `/home/dev/.claude`
- `${DJINN_CONFIG_ROOT}/gemini` to `/home/dev/.gemini`
- `${DJINN_CONFIG_ROOT}/codex` to `/home/dev/.codex`
- `${DJINN_CONFIG_ROOT}/opencode` to `/home/dev/.opencode`
- `${DJINN_CONFIG_ROOT}/gh` to `/home/dev/.config/gh`
- `${DJINN_CONFIG_ROOT}/age` to `/home/dev/.config/age`
- named volumes for caches, OpenCode data, VS Code server state, and workspace
  metadata
- read-only `~/.ssh` and `~/.gitconfig`
- the writable `config/claude` seed mount plus nested direct mounts for its
  managed files, including `CLAUDE.md` and `AGENTS.md`
- the read-only canonical `./config` mount at `/home/dev/.djinn-canonical` for
  the shared publisher
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

The same module owns the repeatable user-mount contract:

- `ContainerMount` stores a resolved host source, container target, and
  read-only flag.
- `parse_mount_spec()` accepts `SRC[:DST[:ro|rw]]`, normalizes absolute targets,
  and rejects relative targets or invalid modes.
- `resolve_container_mounts()` resolves every source directory, keeps
  `--here` at `/home/dev/workspace`, and derives target-free mounts below
  `/home/dev/mount/<basename>`. A duplicate basename first receives one parent
  component (`parent-basename`), then a numeric suffix (`-2`, `-3`, ...).
- `validate_container_mounts()` checks the targets actually occupied by this
  `dev` invocation, including Compose, image-alias, runtime, Direct-socket, and
  user mounts. Equal targets and user targets that are ancestors of an occupied
  target raise `MountCollisionError`; child targets remain valid.
- `MountSpecificationError` reports invalid mount grammar or reserved targets;
  `MountCollisionError` reports the two involved mounts and the conflict path.

When a mount exists, `compose_run()` uses the first mount target as
`--workdir`. With no mount it omits `--workdir`, so the Compose service's
`working_dir: /home/dev/projects` remains effective.

## Image Build

`Dockerfile` builds from `debian:bookworm-slim`. It installs base packages,
audio client support, optional packages from `packages.txt`, Docker CLI,
Compose plugin, GitHub CLI, uv, a non-root `dev` user, zsh setup, Node via fnm,
and the supported coding agent CLIs.

The Python `djinn` CLI and its parser dependencies run on the host. The image
copies the stdlib-only `workflow_publisher.py` to
`/home/dev/workflow-publisher.py` and `settings-copy.py` to
`/home/dev/settings-copy.py`. The Dockerfile also sets
`djinn.workflow.publisher="1"`; Compose starts and OpenCode session refreshes
check that label before doing workflow work. Node agents are installed through
fnm, and the final image PATH includes `~/.local/share/fnm/aliases/default/bin`
so non-interactive processes resolve Codex and OpenCode without sourcing shell
initialization.

The image locale is `C.UTF-8`. Runtime Docker access is disabled unless the user
starts with proxy or direct Docker options.

Optional runtime tool installers are copied from `tools/`, with cache locations
backed by named volumes.

## Container Lifecycle Commands

`commands/container.py` implements:

- `build()`: loads config, runs `preflight(config)`, refreshes build-time
  local-only files via `_sync_build_files()`, then calls `compose_build()`.
- `start()`: resolves Docker mode, preflights, ensures `djinn-network`, parses
  repeatable `--mount SRC[:DST[:ro|rw]]` values, and resolves each source with
  `resolve_container_mounts()`. `--here` is placed first at
  `/home/dev/workspace`; explicit targets are assigned before derived targets,
  which use `/home/dev/mount/<basename>` with parent and numeric collision
  fallbacks. The command rejects source errors and mount collisions before
  calling `compose_run()`, then prints one source-to-target mode line per mount
  in the `Environment`/`Container` output. With `--detach` it calls
  `compose_up_detached()` instead, skips `cleanup_docker_proxy()` (the container
  outlives the process, so its proxy has to stay up), and refuses up front when a
  Djinn container is already running, because `up` collides with the fixed
  `container_name`.
- Background-start guard: `compose_run()` refuses an interactive start from a
  background process group. `docker compose run` allocates a TTY and calls
  `tcsetattr()` on it; from the background that raises SIGTTOU unconditionally
  (the `tostop` flag gates background *writes*, not attribute changes), Compose
  forwards the signal into the container, and container PID 1 installs no SIGTTOU
  handler. `djinn start ... &` is exactly that shape and produces a signal storm
  of tens of events per second, which also floods Docker's event ring buffer.
  `is_background_process_group()` compares `os.tcgetpgrp(stdin)` against
  `os.getpgrp()` and returns False when stdin is not a TTY or there is no
  controlling terminal, so `< /dev/null`, pipes, and `setsid` stay allowed.
  Headless runs pass `-T`, allocate no TTY, and are never blocked.
- `status()`: reports config, containers, known volumes, config-root paths,
  networks, Docker proxy, and MCP Gateway status.
- `clean_default()`: `djinn clean` stops and removes containers with
  `compose_down(config=None)`, using best-effort placeholders. `compose_down`
  passes `--remove-orphans`, without which Compose skips the one-off containers
  that `start` and `run` create and also leaves a proxy from `--docker` behind.
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
- `preflight(config)`: fast critical path used before `build` and `start`. It
  provisions the host bind-mount sources unless the caller passes
  `provision_host=False`, which `start` does

Host bind-mount provisioning (`ensure_host_env`) is reached through two entry
paths. `init`, `doctor --fix`, and the `build` preflight call it directly.
`start`, `run`, and container-mode `session` reach it through
`prepare_config_workflow(require_compose_host_env=True)`, which provisions after
the image-compatibility check and before Compose runs. `start` therefore skips
only the preflight provisioning, not provisioning as such.

`run_checks(config, config_error)` reports Docker installation, daemon reach,
socket permission, Compose v2, configuration, projects directory, config root,
image, network, optional Docker MCP plugin, D-Bus session availability, and seed
config presence. It also includes the read-only `Config workflow` audit, which
is `PASS` when clean and `WARN` when drift or validation needs attention.

`doctor --fix` calls `_doctor_fix(config)`, which attempts:

- `seed_config(project_root)`
- `ensure_host_env(config)`
- `ensure_network()`

It exits non-zero when hard checks or repairs fail.

`preflight(config)` first verifies Docker is installed and the daemon is
reachable. Only after Docker is usable does it provision host directories with
`ensure_host_env(config)`. It does not call `seed_config()`; this keeps
Docker-down failures from creating unrelated host artifacts and preserves the
workflow seeding boundary.

## Agent Commands

`commands/agent.py` implements headless one-shot agent runs.

`build_agent_command(agent_config, write, json_output, model)` assembles a shell
command string from `AgentConfig`. It appends the prompt template, which expands
`$AGENT_PROMPT` inside the container. An explicit `model` takes precedence;
otherwise the command uses `AgentConfig.default_model` when configured.

`run()` loads app config and agent config, validates the requested agent, runs
the shared workflow preparation for Claude/Codex/OpenCode, accepts repeatable
`--mount SRC[:DST[:ro|rw]]` values, and ensures the Docker network. Without an
explicit mount it keeps the implicit current-directory mount at
`/home/dev/workspace`; with explicit mounts it uses their resolved targets and
the first target as the workdir. It then calls
`compose_run(..., interactive=False, env={"AGENT_PROMPT": prompt})` and reports
the complete resolved mount collection before execution.

`agents()` lists configured agents, with verbose and JSON modes.

## Session Workspace Contract

`commands/session.py` exposes `djinn session`.

`--model` is optional. Interactive and headless sessions use the selected
agent's `default_model` when the caller omits it, so a non-Claude agent never
receives the former session-wide `sonnet` fallback.

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

If no container is running, `SessionManager.preflight_check()` resolves the
selected agent definition and requires that agent's binary on host `PATH`.
Claude, Codex, and OpenCode host sessions first receive their selected canonical
workflow view. Host-mode interactive and headless commands then run directly in
the host workspace. Container-mode OpenCode sessions refresh the live runtime
through the shared publisher before the agent starts. Its image compatibility
check inspects the running container image, not the current image tag.

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
- `SYNC_PATHS["credentials"]`: `claude`, `gemini`, `codex`, `opencode`, `gh`, `age`
- `SYNC_PATHS["repo-dotfiles"]`: `repo-dotfiles`

`backup()` refuses to run while Djinn containers are active. It stages one
archive per selected named volume or config-root subdirectory, then encrypts the
outer tar with `age --passphrase` into a `0600` temporary file in
`~/.djinn/backups/` and atomically publishes
`djinn-backup-YYYY-MM-DD.tar.gz.age`. The backup directory is actively set to
`0700`; the default flow keeps only the newest archive across encrypted and
legacy cleartext formats. `--no-encrypt` is the explicit cleartext opt-out and
uses the same atomic publication path.

`restore()` also refuses to run while containers are active. It identifies an
age archive from its `age-encryption.org/v1` header, decrypts it into a separate
restore-staging subdirectory, then extracts the outer tar. Legacy cleartext gzip
archives remain supported. It restores config-root path archives by filename
prefix and named volumes by validated volume name.

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

The dev container receives `MCP_GATEWAY_URL` pointing at the gateway over the
Docker network.

## Data Flow Diagrams

First run:

```text
djinn init
  |
  +-- prompt for code_dir and timezone
  +-- optionally prompt for resources and shell mounts
  +-- save ~/.config/djinn_in_a_box/config.toml atomically
  +-- seed_config(project_root)
  +-- ensure_host_env(config)
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
  +-- Seed & Config: merge Claude settings and publish the OpenCode workflow
      from the read-only canonical mount
  +-- MCP: register MCP servers and box third-party CLI output
  +-- Tools: install optional tools
  +-- Security: summarize firewall, Docker access, and MCP gateway state
  +-- run interactive shell as a background job
  +-- reverse-sync selected settings on shell exit or on SIGTERM
```

Backup:

```text
djinn backup
  |
  +-- refuse if containers are running
  +-- collect selected named volumes
  +-- collect existing config-root paths
  +-- stage per-item tar.gz files
  +-- encrypt and validate the outer tar in a same-directory temp file
  +-- atomically publish ~/.djinn/backups/djinn-backup-YYYY-MM-DD.tar.gz.age
  +-- rotate older encrypted and legacy cleartext archives
```

Session:

```text
djinn session --project name [--create]
  |
  +-- validate project name
  +-- contain workspace under ~/.djinn/sessions
  +-- require or create workspace
  +-- prefer docker exec into running djinn container
  +-- otherwise require the selected agent binary on host PATH
  +-- publish selected Claude/Codex/OpenCode host workflow when in host mode
  +-- refresh running-container OpenCode with the shared publisher before invocation
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
- closed workflow ownership, adapter directions, manifest safety, and read-only
  config-workflow audit output
- shared publisher locking, stable snapshots, crash recovery, carrier
  preservation, canonical/runtime manifest adoption, and standalone CLI use
- deterministic projection across the 3×2 adapter matrix, non-portable
  fail-closed behavior, runtime publication, image compatibility, and shared
  start/run/session preparation
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
