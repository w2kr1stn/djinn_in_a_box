# Sync Across Machines

Djinn can be used on more than one machine, but it does not prescribe a sync
service. Use Git for this repository, and use any bidirectional file
synchronizer for local config files you choose to share.
See the [security model](../SECURITY-MODEL.md#credentials-backups-and-outbound-traffic)
for credential, transcript and backup implications.

## What To Sync

Sync these deliberately:

- The repo itself: clone or pull it with Git on `host-a` and `host-b`.
- The app config directory: `~/.config/djinn_in_a_box/`.
- Your configured credential/config root, usually `~/.djinn/config/`. This is
  the credential/config zone that Djinn backs up.
- Its shared sibling, usually `~/.djinn/config.shared/`, when you want
  transcripts on both machines. Djinn does not back this zone up.
- The repo-local `config/` directory if you want the same local seed overlays,
  agent settings, and MCP registry files on both hosts.

Do not mirror the local sibling, usually `~/.djinn/config.local/`. It holds
runtime caches, scratch and Codex SQLite stores, including durable operational
state, and is intentionally host-local. If you use explicit `shared_root` or
`local_root` values, apply the same policy to those
paths instead of the derived siblings.

`~/.config/djinn_in_a_box/config.toml` may contain host-specific paths. If
`code_dir` or the workspace mode differs between machines, keep separate copies
or set both explicitly after sync. For a standalone projects directory:

```bash
djinn config set general.workspace projects
djinn config set general.code_dir /path/to/projects
```

For an AIOS root (its `projects/` appears at `/home/dev/aios/projects`):

```bash
djinn config set general.workspace aios
djinn config set general.code_dir /path/to/aios
```

Both modes use one workspace bind and need no Compose edit. Djinn does not
autodetect the mode or move files when the setting changes.

If you want a non-default credential/config root, set it per host:

```bash
djinn config set general.config_root /path/to/djinn-config
```

### Codex sessions and local state

Djinn sets `CODEX_SQLITE_HOME=/home/dev/.codex/sqlite`; `sqlite` is a default
local-zone directory. All Codex SQLite stores (`state_*`, `logs_*`, `goals_*`,
`memories_*`, `queue_*`, `thread_history_*`), their `-wal`/`-shm` siblings and
`.sqlite-maintenance.lock` stay per host. An explicit `sqlite_home` in Codex's
`config.toml` takes precedence; keep that path host-local too.

Sessions under `~/.codex/sessions/YYYY/MM/DD/` remain shared. After the rollout
files arrive on the other machine, continue by session ID:

```sh
codex resume <id>
codex exec resume <id>
```

The ID is in the rollout filename. Fresh SQLite indexes backfill shared
rollouts, but once a local index has usable rows, the resume picker can omit
sessions created on the other machine after that first index, and `--last` can
select an older session. Session names also use the local index. Direct ID
resume works in this case; a rollout pathname is not a CLI session-ID argument.
For exec-created sessions, the TUI needs `--include-non-interactive` for listing.

Goals and budgets, queued unsent messages, pins/projects/read state,
remote-control enrollment and diagnostic SQLite logs remain per machine.
Memory extraction/consolidation state is local, while generated text in
`memories/` stays shared. Another machine's consolidation can regenerate or
prune shared summaries from its own local state.

Do not file-sync these live databases. SQLite can leave committed data in its
WAL even after Codex exits or the daemon stops; a synchronizer can deliver mixed
or incomplete main/WAL files that silently show stale data, even with a passing
integrity check. The local zone is excluded from the config-root backup, so
preserve its durable state separately if needed. Shared rollouts do not restore
goals, pending inputs or all preferences.

## First Host

On `host-a`:

```bash
git clone <repo-url> djinn-in-a-box
cd djinn-in-a-box
djinn init
djinn build
```

Then configure your file synchronizer to mirror the config paths and shared root
listed above. Exclude the local root. `djinn init` creates the assigned overlay
directories before the first start.

## Second Host

On `host-b`:

```bash
git clone <repo-url> djinn-in-a-box
cd djinn-in-a-box
djinn init
djinn doctor --fix
djinn build
```

Run `djinn init` on each machine so host-specific paths and resource suggestions
are valid. After your synchronized files arrive, run `djinn doctor --fix` to
provision missing directories and reseed neutral defaults without overwriting
existing local config.

## Credential and Data Backups

For Docker-managed state, use Djinn backup/restore instead of file syncing live
Docker internals:

```bash
djinn backup
djinn restore
```

By default, `djinn backup` archives the config-zone credential/config-root paths
(`claude`, `codex`, `opencode`, `gh`, `age`), `repo-dotfiles`, and data
volumes (`djinn-opencode-data`, `djinn-vscode-workspaces`, `djinn-packages`). It
does not archive the shared transcript zone or the local zone, including Codex
SQLite and its durable operational state.
Cache volumes are not included by default because they are rebuildable.

OpenCode logins travel with the config root: `credentials.json` and
`credentials.removed.json` are part of that credential/config zone, while the
`djinn-opencode-data` volume stays local to each host. Syncthing conflict copies
of both files are merged and then removed from the config root into a private
backup inside the dev container. A logout stays on the host where you made it;
log out everywhere with `djinn clean volumes --credentials --opencode-id <id>`.

The `djinn-packages` archive stores the root-owned runtime package list at
`/var/lib/djinn-packages/packages.txt`, which is the
install intent rather than installed package files. Restoring the volume restores
that intent; the next container start checks the list and installs packages that
are missing.

Backups are age-encrypted with a passphrase by default and use the filename
`djinn-backup-YYYY-MM-DD.tar.gz.age`. `age` asks for that passphrase directly at
the terminal during backup and restore. Existing cleartext `.tar.gz` backups
remain restorable; `djinn backup --no-encrypt` creates that format.

Stop Djinn containers before backup or restore; the command enforces this.

## What Not To Sync

Do not sync live containers, Docker volume directories, Unix sockets, PID files,
or the local zone, including Codex SQLite. Codex daemon settings, control sockets
and managed packages also stay local; Djinn applies its daemon policy at each
dev-container start. Do not run a file synchronizer against Docker's internal
storage. Exclude `~/.local/state/djinn`: it contains
host-private post-mortem records and runtime state. Use `djinn clean`,
`djinn backup`, and `djinn restore` for Djinn-managed runtime state.

## Hostctl node identity

Exclude `djinn-hostctl-state` and the hostctl journal/supervisor directories
from synchronization. Build and enroll separately on each Docker host using
[host control setup](../README.md#host-control-windows). Identity persistence,
cleanup and the prohibition on node-state copies/backups are defined in
[enrollment and tailnet policy](../SECURITY-MODEL.md#enrollment-and-tailnet-policy).
