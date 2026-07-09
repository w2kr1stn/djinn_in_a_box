# Sync Across Machines

Djinn can be used on more than one machine, but it does not prescribe a sync
service. Use Git for this repository, and use any bidirectional file
synchronizer for local config files you choose to share.

## What To Sync

Sync these deliberately:

- The repo itself: clone or pull it with Git on `host-a` and `host-b`.
- The app config directory: `~/.config/djinn_in_a_box/`.
- Your configured credential/config root, usually `~/.djinn/config/`.
- The repo-local `config/` directory if you want the same local seed overlays,
  agent settings, and MCP registry files on both hosts.

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
above.

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

By default, `djinn backup` archives existing credential/config-root paths
(`claude`, `gemini`, `codex`, `opencode`, `gh`, `age`), `repo-dotfiles`, and data
volumes (`djinn-opencode-data`, `djinn-vscode-workspaces`). Cache volumes are
not included by default because they are rebuildable.

Stop Djinn containers before backup or restore; the command enforces this.

## What Not To Sync

Do not sync live containers, Docker volume directories, Unix sockets, PID files,
or runtime cache directories. Do not run a file synchronizer against Docker's
internal storage. Use `djinn clean`, `djinn backup`, and `djinn restore` for
Djinn-managed runtime state.
