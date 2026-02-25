# Djinn in a Box

Minimales, wiederverwendbares Base-Image fuer AI-gestuetzte Entwicklung mit **Claude Code**, **Gemini CLI**, **Codex CLI** und **OpenCode**.

## Features

| Tool | Zweck |
|------|-------|
| **fnm** | Fast Node Manager - Node.js Versionen pro Projekt |
| **uv** | Ultraschneller Python Package Manager |
| **Claude Code** | Anthropic's AI Coding Agent |
| **Gemini CLI** | Google's AI Coding Agent |
| **Codex CLI** | OpenAI's AI Coding Agent |
| **OpenCode** | Multi-Provider AI Coding Agent (lokal & Cloud) |
| **Oh My Zsh** | ZSH Framework mit Custom-Plugins |
| **Oh My Posh** | Prompt-Theming mit Custom-Theme |

## Installation

```bash
# Install CLI tools via uv
cd djinn_in_a_box
uv tool install .

# Or install in development mode
uv sync
```

After installation, `djinn` and `mcpgateway` commands are available globally.

## Quick Start

```bash
# 1. Initialize configuration (interactive)
djinn init

# 2. Build the Docker image
djinn build

# 3. Authenticate with AI services (once)
djinn auth
# Im Container: claude, gemini auth, codex, opencode

# 4. Start development shell (daily)
djinn start

# Or start with Docker access
djinn start --docker
```

Die Credentials werden in persistenten Docker-Volumes gespeichert und ueberleben Container-Neustarts.

> **Hinweis:** `djinn auth` nutzt `network_mode: host` fuer OAuth-Callbacks. Nach der einmaligen Authentifizierung verwendet `djinn start` ein dediziertes Docker-Netzwerk.

---

## CLI Commands

### djinn

Container lifecycle and agent execution:

```bash
djinn --help              # Show all commands
djinn --version           # Show version

# Setup
djinn init                # Initialize config interactively
djinn config show         # Show current configuration
djinn config path         # Show config file path

# Container lifecycle
djinn build               # Build Docker image
djinn start [options]     # Start interactive shell
djinn auth                # OAuth authentication
djinn status              # Show system status
djinn enter               # Attach to running container
djinn update              # Update CLI agent versions

# Agent execution (headless)
djinn run claude "prompt" # Run agent non-interactively
djinn run gemini "prompt" --write --model gemini-2.5-pro
djinn agents              # List available agents

# Cleanup
djinn clean               # Remove containers
djinn clean volumes       # List volumes by category
djinn clean volumes --credentials  # Delete auth tokens
djinn clean volumes --all # Remove ALL volumes
```

### Start Options

| Option | Beschreibung |
|--------|--------------|
| `--docker` | Docker-Zugriff via sicherem Proxy aktivieren |
| `--firewall` | Netzwerk-Firewall aktivieren (Egress-Whitelist) |
| `--here` | Aktuelles Verzeichnis zusaetzlich unter `~/workspace/` mounten |
| `--mount <path>` | Beliebigen Pfad zusaetzlich unter `~/workspace/` mounten |

**Beispiele:**

```bash
# Einmalig: Authentifizierung
djinn auth

# Taeglicher Workflow
djinn start

# Mit Docker-Zugriff
djinn start --docker

# Mit maximaler Sicherheit
djinn start --docker --firewall

# Nach Dockerfile-Aenderungen
djinn build

# Kompletter Reset (loescht auch Auth-Credentials!)
djinn clean volumes --all
djinn build
```

### mcpgateway

MCP Gateway management for AI tool integration:

```bash
mcpgateway --help             # Show all commands

# Lifecycle
mcpgateway start              # Start MCP Gateway
mcpgateway stop               # Stop MCP Gateway
mcpgateway restart            # Restart MCP Gateway
mcpgateway status             # Show status

# Server management
mcpgateway enable <server>    # Enable MCP server
mcpgateway disable <server>   # Disable MCP server
mcpgateway servers            # List enabled servers
mcpgateway catalog            # Show available servers

# Diagnostics
mcpgateway test               # Test gateway connectivity
mcpgateway logs               # Show gateway logs
mcpgateway clean              # Full reset
```

---

## Configuration

Configuration is stored in `~/.config/djinn_in_a_box/`:

```
~/.config/djinn_in_a_box/
|-- config.toml      # Main configuration
|-- agents.toml      # Agent definitions (optional)
```

### config.toml

```toml
[general]
code_dir = "/path/to/your/projects"
timezone = "Europe/Berlin"

[resources]
cpu_limit = 6
memory_limit = "12G"
cpu_reservation = 2
memory_reservation = "4G"

[shell]
skip_mounts = false
# omp_theme_path = "/path/to/custom/theme.omp.json"
```

Run `djinn init` for interactive setup or `djinn config show` to view current settings.

---

## Workspace Mount (Temporaeres Verzeichnis)

Neben dem festen Projekt-Verzeichnis (`~/projects/`) kann ein zusaetzliches Verzeichnis temporaer gemountet werden - ideal um mit AI-Agents am lokalen System zu arbeiten.

### Verzeichnisstruktur im Container

| Pfad | Quelle | Persistenz |
|------|--------|------------|
| `~/projects/` | Festes Code-Verzeichnis (config.toml) | Immer |
| `~/workspace/` | Via `--here` oder `--mount` | Nur diese Session |

### Verwendung

```bash
# Aktuelles Verzeichnis mounten
cd ~/.config
djinn start --here

# Beliebigen Pfad mounten
djinn start --mount ~/dotfiles

# Kombiniert mit anderen Optionen
djinn start --docker --firewall --here
djinn start --docker --mount /etc/nginx
```

### Im Container

```bash
# Festes Arbeitsverzeichnis (immer verfuegbar)
cd ~/projects/
claude "Arbeite an meinem Projekt"

# Temporaerer Workspace (nur mit --here/--mount)
cd ~/workspace/
claude "Optimiere diese Config-Dateien"
```

### Wichtig: Bind-Mount Verhalten

Der Workspace ist ein **Bind-Mount** - Aenderungen sind **sofort** auf der lokalen Platte:

```bash
# Im Container:
rm ~/workspace/wichtig.conf  # -> Datei ist SOFORT weg auf dem Host!
```

**Empfehlung fuer kritische Verzeichnisse:**

```bash
# Vorher Backup machen
cp -r ~/.config ~/.config.bak

# Dann mounten
djinn start --mount ~/.config --docker
```

### Workflow-Beispiele

```bash
# Dotfiles mit AI bearbeiten
cd ~/dotfiles
djinn start --here --docker
# Im Container: claude "Refactore meine zsh config"

# System-Configs analysieren (read-only empfohlen)
djinn start --mount /etc --docker
# Im Container: claude "Erklaere mir die nginx config"

# An anderem Projekt arbeiten ohne config.toml zu aendern
djinn start --mount ~/anderes-projekt --docker
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
cp /pfad/zu/djinn_in_a_box/templates/devcontainer.json .devcontainer/
```

Dann in VS Code: `Cmd+Shift+P` → "Reopen in Container"

### Option B: Mit projekt-spezifischen Erweiterungen

```jsonc
// .devcontainer/devcontainer.json
{
  "name": "Mein Projekt",
  "image": "djinn-in-a-box:latest",
  
  // Network mode für OAuth (wenn schon authentifiziert --> unwichtig)
  "runArgs": ["--network=host"],
  
  // Basis-Volumes (credentials)
  "mounts": [
    "source=djinn-claude-config,target=/home/dev/.claude,type=volume",
    "source=djinn-gemini-config,target=/home/dev/.gemini,type=volume",
    "source=djinn-codex-config,target=/home/dev/.codex,type=volume",
    "source=djinn-opencode,target=/home/dev/.opencode,type=volume",
    "source=djinn-uv-cache,target=/home/dev/.cache/uv,type=volume"
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

# Gemini CLI starten
gemini

# Codex CLI starten  
codex

# OpenCode starten (mit lokalem LLM Support)
opencode
```

---

## Persistente Daten

Alle Tool-Credentials und Caches werden in Docker-Volumes gespeichert – nicht auf dem Host-System:

| Volume | Inhalt |
|--------|--------|
| `djinn-claude-config` | Claude Code Credentials & Settings |
| `djinn-gemini-config` | Gemini CLI Credentials & Settings |
| `djinn-codex-config` | Codex CLI Credentials & Settings |
| `djinn-opencode` | OpenCode Credentials & Settings |
| `djinn-uv-cache` | Python Package Cache |
| `djinn-fnm-versions` | Node.js Installationen |

**Vorteil:** Lokales Home-Verzeichnis bleibt sauber. Die Credentials existieren nur im Docker-Kontext.

### Volumes verwalten

```bash
# Alle Volumes anzeigen
docker volume ls | grep djinn

# Ein Volume löschen (z.B. zum Reset)
docker volume rm djinn-claude-config

# Alle löschen
docker volume rm $(docker volume ls -q | grep djinn)
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

| Modus | Command | Docker | Firewall | Empfohlen fuer |
|-------|---------|--------|----------|----------------|
| **Standard** | `djinn start` | - | - | Normale Entwicklung |
| **Docker** | `djinn start --docker` | Proxy | - | Container-Entwicklung |
| **Firewall** | `djinn start --firewall` | - | Ja | Sensitive Projekte |
| **Maximum** | `djinn start --docker --firewall` | Proxy | Ja | **Allgemein empfohlen** |

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

Das Setup enthaelt einen optionalen [Docker MCP Gateway](./mcp/README.md), der MCP Server aus dem Docker MCP Catalog fuer alle AI Coding Agents bereitstellt.

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
mcpgateway start

# 2. MCP Server aktivieren (dynamisch!)
mcpgateway enable duckduckgo    # Web-Suche
mcpgateway enable memory        # Persistenter Speicher

# 3. Djinn Container starten
djinn start

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
| Isoliertes Netzwerk | Eigenes Bridge-Network fuer AI-Tools |
| Lokale Bindung | Port nur auf 127.0.0.1 exponiert |

Vollstaendige Dokumentation: [mcp/README.md](./mcp/README.md)

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
docker volume ls | grep djinn
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

### Custom APT Packages

Zusaetzliche System-Pakete koennen ueber `packages.txt` installiert werden:

```bash
# Template kopieren
cp packages.txt.example packages.txt

# Pakete hinzufuegen (ein Paket pro Zeile)
echo "vim" >> packages.txt
echo "htop" >> packages.txt

# Image neu bauen
djinn build
```

Die `packages.txt` ist in `.gitignore` - jeder Entwickler kann eigene Pakete pflegen.

### Optional CLI Tools (Runtime)

Komplexere Tools (Azure CLI, Pulumi, etc.) werden bei Container-Start installiert:

```bash
# Template kopieren
cp tools/tools.txt.example tools/tools.txt

# Tools aktivieren (Zeilen auskommentieren)
nano tools/tools.txt

# Container starten - Tools werden automatisch installiert
djinn start
```

**Verfügbare Tools:**

| Tool | Beschreibung |
|------|--------------|
| `azure-cli` | Microsoft Azure CLI (`az`) |
| `pulumi` | Infrastructure as Code CLI |
| `psql` | PostgreSQL Client (psql, pg_dump) |

**Vorteile:**
- Kein Rebuild nötig
- Installation wird gecached (schneller Restart)
- Projekt-spezifische Tool-Sets möglich

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

### Festes Projekt-Verzeichnis aendern

In `~/.config/djinn_in_a_box/config.toml` anpassen:

```toml
[general]
code_dir = "/dein/pfad/zu/projekten"
```

Oder interaktiv mit `djinn init --force`.

### Shell-Mounts deaktivieren

Falls du keine oh-my-zsh oder oh-my-posh Installation auf dem Host hast:

```toml
# In config.toml setzen:
[shell]
skip_mounts = true
```

### Resource-Limits anpassen

```toml
# In config.toml setzen:
[resources]
cpu_limit = 4
memory_limit = "8G"
cpu_reservation = 1
memory_reservation = "2G"
```

---

## Migration from Bash Scripts

If you previously used the Bash scripts (`dev.sh`, `mcp.sh`), here is the migration guide:

| Old Command | New Command |
|-------------|-------------|
| `./scripts/dev.sh build` | `djinn build` |
| `./scripts/dev.sh start` | `djinn start` |
| `./scripts/dev.sh start --docker` | `djinn start --docker` |
| `./scripts/dev.sh auth` | `djinn auth` |
| `./scripts/dev.sh status` | `djinn status` |
| `./scripts/dev.sh clean` | `djinn clean` |
| `./scripts/dev.sh clean --all` | `djinn clean volumes --all` |
| `./scripts/dev.sh run claude "prompt"` | `djinn run claude "prompt"` |
| `./mcp/mcp.sh start` | `mcpgateway start` |
| `./mcp/mcp.sh enable <server>` | `mcpgateway enable <server>` |
| `./mcp/mcp.sh status` | `mcpgateway status` |

### Shell Wrapper Migration

Remove old shell wrappers from `~/.zshrc` or `~/.zshrc.local`:

```bash
# OLD (can be removed):
djinn() {
    local script_path="/path/to/djinn_in_a_box/scripts/dev.sh"
    ...
}

mcpgateway() {
    local script_path="/path/to/djinn_in_a_box/mcp/mcp.sh"
    ...
}

# NEW: CLI is directly available after `uv tool install .`
# No wrappers needed!
```