# MCP Gateway fuer Djinn Container

Dieses Setup stellt einen Docker MCP Gateway bereit, der MCP Server aus dem [Docker MCP Catalog](https://hub.docker.com/search?q=mcp%2F) fuer alle CLI Coding Agents verfuegbar macht.

## Voraussetzungen

### Docker MCP CLI Plugin (auf dem Host installieren)

```bash
# Option 1: Selbst bauen
git clone https://github.com/docker/mcp-gateway.git
cd mcp-gateway
make docker-mcp

# Option 2: Selbst bauen (no foot prints)
docker run --rm -v ~/.docker/cli-plugins:/output golang:alpine sh -c "
    # 1. Setup
    apk add --no-cache git make

    # 2. Ordner faken, damit das Makefile nicht abstuerzt
    mkdir -p /root/.docker/cli-plugins

    # 3. Klonen
    git clone https://github.com/docker/mcp-gateway.git /build
    cd /build

    # 4. Bauen (jetzt laeuft es durch, weil der Zielordner existiert)
    make docker-mcp

    # 5. Das fertige Ergebnis auf deinen Host kopieren
    # (Das Makefile hat es bereits nach /root/.docker/cli-plugins gelegt)
    cp /root/.docker/cli-plugins/docker-mcp /output/

    # Rechte fuer den User anpassen (da Container root war)
    chown $(id -u):$(id -g) /output/docker-mcp
"

# Option 3: Binary von GitHub Releases
# https://github.com/docker/mcp-gateway/releases
# -> Nach ~/.docker/cli-plugins/docker-mcp kopieren

# Verifizieren
docker mcp --help
```

## Architektur

```
┌─────────────────────────────────────────────────────────────────┐
│                    djinn-network (Bridge)                      │
│                                                                 │
│  ┌─────────────────┐           ┌─────────────────────────────┐  │
│  │  MCP Gateway    │           │    Djinn Container         │  │
│  │  (mcp-gateway)  │◄─────────►│  (djinn)                   │  │
│  │                 │   SSE     │                             │  │
│  │  :8811          │           │  Claude Code / Codex        │  │
│  └────────┬────────┘           └─────────────────────────────┘  │
│           │                                                     │
│           │ spawnt                                              │
│           ▼                                                     │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  MCP Server Container (isoliert, signiert, limitiert)     │  │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐          │  │
│  │  │DuckDuck │ │ Memory  │ │  Fetch  │ │  Time   │  ...     │  │
│  │  │  Go     │ │         │ │         │ │         │          │  │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘          │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Sicherheits-Features

| Feature | Beschreibung |
|---------|--------------|
| `--verify-signatures` | Nur kryptografisch signierte MCP Images |
| `--log-calls` | Audit-Log aller Tool-Aufrufe |
| `--block-secrets` | Verhindert Secrets in Responses |
| Resource Limits | CPU/Memory-Limits für Gateway |
| Lokale Bindung | Port nur auf 127.0.0.1 exponiert |
| Isoliertes Netzwerk | Dediziertes Bridge-Network |

## Schnellstart

### 1. MCP Gateway starten

```bash
mcpgateway start
```

### 2. MCP Server aktivieren

```bash
# Web-Suche
mcpgateway enable duckduckgo

# Persistenter Speicher
mcpgateway enable memory

# HTTP Requests
mcpgateway enable fetch

# Status pruefen
mcpgateway servers
```

### 3. Djinn Container starten

```bash
djinn start

# Im Container: Claude Code nutzt automatisch den Gateway
claude
```

## Befehle

| Befehl | Beschreibung |
|--------|--------------|
| `mcpgateway start` | Gateway starten |
| `mcpgateway stop` | Gateway stoppen |
| `mcpgateway status` | Status und aktivierte Server anzeigen |
| `mcpgateway logs` | Gateway-Logs anzeigen |
| `mcpgateway enable <server>` | MCP Server aktivieren |
| `mcpgateway disable <server>` | MCP Server deaktivieren |
| `mcpgateway servers` | Aktivierte Server auflisten |
| `mcpgateway catalog` | Verfuegbare Server anzeigen |
| `mcpgateway test` | Konnektivitaet testen |
| `mcpgateway clean` | Alles zuruecksetzen |

## Verfügbare MCP Server

| Server | Beschreibung |
|--------|--------------|
| `duckduckgo` | Web-Suche |
| `memory` | Persistenter Speicher |
| `fetch` | HTTP Requests |
| `time` | Datum/Uhrzeit |
| `filesystem` | Dateizugriff |
| `sqlite` | SQLite Datenbank |
| `brave-search` | Brave Suche (API Key) |
| `github-official` | GitHub API (OAuth) |

Vollständiger Katalog: https://hub.docker.com/search?q=mcp%2F

### Katalog initialisieren (falls nötig)

```bash
docker mcp catalog init
docker mcp catalog show docker-mcp
```

## Workflow

```bash
# Einmalig: Setup
mcpgateway start
mcpgateway enable duckduckgo
mcpgateway enable memory

# Einmalig: OAuth-Authentifizierung
djinn auth    # Host-Netzwerk fuer OAuth
# -> Claude Code und Codex authentifizieren
exit

# Taeglich: Normal arbeiten
djinn start   # Dediziertes Netzwerk
claude            # MCP Tools verfuegbar
```

## Fehlerbehebung

### Agent findet MCP nicht

```bash
# 1. Gateway laeuft?
mcpgateway status

# 2. Netzwerk existiert?
docker network ls | grep djinn

# 3. Verbindung testen
mcpgateway test

# 4. Im Container pruefen
# a)
cat ~/.claude/claude.json | jq .mcpServers
# b)
grep -A5 'mcp_servers' ~/.codex/config.toml
```

### docker mcp CLI nicht gefunden

```bash
# Plugin installiert?
ls ~/.docker/cli-plugins/

# Falls nicht: Neu installieren
docker run --rm -v ~/.docker/cli-plugins:/output golang:alpine sh -c "
    # 1. Setup
    apk add --no-cache git make

    # 2. Ordner faken, damit das Makefile nicht abstürzt
    mkdir -p /root/.docker/cli-plugins

    # 3. Klonen
    git clone https://github.com/docker/mcp-gateway.git /build
    cd /build

    # 4. Bauen (jetzt läuft es durch, weil der Zielordner existiert)
    make docker-mcp

    # 5. Das fertige Ergebnis auf deinen Host kopieren
    # (Das Makefile hat es bereits nach /root/.docker/cli-plugins gelegt)
    cp /root/.docker/cli-plugins/docker-mcp /output/
    
    # Rechte für den User anpassen (da Container root war)
    chown $(id -u):$(id -g) /output/docker-mcp
"
```

### Gateway startet nicht

```bash
# Logs prüfen
docker logs mcp-gateway

# Docker Socket Berechtigungen
ls -la /var/run/docker.sock
# → Sollte für deinen User/Gruppe lesbar sein
```

## Technische Details

- **Gateway Image**: `docker/mcp-gateway:latest`
- **Transport**: Streaming HTTP (kompatibel mit Claude Code & Codex)
- **Port**: 8811 (nur localhost)
- **Netzwerk**: `djinn-network` (Bridge)
- **Config**: `~/.docker/mcp` (Host) → `/root/.docker/mcp` (Container)
- **Endpoint (Container)**: `http://mcp-gateway:8811`
- **Endpoint (Host)**: `http://localhost:8811`

---

## Systematische Ueberpruefung

### 1. Gateway-Status pruefen

```bash
# Vom Host aus
mcpgateway status
mcpgateway test

# Zeigt aktivierte Server
docker mcp server ls
```

### 2. Claude Code Konfiguration prüfen

```bash
# Im Djinn Container
cat ~/.claude/claude.json | jq .mcpServers

# Erwartete Ausgabe:
# {
#   "docker-gateway": {
#     "type": "http",
#     "url": "http://mcp-gateway:8811/sse"
#   }
# }

# Alternativ: Claude Code CLI
claude mcp list
```

### 3. Codex CLI Konfiguration prüfen

```bash
# Im Djinn Container
cat ~/.codex/config.toml

# Erwartete Ausgabe (relevant):
# [features]
# rmcp_client = true
#
# [mcp_servers.docker-gateway]
# url = "http://mcp-gateway:8811"

# Alternativ: Codex CLI
codex mcp list
```

### 4. MCP Tools in Claude Code testen

```bash
# Im Container
claude

# Dann im Chat:
# > Welche MCP Tools hast du verfügbar?
# > Suche im Web nach "Docker MCP Gateway"
```

### 5. MCP Tools in Codex testen

```bash
# Im Container
codex

# Dann im Chat:
# > /mcp list
# > Suche im Web nach aktuellen Docker News
```

### 6. Gateway-Logs pruefen

```bash
# Vom Host aus
mcpgateway logs

# Zeigt Tool-Aufrufe (--log-calls ist aktiv)
# Beispiel: "Tool called: duckduckgo.search query='Docker MCP'"
```

### 7. Netzwerk-Konnektivität prüfen

```bash
# Im Djinn Container
curl -s http://mcp-gateway:8811/ | head -20

# Sollte JSON oder HTTP-Response zeigen
```

### Checkliste

| Pruefpunkt | Befehl | Erwartung |
|------------|--------|-----------|
| Gateway laeuft | `mcpgateway status` | "Running" |
| Server aktiviert | `docker mcp server ls` | Liste mit Servern |
| Claude Config | `cat ~/.claude/claude.json` | `mcpServers.docker-gateway` |
| Codex Config | `cat ~/.codex/config.toml` | `[mcp_servers.docker-gateway]` |
| Netzwerk | `curl mcp-gateway:8811` | HTTP Response |
| Claude Tools | `claude mcp list` | Gateway gelistet |
| Codex Tools | `codex mcp list` | Gateway gelistet |
