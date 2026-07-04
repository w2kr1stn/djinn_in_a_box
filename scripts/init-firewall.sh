#!/bin/bash
# =============================================================================
# Firewall Initialization Script
# =============================================================================
# Restricts outbound connections to whitelisted domains only.
# Based on Anthropic's DevContainer reference implementation.
#
# Usage: Run as root at container startup
#   docker run --cap-add=NET_ADMIN ... 
#
# NOTE: This script is for the Djinn Container, not the MCP Gateway.
#       The MCP Gateway and MCP Servers have their own isolation.
# =============================================================================

set -euo pipefail

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
# Whitelisted domains
# -----------------------------------------------------------------------------
ALLOWED_DOMAINS=(
    # Package registries
    "registry.npmjs.org"
    "pypi.org"
    "files.pythonhosted.org"
    
    # AI APIs (for direct API usage, not via MCP)
    "api.anthropic.com"
    "api.openai.com"
    
    # Code hosting
    "github.com"
    "api.github.com"
    "raw.githubusercontent.com"
    
    # Node/fnm
    "nodejs.org"
    "fnm.vercel.app"
    
    # uv/Astral
    "astral.sh"
    
    # OAuth endpoints (for authentication)
    "console.anthropic.com"
    "auth.openai.com"
    
    # Add project-specific domains below:
    # "api.example.com"
)

# -----------------------------------------------------------------------------
# Docker network ranges (for container-to-container communication)
# -----------------------------------------------------------------------------
DOCKER_NETWORKS=(
    "172.16.0.0/12"    # Docker private range (includes default bridge 172.17.0.0/16)
    "192.168.0.0/16"   # Alternative Docker networks
    "10.0.0.0/8"       # Docker Swarm / custom networks
)

# -----------------------------------------------------------------------------
# Resolve domains to IPs
# -----------------------------------------------------------------------------
resolve_domain() {
    getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | head -1
}

# -----------------------------------------------------------------------------
# Apply firewall rules
# -----------------------------------------------------------------------------
echo "" >&2
ui_info "Initializing firewall..."

# Flush existing rules
iptables -F OUTPUT 2>/dev/null || true

# Allow loopback (localhost)
iptables -A OUTPUT -o lo -j ACCEPT

# Allow established connections
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Allow DNS (needed for resolution)
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

# -----------------------------------------------------------------------------
# Allow Docker internal networks (for MCP Gateway + Docker Proxy)
# -----------------------------------------------------------------------------
echo "" >&2
ui_info "Allowing Docker internal networks..."
for network in "${DOCKER_NETWORKS[@]}"; do
    iptables -A OUTPUT -d "$network" -j ACCEPT
    ui_ok "Allowed network: $network"
done

# -----------------------------------------------------------------------------
# Allow whitelisted domains
# -----------------------------------------------------------------------------
echo "" >&2
ui_info "Allowing whitelisted domains..."
for domain in "${ALLOWED_DOMAINS[@]}"; do
    ip=$(resolve_domain "$domain")
    if [ -n "$ip" ]; then
        iptables -A OUTPUT -d "$ip" -j ACCEPT
        ui_ok "Allowed: $domain ($ip)"
    else
        ui_warn "Could not resolve: $domain"
    fi
done

# Default deny
iptables -A OUTPUT -j DROP

echo "" >&2
ui_ok "Firewall initialized. Outbound traffic restricted to whitelist."
echo "" >&2
ui_info "Allowed:"
ui_info "Docker internal networks (MCP Gateway, Docker Proxy, etc.)"
ui_info "Whitelisted domains (package registries, AI APIs, etc.)"
echo "" >&2
ui_info "To add domains at runtime:"
ui_info "iptables -I OUTPUT -d \$(getent hosts example.com | awk '{print \$1}') -j ACCEPT"
