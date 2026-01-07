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

${YELLOW}Security Modes:${NC}
  ${GREEN}Default${NC}        Kein Docker-Zugriff, kein Firewall
  ${GREEN}--docker${NC}       Docker via Proxy (gefährliche Ops blockiert)
  ${GREEN}--firewall${NC}     Ausgehender Traffic auf Whitelist beschränkt
  ${GREEN}--docker --firewall${NC}  Maximale Sicherheit + Docker-Zugriff

${YELLOW}Examples:${NC}
  ./dev.sh build                      # Build the image
  ./dev.sh start                      # Sicher: Kein Docker-Zugriff
  ./dev.sh start --docker             # Mit Docker-Zugriff via Proxy
  ./dev.sh start --firewall           # Mit Netzwerk-Firewall
  ./dev.sh start --docker --firewall  # Beides kombiniert
  ./dev.sh auth                       # OAuth Authentifizierung
  ./dev.sh auth --docker              # OAuth mit Docker-Zugriff
  ./dev.sh clean                      # Container entfernen
  ./dev.sh clean --all                # Alles entfernen (inkl. Credentials!)

${YELLOW}Docker-Proxy Sicherheit:${NC}
  Wenn --docker aktiviert ist, läuft ein Docker Socket Proxy der:
  - Container starten/stoppen erlaubt
  - Image pulls erlaubt
  - ${RED}BLOCKIERT:${NC} exec, build, commit, swarm, secrets, auth
  
  Details: DOCKER-SOCKET-SECURITY.md

${YELLOW}Workflow:${NC}
  1. ./dev.sh build                    # Image bauen
  2. cd mcp && ./mcp.sh start          # MCP Gateway starten
  3. ./mcp.sh enable duckduckgo        # MCP Server aktivieren
  4. cd .. && ./dev.sh auth            # Einmalig: OAuth
  5. ./dev.sh start --docker           # Täglich: Mit Docker-Zugriff
EOF
}

ensure_network() {
    if ! docker network inspect ai-dev-network &>/dev/null; then
        echo -e "${BLUE}📡${NC} Creating ai-dev-network..."
        docker network create ai-dev-network
    fi
}

cmd_build() {
    echo -e "${BLUE}🔨${NC} Building ai-dev-base image..."
    docker compose build
    echo -e "${GREEN}✅${NC} Done! Run './dev.sh start' to begin."
}

cmd_start() {
    ensure_network
    
    # Parse arguments
    local docker_enabled="false"
    local firewall_enabled="false"
    
    for arg in "$@"; do
        case "$arg" in
            --docker)   docker_enabled="true" ;;
            --firewall) firewall_enabled="true" ;;
        esac
    done
    
    # Compose files basierend auf Flags
    local compose_files
    compose_files=$(get_compose_files "$docker_enabled")
    
    echo ""
    echo -e "${BLUE}🚀${NC} Starting AI Dev environment..."
    echo ""
    
    # Status anzeigen
    if [[ "$docker_enabled" == "true" ]]; then
        echo -e "   ${GREEN}🐳 Docker:${NC}   Enabled (via secure proxy)"
        echo -e "      └─ Erlaubt: containers, images, networks, volumes"
        echo -e "      └─ Blockiert: exec, build, commit, swarm, secrets"
    else
        echo -e "   ${YELLOW}🐳 Docker:${NC}   Disabled (use --docker to enable)"
    fi
    
    if [[ "$firewall_enabled" == "true" ]]; then
        echo -e "   ${GREEN}🔒 Firewall:${NC} Enabled (outbound restricted)"
    else
        echo -e "   ${YELLOW}🔒 Firewall:${NC} Disabled (use --firewall to enable)"
    fi
    echo ""
    
    # Container starten
    ENABLE_FIREWALL="$firewall_enabled" \
        docker compose $compose_files run --rm dev
}

cmd_auth() {
    # Parse arguments
    local docker_enabled="false"
    
    for arg in "$@"; do
        case "$arg" in
            --docker) docker_enabled="true" ;;
        esac
    done
    
    local compose_files
    compose_files=$(get_compose_files "$docker_enabled")
    
    echo -e "${BLUE}🔐${NC} Starting AI Dev with host network for OAuth authentication..."
    echo ""
    echo "This mode uses network_mode: host so OAuth callbacks work."
    echo "After authenticating Claude Code, Gemini CLI and Codex, exit and use './dev.sh start'"
    echo ""
    
    if [[ "$docker_enabled" == "true" ]]; then
        echo -e "${YELLOW}⚠️  Docker-Proxy wird im Host-Network-Modus separat gestartet...${NC}"
        # Proxy manuell starten wenn im auth-modus mit docker
        docker compose $compose_files up -d docker-proxy 2>/dev/null || true
        sleep 2
    fi
    
    docker compose $compose_files --profile auth run --rm dev-auth
    
    # Proxy wieder stoppen
    if [[ "$docker_enabled" == "true" ]]; then
        docker compose $compose_files stop docker-proxy 2>/dev/null || true
    fi
}

cmd_status() {
    echo -e "${BLUE}📦 Containers:${NC}"
    docker ps -a --filter "name=ai-dev" --filter "name=mcp-" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
    echo ""
    
    echo -e "${BLUE}💾 Volumes:${NC}"
    docker volume ls --filter "name=ai-dev" --filter "name=mcp-" --format "table {{.Name}}\t{{.Driver}}"
    echo ""
    
    echo -e "${BLUE}🌐 Networks:${NC}"
    docker network ls --filter "name=ai-dev" --format "table {{.Name}}\t{{.Driver}}"
    echo ""
    
    # Docker Proxy Status
    if docker ps --format '{{.Names}}' | grep -q '^ai-dev-docker-proxy$'; then
        echo -e "${GREEN}🐳 Docker Proxy: Running${NC}"
        # Proxy-Statistiken
        local blocked
        blocked=$(docker logs ai-dev-docker-proxy 2>&1 | grep -c "blocked" || echo "0")
        echo "   └─ Blocked requests: $blocked"
    else
        echo -e "${YELLOW}🐳 Docker Proxy: Not running${NC}"
        echo "   └─ Start with: ./dev.sh start --docker"
    fi
    echo ""
    
    # MCP Status
    if docker ps --format '{{.Names}}' | grep -q '^mcp-gateway$'; then
        echo -e "${GREEN}🤖 MCP Gateway: Running${NC}"
        if command -v docker &>/dev/null && docker mcp server ls &>/dev/null 2>&1; then
            echo "   Enabled servers:"
            docker mcp server ls 2>/dev/null | sed 's/^/   /'
        fi
    else
        echo -e "${YELLOW}🤖 MCP Gateway: Not running${NC}"
        echo "   Start with: cd mcp && ./mcp.sh start"
    fi
}

cmd_clean() {
    echo -e "${BLUE}🧹${NC} Cleaning up..."
    
    # Alle compose configs verwenden für vollständiges cleanup
    docker compose -f docker-compose.yml -f docker-compose.docker.yml down 2>/dev/null || true
    
    if [[ "${1:-}" == "--all" ]]; then
        echo -e "${RED}⚠️  Removing ALL volumes (including credentials)...${NC}"
        docker volume rm $(docker volume ls -q | grep ai-dev) 2>/dev/null || true
        echo -e "${RED}⚠️  Removing network...${NC}"
        docker network rm ai-dev-network 2>/dev/null || true
        echo -e "${GREEN}✅${NC} All volumes and network removed."
    fi
    
    echo -e "${GREEN}✅${NC} Cleanup complete."
}

# -----------------------------------------------------------------------------
# Audit-Log Funktion (für Debugging)
# -----------------------------------------------------------------------------
cmd_audit() {
    if ! docker ps --format '{{.Names}}' | grep -q '^ai-dev-docker-proxy$'; then
        echo -e "${RED}❌${NC} Docker Proxy is not running."
        echo "   Start with: ./dev.sh start --docker"
        exit 1
    fi
    
    echo -e "${BLUE}📋${NC} Docker Proxy Audit Log (last 50 lines):"
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
