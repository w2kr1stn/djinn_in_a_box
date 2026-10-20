# Security Model

## Threat model

Djinn's host control window protects against unwanted use of remote machines,
including mistakes and prompt injection. It is not a hardened sandbox against
deliberate host escape. Agents run as `dev` with passwordless `sudo` inside the
container; write mode can also bypass the agent CLI's approval prompts. An
approval setting is not an isolation boundary. For package-install elevation
and its limits, see [Runtime package installs](#runtime-package-installs).

The window relies on a trusted host controller, a sealed deployment, no direct
Docker-to-tailnet forwarding, and owner-controlled target permissions. It gates
new SSH access and cuts relay connections; it cannot undo remote changes or stop
detached jobs already launched on a target. See [usage](README.md#host-control-windows)
and [implementation](IMPLEMENTATION.md#hostctl-helper).

## Runtime package installs

As `dev`, `/usr/local/bin/apt-get` is a shim that elevates `update` and supported
`install` calls through `sudo` to the root-owned
`/usr/local/libexec/djinn-apt-root`, allowed by its own
`dev ALL=(root) NOPASSWD: /usr/local/libexec/djinn-apt-root` sudoers line. Each
elevation is announced on stderr. Root callers, including `sudo apt-get`, pass
through to `/usr/bin/apt-get` unchanged.

For direct installs, only `update` and `install` of exact package names present
in the apt cache are accepted; the helper also accepts argument-free `replay` at startup. The shim allows only its documented flags; it does not pass arbitrary
apt options, configuration, source entries, paths, or `.deb` files to the
helper. The helper uses a fixed environment, non-interactive apt,
`--no-install-recommends`, and `--no-remove` (it never removes a package), with
`/dev/null` stdin and a lock that serializes package operations, including replay
and recording. The helper alone writes the root-owned list at
`/var/lib/djinn-packages/packages.txt` (root:root 0644, directory root:root 0755),
atomically after successful installs. Agents cannot edit it; human housekeeping
requires sudo. Root never touches a dev-writable path: the list lives outside
`/home/dev`, whose ancestors dev could rename or replace with symlinks.
Apt's history log attributes these installs to root because the fixed environment
drops sudo's caller variables; the list and stderr announcement record agent installs.

The helper fails closed when directory or opened-file descriptor checks fail:
the directory must be root-owned and not group- or world-writable; the list must
also be a regular root-owned file with those permission restrictions and at most
1 MiB. Symlinked files and directories are refused. The opened descriptor is
checked and read, so a path swap cannot substitute the object being validated.
Replay maps NUL bytes before one linear parse, bounds names and warnings, uses a
60-second budget for each phase of per-name checks and a 300-second update timeout.
Installs have no kill timeout because interrupting dpkg can leave packages
half-configured. The dev startup wrapper only announces elevation and formats
the helper’s tagged output; replay failures warn and let startup continue.

Exact-name validation does not enforce package origin. An agent can install as
root any package from signed apt sources configured in the container: the Debian
archive by default, and any source a root tool installer adds (for example,
PostgreSQL's) is trusted the same way. Package maintainer scripts run as root
and can change services and configuration inside the container. Services do not
start during install because the base image's `policy-rc.d` forbids it. The
existing `dev ALL=(ALL) NOPASSWD:ALL` rule is unchanged, so this adds no authority
that `dev` did not already have; it does mean a permission layer that denies
`sudo …` no longer blocks package installs. With `--firewall`, `deb.debian.org`
is reachable through its shared CDN addresses.

## Host authority and Docker access

| Exposure to dev | Authority or consequence |
| --- | --- |
| Host home or `/`, even read-only | Exposes host credentials, runtime sockets and control state. Writable delivery also lets agents alter host files. Masking a key directory does not remove these other paths. |
| Host Docker socket | Host-root-equivalent daemon control: create privileged containers, mount `/`, enter other containers, build and commit images. A read-only socket bind still allows API requests. |
| Agent Docker endpoint | Full control of the rootless inner daemon. Bind sources resolve inside the companion; delivered workspace files remain exposed according to their modes. |
| Helper identity volume, its backing directory or Docker data root | Exposes the reusable tailnet identity and helper state; writable access also defeats state integrity. Ancestor and descendant binds matter too. |
| Raw host D-Bus | Access to host session services, potentially including desktop-shell execution and secret services. |
| Raw host audio socket | Playback/capture and an upstream module-loading interface where the host server permits it. |

Docker access is off by default. [Agent mode](README.md#docker-access-modes)
uses the official pinned `docker:29-dind-rootless` image as UID/GID 1000, with
`seccomp=unconfined`, `systempaths=unconfined` and `/dev/net/tun`. It adds no
capabilities, privileged mode, host namespaces, host Docker socket or other devices.
Those measured relaxations enable rootless overlay2; a shared kernel and disabled
seccomp/system-path protection are limits, not a hardened isolation boundary.

Dev has full inner-daemon API access through a read-only Unix socket mount. Read-only
prevents unlinking/replacing the endpoint through that mount; it permits API writes.
The daemon has no TCP API listener or host port publication. Network peers can reach
published workload ports at `agent-docker:<port>` but receive no endpoint volume.

An agent can deliberately relay its inner API through a published workload port;
that exposes the inner daemon and its delivered workspace, with no host-daemon
authority.

Only the workspace delivery is shared with the companion, including sessions.
Choosing host `/` or a home directory exposes whatever it contains, even read-only.
Inner bind mounts resolve against the companion filesystem and its delivered binds;
its persistent rootless data lives in the cache volume `djinn-agent-docker`.
The companion follows dev's resource configuration. With `--firewall`, a temporary
root initializer from the inspected dev image applies the unchanged firewall rules
in its outer network namespace before the daemon starts. It adds `NET_ADMIN` and
mounts only the temporary endpoint. The daemon consumes the readiness marker;
a manual restart waits for fresh rules and fails health until Djinn initializes it.
The policy keeps its existing private-network/DNS allowances and fixed allowlist.

`--docker-direct` delivers the host socket read-write. The entrypoint adjusts socket
group access for dev; a non-root client still has host-root-equivalent authority.
Host workloads are independent of dev's firewall and resource limits. Direct mode
is unsealed. Agent mode contributes no Docker cause only after the same-generation
verification described below. Prefer no Docker access when unnecessary.

The temporary audit assistant holds host Docker authority and shares `djinn-network`.

`build.network = "host"` shares the host network namespace for image `RUN`
steps, including access to loopback-only host services. It applies to the whole
build, so use it only with trusted Dockerfiles and build inputs. It affects
build-time networking, not the resulting container's runtime network.

## Remote agent server access

`djinn serve` runs agent server modes inside the detached dev container. See
[usage and prerequisites](README.md#remote-agent-servers) and the
[process implementation](IMPLEMENTATION.md#remote-server-lifecycle).

OpenCode listens on `0.0.0.0:4096` inside the container. Detached creation
publishes it only on host `127.0.0.1:<serve.opencode_host_port>` (default
`49380`). Every local host user/process can reach that loopback endpoint.
**The container address is also reachable from the host and every peer on
`djinn-network`**: agent-docker workloads through their companion, the hostctl
helper, the audit assistant and anything else attached. Tailnet policy does not
filter these bridge paths. The API requires the OpenCode password on these
paths too. The unauthenticated web shell at `/` is a static page that reveals
only the app version; it does not establish an authenticated API session.

Publication of container port `4096` lasts for the detached container's
lifetime. Stopping the managed server leaves that mapping and any host
Tailscale Serve entry in place. A foreign listener accepting connections on
that container interface/port is therefore reachable through them as well,
with whatever authentication that listener implements. `djinn serve --status`
notes `note: port 4096 is used by another process`; it does not stop that process
or revoke publication. An absent/stale discovery record never authorizes a
signal: replacing OpenCode's default service requires command-line and socket
ownership proof, and Djinn never calls native `opencode service stop`.

OpenCode start/pair require Docker Engine **28.3.3 or newer**, an exact IPv4
loopback publication and only user bridge networks without
`com.docker.network.bridge.trusted_host_interfaces` or IPv4/IPv6 gateway modes
`routed`/`nat-unprotected`. Unknown versions or uninspectable networks refuse.
The version gate includes the fix for
[CVE-2025-54388](https://github.com/moby/moby/security/advisories/GHSA-x4rx-4gw3-53p4),
which let a firewalld reload expose published ports via container addresses
despite loopback publication.

**Host prerequisite:** check `/etc/docker/daemon.json` once before rollout.
`allow-direct-routing` must be absent or `false`; `iptables` must be absent or
`true`. These daemon-wide settings are invisible to Djinn through the Docker
API. Even with the runtime gates, a daemon configured to permit direct routing
or omit firewall rules can expose OpenCode beyond host loopback and the intended
tailnet path. This residual risk, and access through the container address,
remain until the structural container-side restriction tracked in
[#167](https://github.com/w2kr1stn/djinn_in_a_box/issues/167).

The host administrator owns the HTTPS Tailscale Serve entry and tailnet policy.
Djinn only reads `tailscale serve status --json`; it never writes either.
Grant HTTPS to this host only from intended devices or a dedicated tag, rather
than every device of the account. Djinn's hostctl helper is itself an account
device and would be included by an account-wide grant. Keep it outside that
grant; the password remains a second barrier.
Funnel would expose the service to the public internet, so detected Funnel
refuses both start and pair and warns in status. Detection covers root and
`Foreground` configurations with the union of their Funnel flags. Missing or
unreadable Tailscale status allows start with an unknown-Funnel note; it is not
proof that Funnel is off. Pairing refuses without a known tailnet URL.

The password is generated once on the host at
`${XDG_STATE_HOME:-~/.local/state}/djinn/serve/opencode-password`, mode `0600`
under a private directory. Before creation or reading, Djinn checks every component
of the password path without following symlinks and refuses locations inside the
config root, zone sources, Djinn backups or any running-container bind source, each
taken at its resolved location. Symlinks in the password path and undeterminable
locations refuse. Staying outside synced trees and backups also
requires keeping `XDG_STATE_HOME` host-local, outside other sync, indexing and
backup tools. Delete it and run `djinn serve opencode` to
rotate; the running managed server restarts with the new password. Invalid
existing files refuse rather than silently regenerating. Delivery uses stdin
into private `~/.config/opencode/service.json`; OpenCode also puts the password
in `~/.local/state/opencode/service.json`. Both container copies stay in the
writable layer, outside binds/volumes. Mounts covering those paths, relocated
settings roots, symlinked components and service settings other than `password`
are refused on the applicable host/helper start gates. Agents running as dev
can read these files: the password protects network access, not against code
already executing as that user.

The implementation rule is password delivery through stdin and a private file,
never server argv/environment or Djinn logs. A live leak scan is best-effort
corroboration, not a proof of absence: oversized/unreadable files, paths outside
its scanned roots and the captured event window limit its coverage.

Claude Remote Control has a different boundary: it relies on the signed-in
vendor account and makes outbound HTTPS connections through Anthropic, without
opening an inbound listener. Remote sessions execute with the container's
authority, including its mounted files, credentials, tools and any granted
Docker/host access. Claude's documentation gives a default capacity of **32
concurrent sessions**, sharing the working directory by default; Djinn passes
neither a capacity override nor a spawn-mode override. Treat account/device
access as authority to operate those sessions. See
[Claude's Remote Control documentation](https://code.claude.com/docs/en/remote-control)
for connection security and server defaults. `--firewall` coverage of the hosts
Remote Control needs has not been measured.

## Host-side workflow publishing

The host CLI reads and writes workflow trees that dev can write: the per-CLI
config roots and the `config/claude` and `config/opencode` seeds. The workflow
publisher and config sync anchor every access there to an opened root directory
and follow no symlink at or below it. A symlinked or non-directory path component
is refused as a collision, and links, FIFOs, sockets and devices are never opened
for I/O. Ancestors of these roots are trusted. A workspace or other managed root
that contains the installation is refused at creation, because the installation
is a host execution input ([creation-time rule](#sealed-deployments-and-trusted-controller));
placing a config root inside a writable mount such as the workspace remains
outside this guarantee. The publisher contains its own I/O; it does not protect a
dev-writable tree from dev.
Host creates, chmods and deletes below the config, shared and local zone roots,
sessions root and Claude seed root use the same opened-root anchoring. They never
follow a symlink below it. Backup and restore keep pathname tar and the post-restore
chmod walk under the lifecycle guard with no Djinn container running, so no dev
container can start meanwhile. Pathname operations remain for missing roots under
trusted ancestors, the installation's own files and the chosen code directory.

## Desktop boundaries

The delivered D-Bus and audio helpers close the raw desktop paths tracked by
[#80](https://github.com/w2kr1stn/djinn_in_a_box/issues/80) and
[#82](https://github.com/w2kr1stn/djinn_in_a_box/issues/82). They qualify as sealed
only when the live inspector verifies their delivery and provenance. Upstream
sockets stay private to the helpers; dev receives read-only output directories.

D-Bus uses `xdg-dbus-proxy --filter`, with `--call` and `--broadcast` restricted
to the `org.freedesktop.Notifications` interface on
`/org/freedesktop/Notifications`, plus necessary bus bookkeeping and the
client's own unique ID. It does not grant name-wide TALK: other interfaces and
objects of the notification name's owner, often the desktop shell, are denied.
Other services, including systemd1 and the Secret Service, are denied. The helper
build requires Debian's fixed proxy revision `0.1.6-1+deb13u3` or later.
Notification contents and hints, including sound hints, are not sanitized and
still reach the host service. See the [exact policy](helpers/desktop/policy.json).

Audio loads only its native Unix listener and playback/capture tunnels to host
defaults, then disables module loading and daemon exit. Its relay cookie is
distinct from the optional upstream cookie; only the relay cookie reaches dev.
Microphone capture intentionally remains available: an agent can listen to the
microphone. Module denial is not a microphone privacy control.

Helpers have no Docker socket, host namespaces, devices or network access. Their
daemons and health clients run as UID/GID 1000 with groups/capabilities cleared.
Unsupported UID mappings or failed authentication omit that channel with a
warning; there is no raw fallback. Desktop helpers use `restart: on-failure`:
after a host reboot they may run without dev until the next `djinn start`
reclaims them ([#98](https://github.com/w2kr1stn/djinn_in_a_box/issues/98)). This
can prolong their access to upstream desktop sockets; it does not reopen a
hostctl window, whose helper uses `restart: no`.

## Sealed deployments and trusted controller

“Sealed” is the result of [core/host_sealing.py](src/djinn_in_a_box/core/host_sealing.py),
not a promise that every mounted secret or host-escape technique is blocked.
For a running dev container it requires complete inspection and no named causes:

- No host Docker socket (including relocated sockets) or unverified Docker
  endpoint/context. The sole exception is the managed Unix endpoint of the
  verified rootless companion belonging to this dev generation. Host-owned state
  must record the dev and companion IDs and trusted manifest; host inspection
  must match the pinned image, complete environment/startup/profile, network ID,
  managed volumes and endpoint consumers. Companion workspace mounts must equal
  the trusted delivery and be a subset of dev's own mounts, at the same paths and
  modes. Names, labels and daemon-reported identity alone never qualify.
- No other reachable Docker proxy through shared networks, host networking or
  non-loopback published ports. Only the verified companion's inherited EXPOSE
  2375/2376 metadata is exempt; all other peers are still inspected. A leftover
  host proxy matters even without a Docker start flag.
- No bind of host `/`, the controller user's home or its ancestors; no exposure
  of `djinn-hostctl-state`, its backing storage or the Docker data root.
- No bind exposing private controller/journal, post-mortem records, agent or
  runtime control state.
- No writable bind overlapping host execution inputs: Djinn's installation and
  build context, configuration/agent/zone files, Python environments/executable,
  effective import directories and loaded modules, Docker CLI/configuration/
  plugin directories, host `PATH` directories, and the location of every symlink
  on one of these paths, all determined at check time.
  Other configuration the host interprets, such as shell startup files, Git, SSH
  or user service configuration, is not in this inventory, nor are the targets of
  symlinked commands inside `PATH` directories (for example a tool's own install
  tree); do not mount them writable.
  At creation, Djinn refuses a writable `[mounts.*]`, `--mount` or `--here` bind
  overlapping this same execution-input inventory. The same predicate covers
  managed writable binds whose host paths the configuration selects: the workspace
  root (also delivered to the agent-Docker companion), config-root tool directories
  (`claude`, `codex`, `opencode`, `gh`, `age`), assigned shared/local zone overlay
  sources, and `~/.djinn/sessions`, in every Docker mode. Undeterminable inputs or
  sources refuse creation, as do managed sources whose paths contain `..` or `:`.
  Inaccessible execution inputs make sealing report inspection uncertain (under
  Docker authority see below). The creation check
  applies to pathnames at that point in time and pins no inode: sources and their
  ancestors must stay stable and outside any agent's rename authority (for example
  through another writable bind of a parent directory) until Docker has bound them.
  Sealing continues to report managed-bind and running-state findings.
- Desktop channels are absent or verified filtered/locked, with no raw endpoints.

Workspace content that host tools execute anyway is outside this creation-time
rule: Git hooks (`core.hooksPath`, `.git/hooks`), build files and build backends,
`.envrc`/direnv, and editor tasks. The workspace is writable by design; Djinn cannot
inventory what a host tool will execute from it. Running such tools on the host
against the workspace executes agent-writable content. Host-mode `djinn session`
keeps its separate [automatic-loading guard](#direct-routes-and-target-authority).

The installation's built-in workflow seed binds (`config/claude`,
`config/opencode`) are outside this rule because their host paths are not
configurable. Their bind modes are decided separately; sealing reports a writable
one as writable installation content.

Inspection resolves canonical ancestors, inode aliases and local named-volume
bind options, and inventories nested sockets/hardlinks with bounds. Missing,
malformed, unsupported or incomplete provenance is **unknown**, not sealed.
The exception removes only a Docker cause. A sensitive workspace, such as host
`/`, remains a dev cause even when delivered identically to the companion.

The nested inventory is skipped when a cause is treated as Docker authority:
a Docker socket or relocated socket bind, a host-root bind, or an unverified
`DOCKER_HOST`. These are conservative classifications, not verified access.
Such a dev cannot become sealed, so the inventory cannot change the result. The
same holds for execution inputs that cannot be determined: under Docker
authority they do not refuse, without it they leave the inspection uncertain. The
cause still requires `--allow-unsealed`, and every other inspection error still
refuses. The inventory is a point-in-time scan: symlinks are not followed and
entries that vanish during the scan are skipped; a vanished bind root,
unreadable directories and other errors remain unknown. It is no boundary
against a hostile dev that can write to the bind, because dev can rename
entries while the scan runs. Protected inodes stay protected only in host trees
dev cannot write through any bind, or when they are kept out of the delivered
trees.

Sealed means verified provenance and absence of these named causes. It does not
promise kernel-exploit resistance, isolation from every host/LAN TCP service, or
absence of intentionally delivered credentials. The accepted rootless relaxations
and shared-kernel exposure still apply. Companion inspection uses the host only;
no companion exec or UID-map introspection contributes to sealing.
Without a running dev the assessment is deferred. Install the controller and
all its execution inputs outside dev-writable mounts; pinning an executable or
image ID cannot establish trust in writable inputs. Host support is Linux with
Docker; no init service is required.

`djinn hostctl on --allow-unsealed` acknowledges known causes and journals the
lost boundary. The journal groups bind-related causes; `djinn doctor` lists the
individual findings. With host authority, the window is only an operating aid.
The flag cannot override uncertain inspection, direct-route results, deadline,
trust or journal failures. The one exception is the nested inventory above: a
Docker-authority cause skips it instead of overriding its uncertainty, because
it cannot make such a dev sealed. The flag applies to that opening only. Before any new
interactive, headless or detached dev starts, unsealed/unknown planned delivery
closes and verifies the helper; sealed delivery pauses admission until actual
delivery and the probes in both dev and companion namespaces pass. Companion
ID, profile, network and storage are revalidated at admission commit and during
the open window; replacement, drift or uncertain inspection closes the window.
A creator never inherits the override. External dev removal or replacement closes
the window.

## Window state, expiry and logs

The host starts a separate helper using a version/digest-pinned official
Tailscale image and a protected static supervisor installed by `djinn build`.
Only that helper mounts `djinn-hostctl-state`. Its window/control directories
are `0700`, and window state/control socket are `0600`. Dev receives neither LocalAPI nor
control sockets; raw SOCKS is helper-loopback-only. The public relay admits
only declared peers on TCP 22 after enrollment, authenticated peer-key snapshot,
sealing/direct-route checks and durable journal readiness. Peer routes and host
keys stay fixed for an opening; generated strict host-key checking rejects
changed keys until the next opening. The relay has no per-client authentication:
any client that can reach it on the helper
network can request allowed destinations while admission is open. Keep
`djinn-network` dedicated to intended consumers.

PID 1 owns the deadline in the private state volume. UTC and Linux
`CLOCK_BOOTTIME` deadlines, generation and boot ID include login time and
suspend, and prevent clock rollback or reboot from extending a window. Admission
checks persisted state and deadline; missing, malformed or mismatched admission
state refuses access. Expiry is polled every 250 ms. Off/expiry
closes streams, including multiplexed SSH, stops tailscaled and exits. Restart
cannot reopen a generation. Host observer loss does not remove this timer.
`limit` resets the remaining duration through private, generation-checked IPC;
it cannot resurrect an expired or closed window.

The owner-only host journal is
`${XDG_STATE_HOME:-~/.local/state}/djinn/hostctl/journal.jsonl`, outside public
SSH delivery. It records on/off/limit attempts and outcomes, overrides, readiness
and observed helper expiry, with rotation. Admission requires successful journal
writes; a later host journal failure requests closure. New journal entries group
bind-related causes and unknowns, including reasons built from them; standalone
findings and existing entries are unchanged. Helper Docker logs
(`docker logs djinn-hostctl`, rotated at 10 MB with three files) record connection
IDs, destinations, UTC start/end and end reason, plus helper events. These are
metadata logs, not payload or session recordings; login URLs and private keys
are not journaled. SIGKILL, host loss, observer outages or log-write failures can
leave gaps or unmatched starts; exact missing end times cannot be reconstructed.

`connect` first checks generation and live admission over the private control
socket, then sends one SOCKS5 CONNECT to `127.0.0.1:1080`, always on TCP 22. It
adds no listener, direct route or per-client relay authentication. Connection
setup has one 10-second deadline, cleared before streaming. Djinn places no
timeout on waiting for the owner's SSH approval; window closure still cuts the
stream. After Ctrl-C the relay's stream to the target can persist until the
target closes it or the window ends. Only connection metadata is logged.

The check's configuration and `known_hosts` are private temporary files under
the hostctl state root, removed on completion, error or Ctrl-C. After SIGKILL or
power loss a `check-*` directory can remain under the hostctl state root; it
holds only public host keys and configuration and can be removed. Status's
trailing `Summary` is passive; an unknown observation remains unknown and is
not a claim of complete logging.

Post-mortem records are separate JSON files under
`~/.local/state/djinn/post-mortem/`, fixed to the controller's passwd home rather
than `XDG_STATE_HOME`. They include final container state, limits and log tails;
those tails may contain secrets that agents printed and are not filtered.
The directory is `0700`, files are `0600`, with at most 10 records / 8 MiB
retained and a 1 MiB cap per record. They share the protection of the private
git-agent socket beside them. Commands and observer notifications print only
the record path or a recorder-owned failure code, never record contents.
Records are never included in Djinn backups; operator-managed backup and sync
must exclude `~/.local/state/djinn` too.

This confidentiality is documented best effort. Sealing's private-path map
includes the records directory, so hostctl admission and doctor report a bind
exposing it. Ordinary dev starts do not enforce this private-state boundary.
An operator who binds the controller home or state directory into dev exposes
the records to its agents.

Evidence is best effort too: Docker removes a stopped container's cgroup, so
cgroup memory figures may already be unavailable. Foreground `--rm` containers
leave no container evidence after removal. Recording failures do not prevent
recovery. The 15 s budget bounds Docker calls and records-lock waits; local
filesystem calls, including write, fsync, rename and pruning, are outside that
bound. A hung host state filesystem can still block the command.

## Direct routes and target authority

The host must prevent forwarding from the Docker network into the tailnet for
all declared IPv4/IPv6 addresses. Hostctl changes no firewall. Its throwaway,
digest-pinned probe shares only dev's network namespace and runs trusted isolated
Python, without dev files/environment, to attempt raw TCP connections on port
22. Reached or unknown results refuse admission; doctor uses the same probe and
reports deferred results without dev or authenticated peers.

This is a diagnostic, not enforcement: dev has `NET_ADMIN` and can hide routes
during a probe and restore them later. A “blocked” result can also mean the
target refused or timed out. It does not prove host forwarding policy. The
owner must configure and verify that policy independently.

A declared target account with authority to run `djinn` or Docker, or use `sudo`,
on the **controller host** can renew or reopen windows, including through a
detached remote job. Declaring the controller itself is a common way to expose
this path. Use target accounts without controller, Docker or `sudo` authority
on that host. Closing SSH does not stop detached target jobs or undo their work;
target-side job and privilege controls remain the owner's responsibility.

Generated SSH options request strict host-key checking and disable agent/X11/port
forwarding, local commands and automatic multiplexing. Agents can override client options or use another
client. The relay enforces destination/port/deadline, not the OS user, command
or encrypted SSH channels. Enforce users, privileges and any forwarding or
command restrictions on the target with controls supported by its SSH server.
Tailscale SSH uses its own server; ordinary OpenSSH `sshd_config` and
`authorized_keys` do not constrain that server.

The container can edit the workflow source in `config/`. Without a running
container, `djinn session` publishes the selected agent's workflow to its native
host root and runs it as the host user. Before that publication, Djinn requires
confirmation of the managed item set for that host root: relative file paths,
SHA-256 hashes of the final bytes, executable flags, and managed settings
fragments identified by carrier and key path. This includes Claude's host hook
rewrite and `CLAUDE.md` bridge. Changed, added or removed items require another
confirmation at a terminal; non-TTY callers refuse with exit 1 until confirmed.
The prompt defaults to No and lists the changes and their feeding source paths.
Review output shows names literally, without markup or emoji codes, and escapes
non-printable characters; an exclusive directory lock serializes trust-record
updates.

Confirmation is recorded in
`~/.config/djinn_in_a_box/host-workflow-trust.json`, separate from the publisher's
last-published manifest. Missing, unreadable or malformed records count as
unconfirmed. Djinn releases the config lease while waiting for the user, then
reloads the final payload under a new lease and compares it with the approved
set. A changed or unloadable payload, or a source change detected by the
publisher before its first write, aborts publication and launch. Only the
approved immutable bytes are written; trust is saved atomically with owner-only
permissions after successful publication. A record-save failure prevents launch.
Container delivery and in-place workflow editing remain unchanged.

This protection covers managed items, not every byte in the host root:
neighbouring personal settings remain host-owned, and managed removals follow
the runtime manifest. Confirmed hook commands can reference files outside the
published payload; changes to those files are not covered. Deliberately widened
mounts (`--mount`, `CODE_DIR`, or `--here`) can expose a native host root or the
trust record. A config root whose tool directory is the native host root also
allows direct container writes and is outside this protection. Docker-direct
and `djinn audit` have host authority; audit mounts the Djinn configuration
directory alongside the Docker socket. The trust directory has no dedicated
mount in the normal container setup, but is not protected against those routes.
Symlink-following host-side publisher writes into container-writable trees remain
a separate issue, [#133](https://github.com/w2kr1stn/djinn_in_a_box/issues/133).
The sealed check is unchanged. See [session integration](docs/suite-integration.md).

Without a running container, `djinn session` must not automatically load
executable configuration from its container-writable session workspace. The
boundary covers the host agent start, Git's automatic inspection of the
workspace repository (including submodules and ancestor discovery through
`~/.djinn/sessions`), and destinations configured in that repository. Claude
starts with `--setting-sources user`, so project/local settings, `.mcp.json`,
and project `CLAUDE.md`, skills, agents and commands are not loaded. OpenCode
starts with `OPENCODE_DISABLE_PROJECT_CONFIG=1` and refuses `.opencode`,
`opencode.json`, `opencode.jsonc`, `tui.json` and `tui.jsonc`; Codex refuses
`.codex`. Other configured agents are refused on the host and run unchanged in
the container.

Before a host start, Djinn requires an inert workspace repository for Git's
automatic inspection. Its `.git` must be a real directory with a regular
`HEAD` and real `objects` and `refs` directories, with no `commondir` or
`modules`. The config may contain only allowlisted keys; every remote needs a `url`, and
remote destinations must use network URLs or scp-style addresses, not local
paths, `file://` or remote helpers. `hooks` must be absent or a real directory containing only
`*.sample` entries; legacy `remotes` and `branches` must be absent or real,
empty directories. Any `.git` entry below the workspace root refuses the host
start, including nested repositories and submodules. The workspace root must
not hold `HEAD`, and physical ancestors from the workspace's parent through
`~/.djinn/sessions` must contain neither `.git`, `HEAD` nor selected-agent
project configuration, so Git cannot discover another repository there. Repository inspection reads files
and uses `git config --file` to parse config without running commands the
workspace configures. A workspace containing a nested clone is refused in host mode.

A refusal starts no host agent and returns `SessionResult(returncode=1, …)`;
the CLI exits 1. Its message identifies the offending entry, key or destination
and advises running in the container or reviewing and removing the entry on
the host. See [session integration](docs/suite-integration.md).

This boundary covers automatic loading only. Workspace content can still
prompt-inject the model, which may explicitly run workspace programs or issue
Git commands against other repositories—for example through a directory
symlink, a bare repository, or a repository under `~/.djinn/sessions` addressed
by path. Interactive host starts retain their permission-bypass flags. These
checks are a pre-launch snapshot, not writer isolation: a `djinn run` container,
a later container start, failed container discovery, or another host process
can still write to the workspace.

Host-owned inputs—`agents.toml` flags, the caller's `env` and inherited
environment—are trusted and must not be writable by a container. Remote
endpoints in an accepted repository remain container-chosen network
destinations. As in the CLI, a workspace path below `~/.djinn/sessions` must
resolve strictly below it. For direct API callers with workspaces chosen
outside `~/.djinn/sessions`, only the workspace itself is checked. Measurements
used Claude Code 2.1.296, Codex 0.162.1, OpenCode 1.19.0 and Git 2.39.5; a host
Claude without `--setting-sources` fails to start.

## Git keys and browser identity

The dedicated host Git agent loads only declared identities; its export permits
list/sign and rejects key-set changes, lock/provider/extension requests. Managed
delivery contains public selectors/trust and a signing socket, without private
key files. Custom mounts can reintroduce private keys; the sealed check does not
inventory every secret. A read-only
socket mount still allows signing requests. Key secrecy therefore does not stop
an agent from authenticating or signing with a loaded key; there is no destination
restriction on that capability. Keep Git public keys out of target
`authorized_keys` wherever access should depend on hostctl, or ordinary SSH can
provide a route around the window. Public `IdentityFile` and `IdentityAgent`
selection are documented in [Git setup](README.md#git-identities-and-ssh-signing).

Use a separate, owner-controlled browser/profile for Tailscale enrollment and
SSH check approval. An agent-controlled browser must not hold the owner's IdP
or tailnet administration session, cookies or reusable login state. Otherwise
the agent can approve its own requests. Djinn does not configure IdP/MFA policy
and cannot guarantee fresh MFA for every SSH connection.

## Enrollment and tailnet policy

On each controller host, the first `on` enrolls one untagged `djinn-<machine>`
node under the owner's account. Obtain the login URL with `hostctl status` on
the host terminal and complete it in the owner-controlled browser. No saved
auth key is used. In the admin console's Machines page, select that node and
**Disable Key Expiry**. This keeps a persistent identity usable across openings;
revocation remains an owner action. See [Tailscale key expiry](https://tailscale.com/docs/features/access-control/key-expiry).

These are independent controls:

| Control | Meaning |
| --- | --- |
| Node-key expiry | Tailscale's device identity lifetime; disabling it does not open a window. |
| Window expiry | Helper-enforced cutoff for relay access, independent of node identity. |
| SSH check reauthentication | Target policy's approval interval, independent of window duration. Opening a window does not reset or force this check. |

After enrollment and relay admission, the owner can run
`djinn hostctl check [HOST]` in the host terminal. Host OpenSSH connects through
`docker exec -i` and the helper's generation-checked `connect` operation to the
same gated relay used by agents. It verifies the opening's frozen peer host keys
and uses the opening's configured target user. Its generated `-F` configuration
isolates user and system SSH settings, disables host private-key/agent
authentication, forwarding, local commands and connection sharing, and keeps
strict peer host-key checking. It sets `PasswordAuthentication no`;
keyboard-interactive authentication keeps its default, so a keyboard-interactive
approval prompt stays possible. Djinn never
reads, alters, captures, parses or journals the SSH banner or approval link;
OpenSSH inherits the terminal streams. The enrollment login link from `status`
is separate from that SSH approval banner. Approval remains in the owner's
browser, in the owner-controlled browser profile required above. `connect` is
reachable only through `docker exec` on the controller host, with the same
authority as `admit` and `pause`.

Tailscale documents check reuse only for the applicable `checkPeriod` (default
12 hours); its exact cache key across source node, SSH user and destination is
undocumented. With `checkPeriod: "always"`, a host check does not approve the
agent's later connection. Each check is a real login as the target user running
`true`, visible as a connection in the relay log and as a login/session in the
target's auth log.

Approving the banner of a host check approves the helper's source identity for
that target user: within the target policy's `checkPeriod` it can also cover the
agents' later connections as that user; that is the intended effect.

Never sync or back up node state, including through host/Docker snapshots.
Copying it copies the identity. Djinn excludes the volume from backup/restore;
normal cleanup retains it, explicit name deletion refuses, and only
`djinn clean all` deletes it and requires fresh enrollment. Host journal and
supervisor storage are host-local too.

The owner enables Tailscale SSH on intended targets and edits the tailnet policy.
This generic example grants account-source TCP 22 and checked SSH only to tagged
targets as the explicitly declared OS user:

```json
{
  "tagOwners": {"tag:djinn-target": ["owner@example.com"]},
  "grants": [{
    "src": ["owner@example.com"],
    "dst": ["tag:djinn-target"],
    "ip": ["tcp:22"]
  }],
  "ssh": [{
    "action": "check",
    "src": ["owner@example.com"],
    "dst": ["tag:djinn-target"],
    "users": ["operator"]
  }]
}
```

Tag only intended targets; leave the helper untagged. The account source also
covers other nodes owned by that account. `autogroup:self` would broaden
destinations to the owner's devices. Review other rules for broader grants.
The default SSH check period is 12 hours and available on all plans;
`checkPeriod: "always"` is an optional, plan-dependent per-connection check,
not a Djinn promise of fresh MFA. See [policy syntax](https://tailscale.com/docs/reference/syntax/policy-file),
[Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh) and
[plan entitlement](https://tailscale.com/pricing).

## Credentials, backups and outbound traffic

Remote agent access adds the [inbound server boundary and host-local OpenCode
password](#remote-agent-server-access), alongside the credential and outbound
limits below.

Per-CLI storage separates files, not agent authority. Credential/config roots
are ordinary cleartext files protected by host directory permissions (`0700`);
`djinn doctor` reports drift and `--fix` repairs it. Every agent running as dev
can read the credentials mounted for every CLI, including an `age` master
decryption identity. Sealed does not mean those secrets are hidden. Prompt
injection into a write-enabled agent can use or disclose them.

`--firewall` is a container-side IPv4 outbound allowlist, not an exfiltration
guarantee. Dev has `sudo` and `NET_ADMIN` and can change it. Rules allow DNS,
established connections, loopback, all RFC1918 internal destinations and resolved
allowlisted addresses, which can also carry secrets. They resolve domains once
at startup and do not cover IPv6. They permit the helper's private bridge address;
a custom helper network outside RFC1918 refuses readiness. This firewall cannot
replace the host no-forwarding prerequisite. See [the rules](scripts/init-firewall.sh)
and [firewall usage](README.md#what---firewall-allows).

Codex's managed app-server daemon defaults to remote control off upstream.
Enabling it requires `codex app-server daemon enable-remote-control` and a
ChatGPT login; Djinn does not change `remoteControlEnabled`. The log line
`remote control websocket loop started … initial_desired_state=Disabled`
describes a waiting task, not an established connection.

Every dev-container start sets only `updater.autoUpdateEnabled=false`, aligns
an existing daemon selection with the image's Codex CLI and prunes old releases.
Other settings are preserved; malformed or symlinked settings warn and remain
untouched, and policy failures do not stop startup. This is an update policy,
not a vendor-traffic block: plugin discovery can contact `chatgpt.com`,
`github.com` and `api.github.com`; model traffic can use `api.openai.com` or
`chatgpt.com` depending on authentication/provider. The optional firewall's
[listed domains](README.md#what---firewall-allows) include `api.openai.com` and
`auth.openai.com`, but omit `chatgpt.com`.

Backups are age-encrypted by default under `~/.djinn/backups/`. `age` prompts
directly at the terminal; Djinn never handles or stores the passphrase. Losing
it makes the archive unrecoverable. `--no-encrypt` produces cleartext archives
that need the same protection as live credentials.

The config zone is backed up and may be deliberately mirrored; the shared
sibling contains transcripts and needs an owner-managed backup/mirror; local
caches/scratch and Codex SQLite stores should remain host-local. SQLite includes
durable goals, pending messages and preferences, not just rebuildable history;
the config-root backup excludes it. Transcripts may contain sensitive content.
Djinn does not delete agent-owned transcripts; configure retention with
the agent's native controls. See [backup and sync usage](docs/sync-across-machines.md).
