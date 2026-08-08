# Sync Across Machines

Djinn can be used on more than one machine, but it does not prescribe a sync
service. Use Git for this repository, and use any bidirectional file
synchronizer for local config files you choose to share.

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
rebuildable caches and scratch and is intentionally host-local. If you use
explicit `shared_root` or `local_root` values, apply the same policy to those
paths instead of the derived siblings.

`~/.config/djinn_in_a_box/config.toml` may contain host-specific paths. If
`code_dir` differs between machines, keep separate copies or edit it after sync
with:

```bash
djinn config set general.code_dir /path/to/projects
```

If you want a non-default credential/config root, set it per host:

```bash
djinn config set general.config_root /path/to/djinn-config
```

## First Host

On `host-a`:

```bash
git clone <repo-url> djinn-in-a-box
cd djinn-in-a-box
djinn init
djinn build
```

Then configure your file synchronizer to mirror the local config paths listed
above. Before the first `djinn migrate-zones`, add the shared root to that set
or pause synchronization. The migration moves data out of the config root, so a
synchronizer otherwise sees source deletions and can propagate them before the
shared copy is protected.

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
(`claude`, `gemini`, `codex`, `opencode`, `gh`, `age`), `repo-dotfiles`, and data
volumes (`djinn-opencode-data`, `djinn-vscode-workspaces`). It does not archive
the shared transcript zone or the local cache/scratch zone. Cache volumes are not
included by default because they are rebuildable.

Backups are age-encrypted with a passphrase by default and use the filename
`djinn-backup-YYYY-MM-DD.tar.gz.age`. `age` asks for that passphrase directly at
the terminal during backup and restore; Djinn does not store it. Move the
archive by your chosen secure transport and retain the passphrase separately: a
forgotten passphrase cannot be recovered. Existing cleartext `.tar.gz` backups
remain restorable. `djinn backup --no-encrypt` is an explicit cleartext opt-out
for controlled use only.

Stop Djinn containers before backup or restore; the command enforces this.

## What Not To Sync

Do not sync live containers, Docker volume directories, Unix sockets, PID files,
or the local zone's runtime cache directories. Do not run a file synchronizer
against Docker's internal storage. Use `djinn clean`, `djinn backup`, and
`djinn restore` for Djinn-managed runtime state.
