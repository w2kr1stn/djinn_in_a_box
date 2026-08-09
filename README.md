# Djinn in a Box

[![CI](https://github.com/w2kr1stn/djinn_in_a_box/actions/workflows/ci.yml/badge.svg)](https://github.com/w2kr1stn/djinn_in_a_box/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](pyproject.toml)

Djinn in a Box is a Docker-based development environment for running CLI coding
agents with isolated credentials and a managed container lifecycle.

It ships the mechanism:

- a Docker image with Claude Code, Gemini CLI, Codex CLI, and OpenCode
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
  container with SIGTTOU until it dies. `djinn start` refuses that shape.
- Run a one-shot agent prompt with `djinn run`.
- Attach another shell to a running container with `djinn enter`.
- Keep reusable session workspaces under `~/.djinn/sessions/` with
  `djinn session`.
- Diagnose the host and seeded configuration with `djinn doctor`.
- Back up and restore the managed volumes and config-root directories with
  `djinn backup` and `djinn restore`.

Credentials are separated by CLI. By default, Claude Code, Gemini CLI, Codex CLI,
OpenCode, and the GitHub CLI each get their own host directory under the
configured Djinn config root. The container sees those directories at the paths
each CLI expects. An `age` encryption identity directory is provisioned the same
way and appears at `~/.config/age`, so plain `age` keys persist across runs
(`age -i ~/.config/age/keys.txt`); SOPS users set `SOPS_AGE_KEY_FILE` to that path.

## Requirements

Install these on the host:

- Docker Engine or Docker Desktop
- Docker Compose v2, available as `docker compose`
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

Claude Code, Gemini CLI, and OpenCode select that flow on their own inside the
container. **Codex needs to be told:** plain `codex login` starts a login server
on a container-local port that your host browser cannot reach, and the sign-in
never completes. Use the device flow instead:

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
| Claude Code, Gemini CLI, Codex, OpenCode, GitHub CLI | your configured config root | `credentials` |

`djinn backup` includes both categories by default. If you back up selectively,
copy the matching credential category.

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

Supported `djinn config set` keys are:

| Key | Meaning | Default or source |
| --- | --- | --- |
| `general.code_dir` | Host directory mounted as `/home/dev/projects` | chosen during `djinn init` |
| `general.timezone` | IANA timezone passed as `TZ` | detected from host, fallback `UTC` |
| `general.config_root` | Host root for credentials and local CLI state | `~/.djinn/config` |
| `resources.cpu_limit` | Compose CPU limit | `4` |
| `resources.memory_limit` | Compose memory limit | `8G` |
| `resources.cpu_reservation` | Compose CPU reservation | `1` |
| `resources.memory_reservation` | Compose memory reservation | `2G` |
| `shell.skip_mounts` | Skip host shell config mounts | `false` |
| `shell.omp_theme_path` | Optional Oh My Posh theme file mounted read-only | unset |
| `config_sync.source` | Native global workflow source: `claude`, `codex`, or `opencode` | `claude` |

Memory values must use Docker-style units such as `8G`, `4096M`, or `512K`.
CPU values are positive integers. Reservations cannot exceed limits.

`DJINN_CONFIG_ROOT` is not something you normally have to export. The CLI loads
`config.toml` and injects Compose interpolation variables, including
`DJINN_CONFIG_ROOT`, `CODE_DIR`, `TZ`, and the resource settings, into the
`docker compose` subprocess environment. If you do export `DJINN_CONFIG_ROOT`,
that environment value takes precedence for config-root resolution.

## Global Agent Workflow Ownership

Each Djinn deployment selects one native global workflow as its source of truth:

```sh
djinn config set config_sync.source claude   # or codex / opencode
djinn config sync
djinn config status  # exit 0 when clean, otherwise exit 1
```

When changing authority, use **switch → sync → edit**: select the new source,
run `djinn config sync`, then edit it. Sync requires a valid source and refuses
to overwrite an edited managed target. It can adopt the recorded state of an
existing deployment, but an unowned file at a managed path is a collision.

The choice is deployment-wide. The shared demo is one deployment with one
source; this is not a per-tenant setting. Selecting a workflow source does not
select which agent `djinn run` or `djinn session` launches.

The source stays in its native project-local root:

| Tool | Authoritative root instructions | Generated companion | Native agents | Native commands |
| --- | --- | --- | --- | --- |
| Claude Code | `config/claude/CLAUDE.md` | `AGENTS.md` | `agents/*.md` | `commands/*.md` |
| Codex | `config/codex/AGENTS.md` | `CLAUDE.md` | `agents/*.toml` | `skills/command-*/**` |
| OpenCode | `config/opencode/AGENTS.md` | `CLAUDE.md` | `agents/*.md` | `commands/*.md` |

The cross-tool projection surface also includes `skills/<name>/**`,
`context/**`, and `scripts/**`. Hooks are native-only per tool: the known
startup/security/ready implementations and their `SessionStart`, `PreToolUse`,
and `Stop` registrations stay in that tool's own view. They are optional and
author-owned, validated there when present, never projected to another tool,
and never stale-removed. The Claude-only `/codex-review` command follows the
same source-only rule. Repository-local instructions, agents, skills, and
commands remain outside this global feature and are never rewritten.

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
Neighboring keys in shared JSON or TOML carriers stay operator-owned. A legacy
installation is adopted safely during sync; obsolete canonical hook entries are
released without deleting their files or carrier keys. It does not introduce
another active manifest format.

`djinn config status` is read-only: it reports the selected source, sanitized
locations, one drift class, and one remedy without printing workflow or settings
bodies. Its exit status is `0` only for `clean`, and `1` for every other state.
`djinn config sync` is the explicit full writer and exits non-zero when blocked.
The five states are:

| State | Meaning | Remedy |
| --- | --- | --- |
| `clean` | The selected source and all managed views match their manifests. | None. |
| `source-changed` | The source projection changed or was unstable during publication. | Run `djinn config sync`, then retry. |
| `target-drift` | A manifest-managed item was edited. | Restore or move the modified item, then retry. |
| `collision` | An unmanaged item occupies a managed path. | Move or remove the conflicting item, then retry. |
| `invalid-or-semantic` | The source is invalid, empty, or contains a non-portable artifact. | Author or edit the artifact natively in the target tool's view, or make the source form portable. |

There is no semantic-provider fallback: workflow sync never invokes a provider.
Normal `start`, `run`, and `session` preparation repairs only deterministic
`source-changed` projection drift; all other states stop the command before an
agent starts. Preflight, status, audit, and sync never seed or repair source
roots. Only `djinn init` and `djinn doctor --fix` perform source seeding.

Host fallback for Claude, Codex, or OpenCode receives the selected canonical
view through the shared publisher. The container OpenCode runtime is refreshed
the same way from the read-only canonical mount. The workflow publisher requires
an image marked `djinn.workflow.publisher=1`; an old image causes a content-free
`Rebuild/recreate required.` failure before Compose starts or a running-container
session refresh executes.

Compose Claude is the deliberate exception: it is manifestless and uses direct
mounts, including both `CLAUDE.md` and the generated `AGENTS.md` companion,
together with the existing settings merge. The publisher never writes into the
Compose Claude runtime root.

## Doctor

Run:

```sh
djinn doctor
```

The doctor command checks Docker, the Docker daemon, socket permissions, Compose
v2, the main config, the projects directory, the config root, the image, the
Docker network, the optional Docker MCP plugin, desktop notification detection,
and seed target presence.

For idempotent local repairs:

```sh
djinn doctor --fix
```

`--fix` provisions expected host directories, repairs missing seed targets, and
creates the Docker network when possible. It does not install Docker, repair an
invalid config file, or build the image.

## The Blank-Space and Seed Model

Djinn treats your project-local `config/` directory as blank space. It is
root-anchored in `.gitignore`, seeded on first run, and then owned by you.

Only `djinn init` and `djinn doctor --fix` copy missing seed targets from
`templates/seed/` into local paths:

| Seed source | Local target | Purpose |
| --- | --- | --- |
| `templates/seed/config/claude/CLAUDE.md` | `config/claude/CLAUDE.md` | neutral Claude Code instruction starting point |
| `templates/seed/config/claude/settings.json` | `config/claude/settings.json` | minimal Claude Code settings |
| `templates/seed/config/claude/skills/` | `config/claude/skills/` | empty local skills directory |
| `templates/seed/config/claude/commands/` | `config/claude/commands/` | empty local commands directory |
| `templates/seed/config/claude/agents/` | `config/claude/agents/` | empty local Claude subagent directory |
| `templates/seed/config/claude/context/` | `config/claude/context/` | empty local context directory |
| `templates/seed/config/claude/scripts/` | `config/claude/scripts/` | empty local scripts directory |
| `templates/seed/config/gemini/` | `config/gemini/` | empty Gemini seed directory |
| `templates/seed/config/opencode/` | `config/opencode/` | empty OpenCode seed directory |
| `templates/seed/config/mcp-servers.json` | `config/mcp-servers.json` | empty local MCP registry |
| `templates/seed/config/agents.toml.example` | `config/agents.toml.example` | documentation-only agent override example |
| `templates/seed/tools.txt` | `tools/tools.txt` | optional runtime installer list |
| `templates/seed/packages.txt` | `packages.txt` | optional Debian package list |

Existing targets are left alone when they already have the expected type. This
means seed files are starting points, not managed config.

Bring your own native workflow in the root selected by `config_sync.source`.
The seeded Claude `CLAUDE.md`, `settings.json`, `mcp-servers.json`, and empty
local directories are deliberately minimal. Replace the selected source with
your own instructions, settings, commands, skills, hooks, or subagents as
needed; MCP entries remain separate.

`packages.txt` is read at image build time and may list extra Debian packages,
one per line. `tools/tools.txt` is read at container start and may list installer
names that correspond to scripts under `tools/installers/`.

### Devcontainer Template Status

There is no `.devcontainer/` directory in the repository. The only devcontainer
artifact is `templates/devcontainer.json`, and that path is experimental and
out of scope for v1. It uses its own named-volume mounts and does not run the
seed/config-root setup path, so credentials and seed files would be skipped.

## Agents

Djinn has four built-in agent definitions:

| Agent name | Binary | Default headless mode | Notes |
| --- | --- | --- | --- |
| `claude` | `claude` | `claude -p` | read-only uses `--permission-mode plan`; write mode uses `--dangerously-skip-permissions` |
| `gemini` | `gemini` | `gemini -p` | model flag is `-m` |
| `codex` | `codex` | `codex exec` | write mode uses `--full-auto` |
| `opencode` | `opencode` | `opencode run` | read-only uses `--agent plan`; model flag is `-m` |

List the effective definitions:

```sh
djinn agents
djinn agents --verbose
djinn agents --json
```

### Agent Overrides

The only automatically honored agent override path is:

```text
~/.config/djinn_in_a_box/agents.toml
```

If that file exists, Djinn loads it instead of the built-in defaults. If it does
not exist, Djinn uses the built-in definitions from the package.

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
| `djinn start` | Runs the `dev` service interactively with `docker compose run --rm`; removed after exit. `--detach` uses `docker compose up -d` instead and leaves no client attached | Starts in `/home/dev/projects` without an extra mount; `--here` mounts `/home/dev/workspace`; repeatable `--mount` values add directories at chosen or derived targets | Daily interactive shell; `--detach` for a long-lived container |
| `djinn enter` | Uses `docker exec -it <running-container> zsh` | Enters an already running Djinn container | Open a second shell while `djinn start` is still running |
| `djinn run AGENT PROMPT` | Runs the `dev` service headlessly with `docker compose run --rm -T`; removed after exit | Without `--mount` and without `--here`, mounts the current directory at `/home/dev/workspace`; `--here` keeps that mount when combined with repeatable `--mount` values | One-shot agent prompts |
| `djinn session` | Uses `docker exec` into a running `djinn` container when available; otherwise host fallback preflight checks the selected agent binary on `PATH`. Claude, Codex, and OpenCode host fallback receives that agent's canonical workflow at its native host root. Running-container OpenCode sessions refresh the live runtime through the shared publisher before invocation. | Uses `~/.djinn/sessions/<project>` on the host and `/home/dev/sessions/<project>` in the container; `--create` creates the host workspace | Reusable session workspaces |

Common `start` options:

| Option | Effect |
| --- | --- |
| `--docker`, `-d` | Adds `docker-compose.docker.yml`, starts the Docker socket proxy, and sets `DOCKER_HOST=tcp://docker-proxy:2375` inside the dev container |
| `--docker-direct` | Adds `docker-compose.docker-direct.yml` and mounts `/var/run/docker.sock` directly into the dev container |
| `--firewall`, `-f` | Sets `ENABLE_FIREWALL=true`; the entrypoint initializes the network firewall |
| `--here` | Mounts the current directory as `/home/dev/workspace` and uses it as the working directory |
| `--mount SRC[:DST[:ro\|rw]]`, `-m …` | Repeatable host-directory mount. Without `DST`, it maps to `/home/dev/mount/<basename>`; append `:ro` for read-only. |

`--docker` and `--docker-direct` are mutually exclusive.

### What `--firewall` allows

The firewall denies outbound traffic by default and permits a fixed domain list
in `scripts/init-firewall.sh`: package registries, the four bundled CLIs' API and
sign-in endpoints, GitHub, and the Docker networks. Every address a domain
resolves to is permitted.

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

## Docker Access Modes

The base mode gives the container no Docker socket access. This is the safest
default and is enough for ordinary editing, tests, and agent conversations that
do not need to create containers.

`djinn start --docker` enables Docker access through a Docker socket proxy. The
dev container talks to `docker-proxy` over the internal Docker network, and the
proxy bind-mounts the host socket read-only. The proxy permits a limited API
surface and blocks several high-risk Docker API areas.

`djinn start --docker-direct` mounts `/var/run/docker.sock` directly into the
container. This gives the container broad control over the host Docker daemon.
Use it only when the proxied mode is insufficient.

Read [DOCKER-SOCKET-SECURITY.md](DOCKER-SOCKET-SECURITY.md) before enabling
Docker socket access, especially direct access.

## Storage and Mounts

Djinn uses both bind mounts and named volumes. They serve different purposes.

Bind mounts are host paths that you can inspect and manage directly:

| Host path | Container path | Purpose |
| --- | --- | --- |
| `${DJINN_CONFIG_ROOT}/claude` | `/home/dev/.claude` | Claude Code credentials and state |
| `${DJINN_CONFIG_ROOT}/gemini` | `/home/dev/.gemini` | Gemini CLI credentials and state |
| `${DJINN_CONFIG_ROOT}/codex` | `/home/dev/.codex` | Codex CLI credentials and state |
| `${DJINN_CONFIG_ROOT}/opencode` | `/home/dev/.opencode` | OpenCode state |
| `${DJINN_CONFIG_ROOT}/gh` | `/home/dev/.config/gh` | GitHub CLI state |
| `${DJINN_CONFIG_ROOT}/age` | `/home/dev/.config/age` | age encryption identities (`keys.txt`) |
| `${CODE_DIR}` | `/home/dev/projects` | Your projects directory |
| `${HOME}/.djinn/sessions` | `/home/dev/sessions` | Session workspaces |
| `~/.ssh` | `/home/dev/.ssh:ro` | Read-only SSH access |
| `~/.gitconfig` | `/home/dev/.gitconfig:ro` | Read-only Git config |
| `./config/claude` | `/home/dev/.claude_seed` | Local Claude seed and settings sync source |
| `./config/claude/CLAUDE.md` and `AGENTS.md` | matching files in `/home/dev/.claude` | Direct Compose-Claude instruction mounts |
| `./config/gemini` | `/home/dev/.gemini_seed` | Local Gemini seed source |
| `./config/opencode` | `/home/dev/.opencode/seed` | Local OpenCode seed source |
| `./config` | `/home/dev/.djinn-canonical:ro` | Read-only canonical workflow source for the publisher |
| `./config/mcp-servers.json` | `/home/dev/.config/mcp-servers.json:ro` | Local MCP registry |

`--here` mounts the current directory at `/home/dev/workspace` for both `start` and
`run`; for `run` it can be combined with any number of `--mount` values. Each
`--mount SRC[:DST[:ro|rw]]` adds a host directory; without `DST`, Djinn derives
`/home/dev/mount/<basename>`, and `:ro` makes that mount read-only. The working
directory is `/home/dev/workspace` with `--here`, otherwise the first mount target;
`djinn start` without mounts passes no `--workdir`, so the Compose service default
`working_dir: /home/dev/projects` applies.
`djinn run` without `--mount` and without `--here` keeps its implicit `--here`
behavior.

Named volumes are Docker-managed and host-local:

| Volume name | Container path | Category |
| --- | --- | --- |
| `djinn-opencode-data` | `/home/dev/.local/share/opencode` | data |
| `djinn-uv-cache` | `/home/dev/.cache/uv` | cache |
| `djinn-tools-cache` | `/home/dev/.cache/djinn-tools` | cache |
| `djinn-vscode-server` | `/home/dev/.vscode-server` | cache |
| `djinn-vscode-workspaces` | `/home/dev/workspaces` | data |

The backup command includes credentials, repo-dotfiles, and data by default. It
does not include cache volumes unless you explicitly request the `cache`
category.

## Credential Security

Djinn isolates credentials per CLI. It does not encrypt them. Understand the
model before you store a high-value key such as an `age` identity.

- **Cleartext on the host.** Credential directories under the config root are
  ordinary files guarded by filesystem permissions. Djinn creates new credential
  directories with mode `0700`. A config root provisioned before that became the
  default keeps the looser mode until you repair it: `djinn doctor` reports the
  drift, `djinn doctor --fix` tightens those directories to `0700`.
- **Readable by every agent in the container.** Each credential is mounted where
  its tool expects it, so the `dev` user — and therefore every coding agent you
  run — can read all of them. An `age` identity is a master decryption key. An
  agent running in write mode (`--dangerously-skip-permissions`, `--full-auto`)
  acts without approval, so a prompt injection reaching that agent can read the
  key. Run `djinn start --firewall` when an agent processes untrusted content, so
  a leaked secret cannot be sent outbound.
- **Backups are encrypted by default.** `djinn backup` writes an age-encrypted
  `tar.gz.age` archive under `~/.djinn/backups/`. `age` prompts for the
  passphrase directly at your terminal; Djinn never receives or stores it.
  A forgotten passphrase makes that archive unrecoverable. Use `--no-encrypt`
  only when you explicitly need a cleartext archive, and protect it as
  carefully as the credentials themselves.

Read [DOCKER-SOCKET-SECURITY.md](DOCKER-SOCKET-SECURITY.md) before enabling
Docker socket access, which widens this surface further.

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

- `credentials`: config-root directories for Claude Code, Gemini CLI, Codex CLI,
  OpenCode, the GitHub CLI, and the `age` encryption identity store
- `repo-dotfiles`: the optional config-root `repo-dotfiles` directory if present
- `data`: the OpenCode data and VS Code workspace named volumes

The archive is written to:

```text
~/.djinn/backups/djinn-backup-YYYY-MM-DD.tar.gz.age
```

`age` prompts twice for a passphrase at your terminal. Keep it safe: Djinn
cannot recover a forgotten passphrase. Only the newest
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

To back up specific categories:

```sh
djinn backup --categories credentials --categories data
```

To deliberately create a cleartext archive (for example, for a controlled
one-time migration), use:

```sh
djinn backup --no-encrypt
```

For cross-machine usage, see
[docs/sync-across-machines.md](docs/sync-across-machines.md).

## Cleanup and Uninstall

Remove running Djinn containers while keeping volumes, config, and the network:

```sh
djinn clean
```

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

Clear credential directories under the config root:

```sh
djinn clean volumes --credentials
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

Remove containers, managed named volumes, config-root sync-path contents, and the
Docker network:

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

## MCP

MCP support is optional. The base compose file mounts the local
`config/mcp-servers.json` registry into the container, and the entrypoint
registers enabled entries for supported agents at startup. The separate
`mcpgateway` CLI manages a Docker MCP gateway when you want one. See
[mcp/README.md](mcp/README.md) for that workflow.

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

When the Docker proxy is running, show its recent logs:

```sh
djinn audit
djinn audit --tail 200
```

Update the pinned agent versions in the Dockerfile through the project script:

```sh
djinn update
```

Rebuild afterward:

```sh
djinn build
```

## Typical Workflows

Interactive shell in your configured projects directory:

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
