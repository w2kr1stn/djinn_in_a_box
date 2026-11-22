# Storage and recovery

Find out what persists, how credentials are mounted, and how to back up or remove data.

[Back to the README](../README.md)

## Storage and Mounts

Bind mounts expose host paths directly. Built-ins cover credentials, seeds, workflow and caches. Configuration
selects the workspace, config root, SOPS key and OMP theme. Add declarations without editing tracked Compose files.
See [Configuration](configuration.md#configuration) for declarations and the managed writable source restriction.

| Host path | Container path | Purpose |
| --- | --- | --- |
| `${DJINN_CONFIG_ROOT}/claude` | `/home/dev/.claude` | Claude Code credentials and state |
| `${DJINN_CONFIG_ROOT}/codex` | `/home/dev/.codex` | Codex CLI credentials and state |
| `${DJINN_CONFIG_ROOT}/opencode` | `/home/dev/.opencode` | OpenCode state |
| `${DJINN_CONFIG_ROOT}/gh` | `/home/dev/.config/gh` | GitHub CLI state |
| `${DJINN_CONFIG_ROOT}/age` | `/home/dev/.config/age` | age encryption identities (`keys.txt`) |
| `${CODE_DIR}` | `/home/dev/projects` (`projects`) or `/home/dev/aimi` (`aimi`) | One configured workspace root; an aimi root's `projects/` appears at `/home/dev/aimi/projects` |
| `${HOME}/.djinn/sessions` | `/home/dev/sessions` | Session workspaces |
| Djinn-generated public SSH directory (starts only) | `/home/dev/.ssh:ro` | Declared Git aliases, public keys and host/signing trust |
| Djinn Git agent export directory (starts only) | `/run/djinn-git-agent:ro` | Filtered SSH agent socket (`auth.sock`) |
| `~/.gitconfig` | `/home/dev/.gitconfig:ro` | Read-only Git config |
| `./config/claude` | `/home/dev/.claude_seed` | Local Claude seed and settings sync source |
| `./config/claude/AGENTS.md` | `/home/dev/.claude/AGENTS.md` | Direct Compose-Claude instruction mount |
| `./templates/claude/CLAUDE.md` | `/home/dev/.claude/CLAUDE.md:ro` | Read-only bridge importing `AGENTS.md` |
| `./config/opencode` | `/home/dev/.opencode/seed:ro` | Read-only OpenCode workflow view for the publisher |
| `./config` | `/home/dev/.djinn-canonical:ro` | Read-only canonical workflow source for the publisher |
| `./config/mcp-servers.json` | `/home/dev/.config/mcp-servers.json:ro` | Local MCP registry |

Writes through `~/.opencode/seed` and `~/.djinn-canonical` fail with `EROFS`. Only `config/claude` workflow binds stay
writable, as aliases of generated files when a different source is selected. With OpenCode as the source, there is no
writable source alias, and delivery can report `source-changed` due to a seed verification problem. The read-only views
protect only those two paths. Creation refuses writable aliases through the installation, including a workspace
root that contains it. See [Configuration](configuration.md#configuration).

The default local-zone overlay sets `CODEX_SQLITE_HOME=/home/dev/.codex/sqlite` and stays outside the config-root
backup. An explicit `sqlite_home` in Codex's `config.toml` takes precedence. Find shared rollout IDs in filenames
under `~/.codex/sessions/YYYY/MM/DD/` and continue with `codex resume <id>` or `codex exec resume <id>`.
See [Codex sessions and local state](sync-across-machines.md#codex-sessions-and-local-state) for SQLite files,
`--last`, picker and naming limits, and the split between shared and local state.

### Git identities and SSH signing

Git SSH delivery is limited to foreground and detached `djinn start`. Runs get no `SSH_AUTH_SOCK`, generated `~/.ssh`
or SSH signing. Commits requiring SSH signing fail. HTTPS works with config-root `gh` credentials.

Declare each Git SSH alias and both key paths with `djinn config edit`.

```toml
[git]
signing_identity = "git-work"                 # optional explicit default
allowed_signers_file = "~/.ssh/allowed_signers" # optional public signing trust

[git.identities.git-work]
hostname = "git.example.com"
user = "git"
key_file = "~/.ssh/work_git"
public_key_file = "~/.ssh/work_git.pub"
```

Verify each host key on the host and add it to `~/.ssh/known_hosts`. Djinn selects entries for declared hosts,
including hashed entries and markers, and refuses missing trust or a key mismatch. Host SSH config and private
keys stay outside built-in mounts. Generated read-only `~/.ssh` contains `config`, `known_hosts`,
`tailnet_known_hosts` (empty until trust is delivered), `git.json`, public keys under their original filenames,
optional `allowed_signers` and `tailnet_config` for declared hostctl hosts. Alias URLs such as `git@git-work:group/repo.git`
select the declared account. Hardware keys and certificates are unsupported.

A dedicated host agent loads only declared keys in one `ssh-add` call. It tries the last passphrase on subsequent
keys, so adjacent keys can share one prompt. Encrypted keys require host terminal stdin and refuse startup without it.
Unencrypted keys also load with redirected stdin. Linux host UID 1000 must match dev. The owner-only runtime directory
is `.local/state/djinn/runtime/git-agent` under the home recorded for that UID, independently of `HOME` or
`XDG_RUNTIME_DIR`. Both starts deliver `SSH_AUTH_SOCK=/run/djinn-git-agent/auth.sock`. Detached starts keep the agent
until dev stops or is removed. Concurrent dev creation is refused. Run `djinn clean` on the host before replacing dev.

Djinn writes `gpg.format=ssh`, the selected public signing key and optional `gpg.ssh.allowedSignersFile` to
`~/.gitconfig_local`. Add the include to the host's read-only Git config and adapt the path for your workspace mode.

```gitconfig
[includeIf "gitdir:/home/dev/projects/"]
    path = /home/dev/.gitconfig_local
```

A declared public file still works with `user.signingkey = ~/.ssh/work_git.pub`. Without `git.signing_identity`,
Djinn omits `user.signingkey` and host/repository choices apply. Set `commit.gpgsign` and supply allowed-signers
trust yourself. Djinn infers no trust from email.

`djinn doctor` reports declarations, agent state, key-file references and their origins, and uninspected repository
roots. Replace private `core.sshCommand -i` selectors, removed `-F` configs, SSH `IdentityFile`/`Include` paths and
old allowed-signers paths with generated config/public selectors. Djinn rewrites neither repositories nor host Git
config. See [Git keys and browser identity](../SECURITY-MODEL.md#git-keys-and-browser-identity).

### Workspace mounts

`--here` mounts the current directory at `/home/dev/workspace` for `start` and `run`, with or without extra mounts.
`djinn run` implies `--here` when you supply neither flag. Repeat `--mount SRC[:DST[:ro|rw]]` for extra directories.
Omitting `DST` uses `/home/dev/mount/<basename>`, and `:ro` makes it read-only. The working directory is
`/home/dev/workspace` with `--here` or the first mount target otherwise. Without mounts, `djinn start` passes no
`--workdir` and uses Compose's `working_dir: ${DJINN_WORKSPACE_TARGET:-/home/dev/projects}`, which resolves to
`/home/dev/projects` in projects mode or `/home/dev/aimi` in aimi mode.

Djinn refuses an invocation target strictly inside `--here` or another `--mount` to avoid creating root-owned
directories in host binds. See [declared mount targets](#named-volumes) for when declarations are skipped. User
mounts cannot equal, contain or lie below the active workspace target. The unused root can be mounted explicitly.
Djinn creates only the selected workspace bind. Both `:rw` and `:ro` refuse targets at or below managed roots.

Writable `--mount`, explicit or implicit `--here`, and [managed sources](configuration.md#configuration) are refused
if they overlap host execution inputs. Use another directory, remove the mount, or use `--mount SRC:ro`.
`djinn doctor` checks declarations but cannot check these invocation binds.

Runs refuse protected host and socket sources even for read-only mounts in resolved Compose or CLI mounts.
This includes `--here` from a directory with private Djinn state such as `$HOME`. Start from a subdirectory or
use `--mount <dir>:ro` outside private state. `general.code_dir` is bound even with explicit mounts, so move it
too if it contains protected state. See the [protected-source check](../SECURITY-MODEL.md#guest-runs).

### Named volumes

Named volumes are Docker-managed and host-local:

| Volume name | Container path | Category |
| --- | --- | --- |
| `djinn-opencode-data` | `/home/dev/.local/share/opencode` | data |
| `djinn-packages` | `/var/lib/djinn-packages` | data |
| `djinn-uv-cache` | `/home/dev/.cache/uv` | cache |
| `djinn-tools-cache` | `/home/dev/.cache/djinn-tools` | cache |
| `djinn-vscode-server` | `/home/dev/.vscode-server` | cache |
| `djinn-vscode-workspaces` | `/home/dev/workspaces` | data |

The backup command includes credentials, repo-dotfiles, and data by default. See [Backup and Restore](#backup-and-restore).

Declared targets must be absolute. Djinn refuses nested declarations and any target equal to or containing a
built-in, reserved, assigned zone, active workspace or `--mount`/`--here` target. Reservations span all Docker modes.
For `run`, interactive `start` and `start --detach`, validated declarations strictly below an invocation target are
skipped to avoid Docker creating root-owned directories in the outer host bind. Stderr shows `Skipped` with
`mounts.<name> -> <target> (inside <enclosing target>)`. With `--mount <dir>` alone, declarations under
`/home/dev/workspace` stay unless they are under that mount's chosen or derived target.

Declared and `--mount` targets cannot nest under host binds `/home/dev/.claude`, `/home/dev/.codex`,
`/home/dev/.opencode`, `/home/dev/.config/gh`, `/home/dev/.config/age`, `/home/dev/.claude_seed`,
`/home/dev/.djinn-canonical`, `/home/dev/sessions` or the active workspace target. Children of named volumes,
`/home/dev/mount`, the inactive root and other reserved targets are allowed except at or below managed roots.
These include `/home/dev/.cache/uv`, `/home/dev/.cache/djinn-tools`, `/home/dev/.local/share/fnm`,
`/home/dev/.vscode-server` and `/home/dev/workspaces` (with recursive ownership repair), and root-owned
`/var/lib/djinn-packages` (without dev repair). The managed-root rule also protects `/home/dev/.ssh`,
`/run/djinn-git-agent`, `/home/dev/.gitconfig_local`, `/run/djinn/dbus`, `/run/djinn/audio`,
`/run/djinn/agent-docker` and their image aliases.

Declarations support directory binds and named volumes. File binds are refused. Binds default to read-write.
`read_only = true` applies to dev and the agent daemon. Invocation `--mount ...:ro` also supports read-only directories.

Startup repairs ownership only on an empty declared-volume root unwritable by dev, leaving contents alone. A
populated, unwritable root warns and needs manual repair. Helper errors stop startup. Use `djinn build` if
upgrading an image without the helper. `djinn status` and `djinn clean volumes` list all categories, including
`none`, and report absent volumes as `not created`.

| Declared volume category | Backup and restore | Cleanup |
| --- | --- | --- |
| `data` | Default backup, or explicit `--categories data`; restore while currently declared as data/cache | `clean volumes --data`, `clean all`, or by actual name |
| `cache` | Only with `--categories cache`; restore while currently declared as data/cache | `clean volumes --cache`, `clean all`, or by actual name |
| `none` | Never backed up or restored; no backup selector | Only `clean all` or by actual name; no `--none` selector |

Backup includes only existing selected volumes. Declarations use `declared-volumes/<actual-name>.tar.gz`, separate
from built-in archives. Restore warns and skips removed or `none` declarations, and accepts changes between `data`
and `cache`. Root `djinn-sync-*` archives restore credentials/config-root paths even with a same-named declared volume.

## Credentials and encryption keys

Each CLI gets its own [config-root bind](#storage-and-mounts). The same applies to persistent age identities
at `~/.config/age` (`age -i ~/.config/age/keys.txt`). For SOPS, configure a host-local key file.

```sh
djinn config set general.sops_age_key_file /path/outside/the/config/root/keys.txt
```

Djinn sets `SOPS_AGE_KEY_FILE` and mounts the key read-only at SOPS's default `~/.config/sops/age/keys.txt`.
It must be a regular readable file with mode `0600` or `0400`. Otherwise `djinn start` and `djinn run` refuse and
`djinn doctor` names the problem. Keep the key out of a mirrored config root and its replicas' backups. Agents
can read mounted keys, so deny access in their settings, for example `"Read(~/.config/sops/age/**)"` in
Claude Code's `permissions.deny`.

## Runtime Packages

As `dev`, run `apt-get update && apt-get install -y <package>` without `sudo`. The shim accepts `-y`, `--yes`,
`--assume-yes`, `-q`, `--quiet`, `-qq`, `--no-install-recommends`, `-h` and `--help` anywhere. `apt-get --help`
lists accepted syntax. Names must match `[a-z0-9][a-z0-9+.-]*`, with at least two characters and at most 128 bytes.
Installs always use `-y` and `--no-install-recommends`.

Other subcommands, unsupported flags, paths, `.deb` files and malformed names exit 2. A name without an exact apt
cache match exits 100 with `E: Unable to locate package <name>` and `Run apt-get update and try again.` Refusals point to
`apt-get --help` and the README section "Runtime Packages", which links here. Each elevation reports on stderr.
Direct calls print `apt-get: using sudo to run /usr/local/libexec/djinn-apt-root as root.` Startup replay prints
`Packages: using sudo to run /usr/local/libexec/djinn-apt-root replay as root.` For other commands, use unprivileged
`/usr/bin/apt-get`. Root invocations such as `sudo apt-get` pass arguments and streams unchanged to that binary
and skip recording.

The root helper records successful installs made as `dev` in `djinn-packages` (category `data`), one name per line
in `/var/lib/djinn-packages/packages.txt`. It writes atomically, deduplicates under the lock, and ignores blanks and
`#` comments. The file is root:root 0644 in a root:root 0755 directory. Agents cannot change it. Read it with `cat`.
A human can remove an entry with `sudo`.

```sh
sudo sed -i '/^NAME$/d' /var/lib/djinn-packages/packages.txt
```

Removing a line stops replay, leaving the package installed until recreation. Recording preserves existing lines,
refuses additions beyond 256 distinct names or 1 MiB, and warns on failure without changing a successful install status.

At startup the dev wrapper calls the root helper's `replay` mode after MCP and before Tools. With all packages
installed it makes no network calls. Otherwise it updates apt, warns and skips names absent from the cache, and
installs remaining missing packages. Replay warnings leave startup running. `djinn run --allow` skips replay
because it has no DNS. See [allowlist runs](access-and-boundaries.md#per-run-allowlist-djinn-run---allow).

The helper checks the opened descriptor and requires a regular root-owned file of at most 1 MiB in a root-owned
directory. Symlinks or files/directories writable by group or others cause refusal with a warning. Replay parses
once, considers at most 256 distinct names, warns with the number skipped, rejects entries over 128 bytes and
names at most five invalid lines. Each check phase before and after update warns and stops after 60 seconds. The
package list update is limited to 300 seconds. Install has no limit because interrupting dpkg can leave partial state.
Use build-time `packages.txt` for packages every image needs. See the
[runtime package install security model](../SECURITY-MODEL.md#runtime-package-installs).

## Backup and Restore

Stop Djinn before backup or restore.

```sh
djinn clean
djinn backup
```

Default backup categories are `credentials` (config-root `claude`, `codex`, `opencode`, `gh` and `age`),
`repo-dotfiles` (when present), and `data` (`djinn-opencode-data`, `djinn-vscode-workspaces` and `djinn-packages`).
Request `cache` explicitly for cache volumes. Restore includes the package list when archived. See
[Codex's local zone](sync-across-machines.md#codex-sessions-and-local-state)
for excluded SQLite state and what shared rollouts cannot restore. Djinn excludes post-mortem records. Keep
`~/.local/state/djinn` out of your own backups and sync too.

`age` prompts twice at the terminal. Djinn writes `~/.djinn/backups/djinn-backup-YYYY-MM-DD.tar.gz.age` and keeps
only the newest `djinn-backup-*.tar.gz` or `djinn-backup-*.tar.gz.age`. Restore accepts cleartext archives, and a
successful new backup removes old ones.

Restore the newest backup.

```sh
djinn restore
```

Restore asks for confirmation and overwrites restored volumes and config-root contents. For encrypted backups,
`age` prompts on the terminal before any restore. After interruption, lifecycle commands refuse while a restore
writer runs and name the blocking process.

Before overwriting, `djinn restore` parks the union of current OpenCode volume and config-root logins in a private
recovery session. Export or parking failure stops restore before changes. `--discard-unexported-credentials`
proceeds and loses logins present only in the volume. Audit and start leave recovery alone while restore writes.
Restore then merges all recovery sessions. On merge failure it warns and leaves sessions for the next audit or
start. An old `credentials.removed.json` can revive a login logged out everywhere after that backup.

Select categories with `djinn backup --categories credentials --categories data` or explicitly request a cleartext
archive with `djinn backup --no-encrypt`. See [cross-machine sync](sync-across-machines.md) and the
[backup security model](../SECURITY-MODEL.md#credentials-backups-and-outbound-traffic).

## Cleanup and Uninstall

`djinn clean` removes the Compose project's containers (including active runs) and run network `djinn-in-a-box_default`.
It keeps persistent volumes, config and `djinn-network`. Run removal verifies name and labels, stops with a 3 s grace
period and then force-removes by ID. Project teardown can also remove foreign containers carrying project labels.

Before removing a dev, companion or helper that stopped outside Djinn, `djinn clean` and `djinn start` attempt
to write a post-mortem under `~/.local/state/djinn/post-mortem/` (`0700` directory, `0600` files, last 10 records
within 8 MiB). They print only its path and continue on recording failure. See the
[confidentiality and evidence limits](../SECURITY-MODEL.md#window-state-expiry-and-logs).
Removing a large stale container waits up to 60 s. A timeout names the object, notes that Docker may finish in the background,
and says retrying is safe. Any recorded post-mortem remains available.

`djinn clean` keeps declared and built-in volumes including `djinn-packages`. Category cleanup includes
[declared volumes](#named-volumes). `clean all` includes volumes with `backup = "none"`. Declared bind sources and
markers, arbitrary host binds, workspace, SSH and shared/local zone data are outside all cleanup sets.

`djinn clean volumes` lists managed volumes and config-root paths. It serializes with restore, backup, clean all
and dev start/stop. A concurrent operation fails after at most 15 s with "host control is busy".

| Delete | Command |
| --- | --- |
| Cache volumes | `djinn clean volumes --cache` |
| Data volumes, including the root-owned `djinn-packages` list | `djinn clean volumes --data` |
| Optional config-root `repo-dotfiles` directory | `djinn clean volumes --repo-dotfiles` |
| A specific managed named volume | `djinn clean volumes djinn-uv-cache` |

`djinn clean volumes NAME` refuses names without a `djinn-` prefix. Data cleanup and `djinn clean volumes djinn-opencode-data`
export logins via a one-off container and merge them into the config root before volume deletion. Failure keeps
the volume and names the reason. `--discard-unexported-credentials` deletes it and loses logins stored only there.
`djinn clean all` attempts the same export and only warns on failure. See [restore recovery](#backup-and-restore).

`djinn clean volumes --credentials` logs out all OpenCode logins everywhere, then clears config-root credential
directories. It keeps `credentials.removed.json`, whose IDs each host applies at its next sync. This command and
`clean all` refuse while audit runs. `djinn clean volumes --credentials --opencode-id <id>` logs out one login
everywhere, including during an audit.

`djinn clean all` removes containers, managed volumes including `djinn-packages`, config-root sync-path contents
and the Docker network. Zone data in `<config-root>.shared` and `<config-root>.local` stays. Use `--force` to skip
destructive cleanup prompts, for example `djinn clean all --force`.

For an installation made with `uv tool install`, `uv tool uninstall djinn-in-a-box` removes only the Python CLI.
Images, volumes, config file and root, backups and sessions stay. Delete `~/.local/state/djinn/post-mortem/` by hand
to discard retained records.
