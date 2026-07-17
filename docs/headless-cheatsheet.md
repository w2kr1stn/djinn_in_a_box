# Headless Mode Cheatsheet

Quick reference for running CLI agents in headless mode with `djinn run`, plus
the session-oriented `djinn session --prompt` path.

---

## Model Handling

Djinn does not validate model names. It forwards an explicit `--model` value to
the selected agent CLI; otherwise it uses that agent's configured
`default_model` when present:

| Agent | Model flag forwarded by Djinn | Notes |
|-------|-------------------------------|-------|
| `claude` | `--model <name>` | Example aliases depend on the installed Claude Code CLI. |
| `gemini` | `-m <name>` | Use a model supported by the installed Gemini CLI. |
| `codex` | `--model <name>` | Uses `codex exec` for headless runs. |
| `opencode` | `-m <name>` | Uses `opencode run` for headless runs. |

Check available models with the agent's own documentation or CLI help.

---

## Global Workflow Source and Audit

The agent selected for a headless run is independent of the deployment's global
workflow source. Configure that native source separately:

```bash
djinn config set config_sync.source claude   # or codex / opencode
djinn config sync
djinn config status
```

For a source change, keep the order **switch → sync → edit**. `status` is always
read-only; `sync` is the explicit writer. Sync requires a valid source and
refuses to overwrite an edited managed target. An unowned item at a managed path
is reported as a collision.

The setting is deployment-wide; the shared demo is one deployment, not a
per-tenant source selector. It covers only Djinn's global instructions, agents,
skills, commands, support files, and known startup/security/ready behavior.
Repository-local workflow files remain read-only, while credentials, auth,
history, themes, UI policy, MCP, arbitrary plugins, and unlisted settings remain
outside this ownership boundary.

`djinn config status` is safe for diagnostics and automation: it reports a
sanitized source, drift class, artifact locations, and one remedy without
printing workflow or settings bodies. It exits `0` only when the state is
`clean`; it exits `1` for `source-changed`, `target-drift`, `collision`, or
`invalid-or-semantic`. `djinn config sync` is non-zero when blocked.

Normal `start`, `run`, and `session` preparation repairs only deterministic
`source-changed` projection drift. `target-drift`, `collision`, and
`invalid-or-semantic` stop the command before an agent starts. There is no
semantic-provider fallback: non-portable artifacts block with this remedy:
“Author or edit the artifact natively in the target tool's view, or make the
source form portable.”

The shared publisher records the canonical `config/` tree in
`.djinn-config-sync.json` and every publisher-managed runtime root in
`.djinn-workflow-state.json`. Compose Claude is manifestless: it uses direct
mounts for both `CLAUDE.md` and its generated `AGENTS.md` companion plus the
Claude settings merge. An image without the `djinn.workflow.publisher=1` label
is rejected with `Rebuild/recreate required.` before a Compose run or an OpenCode
session refresh.

---

## CLI Usage (`djinn run`)

```bash
# Claude read-only analysis
djinn run claude "Explain the architecture" --model sonnet

# Claude with file modifications enabled
djinn run claude "Fix the bug in main.py" --write --model sonnet

# Gemini with a model override
djinn run gemini "Refactor the auth flow" --write --model gemini-2.5-pro

# Codex in headless mode
djinn run codex "Review this change" --model gpt-5

# OpenCode in read-only plan mode
djinn run opencode "Plan a cleanup for this module" --model default

# JSON output for scripting
djinn run claude "List all TODOs" --model sonnet --json | jq '.result'

# Mount a different workspace directory
djinn run claude "Analyze this project" --model sonnet --mount ~/other-project

# Mount the current working directory implicitly
djinn run claude "Summarize this repository"

# Enable Docker through the filtered proxy
djinn run claude "Build and test the project" --docker

# Enable direct Docker socket access only when the task requires it
djinn run claude "Debug this Dockerfile" --docker-direct

# Enable the outbound firewall
djinn run claude "Audit network usage" --firewall

# Show configured agents
djinn agents
```

`djinn run` starts an ephemeral Docker Compose container, sends the prompt through
the `AGENT_PROMPT` environment variable, prints agent output to stdout, and exits
with the agent's return code.

---

## `djinn run` Flags

| Flag | Description |
|------|-------------|
| `<agent>` | Agent name: `claude`, `gemini`, `codex`, or `opencode`. |
| `<prompt>` | Prompt to send to the agent. |
| `--model <name>`, `-m <name>` | Model override forwarded to the agent. |
| `--write`, `-w` | Enable the agent's write/edit mode. |
| `--json`, `-j` | Request JSON output from the agent. |
| `--mount <path>` | Mount a workspace directory as `/home/dev/workspace`. Defaults to the current directory. |
| `--docker`, `-d` | Enable Docker socket access through the proxy. |
| `--docker-direct` | Enable direct Docker socket access without the proxy. |
| `--firewall`, `-f` | Enable the outbound firewall inside the container. |
| `--timeout <sec>`, `-t <sec>` | Timeout for the headless container run. |

`--docker` and `--docker-direct` are mutually exclusive.

---

## Session Headless Mode

`djinn session` is a separate workflow for per-project session workspaces under
`~/.djinn/sessions/<project>/`.

```bash
# Create a session workspace if it is missing
djinn session --project my-project --create --prompt "Explain this codebase"

# Reuse an existing session workspace
djinn session --project my-project --prompt "Continue the review"

# Choose a different agent and model
djinn session --project my-project --agent gemini --model gemini-2.5-flash --prompt "Summarize changes"

# Increase the headless timeout
djinn session --project my-project --prompt "Run a deeper analysis" --timeout 900
```

When a running `djinn` container exists, session commands run through
`docker exec` inside that container. If no container is running, session
preflight checks the selected agent's configured binary on host `PATH`. Claude,
Codex, and OpenCode host fallback also receives that selected agent's canonical
workflow.
For a running-container OpenCode session, Djinn refreshes the live OpenCode
runtime through the shared publisher before invocation.

---

## `djinn session` Flags

| Flag | Description |
|------|-------------|
| `--project <name>`, `-p <name>` | Session namespace under `~/.djinn/sessions/`. Defaults to `default`. |
| `--agent <name>`, `-a <name>` | Agent to run. Defaults to `claude`. |
| `--model <name>`, `-m <name>` | Model override. When omitted, the selected agent's `default_model` applies. |
| `--prompt <text>` | Run headless. Omit this flag for interactive mode. |
| `--timeout <sec>`, `-t <sec>` | Headless timeout. Defaults to `300`. |
| `--create` | Create the session workspace if it does not exist. |

Project names may contain letters, numbers, underscores, dashes, and dots, and
must start with a letter or number.

---

## Choosing a Mode

| Use case | Suggested mode | Reason |
|----------|----------------|--------|
| One-off repository analysis | `djinn run <agent> "..."` | Uses the current directory by default and exits cleanly. |
| Scripted batch work | `djinn run ... --json` | Keeps stdout suitable for pipelines. |
| File edits in the current project | `djinn run ... --write` | Enables the agent's write mode for the mounted workspace. |
| Long-running project context | `djinn session --project <name>` | Reuses a stable workspace under `~/.djinn/sessions/`. |
| Container build or test tasks | `djinn run ... --docker` | Uses the Docker proxy instead of direct socket access. |
| Dockerfile or daemon debugging | `djinn run ... --docker-direct` | Uses unfiltered Docker access; treat as host-level authority. |
