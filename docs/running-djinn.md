# Running Djinn

Choose an interactive shell, a one-shot run, or a workspace you can return to.

[Back to the README](../README.md)

## Container Entry Points

| Command | Container behavior | Workspace behavior | Main use |
| --- | --- | --- | --- |
| `djinn start` | `docker compose run --rm` removes the interactive dev container on exit. `--detach` uses `docker compose up -d` without an attached client | [Workspace and extra mounts](storage-and-recovery.md#workspace-mounts) | Interactive or detached shell |
| `djinn enter` | Uses `docker exec -it <running-container> zsh` | Enters an already running Djinn container | Open a second shell while `djinn start` is still running |
| `djinn serve` | Starts Claude Remote Control or the OpenCode web UI/API inside the running detached container | Uses the container's working directory, mounts and sign-ins | [Remote agent access](remote-agent-servers.md#remote-agent-servers), status, stop and OpenCode pairing |
| `djinn run AGENT PROMPT` | `docker compose run --rm -T` runs a [guest](#guest-runs) and removes it on exit | [Workspace and extra mounts](storage-and-recovery.md#workspace-mounts) | One-shot agent prompts |
| `djinn session` | `docker exec` into running dev, or host fallback after verifying the selected binary on `PATH` and checking the workspace. See [workflow delivery](agent-workflow.md#tool-owned-runtime-state) for host and OpenCode runtime setup | `~/.djinn/sessions/<project>` on the host and `/home/dev/sessions/<project>` in the container. `--create` creates the host workspace | Reusable session workspaces |

`djinn run` exit codes:

| Code | Meaning |
| --- | --- |
| Agent's code | Normal agent completion, including failure. |
| `1` | Any run refusal. |
| `124` | Timeout. |
| `126` / `127` | Command cannot be executed / command not found. |
| `130` | Ctrl-C, SIGTERM or SIGHUP. |

Every exit after launch cleans up only that run. Djinn verifies its name and labels, stops it with a 10 s grace
period, then force-removes it if needed. Cleanup takes up to about 55 s and ignores further interrupt, termination
and hangup signals. Failure to verify or remove the container warns with its name and `djinn clean` as a remedy.
`docker ps -a` can briefly show `Dead` after `djinn run` returns while Compose finishes auto-removal.

See the [Codex daemon policy](agent-workflow.md#codex) for when dev applies the policy. `enter` and `session`
attach without rerunning the entrypoint.

Host fallback asks before publishing an unconfirmed or changed workflow to `~/.claude`, `~/.codex` or
`~/.config/opencode`. Stderr lists new (`+`), changed (`~`) and removed (`-`) managed items, executables, settings
fragments and source locations. Confirmation defaults to No. Declining leaves the host root, manifest and trust
record intact and exits 1. Non-TTY callers exit 1 until you confirm using `djinn session --agent <agent>` at a terminal.
Confirmed, unchanged workflows run without another prompt, including with `--prompt`.

Confirmation is per agent root and recorded in `~/.config/djinn_in_a_box/host-workflow-trust.json`. The manifest
alone grants no trust. Container edits to published files or managed `settings.json` hooks trigger another review.
Personal `config/claude/settings.local.json` stays outside host delivery and review. Host review applies only to
host fallback. Host start refuses unsafe agent or Git workspace settings. See the
[host-start boundary](../SECURITY-MODEL.md#direct-routes-and-target-authority).

See [Docker access modes](access-and-boundaries.md#docker-access-modes) for `--docker` (`-d`, managed Unix
`DOCKER_HOST`) and `--docker-direct` (`docker-compose.docker-direct.yml`). The two are mutually exclusive.
`--firewall` (`-f`) applies outbound rules in dev and before a companion starts. See
[workspace mounts](storage-and-recovery.md#workspace-mounts) for `--here` and `--mount` (`-m`).

## Guest runs

See the [README example](../README.md#one-prompt-one-container) for a read-only prompt. `djinn run` uses the
agent's read-only mode by default when one is configured. Add `--write` to request edits.

```sh
djinn run claude "Fix the failing test." --write
```

A run can coexist with dev and other runs without blocking `djinn start`. It retains workspace mounts, config-root
credentials, workflow, shell/SOPS/zone mounts, declarations and `--firewall`. Desktop endpoints and hostctl aliases
are excluded. Its network is separate from dev and companions. See the [run boundary](../SECURITY-MODEL.md#guest-runs)
and [Git/SSH limits](storage-and-recovery.md#git-identities-and-ssh-signing).

## Desktop integration

Two isolated helpers built by `djinn build` serve notifications, playback and microphone capture. Dev receives
only their read-only output directories. Foreground or detached `djinn start` prepares each available endpoint
independently and warns and omits a failed one. `djinn clean` removes helper volumes. Backup and restore exclude them.

Recreate dev with `djinn start` after rebuilding or restarting the host desktop session or audio server. A plain
container restart keeps existing mounts and cannot add omitted endpoints. See the
[desktop boundaries](../SECURITY-MODEL.md#desktop-boundaries).

## Starting without an attached terminal

Use `djinn start --detach`, then `djinn enter`. Djinn refuses interactive start in a background process group
because the TTY-attached Compose client can flood the container with SIGTTOU and overflow Docker's event buffer.

## Suite Mode

External applications can use the same workspace mapping as `djinn session`.
See [suite integration](suite-integration.md) for the public session contract
and optional host-local MCP service pattern.

## Typical Workflows

| Task | Command |
| --- | --- |
| Mount the current directory and open a shell | `djinn start --here` |
| Use a separate Docker daemon and outbound firewall | `djinn start --docker --firewall` |
| Let Codex write files, with a timeout | `djinn run codex "Implement the missing test." --write --timeout 300` |
| Create and open a reusable workspace | `djinn session --project my-project --create` |

See [entry points](#container-entry-points) for shell commands, [Guest runs](#guest-runs) for a read-only prompt and
[Session headless mode](headless-cheatsheet.md#session-headless-mode) for sending
a prompt to a named workspace.
