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
- `run_interactive(workspace_dir=..., agent=..., model=..., initial_prompt=...)`:
  starts an interactive session.
- `run_headless(workspace_dir=..., prompt=..., agent=..., model=..., timeout=...)`:
  runs a prompt and captures output.

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
