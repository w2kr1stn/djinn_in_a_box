#!/bin/bash
# =============================================================================
# MCP Gateway Management Script
# =============================================================================
# Requires: docker mcp CLI plugin installed on host
# Install: https://github.com/docker/mcp-gateway#install-as-docker-cli-plugin
# =============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
source "$SCRIPT_DIR/../scripts/colors.sh"

# Check if docker mcp CLI plugin is installed
check_mcp_cli() {
    if ! docker mcp --help &>/dev/null; then
        echo -e "${RED}Error: docker mcp CLI plugin not installed${NC}"
        echo ""
        echo "Install it with:"
        echo "  git clone https://github.com/docker/mcp-gateway.git"
        echo "  cd mcp-gateway"
        echo "  make docker-mcp"
        echo ""
        echo "Or download a binary from:"
        echo "  https://github.com/docker/mcp-gateway/releases"
        exit 1
    fi
}

print_help() {
    cat << 'EOF'
MCP Gateway Management

Usage: ./mcp.sh <command> [options]

Setup Commands:
  start                 Start the MCP Gateway service
  stop                  Stop the MCP Gateway service
  restart               Restart the MCP Gateway service
  status                Show gateway status and enabled servers
  logs                  Show gateway logs (follow mode)

Server Management (via docker mcp CLI):
  enable <server>       Enable an MCP server
  disable <server>      Disable an MCP server
  servers               List enabled servers
  catalog               Show available servers in catalog

Utility:
  test                  Test gateway connectivity
  clean                 Stop gateway and remove config (full reset)
  help                  Show this help message

Examples:
  ./mcp.sh start
  ./mcp.sh enable duckduckgo
  ./mcp.sh enable memory
  ./mcp.sh servers
  ./mcp.sh status

Popular MCP Servers:
  duckduckgo           Web search via DuckDuckGo
  memory               Persistent memory/knowledge base
  fetch                HTTP fetch capabilities
  time                 Current time and timezone tools
  filesystem           Local filesystem access
  sqlite               SQLite database
  
Full catalog: https://hub.docker.com/search?q=mcp%2F

Security Features (enabled by default):
  --verify-signatures  Only run signed MCP images
  --log-calls          Audit log for all tool invocations
  --block-secrets      Prevent secrets in responses
EOF
}

ensure_network() {
    if ! docker network inspect ai-dev-network &>/dev/null; then
        echo -e "${BLUE}Creating ai-dev-network...${NC}"
        docker network create ai-dev-network
    fi
}

require_running() {
    if ! docker ps --format '{{.Names}}' | grep -q '^mcp-gateway$'; then
        echo -e "${RED}Error: MCP Gateway is not running${NC}"
        echo "Start it with: ./mcp.sh start"
        exit 1
    fi
}

case "${1:-help}" in
    start)
        check_mcp_cli
        ensure_network
        
        echo -e "${BLUE}🚀 Starting MCP Gateway...${NC}"
        docker compose up -d
        sleep 3
        
        if docker ps --format '{{.Names}}' | grep -q '^mcp-gateway$'; then
            echo -e "${GREEN}✓ MCP Gateway is running${NC}"
            echo ""
            echo "Endpoint: http://mcp-gateway:8811 (from containers)"
            echo "          http://localhost:8811 (from host)"
            echo ""
            echo "Next steps:"
            echo "  ./mcp.sh enable duckduckgo    # Enable web search"
            echo "  ./mcp.sh enable memory        # Enable persistent memory"
            echo "  ./mcp.sh servers              # List enabled servers"
        else
            echo -e "${RED}✗ Failed to start MCP Gateway${NC}"
            docker compose logs
            exit 1
        fi
        ;;
        
    stop)
        echo -e "${YELLOW}Stopping MCP Gateway...${NC}"
        docker compose down
        echo -e "${GREEN}✓ MCP Gateway stopped${NC}"
        ;;
        
    restart)
        echo -e "${YELLOW}Restarting MCP Gateway...${NC}"
        docker compose restart
        sleep 2
        echo -e "${GREEN}✓ MCP Gateway restarted${NC}"
        ;;
        
    status)
        echo -e "${BLUE}MCP Gateway Status${NC}"
        echo "===================="
        
        if docker ps --format '{{.Names}}' | grep -q '^mcp-gateway$'; then
            echo -e "Gateway: ${GREEN}Running${NC}"
            echo ""
            docker ps --filter "name=mcp-gateway" --format "ID: {{.ID}}\nImage: {{.Image}}\nStatus: {{.Status}}\nPorts: {{.Ports}}"
            echo ""
            echo -e "${BLUE}Enabled Servers:${NC}"
            docker mcp server ls 2>/dev/null || echo "  (none)"
            echo ""
            echo -e "${BLUE}Running MCP Containers:${NC}"
            docker ps --filter "name=mcp-" --format "  {{.Names}} ({{.Status}})" | grep -v mcp-gateway || echo "  (none)"
        else
            echo -e "Gateway: ${RED}Stopped${NC}"
        fi
        ;;
        
    logs)
        require_running
        docker logs -f mcp-gateway
        ;;
        
    enable)
        check_mcp_cli
        require_running
        
        if [[ -z "$2" ]]; then
            echo -e "${RED}Error: Please specify a server to enable${NC}"
            echo "Usage: ./mcp.sh enable <server-name>"
            echo "Example: ./mcp.sh enable duckduckgo"
            echo ""
            echo "Available servers: ./mcp.sh catalog"
            exit 1
        fi
        
        echo -e "${BLUE}Enabling MCP server: $2${NC}"
        docker mcp server enable "$2"
        echo -e "${GREEN}✓ Server '$2' enabled${NC}"
        ;;
        
    disable)
        check_mcp_cli
        require_running
        
        if [[ -z "$2" ]]; then
            echo -e "${RED}Error: Please specify a server to disable${NC}"
            exit 1
        fi
        
        echo -e "${YELLOW}Disabling MCP server: $2${NC}"
        docker mcp server disable "$2"
        echo -e "${GREEN}✓ Server '$2' disabled${NC}"
        ;;
        
    servers)
        check_mcp_cli
        echo -e "${BLUE}Enabled MCP Servers${NC}"
        echo "===================="
        docker mcp server ls 2>/dev/null || echo "No servers enabled or gateway not running"
        ;;
        
    catalog)
        check_mcp_cli
        echo -e "${BLUE}MCP Server Catalog${NC}"
        echo "===================="
        docker mcp catalog show docker-mcp 2>/dev/null || {
            echo "Unable to fetch catalog."
            echo ""
            echo "Initialize catalog first: docker mcp catalog init"
            echo "Or browse online: https://hub.docker.com/search?q=mcp%2F"
        }
        ;;
        
    test)
        echo -e "${BLUE}Testing MCP Gateway...${NC}"
        echo ""
        
        # Check container
        echo -n "Container status: "
        if docker ps --format '{{.Names}}' | grep -q '^mcp-gateway$'; then
            echo -e "${GREEN}Running${NC}"
        else
            echo -e "${RED}Not running${NC}"
            exit 1
        fi
        
        # Check localhost endpoint
        echo -n "Localhost endpoint (host access): "
        if curl -s --connect-timeout 2 -o /dev/null -w "%{http_code}" http://localhost:8811/ | grep -qE "200|404"; then
            echo -e "${GREEN}OK${NC}"
        else
            echo -e "${YELLOW}Not responding${NC}"
        fi
        
        # Check container endpoint
        echo -n "Container endpoint (network access): "
        if docker run --rm --network ai-dev-network curlimages/curl:latest \
            -s --connect-timeout 2 -o /dev/null -w "%{http_code}" http://mcp-gateway:8811/ 2>/dev/null | grep -qE "200|404"; then
            echo -e "${GREEN}OK${NC}"
        else
            echo -e "${YELLOW}Not responding (network may not exist yet)${NC}"
        fi
        
        # Check Docker socket
        echo -n "Docker socket access: "
        if docker exec mcp-gateway ls /var/run/docker.sock &>/dev/null; then
            echo -e "${GREEN}OK${NC}"
        else
            echo -e "${RED}Failed${NC}"
        fi
        
        # Check CLI plugin
        echo -n "docker mcp CLI plugin: "
        if docker mcp --help &>/dev/null; then
            echo -e "${GREEN}Installed${NC}"
        else
            echo -e "${YELLOW}Not installed${NC}"
        fi
        
        echo ""
        echo "MCP Gateway URLs:"
        echo "  Claude Code: http://mcp-gateway:8811/sse"
        echo "  Codex CLI:   http://mcp-gateway:8811"
        ;;
        
    clean)
        echo -e "${RED}⚠ This will stop the gateway and remove all configuration!${NC}"
        read -p "Are you sure? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            docker compose down 2>/dev/null || true
            docker network rm ai-dev-network 2>/dev/null || true
            rm -rf ~/.docker/mcp 2>/dev/null || true
            echo -e "${GREEN}✓ MCP Gateway cleaned${NC}"
        else
            echo "Cancelled."
        fi
        ;;
        
    help|--help|-h)
        print_help
        ;;
        
    *)
        echo -e "${RED}Unknown command: $1${NC}"
        echo ""
        print_help
        exit 1
        ;;
esac
