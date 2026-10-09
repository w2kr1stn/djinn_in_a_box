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
├── docker-compose.agent-docker.yml
├── docker-compose.docker-direct.yml
├── docs/
│   ├── design/
│   │   └── CLI_DESIGN_SYSTEM.md
│   ├── headless-cheatsheet.md
│   ├── suite-integration.md
│   └── sync-across-machines.md
├── src/djinn_in_a_box/
│   ├── cli/
│   │   └── djinn.py
│   ├── commands/
│   │   ├── agent.py
│   │   ├── backup.py
│   │   ├── config.py
│   │   ├── container.py
│   │   ├── doctor.py
│   │   └── session.py
│   ├── config/
│   │   ├── defaults.py
│   │   ├── loader.py
│   │   ├── models.py
│   │   └── zones.py
│   └── core/
│       ├── __init__.py
│       ├── banner.py
│       ├── agent_runner.py
│       ├── agent_versions.py
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
│   ├── opencode-credentials.sh
│   ├── init-firewall.sh
│   ├── check-build-dns.sh
│   └── update-agents.sh      # read-only discovery or maintainer default bumps
├── tools/
│   ├── install.sh
│   ├── installers/
│   └── tools.txt.example
├── templates/
│   └── seed/
│       ├── config/
│       ├── packages.txt
│       └── tools.txt
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
  +-- commands/container.py  build, start, status, clean, update, enter
  +-- commands/assistant.py  interactive installation audit
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
  +-- core/assistant.py      standalone assistant image and temporary session
  +-- core/agent_versions.py numeric version policy and atomic host record
  +-- core/seeding.py        host-side first-run seed repair/copy
  +-- core/session.py        docker exec and host-mode session runner
  |
  v
Docker Compose + container entrypoint
```

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
- The build path is the one exception to captured output: it inherits the child's
  streams (see "Image Build"), so `print_captured()` does not apply there — the
  output never passes through Rich.
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

- `code_dir: Path`: required host root for the selected workspace mode
- `workspace: Literal["aios", "projects"]`: default `projects`; mounts the root
  at `/home/dev/aios` or `/home/dev/projects`, respectively
- `timezone: str`: IANA timezone, default `UTC`
- `config_root: Path`: local credential/config bind-mount root, default
  `~/.djinn/config`
- `shared_root: Path | None`: optional mirrorable, non-backed-up zone root;
  defaults to `<config_root>.shared`
- `local_root: Path | None`: optional host-local, non-backed-up zone root;
  defaults to `<config_root>.local`
- `resources: ResourceLimits`
- `shell: ShellConfig`
- `config_sync: ConfigSyncConfig`
- `mounts: dict[str, BindDeclaration | VolumeDeclaration]`: empty by default
- `environment: dict[str, str]`: empty by default, literal dev-container values
- `build: BuildConfig`: `network: BuildNetwork` (`default` | `host`, default
  `default`) — reaches compose as `DJINN_BUILD_NETWORK`, which
  `docker-compose.yml` interpolates into `build.network`. `host` uses the
  host's resolver path. Buildkit's third mode,
  `none`, is not offered: no layer of this image builds without a network. A named
  network buildkit rejects itself.

`AppConfig.workspace_target` derives that fixed container path as a plain
property, excluded from JSON and TOML. Djinn does not infer the mode from the
host path or validate AIOS subdirectories.

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
Declarations remain root `[mounts.<name>]` and `[environment]` tables; they are
never nested under `[general]`. For example:

```toml
[mounts.archive]
source = "/mnt/archive"
target = "/home/dev/archive"
marker = ".drive-ready"
read_only = true

[mounts.journal]
volume = true
target = "/home/dev/journal"
backup = "data" # also cache or none

[environment]
CDP_HOST = "192.0.2.1"
CDP_PORT = "9222"
EXAMPLE_LITERAL = "${HOST_VALUE}"
```

`config/declarations.py` owns frozen, strict, extra-forbid declaration models.
Only literal `volume = true` selects `VolumeDeclaration`, with required absolute
`target` and `backup = "data" | "cache" | "none"`; binds require absolute
`source`/`target` and optionally a marker filename and strict boolean `read_only`
(default `false`). A non-boolean yields `invalid read_only: value must be a boolean`;
volumes refuse the field. Mixed shapes, unsupported
fields, NUL in any declared string, invalid names/keys and non-string environment
values fail model validation. Names match `[a-z0-9][a-z0-9_.-]*` and keys match
`[A-Za-z_][A-Za-z0-9_]*`. Quoted dotted table components serialize as one name;
duplicate TOML tables/keys are parse errors. Model loading performs no bind host
inspection. Directory binds default to writable and support `read_only = true`;
file binds are outside this format.

`load_config()` flattens
`[general]` into the `AppConfig` constructor and raises:

- `ConfigNotFoundError` when the file is absent
- `ConfigValidationError` for invalid TOML or Pydantic validation failures

`save_config()` serializes back to nested TOML and writes atomically with
`tempfile.mkstemp()` plus `os.replace()`.
The `workspace` key is saved under `[general]` beside `code_dir`.
Saving preserves effective declaration values, omits an absent marker and false
`read_only` from bind tables only, and discards TOML
comments. Parseable schema failures attach per-entry diagnostics to
`ConfigValidationError` for doctor; no partially valid `AppConfig` is returned.

Agent definitions are loaded by `load_agents()` with this priority:

1. explicit file path, if supplied
2. `~/.config/djinn_in_a_box/agents.toml`
3. `DEFAULT_AGENTS` from `config/defaults.py`

The shipped defaults cover `claude`, `codex`, and `opencode`.

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
for workspace mode (`aios` or `projects`, default `projects`), then the host root
with its mode-specific container path and timezone. Suggested roots are `~/aios`
or `~/projects`. It then uses progressive disclosure for
advanced resource and shell settings. The simple path accepts suggested
resources from `core/hostinfo.py`; advanced prompts allow explicit CPU, memory,
and shell-mount choices.
It asks no declaration questions; `init --force` replaces the whole file,
including declarations. Declaration maintenance uses `config edit`, whose
post-editor checks cover syntax/model validation. There is no new CLI writer or
migration: existing Compose edits and data transfers are handled by hand.
Every scalar `config set` preserves mounts/environment through `_build_config`,
but loses comments through serialization. Text and JSON `config show` include both.

`core/hostinfo.detect_timezone()` reads `/etc/localtime` when it is an IANA
timezone symlink and falls back to `UTC`. `suggest_resources()` reads
`/proc/meminfo`, uses half the host memory and half the CPU count, clamps CPU to
the `ResourceLimits` bounds, and falls back to model defaults on probe failure.

`ALLOWED_CONFIG_KEYS` controls `djinn config set`:

- `general.code_dir`
- `general.workspace`
- `general.timezone`
- `general.config_root`
- `resources.cpu_limit`
- `resources.memory_limit`
- `resources.cpu_reservation`
- `resources.memory_reservation`
- `shell.skip_mounts`
- `shell.omp_theme_path`
- `config_sync.source`

`general.workspace` accepts only exact lowercase values. `_build_config()`
validates the rebuilt frozen model and carries workspace through unrelated
updates. Human and JSON `config show` both include the mode.

`config_edit()` runs `$EDITOR` or `vi`, then reloads and validates the file.
Changes that may select a different workflow source coordinate through the
exclusive lock on the existing `config/` directory.

## Global Workflow Ownership and Audit

The workflow source is deployment-wide, including the shared demo deployment.
The implementation has no per-tenant source selector. Canonical native roots
remain under the ignored project-local `config/{claude,codex,opencode}` tree.
Only the selected tool's `AGENTS.md` is authoritative:

| Category          | Claude Code                                         | Codex                                            | OpenCode                             |
| ----------------- | --------------------------------------------------- | ------------------------------------------------ | ------------------------------------ |
| Root instructions | `AGENTS.md`                                         | `AGENTS.md`                                      | `AGENTS.md`                          |
| Agents            | `agents/*.md`                                       | `agents/*.toml`                                  | `agents/*.md`                        |
| Skills            | `skills/<name>/**`                                  | `skills/<name>/**`                               | `skills/<name>/**`                   |
| Commands          | `commands/*.md`                                     | `skills/command-<name>/**`                       | `commands/*.md`                      |
| Support           | `context/**`, `scripts/**`                          | `context/**`, `scripts/**`                       | `context/**`, `scripts/**`           |
| Native-only hooks | three Python scripts plus `settings.json` fragments | three Python scripts plus `hooks.json` fragments | three named plugin files             |

The known hook fragments are `SessionStart`, `PreToolUse`, and `Stop`; Codex
also owns the `project_doc_fallback_filenames` bridge in `config.toml`.
Claude Code reads global instructions only from `~/.claude/CLAUDE.md`, so Djinn
mounts the shipped `templates/claude/CLAUDE.md` read-only in the container and
publishes it as a managed file on the host fallback; it imports `AGENTS.md`.
Hooks and their registrations are native-only, like the Claude-only `/codex-review`
command: a present native item is validated for ownership, UTF-8, and containment
(with the OpenCode export-marker check), but is never cross-tool projected or
stale-removed. Missing native hooks are allowed.
Repository-local instruction files, agents, skills, and commands are outside
this global projection and are not rewritten.

### Native-only workflow artifacts

These tables list the closed native-only set. Script paths are relative to the
selected workflow source root (`config/<tool>`); a dash means the matrix has no
carrier or event for that artifact.

#### Claude

| Name | Kind | Script path in workflow source root | Carrier | Event |
| --- | --- | --- | --- | --- |
| startup | hook | `scripts/session-start-status.py` | `settings.json` | `SessionStart` |
| security | hook | `security_reminder_hook.py` | `settings.json` | `PreToolUse` |
| ready | hook | `ready_notify_hook.py` | `settings.json` | `Stop` |
| codex-review | command | `commands/codex-review.md` | — | — |

#### Codex

| Name | Kind | Script path in workflow source root | Carrier | Event |
| --- | --- | --- | --- | --- |
| startup | hook | `scripts/session-start-status.py` | `hooks.json` | `SessionStart` |
| security | hook | `hooks/security_guard.py` | `hooks.json` | `PreToolUse` |
| ready | hook | `hooks/ready_notify.py` | `hooks.json` | `Stop` |

#### OpenCode

| Name | Kind | Script path in workflow source root | Carrier | Event |
| --- | --- | --- | --- | --- |
| startup | hook | `plugins/session-start-status.js` | — | — |
| security | hook | `plugins/security-reminder.js` | — | — |
| ready | hook | `plugins/ready-notify.js` | — | — |

In the container, Claude registers startup with
`python3 ~/.claude/scripts/session-start-status.py` and root-level hook
scripts with `python3 ~/.claude_seed/<script>`. The `scripts/` subtree
is directly mounted at `~/.claude/scripts/`. The host fallback rewrites
Claude's root-level seed commands to `python3 ~/.claude/<script>`.
Codex hook commands use
`bash -lc 'python3 "${CODEX_HOME:-$HOME/.codex}/<script>"'`; the
container mounts its Codex runtime root at `~/.codex`, which is the default
when `CODEX_HOME` is unset. Hook scripts use only the standard library and run
with plain `python3`: `uv run` would sync and build any Python project found in
the agent's working directory. OpenCode has no hook command carrier: its plugin
files are read from the mounted `~/.opencode/seed/<script>` workflow and
published to `~/.config/opencode/<script>` for runtime use.

For Claude and Codex hooks, the script and its carrier registration must
coexist. If only one is present, validation reports `hook-incomplete`, marks
the workflow invalid, and `djinn start` refuses to proceed. Restore the script
at its spec path or remove a stale registration; if the script is present but
its registration is missing, restore the registration or remove the script.
Hooks a user registers beyond this managed set are personal and may live
anywhere, including under `scripts/`; never move a managed script away from
its spec path.

Tool-owned runtime state inside a source root is not a workflow source.
`workflow_publisher.runtime_residue_prefixes()` names the per-tool subtrees
(`skills/synced/**` under a Claude source; none for Codex and OpenCode), and
`is_runtime_residue()` adds `__pycache__` unconditionally, because the
interpreter defines it as a regenerable cache. Both are skipped at every point
that reads the source — the adapter scan and `_read_file_tree`, whose result
also becomes the audit snapshot — so the bytes are never read, decoded,
fingerprinted, copied or projected. The exclusion is deliberately not a
binary filter: a `.pyc` outside `__pycache__` and a `.pyd` shipped inside a skill
are still UTF-8 failures. Excluded paths are never deleted; the writing tool owns
them, and a change to one is not drift, which is what keeps repeated audits
stable while Claude Code re-syncs its account skills through the writable
`./config/claude/skills` bind-mount.

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
settings merge keep their existing inputs. The private snapshot is written from
the bytes `snapshot_file_view()` read and fingerprinted; the live source tree is
never copied. It uses the publisher's content fingerprint both after snapshot
creation and at the commit point. A source
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

Dev can write the target and source trees, so every access below a root is
anchored to the root's locked directory descriptor and follows no symlink. Each
path component is opened with `O_DIRECTORY|O_NOFOLLOW`; a symlinked or
non-directory component is `collision`. A file is pinned with `O_PATH|O_NOFOLLOW`
and read through `/proc/self/fd` only when the pinned inode is regular, so links,
FIFOs, sockets and devices are never opened for I/O. A write creates a random
`.djinn-publisher-*` file with `O_EXCL|O_NOFOLLOW` in the verified parent and
renames it there; a stale entry is unlinked in the verified parent. Roots are
opened without following, and root identity is decided by `lstat`/`fstat`, so a
symlinked canonical or target root is refused. The source walk works the same
way and refuses a symlinked directory instead of skipping it. Config sync and the
native-only adapter reads use the same reader, `read_regular_file()`. Ancestors
of the roots are trusted; dev can still rewrite a managed file after a publish,
which the next publish reports as drift or collision.

The one manifest schema is `{source, items}`. An item is either a file path or a
carrier path plus key path and records `content_hash` and `executable`. The
canonical instance is `config/.djinn-config-sync.json`; each publisher-managed
runtime root uses `.djinn-workflow-state.json`. Neighboring JSON carrier keys
are preserved semantically. The managed top-level TOML assignment is spliced
while preserving every other byte and then re-parsed. Unknown or edited current
manifest state fails closed.

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
`${CODE_DIR}`, `${DJINN_WORKSPACE_TARGET}`, `${DJINN_CONFIG_ROOT}`, `${TZ}`, and
resource variables. Those are not the same as `docker compose run -e` container
variables. Djinn centralizes host interpolation through:

- `build_compose_env(config)` renders Compose variables from `AppConfig`
- `_compose_host_env(config)` overlays them onto `os.environ`
- `_run_compose(args, config, cwd)` is the captured `docker compose` choke-point

`DJINN_WORKSPACE_TARGET` derives from the mode and drives both the configured
workspace bind target and default cwd. Rendered `CODE_DIR` and workspace target
override stale inherited values. With `config=None`, teardown/parser calls use
the home-directory source placeholder and `/home/dev/projects` target default.

Captured Compose calls such as `compose_down()` and companion preparation route
through `_run_compose()`. `compose_build()` is no compose call — it runs
`docker buildx bake` on the compose file — but takes its env from the same
`_compose_host_env(config)`.
`build()` supplies resolved `agent_args`; bake adds sorted
`--set dev.args.<ARG>=<version>` pairs before its targets only for recorded
versions strictly above their upstream defaults. No overrides keep the existing
argv. These are build args rather than runtime environment variables.
`compose_run()` is the sanctioned interactive/headless run site; it also builds
`host_env = _compose_host_env(config)` before calling `subprocess.run()`.
When stdout or stderr is a TTY, `build_compose_env()` also renders
`DJINN_TERM_WIDTH` from `shutil.get_terminal_size().columns`; otherwise that
variable is left to inherited host environment or Compose defaults.

`ensure_host_env(config)` provisions bind-mount sources before Compose runs.
It creates the three zone roots, credential subdirectories and every assigned
local/shared overlay directory as the invoking user, with mode `0700`, plus
sessions/backups/gitconfig sources. Host `.ssh` is neither provisioned nor delivered. Existing overlay content remains
untouched. Current directory checks reject files and symlinks; private-directory
modes are secured without changing ownership. `djinn doctor` reports permission
drift and `djinn doctor --fix` repairs it.

## Host-Side Seeding

`core/seeding.py` copies neutral seed templates from `templates/seed/` into the
local root-level `config/`, `packages.txt`, and `tools/tools.txt` locations.
It also ensures empty `config/claude`, `config/codex`, and `config/opencode`
workflow roots. The source-aware `seed_config(..., source=...)` entry point only
installs the Claude baseline when Claude is selected and that root is
uninitialized.
`seed_config()` is called only by `djinn init` and `djinn doctor --fix`, before
`ensure_host_env()`. Status, config-workflow audits, sync, and workflow preflight
never seed or repair a source root.

`SEED_MANIFEST` defines every seed source, target, and kind:

- `config/claude/AGENTS.md`
- `config/claude/settings.json`
- `config/claude/skills`
- `config/claude/commands`
- `config/claude/agents`
- `config/claude/context`
- `config/claude/scripts`
- `config/opencode`
- `config/mcp-servers.json`
- `config/agents.toml.example`
- `tools.txt` copied to `tools/tools.txt`
- `packages.txt`

The seeded `AGENTS.md` instructs agents to read agent-relevant material from a
working directory's `.agents/` directory before working there. Djinn provides no
additional per-directory discovery, mounting, or synchronization.

`seed_config(project_root)` is copy-if-absent. Existing targets of the correct
type are never overwritten. Wrong-type targets are repaired by `_repair_wrong_type()`.
Dangling symlinks are treated as existing targets because `Path.exists()` would
otherwise miss them.

The publisher is the only writer for publisher-managed workflow roots.

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
- `claude_settings_merge(seed_dir, target_settings_file)`: merges the tracked
  Claude settings baseline with optional `settings.local.json`. It has a
  minimal-seed guard: if `AGENTS.md` or `settings.json` is missing, it prints a
  repair hint and skips the merge rather than writing incomplete state. The
  baseline wins for the owned `SessionStart`, `PreToolUse`, and `Stop` hook
  fragments; neighboring settings remain overlay-controlled.
- `reverse_sync_file(runtime_file, target_file, acknowledged_file, mode)`:
  checkpoints changed container state to writable seed mounts and performs the
  same sync at clean stop; `mode` is `checkpoint` or `final`.
- `reverse_sync_claude_settings(runtime_file, target_file, acknowledged_file, mode)`:
  uses the same checkpoint/final change-only rule for the personal Claude overlay,
  removing only those three managed hook fragments from the destination.

`entrypoint.sh` applies those helpers as follows:

```text
container start
  |
  +-- volume ownership repair for cache/workspace paths
  +-- source seed-lib.sh
  +-- restore ~/.claude.json from the persistent config root when present
  +-- claude_settings_merge ~/.claude_seed -> ~/.claude/settings.json
  +-- settings-copy.py persists personal OpenCode settings only
  +-- opencode-credentials.sh initializes config-root credentials and
      canonical data-volume symlinks
  +-- workflow-publisher.py publishes ~/.opencode/seed -> ~/.config/opencode
      using the read-only /home/dev/.djinn-canonical root and the runtime state manifest
  +-- source mcp-register.sh and register MCP servers
  +-- install optional cached tools
  +-- print security summary, including firewall, Docker access, and MCP state
  +-- start checkpointing changed settings every 30 s
  +-- run interactive zsh as a background job, waited on by PID 1
  +-- stop/join checkpointer and sync once on shell exit or SIGTERM/SIGINT
```

Both shutdown paths reach the reverse-sync, which matters because a detached
container (`djinn start --detach`) never exits its shell — `docker stop` sends
SIGTERM to PID 1 and that is its only shutdown. `entrypoint.sh` therefore:

- collects the reverse-sync calls in `persist_session_state()`, guarded by
  `_DJINN_STATE_PERSISTED` so the signal path and the normal path cannot both
  run it; stops and joins the checkpoint worker before the final shared sync;
- traps TERM and INT into `_djinn_on_termination_signal`, which persists
  immediately and exits `128 + signal`. It deliberately does not signal the
  shell and wait for it: an interactive zsh ignores SIGTERM, so waiting would
  burn the whole `docker stop` grace period and end in SIGKILL having persisted
  nothing. The agent CLIs write settings as they change, not on exit, so there
  is nothing to flush first;
- runs the shell as a background job and `wait`s on it. As a foreground command
  it would defer every trap until it returned, which under `docker stop` never
  happens — the traps would be dead code. Backgrounding costs the shell its
  stdin, and that is the subtle part: with job control off, a background job's
  fd 0 is reassigned to `/dev/null` *before* any explicit redirection is applied.
  zsh would then not be interactive at all — it reads EOF and exits within
  milliseconds, taking the container with it. The entrypoint therefore duplicates
  fd 0 first (`exec 3<&0`) and hands it back explicitly (`<&3`, plus `3<&-` to
  keep the spare descriptor out of the child). `<&0` cannot do this: by the time
  it is evaluated, fd 0 is already `/dev/null`. With stdin restored, the shell
  claims its own process group and the terminal as usual, and interactive
  behaviour (job control, Ctrl+C) is unchanged.

  The container passes no arguments (`ENTRYPOINT` with no `CMD`, no compose
  `command:`), so `tests/test_entrypoint_shutdown.py` covers that exact shape on
  a real pty: a shell launched with `-c <cmd>` never needs a terminal and would
  hide the regression entirely.

- refuses to start an interactive shell in two cases — no terminal at all, or
  `DJINN_DETACHED=true` — and keeps the container alive with a `sleep infinity`
  keeper instead. This closes a whole class rather than one instance: an
  interactive zsh without a TTY reads EOF and returns immediately,
  so PID 1 exits 0 and the container disappears with an empty log — the very
  signature this work started from. The class has several entrances, and the
  background-start guard deliberately does not cover them all: it refuses only
  the shapes that can actually storm (stdout on a background terminal), leaving
  the genuinely TTY-less ones to be handled here. Since nobody can
  use PID 1's shell without a terminal anyway, while `djinn enter` brings its own
  TTY through `docker exec`, staying up is strictly better than dying silently.
  The reverse-sync on `docker stop` works unchanged in that state.

  The detached case needs the flag because it is invisible from inside: the
  compose file sets `tty: true`, so a terminal *does* exist under `up -d` — there
  is simply nobody on it. An unused interactive shell as PID 1 makes the session
  hostage to that terminal, since any EOF on it (a stray attach, a closed pty
  master, a Ctrl-D) ends the shell with 0 and takes the container down with no
  signal and no error to point at. `compose_up_detached()` therefore exports
  `DJINN_DETACHED=true` through the same compose interpolation `ENABLE_FIREWALL`
  uses (`${DJINN_DETACHED:-false}` in `docker-compose.yml`), leaving the generated
  override purely about mounts. Every other path sees `false`.

Shell-side startup output is sectioned through `scripts/output-lib.sh`:
`Seed & Config`, `MCP`, `Tools`, and `Security`. `mcp-register.sh` captures
third-party CLI output from MCP add/remove commands and passes non-empty output
through `ui_boxed`, so external tool chatter stays visibly nested under the MCP
section while remaining on stderr.

For Claude, `docker-compose.yml` mounts selected directories and files from
root-level `config/claude` directly into the live `~/.claude` tree, including
`AGENTS.md` as the root instruction file. Only settings are
merged. This Compose-Claude runtime is manifestless: the publisher never writes
to `${DJINN_CONFIG_ROOT}/claude`. In-session settings changes are reverse-synced
to `config/claude/settings.local.json`, not to the tracked baseline template.

The entrypoint captures initial runtime references in a private mode-0700
directory, then checkpoints Claude state, filtered Claude personal settings and
OpenCode personal settings every fixed 30 s. Changes older than about 30 s
survive a crash, qualified by checkpoint duration, scheduling, valid JSON and
writable healthy storage. Each carrier is captured once and compared with its
last acknowledged runtime bytes first; unchanged content skips validation and
writing. Changed content is validated as exactly one JSON document, copied
atomically through `settings-copy.py`, and acknowledged only after file fsync,
replacement and directory fsync succeed. Destination files have mode 0600.
Claude acknowledgement retains raw captured bytes; only the destination has the
three managed hook keys removed. Unchanged runtime content leaves host edits
alone, including a host-only `settings.local.json` edit at clean stop; when both
changed, runtime wins. Checkpoint failures warn once per carrier per session and
retry; changed invalid input is silent until final sync. Claude settings must
be an object accepted by the managed-hook filter; filtered-output failures warn
as storage failures. Comparison read errors preserve destinations and references
and retry. Clean stop joins any in-flight write, runs the same sync, and keeps
shell/TERM/INT exit codes. Failed initial capture or state creation disables
checkpointing with one warning. Final sync
recreates missing private state once; failure warns per existing carrier and
writes nothing. Without references, every valid carrier is written, so a
host-only overlay edit can be overwritten in that degraded path.

Limits: JSON validation cannot detect coherent-looking mixed bytes from an
in-place writer; carriers are independent snapshots. Invalid input, storage
failures or blocked I/O extend the bound and join time. Real overlay changes
still participate in workflow source audits. Fsync durability depends on the
filesystem and mirroring; hosts remain last-writer-wins. A second termination
signal during finalization can act like a crash. Copier `.djinn-settings-*`
residue is inert but can accumulate: doctor reports zone drift in config-root
`claude/` and `opencode/`, while residue beside the `config/claude/` overlay is
unreported. Startup reads exact filenames and delivery never projects residue.

`core/config_workflow.prepare_config_workflow()` is the common preparation path
for `djinn start`, `djinn run`, and `djinn session`: it verifies image
compatibility for Compose paths, provisions only required runtime roots, audits,
auto-repairs deterministic `source-changed` drift, and publishes only explicit
runtime targets. It never seeds. `target-drift`, `collision`, and
`invalid-or-semantic` stop the command before agent or Compose invocation. Host
fallback publishes the selected confirmed Claude/Codex/OpenCode view to its native
host root. A running-container OpenCode session invokes the copied publisher with the
same canonical-root, target, state-manifest, and profile arguments as the
entrypoint.

### Host workflow confirmation

Preparation gates every delivery target whose destination differs from
`get_config_root(config) / target.tool`. The equal config-root destination is
container delivery and keeps its existing provisioning and Compose-Claude skip.
A config-root tool directory that overlaps a native host root is therefore
outside this protection: the container can write that root directly. Sealing
and symlink-following publisher writes are unchanged; the latter belongs to
[#133](https://github.com/w2kr1stn/djinn_in_a_box/issues/133).

For gated targets, the destination is inspected with `lstat` before approval,
without creating a missing root or parent. Under a shared canonical lease, the
delivery view is loaded and Claude's host hook rewrite and bridge are applied.
The reviewed `frozenset[ManifestItem]` contains each relative file path, SHA-256
content hash and executable flag, plus each fragment's carrier path, key path
and SHA-256 hash of its canonical JSON value. It covers the final managed
payload; neighbouring host-owned settings and manifest-driven removals retain
the publisher's existing semantics. Personal `settings.local.json` content is
excluded and does not cause re-confirmation.

The host-only record at
`~/.config/djinn_in_a_box/host-workflow-trust.json` maps absolute destination
roots to lists of items with exactly `path` (relative POSIX string), `key_path`
(null for files, a nonempty array of nonempty strings for fragments),
`content_hash` (64 lowercase hexadecimal characters), and `executable`
(boolean, always false for fragments). Strict JSON decoding rejects duplicate
keys. Non-absolute or empty roots, wrong field types, parent-traversing or
absolute item paths, invalid hashes and duplicate `(path, key_path)` identities
invalidate the entire record. Missing, unreadable or malformed records mean
unconfirmed; an empty item list is a confirmed empty set. The existing runtime
manifest records publication and is never interpreted as user approval.

An identical confirmed set publishes under the current lease without rewriting
the record. Otherwise the lease is released and a frozen `HostWorkflowReview`
lists the complete proposed set, sorted new/changed/removed labels, selected
source directory, any distinct target-native directory and the actual Claude
bridge template. The session command renders this on stderr and uses
`typer.confirm(default=False, err=True)` only when stdin is a TTY; a missing
callback or refusal stops before any host-root writes and before launch.

After approval, preparation re-acquires a shared lease and reloads the payload
without auto-repair. An unloadable or different set, or the publisher's
pre-write `SOURCE_CHANGED`, produces `host-workflow-changed` and asks for a
retry. Canonical-lock and host publisher lock/drift/collision/write errors
retain their existing mappings. An abort before the first write (changed or
unloadable set, `SOURCE_CHANGED`, lock, drift or collision) can leave the
destination directory prepared for the attempt, empty; a write error after the
first write can leave approved files without a manifest or trust entry. Djinn
does not remove directories, because a path-based cleanup cannot prove which
ones it created. Publication writes the immutable approved bytes. Only after
successful publication and lease release does Djinn re-read
the trust record, preserve other valid roots, and atomically replace it using
a same-parent temporary file, mode 0600, and fsync; a new record parent uses
0700. Every record operation derives from the call-time
`HOST_WORKFLOW_TRUST_FILE`. Save failure reports the path/cause and stops launch.
Review output disables Rich markup and emoji codes and escapes non-printable
characters with `unicode_escape`; a directory lock serializes each trust-record
load/update/atomic-replace transaction.
The first host fallback asks once per root, and any subsequent
container change to published content requires review again. See the
[security model](SECURITY-MODEL.md#direct-routes-and-target-authority) for external
hook references, widened mounts and tools with host authority.

## Docker Compose Runtime

`docker-compose.yml` defines a stable project name and one service:

- `dev`: normal development container on `djinn-network`

There is no separate authentication service. Every bundled CLI signs in from
inside a normal `dev` session: the tool prints a URL, the user opens it in the
host browser and pastes the returned code back into the container. No loopback
callback is *needed*, so the container requires neither host networking nor a
published port.

Selecting that flow is not uniform. Claude Code and OpenCode prompt for a pasted
code by default. Codex is the exception: plain `codex login` starts a
container-local login server that the host browser cannot reach, so users must
run `codex login --device-auth` (or choose the remote/headless option in its
TUI). README documents this.

Claude Code, Codex, GitHub CLI, and OpenCode persist credentials in config-root
bind mounts. OpenCode `auth.json` and `mcp-auth.json` live under `~/.opencode`.
Startup initializes missing files with mode `0600` and ensures the paths under
`~/.local/share/opencode` are canonical symlinks to those files. Unexpected
credential redirects are refused.

Common mounts include:

- `${DJINN_CONFIG_ROOT}/claude` to `/home/dev/.claude`
- `${DJINN_CONFIG_ROOT}/codex` to `/home/dev/.codex`
- `${DJINN_CONFIG_ROOT}/opencode` to `/home/dev/.opencode`
- `${DJINN_CONFIG_ROOT}/gh` to `/home/dev/.config/gh`
- `${DJINN_CONFIG_ROOT}/age` to `/home/dev/.config/age`
- named volumes for caches, OpenCode data, VS Code server state, and workspace
  metadata
- read-only host `~/.gitconfig`
- generated public SSH files at `/home/dev/.ssh` and the filtered Git agent export
  directory at `/run/djinn-git-agent`, both read-only
- the writable `config/claude` seed mount plus nested direct mounts for its
  workflow files, including `AGENTS.md`
- the read-only `templates/claude/CLAUDE.md` bridge at `/home/dev/.claude/CLAUDE.md`
- the read-only canonical `./config` mount at `/home/dev/.djinn-canonical` for
  the shared publisher
- `${CODE_DIR}` to `${DJINN_WORKSPACE_TARGET:-/home/dev/projects}`: one workspace
  root at `/home/dev/projects` or `/home/dev/aios`; AIOS's `projects/` is then at
  `/home/dev/aios/projects`
- `${HOME}/.djinn/sessions` to `/home/dev/sessions`

The base Compose environment sets `TZ`, `NO_COLOR`, `DJINN_TERM_WIDTH`,
`UV_LINK_MODE=copy`, and `LOCAL_ENDPOINT`.
`NO_COLOR` and `DJINN_TERM_WIDTH` propagate the host's plain-output and terminal
width decisions into the container shell UI. Resource limits use the Compose
variables rendered by `build_compose_env()`.

`docker-compose.agent-docker.yml` defines the pinned UID-1000 rootless companion
profile and healthcheck (rootless, overlay2, expected data root). `core/agent_docker.py`
contains profile constants, pure workspace delivery data and the side-effect-free
endpoint verifier. The shared dev
creators build that delivery once from host-resolved code, CLI, declaration and session
mounts; only those mounts go to the companion. Preparation creates a generation-owned
local tmpfs endpoint volume (UID/GID 1000, mode 0700) and a persistent cache volume,
resolves Compose, inspects the exact image/profile/limits/mounts and waits for health.
Dev gets the endpoint read-only and managed Unix `DOCKER_HOST`; selector overrides
are rejected. Buildx in the dev image is checksum-verified at build time.

With the firewall enabled, the fixed launcher waits for a marker written by an
exact-ID-owned, short-lived initializer from the inspected dev image. The initializer
joins the companion's outer network namespace with user 0 and `NET_ADMIN`, mounting
only the endpoint. It runs the existing firewall script; the launcher unlinks the
marker before the official entrypoint executes explicit Unix-only dockerd arguments.
No firewall means no marker gate or initializer.

The existing host observer owns the daemon even without Git identities. It stops
the companion when dev stops, revalidates and restores firewall rules on same-generation
resume, and fails closed if resume cannot qualify. Reclaim/clean uses generation labels
and exact IDs, preserves unknown resources, removes the endpoint after its consumers,
and keeps the cache. Docker's inner restart policies apply on the next daemon start.
Published inner ports use the companion's `agent-docker` network alias.

`docker-compose.docker-direct.yml` mounts `/var/run/docker.sock` directly and
sets `DOCKER_DIRECT=true`; the entrypoint adjusts socket permissions.
See [host authority and Docker access](SECURITY-MODEL.md#host-authority-and-docker-access)
for the analysis of these delivery paths.

Git delivery is owned by `config/ssh.py`, `core/ssh_delivery.py`,
`core/git_agent.py` and `core/host_runtime.py`. The frozen Git declarations specify
literal SSH aliases, hostnames, users and host private/public paths. Public files
are validated; conflicting original basenames and reserved filenames refuse.
`ssh-keygen -F` extracts host trust by real hostname; hashed selectors and markers
survive. No keyscan or implicit host SSH config is used. Exact alias blocks select
`IdentityAgent`, an original-name public `IdentityFile` and `IdentitiesOnly yes`.

Both Compose creators acquire the same runtime before launch and append its one
internal mount/environment fragment to declared delivery. Only `public/` and
`export/` are bound, never `private/` or the runtime parent. The generated directory
inode stays stable; files are replaced atomically. Generated targets and children,
including `/var/run` aliases, and the emitted `SSH_AUTH_SOCK`/`DJINN_GIT_MANIFEST`
variables are reserved against caller/declaration overrides.

The host starts an empty `ssh-agent -D`, loads deduplicated explicit key paths with
one `ssh-add` call on the host terminal; `ssh-add` tries the last entered
passphrase on each following key, so keys that share a passphrase prompt once.
Off a terminal, `ssh-add` runs in its own session with stdin from `/dev/null`, so
piped input cannot unlock a key and encrypted keys refuse immediately.
The host verifies the complete public blob set. The exported protocol filter
permits list/sign only for that set; it rejects
add/remove, lock, provider and extension requests. Host UID 1000 matches the image;
runtime roots are owner-only (0700), sockets 0600. There is no ambient-agent import
or init service.

A detached observer starts before loading, detects creator death during startup,
then observes Docker's actual dev ID with a per-creation label. An inherited flock
serializes pending creators and is released after ID handoff. The observer bounds
key loading at 120 seconds and container discovery at 60 seconds; the single
`ssh-add` call itself is bounded at 120 seconds on a terminal and 10 seconds
otherwise. CLI exit after detached handoff keeps the agent; foreground
completion, timeout, interruption, failure, external stop/removal/replacement or
inspection failure releases it. Observation failures
are recorded in the host-only `observer.log`; `doctor` reports unavailable state.

The image packages `scripts/git-config.py`; the entrypoint reads the public-only
manifest through `DJINN_GIT_MANIFEST`. Git's config writer generates
`~/.gitconfig_local`: `gpg.format=ssh`, optional explicitly selected
`user.signingkey` and copied `gpg.ssh.allowedSignersFile`. Global-ignore generation
is independent of signing selection. Without a signing default it preserves the
host/repository choice by omitting `user.signingkey`; `commit.gpgsign` stays user-owned.
See [Git identities and SSH signing](README.md#git-identities-and-ssh-signing) for
manual declarations, `includeIf` wiring and repository changes. Doctor inspects
configuration with origins and bounded repository discovery, and parses SSH
Includes without executing `Match exec`; it never loads keys or repairs Git/SSH config.
See [Git keys and browser identity](SECURITY-MODEL.md#git-keys-and-browser-identity)
for the implications of signing access.

`core/desktop.py` discovers only standard runtime Unix sockets, renders helper
delivery, and exposes `inspect_desktop_endpoints()` for doctor and the shared
sealing assessment. `core/docker.py` executes bounded helper preparation and a
downstream probe from the dev image. Only healthy, authenticated endpoints receive
read-only volume mounts at `/run/djinn/dbus` or `/run/djinn/audio` and their paired
environment. Failed channels warn and remain absent, with no raw fallback.
See [desktop boundaries](SECURITY-MODEL.md#desktop-boundaries) for policy and limits.

`core/host_runtime.py` always takes the canonical creation guard, including without
Git identities. Generation labels, actual container IDs and persisted ownership
coordinate creation, clean, and the detached observer's helper stop/start/remove
transitions. Docker's absolute executable is resolved once and passed to the
isolated observer. Empty Git identities still create no agent, keys or SSH mounts.
Unknown Docker state and replacement generations authorize no resource cleanup.

`core/docker.py` also auto-detects optional shell mounts:

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
  `dev` invocation, including Compose, image-alias, runtime, Direct-socket,
  zone-overlay, and user mounts. Equal targets and user targets that are
  ancestors of an occupied target raise `MountCollisionError`; child targets
  remain valid except at or below the shared `MANAGED_TARGET_ROOTS`. Assigned
  zone targets are reserved like other occupied targets. The managed-root set
  includes the five recursively repaired roots, managed SSH targets, desktop
  directories and the agent-Docker endpoint.
  Equality and descendants (including `/var/run` aliases) are refused for both
  read-only and read-write mounts before occupied-target checks, with the
  `conflicts with Djinn-managed path <root> (conflict path: <root>)` message.
  Targets are compared after lexical normalization and the fixed image aliases;
  symlinks inside mounted content, such as a workspace link into a managed root,
  are not resolved.
  The typed static Compose target table excludes the workspace root;
  `_reserved_mount_targets()` adds only the active `config.workspace_target`.
  The unused workspace root remains available for explicit user mounts.
- `MountSpecificationError` reports invalid mount grammar or reserved targets;
  `MountCollisionError` reports the two involved mounts and the conflict path;
  managed-root conflicts identify the Djinn-managed root.

When a mount exists, `compose_run()` uses the first mount target as
`--workdir`. With no mount it omits `--workdir`, so the Compose service's
`working_dir: ${DJINN_WORKSPACE_TARGET:-/home/dev/projects}` remains effective,
using the same selected target as the workspace bind. This also applies to
mount-less detached starts.

`config/zones.py` resolves additive shipped and user `zones.toml` assignments.
`ensure_host_env` creates every assigned local/shared overlay directory before
Compose runs. `compose_run` and `compose_up_detached` mount empty and populated
directories over the config-zone bind mounts. These overlays do not change the
user mount that supplies `--workdir`. Doctor reports current assignment validity,
skipped defaults, zone drift, private-directory modes and large direct files
that cannot be overlaid.

Declared mounts follow the three-layer configuration boundary: fixed built-ins
(credentials, seeds, workflow and cache targets), built-ins with a config value
(workspace, config root, SOPS key file, OMP theme), then additive declarations.
Declarations never replace, redirect, disable or remove built-ins.

`resolve_declared_entries()` is shared by `compose_run()` (interactive and
headless) and `compose_up_detached()`, before any creation subprocess or temporary
override. It resolves bind symlinks, checks that the absolute colon-free source
exists as a directory, and inspects an optional marker directly under it with
`lstat`: a regular file passes; a missing marker, directory or symlink refuses
creation with the declaration name. There is no marker content/identity check.
The resolver never creates sources or markers, repairs binds or adds declared
paths to `ensure_host_env`. Existing built-in provisioning precedes validation
and retains its behavior even on a path overlapping a declared source. Validation
can race a disappearing drive; there is no mount-liveness protocol.

Targets use the existing canonicalizer and `_reserved_mount_targets`, with the
union of all Docker modes for declarations in start/run/doctor. Equality and
declared ancestors of built-in/reserved/zone/active-workspace/invocation targets
are refused; declared pairs cannot nest. Children otherwise remain allowed,
except at or below `MANAGED_TARGET_ROOTS`, the same set used by invocation mounts.
Its five recursively repaired roots come from `MANAGED_VOLUME_REPAIR_TARGETS`,
checked against the unchanged entrypoint repair list; the remaining roots are
managed SSH targets, desktop directories and the agent-Docker endpoint. The
declared diagnostic retains its `Djinn-managed volume root` wording. Actual volume
names are `djinn-<name>` and cannot collide with the built-in volume registry.

The single reserved-environment registry covers all Compose service modes and
everything the repository ships into the image or runs at startup: Python,
scripts, tools, Dockerfile ENV/ARG/RUN and generated shell startup content.
Actual caller keys also reserve that invocation's keys. Mounted host shell
startup files and third-party tools outside the repository are outside this
boundary. Environment values are literal strings with no host override or
interpolation. Literal endpoints, including a CDP gateway, need manual edits
when the host gateway changes.

Both creators serialize the same temporary Compose fragment: long-form binds
with `bind.create_host_path: false` and `read_only: true` only for read-only binds,
volumes with actual sources and top-level
`volumes: {djinn-<name>: {name: djinn-<name>}}`, and a dev environment mapping.
Each `$` is escaped as `$$` in Compose-bound strings without mutating config.
The override is removed in `finally` on success, failure and timeout. Existing
invocation flags and working-directory selection remain independent of declarations.
Declared environment applies only to dev creation; workspace declarations also feed
the companion. Neither applies to builds or raw archive helpers.
`_prepare_workspace` forwards the resolved access mode into `WorkspaceMount` for
identical dev and companion delivery.
`session` and `enter` inherit the running container; edits affect the next
creation, with no running-mount comparison or attach-time update.

The fragment also carries `DJINN_DECLARED_VOLUME_TARGETS`, an escaped JSON array
of canonical volume targets (`[]` when empty). After its unchanged fixed repair,
the entrypoint invokes `scripts/ownership-repair.py` directly as dev with one
quoted JSON argument. The helper checks writability with dev credentials and
enumerates only direct root entries (dotfiles count). If enumeration is denied,
it uses read-only `sudo ls -A`. Only an empty, unwritable root receives
`sudo chown -h` of the root itself; no recursion or existing-content changes.
A populated, unwritable root warns with a manual ownership remedy. Malformed
transport or a failing privileged call stops startup; warnings do not.
Dockerfile delivery of this helper requires an image rebuild (`djinn build`).
Doctor reports exactly one PASS/FAIL per declared mount/environment key using
the same diagnostics; valid read-only binds say `valid declaration (read-only)`,
including retained valid binds next to invalid declarations. It never repairs
declared sources, markers or volumes.

## Hostctl helper

The hostctl helper runs separately from Compose on the shared Djinn network.
`core/hostctl.py` creates the pinned official Tailscale image with an extracted
static supervisor bound read-only as PID 1. `Dockerfile.hostctl-helper` builds
that executable with Go 1.27.1; `djinn build` builds it for the host platform after
the Compose images and installs it into owner-only host state storage. No
Tailscale daemon or Go SDK is added to dev. The
[security model](SECURITY-MODEL.md#window-state-expiry-and-logs) owns the
deployment conditions, guarantees and residual paths.

The host control flock covers on/off/limit, short dev generation transitions,
normal cleanup and the full all-clean interval. It is distinct from the existing
creator flock, which protects lengthy preparation. Enrollment runs in the
host_runtime detached observer, outside the control lock; each publication
rechecks helper identity and window generation.

PID 1 persists generation, boot ID, UTC deadline and CLOCK_BOOTTIME deadline in
the state volume and polls every 250 ms.
The exec updater communicates over a helper-private Unix socket, atomically
persists a reset deadline, and acknowledges the effective window. Host metadata
contains observations; the observer has no deadline timer.

PID 1 exposes only the gated TCP relay at :1080 to dev. Its raw SOCKS5 listener
is loopback-only at 127.0.0.1:1055; LocalAPI and control use private Unix sockets.
The observer snapshots declared peers from authenticated `Peer[*].sshHostKeys`
once after enrollment, publishes public trust, durably journals readiness and
commits admission over private IPC. PID 1 refuses replacement routes within
that generation and checks both persisted state and deadline on admission.
Status reports open only after PID 1 acknowledges admission.

The public SSH renderer composes a tailnet Include before Git aliases, including
when no Git identities exist. Opening atomically refreshes only the tailnet
files in the mounted directory. The dev image's `djinn-hostctl-connect` uses
SOCKS5 without a direct connection fallback. Each relay stream has structured
start/end records in the rotated Docker log.

`core/host_sealing.py` supplies the shared assessment for on, doctor and both
creators. It reads actual ID, mounts, environment and network peers, canonicalizes
host sources/aliases, and consumes the desktop provenance inspector. Assessment
stores the ordered sealing findings with their bind, class and item, derives the
per-item `cause_details` and `error_details` from them, and derives grouped
`causes` and `errors` for status, refusals, warnings, journal records and
`assessment.json`. `core/docker.py::inspect_agent_endpoint` collects bounded
host-only inspection of recorded resource IDs, the pinned image, managed volumes,
network and endpoint consumers. `agent_docker.verify_endpoint` consumes that
evidence and host-owned generation state; doctor and sealing use the same result.
The existing `require_agent_profile` checker compares complete image-plus-Compose
environment, startup, healthcheck, security/namespaces/resources, exact mounts and
network ID. Workspace delivery must also be a subset of dev's mounts; dev's
existing content assessment covers those paths. Only a verified companion removes
its Docker endpoint and inherited EXPOSE peer causes. The
[sealed definition and override](SECURITY-MODEL.md#sealed-deployments-and-trusted-controller)
are documented in the model. Snapshot exposes `sealing_cause_details` and
`sealing_error_details` so doctor retains one row per finding.

The direct probe runs trusted raw-socket Python in a digest-pinned throwaway
container sharing only the assessed network namespace, once for dev and once
for its verified companion. Each result includes its namespace container ID. It validates results
for every authenticated peer IPv4/IPv6 address and verifies removal after success,
timeout and cancellation. Reached or unknown results refuse admission. Doctor
uses the same probe with current/cached peers without starting a helper.
See [direct routes and target authority](SECURITY-MODEL.md#direct-routes-and-target-authority)
for the host prerequisite and diagnostic limits.

Both creators inspect resolved Compose delivery before launch. Unsealed delivery
closes and verifies the helper; sealed delivery pauses admission via private IPC.
The existing asynchronous observer inspects the actual created dev and probes
outside the control lock, then re-inspects dev and companion IDs/profile under the lock before
admitting/resuming. Planned and actual assessments carry companion generation,
network ID and profile fingerprint; opening and commit bind the same evidence.
The observer revalidates the companion throughout an open window and closes on
replacement, profile/storage/network drift or inspection uncertainty. Pause preserves immutable routes and helper-owned deadlines;
creator starts never inherit an override. External replacement/removal closes.

## Image Build

The Dockerfile refuses a build network it cannot resolve names on:
`scripts/check-build-dns.sh`, invoked *inside* the network-dependent `RUN`
instructions rather than in a layer of its own — the base `apt-get`, the optional
`packages.txt` install, and the global `npm install`. A guard in its own layer
would be cache-independent of the download it protects: it can stay cached while
the download re-runs. Sharing the instruction is what makes them share the cache
decision, which matters most for the npm layer: any effective agent ARG change
re-runs every `RUN` after the ARG block — the Claude installer and the npm layer —
while the layers above the ARG block stay cached. The
curl-based steps in between are deliberately unguarded: they fail in seconds with
their own resolver error, so the guard would add noise without adding information.
One script, so the check and its message have a single home; it uses `getent`,
which ships with glibc and therefore works on the bare base image. Without the
guard the failure surfaced one timeout at a time: measured at 70 minutes for the
npm layer, because npm retries every package six times with a backoff, and over
two minutes for `apt-get update` alone.

The build runs `docker buildx bake -f docker-compose.yml --load`, not
`docker compose build`. Bake reads the compose file itself, so it stays the one
build definition, and it receives the same host interpolation env as every
compose call. Compose is bypassed because it cannot grant an entitlement: it
drives bake internally but forwards only its own `fs.read` and
`security.insecure` grants, and since buildx 0.37.2 bake rejects an ungranted
entitlement instead of skipping the consent check. `build.network host` requests
`network.host`, so through compose that build failed before its first step.
`compose_build()` adds `--allow network.host` when the interpolated
`DJINN_BUILD_NETWORK` is `host` — the very value the compose file receives, so
request and grant cannot disagree — and grants nothing otherwise. `--load`
replaces compose's implicit `output: type=docker`, so a `docker-container`
builder also lands the image in the local store. `djinn doctor` reports a
missing buildx plugin as a warning: only the build needs it.

The build streams. `compose_build()` runs bake through `_run_streamed()`
instead of `_run_captured()`, so stdout and stderr are inherited and the log
appears while the build runs rather than after it exits. It passes
`--progress plain`, so every stage line stays on screen instead of being redrawn
in place, and the stage a stalled build last entered remains readable. Setting
`DJINN_BUILD_PROGRESS` to another bake progress mode (`auto`, `tty`, `quiet`,
`rawjson`) overrides that; an unusable value falls back to `plain` with a
warning rather than letting buildx reject the build over a typo.

The streamed `RunResult` carries no output: the log already went to the terminal,
and `commands/container.py build()` therefore points the reader upwards on
failure instead of reprinting. The exception is a spawn failure, where the
process never ran — there `stderr` holds the 126/127 diagnosis and is printed.
Streaming has no timeout: killing the buildx client would leave the BuildKit
solve in the daemon running, so a timeout here would report a cancellation it
cannot perform.

`Dockerfile` builds from `debian:bookworm-slim`. It installs base packages,
audio clients, `notify-send`, D-Bus clients, optional packages from `packages.txt`, Docker CLI,
Compose plugin, GitHub CLI, uv, a non-root `dev` user, zsh setup, Node via fnm,
and the supported coding agent CLIs.

`docker-compose.desktop.yml` defines two independent profiled helper services.
Bake explicitly selects `dev` and the shared `dbus-helper` build target; both
helpers use `djinn-desktop-helper:1`, resolved to a local image ID at preparation.
`helpers/desktop/Dockerfile` pins Debian trixie by manifest digest and asserts the
Debian proxy package is at least `0.1.6-1+deb13u3`. Image-owned policy and bootstrap
scripts drop to UID/GID 1000, clear supplementary groups and capabilities, and
run isolated Python from `/`. Helper-local health uses ordinary client credentials;
the independent dev-image probe checks downstream authentication before delivery.

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
starts with agent or direct Docker options.

Optional runtime tool installers are copied from `tools/`, with cache locations
backed by named volumes.

## Container Lifecycle Commands

`commands/container.py` implements:

- `build()`: loads config, resolves the host agent record once before
  `preflight(config)` or `_sync_build_files()`, then refreshes local build files
  and calls `compose_build(agent_args=...)`. Invalid records fail before
  provisioning, sync or Docker work, naming the record file.
- `start()`: resolves Docker mode, preflights, ensures `djinn-network`, parses
  repeatable `--mount SRC[:DST[:ro|rw]]` values, and resolves each source with
  `resolve_container_mounts()`. `--here` is placed first at
  `/home/dev/workspace`; explicit targets are assigned before derived targets,
  which use `/home/dev/mount/<basename>` with parent and numeric collision
  fallbacks. The command rejects source errors and mount collisions before
  calling `compose_run()`, then prints one source-to-target mode line per mount
  in the `Environment`/`Container` output. With `--detach` it calls
  `compose_up_detached()` instead and retains the generation observer. It refuses when a
  Djinn container is already running, because `up` collides with the fixed
  `container_name`.
- Background-start guard: `compose_run()` refuses an interactive start from a
  background process group. `docker compose run` allocates a TTY and calls
  `tcsetattr()` on it; from the background that raises SIGTTOU unconditionally
  (the `tostop` flag gates background *writes*, not attribute changes), Compose
  forwards the signals into the container. `djinn start ... &` is exactly that
  shape and produces tens of events per second. Container PID 1 is not what dies —
  namespace init discards a signal it has no handler for, and a container was
  observed surviving 45+ minutes under continuous SIGTTOU. What the storm reliably
  does is load the host and overflow Docker's event ring buffer — which is why the
  early crashes left no records at all, and why the first usable post-mortem only
  arrived once the storm was gone. Whether it also ends the container is unproven;
  the plausible path is the host-side compose client being stopped, after which
  `--rm` reaps it.
  `is_background_process_group()` compares `os.tcgetpgrp(stdout)` against
  `os.getpgrp()` — **stdout, and only stdout**, because that is what Compose keys
  on: it derives `noTty` from `!dockerCli.Out().IsTerminal()` and allocates a TTY
  only when stdout is a terminal. Consequently `djinn start < /dev/null &` is
  refused (stdout is still the terminal, so a TTY is allocated and the storm is
  possible), while `djinn start > log &` is allowed (no TTY, nothing calls
  `tcsetattr`, nothing to storm). Checking stdin or stderr as well would refuse
  that second, safe shape. `setsid` is likewise not blocked — with no controlling
  terminal the kernel raises no SIGTTOU. Headless runs pass `-T`, allocate no TTY,
  and are never blocked.

  Not blocked is not the same as supported, and the guard is deliberately not the
  only defence: any shape that ends with no TTY inside the container is handled on
  the container side instead (see *Container-Side Seed and Merge*), where the
  entrypoint keeps the container up rather than exiting. `--detach` remains the
  supported way to background a session.
- `status()`: reports config, containers, known volumes, config-root paths,
  networks, and agent Docker status.
- `clean_default()`: `djinn clean` stops and removes containers with
  `compose_down(config=None)`, using best-effort placeholders. `compose_down()`
  refuses outright when the container it would reap is the one the process runs
  in (`is_own_container()`: `/.dockerenv` plus a hostname match against
  `container_name`). Compose selects by the pinned project name, so a teardown
  from any copy of the repo — a test sandbox, an agent's scratch checkout — would
  otherwise destroy the live session, and the socket is mounted so anything in the
  container can trigger it. Teardown from the host is unaffected. Agent generations are removed by exact owned IDs after ownership checks; their
  clean path preserves foreign resources. Other modes retain Compose teardown for
  the base services, with explicit removal of the one-off dev container.
- `clean_volumes()`: lists or deletes named volume categories and clears
  config-root sync paths by category. Its cache/data flags include existing
  declared volumes of the category; credentials/repo-dotfiles keep their paths.
  `none` has no selector and is deleted only by `clean all` or actual name.
  Name cleanup retains its config-free gate accepting any existing `djinn-*`
  name, without logical-name aliases or narrowing to the registry.
- `clean_all()`: stops containers, deletes all known named volumes, clears all
  config-zone sync paths, and deletes the network. It does not clear shared or
  local zone data.
  It attempts deletion of every built-in and declared volume, including absent
  ones and `none`. Default `clean` keeps all volumes and declared binds.
- `update()`: runs `scripts/update-agents.sh --print` without loading AppConfig
  or requiring init. Captured stdin is closed; discovery runs in a new session
  with one aggregate 120-second timeout. Timeout or interruption kills the owned
  process group and reaps it. npm errors retain their stderr diagnostics.
  Exactly one numeric `ARG=x.y.z` line per agent is required before a single
  atomic save. Failure leaves the old record unchanged; success prints effective
  before/after versions and `djinn build` guidance. It writes no checkout file.
- `enter()`: opens a zsh shell in the first running Djinn container.

Status and `clean volumes` listing group declared data/cache/none volumes and
mark absent declared volumes `not created`. They load one optional config;
missing config keeps built-ins (status retains `config = None` initialization).
Invalid config aborts destructive category/all callers before cleanup.
Declared binds/markers, arbitrary binds, workspace, SSH and shared/local zones
never join destructive sets. Existing confirmations, down and self-teardown
behavior are preserved; there is no Docker prefix scan.

`_sync_build_files(config)` copies `packages.txt` and `tools.txt` from
`get_config_root(config)/repo-dotfiles` into the build context when those local
files exist. This is a build-context refresh helper, not a Compose bind-mount.
During `djinn build`, the loaded `AppConfig` is threaded through, so
`general.config_root` from `config.toml` is honored unless `DJINN_CONFIG_ROOT`
is exported in the host environment, which still takes precedence.

`core/agent_versions.py` reads the upstream Dockerfile defaults and a separate
host record at `~/.config/djinn_in_a_box/agent-versions.toml`, beside `config.toml`.
Only update writes this flat TOML record, validating the complete mapping before
creating its parent, writing sorted keys to a same-directory temporary file and
replacing the destination after closing it. Allowed keys are the three agent ARG
names; a missing file or key uses defaults. Invalid TOML, unknown keys,
non-string/non-numeric values and read errors fail with the record path.
Effective versions are the component-wise integer maximum of default and record,
preferring the default on equality. Stale records are inert; deleting the file
resets to defaults. No main-config write or migration is involved. npm's own
cache/log locations follow operator configuration (default `~/.npm`).

`scripts/update-agents.sh` owns the package map and npm discovery. `--print`
emits protocol lines on stdout and diagnostics on stderr, exits nonzero on any
failed lookup or invalid value and writes no files. No arguments retain the
maintainer Dockerfile rewrite/diff/build guidance and skip-and-continue behavior.
Upstream default bumps are reviewed through pull requests.

## Assistant Audit

`commands/assistant.py` runs `core/assistant.py` independently of the dev image,
Compose lifecycle and workspace delivery. `assistant.agent` selects Claude,
Codex or OpenCode; `--agent` overrides it without saving configuration. Invalid
TOML, zones and missing workspace paths fall back to derivable default mounts
and Claude (unless overridden), with the loader error in the initial message.

`assistant/Dockerfile` installs the selected native CLI and basic diagnostic
tools. The selected agent uses the shared effective version policy, independent
of AppConfig recovery; `DOCKER_VERSION` stays an upstream-only Dockerfile pin.
An invalid complete record aborts before image inspection/build even if the bad
entry belongs to another agent. One content label covers the effective selected
version, agent, Dockerfile/runtime bytes, host
platform/UID/GID and build network. First use or a changed label rebuilds via
buildx; a failed build aborts. Launch uses the inspected immutable image ID.

The foreground `docker run --rm -it` joins `djinn-network` as the host UID/GID
with the socket's numeric group. The project, config directory, ~/.djinn and
socket are rw binds; existing selected credential files are rw aliases under
`/run/djinn-credentials`. No workspace/declaration/extra zone roots are delivered.
The entrypoint copies credential files into fresh native homes and writes only
refreshes back at exit, accommodating CLIs that atomically replace auth files.
Claude imports only account metadata from its mixed `claude.json` carrier;
settings and session history remain ephemeral. Claude safe mode suppresses
customizations; OpenCode uses pure mode and asks for edits/bash; Codex keeps
`--sandbox danger-full-access --ask-for-approval on-request`: its sandbox needs user
namespaces the container does not grant, and 0.160 offers only `on-request`/`never`.
After interruption or client failure, cleanup verifies the unique session label
and removes only that container's inspected ID. Cleanup errors are reported.

The [compact audit guide](assistant/audit-briefing.md), live mount map and
occasion are one initial user message. Host-only operations are returned as
commands for the host terminal. Container removal does not roll back repairs.

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
image, network, actual agent Docker endpoint/identity/health/storage (with separate
stale/orphan reporting), actual D-Bus/audio delivery and raw desktop exposure, and seed
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
agent's `default_model` when it is omitted.

Both `SessionManager` run methods accept keyword-only
`env: dict[str, str] | None = None`. The private validator snapshots this input
before any agent resolution, target discovery, Git setup, or launch. It reuses
`config/declarations.py::validate_environment`, including the complete shared
`RESERVED_ENVIRONMENT` Djinn/Compose policy, and adds the session transport
restrictions documented in [suite-integration.md](docs/suite-integration.md).
These also reject Bash-managed names that the container shell changes or removes
even without startup profiles, maintaining the same accepted names in both modes.
This protection deliberately extends beyond transport names; shared-policy
changes require API compatibility consideration. Invalid types, names, values,
or protected entries raise a static, value-free `ValueError` with suppressed
exception chaining. Values require strict UTF-8 plus an unchanged strict UTF-8
round-trip of `os.fsencode`; NUL, surrogates, and lossy host encoding are rejected.

`None` and `{}` preserve defaults. Allowed values override inheritance literally,
including empty strings, without changing the input map, `os.environ`, later
sessions, or persistent files. Host agent starts merge a fresh inherited
environment, additions, and fixed terminal values; discovery, host Git setup,
and image/workflow preparation receive no additions. Container starts use
name-only `docker exec -e NAME` forwarding with values in the subprocess
environment. The Docker connection, target, prompt transport, and TTY behavior
retain their existing contracts. Session shells and their descendants inherit
the additions, subject to intentional login-profile changes. Map values stay
out of argv and Djinn-authored logs/errors; inherited credentials are not
scrubbed and child output is preserved. OS, daemon, descendant, and profile
visibility, or values independently supplied in prompts, are outside this
secrecy guarantee. No provider selection, authentication checks, or CLI flags
are added.

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
- `SYNC_PATHS["credentials"]`: `claude`, `codex`, `opencode`, `gh`, `age`
- `SYNC_PATHS["repo-dotfiles"]`: `repo-dotfiles`

`volume_categories(config=None)` copies every built-in list and adds only
`VolumeDeclaration` actual names by category, including `none`, rejecting
built-in name collisions before returning. Constants are never mutated and no
bind host checks run: an unplugged declared drive cannot block volume backup
or cleanup. `get_existing_volumes_by_category` receives the config snapshot.
Only the four existing backup selectors are accepted; `none` is storage
metadata, never a backup/cleanup category selector.

`backup()` refuses to run while Djinn containers are active. It stages one
archive per selected named volume or config-root subdirectory, then encrypts the
outer tar with `age --passphrase` into a `0600` temporary file in
`~/.djinn/backups/` and atomically publishes
`djinn-backup-YYYY-MM-DD.tar.gz.age`. The backup directory is actively set to
`0700`; the default flow keeps only the newest archive across encrypted and
cleartext formats. `--no-encrypt` is the explicit cleartext opt-out and
uses the same atomic publication path.
Declared data volumes join default or explicit data backup; declared cache
volumes join only explicit cache backup. Only existing selected volumes are
archived; `none` and declared bind contents/markers are never backed up.
Declared volume archives are staged as
`declared-volumes/<actual-name>.tar.gz`, included in the outer tar; built-in
archives keep their root names/layout. The existing raw Docker volume helper
uses the selected actual name and mounts the backup source read-only.

`restore()` also refuses to run while containers are active. It identifies an
age archive from its `age-encryption.org/v1` header, decrypts it into a separate
restore-staging subdirectory, then extracts the outer tar. Cleartext gzip
archives written with `--no-encrypt` remain supported. Discovery covers root
archives and `declared-volumes/*.tar.gz`. Classification order is binding:

1. Declared namespace: restore into the actual volume name only if currently
   declared as data/cache; removed or current `none` warns by name and skips
   without creating or clearing a volume. A data/cache category change does
   not affect restore: the archive contents determine selection, with no selector.
2. Root `djinn-sync-*`: existing config-root sync route, unchanged.
3. Other root members: existing validated volume-name route, unchanged.

The raw volume restore helper receives the declared subdirectory as its archive
source; it gets no dev mounts/environment. No current-declaration filter applies
to root members: a `sync-claude` declaration at `none` skips its namespaced
volume archive while root `djinn-sync-claude.tar.gz` still restores credentials.
There is no manifest, arbitrary-bind restore, compatibility adapter or migration.
`none` means excluded from backup/restore, without protection from `clean all`.

Cache volumes are intentionally excluded from default backups because they are
large and rebuildable.

## Data Flow Diagrams

First run:

```text
djinn init
  |
  +-- prompt for workspace mode, mode-specific code_dir, and timezone
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
docker compose parses ${CODE_DIR}, ${DJINN_WORKSPACE_TARGET}, ${DJINN_CONFIG_ROOT}, TZ, NO_COLOR,
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
  +-- Environment rule: Projects or AIOS root, workspace mode/target, Docker, Firewall, mounts, Shell, Audio
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
  +-- Security: summarize firewall and Docker access
  +-- run interactive shell as a background job
  +-- checkpoint changed settings every 30 s during the session
  +-- stop/join checkpointer and sync once on shell exit or SIGTERM/SIGINT
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
  +-- rotate older encrypted and cleartext archives
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
  preservation, strict current manifests, and standalone CLI use
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
