# Local configuration

Set up your workspace, mounts, resource limits, and local seed files.

[Back to the README](../README.md)

## Configuration

Use the CLI to manage `~/.config/djinn_in_a_box/config.toml`.

```sh
djinn config show
djinn config show --json
djinn config path
djinn config set resources.memory_limit 12G
djinn config set config_sync.source codex
djinn config status
djinn config edit
```

`djinn config edit` opens `$EDITOR` or `vi` and validates on exit. Edit additional mounts and environment
variables only with this command, in root TOML tables.

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

[environment]
CDP_HOST = "192.0.2.1"
CDP_PORT = "9222"
EXAMPLE_LITERAL = "${HOST_VALUE}"
```

Declarations can only add mounts to the built-ins. Names must match `[a-z0-9][a-z0-9_.-]*` and be unique across
both mount kinds. Quote dotted names, for example `[mounts."archive.disk"]`. Compose creates `djinn-<name>` volumes.
Djinn refuses collisions with built-in names. Volumes require `volume = true`, `target` and `backup` (see
[volume categories](storage-and-recovery.md#named-volumes)). Binds require `source` and `target`, with optional
`marker` and `read_only` (strict boolean, default `false`). Djinn refuses extra fields, `read_only` on volumes and mixed shapes.

A bind source must resolve to an existing absolute host directory without `:`. The optional marker is a regular
file directly inside it. Symlink markers are refused. Missing sources or missing or invalid markers refuse creation
and name the declaration. `djinn doctor` gives each mount or environment key a PASS/FAIL row, with
`valid declaration (read-only)` for valid read-only binds. Marker validation checks neither contents nor identity.
A drive can disappear after validation. Declarations create no host paths, and built-in provisioning still runs
before validation even on overlaps. Use host backups for bind data, which Djinn excludes from backup, restore and cleanup.

At creation, Djinn refuses writable `[mounts.*]` sources that overlap a host execution input, including symlinks
along input paths. The check covers the invoking CLI's `PATH`, Python environment and Docker client configuration.
A relative `PATH` entry, unreadable or malformed Docker config, or an uninspectable input makes the inventory
indeterminate and refuses affected writable binds. `djinn doctor` marks them FAIL. Set `read_only = true` or remove
the bind. Read-only binds and declared volumes are outside this check. See the
[sealing inventory](../SECURITY-MODEL.md#sealed-deployments-and-trusted-controller).

`djinn start` and `djinn run` also check managed writable binds after config load and before host provisioning in
every Docker mode. These include the workspace (also delivered to the companion), config-root tool directories
(`claude`, `codex`, `opencode`, `gh`, `age`), assigned shared/local zone overlays and `~/.djinn/sessions`. Overlap,
indeterminate input or source, or a managed source path containing `..` or `:` is refused. Move the offending
installation, Python environment, `PATH` directory or Docker plugins/config, or change `general.code_dir`,
`general.config_root` (or `DJINN_CONFIG_ROOT`), `general.shared_root` or `general.local_root`. Move inputs out of
the sessions directory if affected. The check pins no inode. Keep sources and ancestors stable and outside agent
rename authority, including through writable parent binds, until Docker mounts them.

Declared strings cannot contain NUL. Environment keys must match `[A-Za-z_][A-Za-z0-9_]*` and values must be
strings. `${HOST_VALUE}` stays literal regardless of host variables. Reservations cover all Docker modes and
repo-provided image/startup keys from Compose, Python, scripts, tools, Dockerfile exports and generated shell config.
Mounted host startup files and third-party tools are outside that boundary. Declared environment affects dev only.
Declared mounts also reach the agent daemon as workspace mounts. Builds and host execution are outside both declarations. `CODEX_HOME` and
`CODEX_SQLITE_HOME` are reserved. See [Codex storage](sync-across-machines.md#codex-sessions-and-local-state).

`djinn config show` displays `read_only=true` for read-only binds in text and explicit booleans for every bind in JSON.
`djinn config set` preserves effective modes while omitting false `read_only` and dropping TOML comments.
`djinn init` skips declaration questions. `djinn init --force` replaces the entire file. Changes apply on creation via
`start` (foreground or detached) or `run`. `session` and `enter` inherit the running mounts without comparison or update.
Edit literal endpoints such as `CDP_HOST` after a gateway change. Migrate Compose edits and volume data manually.

Use these keys with `djinn config set`.

| Key | Meaning | Default or source |
| --- | --- | --- |
| `general.code_dir` | Host workspace root mounted at the path selected by `general.workspace` | chosen during `djinn init` |
| `general.workspace` | `projects` mounts at `/home/dev/projects`; `aimi` mounts at `/home/dev/aimi` | `projects` |
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

`djinn init` asks for the mode first, then the host directory. Use lowercase mode values.

```sh
# Standalone projects directory
djinn config set general.workspace projects
djinn config set general.code_dir /path/to/projects

# aimi workspace root
djinn config set general.workspace aimi
djinn config set general.code_dir /path/to/aimi
```

Memory uses Docker-style units such as `8G`, `4096M`, or `512K`. CPU values are positive integers. Reservations
cannot exceed limits.

Keep `build.network` at `default` on ordinary Docker hosts. Use `host` when a VPN or split-DNS resolver is reachable
only through the host's network. This setting applies only to build steps. See the
[host build-network implications](../SECURITY-MODEL.md#host-authority-and-docker-access).
The image build stops early with this hint when the build network cannot resolve names
(`scripts/check-build-dns.sh`), instead of letting each download time out.

Buildx 0.37.2 and later require `network.host` for host-network builds. `djinn build` grants it only for
`build.network host`. Plain `docker compose build` cannot grant it and stops before build steps with
`additional privileges requested`. BuildKit accepts `none`, which Djinn refuses because the image
needs network access. BuildKit itself rejects named Docker networks.

The CLI passes `DJINN_CONFIG_ROOT`, `CODE_DIR`, `DJINN_WORKSPACE_TARGET`, `TZ` and resource settings from config
to the `docker compose` subprocess. Export `DJINN_CONFIG_ROOT` to override config-root resolution. The configured
mode sets the mount target and default working directory through `DJINN_WORKSPACE_TARGET`. Configured values
override inherited `CODE_DIR` and `DJINN_WORKSPACE_TARGET`, so change modes without editing Compose.

## The Blank-Space and Seed Model

Only `djinn init` and `djinn doctor --fix` copy missing targets from `templates/seed/`. The repository's `config/`
is root-anchored in `.gitignore`, and existing targets of the expected type are kept.

| Seed source | Local target | Purpose |
| --- | --- | --- |
| `templates/seed/config/claude/AGENTS.md` | `config/claude/AGENTS.md` | neutral global instructions with the per-directory `.agents/` convention |
| `templates/seed/config/claude/settings.json` | `config/claude/settings.json` | minimal Claude Code settings; loads `AGENTS.md` beside `CLAUDE.md` |
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

Bring your own files in the [native workflow root](agent-workflow.md#global-agent-workflow-ownership) selected by
`config_sync.source`. See [MCP](agent-workflow.md#mcp) for the separate server registry and [Codex](agent-workflow.md#codex)
for the updater policy applied at each start.

List optional Debian packages one per line in `packages.txt`, read at image build time. `tools/tools.txt` is read at
container start and names installers from `tools/installers/`.

## Resources

During `djinn init`, Djinn suggests resource values from the host.

- CPU limit: half of detected CPUs, clamped between `1` and `128`
- memory limit: half of detected memory, at least `2G`
- CPU reservation: one quarter of the suggested CPU limit, at least `1`
- memory reservation: one quarter of the suggested memory limit, at least `1G`

If detection fails, Djinn uses the model defaults listed under [Configuration](#configuration).

```sh
djinn config set resources.cpu_limit 6
djinn config set resources.memory_limit 12G
djinn config set resources.cpu_reservation 2
djinn config set resources.memory_reservation 4G
```

Docker Compose receives `CPU_LIMIT`, `MEMORY_LIMIT`, `CPU_RESERVATION`, and `MEMORY_RESERVATION` from the CLI.
