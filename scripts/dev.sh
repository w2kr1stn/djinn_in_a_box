#!/bin/bash
# =============================================================================
# AI Dev Base - CLI Wrapper
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Wechsle ins Hauptverzeichnis (eine Ebene über scripts/)
cd "$SCRIPT_DIR/.."

# -----------------------------------------------------------------------------
# Farben für Output
# -----------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# -----------------------------------------------------------------------------
# Load .env file if it exists
# -----------------------------------------------------------------------------
load_env() {
    if [[ -f ".env" ]]; then
        # Export variables from .env (ignore comments and empty lines)
        set -a
        # shellcheck source=/dev/null
        source .env
        set +a
    fi
}

# -----------------------------------------------------------------------------
# Check required configuration
# -----------------------------------------------------------------------------
check_config() {
    if [[ -z "${CODE_DIR:-}" ]]; then
        echo -e "${RED}Error: CODE_DIR is not set.${NC}"
        echo ""
        echo "Please configure your environment:"
        echo "  1. Copy .env.example to .env:"
        echo "     cp .env.example .env"
        echo ""
        echo "  2. Edit .env and set CODE_DIR to your projects directory:"
        echo "     CODE_DIR=/path/to/your/code"
        echo ""
        echo "  Or set it directly:"
        echo "     export CODE_DIR=/path/to/your/code"
        exit 1
    fi

    # Expand tilde
    CODE_DIR="${CODE_DIR/#\~/$HOME}"
    export CODE_DIR

    if [[ ! -d "$CODE_DIR" ]]; then
        echo -e "${RED}Error: CODE_DIR does not exist: $CODE_DIR${NC}"
        exit 1
    fi
}

# -----------------------------------------------------------------------------
# Build optional shell mount arguments
# -----------------------------------------------------------------------------
get_shell_mount_args() {
    local args=""

    # Skip all shell mounts if SKIP_SHELL_MOUNTS is true
    if [[ "${SKIP_SHELL_MOUNTS:-false}" == "true" ]]; then
        echo ""
        return
    fi

    # ZSH config (mounted as .zshrc.local)
    if [[ -f "$HOME/.zshrc" ]]; then
        args="$args -v $HOME/.zshrc:/home/dev/.zshrc.local:ro"
    fi

    # Oh My Posh theme
    local omp_theme="${OMP_THEME_PATH:-$HOME/.oh-my-zsh/custom/themes/.zsh-theme-remote.omp.json}"
    omp_theme="${omp_theme/#\~/$HOME}"
    if [[ -f "$omp_theme" ]]; then
        args="$args -v $omp_theme:/home/dev/.zsh-theme.omp.json:ro"
    fi

    # Oh My ZSH custom directory (plugins, themes, etc.)
    if [[ -d "$HOME/.oh-my-zsh/custom" ]]; then
        args="$args -v $HOME/.oh-my-zsh/custom:/home/dev/.oh-my-zsh/custom:ro"
    fi

    echo "$args"
}

# -----------------------------------------------------------------------------
# Compose File Selection
# -----------------------------------------------------------------------------
get_compose_files() {
    local docker_enabled="${1:-false}"

    if [[ "$docker_enabled" == "true" ]]; then
        echo "-f docker-compose.yml -f docker-compose.docker.yml"
    else
        echo "-f docker-compose.yml"
    fi
}

usage() {
    cat << EOF
${BLUE}AI Dev Base${NC} - Entwicklungsumgebung für Claude Code, Gemini CLI & Codex CLI

${YELLOW}Usage:${NC} ./dev.sh <command> [options]

${YELLOW}Commands:${NC}
  build       Build/rebuild the base image
  start       Start interactive shell in container
  auth        Start container with host network for OAuth (einmalig)
  status      Show container, volume and MCP status
  clean       Remove containers and optionally volumes
  help        Show this help message

${YELLOW}Options:${NC}
  --docker    Enable Docker access (via secure proxy)
  --firewall  Enable network firewall (restricts outbound to whitelist)
  --here      Mount current directory additionally under ~/workspace/
  --mount <path>  Mount specified path additionally under ~/workspace/

${YELLOW}Configuration:${NC}
  Copy .env.example to .env and set at minimum:
    CODE_DIR=/path/to/your/projects

  Optional settings in .env:
    SKIP_SHELL_MOUNTS=true   # Skip zshrc/oh-my-zsh mounts
    CPU_LIMIT=4              # Adjust resource limits
    MEMORY_LIMIT=8G
    TZ=America/New_York      # Timezone

${YELLOW}Security Modes:${NC}
  ${GREEN}Default${NC}        Kein Docker-Zugriff, kein Firewall
  ${GREEN}--docker${NC}       Docker via Proxy (gefährliche Ops blockiert)
  ${GREEN}--firewall${NC}     Ausgehender Traffic auf Whitelist beschränkt
  ${GREEN}--docker --firewall${NC}  Maximale Sicherheit + Docker-Zugriff

${YELLOW}Workspace Mount:${NC}
  ${GREEN}--here${NC}         Mountet \$(pwd) zusätzlich unter ~/workspace/
  ${GREEN}--mount /path${NC}  Mountet beliebigen Pfad unter ~/workspace/

  Im Container:
    ~/projects/   → Dein festes Code-Verzeichnis (immer)
    ~/workspace/  → Temporärer Mount (nur mit --here/--mount)

${YELLOW}Examples:${NC}
  ./dev.sh build                      # Build the image
  ./dev.sh start                      # Sicher: Kein Docker-Zugriff
  ./dev.sh start --docker             # Mit Docker-Zugriff via Proxy
  ./dev.sh start --firewall           # Mit Netzwerk-Firewall
  ./dev.sh start --docker --firewall  # Beides kombiniert
  ./dev.sh start --here               # Mit aktuellem Verzeichnis als Workspace
  ./dev.sh start --mount ~/.config    # Mit spezifischem Pfad als Workspace
  ./dev.sh start --docker --here      # Docker + aktuelles Verzeichnis
  ./dev.sh auth                       # OAuth Authentifizierung
  ./dev.sh clean                      # Container entfernen
  ./dev.sh clean --all                # Alles entfernen (inkl. Credentials!)

${YELLOW}Docker-Proxy Sicherheit:${NC}
  Wenn --docker aktiviert ist, läuft ein Docker Socket Proxy der:
  - Container starten/stoppen erlaubt
  - Image pulls erlaubt
  - ${RED}BLOCKIERT:${NC} exec, build, commit, swarm, secrets, auth

  Details: DOCKER-SOCKET-SECURITY.md

${YELLOW}Workflow:${NC}
  1. cp .env.example .env && edit .env # Konfigurieren
  2. ./dev.sh build                    # Image bauen
  3. cd mcp && ./mcp.sh start          # MCP Gateway starten
  4. ./mcp.sh enable duckduckgo        # MCP Server aktivieren
  5. cd .. && ./dev.sh auth            # Einmalig: OAuth
  6. ./dev.sh start --docker           # Täglich: Mit Docker-Zugriff

${YELLOW}Workspace Workflow:${NC}
  # An lokaler Config schrauben:
  cd ~/.config && /path/to/dev.sh start --here --docker
  # Im Container: cd ~/workspace && claude
EOF
}

ensure_network() {
    if ! docker network inspect ai-dev-network &>/dev/null; then
        echo -e "${BLUE}Creating ai-dev-network...${NC}"
        docker network create ai-dev-network
    fi
}

cmd_build() {
    echo -e "${BLUE}Building ai-dev-base image...${NC}"
    docker compose build
    echo -e "${GREEN}Done! Run './dev.sh start' to begin.${NC}"
}

cmd_start() {
    load_env
    check_config
    ensure_network

    # Parse arguments
    local docker_enabled="false"
    local firewall_enabled="false"
    local mount_path=""
    local skip_next="false"

    local args=("$@")
    for i in "${!args[@]}"; do
        if [[ "$skip_next" == "true" ]]; then
            skip_next="false"
            continue
        fi
        case "${args[$i]}" in
            --docker)   docker_enabled="true" ;;
            --firewall) firewall_enabled="true" ;;
            --here)     mount_path="$(pwd)" ;;
            --mount)
                # Nächstes Argument ist der Pfad
                mount_path="${args[$((i+1))]:-}"
                skip_next="true"
                if [[ -z "$mount_path" ]]; then
                    echo -e "${RED}--mount requires a path argument${NC}"
                    exit 1
                fi
                ;;
        esac
    done

    # Validiere Mount-Pfad falls angegeben
    local extra_volume_args=""
    if [[ -n "$mount_path" ]]; then
        # Tilde expandieren
        mount_path="${mount_path/#\~/$HOME}"
        # Absoluten Pfad sicherstellen
        if [[ -d "$mount_path" ]]; then
            mount_path="$(cd "$mount_path" && pwd)"
        else
            echo -e "${RED}Mount path does not exist or is not a directory: $mount_path${NC}"
            exit 1
        fi
        extra_volume_args="-v ${mount_path}:/home/dev/workspace"
    fi

    # Get optional shell mounts
    local shell_mount_args
    shell_mount_args=$(get_shell_mount_args)

    # Compose files basierend auf Flags
    local compose_files
    compose_files=$(get_compose_files "$docker_enabled")

    echo ""
    echo -e "${BLUE}Starting AI Dev environment...${NC}"
    echo ""
    echo -e "   ${GREEN}Projects:${NC}  $CODE_DIR"

    # Status anzeigen
    if [[ "$docker_enabled" == "true" ]]; then
        echo -e "   ${GREEN}Docker:${NC}    Enabled (via secure proxy)"
    else
        echo -e "   ${YELLOW}Docker:${NC}    Disabled (use --docker to enable)"
    fi

    if [[ "$firewall_enabled" == "true" ]]; then
        echo -e "   ${GREEN}Firewall:${NC}  Enabled (outbound restricted)"
    else
        echo -e "   ${YELLOW}Firewall:${NC}  Disabled (use --firewall to enable)"
    fi

    if [[ -n "$mount_path" ]]; then
        echo -e "   ${GREEN}Workspace:${NC} $mount_path"
    fi

    if [[ "${SKIP_SHELL_MOUNTS:-false}" == "true" ]]; then
        echo -e "   ${YELLOW}Shell:${NC}     Using container defaults (SKIP_SHELL_MOUNTS=true)"
    elif [[ -n "$shell_mount_args" ]]; then
        echo -e "   ${GREEN}Shell:${NC}     Host config mounted"
    else
        echo -e "   ${YELLOW}Shell:${NC}     No host config found"
    fi
    echo ""

    # Container starten
    # shellcheck disable=SC2086
    ENABLE_FIREWALL="$firewall_enabled" \
        docker compose $compose_files run --rm $extra_volume_args $shell_mount_args dev
}

cmd_auth() {
    load_env
    check_config

    # Parse arguments
    local docker_enabled="false"

    for arg in "$@"; do
        case "$arg" in
            --docker) docker_enabled="true" ;;
        esac
    done

    local compose_files
    compose_files=$(get_compose_files "$docker_enabled")

    # Get optional shell mounts
    local shell_mount_args
    shell_mount_args=$(get_shell_mount_args)

    echo -e "${BLUE}Starting AI Dev with host network for OAuth authentication...${NC}"
    echo ""
    echo "This mode uses network_mode: host so OAuth callbacks work."
    echo "After authenticating Claude Code, Gemini CLI and Codex, exit and use './dev.sh start'"
    echo ""

    if [[ "$docker_enabled" == "true" ]]; then
        echo -e "${YELLOW}Docker-Proxy wird im Host-Network-Modus separat gestartet...${NC}"
        # Proxy manuell starten wenn im auth-modus mit docker
        # shellcheck disable=SC2086
        docker compose $compose_files up -d docker-proxy 2>/dev/null || true
        sleep 2
    fi

    # shellcheck disable=SC2086
    docker compose $compose_files --profile auth run --rm $shell_mount_args dev-auth

    # Proxy wieder stoppen
    if [[ "$docker_enabled" == "true" ]]; then
        # shellcheck disable=SC2086
        docker compose $compose_files stop docker-proxy 2>/dev/null || true
    fi
}

cmd_status() {
    load_env

    echo -e "${BLUE}Configuration:${NC}"
    if [[ -n "${CODE_DIR:-}" ]]; then
        echo "  CODE_DIR: $CODE_DIR"
    else
        echo "  CODE_DIR: (not set)"
    fi
    echo ""

    echo -e "${BLUE}Containers:${NC}"
    docker ps -a --filter "name=ai-dev" --filter "name=mcp-" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
    echo ""

    echo -e "${BLUE}Volumes:${NC}"
    docker volume ls --filter "name=ai-dev" --filter "name=mcp-" --format "table {{.Name}}\t{{.Driver}}"
    echo ""

    echo -e "${BLUE}Networks:${NC}"
    docker network ls --filter "name=ai-dev" --format "table {{.Name}}\t{{.Driver}}"
    echo ""

    # Docker Proxy Status
    if docker ps --format '{{.Names}}' | grep -q '^ai-dev-docker-proxy$'; then
        echo -e "${GREEN}Docker Proxy: Running${NC}"
        # Proxy-Statistiken
        local blocked
        blocked=$(docker logs ai-dev-docker-proxy 2>&1 | grep -c "blocked" || echo "0")
        echo "   Blocked requests: $blocked"
    else
        echo -e "${YELLOW}Docker Proxy: Not running${NC}"
        echo "   Start with: ./dev.sh start --docker"
    fi
    echo ""

    # MCP Status
    if docker ps --format '{{.Names}}' | grep -q '^mcp-gateway$'; then
        echo -e "${GREEN}MCP Gateway: Running${NC}"
        if command -v docker &>/dev/null && docker mcp server ls &>/dev/null 2>&1; then
            echo "   Enabled servers:"
            docker mcp server ls 2>/dev/null | sed 's/^/   /'
        fi
    else
        echo -e "${YELLOW}MCP Gateway: Not running${NC}"
        echo "   Start with: cd mcp && ./mcp.sh start"
    fi
}

cmd_clean() {
    echo -e "${BLUE}Cleaning up...${NC}"

    # Alle compose configs verwenden für vollständiges cleanup
    docker compose -f docker-compose.yml -f docker-compose.docker.yml down 2>/dev/null || true

    if [[ "${1:-}" == "--all" ]]; then
        echo -e "${RED}Removing ALL volumes (including credentials)...${NC}"
        docker volume rm $(docker volume ls -q | grep ai-dev) 2>/dev/null || true
        echo -e "${RED}Removing network...${NC}"
        docker network rm ai-dev-network 2>/dev/null || true
        echo -e "${GREEN}All volumes and network removed.${NC}"
    fi

    echo -e "${GREEN}Cleanup complete.${NC}"
}

# -----------------------------------------------------------------------------
# Audit-Log Funktion (für Debugging)
# -----------------------------------------------------------------------------
cmd_audit() {
    if ! docker ps --format '{{.Names}}' | grep -q '^ai-dev-docker-proxy$'; then
        echo -e "${RED}Docker Proxy is not running.${NC}"
        echo "   Start with: ./dev.sh start --docker"
        exit 1
    fi

    echo -e "${BLUE}Docker Proxy Audit Log (last 50 lines):${NC}"
    echo ""
    docker logs --tail 50 ai-dev-docker-proxy 2>&1
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
case "${1:-help}" in
    build)      cmd_build ;;
    start)      shift; cmd_start "$@" ;;
    auth)       shift; cmd_auth "$@" ;;
    status)     cmd_status ;;
    clean)      cmd_clean "${2:-}" ;;
    audit)      cmd_audit ;;
    help|*)     usage ;;
esac
