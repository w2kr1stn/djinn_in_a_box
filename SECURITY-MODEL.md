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
| Docker socket proxy | Fewer API paths, but container creation still gives host authority through unchecked bind mounts, privilege and networking options. |
| Helper identity volume, its backing directory or Docker data root | Exposes the reusable tailnet identity and helper state; writable access also defeats state integrity. Ancestor and descendant binds matter too. |
| Raw host D-Bus | Access to host session services, potentially including desktop-shell execution and secret services. |
| Raw host audio socket | Playback/capture and an upstream module-loading interface where the host server permits it. |

Docker access is off by default. [Docker modes](README.md#docker-access-modes)
opt into proxy or direct delivery. The proxy in
[docker-compose.docker.yml](docker-compose.docker.yml) is Tecnativa's
[Docker Socket Proxy](https://github.com/Tecnativa/docker-socket-proxy), configured as follows:

| Settings | Configured API surface |
| --- | --- |
| `CONTAINERS`, `IMAGES`, `NETWORKS`, `VOLUMES`, `INFO`, `VERSION` = `1` | Read/list/inspect APIs. |
| `POST`, `ALLOW_START`, `ALLOW_STOP`, `ALLOW_RESTARTS` = `1` | Container creation/lifecycle and image pulls. |
| `BUILD`, `COMMIT`, `EXEC`, `SWARM`, `SECRETS`, `CONFIGS`, `PLUGINS`, `SERVICES`, `TASKS`, `NODES`, `AUTH` = `0` | Those API families are denied. |

Dev receives `DOCKER_HOST=tcp://docker-proxy:2375`; only the proxy mounts the
socket, read-only. Port 2375 is exposed on `djinn-network`, without a host port
publication. Filtering API paths does not validate container-create payloads.
Blocking exec/build/commit/auth and Swarm operations reduces available tools,
but does not prevent a new container from mounting the host. Both Docker modes
are unsealed; adding `--firewall` does not change that.

Direct delivery is read-write. The entrypoint adjusts socket group access for
`dev`; its non-root UID does not limit Docker authority. Agent write mode plus
Docker access combines workspace modification with daemon control. Dev's default
4-CPU/8G limits and the proxy's 0.5-CPU/128M limits do not constrain containers an
agent creates through that daemon. Those containers can exhaust host resources
or reach other services and networks independently of dev's firewall. Prefer no
Docker access when unnecessary, review created resources, and use
[status/audit commands](README.md#status-and-audit-commands) to inspect proxy logs.

`build.network = "host"` shares the host network namespace for image `RUN`
steps, including access to loopback-only host services. It applies to the whole
build, so use it only with trusted Dockerfiles and build inputs. It affects
build-time networking, not the resulting container's runtime network.

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

- No Docker socket (including relocated sockets), nonempty `DOCKER_HOST`, or
  Docker proxy exposed through shared networks, host networking or non-loopback
  published ports. A leftover proxy matters even without a Docker start flag.
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
Without a running dev the assessment is deferred. Install the controller and
all its execution inputs outside dev-writable mounts; pinning an executable or
image ID cannot establish trust in writable inputs. Host support is Linux with
Docker; no init service is required.

`djinn hostctl on --allow-unsealed` acknowledges known causes and journals the
lost boundary. With host authority, the window is only an operating aid.
The flag cannot override uncertain inspection, direct-route results, deadline,
trust or journal failures. It applies to that opening only. Before any new
interactive, headless or detached dev starts, unsealed/unknown planned delivery
closes and verifies the helper; sealed delivery pauses admission until actual
delivery and the probe pass. A creator never inherits the override. External
dev removal or replacement closes the window.

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
writes; a later host journal failure requests closure. Helper Docker logs
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

One known host path remains open:
[#81](https://github.com/w2kr1stn/djinn_in_a_box/issues/81), host-mode session
fallback executing container-writable workflows. Without a running container,
`djinn session` can deliver the selected agent's workflow to its native host
root and run the agent there. The sealed check does not close this execution
path. Treat these workflows as trusted host inputs and avoid host fallback with
container-writable workflows. See [session integration](docs/suite-integration.md).

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
