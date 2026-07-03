#!/bin/zsh
set -euo pipefail

# =============================================================================
# Djinn in a Box - Entrypoint Script
# =============================================================================

# -----------------------------------------------------------------------------
# Firewall & Permissions
# -----------------------------------------------------------------------------
if [[ "${ENABLE_FIREWALL:-false}" == "true" ]]; then
    echo "🔒 Initializing firewall..."
    sudo /usr/local/bin/init-firewall.sh
fi

# Fix ownership of volume-mounted directories (Docker creates them as root)
for dir in ~/.cache/uv ~/.cache/djinn-tools ~/.local/share/fnm ~/.vscode-server ~/workspaces; do
    if [[ -d "$dir" ]] && [[ ! -w "$dir" ]]; then
        sudo chown -R $(id -u):$(id -g) "$dir"
    fi
done

# =============================================================================
# Git Configuration (container-specific paths)
# =============================================================================
# Generate ~/.gitconfig_local with container paths for includeIf directives
SIGNING_KEY=$(ls ~/.ssh/*_github.pub 2>/dev/null | head -1 || true)
# Validate path contains only safe characters before interpolating into gitconfig
if [[ -n "$SIGNING_KEY" ]] && [[ "$SIGNING_KEY" =~ ^[/a-zA-Z0-9_.-]+$ ]]; then
    printf '[user]\n    signingkey = %s\n' "$SIGNING_KEY" > ~/.gitconfig_local
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
SEED_LIB="${SEED_LIB:-/home/dev/seed-lib.sh}"
if [[ ! -r "$SEED_LIB" ]]; then
    echo "✗ seed library not found at $SEED_LIB — the image is stale or broken." >&2
    echo "  Rebuild it with: djinn build" >&2
    exit 1
fi
source "$SEED_LIB"

# Restore claude.json from volume (Claude Code uses atomic writes which break
# the Dockerfile symlink, so we sync the file explicitly instead)
[[ -L "$HOME/.claude.json" ]] && rm -f "$HOME/.claude.json"
if [[ -f "$HOME/.claude/claude.json" ]]; then
    cp "$HOME/.claude/claude.json" "$HOME/.claude.json"
fi

claude_settings_merge "$HOME/.claude_seed" "$HOME/.claude/settings.json"

sync_seed "gemini"   "$HOME/.gemini_seed"   "$HOME/.gemini"          "$HOME/.gemini/settings.json"
sync_seed "opencode" "$HOME/.opencode/seed" "$HOME/.config/opencode" "$HOME/.config/opencode/.opencode.json"

# =============================================================================
# MCP Server Registration (all CLI tools, from canonical config)
# =============================================================================
source /home/dev/mcp-register.sh
register_mcp_servers

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
    # Direct socket mode: fix permissions so dev user can access immediately
    if [[ -S /var/run/docker.sock ]]; then
        SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
        if ! id -G | grep -qw "$SOCK_GID"; then
            sudo chgrp "$(id -gn)" /var/run/docker.sock
            sudo chmod g+rw /var/run/docker.sock
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

    # Test connection
    if docker version &>/dev/null; then
        echo "   Status: ✓ Connected"

        # Document proxy restrictions
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
    echo "   Enable with: djinn start --docker"
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
printf "  MCP Gateway:  %s\n" "$([[ "$MCP_REACHABLE" == "true" ]] && echo '✓ Connected' || echo '✗ Not connected')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# =============================================================================
# Interactive Shell (reverse-sync settings on exit)
# =============================================================================
# Fence set -e around the interactive shell: a non-zero shell exit must NOT abort
# the script before EXIT_CODE capture + reverse-sync (else settings persistence is silently skipped).
set +e
/bin/zsh "$@"
EXIT_CODE=$?
set -e

# Persist claude.json state into volume for next container start
reverse_sync_file "$HOME/.claude.json"                    "$HOME/.claude/claude.json"
# → settings.local.json (personal overlay, git-ignored): in-session changes persist there, NOT the tracked baseline
reverse_sync_file "$HOME/.claude/settings.json"          "$HOME/.claude_seed/settings.local.json"
reverse_sync_file "$HOME/.gemini/settings.json"          "$HOME/.gemini_seed/settings.json"
reverse_sync_file "$HOME/.config/opencode/.opencode.json" "$HOME/.opencode/seed/.opencode.json"

exit $EXIT_CODE
