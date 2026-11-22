# Remote access to coding agents

Connect to Claude Code or OpenCode in a detached Djinn container.

[Back to the README](../README.md)

## Remote Agent Servers

`djinn serve <claude|opencode ...>` uses the running detached container's working directory, mounts and sign-ins.
Repeated starts keep healthy servers whose settings match. OpenCode restarts when its password or tailnet CORS
origin changes. After a crash or container restart, start the servers again yourself. Before updating Djinn,
run `djinn serve --stop opencode`. Djinn does not adopt pre-relay records. If a legacy server still owns port 4096,
stop it or recreate the container.

Recreate an existing container once to get the OpenCode port publication:

```sh
djinn clean
djinn start --detach
djinn serve opencode
djinn serve claude opencode
djinn serve --status
djinn serve --pair opencode
djinn serve --stop opencode
djinn serve --stop claude opencode
djinn serve --stop
```

Start, `--status`, `--stop` and `--pair` are separate modes. Start and OpenCode-only pairing require a detached
container. Status and stop can inspect running containers without the start prerequisites. A failed Docker inspection
reports an unknown state. Omitting names with `--stop` stops both servers, and stopping an inactive server succeeds.

### OpenCode Web UI and API

OpenCode serves its UI and API to host users and processes at `http://127.0.0.1:<serve.opencode_host_port>`
(default `49380`, container port `4096`). Change the host port before creating the container.

```sh
djinn config set serve.opencode_host_port 49381
djinn config show
```

To apply a changed port, run `djinn clean`, then `djinn start --detach`. Djinn refuses a missing or mismatched binding with
`The running container does not publish the OpenCode port on 127.0.0.1:<port>.`
The container relay accepts only the gateway on port `4096` and forwards it to OpenCode at `127.0.0.1:4097`.
See the [inbound boundary and its assumptions](../SECURITY-MODEL.md#remote-agent-server-access).

The first `djinn serve opencode` generates a password in host `$XDG_STATE_HOME/djinn/serve/opencode-password`
(mode `0600`). Unset `XDG_STATE_HOME` means `~/.local/state`. An empty value is refused. Djinn refuses symlinks,
locations it cannot verify, and resolved locations in the config root, zone sources, Djinn backups, running-container
bind sources or volume host storage (including local-driver bind aliases). It refuses existing files with unsafe
ownership, permissions or type or invalid size/encoding. Keep `XDG_STATE_HOME` host-local and outside sync, indexing
and backup tools. Djinn prints only the path. Read the password locally. Delete the file and rerun `djinn serve opencode`
to rotate it and restart the server. Attached clients must reconnect.

Log in at the local or tailnet HTTPS URL, or open a one-time link from `djinn serve --pair opencode`. Pairing
requires a running server and a known tailnet URL. CLI clients use `opencode --server <url>` with `OPENCODE_PASSWORD`.
In-container sessions share the service. After `--stop opencode`, the next client that needs it starts the ordinary
loopback service.

For tailnet access, create the Tailscale Serve entry as host administrator with the configured host port.

```sh
tailscale serve --bg --https=443 http://127.0.0.1:49380
```

Djinn reads Tailscale settings with `tailscale serve status --json` to discover the HTTPS URL. Keep Funnel disabled.
`djinn serve opencode` and `djinn serve --pair opencode` refuse detected Funnel, and status warns about it. Missing
Tailscale or unreadable status allows startup with `Funnel state unknown (<reason>)`. Review the host configuration.

Grant HTTPS access only to intended devices or a dedicated device tag. Account-wide grants include the hostctl
helper node, which should stay outside this grant. The OpenCode password is the second barrier. See
[Cleanup and Uninstall](storage-and-recovery.md#cleanup-and-uninstall) for OpenCode login export before volume removal.

### Prerequisites and Refusals

Before first start, check host `/etc/docker/daemon.json`. `allow-direct-routing` must be absent or `false`, and
`iptables` absent or `true`. Docker's API cannot expose these settings to Djinn. See the
[residual routing risk](../SECURITY-MODEL.md#remote-agent-server-access).

OpenCode start and pair refuse unless they can verify these prerequisites.

- A known Docker Engine server version **28.3.3 or newer** with the loopback publication fix for
  [CVE-2025-54388](https://github.com/moby/moby/security/advisories/GHSA-x4rx-4gw3-53p4).
- Exactly the configured IPv4 loopback publication, and only user-defined
  bridge networks. Host/container network mode and drivers such as macvlan,
  ipvlan or overlay refuse. Every attached bridge must omit
  `com.docker.network.bridge.trusted_host_interfaces`; IPv4 and IPv6 gateway
  modes must be neither `routed` nor `nat-unprotected`.
- Every `net.ipv4.conf.*.route_localnet` container and endpoint sysctl must be `0`, including actual values checked
  by the helper. New containers inherit the host's `all`/`default` values and refuse if loopback routing is enabled.
  Disable it on the host or in container and endpoint sysctls, then recreate the container.
- Keep `HOME` at `/home/dev` and unset `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `OPENCODE_CONFIG_DIR`. Mounts
  covering, inside or above `~/.config/opencode` or `~/.local/state/opencode` are refused.

Before changing the service, the helper refuses symlinks in settings paths and allows `~/.config/opencode/service.json`
to contain only `password` or be absent. Remove all other keys with `opencode service unset <key>` inside the container,
including `remote` (OpenTunnel), `hostname`, `port`, `cors`, `env` and `disabled`. Replacing a service requires proof
of process identity beyond its discovery file.

### Claude Code Remote Control

`djinn serve claude` runs `claude remote-control` in server mode. Generated session names start with the host's
short name normalized to lowercase letters, digits and hyphens. Sign in to a claude.ai subscription (for example,
`claude auth login`) inside the container and trust the working directory in an interactive session first. API-key
authentication is refused. Login or trust refusals appear in the startup log tail and in status if Claude exits.
The `--firewall` allowlist may omit required hosts. See
[Claude's requirements](https://code.claude.com/docs/en/remote-control#requirements)
and the [Remote Control trust boundary](../SECURITY-MODEL.md#remote-agent-server-access).

Codex Remote Control runs through [the daemon started by the Codex TUI](agent-workflow.md#codex).

### Server Status

`djinn serve --status` shows OpenCode's local endpoint and tailnet URL or the reason it is unknown. `djinn status`
shows the local URL for running OpenCode under Services. Running or degraded rows show `relay refused since start: <count>`
(reset on relay restart). A missing, unreadable, unrelated or stale counter shows `relay refused: unknown (<reason>)`
and leaves the count undetermined.

If legitimate connections close and the count rises, inspect `/tmp/djinn-serve/opencode-relay.log` inside the
container and check `userland-proxy`, direct routing and the publishing network. With `"userland-proxy": false`, loopback
DNAT/masquerading is assumed to present the gateway source. The relay refuses other sources. Publication through a
non-default network of a multi-network container is also refused. See the
[routing assumption](../SECURITY-MODEL.md#remote-agent-server-access).

| State | Meaning |
| --- | --- |
| `running` | The recorded processes have matching identities. OpenCode also has its loopback listener and service registration plus a live relay owning port 4096; Claude has both its supervisor and child process groups. |
| `degraded` | Recorded processes remain, but OpenCode lost its backend, relay, listener or registration, or Claude lost its child or supervisor. Run `djinn serve <name>` to stop the remains and restart it. |
| `exited` | The server ended in this container run. Its last sanitized log line remains visible until the next start or stop. |
| `stopped` | No current managed server record, or stale state from a container restart or reused PID. Stale state is ignored and cleared when the state lock is available. |
| `busy (start/stop in progress)` | A start or stop holds the state lock; retry the query after it finishes. This is a snapshot warning, not a server state. |

Status distinguishes a missing container, missing `--detach`, and Docker errors. It also reports detected Funnel,
`port not published (recreate once)`, and `note: port 4096 is used by another process` for a foreign listener, with the
legacy hint `a djinn serve from before the update — recreate the container`. Stop leaves the port publication and host
Tailscale Serve entry in place.
