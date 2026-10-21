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
├── docker-compose.run.yml
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
│       ├── opencode_credentials.py  # OpenCode login sync, also copied into the dev image
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
│   ├── check-build-dns.sh
│   ├── probe-claude-isolated.py  # maintainer re-probe of the claude-isolated agent
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
- `serve: ServeConfig`: frozen model with unknown keys forbidden;
  `opencode_host_port` is an integer from 1024 to 65535, default `49380`.
  `djinn config set serve.opencode_host_port <n>` validates through this model;
  `djinn config show` includes a Serve table. The setting takes effect when a
  detached container is created; the container port remains `4096`.

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

The shipped defaults cover `claude`, `codex`, `opencode`, and
`claude-isolated`. A user file replaces all of them. Before it reads a user
file, `load_agents()` opens the file's directory with `O_NOFOLLOW` and the file
relative to that directory with `O_NOFOLLOW` and `O_NONBLOCK`, then checks both
open descriptors. The directory must be a directory owned by the current user
and not writable by group or others. The file must be a regular file with one
link, owned by the current user and not writable by group or others. The
content is read from the checked descriptor, decoded as strict UTF-8 and must
hold a non-empty `agents` table of tables. Only a missing file selects the
built-in agents, and an explicit missing path raises `ConfigNotFoundError`;
every other failure raises `ConfigValidationError` with the path and the
reason.

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
(with the OpenCode v2 default-export marker check), but is never cross-tool
projected or stale-removed. Missing native hooks are allowed.
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
copied byte-for-byte after UTF-8 and v2 default-export marker checks. A
non-portable item is invalid rather than translated: workflow synchronization contains no
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

Whole managed runtime files are written `0444` (`0555` when executable), and
the runtime manifest `0444`. A clean managed file or manifest with any write bit
is replaced at the next delivery to reseal it. Write permissions are not drift
state; the content hash and executable flag remain the comparison. Carriers
(`settings.json`, `config.toml`, `opencode.json`) keep their existing mode
handling, as does the canonical tree (`0644`/`0755`). Read-only modes stop
in-place writers such as Codex/OpenCode edits and shell redirection; rename-based
tools such as Claude Code's Edit and `sed -i` can still replace files. Detection
and the backed-up discard path provide recovery.

Dev can write runtime trees and writable aliases of canonical files, so every
access below a root is anchored to the root's locked directory descriptor and
follows no symlink. Each path component is opened with `O_DIRECTORY|O_NOFOLLOW`;
a symlinked or
non-directory component is `collision`. A file is pinned with `O_PATH|O_NOFOLLOW`
and read through `/proc/self/fd` only when the pinned inode is regular, so links,
FIFOs, sockets and devices are never opened for I/O. A write creates a random
`.djinn-publisher-*` file with `O_EXCL|O_NOFOLLOW` in the verified parent and
renames it there; a stale entry is unlinked in the verified parent. Roots are
opened without following, and root identity is decided by `lstat`/`fstat`, so a
symlinked canonical or target root is refused. The source walk works the same
way and refuses a symlinked directory instead of skipping it. Config sync and the
native-only adapter reads use the same reader, `read_regular_file()`. Ancestors
of the roots are trusted. Each file, carrier, retired unlink and manifest is
rechecked against its preflight snapshot at the mutation boundary; a change is
named as `changed-during-publish` and refused. This narrows but cannot close the
last read-to-rename window against writers that ignore Djinn's advisory locks.
Dev can also replace a managed file after publication; the next check reports
drift or collision. Host-side writers bypassing Djinn remain outside its locks.

The one manifest schema is `{source, items}`. An item is either a file path or a
carrier path plus key path and records `content_hash` and `executable`. The
canonical instance is `config/.djinn-config-sync.json`; each publisher-managed
runtime root uses `.djinn-workflow-state.json`. Neighboring JSON carrier keys
are preserved semantically. The managed top-level TOML assignment is spliced
while preserving every other byte and then re-parsed. Unknown or edited current
manifest state fails closed.

The audit result is one of `clean`, `source-changed`, `target-drift`,
`collision`, or `invalid-or-semantic`. Drift entries retain class, relative path,
carrier key, origin and optional reason. Labels are `config/<tool>/<relative>`
for canonical items, `<tool>:<relative> (<root>)` for runtime/host-fallback
items, `opencode-runtime:<relative>` for container OpenCode, and
`config/opencode/<relative> (seed)` for its seed view. Fragments append
`[key.path]`; manifests use `config/.djinn-config-sync.json` or the runtime
root's `.djinn-workflow-state.json` label. Components are sanitized; human
summaries show at most five items and an `and N more` count. Failures without
a known item do not invent a manifest path; a generic canonical audit names
`config`.

The standalone publisher emits a structured `workflow publisher drift-items:`
JSON diagnostic (at most 200 items plus total/truncation), alongside human item
lines, without file content. Origins are `target`, `seed`, `target-manifest`
and `canonical-manifest`. Its `--check` verifies the seed before inspecting the
runtime and never publishes or provisions. A seed mismatch is `source-changed`
and prevents runtime inspection. Lock and write diagnostics retain precedence.

`start_workflow_targets()` in `core/config_workflow.py` defines the shared root
set for start, status, doctor and discard: config-root Claude and Codex, with
Compose Claude skipped because it uses direct mounts. Runtime inspection builds
the wanted view from the current source and uses the same final delivery
transform as publication, including host-Claude bridge/hook rewrites. Shared
canonical and target locks protect checks; a missing runtime root is clean and
is never provisioned by inspection. An invalid source build reports runtime
`not checked` rather than clean.

`djinn config status` audits canonical and start runtime roots read-only, prints
sanitized item labels and remedies, and exits `1` if either is non-clean (`0`
only when clean). Plain `djinn config sync` writes canonical views, then warns
about runtime drift without repairing it or changing a successful canonical
sync's exit status. Doctor's `Config workflow` row combines the same canonical
and runtime checks with named `WARN`s. `doctor --fix` may seed a source root but
does not synchronize workflow views.

`core/config_sync.py` owns the shared remedy table used by config, preflight and
session; entrypoint advice follows the publisher's class and item origins. See
the [operator state/remedy table](README.md#global-agent-workflow-ownership).
Host-fallback roots are excluded from discard and retain their confirmation
gate. Their target-drift refusals precede confirmation and give per-item
restore sources from the final view: `config/<source-or-tool>/<path>`, the
shipped Claude bridge, or a carrier key's value (with the host-Claude hook path
rewrite). Retired items get `remove` actions. The manifest is never deleted,
so ownership and retirement survive; executable restores retain the exec bit.

`djinn config sync --discard-target-drift` restores managed target edits in the
canonical tree and start runtime roots, including pending source synchronization.
It refuses collisions and invalid states, including a managed TOML key that
cannot be spliced from a multi-line/table form. The named-key remedy is to use
a single-line value or delete that key while preserving personal neighbors.
With no target drift, it behaves like plain sync without creating a drift
backup directory.

`commands/_lifecycle.py` supplies the same lifecycle guard and fail-closed
running-container check to backup and discard. Discard takes the control guard
then the non-blocking creation guard before checking for running Djinn containers;
running or unknown refuses. The guard stays held through the final publish.
Publisher leases are acquired and released within each phase:

1. Audit the canonical tree and inspect all start runtime roots read-only.
   Admit only target drift and pending canonical source changes.
2. Back up every root before publishing any root. Canonical backup uses an
   exclusive canonical lease and the same released ownership records as sync;
   runtime backup uses shared canonical and target locks. Copy whole differing
   files, once per carrier for key drift; record missing items without a copy.
   Flush all backup files and directories, including the home-to-backup chain,
   before crossing the backup barrier. Failure keeps partial backups and
   overwrites no target.
3. Acquire fresh leases for canonical sync, then direct runtime delivery.
   Accept only target-drift items whose current state matches the backed-up
   state; the mutation-boundary recheck still applies. Carrier authorization
   compares the managed key value: later personal-key changes are preserved
   by the merge. Completed root publications remain if a later publish refuses.

`core/workflow_drift_backup.py` admits the backup location without mutation
before taking the lifecycle guard. The fixed location is
`~/.local/state/djinn/workflow-drift/<UTC %Y%m%dT%H%M%SZ>-<pid>/` under the resolved
passwd home, independent of `HOME`/`XDG_STATE_HOME`, with per-root `config/` and
`<tool>/` subdirectories. A no-follow descriptor walk requires owned directories,
creates new directories `0700` and files `0600`, and refuses unsafe existing
components rather than chmodding them. Existing `.local`/`state` modes stay
unchanged; `djinn` and below must be private. Both prospective paths and the
physical paths of opened descriptors must not overlap the installation,
config/shared/local roots, `code_dir`, sessions or any declared bind source.
These records are outside `djinn backup`, sync zones and dev mounts; operators
exclude `~/.local/state/djinn` from their own sync and mounts, as for post-mortem
records. Output names each backed-up or missing item and the backup directory.

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
- `_compose_host_env(config)` overlays them onto `os.environ` and pins `HOME` to
  `str(Path.home())`, so Compose's `${HOME}/.djinn/sessions` is the checked
  directory even without inherited `HOME` or with a project `.env`
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
`compose_run()` is the subprocess site for foreground starts and guest runs.
It builds `host_env = _compose_host_env(config)` once; guests pass that same
mapping to network preflight and `subprocess.run()`.
When stdout or stderr is a TTY, `build_compose_env()` also renders
`DJINN_TERM_WIDTH` from `shutil.get_terminal_size().columns`; otherwise that
variable is left to inherited host environment or Compose defaults.

`ensure_host_env(config)` provisions bind-mount sources before Compose runs.
It creates missing zone roots under trusted ancestors, then opens them without
following links and secures them with `fchmod(0700)`. Explicitly configured roots
arrive canonicalized; linked derived roots are refused. Credential subdirectories
and assigned local/shared overlay directories are walked from those descriptors
without following links and secured to `0700`. Owner-unreadable directories are
tightened before they are opened. Existing content remains untouched.
Bind-mount placeholders use the same walk: new directories are `0700`, new files
are empty, exclusive and `0600`; existing directories keep their modes and existing
files must be regular. Sessions/backups/gitconfig sources keep pathname creation.
Host `.ssh` is neither provisioned nor delivered. `djinn doctor` reports permission
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

`seed_config(project_root)` is copy-if-absent. An initialized Claude root is
untouched. After the gate admits a baseline, the Claude root is opened without
following links. Target type checks use no-follow stat at its descriptor; links
and wrong types are removed without following and replaced. Correct types remain.
Files use the publisher's random exclusive temporary file and rename; trees use
an exclusive random temporary directory and rename. Files keep shipped bytes and
the executable bit. The committed file is read back and must match the shipped
bytes and executable bit, and the committed tree must be the populated directory.
`.gitkeep` files are skipped. Containment refusals and OS
failures become `SeedingError` with the path and a remedy.

Shared seeds keep pathname copies inside the installation. Wrong types are
repaired by `_repair_wrong_type()`, including dangling links. Files and directories
copy to `.<name>.seed-tmp`, then `os.replace()`; `_ignore_gitkeep()` skips `.gitkeep`.
For these shared seeds, `_blocking_ancestor()` detects the
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
  +-- workflow-publisher.py admits OpenCode runtime entries and copies the personal carrier
  +-- opencode-credentials.py sync --mode start merges OpenCode logins (refusal stops startup)
  +-- workflow-publisher.py publishes ~/.opencode/seed -> ~/.config/opencode
      using the read-only /home/dev/.djinn-canonical root and the runtime state manifest
  +-- source mcp-register.sh and register MCP servers
  +-- Codex daemon: updater off, reconcile to image CLI, prune other releases
      (dev-container starts only; warn and continue)
  +-- root helper replays missing runtime packages (no network if none missing; warn and continue)
  +-- install optional cached tools
  +-- print security summary, including firewall, Docker access, and MCP state
  +-- start checkpointing OpenCode logins and changed settings every 30 s
  +-- run interactive zsh as a background job, waited on by PID 1
  +-- stop/join checkpointer, publish OpenCode logins, sync settings once on shell exit or SIGTERM/SIGINT
```

The `Codex daemon` step invokes `/home/dev/codex-daemon-policy.py` with
`--codex-home "$HOME/.codex"` only when the entrypoint has no command arguments
(`$# -eq 0`). This covers foreground and detached dev-container starts; one-shot
`djinn run` and `compose run … <cmd>` skip it. Interactive Codex TUI, resume and
fork normally start the managed daemon; `codex exec` and plain stdio
`codex app-server` do not.

The helper first strictly parses `app-server-daemon/settings.json` and sets only
`updater.autoUpdateEnabled=false`, preserving other keys, including
`remoteControlEnabled`. It creates an absent file and atomically replaces a
changed valid document in the same directory. Malformed JSON, invalid document
shape or symlinked/non-regular settings are left untouched with a warning.

With an existing `packages/app-server-daemon/current` selection, it compares
the selected `codex-package.json` version with `codex --version`. A mismatch or
unreadable selected manifest triggers
`codex app-server daemon update --from-cli --yes`, copying and pinning the
image's CLI package. Codex child processes receive `CODEX_HOME` bound to the
mounted home. Without a selection, reconciliation and pruning do nothing;
Codex normally bootstraps a fresh installation on its first TUI launch.

After rereading and validating the selection, the helper removes other direct
entries under `packages/app-server-daemon/releases/` (about 410 MiB per measured
release). Package ancestors and the selected directory/manifest must pass
symlink/type checks, and a `current` link target containing `..` is refused;
pruning stays inside the opened releases directory, and an entry that cannot be
removed warns without keeping the others. `codex --version` (30 s) and the
native update (300 s) run with closed stdin in their own process group; a
timeout terminates that group, and an update timeout skips pruning. Only stdout
carries the version; names in the helper's output are control-character
escaped. Failures warn and startup continues. Version
changes come through `djinn update`, `djinn build` and a new dev-container start.

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
`Seed & Config`, `MCP`, `Packages`, `Tools`, and `Security`. `mcp-register.sh`
captures third-party CLI output from MCP add/remove commands and passes non-empty output
through `ui_boxed`, so external tool chatter stays visibly nested under the MCP
section while remaining on stderr.

For Claude, `docker-compose.yml` mounts selected directories and files from
root-level `config/claude` directly into the live `~/.claude` tree, including
`AGENTS.md` as the root instruction file. Only settings are
merged. This Compose-Claude runtime is manifestless: the publisher never writes
to `${DJINN_CONFIG_ROOT}/claude`. In-session settings changes are reverse-synced
to `config/claude/settings.local.json`, not to the tracked baseline template.

OpenCode's personal carrier is `~/.opencode/` (host `<config-root>/opencode/`),
with exactly `opencode.json`, `cli.json` and direct `themes/<name>.json` children
(non-empty UTF-8 names without a leading `.`), each a regular file of at most 1 MiB.
Before any OpenCode runtime access, startup admits the runtime root and existing
allowlisted entries, then atomically copies carrier bytes to `~/.config/opencode/`
without JSON validation, before workflow publication and MCP registration.
Symlinked roots, symlinks or non-regular allowlisted entries (including `themes/`)
in either tree, and failed copies, stop startup with the entry and reason;
nothing behind a refused entry is accessed. An oversized carrier file is skipped
with a named warning and protected from all write-back for the session, including
degraded final sync. Invalid JSON is copied; OpenCode reports its content errors.

The personal carrier uses `workflow-publisher.py`'s contained directory-descriptor
operations: `O_NOFOLLOW`, pinned regular files, bounded reads, temporary-file
fsync, atomic rename and directory fsync. The workflow publisher and MCP writer
also use these contained operations for every write into the runtime root;
there are no fixed-name temporary paths there. Root ancestors are trusted;
concurrent swaps by another process of the same user are outside this boundary.
Optional plugin, theme and local-model examples are in [OpenCode 2](docs/opencode-v2.md).

After workflow publication and MCP registration, the entrypoint captures initial
runtime references in a private mode-0700 directory, then checkpoints Claude
state, filtered Claude personal settings and OpenCode personal settings every
fixed 30 s. OpenCode enumerates the allowlist anew at each sync, picking up
runtime-created themes and `cli.json`. Changes older than about 30 s survive a
crash, qualified by checkpoint duration, scheduling, valid JSON and writable
healthy storage. Each file is captured once and compared with its last
acknowledged runtime bytes; unchanged content skips validation and writing.
Changed content must parse as exactly one JSON document, then is installed
atomically and acknowledged only after file fsync, replacement and directory
fsync succeed. Destination files have mode 0600. OpenCode uses the contained
writer and a strict UTF-8 JSON validator rejecting malformed numbers, `NaN` and
`Infinity`/`-Infinity`; Claude keeps its jq validator and `settings-copy.py`.
JSONC is not persisted.

Claude acknowledgement retains raw captured bytes; only the destination has the
three managed hook keys removed. Unchanged runtime content leaves host edits
alone, including a host-only `settings.local.json` edit at clean stop; when both
changed, runtime wins. Refused OpenCode runtime entries (symlink, non-regular or
over 1 MiB) preserve the carrier and acknowledgement. Refused or unwritable
entries warn once during checkpoints and again at final sync if unresolved;
changed invalid JSON is retried and warned about at final sync. Claude settings
must be an object accepted by the managed-hook filter; filtered-output failures
warn as storage failures. Comparison read errors preserve destinations and
references and retry. Clean stop or `docker stop` (SIGTERM/SIGINT) joins any
in-flight write, runs the same sync, and keeps shell/TERM/INT exit codes.
Initial OpenCode entry refusals (including oversize) leave that path unacknowledged,
keep sibling checkpoints running and permit later repair. Failed initial capture
I/O or state creation disables checkpointing with one warning.
Final sync recreates missing private state once; failure writes nothing and warns
per existing Claude file, plus once naming the OpenCode runtime and carrier roots
without enumerating their files. Without references (initial failure or later state loss,
with no extra warning for the latter), every valid runtime file except protected
OpenCode paths is written, so host-only edits can be overwritten and deleted
carrier files can return in that degraded path.

OpenCode persistence is upsert-only: deletions do not propagate in either
direction. A theme deleted only at runtime returns after recreation. A theme
deleted in the carrier stays absent after recreation unless a later runtime edit
or degraded final sync wrote it back. Reliable removal means stopping the
container, deleting the carrier file, then recreating.

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

- `dev`: development container on `djinn-network`, or a guest run on the project
  default network through `docker-compose.run.yml`

There is no separate authentication service. Every bundled CLI signs in from
inside a normal `dev` session: the tool prints a URL, the user opens it in the
host browser and pastes the returned code back into the container. No loopback
callback is *needed*, so authentication requires neither host networking nor a
published port. Separately, `compose_up_detached()` publishes OpenCode for the
`dev` service using a generated long-syntax Compose entry: `target: 4096`,
`published: str(config.serve.opencode_host_port)`, `host_ip: 127.0.0.1`,
`protocol: tcp`. `OPENCODE_CONTAINER_PORT` is defined in `core/docker.py`.
`compose_run()`, helper services and the base Compose file add no such entry.
The publication persists while the detached container exists, including when
no Djinn-managed server runs; see [remote server access](SECURITY-MODEL.md#remote-agent-server-access).

Selecting that flow is not uniform. Claude Code and OpenCode prompt for a pasted
code by default. Codex is the exception: plain `codex login` starts a
container-local login server that the host browser cannot reach, so users must
run `codex login --device-auth` (or choose the remote/headless option in its
TUI). README documents this.

Claude Code, Codex and the GitHub CLI persist credentials in config-root bind
mounts. OpenCode 2 stores its logins (API keys, OAuth tokens, external logins
and MCP OAuth) in the SQLite database `~/.local/share/opencode/opencode.db`,
which lives in the `djinn-opencode-data` volume; the former `auth.json` and
`mcp-auth.json` files are no longer used. Djinn mirrors those logins into the
config-root file `<config-root>/opencode/credentials.json` (the `opencode auth
export` format, mode `0600`). Logins removed everywhere are listed by ID in
`<config-root>/opencode/credentials.removed.json`, which holds no secrets. The
dev container syncs at start, every 30 s and at stop (exit, SIGTERM, SIGINT). A
sync merges the database, `credentials.json` and Syncthing conflict copies of
both files, writes the merged result to the config root first, and only then
updates the database; at stop it only writes the config root, and the next start
updates the database. The merge rule is the union of all logins by ID, minus the
IDs in `credentials.removed.json`. When two copies of one ID differ, the OAuth
login with the later expiry wins; otherwise a fixed order decides and Djinn warns
with the ID. Which account of a provider is active stays per host; it is not
synced.

The mechanics:

- `/home/dev/opencode-credentials.py` (from
  `src/djinn_in_a_box/core/opencode_credentials.py`, standard library only) runs
  every OpenCode credential command with `--standalone`, a fresh empty config
  home and no project config, so plugins and MCP servers are not loaded.
- Host-local state `~/.local/share/opencode/djinn-credentials-host.json` (no
  secrets) records the fingerprints last seen in this database, the logins hidden
  on this host, and an account to reactivate; it is reset when the database file
  changes.
- Writers take an exclusive `flock` on the carrier directory itself; files are
  written atomically with compare-before-rename.
- Exit codes of the helper: 0 done, 3 refused (path type or ownership), 1
  anything else.

Common mounts include:

- `${DJINN_CONFIG_ROOT}/claude` to `/home/dev/.claude`
- `${DJINN_CONFIG_ROOT}/codex` to `/home/dev/.codex`
- `${DJINN_CONFIG_ROOT}/opencode` to `/home/dev/.opencode`
- `${DJINN_CONFIG_ROOT}/gh` to `/home/dev/.config/gh`
- `${DJINN_CONFIG_ROOT}/age` to `/home/dev/.config/age`
- the `djinn-packages` data volume at `/var/lib/djinn-packages`
- named volumes for caches, OpenCode data, VS Code server state, and workspace
  metadata
- read-only host `~/.gitconfig`
- for dev starts only, generated public SSH files at `/home/dev/.ssh` and the
  filtered Git agent export at `/run/djinn-git-agent`, both read-only
- the writable `config/claude` seed mount plus nested direct mounts for its
  workflow files, including `AGENTS.md`
- the read-only `templates/claude/CLAUDE.md` bridge at `/home/dev/.claude/CLAUDE.md`
- the read-only canonical `./config` mount at `/home/dev/.djinn-canonical` for
  the shared publisher
- the read-only `./config/opencode` view at `/home/dev/.opencode/seed`
- `${CODE_DIR}` to `${DJINN_WORKSPACE_TARGET:-/home/dev/projects}`: one workspace
  root at `/home/dev/projects` or `/home/dev/aios`; AIOS's `projects/` is then at
  `/home/dev/aios/projects`
- `${HOME}/.djinn/sessions` to `/home/dev/sessions`

Both canonical views refuse writes with `EROFS`; only `config/claude` retains
writable workflow binds. With a non-Claude source these remain writable aliases
of generated targets (follow-up
[#165](https://github.com/w2kr1stn/djinn_in_a_box/issues/165)). With OpenCode as
source, no writable source alias is delivered and its seed verification has a
known mismatch reported as `source-changed`
([#168](https://github.com/w2kr1stn/djinn_in_a_box/issues/168)). The publisher
checks that seed first, so a mismatch can mask runtime drift. A writable
workspace or other bind containing the installation, which would alias these
views, is refused at creation.
OpenCode's `~/.config/opencode` runtime stays in the container layer and is
rebuilt at every start. The image gate requires `djinn.workflow.publisher=2`;
older or unlabeled images refuse with `Rebuild/recreate required.` before
Compose or a running-container refresh.

The base Compose environment sets `TZ`, `NO_COLOR`, `DJINN_TERM_WIDTH`,
`UV_LINK_MODE=copy`, and `LOCAL_ENDPOINT`.
`NO_COLOR` and `DJINN_TERM_WIDTH` propagate the host's plain-output and terminal
width decisions into the container shell UI. Resource limits use the Compose
variables rendered by `build_compose_env()`.

The dev service also sets `CODEX_SQLITE_HOME=/home/dev/.codex/sqlite`. Codex
honors an explicit `sqlite_home` in its `config.toml` ahead of that environment
variable. `COMPOSE_ENV_KEYS` reserves `CODEX_SQLITE_HOME`; `DJINN_ENV_KEYS`
reserves `CODEX_HOME` and the policy helper override so declarations cannot
redirect the mounted Codex home or its policy.

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

Foreground and detached dev starts acquire the same runtime before launch and
append its one internal mount/environment fragment to declared delivery. Only
`public/` and `export/` are bound, never `private/` or the runtime parent. The generated directory
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
serializes pending dev starts and is released after ID handoff. The observer bounds
key loading at 120 seconds and container discovery at 60 seconds; the single
`ssh-add` call itself is bounded at 120 seconds on a terminal and 10 seconds
otherwise. CLI exit after detached handoff keeps the agent; foreground completion,
timeout, interruption or failure releases it. Unknown Docker,
state-file or agent reads retry on the next tick. Stable ticks take no lock;
transitions recheck ownership under a non-blocking guard. Busy locks retry.
Each companion or helper transition retries independently after an uncertain result.
Retry reasons carry UTC timestamps in the host-only `observer.log`; repeated
identical reasons are logged on change and at most once per 60 seconds.

The observer ends only on a runtime-close signal, a changed or removed runtime
generation, confirmed dev removal or replacement, a stopped Git-only dev without
resources, a changed dedicated-agent key set, a confirmed dead dedicated agent,
an exhausted filter rebind allowance, an agent Docker companion resume failure,
an unexpected loop error, or failed startup before attachment. Replacement leaves
foreign resources intact. A companion resume failure attempts a dev stop (verified when it can be) before ending.
The agent is never restarted without host-terminal key loading.

The protocol filter retries transient accept and client-thread startup errors.
A dead filter thread is rebound at the same exported socket path under the guard,
after checking the generation. At most five rebinds are allowed in a rolling
300 seconds; failed attempts count and are spaced (the next attempt waits 5 seconds,
doubling per consecutive failure up to 60 seconds; a successful rebind resets it). Every rebind attempt, successful or failed,
logs its UTC time and outcome in `observer.log` and publishes the last five times
in `state.json` as `filter_rebinds`. A failed state publication
retries without creating another filter or consuming another rebind allowance.

Terminal causes are `dev-replaced`, `key-set-changed`, `agent-exited`,
`filter-failed`, `companion-resume-failed` and `observer-error`, corresponding to
the replacement, changed keys, dead agent, exhausted rebinds, failed companion
resume and unexpected loop error above. They populate `state.json`'s optional
`observer_exit` object with `code`, host-only `detail` (the fixed cause text plus a
truncated error, at most 512 characters), UTC `time` and `rebinds` (the last five
rebind attempt times). The same cause is also written first as a flushed UTC line in
`observer.log`, before the post-mortem record, the guard and cleanup, so it survives
a busy guard or a failing recorder.
Except for dev replacement, the observer also writes `export/observer-exit`,
visible read-only in dev at `/run/djinn-git-agent/observer-exit`. That file contains
the UTC time, generic cause and host recovery hint, without paths, keys or
exception text. State and export writes require the same runtime generation.
Each cause channel is best effort. Cleanup and the next start remove the file.

Doctor's Git agent row checks observer liveness before probing the socket.
Unknown liveness warns. A dead observer with the same running dev ID and
generation fails with its recorded cause and time, or names the missing cause;
the remedy points to `observer.log` and host-side `djinn clean`, then `djinn start`.
A dead observer with a stopped or absent dev warns that the next start reclaims
stale state. A replacement dev warns and includes any recorded cause. A live
observer attached to the running dev fails when its exported filter socket does
not serve. Confirmed delivery with `filter_rebinds` warns with the count and last
UTC time; otherwise confirmed delivery passes. Stopped devs and other unconfirmed
key sets or lifecycle states warn.

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

Helper and companion healthchecks probe every 2 s only while Docker reports them
`starting` (`start_interval`), then every 30 s; each probe is an exec and would
otherwise fill the Docker event stream. Until the first successful probe, Docker
ignores failed probes inside the start period (30 s for desktop helpers, 60 s for
the companion), so a desktop helper that runs but never passes its probe stays
`starting` and fails creation at the 15 s deadline with
`helper health starting (readiness timeout)`. After startup, Docker reports a
failure as `unhealthy` only after about 60–90 s (up to about 2 minutes with probe
timeouts) and a recovery as `healthy` within 30 s. Desktop creation, doctor and
sealing add their own fresh `health.py` probe to the Docker status, so they see a
failure at once. Djinn never execs into the companion itself: doctor and sealing
still check its profile by host inspection against the trusted manifest, but take
its liveness from the Docker status, which lags accordingly.

For dev starts, `core/host_runtime.py` always takes the canonical creation guard,
including without Git identities. Generation labels, actual container IDs and
persisted ownership coordinate creation, clean, and the detached observer's helper stop/start/remove
transitions. Docker's absolute executable is resolved once and passed to the
isolated observer. Empty Git identities still create no agent, keys or SSH mounts.
Unknown Docker state and replacement generations authorize no resource cleanup.

`core/post_mortem.py` collects host-private JSON records outside the records
lock, then serializes deduplication, publication and pruning under
`~/.local/state/djinn/post-mortem/.lock`. The root uses the controller's passwd
home, with no `XDG_STATE_HOME` relocation: directory `0700`, files `0600`.
Publication uses an exclusive no-follow temporary file, fsync and atomic rename.
Only record-pattern regular files owned by this UID with no group/other access
participate in deduplication, latest lookup and retention; other entries are ignored.

The dedup key is the first 12 hex characters of SHA-256 over sorted
`id|FinishedAt` subject pairs. Observation-only records use hostctl generation
and expected/actual dev IDs. After publication, pruning drops oldest records to
keep at most 10 files / 8 MiB, always keeping the new record. Each record is capped
at 1 MiB, dropping log tails with a marker to fit. Inspect strings are capped at
2 KiB; `docker logs --tail 200` streams into a 64 KiB tail buffer. Records include
UTC time, trigger, separate runtime/hostctl generations, observed dev identity,
final state (including exit code and OOM flag), image, labels, restart count,
resource limits and best-effort cgroup v2 memory readings with unavailable reasons.

`RECORD_SECONDS = 15` bounds waits started by the recorder: each Docker call gets
`min(5, remaining)`, and lock retries use the remaining budget. Expiry may leave
an incomplete record or no record if the lock cannot be acquired. Local filesystem
calls are outside this bound. Callers can hold lifecycle guards during recording,
so concurrent lifecycle commands may see the existing busy refusal. Recorder and
notification failures never abort recovery. Start/clean print
`Post-mortem record: <path>` for a new or matched record, or
`Post-mortem record failed (<code>)`; output is literal with control characters
escaped, never raw Docker errors or record contents. Detached observers send
these lines to their host-only `observer.log`. The Git observer closes Git
delivery before final `observer-cleanup` recording; that hook can capture the
stopped dev even when it leaves the dev for a later clean. Terminal observer
causes also produce an independent `observer-exit` post-mortem trigger with the
cause code, runtime generation, expected and last observed dev IDs, and rebind
UTC times in `detail.rebinds`. It requires neither cleanup ownership nor the
runtime guard. Its dedup key includes the generation and cause code; repeated
identical incidents deduplicate. Rebinds alone create no post-mortem record.
Records survive runtime state cleanup and are never included in backup or sync sets.

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
  includes the five recursively repaired roots, the reserved root-owned package root
  `/var/lib/djinn-packages`, managed SSH targets, desktop
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
  After all target checks, writable invocation binds are checked against host
  execution inputs; read-only mounts skip this check. The inventory is built on
  the first writable source that needs comparison and reused for that validator
  call. `host_sealing.execution_overlap()` applies the sealing canonicalizer and
  overlap predicate, returning the first match. Inventory or comparison failures
  fail closed with `cannot be checked against host execution inputs (<cause>)`.
  The inventory also records the resolved location of every symlink traversed
  on an execution input's path. Resolved invocation sources containing `:` are
  refused, as for declared binds.
  The execution inventory and guard comparison use strict presence checks that
  propagate inspection errors; other sealing paths retain lenient checks.
- `MountSpecificationError` reports invalid mount grammar or reserved targets;
  `MountCollisionError` reports the two involved mounts and the conflict path;
  managed-root conflicts identify the Djinn-managed root.

`managed_bind_problems(config)` collects problems for configurable managed
writable sources without provisioning: the expanded `config.code_dir`,
`get_config_root(config) / name` for `SYNC_PATHS["credentials"]` (`claude`, `codex`,
`opencode`, `gh`, `age`), assigned zone overlay sources, and
the sessions directory under `Path.home() / ".djinn" / "sessions"`.
A source whose path contains a `..` component or `:` (which Compose splits) is
refused as `cannot be checked` without calling the overlap check; a relative
home still fails closed.
One `_execution_input_check()` closure per
call reuses the declared/invocation inventory and `host_sealing.execution_overlap()`
predicate, including symmetric overlap, inode aliases, symlink locations and
missing suffixes. Relative or uninspectable sources and undeterminable inputs
fail closed. Source construction also contains `OSError`, `ValueError`, `KeyError`,
`TypeError` and `RuntimeError` (including `ConfigValidationError`), so config-root,
zone-root, zone-assignment or home-lookup failures become family-specific
`cannot be checked against host execution inputs (<cause>)` problems. Each problem
names the family, source or configuration setting, cause and remedy.
Identical uncheckable causes from the per-source check are reported once, with
the neutral remedy `fix that cause and retry`; overlap remedies and
construction-error problems are unchanged.
`validate_managed_bind_sources(config)` raises `MountSpecificationError` with the
first problem.

For dev starts, `compose_run()` and `compose_up_detached()` call this wrapper in
every Docker mode, before shell/SOPS/zone mount builders, declared/invocation
validators, workspace preparation, companions, helpers or temporary overrides.
Guests first check phase-1 user and declared sources, then build shell/SOPS/zone
mounts and check their sources. Next come `validate_managed_bind_sources()`,
the declared/invocation validators and the DIRECT helper check, then preflight
and the phase-2 source check described under [Guest runs](#guest-runs).
These source-validation gates also cover direct callers.

When a mount exists, `compose_run()` uses the first mount target as
`--workdir`. With no mount it omits `--workdir`, so the Compose service's
`working_dir: ${DJINN_WORKSPACE_TARGET:-/home/dev/projects}` remains effective,
using the same selected target as the workspace bind. This also applies to
mount-less detached starts.

`config/zones.py` resolves additive shipped and user `zones.toml` assignments.
`_zone_overlay_sources()` enumerates every assigned source/target pair, including
missing sources, for both the managed collector and
`_zone_overlay_mount_args_and_targets()`. The latter reserves all assigned targets,
refuses symlinked or non-directory sources, and emits mounts for existing directories.
`ensure_host_env` creates every assigned local/shared overlay directory before
Compose runs. `compose_run` and `compose_up_detached` mount empty and populated
directories over the config-zone bind mounts. These overlays do not change the
user mount that supplies `--workdir`. Doctor reports current assignment validity,
skipped defaults, zone drift, private-directory modes and large direct files
that cannot be overlaid.

`DEFAULT_ZONES["codex"]["local"]` includes `sqlite`, alongside the daemon's
`packages`, `app-server-daemon` and `app-server-control` directories. With the
Compose SQLite setting, `state_*`, `logs_*`, `goals_*`, `memories_*`, `queue_*`
and `thread_history_*` databases, their `-wal`/`-shm` siblings and
`.sqlite-maintenance.lock` stay in `<local_root>/codex/sqlite/`.
`KNOWN_CONFIG_ROOT_ENTRIES["codex"]` does not list SQLite files or the
maintenance lock; such files in the config root report zone drift and, when
large, large-file findings. Sessions remain shared; SQLite operational state stays per host
and outside config-root backups. Cross-host discovery and memory-state limits
are documented in [the sync guide](docs/sync-across-machines.md#codex-sessions-and-local-state).

Declared mounts follow the three-layer configuration boundary: fixed built-ins
(credentials, seeds, workflow and cache targets), built-ins with a config value
(workspace, config root, SOPS key file, OMP theme), then additive declarations.
Declarations never replace, redirect, disable or remove built-ins.

`resolve_declared_entries()` is shared by foreground starts, guest runs and
`compose_up_detached()`, before any creation subprocess or temporary
override. It resolves bind symlinks, checks that the absolute colon-free source
exists as a directory, and inspects an optional marker directly under it with
`lstat`: a regular file passes; a missing marker, directory or symlink refuses
creation with the declaration name. There is no marker content/identity check.
The resolver never creates sources or markers, repairs binds or adds declared
paths to `ensure_host_env`. Existing built-in provisioning precedes validation
and retains its behavior even on a path overlapping a declared source. Validation
can race a disappearing drive; there is no mount-liveness protocol.

Targets use the existing canonicalizer and `_reserved_mount_targets`, with the
union of all Docker modes for declarations in start, run and doctor. Equality and
declared ancestors of built-in/reserved/zone/active-workspace/invocation targets
are refused; declared pairs cannot nest. Children otherwise remain allowed,
except at or below `MANAGED_TARGET_ROOTS`, the same set used by invocation mounts.
Its five recursively repaired roots come from `MANAGED_VOLUME_REPAIR_TARGETS`,
checked against the unchanged entrypoint repair list; the remaining roots are
the reserved `/var/lib/djinn-packages` (no dev ownership repair), managed SSH targets, desktop directories and the agent-Docker endpoint. The
declared diagnostic retains its `Djinn-managed volume root` wording. Actual volume
names are `djinn-<name>` and cannot collide with the built-in volume registry.
After source and target validation, `resolve_declared_entries()` applies the same
lazy check to error-free writable binds; read-only binds and declared volumes are
skipped. Inventory or comparison failures become declaration diagnostics with
the `cannot be checked against host execution inputs (<cause>)` wording, so
`djinn doctor` reports the affected declarations as FAIL rows.
`compose_up_detached()` doubles `$` as `$$` in invocation mount sources, resolved
targets and `working_dir`, and in every `_volume_specs_from_mount_args()` spec for
zone overlays and read-only shell/SOPS mounts as it enters the detached override.
Compose receives those paths literally; attached `-v` arguments remain raw.

The single reserved-environment registry covers all Compose service modes and
everything the repository ships into the image or runs at startup: Python,
scripts, tools, Dockerfile ENV/ARG/RUN and generated shell startup content.
Actual caller keys also reserve that invocation's keys. Mounted host shell
startup files and third-party tools outside the repository are outside this
boundary. Environment values are literal strings with no host override or
interpolation. Literal endpoints, including a CDP gateway, need manual edits
when the host gateway changes.

Foreground starts, guest runs and detached starts serialize the same declaration
fragment: long-form binds with `bind.create_host_path: false` and
`read_only: true` only for read-only binds,
volumes with actual sources and top-level
`volumes: {djinn-<name>: {name: djinn-<name>}}`, and a dev environment mapping.
Each `$` is escaped as `$$` in Compose-bound strings without mutating config.
The override is removed in `finally` on success, failure and timeout. Existing
invocation flags and working-directory selection remain independent of declarations.
Declared environment applies to the dev service, including guest runs; workspace
declarations also feed the companion on dev starts. Neither applies to builds
or raw archive helpers.
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

### Guest runs

For `service="dev"`, `compose_run(..., interactive=False)` takes the guest branch
and returns a `RunResult` with `owner=None`. `DockerMode.AGENT` raises
`RuntimeMountSpecificationError` before side effects. Each run uses
`djinn-run-<12 hex>` and `djinn.run=<same id>`, retains the Compose project
`djinn-in-a-box`, service `dev` and `--rm -T`, and adds no Git generation label.
It never acquires the creator lock or enters `git_runtime()`, prepares companions
or desktop helpers, or calls the hostctl start guard. A dev and other runs can
coexist; a run does not block dev creation. The shared mount/declaration builders,
workflow delivery and firewall remain available. See [the run contract](README.md#quickstart)
for delivery limits and [exit/cleanup behavior](README.md#container-entry-points).

`get_compose_files(docker_mode, guest=True)` places `docker-compose.run.yml`
after the base, desktop and Docker-direct files, before the generated override.
Its `services.dev.networks: !override [default]` requires Compose 2.24.4. The
same argument prefix, captured host environment and project cwd feed
`_run_preflight()` and the run. Preflight resolves `config --format json` with a
30 s timeout. It requires exactly the `default` service network, no
`network_mode`, and a top-level default network named `djinn-in-a-box_default`
with only `name` and empty `ipam`. Resolution errors or any other network
configuration raise `RunRefusedError`. Resolution failures report the actual
return code and last non-empty stderr line, or the exception if Compose could
not run. Network/JSON failures retain the Compose version/fragment diagnostic.

`_guest_source_check()` checks protected sources before execution-input
validation, then checks every bind in the resolved Compose configuration plus
the run's `-v` mounts. It substitutes resolved sources in invocation and declared
mounts, preserving targets, and checks base binds without rewriting them.
Phase 2 allows only data/cache volume names (excluding `agent_docker.CACHE`) and
declared volumes in any backup category. It strictly inspects existing volumes
and uses `host_sealing.volume_bind_source()` to check bind devices as bind sources
and refuse unknown storage provenance; absent volumes may be created by Compose.
Resolved sources containing `:` are refused.
The broad `host_sealing.protected_host_roots()` inventory includes the runtime
root; `private_state_paths()` keeps the narrower sealing map, derived from the
runtime constants, including `state.json` and all control files but excluding
`public/` and `export/`. `docker_storage_roots()` supplies the shared data-root
and helper-volume inventory; absent helper storage defaults to the data root.
Strictly resolved run sources are compared lexically with these storage roots,
without an identity probe into inaccessible Docker directories. Protected-source
or inspection failures raise `RunRefusedError`. DIRECT mode also checks the
helper under `hostctl.control_guard()` and refuses while it is running. The
[security model](SECURITY-MODEL.md#guest-runs) owns the source policy and residuals.

A `finally` block calls `_remove_run(id, 10)` after every launched run.
It checks the exact name, run ID label, project and service labels before
pinning the Docker ID for stop and force-removal. The initial inspect retries
once; failed identity checks authorize no removal. Stop errors do not prevent
a force-remove attempt on the verified ID. Warnings are collected until cleanup
finishes, and output errors cannot interrupt it. `core/signals.py` supplies
`signals_as_interrupt()` around temporary override creation, preflight, launch
and cleanup, and `signals_ignored()` during removal; previous handlers are
restored and non-main threads are unchanged.
Non-dev services retain their existing headless path.

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

A busy control lock skips observation publication for that round while dev and
companion checks continue. Three consecutive skipped rounds queue one
`observation-skipped` note; publication catches up when the lock is free. Every
closure path ends with the pinned helper stopped, including errors before
admission and cancellation. Closure tries the control lock without waiting for
at most two seconds, then uses an unlocked fallback. Both paths inspect with
positive `No such container` absence semantics, confirm the launch ID and
generation immediately before stopping that ID, and share `stop_and_verify`.
A different helper is preserved. Failed or unknown outcomes retry with a
0.5-second backoff doubling to five seconds, with one retry log line per minute.
The SIGTERM, SIGINT and SIGHUP handler ignores all three signals and sets a cancel
flag without raising. The observer checks that flag at each round and after sleeps,
then enters closure through its normal finalizer; a step already in progress (for
example an admission waiting for the control lock) finishes or times out first.
Closure subprocesses have timeouts because they inherit ignored signals.
The PID-1 timer stays the outer bound.

Verified closure journals `relay-closed` under the guard or when the helper is
already absent, stopped or replaced; `relay-closed-unlocked` records a verified
fallback stop. Under the control guard, verified closure also reconciles the pinned
stopped helper's expiry logs using `reconcile(wait=0.5)`. A busy journal leaves
expiry unmarked for a later reconciliation. The verified event and timestamp are
retained before provenance recording, reconciliation and control guard release.
Errors in these steps preserve the verified outcome and its original publication
budget without retrying the stop. All observer diagnostic
output is best effort, including retry, reconciliation and publication warnings.
Each failed attempt journals
`relay-close-failed` with generation, helper ID, attempt number and fixed reason
`inspection-unknown` or `stop-unverified`. Observer writes for skipped publications
and closure outcomes use `journal(wait=0.5)`;
busy or failed writes retain events in memory while stop attempts continue.
The 60-second publication budget starts at verified closure, before reconciliation;
when it expires a best-effort warning ends publication retries. Other journal
callers, including `reconcile()` without a wait argument, keep blocking acquisition.
Journal appends insert a missing newline after a partial earlier write, keeping
retries parseable. Failed closure counts use the highest attempt number so a
disk flush (`fsync`) error after a complete append cannot inflate them. Outcome
publication precedes teardown diagnostics. Doctor reads a bounded 64 KiB journal
tail independently of whether Docker or a status snapshot is available and reports
an unmatched failed closure as FAIL (unverifiable without Docker). A later
verified outcome, newer generation, or positive live evidence of closure clears
it; pre-stop intents do not. Closure state is retained per generation, with the
last `on` opening event in the tail selecting the current generation; without an
opening event, first-seen generation order applies. Delayed older-generation rows
do not promote that generation or clear the current failure. An unreadable journal
keeps its WARN even when the observer is current.

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

For dev starts only, the public SSH renderer composes a tailnet Include before
Git aliases, including when no Git identities exist. Opening atomically refreshes
only the tailnet files in the mounted directory. The dev image's `djinn-hostctl-connect` uses
SOCKS5 without a direct connection fallback. Each relay stream has structured
start/end records in the rotated Docker log.

The supervisor dispatches `connect <generation> <selector>` separately from its
JSON control client. It checks generation and live admission over the private
control socket, then uses the SOCKS5 client framing to enter only the gated
`127.0.0.1:1080` relay, always on TCP 22. It copies the opaque SSH stream in both
directions with half-close. Stdout carries only stream bytes. Before streaming,
failures return exit 1 with one stderr diagnostic:
`hostctl connect: generation changed`, `hostctl connect: window not open`,
`hostctl connect: relay refused destination`,
`hostctl connect: relay could not reach destination`,
`hostctl connect: setup failed` or `hostctl connect: setup timed out`.

One 10-second setup deadline covers the control request, gated dial and SOCKS
handshake; it is cleared before streaming. After admission the operation returns
exit 0 when streaming ends. When stdin ends, `connect` half-closes upstream,
forwards remaining upstream bytes for at most 2 seconds, then closes the
connection and returns without waiting for its stdin reader. After Ctrl-C the
relay's stream to the target can persist until the target closes it or the
window ends; its logs contain only connection metadata.

`djinn hostctl check [HOST]` calls `core.hostctl.check_hosts`, selecting aliases
and users from generation-matching `opening.json` and frozen peer trust. It
requires a running, admitted, unpaused helper, a terminal on stdin and stderr,
and `ssh` on the host PATH. It invokes host OpenSSH once per selected alias in
declaration order with `-F <config> -T -n <alias> true`. The shared tailnet
renderer supplies the host ProxyCommand and frozen `known_hosts` without
changing container delivery. The generated `-F` profile isolates user and system
SSH settings: `IdentitiesOnly yes` without an `IdentityFile`,
`IdentityAgent none`, `PubkeyAuthentication no`, `PasswordAuthentication no`,
`ForwardAgent no`,
`ForwardX11 no`, `ForwardX11Trusted no`, `ClearAllForwardings yes`,
`PermitLocalCommand no`, `ControlMaster no` and `ControlPersist no`. It sets
`StrictHostKeyChecking yes`, `UpdateHostKeys no`, `UserKnownHostsFile` to the
generated file, `GlobalKnownHostsFile /dev/null` and `LogLevel INFO`.
Keyboard-interactive authentication keeps its default.

OpenSSH runs the ProxyCommand through the user's `$SHELL -c`; the quoting is
POSIX (sh, bash, zsh, dash). The owner's environment (for example `DOCKER_HOST`
and `SSH_ASKPASS*`) is inherited by ssh and the proxy and is owner-trusted.

Each check uses a fresh directory under the hostctl state root with mode `0700`
and `config`/`known_hosts` files with mode `0600`, removed in a `finally` scope.
SSH output inherits the terminal without capture or a Djinn timeout. SIGTERM
and SIGHUP are converted to `KeyboardInterrupt` so cleanup runs; the previous
handlers are restored afterwards. Ctrl-C returns 130. Each alias prints
`Checking <alias>...`, then `<alias>: approved and reachable` for SSH exit 0 or
`<alias>: check failed (ssh exit <n>)` on failure. An interrupted SSH invocation
prints `<alias>: check interrupted`. The command exits 0 only if every selected
target succeeds, 1 on refusal or failure, and 130 on interruption.

Status appends a blank line, the header `Summary` and lines from the pure
`summary_lines` function over the existing snapshot and a supplied UTC time.
The `Window:`, `Sealing:`, `Relay:` and `Next:` lines show approximate time left,
grouped sealing counts (unknowns first, at most three groups per list, then
`+<n> more`), current-generation peers and one next action. Time left uses whole
minutes rounded down, `less than 1 min left`, `deadline passed` or
`time left unknown`; it never displays a negative duration. The summary adds no
probe or SSH connection, leaves snapshot and doctor behavior intact, and follows
the existing details and conditional single doctor hint.

`Next:` takes the first matching rule: incomplete or conflicting helper state
calls for `djinn doctor`; helper login needed calls for opening the login link
above in your own browser; paused waits for the dev start; opening waits and
reruns `djinn hostctl status`; open calls for `djinn hostctl check`, then agents
can use `ssh <alias>`. Otherwise, unknown sealing calls for `djinn doctor first`,
unsealed calls for
`djinn hostctl on --allow-unsealed (only if you accept the unsealed boundary)`,
and the remaining closed cases call for `djinn hostctl on`.
While the snapshot state is `opening`, a missing generation-matching observation,
`node_error` or missing node state counts as still starting. A present stale or
failed observation, `relay_error`, `window_error` or conflicting state still
calls for `djinn doctor`.

Tailscale documents check reuse per `checkPeriod` (default 12 hours), with no
documented exact cache key across source node, SSH user and destination. With
`checkPeriod: "always"`, a host check does not approve the agent's later
connection. Each check runs `true` as the target user and appears in the relay
log and the target's auth log.

`core/host_sealing.py` supplies the shared assessment for on, doctor, foreground
and detached dev starts. It reads actual ID, mounts, environment and network
peers, canonicalizes host sources/aliases, and consumes the desktop provenance inspector. Assessment
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

Foreground and detached dev starts inspect resolved Compose delivery before
launch. Unsealed delivery closes and verifies the helper; sealed delivery pauses
admission via private IPC.
The existing asynchronous observer inspects the actual created dev and probes
outside the control lock, then re-inspects dev and companion IDs/profile under the lock before
admitting/resuming. Planned and actual assessments carry companion generation,
network ID and profile fingerprint; opening and commit bind the same evidence.
The observer revalidates the companion throughout an open window and closes on
replacement, profile/storage/network drift or inspection uncertainty. Pause preserves immutable routes and helper-owned deadlines;
dev starts never inherit an override. Guest runs bypass this close/pause path;
see [their boundary](SECURITY-MODEL.md#guest-runs). External replacement/removal closes.

On external dev stop/removal/replacement, the observer stops the helper and
journals closure first, then releases the control guard before recording
`hostctl-teardown`. The observation stores `absent`, `stopped` or `replaced`,
expected/actual dev IDs and the hostctl generation; only the expected stopped
dev can supply container evidence. A replacement is named by ID without inspection.
Recorder failures are best effort, independent of the journal's closure policy.

`open_window()` and `stop_helper_locked(remove=True)` record an already stopped
helper before removing it (`hostctl-helper`), unless a matching teardown marker
exists or the helper log contains an expiry event for this generation at or
after the current run's `StartedAt`. A cached generation-level expiry does not
exempt a later failed restart. For a running helper, closure comes first.
Hostctl teardown markers are written after stop and the closure journal, using
one non-blocking records-lock attempt; diagnostic lock contention never delays
relay closure.

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

Runtime package delivery is added after the build-time `RUN apt-get` steps.
The user-creation `RUN` adds
`dev ALL=(root) NOPASSWD: /usr/local/libexec/djinn-apt-root` to
`/etc/sudoers.d/djinn-apt` (mode `0440`) and checks it with `visudo -cf`.
That root RUN also creates `/var/lib/djinn-packages` as root:root 0755; the
`djinn-packages` volume inherits this ownership. The helper checks ownership and
permissions on the directory and opened file descriptor before using the list,
and atomically records successful installs as root:root 0644 under its lock.
Later root-owned `COPY --chmod=755` steps install `scripts/apt-get-shim.sh` at
`/usr/local/bin/apt-get` and `scripts/djinn-apt-root.sh` at
`/usr/local/libexec/djinn-apt-root`, leaving build-time apt calls unchanged.
`scripts/package-replay.sh` is copied as dev-owned executable
`/home/dev/package-replay.sh`; the entrypoint runs it in Packages after MCP and
before Tools. It announces and calls `sudo -n -u root --
/usr/local/libexec/djinn-apt-root replay`, formats tagged info/warnings, and always
exits 0. Parsing, limits, package checks, update and install all live in the root
helper. See the [runtime package install security model](SECURITY-MODEL.md#runtime-package-installs)
for the boundary and residual risk.

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
`djinn.workflow.publisher="2"`; Compose starts and OpenCode session refreshes
check that label before doing workflow work. OpenCode is installed from npm as
[`@opencode/cli@2.1.1`](https://www.npmjs.com/package/@opencode/cli/v/2.1.1)
(published 2026-10-20), with the numeric `OPENCODE_VERSION` pin. Node agents are installed through
fnm. The final image PATH is `~/.cache/djinn-tools/bin`,
`~/.local/share/fnm/aliases/default/bin`, `~/.local/bin`, `~/.local/share/fnm`,
then the base system paths, so every process resolves optional tools and agents
without sourcing shell initialization.

The image locale is `C.UTF-8`. Runtime Docker access is disabled unless the user
starts with agent or direct Docker options.

Optional runtime tool installers are copied from `tools/`, with cache locations
backed by named volumes. The psql family uses wrappers in the tools bin directory
that prepend the tools library directory only for their real binaries in
`<tools dir>/postgresql/bin`, preserving any existing `LD_LIBRARY_PATH`.

## Container Lifecycle Commands

`commands/container.py` implements:

- `build()`: loads config, resolves the host agent record once before
  `preflight(config)` or `_sync_build_files()`, then refreshes local build files
  and calls `compose_build(agent_args=...)`. Invalid records fail before
  provisioning, sync or Docker work, naming the record file.
- `start()`: resolves Docker mode, loads config, then calls
  `validate_managed_bind_sources()` immediately after `load_config()`, before
  `preflight()`, `get_config_root()` or `prepare_config_workflow()`. A
  `MountSpecificationError` is printed through `error()` and exits 1; mount errors
  from this gate, source resolution and the creator are escaped with
  `rich.markup.escape()` before printing. It then
  preflights, prepares the workflow, ensures `djinn-network`, parses
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
  A stopped dev matching persisted runtime ownership can be reclaimed after a
  host or Docker restart once its observer is gone. `cleanup_owned()` records
  `start-reclaim` after ownership checks and before the first mutation; running
  or replacement containers keep the existing refusal.
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
  networks, agent Docker status and a compact remote server snapshot from
  `core/serve.py:collect_snapshot()`. Missing configuration uses `ServeConfig`
  defaults. Serve query errors become an Unknown line; the quick snapshot uses
  3 s for container inspection, 5 s for helper execution and 2 s for Tailscale,
  querying Tailscale only while OpenCode is running.
- `clean_default()`: `djinn clean` stops and removes containers with
  `compose_down(config=None)`, using best-effort placeholders. `compose_down()`
  refuses outright when the container it would reap is the one the process runs
  in (`is_own_container()`: `/.dockerenv` plus a hostname match against
  `container_name`). Compose selects by the pinned project name, so a teardown
  from any copy of the repo — a test sandbox, an agent's scratch checkout — would
  otherwise destroy the live session, and the socket is mounted so anything in the
  container can trigger it. Teardown from the host is unaffected. Agent generations are removed by exact owned IDs after ownership checks; their
  clean path refuses unknown project resources. Before that check, explicit run
  removal calls `_remove_run(id, 3)` for project-labelled runs, verifying the
  name/run/project/service labels and using the same stop/remove sequence as run
  cleanup. Failure to list or remove runs fails clean. Mismatches warn and skip
  explicit removal; normal `down --remove-orphans` can still remove containers
  carrying project labels. Other modes retain that Compose teardown for the base
  services, with explicit removal of the one-off dev container. Both branches
  remove `djinn-in-a-box_default` after verifying project/default-network labels.
  A run started during clean remains a [residual](SECURITY-MODEL.md#guest-runs).
  Before the first mutation, recording (`clean`) covers every container labelled
  with the Compose project plus recorded runtime resources, even without runtime
  state. A stale recorded dev ID does not hide a project-owned stopped dev.
  Hostctl helper removal has its own recording hook.
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
  ones and `none`. Default `clean` keeps persistent data/cache volumes and
  declared binds; disposable desktop/endpoint runtime volumes are removed.
- `update()`: runs `scripts/update-agents.sh --print` without loading AppConfig
  or requiring init. Captured stdin is closed; discovery runs in a new session
  with one aggregate 120-second timeout. Timeout or interruption kills the owned
  process group and reaps it. npm errors retain their stderr diagnostics.
  Exactly one numeric `ARG=x.y.z` line per agent is required before a single
  atomic save. Failure leaves the old record unchanged; success prints effective
  before/after versions and `djinn build` guidance. It writes no checkout file.
- `enter()`: opens a zsh shell in the first running Djinn container.

The recording subject rule is shared: present, stopped containers within the
caller's removal ownership boundary, excluding Compose
`com.docker.compose.oneoff=True`, `HostConfig.AutoRemove=true`, the one-shot
`agent-docker-firewall` initializer, and runs Djinn verifiably ended. Foreground
devs are one-offs; persistent companions/helpers that fail during those sessions
still qualify. Other recording points are `runtime-close` on every runtime
close (including failed creation), `observer-cleanup` before final owned cleanup,
and `helper-fallback` before removing a failed desktop helper. The observer also
captures an eligible stopped dev that it leaves for later removal.

`provenance.json` in the records directory maps container ID to `StartedAt`,
under the same lock and owner-only atomic writer, with at most 128 entries.
Markers describe verified outcomes for containers seen running before Djinn's
stop/removal: success, or a failed/timed-out operation followed by bounded
re-inspection showing the same run stopped. Removed containers need no marker.
Unreached operations, ambiguous outcomes and already stopped runs get none.
A restart changes `StartedAt`, so an old marker cannot hide a later failure.
Marker errors remain inside the recorder's best-effort boundary.

`host_runtime.REMOVE_TIMEOUT_SECONDS = 60` applies to container `rm -f` and
runtime `volume rm` via `remove_runtime_object()` (`cleanup_owned`,
`remove_runtime_volume`, `GitRuntime.discard_volume`), and clean's direct dev
removal. Other operations retain their deadlines, including the 5 s runtime
command default, desktop fallback and hostctl removals, and 15 s Compose down.
A removal timeout says `Docker is still removing <container|volume> <id or name>
after 60 s; the daemon may finish it in the background. Retrying is safe.`
Dev starts propagate `GitSSHError` to the CLI's `Internal runtime mount
construction failed: <message>` route. Guest refusals use `RunRefusedError`,
which the run CLI prints with exit 1. Clean's direct dev timeout returns
`RunResult(124, <message>)`; cleanup through `cleanup_owned` returns
`RunResult(1, "Cleanup preserved resources: <message>")`. Recording precedes
removal, so a removal timeout does not lose an already published record.

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

Build-time `packages.txt` puts packages in the image and suits packages every
new container needs from its first start. Agent-selected runtime packages are
recorded as intent in the persistent `djinn-packages` volume and replayed when a
container starts; the list does not add packages to the image build.

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
Upstream default bumps are reviewed through pull requests. After any default
bump, the maintainer builds once with the defaults alone (no local
`agent-versions.toml` record) and commits the bump only after that build
succeeds; CI does not build the image.

### Remote Server Lifecycle

`commands/serve.py` handles `djinn serve` mode validation, the start/pair gates
and literal output. `core/serve.py` owns host inspection, password storage,
helper execution and the snapshot shared by `djinn serve --status` and
`djinn status`. `core/serve_helper.py` owns container process state. The host
reads that file as text, caches it and sends it on **every** helper call:

```text
docker exec [-i] <inspected-container-id> python3 -I -c <source> <subcommand> [args]
```

The source is at most 100,000 bytes, standard-library-only and compatible with
the image's Python 3.11. No image or entrypoint change is required. `-i` is used
only for OpenCode start, with one JSON line containing `password` on stdin
(4 KiB input cap). Other captured calls have stdin closed. The helper emits
exactly one JSON object plus a newline: exit 0 with `ok: true`, or exit 1 with
`ok: false`, `code` and a secret-free `message`.

| Subcommand | Successful reply |
| --- | --- |
| `status` | `pid1_start`, `busy`, and `servers` keyed by `claude` and `opencode`; each row has `state`, `reason`, `pid`, `since`, `last_line`. OpenCode adds `listening`, `discovery_pid_ok`, `foreign_listener`, `relay_refused` (int or null), `relay_note` (string or null); for an exited OpenCode, `last_line` is the relay's `relay failed:` line when that ends the relay log. |
| `claude-start --prefix <slug>` | `name`, `result`, `pid`, `since`, `log_tail` |
| `opencode-preflight` | `ok` only; reads environment, settings paths, service allowlist and route_localnet without changing files or processes |
| `opencode-start --port 4096 [--cors <url>]` | The start fields plus `stopped_previous` |
| `stop --name <name> [--name <name>]` | `results` keyed by requested name, each with `result` and `pid` |

The host validates reply shapes and keeps copies of the helper's frozen enum
sets; a schema test checks their equality:

| Field | Frozen values |
| --- | --- |
| State | `running`, `degraded`, `exited`, `stopped` |
| Start result | `started`, `already-running`, `restarted`, `exited` |
| Stop result | `stopped`, `not-running`, `survived` |
| Error code | `state-dir`, `state-path`, `env-root`, `password`, `service-json`, `service-config`, `remote-config`, `route-localnet`, `spawn`, `not-registered`, `busy`, `timeout`, `usage` |

#### State and Process Identity

`/tmp/djinn-serve` is outside volumes and binds, mode `0700`, opened with
`O_DIRECTORY|O_NOFOLLOW` and checked for owner and permissions. Start/stop
serialize on its `lock` with `flock`; the lock wait is at most 10 s. Status
tries the lock without waiting and sets `busy` if it is held. Records and logs
use directory-relative access, `O_NOFOLLOW` and private files; record replacement
is atomic. Records include leader PID/start time, Claude child PID/start time,
PID 1 start time, working directory, argv tag, start timestamp, server port and
OpenCode's CORS origin (or null), plus its relay PID/start identity. Old OpenCode
records without relay identity are rejected on decode; their wildcard listener
on 4096 is foreign, with a literal stop/recreate hint, never adopted.

PID 1's changed start time detects a container restart even when `/tmp` survives.
A reused recorded PID also makes the record stale: status reports `stopped`
and removes it when it owns the lock, without signalling, including when the
reused PID is a zombie. A matching zombie follows the leaderless-group rule.
With the same PID 1 and all recorded groups gone, the state is `exited`; the record and last
sanitized log line remain until the next start/stop. A leaderless group still
counts as alive when its remaining members have suitable start times.

OpenCode is `running` only when both its backend and relay groups are alive,
the backend owns `127.0.0.1:4097`, discovery names that backend PID, and the relay
owns the port 4096 listener. A stale identity in either group makes the whole
record stale and neither group is signalled. If at least one group remains,
any missing part is `degraded` (for example `relay gone`, `relay listener lost`,
`opencode gone`, `listener lost` or lost discovery); only both groups gone means
`exited`. Stop and replacement stop every live group before record removal,
even when the backend looks exited. Claude requires both supervisor and child
groups; losing either is `degraded`.
Starting a degraded server stops its remains before replacing it; starting a
healthy server is a no-op, except for OpenCode password or CORS origin changes.

The backend launch forks, then the child calls `setsid()` and verifies it is
the process-group leader. It keeps the working directory, redirects stdin to
`/dev/null`, stdout/stderr to the new private log, and closes other descriptors,
including the lock. OpenCode execs directly without a PTY or runtime log cap.
Claude's supervisor uses `pty.fork()` for a child in its own session/group,
sets a 120×40 terminal and continuously drains it without sending input. One
inherited pipe hands the child's PID/start time to the helper within 2 s;
the complete record is written under the lock before startup is reported.
The PTY child blocks before exec on a private gate pipe whose sole writer is the
supervisor. Only after publishing the record does the helper acknowledge it on
a second pipe. The supervisor waits at most 50 s; on a valid acknowledgement it
sends the exact go token to the child. EOF, an error or a wrong token prevents
exec. Only the required descriptors survive each fork; the acknowledgement's
write end stays with the helper, and supervisor death closes the child's gate.
Every failure after spawning and before record publication uses one launch guard,
including the leader/handoff handshakes (`spawn`) and record construction or
publication (`state-dir`). It immediately KILLs the unreaped direct child and its
process group (including a group created late) while the zombie still reserves the
PID, then reaps it within a separate 3 s cleanup budget. Claude's PTY child group is also KILLed after checking its
start-time identity. The guard closes acknowledgement without sending it before
killing. No TERM grace is given to an unpublished launch; a warning
says the process may survive only when a proven member remains alive after that
budget. The helper's 50 s deadline plus this 3 s reserve stays below the host's
60 s execution timeout.
The supervisor forwards TERM/INT, waits for its child and caps its log at
1 MiB by truncating to a marker before continuing with fresh output. It ignores
SIGPIPE so a broken handoff pipe triggers child-group cleanup; only exec children
reset SIGPIPE and SIGXFSZ to their default dispositions.

OpenCode readiness waits at most 15 s; Claude gets a 5 s grace period. Log tails
strip ANSI/control characters and retain at most 20 lines. A mutating helper
operation uses one 50 s monotonic deadline including lock wait; start/stop host
execution is bounded at 60 s. Stop verifies identity, sends TERM to the recorded
groups (OpenCode relay then backend; Claude child then supervisor), waits up to
10 s, then KILL and up to 2 s more, all within the deadline. PIDs/PGIDs ≤ 1 and the helper's own group are
excluded from signalling. Servers are not automatically restarted after crashes
or container restarts. A failed readiness cleanup retains the surviving group's
record and reports the failure, allowing status and a later stop to recover it.

#### OpenCode Replacement and Password Delivery

OpenCode start first checks environment roots, symlink-free settings paths and
the `service.json` allowlist (`password` only; `remote` has its own refusal
code). It then reads the stdin password and takes the state lock. A healthy
server with the same password and recorded CORS origin (or neither origin set)
returns `already-running`; a changed password, changed CORS origin or degraded
state stops ours before merging the password. A failed stop aborts
before that merge.
Rerunning while no tailnet URL is known (unknown tailnet state or no Serve entry)
starts without the CORS origin and therefore restarts a server that has one;
this affects availability only, since CORS only narrows access.

The host generates a stable password once in
`$XDG_STATE_HOME/djinn/serve/opencode-password`, using `~/.local/state` when
`XDG_STATE_HOME` is unset; an empty value refuses. It uses a private temporary
file, fsync and an exclusive hard-link publication. A concurrent
creator reads the winner. Reads require a user-owned regular file, mode `0600`,
20–256 bytes. Before creation or reading, an lstat walk refuses symlink components
of the password path and paths inside the config root, local/shared zone roots, backups
or running-container bind sources or volume host storage, including local-driver
bind aliases. Those roots count at their resolved location, so a
symlinked dotfile bind does not refuse; a root the user cannot traverse (rootful
Docker's data directory) counts at its lexical location, because the password walk
itself refuses a password beneath it; other undeterminable roots/sources refuse. Keep `XDG_STATE_HOME` local and
outside other sync, indexing and backup tools. The host state directory reuses
`hostctl.state_root(create=False).parent`, with a private `serve` child, and is
included in host-sealing protection. The password travels only on stdin into
an atomic mode-`0600` `~/.config/opencode/service.json`; it is never supplied in
argv, container environment, `opencode service set` or Djinn logs. OpenCode
reads it from the file and also stores it in its own discovery file in the
container's writable layer. Neither file is in the synced carrier allowlist.

A pre-existing default service is stopped only after the discovery PID is
proved to have argv[0] basename `opencode` or `opencode.exe`, followed by
`serve --service`, and own the LISTEN socket named by
the discovery URL. Before password changes or default-service signals, a
non-loopback listener on relay port 4096 refuses with the stop/recreate hint;
this includes wildcard and IPv4-mapped addresses. Verified loopback default
services can still be replaced. The service's start time is checked again before
KILL. Unprovable or stale discovery signals nothing. Djinn never invokes native
`opencode service stop`, which trusts the discovery PID without these checks.
It starts `opencode serve --service --hostname 127.0.0.1 --port 4097`, optionally
with `--cors <tailnet-url>`; bind/port are flags, not persistent service settings.
Discovery names `127.0.0.1:4097`. A registration race fails with `not-registered`
after stopping the new pair. After default-service replacement, any remaining
listener on port 4096 or backend port 4097, at any IPv4 or IPv6 address, refuses
before password changes or spawning.
After stop/restart, the next in-container client can start the normal loopback
service again.

#### Gateway Relay

OpenCode's relay is forked from the helper's already-loaded Python code, in its
own session/group; no runtime root or image change is needed. It binds
`[::]:4096` with `IPV6_V6ONLY=0` when IPv6 is available, otherwise
`0.0.0.0:4096`, and splices bytes to `127.0.0.1:4097` without interpreting them.
The host's loopback publication and the `opencode-start --port 4096` protocol
argument stay unchanged. The relay is independent of `--firewall`.

Launch waits for the OpenCode leader, then forks the relay. A handoff pipe
reports that the relay has bound its listener; the relay accepts nothing until
the helper publishes the complete pair record and writes ACK on a separate
pipe. Only then does readiness check both groups, backend listener/discovery
and relay listener ownership. The helper alone retains the ACK write end;
the relay closes inherited copies and the helper's lock fd. Before publication,
the launch guard closes ACK unwritten and kills/reaps both unreaped direct
children and their groups. After publication, early backend or relay death
reports `exited` with the log tail and stops the surviving part; a failed cleanup
retains a recoverable record. Stop, degraded replacement, password rotation and
CORS changes operate on the pair, with the existing identity checks, signal
floor and TERM/KILL fallback. Container restart makes the record stale.

For each new connection the relay normalizes IPv4-mapped IPv6 to IPv4 and reads
`/proc/net/route` or `/proc/net/ipv6_route` for that family. It admits exactly the
single eligible default gateway: destination `0.0.0.0/0` or `::/0`, both
`RTF_UP` and `RTF_GATEWAY` set, `RTF_REJECT` clear, non-unspecified next hop;
IPv4 hex fields are little-endian. No eligible row, several eligible rows,
malformed or unreadable routing state refuses with `gateway undetermined`.
IPv6 link-local or otherwise scoped gateways and peers with nonzero scope IDs
refuse. Genuine IPv6 packets with IPv4-mapped source addresses are normalized
the same way and are not distinguished from IPv4 connections.

There is no cached authorization: a route/network change is checked on the
next connection, so an old gateway address reused by a peer is refused. Reading
the route and admitting the connection are **not atomic**; an administrative
route change racing that connection is not distinguished, and no duration
bound is promised. Proc exposes only the first next hop of a multipath route;
multipath is not detected and other next hops are not admitted. For a container
on several networks only the default-route gateway of that family is admitted;
a host publication through another network fails closed. Host-local connections
to the container IP arrive as the gateway and are admitted. See the
[source-address assumptions and residuals](SECURITY-MODEL.md#remote-agent-server-access).

A single-threaded `selectors` loop owns all connections, refusal logs and the
count. It permits at most 64 admitted connections; non-gateway refusals consume
no slot, and excess admitted peers are accepted then closed and counted as
limit refusals. Backend connect timeout is 5 s, idle timeout is 30 minutes with
no bytes in either direction, and buffers are capped at 64 KiB per direction.
Half-closes propagate. SIGPIPE stays ignored; EPIPE, reset and timeout close
only the affected connection. TERM/INT close listener and connections and exit 0.
Persistent accept errors pause new accepts for one second while established
streams keep flowing, with one capped diagnostic per pause. Would-block,
interrupted and aborted accepts retry without a pause.

Refusal logs contain only source IPs and reasons, with userland-proxy/direct-routing
and non-default-network hints where applicable; no content is logged. Refusal
logs are rate-limited to the first per source per minute and capped like the
other managed logs. The source map holds at most 256 entries; after expiry,
new sources beyond that cap are counted without a source log line, with at
most one `refusal log suppressed: too many sources` line per minute.
Every refusal increments the count independently of log
rate limiting, starting at zero on each relay start. Private
`opencode-relay.count` is atomically replaced at most once per second and on
exit, with a heartbeat at least every 10 s. Status trusts a count only when its
relay identity matches the record and its heartbeat is at most 30 s old;
otherwise `relay_refused` is null and `relay_note` explains missing, unreadable,
foreign-identity or stale data. Failed writes log once per failure streak and
retry without resetting the count. The host validates both fields and displays
the count since start or its unknown note in the OpenCode row, never a fabricated
zero. New degraded reasons pass through as literal text.

#### Exposure Gates and Tailnet Discovery

Host checks run in this order: mode/names, config, container inspection,
detached marker, then (for every start) process-state placement: unknown mounts,
mounts overlapping `/tmp/djinn-serve` and any `DJINN_SERVE_TEST_` container
environment variable refuse. Status, stop and pair skip this placement gate.
Then (for OpenCode start/pair) exact loopback port binding,
environment roots and mounts, every attached network, Engine version and Funnel.
All host start gates and the non-mutating `opencode-preflight` finish before
password creation or any server start. The start loop ends on its first failure.
Inspection distinguishes absent from unknown, validates the full container ID
and uses that ID for every exec. The helper's settings checks above run during
preflight and again on OpenCode start; pair instead requires helper status
`running` and a tailnet URL.
Status/stop do not require the OpenCode exposure gates; non-detached status
reports unavailability without executing the helper.

Network inspection requires user bridge networks without trusted host
interfaces or `routed`/`nat-unprotected` gateway modes. The same inspected
`HostConfig.Sysctls` and every attached endpoint's
`DriverOpts["com.docker.network.endpoint.sysctls"]` must not set any
`net.ipv4.conf.*.route_localnet` to a value other than `0`; start/pair refuse with
code `gate` and a literal disable/recreate hint. The helper's preflight and start
also read `/proc/sys/net/ipv4/conf/all/route_localnet` and every interface entry,
including `default`; any nonzero integer, non-integer or unreadable value refuses
with `route-localnet`. Effective interface routing is `all OR interface`, so
either being nonzero suffices; nothing starts. This also catches values a new
container inherits from the host's `all`/`default` entries. The host maps the
helper code to a literal refusal that names the host as a possible source. There is no runtime watchdog (see the security boundary).
The Engine gate compares the leading version triple against 28.3.3 and refuses
unknown versions. The daemon-wide routing/iptables prerequisites cannot be
checked through this API;
see [the residual boundary](SECURITY-MODEL.md#remote-agent-server-access).

`tailnet_state()` only runs `tailscale serve status --json`, with closed stdin
and a 5 s bound. It traverses the root and recursive `Foreground` layers,
unioning `AllowFunnel` keys across them before matching handlers or TCP forwards.
For Funnel detection, any target naming the host port counts regardless of
scheme, host or path. The HTTPS URL requires a root handler, an HTTP proxy to
`127.0.0.1`, `localhost` or `[::1]` at that port, and `TCP[port].HTTPS: true`.
Validated candidates are selected by sorted key; port 443 is omitted in the URL.
Detected Funnel refuses start/pair and warns in status, regardless of where
parse anomalies affect unrelated elements. Without confirmed evidence,
missing/unreadable Tailscale status leaves Funnel unknown and allows start with
the printed note
`Funnel state unknown (<reason>)`; pairing still needs a discoverable URL.
Parsing is fixture-tested; live host configuration is a rollout check.

Pairing runs `opencode pair --url <url>` in the inspected container with closed
stdin, inherited stdout/stderr and a 30 s bound; pairing output is not captured
or logged. Helper/log/Tailscale strings are printed literally with control
characters escaped, and the CLI disables traceback local-variable display.

## Assistant Audit

`commands/assistant.py` runs `core/assistant.py` independently of the dev image,
Compose lifecycle and workspace delivery. `assistant.agent` selects Claude,
Codex or OpenCode; `--agent` overrides it without saving configuration. Invalid
TOML, zones and missing workspace paths fall back to derivable default mounts
and Claude (unless overridden), with the loader error in the initial message.

`assistant/Dockerfile` installs the selected native CLI and basic diagnostic
tools. OpenCode's native npm archive is checked against the registry's `sha512`
integrity before extraction by `assistant/install-opencode.sh`. The selected
agent uses the shared effective version policy, independent of AppConfig
recovery; `DOCKER_VERSION` stays an upstream-only Dockerfile pin.
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
customizations; OpenCode runs `opencode --standalone --prompt <prompt>` with v2
`permissions` asking before `edit` and `shell`, without the personal carrier;
Codex keeps
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
combines canonical and start runtime roots: `PASS` when clean, otherwise `WARN`
with named items and the shared remedy.

`OpenCode runtime` describes the container-local delivery rebuilt at every
`djinn start`. Absent/stopped dev is `PASS` for that lifecycle, without a runtime
inspection. Running dev first passes the publisher image-label gate, then runs
the publisher `--check` via `docker exec` with closed stdin and a bounded
timeout. Only exit `0` is clean; known non-zero drift exits warn with item labels.
Unknown exits, malformed diagnostics, timeout or unavailable Docker warn
`check unavailable`. Seed mismatch is `source-changed`, with sync then
recreation advice. Session refresh uses the same argv builder for publication;
runtime target drift names `opencode-runtime:<relative>` and advises keeping
wanted edits in the selected source, then `djinn clean` and `djinn start`.

Workflow preparation for start, run and session inspects every delivery target
before canonical auto-sync or runtime publication. Host directory provisioning
still precedes this workflow check after the image gate. Host-fallback drift
refuses before confirmation. A successful auto-sync produces
`Synchronized source changes into the canonical workflow: <n> changed, <n> removed.`;
all callers print the notice even when a later step refuses.

With valid config, the read-only `Managed bind sources` row calls
`managed_bind_problems()`: `PASS` prints `no overlap with host execution inputs`;
`FAIL` joins every problem and remedy with `; `. `run_checks()` contains
`get_config_root()` resolution errors (`OSError`, `ValueError`, `KeyError`,
`TypeError`, `RuntimeError`) as a `Config root` FAIL with the cause. It skips the
dependent SOPS, credential/zone-mode and zone-diagnostic rows, so a config-root
failure does not prevent the managed row or remaining table from rendering.
Zone diagnostics contain `OSError`/`ValueError` as a `Zone configuration` WARN.

Zone diagnostics include `_codex_daemon_checks()` against the resolved
`<local_root>/codex/` without invoking Codex. It inspects directory components
without following symlinks, strictly reads regular settings up to 64 KiB and
bounds release traversal to 200,000 entries.

- `Codex daemon updater`: PASS for `updater.autoUpdateEnabled=false`, or absent
  settings with no managed copy; WARN for true/unset/invalid keys, missing
  settings with an installed copy, or malformed/oversized/symlinked settings.
  `remoteControlEnabled` is displayed as on/off/unset information only.
- `Codex daemon releases`: release count, total size and current selection;
  PASS for zero or one release, WARN for multiple releases, unreadable paths,
  an incomplete scan, or a `current` link outside the helper's rule (a direct
  `releases/<name>` child without `..`, holding a regular manifest). Details
  escape control characters from container-writable names. A skipped `packages` or `app-server-daemon` default overlay
  makes both rows WARN with `not inspected: … overlay skipped`.

These rows are read-only and have no `--fix` action; their remedy points to a
dev-container start, where the entrypoint applies the policy.

The `Post-mortem records` row calls `post_mortem.latest()` independently of config
and Docker availability. It reads only the host filesystem, returning `PASS`
with `none` or `<n> kept; latest <path>`. A historical incident is not a current
failure. Directory ownership/mode violations or read failures yield `WARN` with
a recorder-owned code; no record contents are printed.

`doctor --fix` calls `_doctor_fix(config)`, which attempts:

- `seed_config(project_root)`
- `ensure_host_env(config)`
- `ensure_network()`

Directory-mode repair reopens each finding from its zone root without following
links and applies `fchmod(0700)` to the opened directory. A swapped or refused path
is reported as `Could not fix` with its path; outside targets are never chmodded.
Provisioning refusals are reported with their kind and path.

It exits non-zero when hard checks or repairs fail.

`preflight(config)` first verifies Docker is installed and the daemon is
reachable. Only after Docker is usable does it provision host directories with
`ensure_host_env(config)`. It does not call `seed_config()`; this keeps
Docker-down failures from creating unrelated host artifacts and preserves the
workflow seeding boundary.
Preflight does not run the daemon diagnostic rows or apply the daemon policy.

## Agent Commands

`commands/agent.py` implements headless one-shot agent runs.

`build_agent_command(agent_config, write, json_output, model)` assembles a shell
command string from `AgentConfig`. It appends the prompt template, which expands
`$AGENT_PROMPT` inside the container. An explicit `model` takes precedence;
otherwise the command uses `AgentConfig.default_model` when configured.

`run()` refuses `--docker` with exit 1 before loading config or preparing workflow;
[the diagnostic](README.md#docker-access-modes) names the supported alternatives.
It then loads app config and calls `validate_managed_bind_sources()` immediately
after `load_config()`, before `get_config_root()` or `prepare_config_workflow()`.
It prints a `MountSpecificationError` through `error()` and exits 1; mount errors
from this gate, source resolution and the creator are escaped with
`rich.markup.escape()` before printing. It then
runs the shared workflow preparation for Claude/Codex/OpenCode and accepts
repeatable `--mount SRC[:DST[:ro|rw]]` values; `run_headless_agent()` loads the
agent definitions and validates the requested agent without calling
`ensure_network()`. Compose creates the guest network on demand. Without an
explicit mount it keeps the implicit current-directory mount at
`/home/dev/workspace`; with explicit mounts it uses their resolved targets and
the first target as the workdir. It then calls
`compose_run(..., interactive=False, env={"AGENT_PROMPT": prompt})` and reports
the complete resolved mount collection before execution.

`agents()` lists configured agents, with verbose and JSON modes. The JSON mode
writes `json.dumps(definitions, indent=2, sort_keys=True)` plus a newline
directly to stdout, with every `AgentConfig` field including `prompt_template`,
so the bytes do not depend on the terminal width.

`claude-isolated` keeps its five isolation flags in both `read_only_flags` and
`write_flags`. The headless builder, the container session's headless builder
and its interactive builder therefore all carry them. Its `prompt_template` is
`-- "$AGENT_PROMPT"`. `scripts/probe-claude-isolated.py` is the maintainer
re-probe described in the README.

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
environment, additions, and fixed terminal values, and for OpenCode a final
`OPENCODE_DISABLE_PROJECT_CONFIG=1` that overrides both; discovery, host Git setup,
and image/workflow preparation receive no additions. Container starts use
name-only `docker exec -e NAME` forwarding with values in the subprocess
environment. The Docker connection, target, prompt transport, and TTY behavior
retain their existing contracts. Container sessions launch non-login `bash -c`
shells that inherit the image PATH and forward the additions to their descendants.
Map values stay out of argv and Djinn-authored logs/errors; inherited credentials
are not scrubbed and child output is preserved. OS, daemon, descendant, and profile
visibility, or values independently supplied in prompts, are outside this
secrecy guarantee. No provider selection, authentication checks, or CLI flags
are added.

Host workspaces live under:

```text
~/.djinn/sessions/<project>/
```

The `project` name must match `^[a-zA-Z0-9][a-zA-Z0-9_.-]*$`, enforced by
`core/session.py::SessionManager.__init__()`.

With `--create`, the command creates a missing sessions root under trusted
ancestors, then opens it without following links. Every workspace, including an
existing one, is validated through a no-follow descriptor walk. Missing workspaces
are created with the usual umask; linked roots/workspaces and non-directories
refuse with the path and exit 1. Existing-file refusals keep their removal remedy.
Without `--create`, the existing resolve/containment check remains and the workspace
must already exist.

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

In host mode, `run_interactive()` and `run_headless()` call
`_guard_host_workspace()` after target resolution selects the no-container
branch and before building or launching the agent command. The guard checks, in
order:

1. The agent must be Claude, Codex or OpenCode, and the workspace top level
   must hold none of the entries the selected agent would load.
2. It resolves the workspace and `~/.djinn/sessions`; a path below the
   sessions root must resolve strictly below it, as in the CLI. The
   workspace root must not hold `HEAD` (Git could take it as a bare
   repository); for a workspace below the sessions root, every physical
   ancestor from its parent through that root must contain neither `.git`,
   `HEAD` nor selected-agent project entries.
3. Only after those checks, it runs `git init` if `.git` is missing, then
   checks the repository shape, config, hooks and legacy destination carriers.
   `.git` must be a real directory with regular `HEAD` and real `objects` and
   `refs` directories, no `commondir` or `modules`, allowlisted config keys,
   network-only remote destinations (every remote has a `url`; no remote name
   starts with `/`), only `*.sample` hooks, and absent or real
   empty `remotes` and `branches` directories.
4. It refuses any `.git` entry below the workspace root, including files and
   symlinks. This conservative walk refuses nested repositories and
   submodules, so workspaces containing a nested clone cannot start in host mode.

Repository inspection reads files and invokes
`git config --file <workspace>/.git/config --null --list` with cwd outside the
workspace; it does not run workspace-configured commands. The only separate
Git command is `git init` when `.git` is absent, after the earlier checks pass.
Host Claude appends `--setting-sources user`
after configured flags and the model flag, immediately before the prompt. Host
OpenCode sets `OPENCODE_DISABLE_PROJECT_CONFIG=1` after inherited and caller
environment values, and still refuses `.opencode`, `opencode.json`,
`opencode.jsonc`, `tui.json` and `tui.jsonc`. Host Codex refuses `.codex`; other
configured agents are refused on the host and run unchanged in the container.

An unsupported agent, offending entry, invalid repository state or inspection
failure refuses before launch. Both run methods return
`SessionResult(returncode=1, stdout="", stderr=<message>,
workspace_dir=<workspace>)`; the CLI prints the message on stderr and exits 1.
It identifies the offending entry, key or destination and advises running in
the container or reviewing and removing the entry on the host. Container-mode
starts do not run this guard and retain their existing behavior.

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
  `djinn-vscode-workspaces`, `djinn-packages`
- `SYNC_PATHS["credentials"]`: `claude`, `codex`, `opencode`, `gh`, `age`
- `SYNC_PATHS["repo-dotfiles"]`: `repo-dotfiles`

Codex SQLite in the local zone is outside these config-root archives, including
durable goals, queued messages, preferences and memory-extraction state. Shared
rollouts and generated memory text are not a complete backup of that state.

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

Post-mortem records in `~/.local/state/djinn/post-mortem/` are outside every backup
selector and are never archived. Operator-managed backups and synchronization
must also exclude `~/.local/state/djinn`.

`restore()` also refuses to run while containers are active. It identifies an
age archive from its `age-encryption.org/v1` header, decrypts it into a separate
restore-staging subdirectory, then extracts the outer tar. Cleartext gzip
archives written with `--no-encrypt` remain supported. Discovery covers root
archives and `declared-volumes/*.tar.gz`. Classification order is binding:

1. Declared namespace: restore into the actual volume name only if currently
   declared as data/cache; removed or current `none` warns by name and skips
   without creating or clearing a volume. A data/cache category change does
   not affect restore: the archive contents determine selection, with no selector.
2. Root `djinn-sync-*`: config-root sync route, only for names in `SYNC_PATHS`.
3. Other root members: existing validated volume-name route, unchanged.

The raw volume restore helper receives the declared subdirectory as its archive
source; it gets no dev mounts/environment. No current-declaration filter applies
to root members: a `sync-claude` declaration at `none` skips its namespaced
volume archive while root `djinn-sync-claude.tar.gz` still restores credentials.
There is no manifest, arbitrary-bind restore, compatibility adapter or migration.
`none` means excluded from backup/restore, without protection from `clean all`.

Both commands hold `control_guard()` then `creation_guard()` from before the
running-container check through staging cleanup. Concurrent dev creation is
blocked; a pending creation or busy control guard makes the command fail.

Clean discovers configured sync entries with `lstat`, including dangling links
and wrong types. Clearing opens the root without following and removes children
through directory descriptors. Links are unlinked; swapped directories refuse.
Every sibling is visited. Only empty mount placeholders that cannot be removed
with `EACCES`, `EBUSY` or `EPERM`, and their ancestors, survive a successful clear.
Any other unremovable entry fails it.

Restore creates a missing sync root under the trusted config root. Linked or
non-directory roots, unknown names and failed clears are failed items without
extraction or hardening. Admitted items extract with tar, then harden real
directories to `0700` even if tar fails. The hardening walk tightens each real
directory to `0700` before reading it, does not follow links and runs under the
lifecycle guard. Hardening failures fail that item and the
loop continues; any failed item makes restore exit non-zero.

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
  +-- optional pre-seed firewall initialization (djinn run --allow: the root wrapper
      applied and locked the rules before this entrypoint; it only checks the lock)
  +-- repair writable volume ownership
  +-- Seed & Config: merge Claude settings and publish the OpenCode workflow
      from the read-only canonical mount
  +-- MCP: register MCP servers and box third-party CLI output
  +-- Codex daemon: disable updater, reconcile image version, prune old releases
      (no command arguments; failures warn and continue)
  +-- Packages: dev wrapper calls root helper replay for the root-owned persistent list
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
