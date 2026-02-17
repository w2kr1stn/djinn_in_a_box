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
for dir in ~/.cache/uv ~/.cache/ai-dev-tools ~/.local/share/fnm ~/.azure ~/.pulumi ~/.config/sops ~/.vscode-server ~/workspaces; do
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
mkdir -p ~/.claude/{agents,skills,commands} ~/.gemini ~/.codex ~/.config/opencode/commands

# -----------------------------------------------------------------------------
# sync_seed: Clean-sync managed dirs/files from read-only seed to volume.
# Subdirectories are fully replaced (rm + cp). Root files are overwritten.
# A .seed-manifest tracks synced files to detect and remove stale entries.
#   $1 = label       (for logging)
#   $2 = seed_dir    (read-only bind mount from host)
#   $3 = target_dir  (persistent Docker volume)
#   $4 = config_file (path for settings.json deep-merge, optional)
# -----------------------------------------------------------------------------
sync_seed() {
    local label=$1 seed_dir=$2 target_dir=$3 config_file=${4:-}
    local manifest="${target_dir}/.seed-manifest"
    local tmp_manifest="${manifest}.tmp"

    if [[ ! -d "$seed_dir" ]] || [[ -z "$(ls -A "$seed_dir" 2>/dev/null)" ]]; then
        return
    fi

    echo "  [seed-sync] ${label}:"
    : > "$tmp_manifest"

    # Phase 1: Clean-replace subdirectories (seed = source of truth)
    for dir in "$seed_dir"/*(N/); do
        local dirname=${dir:t}
        echo "    ↻ ${dirname}/"
        rm -rf "${target_dir}/${dirname}"
        cp -r "$dir" "${target_dir}/${dirname}"
        find "${target_dir}/${dirname}" -type f -printf "${dirname}/%P\n" >> "$tmp_manifest"
    done

    # Phase 2: Overwrite root-level files (skip settings.json — handled via merge)
    for file in "$seed_dir"/*(N.); do
        local filename=${file:t}
        [[ "$filename" == "settings.json" ]] && continue
        echo "    ↻ ${filename}"
        cp "$file" "${target_dir}/${filename}"
        echo "$filename" >> "$tmp_manifest"
    done

    # Phase 3: Remove stale files tracked by previous manifest
    if [[ -f "$manifest" ]]; then
        while IFS= read -r entry; do
            [[ -z "$entry" ]] && continue
            if ! grep -qxF "$entry" "$tmp_manifest"; then
                if [[ -e "${target_dir}/${entry}" ]]; then
                    echo "    ✕ ${entry} (stale)"
                    rm -f "${target_dir}/${entry}"
                fi
            fi
        done < "$manifest"
        find "$target_dir" -mindepth 1 -type d -empty -delete 2>/dev/null || true
    fi

    # Phase 4: Persist new manifest
    sort -o "$manifest" "$tmp_manifest"
    rm -f "$tmp_manifest"

    # Phase 5: Deep-merge settings.json (seed wins, volume-only keys preserved)
    if [[ -n "$config_file" ]] && [[ -f "$seed_dir/settings.json" ]]; then
        if [[ -f "$config_file" ]]; then
            echo "    ⊕ settings.json (merged)"
            jq -s '.[0] * .[1]' "$config_file" "$seed_dir/settings.json" \
                > "${config_file}.tmp" && mv "${config_file}.tmp" "$config_file"
        else
            echo "    + settings.json (init)"
            cp "$seed_dir/settings.json" "$config_file"
        fi
    fi
}

sync_seed "claude"   "$HOME/.claude_seed"   "$HOME/.claude"          "$HOME/.claude/claude.json"
sync_seed "gemini"   "$HOME/.gemini_seed"   "$HOME/.gemini"          "$HOME/.gemini/settings.json"
sync_seed "opencode" "$HOME/.opencode/seed" "$HOME/.config/opencode" "$HOME/.config/opencode/.opencode.json"

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
if [[ "${DOCKER_DIRECT:-false}" == "true" ]]; then
    # Direct socket mode: fix permissions if needed
    if [[ -S /var/run/docker.sock ]]; then
        SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
        if ! id -G | grep -qw "$SOCK_GID"; then
            DOCKER_GRP=$(getent group "$SOCK_GID" 2>/dev/null | cut -d: -f1)
            if [[ -z "$DOCKER_GRP" ]]; then
                DOCKER_GRP="docker-host"
                sudo groupadd -g "$SOCK_GID" "$DOCKER_GRP" 2>/dev/null || true
            fi
            sudo usermod -aG "$DOCKER_GRP" "$(whoami)" 2>/dev/null || true
        fi
    fi

    echo ""
    echo "🐳 Docker Access: Direct socket (NO PROXY)"
    echo "   Socket: /var/run/docker.sock"
    echo ""
    echo "   ⚠  WARNING: Full Docker access — no API filtering!"
    echo "   All operations allowed: build, exec, push, etc."
    echo ""

    if docker version &>/dev/null; then
        echo "   Status: ✓ Connected"
    else
        echo "   Status: ✗ Connection failed"
        echo "   Hint: Check socket permissions (host docker GID: ${SOCK_GID:-unknown})"
    fi
    echo ""
elif [[ -n "${DOCKER_HOST:-}" ]]; then
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
if [[ "${DOCKER_DIRECT:-false}" == "true" ]]; then
    printf "  Docker:       ✓ Enabled (DIRECT — no proxy)\n"
elif [[ "${DOCKER_ENABLED:-false}" == "true" ]]; then
    printf "  Docker:       ✓ Enabled (proxied)\n"
else
    printf "  Docker:       ✗ Disabled\n"
fi
printf "  MCP Gateway:  %s\n" "$(curl -s --connect-timeout 1 "http://${MCP_HOST}/" &>/dev/null && echo '✓ Connected' || echo '✗ Not connected')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

exec /bin/zsh "$@"
