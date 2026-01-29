# Docker Socket Security Analysis

## Sicherheitsanalyse: Docker Socket Zugriff für AI Dev Container

Diese Analyse bewertet die Sicherheitsimplikationen des Docker-Zugriffs im Kontext eines Entwicklungscontainers für AI Coding Agents (Claude Code, Gemini CLI, Codex).

---

## Implementierte Sicherheitsarchitektur

```
┌─────────────────────────────────────────────────────────────────┐
│                         HOST SYSTEM                              │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                      Docker Daemon                          │ │
│  │                                                             │ │
│  │  docker.sock ◄────────────────────────────────────────┐    │ │
│  │       │                                                │    │ │
│  │       ▼                                                │    │ │
│  │  ┌─────────────────────────────────────────────────┐  │    │ │
│  │  │              ai-dev-network                      │  │    │ │
│  │  │                                                  │  │    │ │
│  │  │  ┌──────────────┐      ┌──────────────────┐    │  │    │ │
│  │  │  │ docker-proxy │◄─────│     ai-dev       │    │  │    │ │
│  │  │  │              │      │   (Dev Container) │    │  │    │ │
│  │  │  │ FILTERS:     │      │                  │    │  │    │ │
│  │  │  │ ✓ run/start  │      │ Claude Code      │    │  │    │ │
│  │  │  │ ✓ ps/images  │      │ Gemini CLI       │    │  │    │ │
│  │  │  │ ✗ exec       │      │ Codex CLI        │    │  │    │ │
│  │  │  │ ✗ build      │      └──────────────────┘    │  │    │ │
│  │  │  │ ✗ commit     │                              │  │    │ │
│  │  │  └──────┬───────┘      ┌──────────────────┐    │  │    │ │
│  │  │         │              │   mcp-gateway    │    │  │    │ │
│  │  │         │              └──────────────────┘    │  │    │ │
│  │  │         │                                      │  │    │ │
│  │  │         ▼                                      │  │    │ │
│  │  │  ┌──────────────┐                              │  │    │ │
│  │  │  │   Spawned    │ (von AI erstellt)            │  │    │ │
│  │  │  │  Containers  │                              │  │    │ │
│  │  │  └──────────────┘                              │  │    │ │
│  │  └─────────────────────────────────────────────────┘  │    │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘

Legende:
  ─────►  Erlaubter Datenfluss
  ✓       Erlaubte Operation
  ✗       Blockierte Operation
```

---

## Toggle-Mechanismus

### Verfuegbare Modi

| Modus | Command | Docker | Firewall | Sicherheit |
|-------|---------|--------|----------|------------|
| **Standard** | `codeagent start` | - | - | Hoch |
| **Docker** | `codeagent start --docker` | Proxy | - | Mittel |
| **Firewall** | `codeagent start --firewall` | - | Ja | Hoch |
| **Maximum** | `codeagent start --docker --firewall` | Proxy | Ja | **Empfohlen** |

### Warum ein Toggle?

1. **Principle of Least Privilege**: Docker-Zugriff nur wenn benoetigt
2. **Explizite Aktivierung**: Bewusste Entscheidung bei jedem Start
3. **Audit Trail**: `codeagent status` zeigt aktuelle Konfiguration
4. **Flexibilitaet**: Verschiedene Sicherheitslevel fuer verschiedene Tasks

---

## Docker Socket Proxy: Technische Details

### Was ist der Proxy?

Der [Tecnativa Docker Socket Proxy](https://github.com/Tecnativa/docker-socket-proxy) ist ein HTTP-Proxy, der zwischen dem Dev-Container und dem Docker Socket sitzt. Er filtert API-Aufrufe basierend auf Whitelists.

### Erlaubte Operationen

```yaml
# Lese-Operationen (sicher)
CONTAINERS=1    # docker ps, inspect
IMAGES=1        # docker images, inspect
NETWORKS=1      # docker network ls
VOLUMES=1       # docker volume ls
INFO=1          # docker info
VERSION=1       # docker version

# Schreib-Operationen (kontrolliert)
POST=1          # Erforderlich für run/start/stop
ALLOW_START=1   # docker start
ALLOW_STOP=1    # docker stop
ALLOW_RESTARTS=1 # docker restart
```

### Blockierte Operationen

```yaml
# Sicherheitskritisch - IMMER blockiert
BUILD=0         # Kein Image Build (könnte Secrets leaken)
COMMIT=0        # Kein Container-zu-Image (Persistence-Angriff)
EXEC=0          # Kein Exec in Container (Escape-Vektor)
SWARM=0         # Kein Swarm (Cluster-Kompromittierung)
SECRETS=0       # Kein Secrets-Zugriff
CONFIGS=0       # Kein Configs-Zugriff
AUTH=0          # Keine Registry-Auth (Credential-Leak)
```

### Warum diese Einschränkungen?

| Blockiert | Grund |
|-----------|-------|
| `exec` | Ermöglicht Zugriff auf laufende Container, potentieller Escape |
| `build` | Könnte Host-Dateien in Images einbetten |
| `commit` | Ermöglicht Persistenz von Malware |
| `auth` | Schützt Registry-Credentials |
| `swarm` | Verhindert Cluster-weite Kompromittierung |

---

## Risiko-Analyse: Mit vs. Ohne Proxy

### Direkter Socket-Mount (NICHT implementiert)

```yaml
# GEFÄHRLICH - Nicht verwenden!
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

**Risiken:**
- Voller API-Zugriff
- Host-Root via `docker run -v /:/host`
- Keine Audit-Möglichkeit
- Keine Einschränkungen

### Proxy-basierter Zugriff (Implementiert)

```yaml
# SICHER - Unsere Implementierung
environment:
  - DOCKER_HOST=tcp://docker-proxy:2375
```

**Vorteile:**
- API-Filterung
- Audit-Logging
- Keine direkten Socket-Zugriff
- Gefährliche Ops blockiert

---

## Verbleibende Risiken

Auch mit Proxy bleiben einige Risiken:

### 1. Privilegierte Container

**Risiko:** AI könnte privilegierte Container starten:
```bash
docker run --privileged -v /:/host alpine
```

**Mitigation:**
- Resource Limits im Compose
- Monitoring via `codeagent audit`
- Firewall fuer Netzwerk-Exfiltration

### 2. Resource Exhaustion

**Risiko:** Unkontrolliertes Container-Spawning

**Mitigation:**
```yaml
deploy:
  resources:
    limits:
      cpus: '6'
      memory: 12G
```

### 3. Network-based Attacks

**Risiko:** Container im gleichen Netzwerk könnten kommunizieren

**Mitigation:**
- `--firewall` Flag für strikte Egress-Kontrolle
- Isoliertes `ai-dev-network`

---

## Sicherheits-Checkliste

### Taeglicher Gebrauch

```markdown
## Vor der Session
- [ ] Brauche ich Docker-Zugriff? Wenn nein: `codeagent start`
- [ ] Wenn ja: `codeagent start --docker --firewall`

## Nach der Session
- [ ] `codeagent status` - Unbekannte Container?
- [ ] `codeagent audit` - Verdaechtige Requests?
```

### Wöchentlich

```markdown
- [ ] `docker ps -a` - Alle Container bekannt?
- [ ] `docker images` - Unbekannte Images?
- [ ] `docker volume ls` - Unbekannte Volumes?
```

### Bei Verdacht

```bash
# Alle ai-dev Container stoppen
docker stop $(docker ps -q --filter "network=ai-dev-network")

# Proxy-Logs prüfen
docker logs ai-dev-docker-proxy 2>&1 | grep -i "blocked\|error\|denied"

# Netzwerk-Aktivität prüfen
docker network inspect ai-dev-network
```

---

## Audit-Logging

### Proxy-Logs anzeigen

```bash
# Via CLI
codeagent audit

# Direkt
docker logs -f ai-dev-docker-proxy
```

### Log-Format

```
time="2024-01-15T10:30:45Z" level=info msg="GET /v1.43/containers/json"
time="2024-01-15T10:30:46Z" level=info msg="POST /v1.43/containers/create"
time="2024-01-15T10:30:47Z" level=warning msg="Blocked: POST /v1.43/exec/abc123/start"
```

### Alerts einrichten (optional)

```bash
# Einfaches Monitoring-Script
#!/bin/bash
docker logs -f ai-dev-docker-proxy 2>&1 | while read line; do
    if echo "$line" | grep -qi "blocked"; then
        notify-send "Docker Proxy Alert" "$line"
    fi
done
```

---

## Vergleich: Sicherheitsoptionen

| Feature | Standard | +Docker | +Firewall | +Docker+Firewall |
|---------|----------|---------|-----------|------------------|
| AI kann Container starten | ❌ | ✅ | ❌ | ✅ |
| Gefährliche Docker-Ops | ❌ | ❌ | ❌ | ❌ |
| Netzwerk-Exfiltration | ⚠️ | ⚠️ | ❌ | ❌ |
| Resource Limits | ✅ | ✅ | ✅ | ✅ |
| Audit-Logging | ❌ | ✅ | ❌ | ✅ |
| Empfohlen für | Lesen/Schreiben | Container-Dev | Sensitive Projekte | **Allgemein** |

---

## FAQ

### Kann der AI-Agent trotz Proxy Host-Root bekommen?

**Theoretisch ja**, via:
```bash
docker run -v /:/host alpine cat /host/etc/shadow
```

**Aber:**
1. AI-Agents haben Sicherheitsrichtlinien
2. Du reviewst den Code
3. Firewall verhindert Exfiltration
4. Audit-Logs zeigen den Vorgang

### Warum nicht Docker-in-Docker (DinD)?

| DinD | Proxy |
|------|-------|
| Vollständige Isolation | Shared Daemon |
| `privileged: true` erforderlich | Kein privileged |
| Eigener Image-Cache | Shared Cache |
| Komplexeres Setup | Einfacher |
| Mehr Overhead | Minimal |

Für Entwicklung ist der Proxy die bessere Wahl.

### Kann ich `docker exec` aktivieren?

**Ja, aber nicht empfohlen:**

```yaml
# In docker-compose.docker.yml
environment:
  - EXEC=1  # WARNUNG: Sicherheitsrisiko!
```

Besser: `docker run` mit dem gleichen Image verwenden.

---

## Empfehlung

Fuer dein Szenario (Einzelbenutzer, lokale Entwicklung, AI-Agents):

```bash
# Standard-Workflow
codeagent start --docker --firewall
```

Diese Kombination bietet:
- Docker-Funktionalitaet fuer AI-Agents
- Gefaehrliche Operationen blockiert
- Netzwerk-Isolation
- Audit-Logging
- Resource Limits

Der wichtigste Sicherheitsmechanismus bleibt **du selbst**: Code-Review, Awareness, regelmaessige Audits.
