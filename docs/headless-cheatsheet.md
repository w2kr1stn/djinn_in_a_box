# Headless Mode Cheatsheet

Quick reference for running CLI agents in headless mode with `djinn run`, plus
the session-oriented `djinn session --prompt` path.

---

## Model Handling

Djinn does not validate model names. It forwards the value you pass with
`--model` to the selected agent CLI:

| Agent | Model flag forwarded by Djinn | Notes |
|-------|-------------------------------|-------|
| `claude` | `--model <name>` | Example aliases depend on the installed Claude Code CLI. |
| `gemini` | `-m <name>` | Use a model supported by the installed Gemini CLI. |
| `codex` | `--model <name>` | Uses `codex exec` for headless runs. |
| `opencode` | `-m <name>` | Uses `opencode run` for headless runs. |

Check available models with the agent's own documentation or CLI help.

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
preflight permits host execution only when `claude` is on `PATH`. That preflight
does not validate the selected agent's host binary, so choosing another agent can
still fail at invocation if its binary is missing.

---

## `djinn session` Flags

| Flag | Description |
|------|-------------|
| `--project <name>`, `-p <name>` | Session namespace under `~/.djinn/sessions/`. Defaults to `default`. |
| `--agent <name>`, `-a <name>` | Agent to run. Defaults to `claude`. |
| `--model <name>`, `-m <name>` | Model override. Defaults to `sonnet`. |
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
