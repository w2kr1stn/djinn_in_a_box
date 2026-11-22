# Access and security boundaries

Configure file access, host SSH windows, Docker access, and outbound traffic.

[Back to the README](../README.md)

## Denied paths

In `config.toml`, put normalized absolute paths under `[general]`. The list defaults to empty.

```toml
[general]
denied_paths = ["/srv/private"]
```

Each entry blocks its absolute and home-relative forms (`/srv/private` and `~/srv/private`). In dev and `djinn run`
guests, Claude Code checks `Read`, `Edit`, `Write`, `NotebookEdit`, `Grep`, `Glob` and `Bash`. Codex checks `Bash`
(shell calls), `apply_patch` and `view_image`. The hook resolves symlinks and refuses Grep/Glob roots above a denied path.
Shell checks match literal paths at segment boundaries, including `~`, `$HOME` and `${HOME}` forms.
The check protects against slips. OpenCode, MCP tools, broad shell searches such as `rg <pattern> ~`, and deliberate
evasion are outside its scope. `/etc/codex/requirements.toml` enforces Codex hooks even if user settings disable them.

With an open hostctl window, `djinn-path-names HOST USER PATH [DEPTH]` lists names and metadata only.
Use an alias in `~/.ssh/tailnet_config`, the target user, and a normalized absolute path at or below a denied entry.
Depth is 0–5 (default 1), for example `djinn-path-names testhost alice /srv/private 2`. Output is
`TYPE SIZE MODIFICATION_TIME PATH` (`find -printf '%y %s %T+ %p\n'`). File contents stay blocked. Invalid input exits 2.
Otherwise it returns SSH's status.

## Host control windows

On a Linux Docker host, `djinn build` builds and installs the static supervisor. Declare hosts in `config.toml`.

```toml
[hostctl]
default_duration = "2h"

[hostctl.hosts.host-a]
address = "host-a.example.ts.net"
user = "operator"
```

```sh
djinn hostctl on --for 10m
djinn hostctl status
djinn hostctl check
djinn hostctl limit 5
djinn hostctl off
```

Durations are positive integer minutes (`10m`) or hours (`2h`), at most 24h. The deadline starts at `on`, including
login time. `limit` resets the remaining minutes (1–1440). Repeated `on` refuses and points to `limit`.
`off` is idempotent and works without config. For the asynchronous login via host-terminal `status`, node setup
and target policy, see [enrollment and tailnet policy](../SECURITY-MODEL.md#enrollment-and-tailnet-policy).

`on` normally returns after starting the helper. `djinn hostctl on --wait` polls this generation every second
and prints the enrollment link once when available. Use `djinn hostctl on --wait --timeout 60` to change the
timeout (positive integer seconds, default 300, requires `--wait`). Wait confirms relay readiness.
`djinn hostctl check [HOST]` proves SSH access and shows any target approval link.
Exit codes are 0 when open (even with a reported observer gap), 1 if the window closes, the generation changes
or the status check fails, 2 for invalid options, 124 on timeout, and 130 on Ctrl-C. Opening-state read errors are
retried and a timeout names the last error. Timeout and Ctrl-C stop waiting and leave the window open.

When `status` reports open, run `djinn hostctl check [HOST]` from the host terminal. Omit `HOST` for all targets in
declaration order, or use `djinn hostctl check host-a` for one alias. `check` requires terminal stdin and stderr
and `ssh` on the host PATH. Ctrl-C ends the check and removes its private temporary config. OpenSSH alone handles
the target approval banner, which is separate from the enrollment link. Approve it in your browser. Agents can then
use `ssh host-a` within the target policy's `checkPeriod`. `on` prints
"Once status reports open, djinn hostctl check shows the SSH approval link in this terminal."

Approval covers the helper's identity for that target user. Tailscale documents reuse for the applicable
`checkPeriod` (default 12 hours). The exact cache key across source node, SSH user and destination is undocumented.
With `checkPeriod: "always"`, a host check does not approve the agent's later connection. Each check is a real login
as the target user running `true`, visible as a connection in the relay log and as a login/session in the target's auth log.

Generated aliases stay in dev with the window closed, and Git signing works independently. Generated SSH delivery
requires host UID 1000 to match the dev image. See [guest limits](running-djinn.md#guest-runs).

`status` and refusals from `on` group sealing causes and unknowns by bind, naming up to three per class and
counting larger classes. Standalone findings keep their own lines. Grouped status prints `djinn doctor lists each item.`
`djinn doctor` lists each cause and unknown plus reached/blocked/unknown results per address for dev and its verified
companion. Every address must be blocked in both namespaces. Without dev or an authenticated peer snapshot, probes
are deferred. Companion replacement or profile drift closes the window. See the
[`--allow-unsealed` and close/pause rules](../SECURITY-MODEL.md#sealed-deployments-and-trusted-controller) and the
[host networking prerequisite](../SECURITY-MODEL.md#direct-routes-and-target-authority).

The `Summary` uses the existing snapshot without making SSH connections. It shows the window and approximate time left,
sealing counts and up to three groups each of unknowns then causes, relay state, current-generation peer aliases
and one next action. The helper owns the deadline. See
[window state, expiry and logs](../SECURITY-MODEL.md#window-state-expiry-and-logs)
and [host-local identity storage and cleanup](sync-across-machines.md#hostctl-node-identity).

## Docker Access Modes

The base mode delivers no Docker socket or Docker endpoint.

`djinn start --docker` starts a rootless companion from a pinned official image. Dev connects as UID 1000 through a
read-only endpoint volume at `unix:///run/djinn/agent-docker/socket/docker.sock`. Djinn exposes no TCP Docker API
to network peers. Reach published inner-container ports from dev at `agent-docker:<port>`.

An agent can relay the inner API through a published workload port, exposing the inner daemon and its workspace.
This grants no host-daemon authority.

The daemon receives dev's configured code directory, `--here` and CLI mounts, declared binds/volumes, and
`/home/dev/sessions` at the same targets and access modes. Dev's credential and desktop/Git mounts stay separate.
A workspace exposes any sensitive files it contains. Targets shadowing the companion's runtime or binaries are refused.

Dev includes Buildx. `docker buildx build --load .`, `docker run` and `docker compose up` use the inner daemon.
Relative Compose binds must resolve to a delivered workspace path. Images, volumes and BuildKit cache persist in
`djinn-agent-docker`, excluded from default backups. Normal clean removes the companion and temporary endpoint.
Explicit cache clean also removes the volume. Inner containers stop with dev and follow Docker restart policies
when the daemon starts again.

`djinn start --docker-direct` mounts `/var/run/docker.sock` and grants host-daemon authority. Runs refuse
`--docker-direct` while the hostctl helper is running. Close the window with `djinn hostctl off` or omit the flag.
`djinn run --docker` exits 1 before preparation and prints the following message.

> djinn run cannot use --docker: the rootless agent Docker daemon belongs to the dev runtime. Use --docker-direct (host Docker authority) or start a dev with `djinn start --docker` and work there.

A verified companion of dev's generation contributes no Docker sealing cause. Direct mode is unsealed. Unknown
or mismatched companion evidence refuses admission even with `--allow-unsealed`. Other sealing causes still apply.
See [host authority and Docker access](../SECURITY-MODEL.md#host-authority-and-docker-access).

## Outbound access

### What `--firewall` allows

With `--firewall`, the default is to deny outbound traffic. `scripts/init-firewall.sh` allows the Docker networks
and every IPv4 address resolved for its fixed domain list, including `deb.debian.org` and other package registries,
the bundled CLIs' API and sign-in endpoints, and GitHub. Djinn's network disables IPv6. The list resolves once at
startup, so a provider's new address is denied until restart. Blocked connections fail immediately with "connection refused".

`chatgpt.com` (Codex remote control and some plugin/model requests) and custom model providers are absent. Add
needed domains at the marked spot near the end of the array. Other Codex vendor traffic can still occur with its
updater and remote control off. This mutable IP list is no hard network boundary. See the
[limits for sensitive data](../SECURITY-MODEL.md#credentials-backups-and-outbound-traffic).

### Per-run allowlist: `djinn run --allow`

`djinn run … --allow ENTRY` enables the firewall for one run and replaces the fixed list. Repeat `--allow` per
destination. This is a run-only option and refuses `--docker` or `--docker-direct`.

Use `NAME[:PORT]`, `ADDRESS[:PORT]` or `ADDRESS/PREFIX[:PORT]` with IPv4 and prefixes 8–32 without host bits.
An explicit port allows only TCP to that port. Omit it to allow every protocol and port to that destination.

- The host resolves each name once before startup and requires all its addresses to be public. The run has no DNS
  and writes only these mappings to its hosts file. Other names fail immediately for most clients, while OpenCode
  retries until its resolver times out. List private and tailnet destinations as addresses or networks.
- Djinn refuses wildcards, empty entries, IPv6, URLs, spellings such as `0x08080808`, overlaps with `0.0.0.0/8`,
  `127.0.0.0/8`, `169.254.0.0/16`, `224.0.0.0/4` or `240.0.0.0/4`, and names mapped by the container itself
  (`localhost`, `host.docker.internal`, `djinn`). All unlisted traffic is refused immediately, including traffic
  to Docker networks, the fixed list and IPv6.

Root applies the rules before agent code runs. Afterward even root via `sudo` cannot change them or open raw
sockets. The run refuses startup-hook environment variables (`LD_*`, `BASH_ENV` and similar), mounts over
`/`, `/bin`, `/sbin`, `/usr`, `/etc`, `/lib`, `/lib64` or `/run`, and mounts exactly at `/home` or `/home/dev`.
It gets no desktop relays.

Before startup, `djinn run` prints `Firewall  Allowlist (N destinations)` and one `Egress` line per entry with its
resolved destinations. The run reads back kernel rules on stderr, one line per destination followed by the lock line.

```text
djinn-egress: allow 203.0.113.10/32 tcp/443
djinn-egress: locked 1
```

Only Djinn's first stderr block, ending with `djinn-egress: locked N`, proves the run is locked. Later lines with the
same prefix come from the run. Read stderr separately because `2>&1` puts arbitrary agent stdout first.

Hosts the bundled CLIs contact:

| CLI | Allow |
|-----|-------|
| Claude Code (subscription) | `api.anthropic.com:443`, `platform.claude.com:443` (token refresh) |
| Codex | `api.openai.com:443` with an API key, or `chatgpt.com:443` with a ChatGPT login; `auth.openai.com:443` (token refresh) |
| OpenCode | `opencode.ai:443` (its own service, needed at start), plus the API host of any other provider you configured, for example `api.anthropic.com:443` |

The Packages step skips [runtime package replay](storage-and-recovery.md#runtime-packages) and prints
`Packages: skipped in allowlist mode (no network).` on stderr after the `djinn-egress:` block.
With a cold tools cache, optional installer fetches can delay startup. The host firewall may further restrict
private or tailnet access. Compare the `djinn-egress:` lines with connection errors for listed destinations.
