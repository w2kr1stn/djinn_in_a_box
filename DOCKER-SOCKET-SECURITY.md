# Docker Socket Security Analysis

## Security analysis: Docker socket access for Djinn containers

This analysis evaluates the security implications of Docker access in a
containerized development environment for AI coding agents such as Claude Code,
Gemini CLI, Codex, and OpenCode.

The host Docker socket is a high-risk interface. Any mode that can create
containers through the host daemon must be treated as host-level control unless
the request path is tightly restricted and monitored.

---

## Implemented Security Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                         HOST SYSTEM                             │
│                                                                 │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                      Docker daemon                         │ │
│  │                                                            │ │
│  │  docker.sock ◄────────────────────────────────────────┐    │ │
│  │       │                                               │    │ │
│  │       ▼                                               │    │ │
│  │  ┌─────────────────────────────────────────────────┐  │    │ │
│  │  │              djinn-network                      │  │    │ │
│  │  │                                                 │  │    │ │
│  │  │  ┌──────────────┐      ┌──────────────────┐     │  │    │ │
│  │  │  │ docker-proxy │◄─────│      djinn       │     │  │    │ │
│  │  │  │              │ TCP  │ (dev container)  │     │  │    │ │
│  │  │  │ FILTERS:     │      │                  │     │  │    │ │
│  │  │  │ ✓ ps/images  │      │ Claude Code      │     │  │    │ │
│  │  │  │ ✓ run/start  │      │ Gemini CLI       │     │  │    │ │
│  │  │  │ ✓ stop       │      │ Codex CLI        │     │  │    │ │
│  │  │  │ ✗ exec       │      │ OpenCode         │     │  │    │ │
│  │  │  │ ✗ build      │      └──────────────────┘     │  │    │ │
│  │  │  │ ✗ commit     │                               │  │    │ │
│  │  │  └──────┬───────┘      ┌──────────────────┐     │  │    │ │
│  │  │         │              │   mcp-gateway    │     │  │    │ │
│  │  │         │              └──────────────────┘     │  │    │ │
│  │  │         ▼                                       │  │    │ │
│  │  │  ┌──────────────┐                               │  │    │ │
│  │  │  │   Spawned    │                               │  │    │ │
│  │  │  │ containers   │                               │  │    │ │
│  │  │  └──────────────┘                               │  │    │ │
│  │  └─────────────────────────────────────────────────┘  │    │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘

Legend:
  ─────►  Allowed data flow
  ✓       Allowed operation
  ✗       Blocked operation
```

There are three Docker access modes:

- No Docker socket: default.
- Proxy mode: `docker-compose.docker.yml` starts `djinn-docker-proxy` and points
  the development container at `DOCKER_HOST=tcp://docker-proxy:2375`.
- Direct mode: `docker-compose.docker-direct.yml` bind-mounts the host socket
  directly into the development container.

---

## Toggle Mechanism

### Available modes

| Mode | Command | Docker access | Firewall | Security posture |
|------|---------|---------------|----------|------------------|
| **Default** | `djinn start` | None | No | High |
| **Proxy** | `djinn start --docker` | Filtered proxy | No | Medium |
| **Firewall** | `djinn start --firewall` | None | Yes | High |
| **Proxy + firewall** | `djinn start --docker --firewall` | Filtered proxy | Yes | Best option when Docker is needed |
| **Direct** | `djinn start --docker-direct` | Direct socket | No | Critical risk |

`djinn run` supports the same Docker choices for headless execution:
`--docker` enables proxy mode and `--docker-direct` enables direct mode. The two
flags are mutually exclusive.

### Why use a toggle?

1. **Least privilege**: Docker access is disabled unless a task needs it.
2. **Explicit activation**: Each start command declares the intended Docker mode.
3. **Visible status**: `djinn start` and `djinn run` print the selected mode.
4. **Task-specific risk**: Routine analysis can run without Docker access, while
   build or container tasks can opt into a narrower mode.

---

## Docker Socket Proxy: Technical Details

### What is the proxy?

The [Tecnativa Docker Socket Proxy](https://github.com/Tecnativa/docker-socket-proxy)
is an HTTP proxy between the development container and the host Docker socket.
It filters Docker API paths with environment-variable allowlists.

In the current compose files:

- `docker-compose.docker.yml` mounts `/var/run/docker.sock` into the proxy
  container as read-only: `/var/run/docker.sock:/var/run/docker.sock:ro`.
- The development container does not receive the socket in proxy mode.
- The development container receives `DOCKER_HOST=tcp://docker-proxy:2375`.
- The proxy is attached to `djinn-network` and exposes port `2375` only to that
  Docker network, not to the host.

The read-only bind mount limits writes to the socket inode from inside the proxy
container, but it does not make Docker API calls read-only. If the proxy allows a
Docker API operation, the host daemon can still perform that operation.

### Allowed operations

```yaml
# Read operations
CONTAINERS=1      # List and inspect containers
IMAGES=1          # List and inspect images
NETWORKS=1        # List networks
VOLUMES=1         # List volumes
INFO=1            # Retrieve Docker info
VERSION=1         # Retrieve Docker version

# Write operations
POST=1            # Allow POST requests required by create/start/stop
ALLOW_START=1     # Allow container start
ALLOW_STOP=1      # Allow container stop
ALLOW_RESTARTS=1  # Allow container restart
```

The entrypoint also reports `docker pull` as available when proxy mode is
connected. Treat image pulls as part of the Docker attack surface because they
execute image-distribution logic through the host daemon.

### Blocked operations

```yaml
# Security-critical operations
BUILD=0           # Image build
COMMIT=0          # Commit a container to an image
EXEC=0            # Exec into running containers
SWARM=0           # Swarm operations
SECRETS=0         # Docker Secrets
CONFIGS=0         # Docker Configs
PLUGINS=0         # Plugin management
SERVICES=0        # Swarm services
TASKS=0           # Swarm tasks
NODES=0           # Swarm nodes
AUTH=0            # Registry authentication
```

### Why these restrictions matter

| Blocked surface | Reason |
|-----------------|--------|
| `exec` | Prevents entering existing containers from the development container. |
| `build` | Prevents builds that can package host-mounted files or secrets into images. |
| `commit` | Prevents converting a modified container into a persistent image. |
| `auth` | Reduces exposure of registry credentials. |
| Swarm resources | Avoids cluster-wide control paths that are outside Djinn's normal scope. |

---

## Risk Analysis: Direct Socket vs. Proxy

### Direct socket mount

`docker-compose.docker-direct.yml` implements direct socket mode:

```yaml
services:
  dev:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - DOCKER_ENABLED=true
      - DOCKER_DIRECT=true
```

This mount has no `:ro` suffix, so it is read-write. The container runs as the
non-root `dev` user, but the Dockerfile grants passwordless `sudo`. During
startup, the entrypoint changes the socket group to the user's primary group and
adds group read/write permissions when needed. That makes Docker immediately
usable from inside the container.

**Risk:** direct Docker socket access is equivalent to root-level control of the
host through the Docker daemon.

Examples of what direct mode enables:

```bash
docker run --rm -v /:/host alpine sh
docker exec -it some-container sh
docker build .
docker commit some-container captured-image
```

Use direct mode only for work that genuinely needs unfiltered Docker behavior,
such as Dockerfile development or debugging the proxy path itself.

### Proxy-based access

`docker-compose.docker.yml` implements proxy mode:

```yaml
services:
  docker-proxy:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro

  dev:
    environment:
      - DOCKER_HOST=tcp://docker-proxy:2375
      - DOCKER_ENABLED=true
```

**Benefits:**

- No socket bind mount in the development container.
- Docker API filtering blocks several high-risk paths.
- Proxy logs are available through Docker logs and `djinn audit`.
- The proxy is network-local to `djinn-network`.

**Important limitation:** proxy mode still permits some container lifecycle
operations. Creating containers can still be dangerous, especially with bind
mounts, privileged flags, host networking, or sensitive image choices. Proxy mode
reduces the attack surface; it does not make Docker access safe.

---

## Remaining Risks

Even with the proxy, the following risks remain.

### 1. Dangerous container creation

**Risk:** an agent can attempt to create containers with high-risk options:

```bash
docker run --privileged -v /:/host alpine
```

The proxy blocks some Docker API paths but does not fully validate every
container-create option. Treat any ability to create containers as high risk.

**Mitigation:**

- Prefer default mode when Docker is not needed.
- Prefer `--docker --firewall` when Docker is needed.
- Review commands before granting write-capable agent work.
- Inspect containers after sensitive sessions.

### 2. Agent write mode plus Docker access

**Risk:** `djinn run --write` enables the selected agent's write/edit mode. For
Claude Code, the default configuration maps this to
`--dangerously-skip-permissions`. Combining `--write` with `--docker` compounds
the risk because the agent can both modify the mounted workspace and request
Docker operations through the proxy. Combining `--write` with `--docker-direct`
is more severe: direct mode still gives the container host-root-equivalent Docker
daemon access.

**Mitigation:**

- Use `--write` only for tasks that require file modifications.
- Avoid adding Docker access to write-capable agent runs unless the task requires
  Docker.
- Treat `--write --docker-direct` as a critical-risk session and audit Docker
  state afterward.

### 3. Resource exhaustion

**Risk:** an agent can spawn too many containers or resource-heavy containers.

The development container has default compose limits of 4 CPUs and 8G memory,
with reservations of 1 CPU and 2G. The proxy service is limited to 0.5 CPU and
128M memory. These limits do not automatically constrain every container the
host daemon may create.

**Mitigation:**

```yaml
deploy:
  resources:
    limits:
      cpus: '4'
      memory: 8G
    reservations:
      cpus: '1'
      memory: 2G
```

Also monitor host Docker state with `docker ps`, `docker stats`, and
`docker system df` when running untrusted or experimental prompts.

### 4. Network-based attacks

**Risk:** containers on the same Docker network can communicate with each other,
and newly created containers may be able to reach external networks.

**Mitigation:**

- Use `--firewall` for stricter outbound behavior inside the development
  container.
- Keep `djinn-network` dedicated to Djinn services.
- Stop unexpected containers promptly.

### 5. MCP Gateway socket access

The MCP Gateway compose file (`mcp/docker-compose.yml`) also mounts the host
Docker socket directly into the `mcp-gateway` container so it can spawn MCP
server containers. This is separate from `djinn start --docker`. Run MCP only
when you need it, and enable only MCP servers you trust.

---

## Security Checklist

### Daily use

```markdown
## Before a session
- [ ] Do I need Docker access? If not: `djinn start`
- [ ] If Docker is needed: prefer `djinn start --docker --firewall`
- [ ] Use `--docker-direct` only for tasks that require unfiltered Docker access

## After a session
- [ ] `djinn status` - check for unexpected containers or services
- [ ] `djinn audit` - review Docker proxy logs when proxy mode was used
```

### Weekly

```markdown
- [ ] `docker ps -a` - confirm all containers are expected
- [ ] `docker images` - confirm all images are expected
- [ ] `docker volume ls` - confirm all volumes are expected
- [ ] `docker network inspect djinn-network` - inspect attached containers
```

### If you suspect compromise

```bash
# Stop containers attached to the Djinn network
docker stop $(docker ps -q --filter "network=djinn-network")

# Review proxy logs
docker logs djinn-docker-proxy 2>&1 | grep -i "blocked\\|error\\|denied"

# Inspect network membership
docker network inspect djinn-network
```

---

## Audit Logging

### Show proxy logs

```bash
# Via CLI
djinn audit

# Directly
docker logs -f djinn-docker-proxy
```

`djinn audit` reports the last 50 proxy log lines by default and supports
`--tail` / `-n` to change the number of lines.

### Example log lines

```text
time="2026-01-15T10:30:45Z" level=info msg="GET /v1.43/containers/json"
time="2026-01-15T10:30:46Z" level=info msg="POST /v1.43/containers/create"
time="2026-01-15T10:30:47Z" level=warning msg="Blocked: POST /v1.43/exec/abc123/start"
```

### Optional local alert script

```bash
#!/usr/bin/env bash
docker logs -f djinn-docker-proxy 2>&1 | while read -r line; do
    if echo "$line" | grep -qi "blocked"; then
        printf 'Docker proxy alert: %s\n' "$line" >&2
    fi
done
```

---

## Conclusion and Recommendation

1. **Default to no Docker access.** Use `djinn start` or `djinn run` without
   Docker flags for ordinary analysis, editing, and review work.
2. **Use proxy mode when Docker is needed.** Prefer `--docker --firewall` for
   tasks that require container operations.
3. **Treat direct mode as host-root equivalent.** `--docker-direct` mounts the
   host Docker socket read-write into the development container and deliberately
   bypasses API filtering.
4. **Keep MCP optional.** The MCP Gateway also needs direct socket access to
   spawn MCP server containers. Run it only when those tools are required.
5. **Audit after risky sessions.** Review Docker state and proxy logs whenever an
   agent had Docker access, especially write-capable access.

The proxy design is the safer Docker-enabled mode currently implemented, but it
is still a reduced-risk mode, not a sandbox boundary.
