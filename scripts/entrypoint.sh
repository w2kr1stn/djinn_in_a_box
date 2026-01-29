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

# Fix ownership of volume-mounted directories (Docker creates them as root)
for dir in ~/.cache/uv ~/.cache/ai-dev-tools ~/.local/share/fnm ~/.azure ~/.pulumi ~/.config/sops; do
    if [[ -d "$dir" ]] && [[ ! -w "$dir" ]]; then
        sudo chown -R $(id -u):$(id -g) "$dir"
    fi
done

# =============================================================================
# Git Configuration (container-specific paths)
# =============================================================================
# Generate ~/.gitconfig_local with container paths for includeIf directives
SIGNING_KEY=$(ls ~/.ssh/*_github.pub 2>/dev/null | head -1)
if [[ -n "$SIGNING_KEY" ]]; then
    cat > ~/.gitconfig_local << EOF
[user]
    signingkey = $SIGNING_KEY
EOF
    # Add excludesfile if it exists
    if [[ -f ~/.gitignore_global ]]; then
        echo "[core]" >> ~/.gitconfig_local
        echo "    excludesfile = $HOME/.gitignore_global" >> ~/.gitconfig_local
    fi
fi

# =============================================================================
# Tool Configuration & Seed Sync
# =============================================================================
mkdir -p ~/.claude/agents ~/.claude/skills ~/.gemini ~/.codex ~/.config/opencode/commands

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

# OpenCode config sync
# OpenCode looks for config in: ~/.config/opencode/.opencode.json
# OpenCode looks for commands in: ~/.config/opencode/commands/
OPENCODE_SEED="$HOME/.opencode/seed"
OPENCODE_CONFIG="$HOME/.config/opencode"
mkdir -p "$OPENCODE_CONFIG/commands"
if [[ -d "$OPENCODE_SEED" ]] && [[ -n "$(ls -A "$OPENCODE_SEED" 2>/dev/null)" ]]; then
    # Copy commands directory if exists (user: prefix in OpenCode)
    [[ -d "$OPENCODE_SEED/commands" ]] && cp -r "$OPENCODE_SEED/commands"/* "$OPENCODE_CONFIG/commands/" 2>/dev/null || true
    # Merge settings.json if exists
    if [[ -f "$OPENCODE_SEED/settings.json" ]] && [[ -f "$OPENCODE_CONFIG/.opencode.json" ]]; then
        jq -s '.[0] * .[1]' "$OPENCODE_CONFIG/.opencode.json" "$OPENCODE_SEED/settings.json" > "$OPENCODE_CONFIG/.opencode.json.tmp" && mv "$OPENCODE_CONFIG/.opencode.json.tmp" "$OPENCODE_CONFIG/.opencode.json"
    elif [[ -f "$OPENCODE_SEED/settings.json" ]]; then
        cp "$OPENCODE_SEED/settings.json" "$OPENCODE_CONFIG/.opencode.json"
    fi
fi

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

    # OpenCode (JSON in ~/.config/opencode/)
    OPENCODE_MCP_CONFIG=~/.config/opencode/.opencode.json
    [[ ! -f "$OPENCODE_MCP_CONFIG" ]] && echo '{}' > "$OPENCODE_MCP_CONFIG"
    jq --arg url "$MCP_BASE_URL" \
        '.mcpServers["docker-gateway"] = {"type": "sse", "url": $url}' \
        "$OPENCODE_MCP_CONFIG" > "$OPENCODE_MCP_CONFIG.tmp" && mv "$OPENCODE_MCP_CONFIG.tmp" "$OPENCODE_MCP_CONFIG"

    echo "✓ MCP Gateway: Connected ($MCP_BASE_URL)"
else
    echo "⚠ MCP Gateway: Not reachable at $MCP_HOST"
fi

# =============================================================================
# Optional Tools Installation (with caching)
# =============================================================================
if [[ -f ~/.tools/install.sh ]]; then
    ~/.tools/install.sh
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
    echo "   Enable with: codeagent start --docker"
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
