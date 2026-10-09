# Security Model

## Threat model

Djinn's host control window protects against unwanted use of remote machines,
including mistakes and prompt injection. It is not a hardened sandbox against
deliberate host escape. Agents run as `dev` with passwordless `sudo` inside the
container; write mode can also bypass the agent CLI's approval prompts. An
approval setting is not an isolation boundary.

The window relies on a trusted host controller, a sealed deployment, no direct
Docker-to-tailnet forwarding, and owner-controlled target permissions. It gates
new SSH access and cuts relay connections; it cannot undo remote changes or stop
detached jobs already launched on a target. See [usage](README.md#host-control-windows)
and [implementation](IMPLEMENTATION.md#hostctl-helper).

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

## Host-side workflow publishing

The host CLI reads and writes workflow trees that dev can write: the per-CLI
config roots and the `config/claude` and `config/opencode` seeds. The workflow
publisher and config sync anchor every access there to an opened root directory
and follow no symlink at or below it. A symlinked or non-directory path component
is refused as a collision, and links, FIFOs, sockets and devices are never opened
for I/O. Ancestors of these roots are trusted; placing the project or config root
inside a writable mount such as the workspace is outside this guarantee. The
publisher contains its own I/O; it does not protect a dev-writable tree from dev.
Other host operations on these trees (provisioning, seeding, doctor repair,
session workspaces, backup and restore) are tracked in
[#138](https://github.com/w2kr1stn/djinn_in_a_box/issues/138).

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
- No bind exposing private controller/journal, agent or runtime control state.
- No writable bind overlapping host execution inputs: Djinn's installation and
  build context, configuration/agent/zone files, Python environments/executable,
  effective import directories and loaded modules, Docker CLI/configuration/
  plugin directories, and host `PATH` directories, all determined at check time.
- Desktop channels are absent or verified filtered/locked, with no raw endpoints.

Inspection resolves canonical ancestors, inode aliases and local named-volume
bind options, and inventories nested sockets/hardlinks with bounds. Missing,
malformed, unsupported or incomplete provenance is **unknown**, not sealed.
The exception removes only a Docker cause. A sensitive workspace, such as host
`/`, remains a dev cause even when delivered identically to the companion.
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
trust or journal failures. It applies to that opening only. Before any new
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

Another container-to-host path remains open: the host agent runs in the session
workspace `~/.djinn/sessions/<project>`, which every container mounts read-write.
Host agent CLIs load project-scope configuration from that directory, such as
Claude Code `.claude/settings.json` hooks or OpenCode project plugins, and
interactive host sessions start Claude Code and Codex with their permission
prompts disabled. Host workflow confirmation does not cover that configuration. Avoid host fallback in session
workspaces a container has written to; the fix is tracked in
[#140](https://github.com/w2kr1stn/djinn_in_a_box/issues/140).

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

Backups are age-encrypted by default under `~/.djinn/backups/`. `age` prompts
directly at the terminal; Djinn never handles or stores the passphrase. Losing
it makes the archive unrecoverable. `--no-encrypt` produces cleartext archives
that need the same protection as live credentials.

The config zone is backed up and may be deliberately mirrored; the shared
sibling contains transcripts and needs an owner-managed backup/mirror; local
caches/scratch should remain host-local. Transcripts may contain sensitive
content. Djinn does not delete agent-owned transcripts; configure retention with
the agent's native controls. See [backup and sync usage](docs/sync-across-machines.md).
