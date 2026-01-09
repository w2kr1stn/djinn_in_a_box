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

### 1. Konfiguration erstellen

```bash
cd ai-dev-base

# Template kopieren und anpassen
cp .env.example .env

# Mindestens CODE_DIR setzen:
# CODE_DIR=/pfad/zu/deinen/projekten
nano .env
```

### 2. Base-Image bauen

```bash
./scripts/dev.sh build
```

### 3. MCP Gateway starten (optional, aber empfohlen)

```bash
cd mcp
./mcp.sh start
./mcp.sh enable duckduckgo   # Web-Suche
./mcp.sh enable memory       # Persistenter Speicher
cd ..
```

### 4. Tools authentifizieren (einmalig)

```bash
# Startet Container mit Host-Netzwerk für OAuth
./scripts/dev.sh auth

# Im Container:
claude   # Folge dem OAuth-Flow im Browser
codex    # Folge dem OAuth-Flow im Browser
gemini auth  # Authentifizierung für Google Generative AI
exit
```

### 5. Normal arbeiten (täglich)

```bash
./scripts/dev.sh start

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
| `./dev.sh audit` | Zeigt Docker Proxy Audit-Log |
| `./dev.sh clean` | Entfernt Container |
| `./dev.sh clean --all` | Entfernt Container, Volumes **und** Netzwerk |
| `./dev.sh help` | Zeigt Hilfe |

### Optionen für `start`

| Option | Beschreibung |
|--------|--------------|
| `--docker` | Docker-Zugriff via sicherem Proxy aktivieren |
| `--firewall` | Netzwerk-Firewall aktivieren (Egress-Whitelist) |
| `--here` | Aktuelles Verzeichnis zusätzlich unter `~/workspace/` mounten |
| `--mount <path>` | Beliebigen Pfad zusätzlich unter `~/workspace/` mounten |

**Beispiele:**

```bash
# Einmalig: Authentifizierung
./dev.sh auth

# Täglicher Workflow
./dev.sh start

# Mit Docker-Zugriff
./dev.sh start --docker

# Mit maximaler Sicherheit
./dev.sh start --docker --firewall

# Nach Dockerfile-Änderungen
./dev.sh build

# Kompletter Reset (löscht auch Auth-Credentials!)
./dev.sh clean --all
./dev.sh build
```

---

## Workspace Mount (Temporäres Verzeichnis)

Neben dem festen Projekt-Verzeichnis (`~/projects/`) kann ein zusätzliches Verzeichnis temporär gemountet werden – ideal um mit AI-Agents am lokalen System zu arbeiten.

### Verzeichnisstruktur im Container

| Pfad | Quelle | Persistenz |
|------|--------|------------|
| `~/projects/` | Festes Code-Verzeichnis (docker-compose.yml) | Immer |
| `~/workspace/` | Via `--here` oder `--mount` | Nur diese Session |

### Verwendung

```bash
# Aktuelles Verzeichnis mounten
cd ~/.config
./dev.sh start --here

# Beliebigen Pfad mounten
./dev.sh start --mount ~/dotfiles

# Kombiniert mit anderen Optionen
./dev.sh start --docker --firewall --here
./dev.sh start --docker --mount /etc/nginx
```

### Im Container

```bash
# Festes Arbeitsverzeichnis (immer verfügbar)
cd ~/projects/
claude "Arbeite an meinem Projekt"

# Temporärer Workspace (nur mit --here/--mount)
cd ~/workspace/
claude "Optimiere diese Config-Dateien"
```

### ⚠️ Wichtig: Bind-Mount Verhalten

Der Workspace ist ein **Bind-Mount** – Änderungen sind **sofort** auf der lokalen Platte:

```bash
# Im Container:
rm ~/workspace/wichtig.conf  # → Datei ist SOFORT weg auf dem Host!
```

**Empfehlung für kritische Verzeichnisse:**

```bash
# Vorher Backup machen
cp -r ~/.config ~/.config.bak

# Dann mounten
./dev.sh start --mount ~/.config --docker
```

### Workflow-Beispiele

```bash
# Dotfiles mit AI bearbeiten
cd ~/dotfiles
./dev.sh start --here --docker
# Im Container: claude "Refactore meine zsh config"

# System-Configs analysieren (read-only empfohlen)
./dev.sh start --mount /etc --docker
# Im Container: claude "Erkläre mir die nginx config"

# An anderem Projekt arbeiten ohne docker-compose zu ändern
./dev.sh start --mount ~/anderes-projekt --docker
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

## Sicherheit

### Security Modi

| Modus | Command | Docker | Firewall | Empfohlen für |
|-------|---------|--------|----------|---------------|
| **Standard** | `./dev.sh start` | ❌ | ❌ | Normale Entwicklung |
| **Docker** | `./dev.sh start --docker` | ✅ (Proxy) | ❌ | Container-Entwicklung |
| **Firewall** | `./dev.sh start --firewall` | ❌ | ✅ | Sensitive Projekte |
| **Maximum** | `./dev.sh start --docker --firewall` | ✅ | ✅ | **Allgemein empfohlen** |

### Docker Socket Proxy

Wenn `--docker` aktiviert ist, läuft ein sicherer Proxy der gefährliche Operationen blockiert:

| Erlaubt | Blockiert |
|---------|-----------|
| `docker ps`, `images`, `networks` | `docker exec` |
| `docker run`, `start`, `stop` | `docker build` |
| `docker pull` | `docker commit`, `push` |
| | `swarm`, `secrets`, `configs` |

Details: [DOCKER-SOCKET-SECURITY.md](./DOCKER-SOCKET-SECURITY.md)

### Firewall

Mit `--firewall` werden ausgehende Verbindungen auf eine Whitelist beschränkt:

- Package Registries (npm, PyPI)
- AI APIs (Anthropic, OpenAI)
- Code Hosting (GitHub)
- OAuth Endpoints

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

### Workspace Mount funktioniert nicht

```bash
# Pfad existiert?
ls -la /pfad/zum/verzeichnis

# Berechtigungen prüfen
# Der Container läuft als User 'dev' (UID 1000)
```

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

### Festes Projekt-Verzeichnis ändern

In `.env` anpassen:

```bash
CODE_DIR=/dein/pfad/zu/projekten
```

### Shell-Mounts deaktivieren

Falls du keine oh-my-zsh oder oh-my-posh Installation auf dem Host hast:

```bash
# In .env setzen:
SKIP_SHELL_MOUNTS=true
```

### Resource-Limits anpassen

```bash
# In .env setzen:
CPU_LIMIT=4
MEMORY_LIMIT=8G
CPU_RESERVATION=1
MEMORY_RESERVATION=2G
```