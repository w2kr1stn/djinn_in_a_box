#!/bin/zsh
set -euo pipefail

# =============================================================================
# Djinn in a Box - Entrypoint Script
# =============================================================================

OUTPUT_LIB="${OUTPUT_LIB:-/home/dev/output-lib.sh}"
_djinn_define_plain_ui_fallbacks() {
    ui_section() { echo "[info] $1" >&2; }
    ui_ok() { echo "[ok] $1" >&2; }
    ui_warn() { echo "[warn] $1" >&2; }
    ui_err() { echo "[err] $1" >&2; }
    ui_info() { echo "[info] $1" >&2; }
    ui_item() {
        local marker=$1
        local message=$2
        local plain_marker=${3:-}
        local plain_message=${5:-$message}

        if [[ -z "$plain_marker" ]]; then
            case "$marker" in
                "↻") plain_marker="[sync]" ;;
                "⊕") plain_marker="[merge]" ;;
                "✕") plain_marker="[stale]" ;;
                "+") plain_marker="[init]" ;;
                "-") plain_marker="[off]" ;;
                "!") plain_marker="[warn]" ;;
                *) plain_marker="[info]" ;;
            esac
        fi

        echo "$plain_marker $plain_message" >&2
    }
}

if [[ -r "$OUTPUT_LIB" ]] && source "$OUTPUT_LIB"; then
    :
else
    _djinn_define_plain_ui_fallbacks
    ui_warn "output library not found at $OUTPUT_LIB; using plain startup output."
fi

# -----------------------------------------------------------------------------
# Firewall & Permissions
# -----------------------------------------------------------------------------
if [[ "${ENABLE_FIREWALL:-false}" == "true" ]]; then
    ui_info "Initializing firewall..."
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
ui_section "Seed & Config"
mkdir -p ~/.claude/{agents,skills,commands} ~/.gemini ~/.codex ~/.config/opencode/commands
SEED_LIB="${SEED_LIB:-/home/dev/seed-lib.sh}"
if [[ ! -r "$SEED_LIB" ]]; then
    ui_err "seed library not found at $SEED_LIB — the image is stale or broken."
    ui_info "Rebuild it with: djinn build"
    exit 1
fi
source "$SEED_LIB"

# Restore claude.json from volume (Claude Code uses atomic writes which break
# the Dockerfile symlink, so we sync the file explicitly instead)
[[ -L "$HOME/.claude.json" ]] && rm -f "$HOME/.claude.json"
if [[ -f "$HOME/.claude/claude.json" ]]; then
    cp "$HOME/.claude/claude.json" "$HOME/.claude.json"
fi

ui_info "[seed-sync] claude:"
claude_settings_merge "$HOME/.claude_seed" "$HOME/.claude/settings.json" >&2

sync_seed "gemini"   "$HOME/.gemini_seed"   "$HOME/.gemini"          "$HOME/.gemini/settings.json" >&2
sync_seed "opencode" "$HOME/.opencode/seed" "$HOME/.config/opencode" "$HOME/.config/opencode/.opencode.json" >&2

# =============================================================================
# MCP Server Registration (all CLI tools, from canonical config)
# =============================================================================
ui_section "MCP"
MCP_REGISTER="${MCP_REGISTER:-/home/dev/mcp-register.sh}"
source "$MCP_REGISTER"
register_mcp_servers >&2

# =============================================================================
# Optional Tools Installation (with caching)
# =============================================================================
ui_section "Tools"
if [[ -f ~/.tools/install.sh ]]; then
    ~/.tools/install.sh >&2
fi

# =============================================================================
# Security Summary
# =============================================================================
ui_section "Security"
if [[ "${ENABLE_FIREWALL:-false}" == "true" ]]; then
    ui_ok "Firewall:     Enabled"
else
    ui_warn "Firewall:     Disabled"
fi

if [[ "${DOCKER_DIRECT:-false}" == "true" ]]; then
    # Direct socket mode: fix permissions so dev user can access immediately
    if [[ -S /var/run/docker.sock ]]; then
        SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
        if ! id -G | grep -qw "$SOCK_GID"; then
            sudo chgrp "$(id -gn)" /var/run/docker.sock
            sudo chmod g+rw /var/run/docker.sock
        fi
    fi

    ui_ok "Docker Access: Direct socket (NO PROXY)"
    ui_info "Socket: /var/run/docker.sock"
    ui_warn "WARNING: Full Docker access — no API filtering!"
    ui_info "All operations allowed: build, exec, push, etc."

    if docker version &>/dev/null; then
        ui_ok "Status: Connected"
    else
        ui_err "Status: Connection failed"
        ui_info "Hint: Check socket permissions (host docker GID: ${SOCK_GID:-unknown})"
    fi
elif [[ -n "${DOCKER_HOST:-}" ]]; then
    ui_ok "Docker Access: Enabled via proxy"
    ui_info "Host: $DOCKER_HOST"

    # Test connection
    if docker version &>/dev/null; then
        ui_ok "Status: Connected"

        # Document proxy restrictions
        ui_info "Allowed operations:"
        ui_ok "docker ps, images, networks, volumes"
        ui_ok "docker run, start, stop, rm"
        ui_ok "docker pull"
        ui_info "Blocked operations (security):"
        ui_err "docker exec (use 'docker run' instead)"
        ui_err "docker build (use pre-built images)"
        ui_err "docker commit, push"
        ui_err "swarm, secrets, configs"
    else
        ui_err "Status: Connection failed"
        ui_info "Hint: Is docker-proxy running? Check: docker ps | grep proxy"
    fi
else
    ui_warn "Docker Access: Disabled"
    ui_info "Enable with: djinn start --docker"
fi

if [[ "${MCP_REACHABLE:-false}" == "true" ]]; then
    ui_ok "MCP Gateway:  Connected"
else
    ui_warn "MCP Gateway:  Not connected"
fi
echo "" >&2

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
