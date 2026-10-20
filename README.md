# Djinn in a Box

[![CI](https://github.com/w2kr1stn/djinn_in_a_box/actions/workflows/ci.yml/badge.svg)](https://github.com/w2kr1stn/djinn_in_a_box/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](pyproject.toml)

Djinn in a Box is a Docker-based development environment for running CLI coding
agents with persistent per-tool credentials and a managed container lifecycle.

It ships the mechanism:

- a Docker image with Claude Code, Codex CLI, and OpenCode
- a Python CLI named `djinn`
- Docker Compose files for the base container, proxied Docker access, and direct
  Docker access
- neutral seed templates that are copied into local, ignored files on first run

It does not ship your workflow. The `config/` directory, credential stores,
package lists, installer choices, and agent configuration overrides are yours.
They stay local unless you choose to back them up or mirror them.

## What You Get

Djinn gives you one repeatable container image and several ways to use it:

- Open an interactive shell with `djinn start`, or leave the container running in
  the background with `djinn start --detach` and attach to it later with
  `djinn enter`. Do not background the interactive form with `&`: that leaves a
  TTY-attached Compose client in a background process group, which storms the
  container with SIGTTOU — loading the host and overflowing Docker's event ring
  buffer, so nothing that goes wrong in that container stays diagnosable.
  `djinn start` refuses that shape.
- Run a one-shot agent prompt with `djinn run`.
- Attach another shell to a running container with `djinn enter`.
- Keep reusable session workspaces under `~/.djinn/sessions/` with
  `djinn session`.
- Diagnose the host and seeded configuration with `djinn doctor`.
- Back up and restore the managed volumes and config-root directories with
  `djinn backup` and `djinn restore`.

Credentials are separated by CLI. By default, Claude Code, Codex CLI, OpenCode,
and the GitHub CLI each get their own host directory under the configured Djinn
config root. The container sees those directories at the paths each CLI
expects. An `age` encryption identity directory is provisioned the same way and
appears at `~/.config/age`, so plain `age` keys persist across runs
(`age -i ~/.config/age/keys.txt`).

For SOPS, point Djinn at a key file that stays on this machine instead:

```sh
djinn config set general.sops_age_key_file /path/outside/the/config/root/keys.txt
```

Every start then mounts that one file read-only at `~/.config/sops/age/keys.txt`
(SOPS's default location) and sets `SOPS_AGE_KEY_FILE`. The file must be a regular,
readable file with mode `0600` or `0400`; otherwise `djinn start` and `djinn run`
refuse instead of starting without a key, and `djinn doctor` names the problem.
Keep it out of the config root when you mirror that root across machines: a private
key copied to every replica also lands in every backup of them. Agents can still
read a mounted key, so also deny it in their permission settings, for example
`"Read(~/.config/sops/age/**)"` in Claude Code's `permissions.deny`.

## Requirements

Install these on the host:

- Docker Engine 25 or later, or Docker Desktop with such an engine
- Docker Compose v2.20.2 or later, available as `docker compose`
- Docker Buildx, available as `docker buildx`, used by `djinn build`
- `uv`, used to install and run the Python CLI

The CLI and tests target Python 3.14 through the project metadata. You normally
do not need to manage that interpreter yourself when using `uv`.

## Quickstart

Clone the repository, then install the CLI from that clone:

```sh
git clone <repo-url> djinn-in-a-box
cd djinn-in-a-box
uv tool install --editable .
```

Initialize your local configuration:

```sh
djinn init
```

`djinn init` writes `~/.config/djinn_in_a_box/config.toml`, provisions the host
directories used by Docker bind mounts, and seeds local template files into the
repository `config/` directory if they are missing.

Build the image:

```sh
djinn build
```

Verify the host, config, seeds, network, and image:

```sh
djinn doctor
```

Start an interactive development shell:

```sh
djinn start
```

For browser or remote agent access, use a detached container and follow
[Remote Agent Servers](#remote-agent-servers).

Run a headless agent prompt from your current directory:

```sh
djinn run claude "Explain this project structure."
```

By default, `djinn run` is read-only where the agent configuration supports a
read-only mode. Add `--write` when you want the selected agent to modify files:

```sh
djinn run claude "Fix the failing test." --write
```

## First Authentication

Sign in from inside a normal development shell:

```sh
djinn start
```

Authenticate the tools one by one in that shell by running each agent binary and
following its prompts. Every bundled CLI can sign in without a loopback
callback: the tool prints a URL, you open it in your host browser, and you paste
the resulting code back into the container.

Claude Code and OpenCode select that flow on their own inside the container.
**Codex needs to be told:** plain `codex login` starts a login server on a
container-local port that your host browser cannot reach, and the sign-in never
completes. Use the device flow instead:

```sh
codex login --device-auth
```

In the Codex TUI sign-in screen, the equivalent is the *remote or headless
machine* option.

The GitHub CLI is not an agent binary but shares the same model — run
`gh auth login` in that shell and choose the device-code flow when prompted.

The resulting credentials persist outside the container image, so you only do
this once per tool:

| Tool | Credential location | Backup category |
| --- | --- | --- |
| Claude Code, Codex, OpenCode, GitHub CLI | your configured config root | `credentials` |

`djinn backup` includes credentials by default. When selecting categories,
include credentials.
See [credential security](SECURITY-MODEL.md#credentials-backups-and-outbound-traffic)
for host storage, agent access, encryption and retention limits.

### OpenCode logins

OpenCode 2 keeps its logins (API keys, OAuth tokens, external logins and MCP
OAuth) in the SQLite database `~/.local/share/opencode/opencode.db`, in the
`djinn-opencode-data` volume; the former `auth.json` and `mcp-auth.json` files
are no longer used. Djinn mirrors them into
`<config-root>/opencode/credentials.json` (the `opencode auth export` format,
mode `0600`) and lists logins removed everywhere by ID in
`<config-root>/opencode/credentials.removed.json`, which holds no secrets. The
dev container syncs at start, every 30 s and at stop, merging the database,
`credentials.json` and Syncthing conflict copies of both files into the config
root before updating the database. Which account of a provider is active stays
per host; it is not synced. A logout inside OpenCode hides that login on this
host only; other hosts keep it. To log out on this host, or everywhere:

```sh
opencode auth logout                                   # in the dev container, this host only
djinn clean volumes --credentials --opencode-id <id>   # every host; repeatable
```

List IDs with `opencode auth list --format json` inside the dev container. For
entries OpenCode does not list, such as orphaned or cold MCP logins, use
`jq -r '.[] | "\(.id) \(.integrationID) \(.label)"' <config-root>/opencode/credentials.json`.
[Cleanup and Uninstall](#cleanup-and-uninstall) describes logging out every
OpenCode login at once.

## Configuration

The main config file is:

```text
~/.config/djinn_in_a_box/config.toml
```

Use the CLI instead of editing by hand when possible:

```sh
djinn config show
djinn config show --json
djinn config path
djinn config set resources.memory_limit 12G
djinn config set config_sync.source codex
djinn config status
djinn config edit
```

`djinn config edit` opens `$EDITOR` or `vi`, then validates the file after the
editor exits.

Additional mounts and environment variables are maintained only through
`djinn config edit`, in root TOML tables:

```toml
[mounts.archive]
source = "/mnt/archive"
target = "/home/dev/archive"
marker = ".drive-ready"

[mounts.tools]
source = "/opt/tools"
target = "/home/dev/tools"
read_only = true

[mounts.journal]
volume = true
target = "/home/dev/journal"
backup = "data"

[mounts.scratch]
volume = true
target = "/home/dev/scratch"
backup = "cache"

[mounts.worker]
volume = true
target = "/home/dev/worker"
backup = "none"

[environment]
CDP_HOST = "192.0.2.1"
CDP_PORT = "9222"
EXAMPLE_LITERAL = "${HOST_VALUE}"
```

Declarations add to the built-ins; they cannot replace, redirect, disable or
remove them. Names must match `[a-z0-9][a-z0-9_.-]*` and are unique across both
mount kinds. A dotted name needs quotes, for example `[mounts."archive.disk"]`.
Volumes are created by Compose as `djinn-<name>`; names colliding with built-in
volumes are refused. `volume = true`, `target` and `backup` are required for a
volume; `source` and `target` are required for a bind, with optional `marker`
and `read_only` (a strict boolean, default `false`). Volumes refuse `read_only`.
Other fields and mixed shapes are refused.

A bind source must be an existing absolute host directory without `:`; source
symlinks are resolved. The optional marker is a single filename directly inside
that directory and must be a regular file, never a symlink. A missing drive,
missing marker or wrong marker type refuses creation and names the declaration;
`djinn doctor` reports one PASS/FAIL row per declared mount or environment key.
Valid read-only binds report `valid declaration (read-only)`.
Djinn never provisions a source or marker because it is declared, checks no marker
contents or identity, and never backs up, restores or cleans declared bind data.
Use your host backup for it. Existing built-in provisioning still runs before
validation, including when a declared source overlaps a built-in provisioning path.
There is no mount-liveness protocol: a drive can disappear after validation.
The no-create bind setting prevents accidental host directory creation.

At creation, a writable `[mounts.*]` bind is refused if its host source overlaps a
host execution input. Read-only binds (`read_only = true`) and declared volumes
are not affected. Host execution inputs are the paths in the invoking CLI's
sealing inventory ([security model](SECURITY-MODEL.md#sealed-deployments-and-trusted-controller)),
including the location of every symlink on their paths.
The inventory uses that CLI process's `PATH`, Python environment and Docker client
configuration at creation time. A relative `PATH` entry, unreadable or malformed
Docker client config, or an execution input that cannot be inspected (for example
a PATH entry below a directory the user cannot traverse) makes the inputs
undeterminable, so all affected writable binds are refused. Set `read_only = true`
or remove an affected bind.
`djinn doctor` reports affected `[mounts.*]` entries as FAIL rows.

Managed writable binds whose host paths the configuration selects use the same
check: the workspace root (also delivered to the agent-Docker companion), the
config-root tool directories (`claude`, `codex`, `opencode`, `gh`, `age`), assigned
shared/local zone overlay sources, and `~/.djinn/sessions`. `djinn start` and
`djinn run` refuse an overlap immediately after loading the config, before host
provisioning, in every Docker mode. They also refuse when the execution inputs
or a source cannot be determined, or a managed source's path contains `..` or `:`.
Move the overlapping installation, Python environment, `PATH` directory or Docker
plugins/config out of that root, or choose another `general.code_dir`,
`general.config_root` (or `DJINN_CONFIG_ROOT`),
`general.shared_root` or `general.local_root`. For sessions, move the input out
of `~/.djinn/sessions`.

This point-in-time check pins no inode: source pathnames and their ancestors must
stay stable and outside any agent's rename authority (including through another
writable bind of a parent directory) until Docker has bound them.

Environment keys must match `[A-Za-z_][A-Za-z0-9_]*`; values must be strings and
are literal, so `${HOST_VALUE}` above stays exactly that text. Host values do not
override them. Reserved keys cover every Docker mode and everything the repository
ships into the image or runs at startup: Compose, Python, scripts, tools and
Dockerfile exports or generated shell configuration. Keys set by mounted host
shell startup files or third-party tools outside the repository are outside this
reservation boundary. Declared environment affects dev only. Declared mounts are also delivered to
the agent daemon as workspace mounts; neither affects image builds or host execution. No declared string may contain NUL.

`CODEX_HOME` and `CODEX_SQLITE_HOME` are reserved: Codex uses the mounted
`~/.codex`, with SQLite directed to its per-host `sqlite/` overlay.

`djinn config show` includes declarations, with `read_only=true` on read-only
binds in text and an explicit boolean on every bind in JSON. `djinn config set`
preserves effective modes, omits false `read_only`, rewrites the file and loses
TOML comments.
`djinn init` asks no declaration questions; `djinn init --force` replaces the whole
file, including declarations. Changes take effect at the next container creation
through `start` (foreground or detached) or `run`; `session` and `enter` inherit
the running container. There is no comparison with running mounts or attach-time
update. Literal endpoints such as CDP_HOST must be edited manually when the host
gateway changes. Existing Compose edits and volume data must be moved by hand;
there is no migration command.

Supported `djinn config set` keys are:

| Key | Meaning | Default or source |
| --- | --- | --- |
| `general.code_dir` | Host workspace root mounted at the path selected by `general.workspace` | chosen during `djinn init` |
| `general.workspace` | `projects` mounts at `/home/dev/projects`; `aios` mounts at `/home/dev/aios` | `projects` |
| `general.timezone` | IANA timezone passed as `TZ` | detected from host, fallback `UTC` |
| `general.config_root` | Host root for credentials and local CLI state | `~/.djinn/config` |
| `resources.cpu_limit` | Compose CPU limit | `4` |
| `resources.memory_limit` | Compose memory limit | `8G` |
| `resources.cpu_reservation` | Compose CPU reservation | `1` |
| `resources.memory_reservation` | Compose memory reservation | `2G` |
| `shell.skip_mounts` | Skip host shell config mounts | `false` |
| `shell.omp_theme_path` | Optional Oh My Posh theme file mounted read-only | unset |
| `config_sync.source` | Native global workflow source: `claude`, `codex`, or `opencode` | `claude` |
| `build.network` | Network for image-build steps: `default` or `host` | `default` |
| `serve.opencode_host_port` | Host IPv4 loopback port for OpenCode in the detached container; integer 1024–65535 | `49380` |
| `assistant.agent` | Interactive audit agent: `claude`, `codex`, or `opencode` | `claude` |

`djinn init` asks for the mode first, then the corresponding host directory.
Set the mode and host root explicitly; mode values must be lowercase:

```sh
# Standalone projects directory
djinn config set general.workspace projects
djinn config set general.code_dir /path/to/projects

# AIOS workspace root
djinn config set general.workspace aios
djinn config set general.code_dir /path/to/aios
```

Memory values must use Docker-style units such as `8G`, `4096M`, or `512K`.
CPU values are positive integers. Reservations cannot exceed limits.

`build.network` stays at `default` on an ordinary Docker host. Set it to `host`
when your container DNS server is reachable from one Docker network only — a VPN
or split-DNS setup, for instance, where a resolver listens on one bridge while
builds run on another and resolve nothing there. The build then resolves through
the resolver the host itself uses, which keeps DNS on its intended path instead of
bypassing it.

The setting affects build steps, not container runtime networking. See the
[host build-network implications](SECURITY-MODEL.md#host-authority-and-docker-access)
before selecting `host`.

Buildx asks for explicit consent to that: since buildx 0.37.2, a build that
requests the host network fails unless the `network.host` entitlement is granted.
`djinn build` grants it for exactly the builds that run with `build.network host`,
and never otherwise. A plain `docker compose build` cannot grant it and stops with
`additional privileges requested`, so build through `djinn build`.

`djinn build` refuses early with this hint when it detects the situation, instead
of letting each download time out in turn. Buildkit also accepts `none`, which
`djinn` does not offer: no layer of this image can be built without a network. A
named Docker network is rejected by buildkit itself.

`DJINN_CONFIG_ROOT` is not something you normally have to export. The CLI loads
`config.toml` and injects Compose interpolation variables, including
`DJINN_CONFIG_ROOT`, `CODE_DIR`, `DJINN_WORKSPACE_TARGET`, `TZ`, and the resource
settings, into the `docker compose` subprocess environment. If you do export `DJINN_CONFIG_ROOT`,
that environment value takes precedence for config-root resolution.

`DJINN_WORKSPACE_TARGET` comes from the configured mode and controls both the
workspace mount target and default working directory. Configured values override
inherited `CODE_DIR` and `DJINN_WORKSPACE_TARGET`; changing mode needs no Compose edit.

## Global Agent Workflow Ownership

Each Djinn deployment selects one native global workflow as its source of truth:

```sh
djinn config set config_sync.source claude   # or codex / opencode
djinn config sync
djinn config status  # exit 0 when clean, otherwise exit 1
```

When changing authority, use **switch → sync → edit**: select the new source,
run `djinn config sync`, then edit it. Sync requires a valid source and refuses
to overwrite an edited managed target. Unmanaged content at a managed path is a
collision.

The choice is deployment-wide. The shared demo is one deployment with one
source; this is not a per-tenant setting. Selecting a workflow source does not
select which agent `djinn run` or `djinn session` launches.

The source stays in its native project-local root:

| Tool | Authoritative root instructions | Native agents | Native commands |
| --- | --- | --- | --- |
| Claude Code | `config/claude/AGENTS.md` | `agents/*.md` | `commands/*.md` |
| Codex | `config/codex/AGENTS.md` | `agents/*.toml` | `skills/command-*/**` |
| OpenCode | `config/opencode/AGENTS.md` | `agents/*.md` | `commands/*.md` |

All three tools use `AGENTS.md` for global instructions. Agent-relevant material
for a working directory lives in that directory's `.agents/` directory and is
read before working there. Global native skills, agents, commands, context, and
scripts remain in their tool-native locations.

Codex uses the `project_doc_fallback_filenames` bridge; Claude Code reads global
instructions only from `~/.claude/CLAUDE.md`, so Djinn supplies that file as an
`@AGENTS.md` import bridge in the container and on the host fallback.

The cross-tool projection surface also includes `skills/<name>/**`,
`context/**`, and `scripts/**`. Hooks are native-only per tool: the known
startup/security/ready implementations and their `SessionStart`, `PreToolUse`,
and `Stop` registrations stay in that tool's own view. They are optional and
author-owned, validated there when present, never projected to another tool,
and never stale-removed. The Claude-only `/codex-review` command follows the
same source-only rule. Repository-local instructions, agents, skills, and
commands remain outside this global feature and are never rewritten.
Djinn checks that managed hook scripts stay at their specified source paths and
that Claude and Codex registrations remain paired with those scripts. See
[Native-only workflow artifacts](IMPLEMENTATION.md#native-only-workflow-artifacts)
for the per-tool tables, container paths, and validation behavior.

### OpenCode 2

OpenCode uses the pinned `@opencode/cli` v2 package. Its personal carrier
`<config-root>/opencode/` persists `opencode.json`, `cli.json` and
`themes/<name>.json` (flat, non-empty UTF-8 names without a leading `.`). Startup
admits the runtime entries before workflow publication and MCP registration, then
copies carrier bytes into `~/.config/opencode/` without JSON validation.
Directory-descriptor access refuses symlinks and non-regular allowlisted entries
in either tree; these violations and copy failures stop startup with the entry
and reason. Each file is limited to 1 MiB; oversized carrier files warn, are
skipped and stay protected from write-back for the session. See the
[OpenCode 2 guide](docs/opencode-v2.md) for the pin, theme selection, local-provider
template, endpoint declarations and optional plugins.
For its browser UI, API and remote clients, see
[Remote Agent Servers](#remote-agent-servers).

### Tool-Owned Runtime State

Codex daemon settings have one managed runtime key: every dev-container start
sets `updater.autoUpdateEnabled=false`, preserving other settings. See
[Codex](#codex) for the startup policy and remote-control preference.

Claude state, Claude personal settings and OpenCode personal settings are
checkpointed every 30 s and at clean stop or `docker stop`, using atomic writes
and exactly one JSON document for write-back. Changes older than about 30 s survive
a crash, plus checkpoint duration and scheduling delay, when JSON is valid and
storage is writable and healthy. Unchanged runtime content leaves host edits
alone, including `settings.local.json` at clean stop; when both changed, runtime
wins. Without checkpoint references (failed setup or lost private state), final
sync writes every valid carrier except protected OpenCode paths and can overwrite
a host-only edit or restore a deleted carrier file, provided it can recreate its
private state at stop; otherwise it warns and writes nothing.

OpenCode re-enumerates the allowlist each time, including runtime-created themes
and `cli.json`; write-back requires strict UTF-8 JSON (no malformed numbers,
`NaN` or `Infinity`/`-Infinity`); JSONC is not persisted. Refused runtime entries
leave the carrier and acknowledgement unchanged, warn once during checkpoints and again at final
sync if unresolved; invalid JSON is retried and warned about at final sync.
An oversized runtime entry at initialization keeps safe sibling checkpoints
running and can persist after repair; capture I/O failures disable checkpoints.
Persistence is upsert-only: a theme deleted at runtime returns after recreation.
For reliable removal, stop the container, delete it in the carrier, then recreate.

Crash residue (`.djinn-settings-*`) is never restored or projected into workflow
delivery. `djinn doctor` reports it as zone drift in config-root `claude/` and
`opencode/`; beside the seed overlay in `config/claude/` it is unreported.

The agent CLIs write into their own config root while they run. With the default
Claude source, those writes land in the workflow source because
`config/claude/skills` and `config/claude/scripts` are bind-mounted read-write so
you can edit skills in place. Two such trees are runtime state, not authored
workflow, and Djinn skips them everywhere it reads a source:

| Path | Written by | Treatment |
| --- | --- | --- |
| `skills/synced/**` (Claude source only) | Claude Code, syncing account skills | Left in place for Claude, never projected to Codex/OpenCode |
| `**/__pycache__/**` (any source) | any Python interpreter | Left in place, never read |

Skipped means skipped, not deleted: Djinn never removes these files, and
changing them is not workflow drift, so a fresh account sync or a new bytecode
cache cannot block the next `djinn start`. The exclusion is narrow on purpose. A
binary file anywhere else — including a `.pyc` outside `__pycache__` or a `.pyd`
shipped with a skill — is still reported as a non-UTF-8 workflow source, because
portable workflow artifacts must be text. `synced` is reserved only under a
Claude source; under a Codex or OpenCode source it is an ordinary skill name.

Runtime delivery is deliberately broader than cross-tool projection. The shared
publisher receives each complete native view, including its present hooks,
plugins, and registrations; the existing Claude host-path rewrite and
Compose-Claude settings merge still apply.

Everything outside that closed surface remains unmanaged: credentials, auth,
history, caches, themes, UI policy, MCP configuration, arbitrary plugins,
`PostToolUse`, and status-line configuration. In particular, MCP keeps its
separate `config/mcp-servers.json` source of truth.

The shared publisher uses one manifest schema in two locations:

- `config/.djinn-config-sync.json` is the canonical manifest.
- `.djinn-workflow-state.json` is the manifest in every runtime root managed by
  the publisher.

Each entry names either a file or a carrier-file key and records only its
content hash and executable flag, plus the selected source for the manifest.
Neighboring keys in shared JSON or TOML carriers stay operator-owned.

Whole managed files in runtime roots are delivered with mode `0444` (`0555`
when executable), and the runtime manifest with `0444`. Shared carriers such as
`settings.json`, `config.toml` and `opencode.json`, and the canonical tree keep
their existing mode handling. A writable managed file is resealed at the next
delivery; changing write permissions alone is not drift. These modes block
in-place writes, including Codex and OpenCode edit tools and shell redirection.
They do not block replacement by rename, such as Claude Code's Edit tool or
`sed -i`; drift detection and the discard path below remain the safety net.

`djinn config status` is read-only: it audits the canonical tree and the runtime
roots delivered by `djinn start` (currently `<config-root>/codex`; Compose Claude
uses direct mounts). Drift diagnostics name the root and item, for example
`config/opencode/AGENTS.md` or `codex:AGENTS.md (<config-root>/codex)`, with
`[key.path]` for a carrier key. Output is sanitized and never prints workflow or
settings bodies. Status exits `0` only for `clean`, and `1` for every other state.
Plain `djinn config sync` writes the canonical views and exits non-zero when
blocked. After a successful sync it warns about runtime drift without repairing
it or changing the successful canonical-sync exit status.
The five states are:

| State | Meaning | Remedy |
| --- | --- | --- |
| `clean` | The selected source and all managed views match their manifests. | None. |
| `source-changed` | The source projection changed or was unstable during publication. | Run `djinn config sync`, then retry. |
| `target-drift` | A manifest-managed item was edited. | Keep wanted edits in the selected source `config/<source>/`, stop running Djinn containers (`djinn clean`), then run `djinn config sync --discard-target-drift` (backs up edited files first). |
| `collision` | An unmanaged item occupies a managed path. | Move or remove the conflicting unmanaged workflow item, then retry. |
| `invalid-or-semantic` | The source is invalid, empty, or contains a non-portable artifact. | Author or edit the artifact natively in the target tool's view, or make the source form portable. |

There is no semantic-provider fallback: workflow sync never invokes a provider.
Normal `start`, `run`, and `session` preparation repairs only deterministic
`source-changed` projection drift. Preflight checks every delivery target
read-only before changing workflow content; all other states stop the command
before an agent starts. A canonical auto-sync prints
`Synchronized source changes into the canonical workflow: <n> changed, <n> removed.`,
even if a later step refuses. Preflight, status, config-workflow audits, and
sync never seed or repair source roots. Only `djinn init` and
`djinn doctor --fix` perform source seeding.

`djinn config sync --discard-target-drift` backs up differing managed items in
the canonical tree and start runtime roots, then restores their wanted state;
pending source changes are synchronized too. It accepts only target drift, not
collisions or invalid states. It requires stopped Djinn containers and holds the
lifecycle guard from before the fail-closed running check through the last
publish. If needed, stop them with `djinn clean` first.

Backups go to `~/.local/state/djinn/workflow-drift/<UTC>-<pid>/`, under the passwd
home regardless of `HOME` or `XDG_STATE_HOME`, with `0700` directories and `0600`
files. A key drift copies the whole carrier; missing items are reported without
inventing a copy. All backups and their directory chain must be durable before
the first overwrite. The command prints each item and the backup directory;
backup failures keep partial copies and leave all targets untouched. With no
target drift it acts like plain sync and creates no drift backup directory.
The location must not overlap the installation, config or sync-zone roots,
`code_dir`, sessions, or a declared bind source. These records are outside
`djinn backup`, sync zones and dev mounts; operators should exclude
`~/.local/state/djinn` from their own sync and mounts, as for post-mortem records.

Host-fallback roots are excluded from discard. Keep wanted edits in the selected
source and sync it, then follow each named item's restore source or `remove`
action for retired files or keys. Keep the runtime manifest so ownership and
retirement stay tracked. A managed TOML key written as a multi-line value or
table cannot be rewritten: use a single-line value or delete the named key,
preserving neighboring personal keys, then retry.

Host fallback for Claude, Codex, or OpenCode receives the selected canonical
view through the shared publisher. The container-local OpenCode runtime at
`~/.config/opencode` is rebuilt at every `djinn start` and refreshed before an
OpenCode container session. A runtime drift refusal names
`opencode-runtime:<relative>`: keep wanted edits in `config/<source>/`, then run
`djinn clean` and `djinn start` to recreate it. The workflow publisher requires
an image marked `djinn.workflow.publisher=2`; an older or unlabeled image stops
preparation with `Rebuild/recreate required.` before Compose starts or a
running-container refresh executes.

Compose Claude is manifestless and uses direct mounts, including `AGENTS.md`,
together with the existing settings merge. The publisher never writes into the
Compose Claude runtime root.

## Doctor

Run:

```sh
djinn doctor
```

The doctor command checks Docker, the Docker daemon, socket permissions, Compose
v2, Buildx, the main config, the selected workspace root, the config root, the
image, the Docker network, actual desktop helper delivery, and seed target presence.
Desktop rows report off, filtered/locked, missing, or unknown; the raw-socket row
checks the running dev container's actual mounts. Docker rows report the observed
endpoint (none, verified agent, host-direct or unknown), companion identity,
health and cache storage. Unused companion rows say "not in use"; stale/orphan
resources are listed separately. These checks use bounded, read-only host
inspection and never start or repair a daemon. Doctor never starts helpers.

The `Managed bind sources` row applies the same creation-time check without
provisioning. Run it before upgrading or restarting: `PASS` says
`no overlap with host execution inputs`; `FAIL` lists every overlapping or
uncheckable managed source and its remedy.

Two read-only rows inspect Codex's local daemon state:

- `Codex daemon updater`: PASS when `updater.autoUpdateEnabled` is `false`, or
  neither a managed copy nor settings exist. WARN when enabled, when the key is
  missing/invalid, when installed copies lack settings, or when settings are
  malformed, over 64 KiB, or symlinked.
  Remote-control state is informational and does not change the status.
- `Codex daemon releases`: reports release count, size and current selection;
  WARN for more than one release, an unreadable/incomplete scan, or a `current`
  selection the start-time policy cannot use. Both rows WARN when a required
  default overlay was skipped. Names from the Codex home are shown with control
  characters escaped.

Neither row has a fix action. The daemon policy runs at dev-container start.

The read-only `Config workflow` check audits the canonical tree and start
runtime roots, warning with named drift items and a remedy. `OpenCode runtime`
reports the container-local delivery, rebuilt at every start. For a running dev
container it checks the image label first, then invokes the publisher's
read-only check through `docker exec`. Drift is a named `WARN`; an unreachable
container, timeout or unusable diagnostic is `check unavailable`, never clean.
An absent or stopped container reports the rebuild lifecycle without inspecting
its runtime files.

The filesystem-only `Post-mortem records` row works without valid config or a
Docker daemon. It reports `PASS` with `none` or the kept count and latest record
path; historical records do not signal a current failure. An unreadable or
unsafe records directory reports `WARN`.

For idempotent local repairs:

```sh
djinn doctor --fix
```

`--fix` provisions expected host directories, repairs missing seed targets, and
creates the Docker network when possible. It does not install Docker, repair an
invalid config file, or build the image.

## Desktop integration

Desktop notifications, playback and microphone capture use two isolated helpers,
built with `djinn build`. Only their read-only output directories reach dev.
Creation through foreground start, detached start or `djinn run` prepares each
available host endpoint independently. A failed helper produces a warning and
starts dev without that endpoint. `djinn clean` removes their runtime volumes;
backup and restore exclude them.

After rebuilding, recreate dev through the normal start/run path. Recreate after
a host desktop session or audio server restart too; plain container restart keeps
the existing mounts and cannot restore an endpoint omitted during creation.
See [desktop boundaries](SECURITY-MODEL.md#desktop-boundaries)
for the policy and its limits.

## The Blank-Space and Seed Model

Djinn treats your project-local `config/` directory as blank space. It is
root-anchored in `.gitignore`, seeded on first run, and then owned by you.

Only `djinn init` and `djinn doctor --fix` copy missing seed targets from
`templates/seed/` into local paths:

| Seed source | Local target | Purpose |
| --- | --- | --- |
| `templates/seed/config/claude/AGENTS.md` | `config/claude/AGENTS.md` | neutral global instructions with the per-directory `.agents/` convention |
| `templates/seed/config/claude/settings.json` | `config/claude/settings.json` | minimal Claude Code settings |
| `templates/seed/config/claude/skills/` | `config/claude/skills/` | empty local skills directory |
| `templates/seed/config/claude/commands/` | `config/claude/commands/` | empty local commands directory |
| `templates/seed/config/claude/agents/` | `config/claude/agents/` | empty local Claude subagent directory |
| `templates/seed/config/claude/context/` | `config/claude/context/` | empty local context directory |
| `templates/seed/config/claude/scripts/` | `config/claude/scripts/` | empty local scripts directory |
| `templates/seed/config/opencode/` | `config/opencode/` | empty OpenCode seed directory |
| `templates/seed/config/mcp-servers.json` | `config/mcp-servers.json` | empty local MCP registry |
| `templates/seed/config/agents.toml.example` | `config/agents.toml.example` | documentation-only agent override example |
| `templates/seed/tools.txt` | `tools/tools.txt` | optional runtime installer list |
| `templates/seed/packages.txt` | `packages.txt` | optional Debian package list |

Existing targets are left alone when they already have the expected type. This
means seed files are starting points, not managed config.

The [Codex daemon policy](#codex) is separate from seeding: it reapplies the
updater setting at every dev-container start.

Bring your own native workflow in the root selected by `config_sync.source`.
The seeded Claude `AGENTS.md`, `settings.json`, `mcp-servers.json`, and empty
local directories are deliberately minimal. Replace the selected source with
your own instructions, settings, commands, skills, hooks, or subagents as
needed; MCP entries remain separate.

`packages.txt` is read at image build time and may list extra Debian packages,
one per line. `tools/tools.txt` is read at container start and may list installer
names that correspond to scripts under `tools/installers/`.

### Runtime Packages

As `dev`, `apt-get update && apt-get install -y <package>` works without a
`sudo` prefix. The shim accepts `-y`, `--yes`, `--assume-yes`, `-q`, `--quiet`,
`-qq`, and `--no-install-recommends` anywhere in the command; `-h` or `--help`
also works anywhere. Names must match `[a-z0-9][a-z0-9+.-]*` and contain at
least two characters and at most 128 bytes. `apt-get --help` prints the accepted syntax.
Installs always use `-y` and `--no-install-recommends`.

Other subcommands, unsupported flags, paths, `.deb` files, and malformed names
are refused with exit 2. A name not found exactly in the current apt cache exits
100 with `E: Unable to locate package <name>` and `Run apt-get update and try
again.` Refusal messages point to `apt-get --help` and this README section.
Every elevation is announced on stderr; direct calls print
`apt-get: using sudo to run /usr/local/libexec/djinn-apt-root as root.` The
startup replay announces its helper call with `Packages: using sudo to run
/usr/local/libexec/djinn-apt-root replay as root.` `/usr/bin/apt-get` remains available
unprivileged for other subcommands. When `apt-get` runs as root, including
through `sudo apt-get`, it passes the original arguments and streams to
`/usr/bin/apt-get` unchanged and does not record the invocation.

The root helper records successful installs made as `dev` in
`/var/lib/djinn-packages/packages.txt`, one name per line, using an atomic,
deduplicated write under its package lock. The file is root:root 0644 in a
root:root 0755 directory. Agents cannot change it. Blank lines and `#` comments
are ignored. Read the list with `cat`; a human removes an entry with:

```sh
sudo sed -i '/^NAME$/d' /var/lib/djinn-packages/packages.txt
```

Removing a line stops replaying that package; it stays installed in the current
container until recreation. Recording keeps existing lines and refuses additions
beyond 256 distinct names or 1 MiB. If recording fails, the helper warns and
the install keeps its successful exit status.
The file is stored in the `djinn-packages` named volume at
`/var/lib/djinn-packages`, category `data`.

At startup, the Packages step runs after MCP and before Tools. Its dev wrapper
announces and calls the root helper's `replay` mode, which checks the recorded
names against installed packages. If none are missing, it makes no
network calls. Otherwise it updates apt, warns and skips names not present in the
apt cache, then installs the remaining missing packages. Replay problems are
warnings and do not stop startup. The helper checks the opened file descriptor:
only a regular root-owned file of at most 1 MiB, in a root-owned directory, is
accepted. Symlinks and group- or world-writable files or directories are refused
with a warning. Replay uses one linear parse and considers at most 256 distinct names
and warns how many more it did not replay, treats entries over 128 bytes as
invalid, reports at most five invalid lines one by one, and stops its package
checks with a warning after 60 seconds in each check phase, before and after the
update. The package list update has a 300-second
limit; the install step has none, because interrupting dpkg can leave packages
half-configured. Prefer build-time `packages.txt` for packages
every new image needs from its first start; runtime installs are for packages
agents choose while using a container. See the [runtime package install
security model](SECURITY-MODEL.md#runtime-package-installs) for the boundary and
residual risk.

## Agents

Djinn has four built-in agent definitions:

| Agent name | Binary | Default headless mode | Notes |
| --- | --- | --- | --- |
| `claude` | `claude` | `claude -p` | read-only uses `--permission-mode plan`; write mode uses `--dangerously-skip-permissions` |
| `codex` | `codex` | `codex exec` | write mode uses `--full-auto` |
| `opencode` | `opencode` | `opencode run` | read-only uses `--agent plan`; model flag is `-m` |
| `claude-isolated` | `claude` | `claude -p --no-session-persistence` | Claude Code without user or project context; see [Minimal-context Claude agent](#minimal-context-claude-agent) |

List the effective definitions:

```sh
djinn agents
djinn agents --verbose
djinn agents --json
```

### Minimal-context Claude agent

`claude-isolated` runs Claude Code as a plain model call. It has no tools and no
MCP servers, and it loads no `CLAUDE.md` or `AGENTS.md`, no user, project or local
settings or hooks, no skills, commands or custom agents, and no auto-memory.
Headless runs also keep no transcript. It still uses the stored subscription
login, which Claude Code's `--bare` mode cannot use. A `djinn run claude-isolated`
call runs this command:

```text
claude -p --no-session-persistence [--model M] --safe-mode --setting-sources= --strict-mcp-config --disable-slash-commands --tools= [--output-format json] -- "$AGENT_PROMPT"
```

- `--write` grants nothing extra, because the agent has no tools.
- The prompt follows `--`, so a prompt that starts with `-` stays a prompt.
- An interactive `djinn session --agent claude-isolated` uses the same isolation
  flags but keeps Claude Code's normal transcript, because
  `--no-session-persistence` works only with `-p`.
- The host fallback of `djinn session` refuses this agent.
- The model still sees Claude Code's own environment block: the working
  directory, whether it is a Git repository, the platform, the shell and the OS
  version.
- A run still writes Claude Code's own app state: `~/.claude.json` with its
  backups, and short-lived session files under `~/.claude/sessions/` that Claude
  Code removes at exit. It writes no transcript, memory or prompt history.
- The guarantee holds for the measured version, Claude Code 2.1.304, the dev
  image's default. Flag behaviour can change between Claude Code releases, so
  rerun the probe below after every change of the Claude Code version, including
  an update that Claude Code installs by itself in a running container.

To pin the exact definition a pipeline runs with, fingerprint it:

```sh
djinn agents --json | jq -cS '."claude-isolated"' | sha256sum
```

`djinn agents --json` prints every field of every effective agent definition,
including `prompt_template`, with sorted keys and independent of the terminal
width. The fingerprint changes whenever the definition changes, also when an
`agents.toml` replaces the built-in agents.

#### Re-probing the isolation

Run the probe inside the Djinn container from a checkout:

```sh
uv run python scripts/probe-claude-isolated.py            # full probe
uv run python scripts/probe-claude-isolated.py --offline  # without the login
```

The first part runs Claude Code in a temporary home without any login. It
compares a run without the isolation flags against the preset: tools, MCP
servers, skills, commands, custom agents, auto-memory, settings and hooks must
appear in the first run and be absent in the second. The second part runs only
when the first part passed, and it uses the stored login. It starts a few short isolated Claude Code runs, at most one of
them without the isolation flags, and checks that a `CLAUDE.md` and an
`AGENTS.md` canary stay out of the preset's context; only the `CLAUDE.md`
canary has a positive control in the same probe. Like any normal run, these
runs may refresh the login token and write Claude Code's app state. Afterwards
the probe checks that no transcript, memory, session environment or
prompt-history entry was left behind; it reports a finding as `FAIL` with the
path and deletes nothing. Exit code 0 means that every check passed.

### Codex

By default, interactive `codex`, `codex resume` and `codex fork` start Codex's
managed app-server daemon. `codex exec` and plain `codex app-server` (stdio) do
not start that managed daemon.

On every dev-container start, Djinn applies this policy in the mounted `~/.codex`:

1. Set only `updater.autoUpdateEnabled=false` in
   `app-server-daemon/settings.json`, creating it if absent and preserving all
   other keys. A malformed or symlinked file is left untouched with a warning.
2. If a selected daemon release differs from the image's Codex CLI version,
   run `codex app-server daemon update --from-cli --yes` to select the image's
   pinned Codex. Without a selected release, this step does nothing.
3. Keep only the selected release under
   `packages/app-server-daemon/releases/` once the selection is validated.
   A measured release occupies about 410 MiB.

Failures warn and container startup continues. Updates come from `djinn update`
followed by `djinn build` and a new dev-container start; the daemon's automatic
updater is disabled.

The managed daemon's remote control is off by default upstream. Enabling it
requires an explicit `codex app-server daemon enable-remote-control` and a
ChatGPT login; Djinn preserves that preference. A log line saying
`remote control websocket loop started … initial_desired_state=Disabled`
does not mean a connection was established. Plugin discovery and model traffic
can still contact vendor services with both updater and remote control off;
see [the firewall allowlist](#what---firewall-allows).

### Agent Overrides

The only automatically honored agent override path is:

```text
~/.config/djinn_in_a_box/agents.toml
```

If that file exists, Djinn loads it instead of the built-in defaults. If it does
not exist, Djinn uses the built-in definitions from the package.

The host runs the commands this file defines, so Djinn accepts the file only
when you alone control it. It must be a regular file with a single link (not a
symlink or a hard link), owned by you and not writable by group or others, in a
real directory (not a symlink) that you own and that is not writable by group or
others. Otherwise `djinn run`, `djinn agents` and `djinn session` stop and name
the reason instead of falling back to the built-in agents. A file that replaces
the built-in agents without defining `claude-isolated` removes that agent too.

The seeded file `config/agents.toml.example` is documentation only. Copy it to
`~/.config/djinn_in_a_box/agents.toml` before editing if you want to override
agent definitions:

```sh
mkdir -p ~/.config/djinn_in_a_box
cp config/agents.toml.example ~/.config/djinn_in_a_box/agents.toml
djinn agents --verbose
```

Define agents under `[agents.<name>]`. Supported fields are `binary`,
`description`, `headless_flags`, `read_only_flags`, `write_flags`, `json_flags`,
`model_flag`, `default_model`, and `prompt_template`. An explicit `--model`
value overrides `default_model` for that invocation.

## Container Entry Points

| Command | Container behavior | Workspace behavior | Main use |
| --- | --- | --- | --- |
| `djinn start` | Runs the `dev` service interactively with `docker compose run --rm`; removed after exit. `--detach` uses `docker compose up -d` instead and leaves no client attached | Starts in `/home/dev/projects` (`projects`) or `/home/dev/aios` (`aios`) without an extra mount; `--here` mounts `/home/dev/workspace`; repeatable `--mount` values add directories at chosen or derived targets | Daily interactive shell; `--detach` for a long-lived container |
| `djinn enter` | Uses `docker exec -it <running-container> zsh` | Enters an already running Djinn container | Open a second shell while `djinn start` is still running |
| `djinn serve` | Starts Claude Remote Control or the OpenCode web UI/API inside the running detached container | Uses the container's working directory, mounts and sign-ins | [Remote agent access](#remote-agent-servers), status, stop and OpenCode pairing |
| `djinn run AGENT PROMPT` | Runs the `dev` service headlessly with `docker compose run --rm -T`; removed after exit | Without `--mount` and without `--here`, mounts the current directory at `/home/dev/workspace`; `--here` keeps that mount when combined with repeatable `--mount` values | One-shot agent prompts |
| `djinn session` | Uses `docker exec` into a running `djinn` container when available; otherwise host fallback checks the selected agent binary on `PATH` and applies workspace checks before launch. Claude, Codex, and OpenCode host fallback receives that agent's confirmed canonical workflow at its native host root. Running-container OpenCode sessions refresh the live runtime through the shared publisher before invocation. | Uses `~/.djinn/sessions/<project>` on the host and `/home/dev/sessions/<project>` in the container; `--create` creates the host workspace | Reusable session workspaces |

The [Codex daemon policy](#codex) runs on foreground and detached dev-container
starts, not on one-shot `djinn run` or `compose run … <cmd>` invocations.
Attaching with `enter` or `session` does not rerun the entrypoint.

Without a running container, `djinn session` asks before publishing an
unconfirmed or changed workflow to `~/.claude`, `~/.codex`, or
`~/.config/opencode`. It lists new (`+`), changed (`~`) and removed (`-`) managed
items on stderr, identifies executable files and settings fragments, and names
the source locations for reviewing their contents. At a terminal, confirmation
defaults to No. Declining leaves the host root, manifest and trust record
unchanged and exits 1. Non-TTY callers also exit 1 until the user runs
`djinn session --agent <agent>` at a terminal and confirms the listed workflow;
unchanged confirmed workflows then run without another prompt, including with
`--prompt`.

The first host fallback asks once per native agent root; a publisher manifest
alone does not count as confirmation. Confirmed item sets
are stored in `~/.config/djinn_in_a_box/host-workflow-trust.json`. Container edits
to published files or managed hooks in `settings.json` require another review
on the next host fallback. Personal settings written back into
`config/claude/settings.local.json` are not delivered to the host and do not
cause another prompt. Container sessions and in-place editing stay unchanged.
Host starts also refuse unsafe agent and Git configuration in the session
workspace. See the [security model](SECURITY-MODEL.md#direct-routes-and-target-authority)
for the managed-payload boundary, host-authority limits, the host-start boundary
and its residual risks.

Common `start` options:

| Option | Effect |
| --- | --- |
| `--docker`, `-d` | Starts the rootless `agent-docker` companion and sets a managed Unix `DOCKER_HOST` in dev |
| `--docker-direct` | Adds `docker-compose.docker-direct.yml` and mounts `/var/run/docker.sock` directly into the dev container |
| `--firewall`, `-f` | Applies the existing outbound rules in dev and, with `--docker`, before the companion daemon starts |
| `--here` | Mounts the current directory as `/home/dev/workspace` and uses it as the working directory; writable overlaps with host execution inputs are refused |
| `--mount SRC[:DST[:ro\|rw]]`, `-m …` | Repeatable host-directory mount. Without `DST`, it maps to `/home/dev/mount/<basename>`; append `:ro` for read-only; writable overlaps with host execution inputs are refused. |

`--docker` and `--docker-direct` are mutually exclusive.

### What `--firewall` allows

The firewall denies outbound traffic by default and permits a fixed domain list
in `scripts/init-firewall.sh`: package registries including the Debian archive
`deb.debian.org`, the three bundled CLIs' API and sign-in endpoints, GitHub, and
the Docker networks. Every address a domain resolves to is permitted.

`chatgpt.com`, used by Codex remote control and some plugin/model requests, is
not on the list. Disabling Codex's updater and remote control does not disable other
vendor traffic, and this mutable IP allowlist is no hard network boundary.

A blocked connection is refused immediately rather than dropped, so a tool that
hits the allowlist fails in about 0.2 seconds with "connection refused" instead
of hanging until its own timeout. If something fails in unusual ways under
`--firewall`, that list is the first place to look — add the domain you need at
the marked spot near the end of the array.

One limit is worth knowing before you rely on it: the list is resolved **once**
at container start, so an address a provider rotates in later is denied until you
restart. IPv4 only, which matches the Djinn network — it runs with IPv6
disabled.

Third-party model providers you configure yourself (OpenRouter, x.ai, and the
like) are deliberately absent; add the ones you actually use.
See [outbound traffic limits](SECURITY-MODEL.md#credentials-backups-and-outbound-traffic)
before using the firewall with sensitive data.

## Remote Agent Servers

`djinn serve <claude|opencode ...>` starts the selected agent server modes
inside a running detached Djinn container, using its working directory, mounts
and sign-ins. Repeating a start leaves a healthy server running when its settings
match; OpenCode restarts if its password or tailnet CORS origin changes. Servers
are not automatically restarted after a crash or container restart.

Recreate an existing container once to get the OpenCode port publication:

```sh
djinn clean
djinn start --detach
djinn serve opencode
djinn serve claude opencode
djinn serve --status
djinn serve --pair opencode
djinn serve --stop opencode
djinn serve --stop claude opencode
djinn serve --stop
```

`--stop` without names stops both servers; stopping an inactive server succeeds.
Start, `--status`, `--stop` and `--pair` are separate modes. Pairing is available
only for OpenCode. Start and pair require a detached container; status and stop
can inspect a running container without the start prerequisites. A Docker
inspection failure reports an unknown state, rather than treating it as absent.

### OpenCode Web UI and API

OpenCode serves its web UI and API at host
`http://127.0.0.1:<serve.opencode_host_port>` (default `49380`, container port
`4096`). Choose another host port before creating the container if needed:

```sh
djinn config set serve.opencode_host_port 49381
djinn config show
```

Changing the setting requires another `djinn clean`, then `djinn start --detach`.
Djinn refuses a missing or different port binding with
`The running container does not publish the OpenCode port on 127.0.0.1:<port>.`
The host loopback publication is reachable by local host users and processes;
the container address is also reachable by network peers. Read the
[inbound boundary](SECURITY-MODEL.md#remote-agent-server-access) before enabling it.

The first `djinn serve opencode` generates a password at
`$XDG_STATE_HOME/djinn/serve/opencode-password` on the host (mode `0600`), using
`~/.local/state` when `XDG_STATE_HOME` is unset; an empty value refuses.
Before creating or reading it, Djinn refuses symlinks in that path,
locations inside the config root, zone sources, Djinn backups or running-container
bind sources or volume host storage, including local-driver bind aliases (each at
its resolved location), and locations whose safety it cannot
determine. This keeps it outside
those synced trees and backups provided you keep `XDG_STATE_HOME` host-local and
out of other sync, indexing and backup tools. Read it locally when a client asks;
Djinn prints the file path, never the password. An existing file with unsafe
ownership, permissions or type, or
invalid size/encoding, is refused. To rotate, delete this file and run `djinn serve opencode`
again: Djinn generates a new password and restarts the running server. Attached
clients must reconnect.

In a browser, open the local URL or the tailnet HTTPS URL and log in with the
password. Alternatively, run `djinn serve --pair opencode` and open one of its
one-time links. Pairing requires a running OpenCode server and a known tailnet
URL. A CLI client connects with `opencode --server <url>` and the password in
its `OPENCODE_PASSWORD` environment variable. In-container OpenCode sessions
share the service; after `--stop opencode`, the next client that needs it starts
OpenCode's ordinary loopback service again.

For tailnet access, the host's administrator creates a Tailscale Serve entry
once, substituting the configured host port:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:49380
```

Djinn never writes Tailscale settings; it only reads
`tailscale serve status --json` to discover an HTTPS URL such as
`https://host.tailnet.ts.net`. Never enable Funnel for this entry: both
`djinn serve opencode` and `djinn serve --pair opencode` refuse detected Funnel,
and status warns about it. If Tailscale is missing or its status is unreadable,
starting is allowed with `Funnel state unknown (<reason>)`; review the host
configuration yourself. Without a discoverable URL, pairing refuses.

Restrict tailnet policy to HTTPS on this host from the intended devices or a
dedicated device tag. A grant to the whole account includes Djinn's hostctl
helper node, which is itself an account device. Keep that helper outside the
grant. The OpenCode password remains the second barrier.

OpenCode provider credentials live in `opencode.db` on the data volume.
After `djinn clean volumes --data`, the server can still start, but model calls
fail until you log in again; credential persistence is tracked in
[#162](https://github.com/w2kr1stn/djinn_in_a_box/issues/162).

### Prerequisites and Refusals

Before the first OpenCode start, check `/etc/docker/daemon.json` on the host:
`allow-direct-routing` must be absent or `false`, and `iptables` absent or
`true`. Djinn cannot inspect these daemon-wide settings through Docker's API.
Keep these defaults; see the [residual routing risk](SECURITY-MODEL.md#remote-agent-server-access).

OpenCode start and pair also check these prerequisites and refuse if they
cannot verify them:

- Docker Engine server version **28.3.3 or newer**. This includes the fix for
  [CVE-2025-54388](https://github.com/moby/moby/security/advisories/GHSA-x4rx-4gw3-53p4),
  where a firewalld reload could expose loopback-published ports. An unknown
  Engine version also refuses.
- Exactly the configured IPv4 loopback publication, and only user-defined
  bridge networks. Host/container network mode and drivers such as macvlan,
  ipvlan or overlay refuse. Every attached bridge must omit
  `com.docker.network.bridge.trusted_host_interfaces`; IPv4 and IPv6 gateway
  modes must be neither `routed` nor `nat-unprotected`.
- OpenCode's default settings roots under the container's `/home/dev`.
  Mounts covering, inside or above `~/.config/opencode` or
  `~/.local/state/opencode` refuse. `HOME` must retain `/home/dev`, and
  `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `OPENCODE_CONFIG_DIR` must be unset.

On start, the helper also refuses symbolic links in the settings paths and
requires `~/.config/opencode/service.json` to be absent or contain only
`password`. Remove every other key inside the container with
`opencode service unset <key>`, including `remote` (OpenTunnel), `hostname`,
`port`, `cors`, `env` and `disabled`. These checks precede service changes.
An old discovery file never suffices to authorize stopping a process: Djinn
replaces an existing service only after proving its identity.

### Claude Code Remote Control

`djinn serve claude` starts `claude remote-control` in server mode. Generated
session names start with the host's short name, normalized to lowercase letters,
digits and hyphens. Sign in to a claude.ai subscription inside the container
(for example, `claude auth login`) and trust the working directory in an
interactive Claude session first. API-key authentication does not suffice.
Claude's login or trust refusal appears in the startup log tail and in status
if the process exits. See [Claude's requirements](https://code.claude.com/docs/en/remote-control#requirements)
and the [Remote Control trust boundary](SECURITY-MODEL.md#remote-agent-server-access).

With `--firewall`, the outbound allowlist may lack hosts Remote Control needs;
this combination has not been measured.

Codex is not part of `djinn serve`. Codex Remote Control currently runs through
the daemon that the Codex TUI starts itself; `djinn serve codex` follows in
[#164](https://github.com/w2kr1stn/djinn_in_a_box/issues/164).

### Server Status

`djinn serve --status` shows the detailed table, including OpenCode's local
endpoint and the tailnet URL or the reason it is unknown. `djinn status` shows
a compact snapshot in Services, with the local URL for running OpenCode.

| State | Meaning |
| --- | --- |
| `running` | The recorded processes have matching identities. OpenCode also owns the listener and service registration; Claude has both its supervisor and child process groups. |
| `degraded` | Recorded processes remain, but OpenCode lost its listener/registration, or Claude lost its child or supervisor. Run `djinn serve <name>` to stop the remains and restart it. |
| `exited` | The server ended in this container run. Its last sanitized log line remains visible until the next start or stop. |
| `stopped` | No current managed server record, or stale state from a container restart or reused PID. Stale state is ignored and cleared when the state lock is available. |
| `busy (start/stop in progress)` | A start or stop holds the state lock; retry the query after it finishes. This is a snapshot warning, not a server state. |

Status distinguishes a missing container, a container started without `--detach`,
and Docker errors. It also reports `port not published (recreate once)`, detected
Funnel, and `note: port 4096 is used by another process` for a foreign listener.
Stopping a managed server does not remove the port publication or the host's
Tailscale Serve entry.

## Docker Access Modes

The base mode delivers no Docker socket or Docker endpoint.

`djinn start --docker` starts a rootless companion daemon from a pinned official
image. Dev connects as UID 1000 through a read-only endpoint volume at
`unix:///run/djinn/agent-docker/socket/docker.sock`. Djinn exposes no TCP Docker API
to network peers. Published inner-container ports are reachable from dev at
`agent-docker:<port>`; use that hostname instead of `localhost`.

An agent can deliberately relay its inner API through a published workload port;
that exposes the inner daemon and its delivered workspace, with no host-daemon
authority.

The daemon receives exactly dev's workspace delivery: the configured code directory,
`--here` and CLI mounts, declared binds/volumes, and `/home/dev/sessions`, with the
same targets and access modes. Dev's credential and desktop/Git mounts are separate.
A workspace containing sensitive host files still exposes them; choose its scope
carefully. Targets shadowing the companion's runtime or binaries are rejected.

Buildx is included in dev. `docker buildx build --load .`, `docker run` and
`docker compose up` use the inner daemon. Relative Compose binds must resolve to a
delivered workspace path. Its images, volumes and BuildKit cache persist in
`djinn-agent-docker`, a cache volume excluded from default backups. Normal clean
removes the companion and temporary endpoint, keeping that cache; explicit cache
clean removes it. Inner containers stop with dev and follow Docker restart policies
when the daemon starts again.

`djinn start --docker-direct` mounts `/var/run/docker.sock` directly into dev and
grants host-daemon authority. Both Docker flags also work with `djinn run`.
A verified companion of dev's own generation contributes no Docker sealing
cause. Direct mode remains unsealed; unknown or mismatched agent evidence refuses
admission, including with `--allow-unsealed`. Other sealing causes still apply.
Read [host authority and Docker access](SECURITY-MODEL.md#host-authority-and-docker-access)
for the profile's limits and direct mode's implications.

## Storage and Mounts

Djinn uses both bind mounts and named volumes. They serve different purposes.

Mount configuration has three layers: fixed built-ins for credentials, seeds,
workflow and cache targets; built-ins with a config value for the workspace,
config root, SOPS key file and OMP theme; and declared additional mounts and
environment variables. Use the existing config keys to configure the second
layer; declarations extend the set and keep the tracked Compose files unchanged.
See [Configuration](#configuration) for the managed writable source restriction.

Bind mounts are host paths that you can inspect and manage directly:

| Host path | Container path | Purpose |
| --- | --- | --- |
| `${DJINN_CONFIG_ROOT}/claude` | `/home/dev/.claude` | Claude Code credentials and state |
| `${DJINN_CONFIG_ROOT}/codex` | `/home/dev/.codex` | Codex CLI credentials and state |
| `${DJINN_CONFIG_ROOT}/opencode` | `/home/dev/.opencode` | OpenCode state |
| `${DJINN_CONFIG_ROOT}/gh` | `/home/dev/.config/gh` | GitHub CLI state |
| `${DJINN_CONFIG_ROOT}/age` | `/home/dev/.config/age` | age encryption identities (`keys.txt`) |
| `${CODE_DIR}` | `/home/dev/projects` (`projects`) or `/home/dev/aios` (`aios`) | One configured workspace root; AIOS's `projects/` appears at `/home/dev/aios/projects` |
| `${HOME}/.djinn/sessions` | `/home/dev/sessions` | Session workspaces |
| Djinn-generated public SSH directory | `/home/dev/.ssh:ro` | Declared Git aliases, public keys and host/signing trust |
| Djinn Git agent export directory | `/run/djinn-git-agent:ro` | Filtered SSH agent socket (`auth.sock`) |
| `~/.gitconfig` | `/home/dev/.gitconfig:ro` | Read-only Git config |
| `./config/claude` | `/home/dev/.claude_seed` | Local Claude seed and settings sync source |
| `./config/claude/AGENTS.md` | `/home/dev/.claude/AGENTS.md` | Direct Compose-Claude instruction mount |
| `./templates/claude/CLAUDE.md` | `/home/dev/.claude/CLAUDE.md:ro` | Read-only bridge importing `AGENTS.md` |
| `./config/opencode` | `/home/dev/.opencode/seed:ro` | Read-only OpenCode workflow view for the publisher |
| `./config` | `/home/dev/.djinn-canonical:ro` | Read-only canonical workflow source for the publisher |
| `./config/mcp-servers.json` | `/home/dev/.config/mcp-servers.json:ro` | Local MCP registry |

Writes through `~/.opencode/seed` and `~/.djinn-canonical` fail with `EROFS`.
Only the default source's `config/claude` binds remain writable. With another
selected source, those Claude binds remain writable aliases of generated files
([#165](https://github.com/w2kr1stn/djinn_in_a_box/issues/165)). With OpenCode
selected, the container has no writable source alias, and delivery has a known
seed-verification problem reported as `source-changed`
([#168](https://github.com/w2kr1stn/djinn_in_a_box/issues/168)). The read-only
views protect only these two paths. A writable bind that would alias the same
files through the Djinn installation, such as a workspace root that contains it,
is refused at creation (see [Configuration](#configuration)).

Codex's `sqlite` directory is a default local-zone overlay. Djinn sets
`CODEX_SQLITE_HOME=/home/dev/.codex/sqlite`, so its SQLite stores (`state_*`,
`logs_*`, `goals_*`, `memories_*`, `queue_*`, `thread_history_*`), their
`-wal`/`-shm` files and `.sqlite-maintenance.lock` stay on each host rather than
travel through the synchronized config root. An explicit `sqlite_home` in your
Codex `config.toml` takes precedence.

Session rollouts stay shared, so continuation on another machine works with
`codex resume <id>` or `codex exec resume <id>`; the ID is in the rollout filename
under `~/.codex/sessions/YYYY/MM/DD/`. An already populated local index can omit
new sessions from the other machine in the resume picker and `--last`; session
names also use the local index. Goals, queued messages, pins/projects/read state
and remote-control enrollment are per machine. Memory extraction state is local
while generated text in `memories/` stays shared. See
[cross-machine behavior](docs/sync-across-machines.md#codex-sessions-and-local-state).

Local-zone SQLite is outside the config-root backup.

### Git identities and SSH signing

Declare each existing Git SSH alias and both host key paths with `djinn config edit`:

```toml
[git]
signing_identity = "git-work"                 # optional explicit default
allowed_signers_file = "~/.ssh/allowed_signers" # optional public signing trust

[git.identities.git-work]
hostname = "git.example.com"
user = "git"
key_file = "~/.ssh/work_git"
public_key_file = "~/.ssh/work_git.pub"

[git.identities.git-personal]
hostname = "git.example.com"
user = "git"
key_file = "~/.ssh/personal_git"
public_key_file = "~/.ssh/personal_git.pub"
```

Verify the real Git host's key on the host and put its trusted entry in
`~/.ssh/known_hosts` before starting. Djinn selects entries for declared hostnames,
including hashed entries and key markers. Missing trust or a mismatched key refuses
startup. Built-in delivery excludes the host SSH config and private keys. The generated
read-only `~/.ssh` contains `config`, `known_hosts`, `tailnet_known_hosts` (empty until
tailnet trust is delivered), `git.json`, the public keys under their original filenames,
optional `allowed_signers`, and `tailnet_config` when hostctl hosts are declared.
Alias URLs such as `git@git-work:group/repo.git`
continue to select the declared account. Hardware keys and certificates are unsupported.

A dedicated host agent loads only these keys; encrypted keys prompt on the host
terminal at startup. Djinn loads them with one `ssh-add` call, which tries the last
entered passphrase on each following key. Keys that share a passphrase ask for it
once when declared next to each other. Run Djinn from a host terminal when keys
need unlocking.
Djinn never takes a passphrase from piped or redirected input: without a terminal
on stdin, any encrypted key refuses startup, while unencrypted keys still load.
Linux host numeric UID 1000 must match the dev image. Host runtime state lives in the
owner-only `.local/state/djinn/runtime/git-agent` directory under the home recorded for
the host UID; `HOME` and `XDG_RUNTIME_DIR` do not affect its location. Both `start` and
`run` deliver `SSH_AUTH_SOCK=/run/djinn-git-agent/auth.sock`. A detached start keeps
the agent until the actual dev container stops or is removed. Concurrent creation is
refused; use `djinn clean` on the host before replacing a live container.

For SSH signing, Djinn writes `gpg.format=ssh`, an explicitly selected public signing
key and optional `gpg.ssh.allowedSignersFile` to `~/.gitconfig_local`. Add this manually
to the host's read-only Git config, adjusting the workspace path for your mode:

```gitconfig
[includeIf "gitdir:/home/dev/projects/"]
    path = /home/dev/.gitconfig_local
```

An existing `user.signingkey = ~/.ssh/work_git.pub` remains valid when that public
file is declared. Without `git.signing_identity`, Djinn omits `user.signingkey` so
host/repository choices remain effective. Enable `commit.gpgsign` yourself and supply
your own public allowed-signers trust; Djinn does not infer it from email.

Run `djinn doctor` to inspect declarations, agent state and global/repository key-file
references with their origins. Manually replace private `core.sshCommand -i` selectors,
removed `-F` configs, SSH `IdentityFile`/`Include` paths and old allowed-signers paths
with the generated config/public selectors. Repository discovery is bounded and
reports uninspected roots. Djinn never rewrites repositories or the host Git config.
See [Git keys and browser identity](SECURITY-MODEL.md#git-keys-and-browser-identity)
for the signing capability and target-key restrictions.

`--here` mounts the current directory at `/home/dev/workspace` for both `start` and
`run`; for `run` it can be combined with any number of `--mount` values. Each
`--mount SRC[:DST[:ro|rw]]` adds a host directory; without `DST`, Djinn derives
`/home/dev/mount/<basename>`, and `:ro` makes that mount read-only. The working
directory is `/home/dev/workspace` with `--here`, otherwise the first mount target;
`djinn start` without mounts passes no `--workdir`, so the Compose service default
`working_dir: ${DJINN_WORKSPACE_TARGET:-/home/dev/projects}` applies: `/home/dev/projects`
in projects mode or `/home/dev/aios` in aios mode.
The managed-source check in [Configuration](#configuration) also applies without
`--mount` or `--here`.
At creation, a writable `--mount`, `--here`, or `djinn run`'s implicit `--here`
is refused if its source overlaps a host execution input. For `--mount`, append
`:ro` or remove it. For `--here`, start from a directory outside the input or use
`--mount SRC:ro`. `djinn doctor` reports affected `[mounts.*]` declarations as
FAIL rows; it cannot check invocation `--mount` or `--here` binds.
User mounts cannot equal or contain the active workspace target; children remain
valid. The unused root is available for explicit user mounts. Djinn creates only
the selected workspace bind. Targets at or below a Djinn-managed root are refused
for both `:rw` and `:ro`, following the shared rule for declared mounts below.
`djinn run` without `--mount` and without `--here` keeps its implicit `--here`
behavior.

Named volumes are Docker-managed and host-local:

| Volume name | Container path | Category |
| --- | --- | --- |
| `djinn-opencode-data` | `/home/dev/.local/share/opencode` | data |
| `djinn-packages` | `/var/lib/djinn-packages` | data |
| `djinn-uv-cache` | `/home/dev/.cache/uv` | cache |
| `djinn-tools-cache` | `/home/dev/.cache/djinn-tools` | cache |
| `djinn-vscode-server` | `/home/dev/.vscode-server` | cache |
| `djinn-vscode-workspaces` | `/home/dev/workspaces` | data |

The backup command includes credentials, repo-dotfiles, and data by default,
including the root-owned `djinn-packages` list at `/var/lib/djinn-packages`. It does not include cache volumes
unless you explicitly request the `cache` category.

Declared targets must be absolute. They cannot equal or contain a built-in,
reserved, assigned zone, active workspace or `--mount`/`--here` target; nested
declared targets are refused too. Target reservations cover all Docker modes,
even inactive ones. Children of built-in, reserved, zone or invocation targets
are allowed, with one exception shared with invocation `--mount`: neither may sit
at or below the Djinn-managed roots `/home/dev/.cache/uv`, `/home/dev/.cache/djinn-tools`,
`/home/dev/.local/share/fnm`, `/home/dev/.vscode-server` or `/home/dev/workspaces`.
Their existing recursive ownership repair remains in place. The root-owned
`/var/lib/djinn-packages` is also reserved, with no dev ownership repair. Generated SSH delivery
(`/home/dev/.ssh`), the Git socket directory (`/run/djinn-git-agent`) and
`/home/dev/.gitconfig_local`, desktop directories (`/run/djinn/dbus`, `/run/djinn/audio`)
and the agent-Docker endpoint (`/run/djinn/agent-docker`) follow the same managed-root
rule, including image aliases. Declared mounts support directory binds and named
volumes only. Binds default to read-write;
`read_only = true` delivers them read-only to dev and the agent daemon.
File binds remain unsupported; invocation `--mount ...:ro` remains available.

At startup an empty declared-volume root that the dev user cannot write receives
one ownership repair of the root itself. Existing contents are never changed;
a populated, unwritable root produces a warning and needs manual ownership repair.
Ownership-helper errors stop startup. This helper requires rebuilding the image
with `djinn build` when upgrading to declaration support.

`djinn status` and `djinn clean volumes` list declared volumes by category,
including `none`, and show absent declared volumes as `not created`.

| Declared volume category | Backup and restore | Cleanup |
| --- | --- | --- |
| `data` | Default backup, or explicit `--categories data`; restore while currently declared as data/cache | `clean volumes --data`, `clean all`, or by actual name |
| `cache` | Only with `--categories cache`; restore while currently declared as data/cache | `clean volumes --cache`, `clean all`, or by actual name |
| `none` | Never backed up or restored; no backup selector | Only `clean all` or by actual name; no `--none` selector |

The built-in `djinn-packages` data volume is included in default backup and
restore. `clean volumes --data` and `clean all` delete it; `djinn clean` keeps it.

Backup includes only existing selected volumes. Declared archives use
`declared-volumes/<actual-name>.tar.gz`; built-in archive names and layout stay
unchanged. Restore uses the archive contents, skips removed or currently `none`
declarations with a warning, and accepts a change between `data` and `cache`.
Root `djinn-sync-*` archives still restore credentials/config-root paths, even
when a declared volume has the same name in the separate namespace.

## Host control windows

On a Linux Docker host, `djinn build` builds and installs the static supervisor.
Then declare hosts in `config.toml`:

```toml
[hostctl]
default_duration = "2h"

[hostctl.hosts.host-a]
address = "host-a.example.ts.net"
user = "operator"
```

```sh
djinn hostctl on --for 10m
djinn hostctl status
djinn hostctl check [HOST]
djinn hostctl limit 5
djinn hostctl off
```

Durations are positive integer minutes (`10m`) or hours (`2h`), at most 24h.
`limit` resets the remaining minutes (1–1440). Repeated `on` refuses and points
to `limit`; `off` is idempotent and works without configuration. The deadline
starts at `on`, including login time. Enrollment is asynchronous; obtain its
login link with `status` on the host terminal. Before the first opening, follow
[enrollment and tailnet policy](SECURITY-MODEL.md#enrollment-and-tailnet-policy)
for node setup, target configuration and the policy example.

Once `status` reports open, run `djinn hostctl check [HOST]` at the host terminal:
omit `HOST` for all declared targets, or use `djinn hostctl check host-a` for one
alias. It connects in declaration order as each target's configured user and
runs `true`. The host's OpenSSH client shows any SSH approval banner; Djinn does
not read or alter the banner. Approve in your own browser. Agents may then reuse
the approval with `ssh host-a` within the target policy's `checkPeriod` (see
limits below). The enrollment login link from `status` is separate from the
target's SSH approval banner. `check` requires a terminal on stdin and stderr
and an `ssh` client on the host PATH. Ctrl-C ends the check and removes its
private temporary configuration. `on` prints: "Once status reports open, djinn
hostctl check shows the SSH approval link in this terminal."

Tailscale documents check reuse only for the applicable `checkPeriod` (default
12 hours); its exact cache key across source node, SSH user and destination is
undocumented. With `checkPeriod: "always"`, a host check does not approve the
agent's later connection. Each check is a real login as the target user running
`true`, visible as a connection in the relay log and as a login/session in the
target's auth log.

Approving the banner of a host check approves the helper's source identity for
that target user: within the target policy's `checkPeriod` it can also cover the
agents' later connections as that user; that is the intended effect.

Generated aliases remain present while the window is closed. Git and signing
work independently.
Generated SSH delivery requires host numeric UID 1000, matching the dev image,
so the dev user can read the owner-only public files.

`status` and the refusal of `on` group sealing causes and unknowns by bind,
naming up to three items per class and counting larger classes; standalone
findings keep their own lines. Status prints `djinn doctor lists each item.`
whenever a finding is grouped under a bind. The explicit
`--allow-unsealed` option and creator close/pause behavior are defined in
[sealed deployments](SECURITY-MODEL.md#sealed-deployments-and-trusted-controller).
Run `djinn doctor` for every individual sealing cause and unknown, plus
per-address reached/blocked/unknown probe results for both dev and its verified
companion; every address must be blocked in both namespaces. Companion
replacement or profile drift closes an open window. No dev or authenticated
peer snapshot means deferred.
Configure the [host networking prerequisite](SECURITY-MODEL.md#direct-routes-and-target-authority)
before relying on these checks.

The trailing `Summary` block shows the window and approximate time left, sealing
counts and up to three unknown groups followed by up to three cause groups,
relay state and current-generation peer aliases, and one next action. It uses
the existing snapshot and makes no SSH connection. An open relay does not mean
SSH approval has completed. Helper-owned deadlines remain authoritative.

For journal/connection-log locations, cutoffs and observation limits, see
[window state, expiry and logs](SECURITY-MODEL.md#window-state-expiry-and-logs).
For host-local identity storage and cleanup, see
[sync guidance](docs/sync-across-machines.md#hostctl-node-identity).

## Resources

The model defaults are:

- CPU limit: `4`
- memory limit: `8G`
- CPU reservation: `1`
- memory reservation: `2G`

During `djinn init`, Djinn suggests resource values from the host:

- CPU limit: half of detected CPUs, clamped between `1` and `128`
- memory limit: half of detected memory, at least `2G`
- CPU reservation: one quarter of the suggested CPU limit, at least `1`
- memory reservation: one quarter of the suggested memory limit, at least `1G`

If host detection fails, Djinn falls back to the model defaults: 4 CPUs, 8G
memory, 1 CPU reserved, and 2G reserved.

Change resource settings with:

```sh
djinn config set resources.cpu_limit 6
djinn config set resources.memory_limit 12G
djinn config set resources.cpu_reservation 2
djinn config set resources.memory_reservation 4G
```

Docker Compose receives these values through the CLI environment bridge as
`CPU_LIMIT`, `MEMORY_LIMIT`, `CPU_RESERVATION`, and `MEMORY_RESERVATION`.

## Backup and Restore

Stop running Djinn containers before backup or restore:

```sh
djinn clean
```

Create a backup:

```sh
djinn backup
```

By default, this backs up:

- `credentials`: config-root directories for Claude Code, Codex CLI, OpenCode,
  the GitHub CLI, and the `age` encryption identity store
- `repo-dotfiles`: the optional config-root `repo-dotfiles` directory if present
- `data`: the OpenCode data, VS Code workspace, and `djinn-packages` named volumes

Codex's local-zone SQLite stores are excluded, including durable goals, queued
messages and preferences. Arrange a separate host-local backup if needed;
the shared session rollouts do not restore all of this state.

Post-mortem records under `~/.local/state/djinn/post-mortem/` are never part of
a Djinn backup. Exclude `~/.local/state/djinn` from operator-managed backups and
synchronization too.

The archive is written to:

```text
~/.djinn/backups/djinn-backup-YYYY-MM-DD.tar.gz.age
```

`age` prompts twice for a passphrase at your terminal. Only the newest
`djinn-backup-*.tar.gz` or `djinn-backup-*.tar.gz.age` archive is kept in that
directory. Existing cleartext archives remain restorable and are removed after
a successful new backup.

Restore the newest backup:

```sh
djinn restore
```

Restore asks for confirmation and overwrites the restored volume and config-root
directory contents. For encrypted backups, `age` prompts for the passphrase at
the terminal before anything is restored.

`djinn restore` keeps logins created after the backup: before its first
destructive step it parks the union of the current volume and config root in a
private recovery session and merges it back after the restore. If that export or
parking fails, the restore stops before it changes anything;
`--discard-unexported-credentials` restores anyway and loses logins that were
only in the volume. While the restore writes, audits and starts leave every
recovery session alone; the restore merges them all once it is done. If that
merge fails, the restore only warns and the sessions stay for the next audit or
start. A restored `credentials.removed.json` from before a later logout
everywhere can make that login reappear.

To back up specific categories:

```sh
djinn backup --categories credentials --categories data
```

To deliberately create a cleartext archive, use:

```sh
djinn backup --no-encrypt
```

For cross-machine usage, see
[docs/sync-across-machines.md](docs/sync-across-machines.md).
For encryption and credential implications, see the
[security model](SECURITY-MODEL.md#credentials-backups-and-outbound-traffic).

## Cleanup and Uninstall

Remove running Djinn containers while keeping volumes, config, and the network:

```sh
djinn clean
```

Before `djinn clean` or `djinn start` removes a dev, companion or helper
container that stopped without Djinn, it writes a post-mortem record to
`~/.local/state/djinn/post-mortem/`. The directory is `0700`, files are `0600`,
and retention keeps at most the last 10 records within 8 MiB. The command prints
only the record path, never its contents. Recording is best effort: failure
does not block recovery. See the [security model](SECURITY-MODEL.md#window-state-expiry-and-logs)
for confidentiality and evidence limits.

Removal of a large stale container waits up to 60 s. A timeout names the object,
explains that Docker may finish removing it in the background, and says retrying
is safe. A record already written remains available after a removal timeout.

`djinn clean` keeps every declared volume and bind. Category cleanup includes
declared volumes of that category. It also keeps built-in named volumes such as
`djinn-packages`. `backup = "none"` excludes backup and restore; it offers no
protection from `clean all`. Declared bind sources and markers, arbitrary host
binds, the workspace, SSH and shared/local zone data are outside the cleanup sets.

List managed volumes and config-root paths:

```sh
djinn clean volumes
```

Delete cache volumes:

```sh
djinn clean volumes --cache
```

Delete data volumes:

```sh
djinn clean volumes --data
```

This deletes the root-owned list at `/var/lib/djinn-packages` in `djinn-packages`
along with the other data volumes.

Before Djinn deletes the `djinn-opencode-data` volume
(`djinn clean volumes --data` or `djinn clean volumes djinn-opencode-data`) it
exports that volume's logins through a one-off container and merges them into the
config root. If that fails, the volume stays and Djinn names the reason;
`--discard-unexported-credentials` deletes it anyway and loses logins that were
only in the volume. `djinn clean all` uses the same export and only warns when it
fails. `djinn restore` parks these logins instead of merging them first; see
[Backup and Restore](#backup-and-restore).

Clear credential directories under the config root:

```sh
djinn clean volumes --credentials
```

Without `--opencode-id` this logs out every OpenCode login everywhere, then
clears the credential directories; `credentials.removed.json` is kept, and each
host removes those IDs from its database at its next sync. To log one login out
everywhere, name its ID:

```sh
djinn clean volumes --credentials --opencode-id <id>
```

Clear the optional `repo-dotfiles` config-root directory:

```sh
djinn clean volumes --repo-dotfiles
```

Delete a specific managed named volume:

```sh
djinn clean volumes djinn-uv-cache
```

`djinn clean volumes NAME` refuses names that do not start with `djinn-`.

Remove containers, managed named volumes (including `djinn-packages`),
config-root sync-path contents, and the Docker network. This does not remove
zone data in `<config-root>.shared` or
`<config-root>.local`:

```sh
djinn clean all
```

Use `--force` to skip prompts for destructive cleanup commands:

```sh
djinn clean all --force
```

To remove the CLI installed by `uv tool install`, use `uv tool uninstall`:

```sh
uv tool uninstall djinn-in-a-box
```

That only removes the installed Python CLI package. It does not delete your
Docker image, Docker volumes, config file, config root, backups, or sessions.
To discard retained post-mortem records when uninstalling, delete
`~/.local/state/djinn/post-mortem/` by hand.

## MCP

MCP support is optional. The base compose file mounts the local
`config/mcp-servers.json` registry into the container, and the entrypoint
registers enabled entries for supported agents at startup.

## Suite Mode

Djinn is standalone by default. External applications can integrate through the
same session workspace contract used by `djinn session`: host files under
`~/.djinn/sessions/<project>` appear in the running container under
`/home/dev/sessions/<project>`. See
[docs/suite-integration.md](docs/suite-integration.md) for the public session
contract and optional host-local MCP service pattern.

## Status and Audit Commands

Show current containers, known volumes, config-root paths, networks, and service
status:

```sh
djinn status
```

Services includes the [remote agent server snapshot](#server-status).
Use `djinn serve --status` for the detailed table and endpoint hints.

Open an interactive installation check or investigate a symptom:

```sh
djinn audit
djinn audit "Container fails to start" --agent opencode
djinn config set assistant.agent opencode
```

Audit uses its own slim assistant image, built on first use and when its agent,
Dockerfile or pinned version changes. It works without the dev image/container
and with broken configuration; loader errors are included in the first input.
The [audit guide](assistant/audit-briefing.md) points into the installation docs.
Run it as your regular user in a foreground terminal on Linux with a local Unix
Docker socket. Remote Docker contexts are unsupported.

The installed repository, Djinn config directory, `~/.djinn` and Docker socket
are mounted read-write, together with existing selected credential files.
Custom config roots outside `~/.djinn` supply only these credential files;
workspace trees and custom zone roots are not mounted. Harness settings, hooks,
plugins and history are fresh in the temporary container. Claude uses normal
manual approvals; Codex runs without its sandbox (`danger-full-access`, which needs
user namespaces the container does not grant) and asks when the model requests approval
(`on-request`); OpenCode asks for edits and shell commands. No approval bypass is used.
Credential refreshes persist at ordinary/signal exit; abrupt SIGKILL cannot
flush them. Missing credentials require signing in through the normal agent
setup first. The agent hands host-only build/sync/recreation commands back to you.
`--rm` removes the assistant session container; approved file edits and Docker
objects created during repairs persist.

An OpenCode audit gets a private session directory under
`~/.local/state/djinn/opencode-audit/` (0700, not synced, not backed up). It is
seeded from the config root, and the audit's database lives in that directory, so
it survives a crash. At the end Djinn merges new logins and token refreshes back
by union — an audit never logs out anywhere — and deletes the session; a session
left over after a crash is recovered by the next `djinn audit` or `djinn start`,
and `djinn doctor` reports leftovers.

Resolve the latest Claude Code, Codex and OpenCode versions from npm:

```sh
djinn update
```

`djinn update` needs no `djinn init` and writes the versions atomically to
`~/.config/djinn_in_a_box/agent-versions.toml`. It writes nothing in the installed
checkout. If any lookup fails, times out or returns an invalid version, the
previous record stays unchanged and the command exits with an error. npm's own
cache and logs follow the operator's npm configuration (default `~/.npm`).

Each image build uses the numerically higher `x.y.z` version from the record or
the upstream Dockerfile default. A later pull with newer defaults therefore
cannot downgrade an agent. Dev builds pass only higher local versions as build
args; the assistant uses the same policy for its selected agent. An unreadable
or invalid record stops either build with an error naming the file. Delete the
record to return to upstream defaults.

Rebuild afterward to install the resolved versions:

```sh
djinn build
```

Maintainers bump upstream defaults with `scripts/update-agents.sh` in a
development checkout and submit the Dockerfile changes through a pull request.
After any default bump, by script or by hand, build once with the defaults
alone: move `~/.config/djinn_in_a_box/agent-versions.toml` aside so no higher
local version replaces a default, run `djinn build`, and commit the bump only
after that build succeeds. CI does not build the image.
The script's `--print` mode resolves versions without editing files. After a
Claude Code bump, also run `scripts/probe-claude-isolated.py`; see
[Re-probing the isolation](#re-probing-the-isolation).

## Typical Workflows

Interactive shell in your configured workspace root:

```sh
djinn start
```

Interactive shell with the current directory mounted as a workspace:

```sh
djinn start --here
```

Interactive shell with proxied Docker access and outbound firewall enabled:

```sh
djinn start --docker --firewall
```

Headless read-only analysis of the current directory:

```sh
djinn run claude "Review the current changes."
```

Headless write task with a timeout:

```sh
djinn run codex "Implement the missing test." --write --timeout 300
```

Create and open a reusable session workspace:

```sh
djinn session --project my-project --create
```

Run a headless prompt in that session workspace:

```sh
djinn session --project my-project --prompt "Summarize the current state."
```

Open a second shell into a running container:

```sh
djinn enter
```

## Troubleshooting

Start with:

```sh
djinn doctor
```

Common fixes:

- Missing config: run `djinn init`.
- Docker not reachable: start the Docker daemon, then rerun `djinn doctor`.
- Image missing: run `djinn build`.
- Seed targets missing: run `djinn doctor --fix` or `djinn init`.
- No running container for `djinn enter`: start one with `djinn start`.
- Session workspace missing: create it manually or pass `djinn session --create`.
- Unknown agent: run `djinn agents` and check
  `~/.config/djinn_in_a_box/agents.toml` if you use overrides.

## License

Djinn in a Box is licensed under the MIT License. See `LICENSE` for details.
