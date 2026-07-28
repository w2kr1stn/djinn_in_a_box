# MCP Gateway for Djinn Containers

This optional setup runs the Docker MCP Gateway so CLI coding agents in Djinn can
reach MCP servers from the Docker MCP Catalog.

Djinn ships the mechanism only: the image, CLI wiring, mounts, and neutral seed
templates. Your local `config/mcp-servers.json` registry is user-owned,
gitignored, and may stay empty. The seed template at
`templates/seed/config/mcp-servers.json` is intentionally `{}`.

## Prerequisites

### Docker MCP CLI plugin

Install the Docker MCP CLI plugin on the host before using `mcpgateway`.

```bash
# Option 1: build from source
git clone https://github.com/docker/mcp-gateway.git
cd mcp-gateway
make docker-mcp

# Option 2: build in a temporary container
docker run --rm -v ~/.docker/cli-plugins:/output golang:alpine sh -c "
    apk add --no-cache git make
    mkdir -p /root/.docker/cli-plugins
    git clone https://github.com/docker/mcp-gateway.git /build
    cd /build
    make docker-mcp
    cp /root/.docker/cli-plugins/docker-mcp /output/
    chown $(id -u):$(id -g) /output/docker-mcp
"

# Option 3: download a release binary
# https://github.com/docker/mcp-gateway/releases
# Copy it to ~/.docker/cli-plugins/docker-mcp

# Verify
docker mcp --help
```

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                    djinn-network (bridge)                       │
│                                                                 │
│  ┌─────────────────┐           ┌─────────────────────────────┐  │
│  │  MCP Gateway    │           │    Djinn container          │  │
│  │  (mcp-gateway)  │◄─────────►│  (djinn)                   │  │
│  │                 │ HTTP      │                             │  │
│  │  :8811          │           │  Claude Code / Codex        │  │
│  └────────┬────────┘           └─────────────────────────────┘  │
│           │                                                     │
│           │ spawns                                              │
│           ▼                                                     │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  MCP server containers                                   │  │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐          │  │
│  │  │ Search  │ │ Memory  │ │ Fetch   │ │ Time    │  ...     │  │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘          │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

The gateway listens on port `8811`, bound to `127.0.0.1` on the host and to
`mcp-gateway:8811` on `djinn-network`.

## Security Features

| Feature | Description |
|---------|-------------|
| `--verify-signatures` | Run only cryptographically signed MCP images. |
| `--log-calls` | Log tool invocations for audit. |
| `--block-secrets` | Block secret-like values in responses. |
| Resource limits | Limit the gateway service to 2 CPUs and 1G memory, with 0.25 CPU and 128M reservations. |
| Local host binding | Expose the host port only on `127.0.0.1`. |
| Dedicated network | Use the existing `djinn-network` bridge network. |

Important: `mcp/docker-compose.yml` mounts `/var/run/docker.sock` directly into
the gateway container because the gateway must spawn MCP server containers. This
grants significant Docker authority. Enable only MCP servers you trust.

## Quick Start

### 1. Start the MCP Gateway

```bash
mcpgateway start
```

The command verifies that `docker mcp` is installed, ensures `djinn-network`
exists, and starts the compose service in `mcp/docker-compose.yml`.

### 2. Enable MCP servers

```bash
# Web search
mcpgateway enable duckduckgo

# Persistent memory
mcpgateway enable memory

# HTTP requests
mcpgateway enable fetch

# Check enabled servers
mcpgateway servers
```

The wrapper calls `docker mcp server enable <server>` and
`docker mcp server ls`.

### 3. Register the gateway for Djinn agents

The global MCP registry is read from `config/mcp-servers.json` inside the repo
and mounted into the container as `/home/dev/.config/mcp-servers.json`.
Because `config/` is gitignored, this file is local to your machine.

To opt in to the Docker MCP Gateway, add a local entry like this:

```json
{
  "docker-gateway": {
    "transport": "streamable-http",
    "url": "http://mcp-gateway:8811/mcp",
    "enabled": true
  }
}
```

On container startup, `scripts/mcp-register.sh` reads the registry and registers
enabled servers for Claude Code, Gemini CLI, Codex, and OpenCode when the
corresponding CLI is available. An empty registry is valid and results in no MCP
registration.

Legacy local entries that use `type` instead of `transport` are tolerated during
startup, but the script prints a migration warning. Use `transport` plus
`enabled` for new entries.

### 4. Start Djinn

```bash
djinn start

# Inside the container, start your agent of choice.
claude
codex
```

## Commands

| Command | Description |
|---------|-------------|
| `mcpgateway start` | Start the gateway. |
| `mcpgateway stop` | Stop the gateway. |
| `mcpgateway restart` | Restart the gateway. |
| `mcpgateway status` | Show gateway status, enabled servers, and running MCP containers. |
| `mcpgateway logs` | Show gateway logs. |
| `mcpgateway logs --follow` | Follow gateway logs. |
| `mcpgateway enable <server>` | Enable an MCP server through the Docker MCP plugin. |
| `mcpgateway disable <server>` | Disable an MCP server through the Docker MCP plugin. |
| `mcpgateway servers` | List enabled MCP servers. |
| `mcpgateway catalog` | Show the Docker MCP catalog. |
| `mcpgateway test` | Test gateway connectivity, endpoints, socket access, and plugin availability. |
| `mcpgateway clean` | Stop the gateway, remove the MCP config directory, and remove the Djinn network. |

## Available MCP Servers

The available server set comes from the Docker MCP Catalog and can change over
time. Inspect the current catalog with:

```bash
mcpgateway catalog
```

If the local catalog is not initialized, run:

```bash
docker mcp catalog init
docker mcp catalog show docker-mcp
```

You can also browse the catalog online:
https://hub.docker.com/search?q=mcp%2F

## Workflow

```bash
# One-time setup
mcpgateway start
mcpgateway enable duckduckgo
mcpgateway enable memory

# Daily work
djinn start
claude
```

MCP is optional. If you do not need MCP servers, leave
`config/mcp-servers.json` as `{}` and skip `mcpgateway start`.

## Troubleshooting

### Agent does not see MCP servers

```bash
# 1. Is the gateway running?
mcpgateway status

# 2. Does the network exist?
docker network ls | grep djinn

# 3. Does the gateway respond?
mcpgateway test

# 4. Check generated agent configs inside the container.
cat ~/.claude/claude.json | jq .mcpServers
grep -A5 'mcp_servers' ~/.codex/config.toml
cat ~/.gemini/settings.json | jq .mcpServers
cat ~/.config/opencode/.opencode.json | jq .mcpServers
```

### `docker mcp` CLI not found

```bash
# Is the plugin installed?
ls ~/.docker/cli-plugins/

# Rebuild the plugin in a temporary container.
docker run --rm -v ~/.docker/cli-plugins:/output golang:alpine sh -c "
    apk add --no-cache git make
    mkdir -p /root/.docker/cli-plugins
    git clone https://github.com/docker/mcp-gateway.git /build
    cd /build
    make docker-mcp
    cp /root/.docker/cli-plugins/docker-mcp /output/
    chown $(id -u):$(id -g) /output/docker-mcp
"
```

### Gateway does not start

```bash
# Show logs
docker logs mcp-gateway

# Check Docker socket permissions on the host
ls -la /var/run/docker.sock
```

The gateway requires access to the host Docker socket. If the socket is missing
or inaccessible, start Docker or fix host-side Docker permissions before
retrying.

## Technical Details

- **Gateway image:** `docker/mcp-gateway:latest`
- **Transport:** streaming HTTP
- **Port:** `8811`
- **Host endpoint:** `http://localhost:8811`
- **Container endpoint:** `http://mcp-gateway:8811`
- **MCP endpoint from containers:** `http://mcp-gateway:8811/mcp`
- **Network:** `djinn-network`
- **Gateway config mount:** `~/.docker/mcp` on the host to `/root/.docker/mcp`
  in the gateway container
- **Djinn registry mount:** repo-local `config/mcp-servers.json` to
  `/home/dev/.config/mcp-servers.json` in the development container
