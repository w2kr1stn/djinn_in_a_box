## The session workspace contract (generic public API)

Djinn is standalone by default. Any external integration can drive sessions by
using the same workspace contract as the `djinn session` command.

The host session root is:

```text
~/.djinn/sessions/<project>
```

The running container sees the same files at:

```text
/home/dev/sessions/<project>
```

`docker-compose.yml` binds `${HOME}/.djinn/sessions` to
`/home/dev/sessions`, so writes are bidirectional between host and container.
Create the project workspace before using the CLI or API — either manually or
via the CLI flag:

```sh
mkdir -p ~/.djinn/sessions/<project>
# or let the CLI create it on first use:
djinn session --project <project> --create
```

Python consumers can install djinn from a git HTTPS URL and import the session
manager:

```sh
python -m pip install "djinn-in-a-box @ git+https://<git-host>/<org>/<repo>.git"
```

```python
from pathlib import Path

from djinn_in_a_box.core.session import SessionManager

project = "<project>"
workspace = Path.home() / ".djinn" / "sessions" / project

manager = SessionManager(project)
manager.preflight_check()

interactive = manager.run_interactive(workspace_dir=workspace)
headless = manager.run_headless(
    workspace_dir=workspace,
    prompt="<prompt>",
    timeout=300,
)
```

`SessionManager(project_name)` accepts project names made of letters, numbers,
underscore, dash, or dot, starting with a letter or number. Its public session
entry points are:

- `preflight_check()`: verifies that a session can run in container mode or
  host fallback mode.
- `run_interactive(workspace_dir=..., agent=..., model=..., initial_prompt=..., env=...)`:
  starts an interactive session.
- `run_headless(workspace_dir=..., prompt=..., agent=..., model=..., timeout=..., env=...)`:
  runs a prompt and captures output.

Both run methods accept the optional keyword-only parameter
`env: dict[str, str] | None = None` for credentials and other additional variables
needed by one session. For example:

```python
headless = manager.run_headless(
    workspace_dir=workspace,
    prompt="<prompt>",
    agent="codex",
    env={"OPENAI_API_KEY": "<provider-api-key>"},
)
interactive = manager.run_interactive(
    workspace_dir=workspace,
    agent="claude",
    env={"ANTHROPIC_API_KEY": "<provider-api-key>"},
)
```

`None` and `{}` preserve the defaults. Other types, including empty lists or
other mappings, raise `ValueError`. Djinn snapshots the dictionary at method
entry, before discovery, Git initialization, or launch. Allowed entries override
inherited values for that session and its descendants. An empty string is a
literal value, not a deletion request. Values are never interpolated, converted,
normalized, or replaced; newlines, quotes, `$`, `=`, and Unicode remain literal.
Neither the caller's map nor `os.environ` changes, and entries do not carry over
to later calls or persist to configuration files.

Keys must match `[A-Za-z_][A-Za-z0-9_]*`. Values must be strings without NUL or
surrogates, strictly encodable as UTF-8, and must round-trip unchanged when the
actual host environment encoding (`os.fsencode`) is decoded as strict UTF-8.
Opaque surrogateescape values are rejected. These rules apply equally in host
and container mode. Invalid entries and protected names raise a value-free
`ValueError` before discovery, Git initialization, or launch; encoding exception
details are suppressed.

Protected names combine the complete shared Djinn/Compose
[`RESERVED_ENVIRONMENT` policy](../src/djinn_in_a_box/config/declarations.py)
with these additional session transport restrictions:

- Prefixes: `DOCKER_`, `COMPOSE_`, `DJINN_`, `LD_`, `DYLD_`, `BASH_`.
- Exact names: `ENV`, `SHELLOPTS`, `BASHOPTS`, `CDPATH`, `GLOBIGNORE`, `BASH_ENV`,
  `SSL_CERT_FILE`, `SSL_CERT_DIR`, `XDG_CONFIG_HOME`, `GODEBUG`, `GOTRACEBACK`.
- Bash-managed names: `BASH`, `BASHPID`, `COMP_WORDBREAKS`, `EPOCHREALTIME`,
  `EPOCHSECONDS`, `HISTCMD`, `LINENO`, `OLDPWD`, `OPTERR`, `OPTIND`, `PPID`,
  `PS1`, `PS2`, `PWD`, `RANDOM`, `SHLVL`, `SRANDOM`, `_`. Bash changes or removes
  these even without startup profiles, so both execution modes reject them.
- Case-insensitive names: `http_proxy`, `https_proxy`, `all_proxy`, `no_proxy`.

This deliberately includes shared reservations beyond transport names, such as
`HOME`, `PATH`, `AGENT_PROMPT`, `TERM`, and `LOCAL_ENDPOINT`. Shared-policy changes
therefore require compatibility consideration for this API. Protected names are
rejected rather than ignored; the map cannot change the Docker connection or
selected target.

Host discovery, Git setup, and image/workflow preparation receive no additions.
Host agent starts use a fresh inherited environment plus the validated entries
and fixed terminal values. Container starts forward only the additional names
with `docker exec -e NAME`; their values travel in the Docker subprocess
environment, outside argv and Djinn-authored logs or diagnostics. The existing
prompt transport is unchanged. Container session shells and their Git/agent
descendants inherit the entries; login profiles can intentionally change them,
and Djinn does not reapply them afterwards.

Inherited credentials are not scrubbed. Values remain visible to the OS, Docker
daemon, descendants, and user profile scripts. Child stdout/stderr passes through
unchanged, even if a child prints a credential. The secrecy guarantee covers
Djinn's handling of this map, not values independently placed in prompts or
emitted by children. This Python API does not select providers, check credentials,
or add a CLI environment flag.

When a container is running, workspace paths under `~/.djinn/sessions/` are
mapped 1:1 into `/home/dev/sessions/`. For example,
`~/.djinn/sessions/<project>/<subdir>` maps to
`/home/dev/sessions/<project>/<subdir>`. A workspace outside the host session
root falls back to `/home/dev/sessions/<project>`.

Both run methods return `SessionResult` with `returncode`, `stdout`, `stderr`,
`workspace_dir`, and `success`. `success` is true when `returncode == 0`.

The CLI equivalent is:

```sh
djinn session --project <project>
djinn session --project <project> --prompt "<prompt>" --timeout 300
djinn session --project <project> --create   # create the workspace if missing
```

Without `--create`, the workspace directory must exist before the command runs.

Without a running container, session preflight checks the selected agent on host
`PATH`; the CLI prepares its native host workflow before invocation. See the
[security model](../SECURITY-MODEL.md#direct-routes-and-target-authority) for the
open host-fallback workflow path and target authority limits.

## Suite mode (optional)

Suite mode is additive. Djinn does not require any peer application, and the
session contract above works without extra services.

To make a host-local MCP service available inside the container, add an entry to
the local MCP registry at `config/mcp-servers.json`. Its seed template is `{}`.
Use generic names and replace the port before use:

```json
{
  "my-local-app": {
    "transport": "sse",
    "url": "http://host.docker.internal:<port>/sse"
  }
}
```

`docker-compose.yml` maps `host.docker.internal` to the host gateway for the
main `djinn` container, so services listening on the host can be reached from
inside the container through that hostname.
Review the [host-network and credential implications](../SECURITY-MODEL.md)
before exposing local services to agents.

At startup, `scripts/mcp-register.sh` reads the registry from
`${MCP_SERVERS_CONFIG:-$HOME/.config/mcp-servers.json}`. The compose file mounts
`./config/mcp-servers.json` into the container at
`/home/dev/.config/mcp-servers.json:ro`, and the registration script consumes
enabled entries from that file. Supported transports are `streamable-http` and
`sse`; clients that do not support a transport skip that entry.

The compose environment also sets:

```text
LOCAL_ENDPOINT=${LOCAL_ENDPOINT:-http://host.docker.internal:11434/v1}
```

Set `LOCAL_ENDPOINT` when containerized agents should call a host-local LLM API
endpoint. Leaving it at the default or not using it does not change standalone
session behavior.
