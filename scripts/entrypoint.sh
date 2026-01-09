#!/bin/zsh

# =============================================================================
# AI Dev Base - Entrypoint Script
# =============================================================================

# -----------------------------------------------------------------------------
# Firewall & Permissions
# -----------------------------------------------------------------------------
if [[ "${ENABLE_FIREWALL:-false}" == "true" ]]; then
    echo "🔒 Initializing firewall..."
    sudo /usr/local/bin/init-firewall.sh
fi

for dir in ~/.cache/uv ~/.local/share/fnm; do
    if [[ -d "$dir" ]] && [[ ! -w "$dir" ]]; then
        sudo chown -R $(id -u):$(id -g) "$dir"
    fi
done

# =============================================================================
# Tool Configuration & Seed Sync
# =============================================================================
mkdir -p ~/.claude/agents ~/.claude/skills ~/.gemini ~/.codex

sync_tool_config() {
    local tool_name=$1
    local config_file=$2
    local seed_dir="/home/dev/.${tool_name}_seed"

    # Check if seed dir exists AND has files (avoids zsh "no matches found" error)
    if [[ -d "$seed_dir" ]] && [[ -n "$(ls -A "$seed_dir" 2>/dev/null)" ]]; then
        cp -r "$seed_dir"/* "/home/dev/.${tool_name}/" 2>/dev/null || true
        if [[ -f "$seed_dir/settings.json" ]] && [[ -f "$config_file" ]]; then
            jq -s '.[0] * .[1]' "$config_file" "$seed_dir/settings.json" > "${config_file}.tmp" && mv "${config_file}.tmp" "$config_file"
        fi
    fi
}

sync_tool_config "claude" "$HOME/.claude/claude.json"
sync_tool_config "gemini" "$HOME/.gemini/settings.json"

# =============================================================================
# MCP Gateway Configuration
# =============================================================================
MCP_GATEWAY_URL="${MCP_GATEWAY_URL:-http://mcp-gateway:8811}"
MCP_BASE_URL=$(echo "$MCP_GATEWAY_URL" | sed -E 's|/sse$||')
MCP_HOST=$(echo "$MCP_BASE_URL" | sed -E 's|https?://([^/]+).*|\1|')

if curl -s --connect-timeout 2 "http://${MCP_HOST}/" >/dev/null 2>&1; then
    # Claude Code
    [[ ! -f ~/.claude/claude.json ]] && echo '{"mcpServers":{}}' > ~/.claude/claude.json
    jq --arg url "$MCP_BASE_URL" '.mcpServers["docker-gateway"] = {"type": "http", "url": $url}' ~/.claude/claude.json > ~/.claude/claude.json.tmp && mv ~/.claude/claude.json.tmp ~/.claude/claude.json
    
    # Gemini CLI
    GEMINI_CONFIG=~/.gemini/settings.json
    [[ ! -f "$GEMINI_CONFIG" ]] && echo '{"mcpServers":{}}' > "$GEMINI_CONFIG"
    if command -v jq &> /dev/null; then
        jq --arg url "$MCP_BASE_URL" \
            '.mcpServers["docker-gateway"] = {"httpUrl": $url}' \
            "$GEMINI_CONFIG" > "$GEMINI_CONFIG.tmp" && mv "$GEMINI_CONFIG.tmp" "$GEMINI_CONFIG"
    fi

    # Codex CLI (TOML)
    CODEX_CONFIG=~/.codex/config.toml
    if [[ ! -f "$CODEX_CONFIG" ]]; then
        cat > "$CODEX_CONFIG" << TOML
[features]
rmcp_client = true
[mcp_servers.docker-gateway]
url = "$MCP_BASE_URL"
TOML
    else
        sed -i "/\[mcp_servers\.docker-gateway\]/,/^\[/{s|url = .*|url = \"$MCP_BASE_URL\"|}" "$CODEX_CONFIG"
    fi
    echo "✓ MCP Gateway: Connected ($MCP_BASE_URL)"
else
    echo "⚠ MCP Gateway: Not reachable at $MCP_HOST"
fi

# =============================================================================
# Docker Status & Verification
# =============================================================================
if [[ -n "${DOCKER_HOST:-}" ]]; then
    echo ""
    echo "🐳 Docker Access: Enabled via proxy"
    echo "   Host: $DOCKER_HOST"
    
    # Verbindung testen
    if docker version &>/dev/null; then
        echo "   Status: ✓ Connected"
        
        # Proxy-Einschränkungen dokumentieren
        echo ""
        echo "   Allowed operations:"
        echo "     ✓ docker ps, images, networks, volumes"
        echo "     ✓ docker run, start, stop, rm"
        echo "     ✓ docker pull"
        echo ""
        echo "   Blocked operations (security):"
        echo "     ✗ docker exec (use 'docker run' instead)"
        echo "     ✗ docker build (use pre-built images)"
        echo "     ✗ docker commit, push"
        echo "     ✗ swarm, secrets, configs"
    else
        echo "   Status: ✗ Connection failed"
        echo "   Hint: Is docker-proxy running? Check: docker ps | grep proxy"
    fi
    echo ""
else
    echo ""
    echo "🐳 Docker Access: Disabled"
    echo "   Enable with: ./dev.sh start --docker"
    echo ""
fi

# =============================================================================
# Security Summary
# =============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Security Status:"
printf "  Firewall:     %s\n" "${ENABLE_FIREWALL:-false}" | sed 's/true/✓ Enabled/;s/false/✗ Disabled/'
printf "  Docker:       %s\n" "${DOCKER_ENABLED:-false}" | sed 's/true/✓ Enabled (proxied)/;s/false/✗ Disabled/'
printf "  MCP Gateway:  %s\n" "$(curl -s --connect-timeout 1 "http://${MCP_HOST}/" &>/dev/null && echo '✓ Connected' || echo '✗ Not connected')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

exec /bin/zsh "$@"
