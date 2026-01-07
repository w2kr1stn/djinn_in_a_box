# AI Dev Base

Minimales, wiederverwendbares Base-Image für AI-gestützte Entwicklung mit **Claude Code** und **OpenAI Codex CLI**.

## Features

| Tool | Zweck |
|------|-------|
| **fnm** | Fast Node Manager – Node.js Versionen pro Projekt |
| **uv** | Ultraschneller Python Package Manager |
| **Claude Code** | Anthropic's AI Coding Agent |
| **Gemini CLI** | Google's AI Coding Agent |
| **Codex CLI** | OpenAI's AI Coding Agent |
| **Oh My Zsh** | ZSH Framework mit Custom-Plugins |
| **Oh My Posh** | Prompt-Theming mit Custom-Theme |

## Schnellstart

### 1. Base-Image bauen

```bash
cd ai-dev-base
./dev.sh build
```

### 2. MCP Gateway starten (optional, aber empfohlen)

```bash
cd mcp
./mcp.sh start
./mcp.sh enable duckduckgo   # Web-Suche
./mcp.sh enable memory       # Persistenter Speicher
cd ..
```

### 3. Tools authentifizieren (einmalig)

```bash
# Startet Container mit Host-Netzwerk für OAuth
./dev.sh auth

# Im Container:
claude   # Folge dem OAuth-Flow im Browser
codex    # Folge dem OAuth-Flow im Browser
gemini auth  # Authentifizierung für Google Generative AI
exit
```

### 4. Normal arbeiten (täglich)

```bash
./dev.sh start

# Im Container:
claude   # Mit MCP Tools verfügbar
codex
```

Die Credentials werden in persistenten Docker-Volumes gespeichert und überleben Container-Neustarts.

> **Hinweis:** `./dev.sh auth` nutzt `network_mode: host` für OAuth-Callbacks. Nach der einmaligen Authentifizierung verwendet `./dev.sh start` ein dediziertes Docker-Netzwerk.

---

## dev.sh Befehle

Das `dev.sh` Script vereinfacht die Container-Verwaltung:

| Befehl | Beschreibung |
|--------|--------------|
| `./dev.sh build` | Baut das Base-Image |
| `./dev.sh start` | Startet Container (dediziertes Netzwerk) |
| `./dev.sh auth` | Startet Container für OAuth (Host-Netzwerk) |
| `./dev.sh status` | Zeigt Container, Volumes und MCP-Status |
| `./dev.sh clean` | Entfernt Container |
| `./dev.sh clean --all` | Entfernt Container, Volumes **und** Netzwerk |
| `./dev.sh help` | Zeigt Hilfe |

**Beispiele:**

```bash
# Einmalig: Authentifizierung
./dev.sh auth

# Täglicher Workflow
./dev.sh start

# Nach Dockerfile-Änderungen
./dev.sh build

# Kompletter Reset (löscht auch Auth-Credentials!)
./dev.sh clean --all
./dev.sh build
```

---

## Shell-Konfiguration

Lokale ZSH-Konfiguration wird automatisch in den Container übernommen:

| Host-Datei | Im Container | Zweck |
|------------|--------------|-------|
| `~/.zshrc` | `~/.zshrc.local` | Lokale Aliases, Funktionen, etc. |
| `~/.oh-my-zsh/custom/themes/.zsh-theme-remote.omp.json` | `~/.zsh-theme.omp.json` | Oh My Posh Theme (Remote-Variante) |
| `~/.oh-my-zsh/custom/` | `~/.oh-my-zsh/custom/` | Plugins (zsh-autosuggestions, etc.) & Themes |
| `~/.gitconfig` | `~/.gitconfig` | Git User & Einstellungen |

Der Container hat eine eigene `~/.zshrc`, die:
1. Die essentiellen PATH-Einträge setzt (fnm, uv, oh-my-posh)
2. Oh My Zsh lädt mit `plugins=(git zsh-autosuggestions)`
3. Remote Oh My Posh Theme lädt
4. `~/.zshrc.local` sourced (lokale Host-Config)

**Hinweis:** Falls eine der Dateien/Verzeichnisse auf dem Host nicht existiert, kann man den entsprechenden Mount in `docker-compose.yml` auskommentieren.

---

## Projekt-Setup mit DevContainer

### Option A: Minimales Setup (empfohlen)

```bash
# Im Projekt:
mkdir -p .devcontainer
cp /pfad/zu/ai-dev-base/templates/devcontainer.json .devcontainer/
```

Dann in VS Code: `Cmd+Shift+P` → "Reopen in Container"

### Option B: Mit projekt-spezifischen Erweiterungen

```jsonc
// .devcontainer/devcontainer.json
{
  "name": "Mein Projekt",
  "image": "ai-dev-base:latest",
  
  // Network mode für OAuth (wenn schon authentifiziert --> unwichtig)
  "runArgs": ["--network=host"],
  
  // Basis-Volumes (credentials)
  "mounts": [
    "source=ai-dev-claude-config,target=/home/dev/.claude,type=volume",
    "source=ai-dev-codex-config,target=/home/dev/.codex,type=volume",
    "source=ai-dev-uv-cache,target=/home/dev/.cache/uv,type=volume",
    "source=ai-dev-fnm-versions,target=/home/dev/.local/share/fnm/node-versions,type=volume"
  ],
  
  // Projekt Konfiguration, Projekt braucht z.B. Python
  "features": {
    "ghcr.io/devcontainers/features/python:1": {}
  },
  
  // Nach Container-Start
  "postCreateCommand": "uv sync && fnm use"
}
```

---

## Workflow

### Python-Projekt

```bash
# Neues Projekt
uv init mein-projekt
cd mein-projekt

# Dependencies
uv add requests pandas

# Script ausführen
uv run main.py

# Oder: venv aktivieren
source .venv/bin/activate
python main.py
```

### Node-Projekt

```bash
# .node-version anlegen (optional)
echo "22" > .node-version

# fnm installiert/wechselt automatisch
cd mein-projekt   # Automatischer Switch dank --use-on-cd

# Oder manuell
fnm install 20
fnm use 20
```

### AI Assistants

```bash
# Claude Code starten
claude

# Codex CLI starten  
codex
```

---

## Persistente Daten

Alle Tool-Credentials und Caches werden in Docker-Volumes gespeichert – nicht auf dem Host-System:

| Volume | Inhalt |
|--------|--------|
| `ai-dev-claude-config` | Claude Code Credentials & Settings |
| `ai-dev-gemini-config` | Gemini CLI Credentials & Settings |
| `ai-dev-codex-config` | Codex CLI Credentials & Settings |
| `ai-dev-uv-cache` | Python Package Cache |
| `ai-dev-fnm-versions` | Node.js Installationen |

**Vorteil:** Lokales Home-Verzeichnis bleibt sauber. Die Credentials existieren nur im Docker-Kontext.

### Volumes verwalten

```bash
# Alle Volumes anzeigen
docker volume ls | grep ai-dev

# Ein Volume löschen (z.B. zum Reset)
docker volume rm ai-dev-claude-config

# Alle löschen
docker volume rm $(docker volume ls -q | grep ai-dev)
```

---

## Lokales Setup (außerhalb Container)

Falls fnm und uv auch auf dem Host genutzt werden soll:

### fnm installieren

```bash
# macOS
brew install fnm

# Linux
curl -fsSL https://fnm.vercel.app/install | bash
```

Zur Shell-Config hinzufügen:

```bash
# ~/.zshrc oder ~/.bashrc
eval "$(fnm env --use-on-cd)"
```

### uv installieren

```bash
# macOS
brew install uv

# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Sicherheit (optional)

Für erhöhte Sicherheit mit `--dangerously-skip-permissions`:

```bash
# Container mit Firewall starten
docker run --cap-add=NET_ADMIN -v $(pwd)/scripts:/scripts ai-dev-base:latest \
    -c "sudo /scripts/init-firewall.sh && zsh"
```

Das Firewall-Skript beschränkt ausgehende Verbindungen auf eine Whitelist.

---

## Troubleshooting

### "fnm: command not found"

```bash
# Shell neu laden
source ~/.zshrc
```

### Python-Pakete mit native Extensions funktionieren nicht

Dieses Image verwendet Debian (glibc), nicht Alpine (musl). Die meisten Pakete sollten funktionieren. Falls nicht:

```bash
# Im Dockerfile zusätzliche Build-Dependencies
apt-get install -y libffi-dev libssl-dev
```

### Credentials verloren nach Container-Neustart

Sicherstellen, dass die Volumes gemountet sind:

```bash
docker volume ls | grep ai-dev
```

---

## MCP Gateway Integration (Optional)

Das Setup enthält einen optionalen [Docker MCP Gateway](./mcp/README.md), der MCP Server aus dem Docker MCP Catalog für alle AI Coding Agents bereitstellt.

### Voraussetzung: docker mcp CLI Plugin

```bash
# Auf dem Host installieren
git clone https://github.com/docker/mcp-gateway.git
cd mcp-gateway
make docker-mcp
```

### Schnellstart MCP

```bash
# 1. Gateway starten
cd mcp
./mcp.sh start

# 2. MCP Server aktivieren (dynamisch!)
./mcp.sh enable duckduckgo    # Web-Suche
./mcp.sh enable memory        # Persistenter Speicher

# 3. AI Dev Container starten
cd ..
./dev.sh start

# 4. Claude Code nutzt jetzt automatisch den Gateway
claude
# Frage: "Suche im Web nach Docker best practices"
```

### Sicherheits-Features

| Feature | Beschreibung |
|---------|--------------|
| `--verify-signatures` | Nur signierte MCP Images erlaubt |
| `--log-calls` | Audit-Log aller Tool-Aufrufe |
| `--block-secrets` | Verhindert Secret-Leaks in Responses |
| Isoliertes Netzwerk | Eigenes Bridge-Network für AI-Tools |
| Lokale Bindung | Port nur auf 127.0.0.1 exponiert |

Vollständige Dokumentation: [mcp/README.md](./mcp/README.md)

---

## Anpassungen

### Weitere globale Node-Pakete

```dockerfile
# Am Ende des Dockerfile hinzufügen:
RUN npm install -g typescript eslint
```

### Weitere Python-Tools

```dockerfile
RUN uv tool install ruff black mypy
```

### Andere Shell

```dockerfile
# bash statt zsh
RUN apt-get install -y bash
ENV SHELL=/bin/bash
ENTRYPOINT ["/bin/bash"]
```
