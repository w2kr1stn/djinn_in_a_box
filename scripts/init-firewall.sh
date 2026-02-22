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
    "172.16.0.0/12"    # Default Docker bridge range
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
echo ""
echo "🔒 Initializing firewall..."

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
echo ""
echo "📡 Allowing Docker internal networks..."
for network in "${DOCKER_NETWORKS[@]}"; do
    iptables -A OUTPUT -d "$network" -j ACCEPT
    echo "  ✓ Allowed network: $network"
done

# -----------------------------------------------------------------------------
# Allow whitelisted domains
# -----------------------------------------------------------------------------
echo ""
echo "🌐 Allowing whitelisted domains..."
for domain in "${ALLOWED_DOMAINS[@]}"; do
    ip=$(resolve_domain "$domain")
    if [ -n "$ip" ]; then
        iptables -A OUTPUT -d "$ip" -j ACCEPT
        echo "  ✓ Allowed: $domain ($ip)"
    else
        echo "  ⚠ Could not resolve: $domain"
    fi
done

# Default deny
iptables -A OUTPUT -j DROP

echo ""
echo "🔒 Firewall initialized. Outbound traffic restricted to whitelist."
echo ""
echo "Allowed:"
echo "  - Docker internal networks (MCP Gateway, Docker Proxy, etc.)"
echo "  - Whitelisted domains (package registries, AI APIs, etc.)"
echo ""
echo "To add domains at runtime:"
echo "  iptables -I OUTPUT -d \$(getent hosts example.com | awk '{print \$1}') -j ACCEPT"
