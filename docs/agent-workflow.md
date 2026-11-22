# Agents and workflow

Choose agent defaults and carry the same workflow across the bundled tools.

[Back to the README](../README.md)

## Global Agent Workflow Ownership

Select one native global workflow per deployment.

```sh
djinn config set config_sync.source claude   # or codex / opencode
djinn config sync
djinn config status  # exit 0 when clean, otherwise exit 1
```

When changing the source, switch, run `djinn config sync`, then edit. Sync requires a valid source and refuses
edited managed targets or unmanaged items at managed paths. A shared deployment uses one source for all users.
`djinn run` and `djinn session` select their agents independently. The source lives in its native project-local root.

| Tool | Authoritative root instructions | Native agents | Native commands |
| --- | --- | --- | --- |
| Claude Code | `config/claude/AGENTS.md` | `agents/*.md` | `commands/*.md` |
| Codex | `config/codex/AGENTS.md` | `agents/*.toml` | `skills/command-*/**` |
| OpenCode | `config/opencode/AGENTS.md` | `agents/*.md` | `commands/*.md` |

Read each directory's agent material from its `.agents/` before working there. Global skills, agents, commands,
context and scripts stay in native locations. Codex uses `project_doc_fallback_filenames`. Claude reads global
instructions only from `~/.claude/CLAUDE.md`, supplied as an `@AGENTS.md` bridge in dev and on host fallback.

Projection also covers `skills/<name>/**`, `context/**` and `scripts/**`. The known startup, security and ready hooks
and their `SessionStart`, `PreToolUse` and `Stop` registrations stay native, as does Claude's `/codex-review`.
They are optional, author-owned, validated when present and never stale-removed. Djinn checks managed hook source
paths and their paired Claude and Codex registrations. Repository-local instructions, agents, skills and commands
remain outside projection and rewriting. See
[Native-only workflow artifacts](../IMPLEMENTATION.md#native-only-workflow-artifacts).

### OpenCode 2

See [OpenCode 2](opencode-v2.md) for the `@opencode/cli` v2 pin, settings, themes, providers, endpoints and plugins.
See [Remote Agent Servers](remote-agent-servers.md#remote-agent-servers) for browser UI, API and remote clients.

### Tool-Owned Runtime State

See [Codex](#codex) for its managed updater key and [OpenCode's personal carrier](opencode-v2.md#personal-carrier)
for allowed files, strict JSON, checkpoints, warnings and theme removal.

Djinn atomically checkpoints Claude state and both tools' personal settings every 30 s and at clean stop or
`docker stop`, requiring exactly one valid JSON document. On writable, healthy storage, the crash recovery window
is about 30 s plus checkpoint duration and scheduling delay. Unchanged runtime files preserve host edits, including
`settings.local.json` at clean stop. Runtime wins when both change. Without checkpoint references after failed
setup or lost private state, final sync writes all valid files except protected OpenCode paths, which can overwrite
host edits or restore deleted files. If private state cannot be recreated at stop, Djinn warns and writes nothing.

Crash residue `.djinn-settings-*` is excluded from restore and projection. `djinn doctor` reports it as zone drift
in config-root `claude/` and `opencode/`. Residue beside the seed overlay in `config/claude/` is unreported.

With the default Claude source, writable `config/claude/skills` and `config/claude/scripts` support in-place edits
and also receive CLI writes. Djinn excludes the following runtime trees when reading workflow sources.

| Path | Written by | Treatment |
| --- | --- | --- |
| `skills/synced/**` (Claude source only) | Claude Code, syncing account skills | Left in place for Claude, never projected to Codex/OpenCode |
| `skills/.trash/**` (Claude source only) | Claude Code, moving stale account skills out of `skills/synced` (it deletes them itself later) | Left in place for Claude, never projected to Codex/OpenCode |
| `**/__pycache__/**` (any source) | any Python interpreter | Left in place, never read |

Excluded files are kept and cannot trigger drift or block `djinn start`. Other binary files, including a `.pyc`
outside `__pycache__` or a `.pyd` in a skill, are reported as non-UTF-8 sources. With Codex or OpenCode as source,
`synced` is an ordinary skill name and `.trash` is invalid.

The runtime publisher receives each complete native view, including hooks, plugins and registrations, with Claude
host-path rewriting and the Compose settings merge. Workflow ownership excludes credentials, auth, history,
caches, themes, UI policy, arbitrary plugins, `PostToolUse` and status-line settings. See [MCP](#mcp) for its
separate registry.

The publisher uses one schema for canonical `config/.djinn-config-sync.json` and each managed runtime root's
`.djinn-workflow-state.json`. The manifest records the selected source. Each file or carrier key records only its
content hash and executable flag. Operators own neighboring JSON or TOML keys.

Whole managed runtime files and the runtime manifest use `0444` (`0555` for executables). Shared carriers
(`settings.json`, `config.toml`, `opencode.json`) and the canonical tree keep their mode handling. Delivery reseals
writable files. Permission changes alone cause no drift. Modes block in-place writes such as Codex and OpenCode
edits or shell redirection. Rename-based replacement by Claude Code Edit or `sed -i` still works and triggers drift checks.

Read-only `djinn config status` audits the canonical tree and `djinn start` runtime roots (currently
`<config-root>/codex`, since Compose Claude uses direct mounts). Drift names the root and item, for example
`codex:AGENTS.md (<config-root>/codex)` with `[key.path]` for a carrier key. Output is sanitized and excludes
workflow and settings bodies. Status exits `0` for `clean` and `1` otherwise. `djinn config sync` writes canonical
views and exits non-zero when blocked. A successful sync warns about runtime drift, leaves it intact and exits successfully.

| State | Meaning | Remedy |
| --- | --- | --- |
| `clean` | The selected source and all managed views match their manifests. | None. |
| `source-changed` | The source projection changed or was unstable during publication. | Run `djinn config sync`, then retry. |
| `target-drift` | A manifest-managed item was edited. | Keep wanted edits in the selected source `config/<source>/`, stop running Djinn containers (`djinn clean`), then run `djinn config sync --discard-target-drift` (backs up edited files first). |
| `collision` | An unmanaged item occupies a managed path. | Move or remove the conflicting unmanaged workflow item, then retry. |
| `invalid-or-semantic` | The source is invalid, empty, or contains a non-portable artifact. | Author or edit the artifact natively in the target tool's view, or make the source form portable. |

Sync invokes no provider. `start`, `run` and `session` first check every delivery target read-only. They repair only
deterministic `source-changed` projection drift and stop on other failures before an agent starts. Canonical auto-sync prints
`Synchronized source changes into the canonical workflow: <n> changed, <n> removed.`,
even if a later step refuses. Preflight, status, audits and sync leave source roots alone. See
[seeding](configuration.md#the-blank-space-and-seed-model) for `djinn init` and `djinn doctor --fix`.

`djinn config sync --discard-target-drift` backs up changed managed items in the canonical and start runtime roots,
restores them, and applies pending source changes. Collisions and invalid states are refused. Stop Djinn containers
with `djinn clean` first. The lifecycle guard covers the fail-closed running check through the final publish.

Backups use passwd-home `~/.local/state/djinn/workflow-drift/<UTC>-<pid>/` regardless of `HOME` or `XDG_STATE_HOME`
with `0700` directories and `0600` files. A changed key copies its whole carrier. Missing items are reported without
a copy. All backups and parent directories must be durable before overwriting. The command prints each item and the
backup directory. Backup failures leave partial copies and preserve targets. Without target drift, plain sync runs
and creates no backup directory. The location cannot overlap the installation, config or sync-zone roots, `code_dir`,
sessions or declared bind sources. Djinn excludes it from backup, sync zones and dev mounts. Keep
`~/.local/state/djinn` out of your own sync and mounts too.

Discard excludes host-fallback roots. Keep wanted edits in the selected source and sync, then follow each item's
restore source or `remove` action for retired files or keys. Keep the runtime manifest to preserve ownership and
retirement tracking. For an unrewritable managed multiline TOML value or table, use a single-line value or delete
that key, preserving personal neighbors, then retry.

Claude, Codex and OpenCode host fallbacks receive the selected canonical view through the publisher. It rebuilds
container-local `~/.config/opencode` at every `djinn start` and refreshes it before an OpenCode container session.
For `opencode-runtime:<relative>` drift, keep wanted edits in `config/<source>/`, then run `djinn clean` and
`djinn start`. An image without `djinn.workflow.publisher=2` stops preparation with `Rebuild/recreate required.`
before Compose starts or a running-container refresh. Compose Claude instead uses direct mounts, including
`AGENTS.md`, and a settings merge. Its runtime root has no publisher writes or manifest.

### Workspace AGENTS.md chain

Compose exports the workspace target `/home/dev/<workspace>` as `DJINN_WORKSPACE_TARGET`. Tools load `AGENTS.md`
between that root and the working directory in different ways.

- **Codex** loads from the Git root down. Djinn's `SessionStart` hook in `/etc/codex/config.toml` adds only
  `AGENTS.md` files between workspace and Git root at startup, after `/clear` and after compaction. Resumes and
  forks already carry them. The hook needs no trust prompt, runs alongside user hooks, and cannot be replaced by
  them. `requirements.toml` enforces `[features] hooks = true`. Output is capped at 64 KiB and names files that do not fit.
- **Claude Code** loads the full chain, but a single project `CLAUDE.md` anywhere makes it skip `AGENTS.md` by default.
  The seed `config/claude/settings.json` sets `pluginConfigs."agents-md@builtin".options.instructionFiles` to
  `"claude-md-and-agents-md"` to load both without double-loading imports. Change or remove it in the source,
  or override it in `settings.local.json`.
- **OpenCode** loads from the working directory to `$HOME`, covering the full chain without a Djinn hook.

`tests/test_agents_chain_native.py` checks these rules in dev and skips checks for missing CLIs.

## Agents

| Agent name | Binary | Default headless mode | Notes |
| --- | --- | --- | --- |
| `claude` | `claude` | `claude -p` | read-only uses `--permission-mode plan`; write mode uses `--dangerously-skip-permissions` |
| `codex` | `codex` | `codex exec` | read-only uses `--sandbox read-only`, in which Codex cannot run shell commands inside the container; write mode uses `--dangerously-bypass-approvals-and-sandbox` |
| `opencode` | `opencode` | `opencode run` | read-only uses `--agent plan`; model flag is `-m` |
| `claude-isolated` | `claude` | `claude -p --no-session-persistence` | Claude Code without user or project context; see [Minimal-context Claude agent](#minimal-context-claude-agent) |

All built-in headless commands end with `-- "$AGENT_PROMPT"` so a leading `-` is passed as part of the prompt.

```sh
djinn agents
djinn agents --verbose
djinn agents --json
```

### Minimal-context Claude agent

`djinn run claude-isolated` uses the stored subscription login, which Claude Code's `--bare` mode cannot use.
It loads no tools, MCP servers, `CLAUDE.md` or `AGENTS.md`, user, project or local settings, hooks, skills, commands,
custom agents or auto-memory. Headless runs keep no transcripts.

```text
claude -p --no-session-persistence [--model M] --safe-mode --setting-sources= --strict-mcp-config --disable-slash-commands --tools= [--output-format json] -- "$AGENT_PROMPT"
```

- `--write` adds no tools. Interactive `djinn session --agent claude-isolated` uses the isolation flags and keeps
  a normal transcript because `--no-session-persistence` requires `-p`. Host fallback refuses this agent.
- Claude's environment block still exposes the working directory, Git-repo status, platform, shell and OS version.
- Claude writes `~/.claude.json` and backups plus short-lived `~/.claude/sessions/` files that it removes at exit.
  A headless run writes no transcript, memory or prompt history.
- Re-probe after every Claude Code version change, including in-container updates, because flag behavior may change.

Fingerprint the effective definition to pin it in a pipeline.

```sh
djinn agents --json | jq -cS '."claude-isolated"' | sha256sum
```

`djinn agents --json` prints all effective fields including `prompt_template`, with sorted keys regardless of
terminal width. The fingerprint changes with the definition, including `agents.toml` replacements.

#### Re-probing the isolation

Run the probe from a checkout inside Djinn.

```sh
uv run python scripts/probe-claude-isolated.py            # full probe
uv run python scripts/probe-claude-isolated.py --offline  # without the login
```

First the probe uses a temporary home without login. It expects tools, MCP servers, skills, commands, custom agents,
auto-memory, settings and hooks in the unisolated run and their absence in the preset. After that passes, a few short
runs use the stored login and check that `CLAUDE.md` and `AGENTS.md` canaries stay out of context. At most one run
lacks isolation flags and only `CLAUDE.md` has a positive control. Runs can refresh the token and write app state.
The probe checks for leftover transcript, memory, session environment or prompt history, reports `FAIL` with the
path and deletes nothing. Exit 0 means all checks passed.

### Codex

By default, interactive `codex`, `codex resume` and `codex fork` start the managed daemon. `codex exec` and plain
`codex app-server` (stdio) skip it.

Djinn applies this policy in mounted `~/.codex` on foreground and detached dev starts. One-shot `djinn run` and
`compose run … <cmd>` skip it.

1. Set only `updater.autoUpdateEnabled=false` in `app-server-daemon/settings.json`, creating it if absent.
   Malformed or symlinked files warn and stay intact.
2. If the selected release differs from the image's Codex, run `codex app-server daemon update --from-cli --yes`.
   Skip this step when no release is selected.
3. After validation, keep only the selected release under `packages/app-server-daemon/releases/`.

Failures warn and startup continues. Update with `djinn update`, `djinn build` and a new dev-container start.

Remote control defaults to off upstream. Enable it explicitly with `codex app-server daemon enable-remote-control`
and a ChatGPT login. Djinn preserves that preference. `remote control websocket loop started … initial_desired_state=Disabled`
can appear without a connection. Plugin and model traffic can still reach vendor services with both controls off.
See [the firewall allowlist](access-and-boundaries.md#what---firewall-allows).

### Agent Overrides

The host executes commands from `~/.config/djinn_in_a_box/agents.toml`, the only automatic override path. When
present, it replaces all built-ins, including `claude-isolated` unless defined anew. Use a regular file with one
link and a real parent directory. Both must be yours, free of symlinks and unwritable by group or others. Otherwise
`djinn run`, `djinn agents` and `djinn session` refuse with a reason. Copy the documentation-only seed before editing.

```sh
mkdir -p ~/.config/djinn_in_a_box
cp config/agents.toml.example ~/.config/djinn_in_a_box/agents.toml
djinn agents --verbose
```

Define `[agents.<name>]` with `binary`, `description`, `headless_flags`, `read_only_flags`, `write_flags`,
`json_flags`, `model_flag`, `default_model` and `prompt_template`. `--model` overrides `default_model` for one invocation.

## MCP

Compose mounts optional `config/mcp-servers.json`. At startup, Djinn registers enabled entries for supported
agents. Each probe waits up to 5 seconds. Djinn reports unresponsive endpoints and continues registration.
