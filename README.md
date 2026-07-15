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

- Open an interactive shell with `djinn start`.
- Run a one-shot agent prompt with `djinn run`.
- Attach another shell to a running container with `djinn enter`.
- Keep reusable session workspaces under `~/.djinn/sessions/` with
  `djinn session`.
- Run OAuth-style authentication flows with `djinn auth`.
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

Use `djinn auth` when an agent needs a browser or loopback OAuth flow:

```sh
djinn auth
```

This starts the auth service with host networking so local callback flows can
complete. Exit the shell after signing in, then use `djinn start` or `djinn run`
for normal work.

You can authenticate tools one by one inside that shell, for example by running
the agent binary directly. The resulting credentials persist in your configured
config root, not in the container image.

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

Memory values must use Docker-style units such as `8G`, `4096M`, or `512K`.
CPU values are positive integers. Reservations cannot exceed limits.

`DJINN_CONFIG_ROOT` is not something you normally have to export. The CLI loads
`config.toml` and injects Compose interpolation variables, including
`DJINN_CONFIG_ROOT`, `CODE_DIR`, `TZ`, and the resource settings, into the
`docker compose` subprocess environment. If you do export `DJINN_CONFIG_ROOT`,
that environment value takes precedence for config-root resolution.

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

On `djinn init`, and during command preflight, Djinn copies missing seed targets
from `templates/seed/` into local paths:

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

Bring your own Claude workflow. The seeded `CLAUDE.md`, `settings.json`,
`mcp-servers.json`, and empty local directories are deliberately minimal. Replace
them with your own instructions, settings, MCP entries, commands, skills, hooks,
or subagents as needed.

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
| `djinn start` | Runs the `dev` service interactively with `docker compose run --rm`; removed after exit | Starts in `/home/dev/projects`; add `--here` or `--mount PATH` to mount `/home/dev/workspace` and work there | Daily interactive shell |
| `djinn auth` | Runs the `dev-auth` service with the Compose `auth` profile and host networking | Starts in `/home/dev/projects` | OAuth and browser callback setup |
| `djinn enter` | Uses `docker exec -it <running-container> zsh` | Enters an already running Djinn container | Open a second shell while `djinn start` is still running |
| `djinn run AGENT PROMPT` | Runs the `dev` service headlessly with `docker compose run --rm -T`; removed after exit | Mounts the current directory as `/home/dev/workspace` by default; `--mount PATH` overrides it | One-shot agent prompts |
| `djinn session` | Uses `docker exec` into a running `djinn` container when available; otherwise host fallback preflight only checks whether `claude` is on `PATH`. A different requested host agent can still fail later if its binary is missing. | Uses `~/.djinn/sessions/<project>` on the host and `/home/dev/sessions/<project>` in the container; `--create` creates the host workspace | Reusable session workspaces |

Common `start` options:

| Option | Effect |
| --- | --- |
| `--docker`, `-d` | Adds `docker-compose.docker.yml`, starts the Docker socket proxy, and sets `DOCKER_HOST=tcp://docker-proxy:2375` inside the dev container |
| `--docker-direct` | Adds `docker-compose.docker-direct.yml` and mounts `/var/run/docker.sock` directly into the dev container |
| `--firewall`, `-f` | Sets `ENABLE_FIREWALL=true`; the entrypoint initializes the network firewall |
| `--here` | Mounts the current directory as `/home/dev/workspace` and uses it as the working directory |
| `--mount PATH`, `-m PATH` | Mounts `PATH` as `/home/dev/workspace` and uses it as the working directory |

`--docker` and `--docker-direct` are mutually exclusive.

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
| `./config/gemini` | `/home/dev/.gemini_seed` | Local Gemini seed source |
| `./config/opencode` | `/home/dev/.opencode/seed` | Local OpenCode seed source |
| `./config/mcp-servers.json` | `/home/dev/.config/mcp-servers.json:ro` | Local MCP registry |

When `--here`, `--mount`, or `djinn run` is used, an additional host directory is
mounted at `/home/dev/workspace`.

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
  directories with mode `0700`. Directories that already exist are not tightened
  retroactively.
- **Readable by every agent in the container.** Each credential is mounted where
  its tool expects it, so the `dev` user — and therefore every coding agent you
  run — can read all of them. An `age` identity is a master decryption key. An
  agent running in write mode (`--dangerously-skip-permissions`, `--full-auto`)
  acts without approval, so a prompt injection reaching that agent can read the
  key. Run `djinn start --firewall` when an agent processes untrusted content, so
  a leaked secret cannot be sent outbound.
- **Backups are unencrypted.** `djinn backup` writes a plain `tar.gz` under
  `~/.djinn/backups/` that contains the credential directories. Protect that
  directory as carefully as the credentials themselves.

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
~/.djinn/backups/djinn-backup-YYYY-MM-DD.tar.gz
```

Only the newest `djinn-backup-*.tar.gz` archive is kept in that directory.

Restore the newest backup:

```sh
djinn restore
```

Restore asks for confirmation and overwrites the restored volume and config-root
directory contents.

To back up specific categories:

```sh
djinn backup --categories credentials --categories data
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
