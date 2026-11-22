# Getting started with Djinn

Install Djinn on the host, build the dev image, and sign in to the bundled tools.

[Back to the README](../README.md)

## Requirements

- Docker Engine 25 or later, or Docker Desktop with such an engine
- Docker Compose v2.24.4 or later, available as `docker compose`. The run overlay replaces the network list
  with [`!override`](https://docs.docker.com/reference/compose-file/merge/#replace-value).
  Older Compose silently ignores the tag, so Djinn's network preflight refuses the run.
- Docker Buildx, available as `docker buildx`, used by `djinn build`
- `uv`, used to install and run the Python CLI

The CLI and tests target Python 3.14. `uv` normally manages the interpreter. See the [project metadata](../pyproject.toml).

## Quickstart

The editable CLI installation runs from the checkout where you follow the [README Quickstart](../README.md#quickstart).
`djinn init` writes `~/.config/djinn_in_a_box/config.toml`, provisions Docker bind sources, and copies
[missing seeds](configuration.md#the-blank-space-and-seed-model). See [Doctor](operations.md#doctor) for the preflight checks.

For browser or remote agent access, see [Remote Agent Servers](remote-agent-servers.md#remote-agent-servers).
For a one-shot prompt, see [Guest runs](running-djinn.md#guest-runs).

## First Authentication

Sign in from inside a normal development shell. Run each agent binary and follow its prompts.
Every bundled CLI supports device sign-in. Open the URL in your host browser and paste the code back into the container.
Claude Code and OpenCode select that flow automatically. Plain `codex login` starts a server on a container-local
port that your host browser cannot reach, so use the device flow for Codex.

```sh
codex login --device-auth
```

In the Codex TUI, choose *remote or headless machine*. For GitHub CLI, run `gh auth login` and choose device-code sign-in.
Credentials persist across image rebuilds. Include `credentials` when selecting backup categories.
See [Backup and Restore](storage-and-recovery.md#backup-and-restore) and the
[credential security model](../SECURITY-MODEL.md#credentials-backups-and-outbound-traffic).

### OpenCode logins

OpenCode 2 stores API keys, OAuth tokens, external logins and MCP OAuth in
`~/.local/share/opencode/opencode.db` on `djinn-opencode-data`. It ignores legacy `auth.json` and `mcp-auth.json`.
Djinn mirrors logins to `<config-root>/opencode/credentials.json` in `opencode auth export` format (mode `0600`).
`credentials.removed.json` in that directory holds globally removed IDs and contains no secrets.
At start, every 30 s, and at stop, Djinn merges the database, export and conflict copies into the config root
before updating the database.
The active account stays host-local, and OpenCode logout hides a login on this host only.
See [cross-machine backups](sync-across-machines.md#credential-and-data-backups) for conflict handling.

```sh
opencode auth logout                                   # in the dev container, this host only
djinn clean volumes --credentials --opencode-id <id>   # every host; repeatable
```

List IDs with `opencode auth list --format json` inside dev. For unlisted entries such as orphaned or cold MCP logins, use
`jq -r '.[] | "\(.id) \(.integrationID) \(.label)"' <config-root>/opencode/credentials.json`.
See [Cleanup and Uninstall](storage-and-recovery.md#cleanup-and-uninstall) to log out every OpenCode login at once.
