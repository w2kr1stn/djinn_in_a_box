# Diagnostics and maintenance

Check a Djinn installation, troubleshoot problems, and update bundled agents.

[Back to the README](../README.md)

## Doctor

```sh
djinn doctor
```

Doctor checks Docker, daemon and socket permissions, Compose v2, Buildx, config, workspace and config roots, image,
network, desktop helper delivery and seed targets. Desktop rows report off, filtered/locked, missing or unknown.
The raw-socket row inspects actual dev mounts. Docker rows report endpoint (none, verified agent, host-direct or
unknown), companion identity, health and cache, or "not in use". Stale/orphan resources get separate rows. Host
inspection is bounded and read-only. Doctor starts no helpers and neither starts nor repairs the daemon.

`Managed bind sources` applies the [creation-time check](configuration.md#configuration) without provisioning.
`PASS` says `no overlap with host execution inputs`. `FAIL` lists each overlapping or uncheckable source and remedy.

- `Codex daemon updater` passes when `updater.autoUpdateEnabled` is `false` or neither managed copy nor settings exist.
  It warns when enabled, when the key is missing or invalid, when installed copies lack settings, or when settings
  are malformed, over 64 KiB or symlinked. Remote-control state is informational.
- `Codex daemon releases` reports count, size and selection. It warns for multiple releases, an unreadable or
  incomplete scan, or an unusable `current` selection. Both rows warn for skipped required overlays and escape control
  characters in names. Neither row has a fix action. See the [startup policy](agent-workflow.md#codex).

`Config workflow` audits the canonical tree and start runtime roots read-only and names drift items and remedies.
`OpenCode runtime` checks the image label and the publisher via `docker exec` in running dev. Drift is a named `WARN`.
An unreachable container, timeout or unusable diagnostic is `check unavailable`. For missing or stopped dev, it
reports the [rebuild lifecycle](agent-workflow.md#tool-owned-runtime-state) without reading runtime files.

`Post-mortem records` reads only the filesystem and works without valid config or Docker. `PASS` shows `none` or
the retained count and latest path. An unreadable or unsafe records directory is `WARN`.

```sh
djinn doctor --fix
```

`--fix` provisions host directories, restores missing seeds and creates the Docker network when possible. Repairs
are idempotent and exclude Docker installation, invalid config and image builds.

## Status and Audit Commands

```sh
djinn status
```

Status shows containers, volumes, config-root paths, networks and services. See the
[server snapshot](remote-agent-servers.md#server-status) and `djinn serve --status` for details and endpoint hints.

```sh
djinn audit
djinn audit "Container fails to start" --agent opencode
djinn config set assistant.agent opencode
```

Run audit as your regular user in a foreground terminal on Linux with a local Unix Docker socket. Remote contexts
are unsupported. Audit builds its own image on first use or when its agent, Dockerfile or pin changes. It works
without dev and puts config load errors in the first input. See the [audit guide](../assistant/audit-briefing.md).

Audit mounts the installed repo, Djinn config directory, `~/.djinn`, local Docker socket and selected existing
credential files read-write. Custom config roots outside `~/.djinn` supply only those files. Workspace and custom zone
roots are excluded. Settings, hooks, plugins and history start fresh. Claude uses manual approvals. Codex uses
`danger-full-access` because its sandbox needs user namespaces unavailable here, with approval `on-request`.
OpenCode asks for edits and shell commands. All use normal approvals. Credential refreshes persist on normal or signal
exit, except abrupt SIGKILL. Sign in normally first if credentials are missing. The agent hands host-only build, sync
and recreation steps back to you. `--rm` removes the session container while approved edits and Docker objects persist.

OpenCode audit seeds a private database from the config root under `~/.local/state/djinn/opencode-audit/` (`0700`,
excluded from sync and backup). On exit Djinn merges logins and refreshes by union without logging out, then deletes
the session. `djinn doctor` reports crash leftovers, which the next `djinn audit` or `djinn start` recovers. Recovery
can wait up to 30 s for the carrier. During restore, backup, clean or a dev transition, an audit times out after at
most 15 s and names the holding process.

Resolve current Claude Code, Codex and OpenCode versions from npm.

```sh
djinn update
```

`djinn update` works without `djinn init` and atomically writes `~/.config/djinn_in_a_box/agent-versions.toml`
outside the checkout. Failed, timed-out or invalid lookups exit with an error and preserve the record. npm uses
your configured cache and logs (default `~/.npm`).

Dev builds pass a local `x.y.z` as a build arg only when it exceeds the Dockerfile default. The assistant applies
the same rule to its selected agent, so newer upstream defaults prevent downgrades. An unreadable or invalid
record stops either build and names the file. Delete it to return to upstream defaults.

Rebuild to install the resolved versions.

```sh
djinn build
```

Maintainers bump defaults in a development checkout with `scripts/update-agents.sh` (`--print` resolves versions
without edits). Move `~/.config/djinn_in_a_box/agent-versions.toml` aside and run `djinn build` with the defaults
and wait for it to succeed before committing and opening a pull request. [CI](https://github.com/w2kr1stn/djinn_in_a_box/actions/workflows/ci.yml)
does not build the image. After a Claude Code bump, run `scripts/probe-claude-isolated.py` as shown in
[Re-probing the isolation](agent-workflow.md#re-probing-the-isolation). After any agent bump, run
`uv run pytest -q tests/test_agents_chain_native.py` inside dev. See [Workspace AGENTS.md chain](agent-workflow.md#workspace-agentsmd-chain).

## Troubleshooting

Start with [Doctor](#doctor).

- Missing config: run `djinn init`.
- Docker not reachable: start the Docker daemon, then rerun `djinn doctor`.
- Image missing: run `djinn build`.
- Seed targets missing: run `djinn doctor --fix` or `djinn init`.
- No running container for `djinn enter`: start one with `djinn start`.
- Session workspace missing: create it manually or pass `djinn session --create`.
- Unknown agent: run `djinn agents` and check
  `~/.config/djinn_in_a_box/agents.toml` if you use overrides.
