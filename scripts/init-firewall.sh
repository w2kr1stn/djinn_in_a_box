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
    # Gemini CLI: model endpoint, plus the Code Assist backend it uses when
    # signed in with a Google account rather than an API key.
    "generativelanguage.googleapis.com"
    "cloudcode-pa.googleapis.com"
    # OpenCode's own service (auth, updates). Third-party providers a user may
    # configure (openrouter, x.ai, …) are deliberately not listed — add the ones
    # you actually use.
    "opencode.ai"
    
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
    # Google sign-in for Gemini CLI: consent screen, token exchange, and the
    # host its pasted-code flow redirects to.
    "accounts.google.com"
    "oauth2.googleapis.com"
    "codeassist.google.com"
    
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
    # Every A record, not just the first.
    #
    # Taking only the first worked, but only by relying on something nothing
    # guarantees: that the resolver returns addresses in a stable order and that
    # every client then picks the same one. Measured here, glibc returns the
    # same first address on repeated lookups and curl connects to it — so a
    # single allowed IP sufficed. That breaks with `options rotate` in
    # resolv.conf, a different resolver, or a client that walks the address list
    # itself (Node's happy-eyeballs does), where the connection lands on an
    # address that was never allowed and is dropped.
    #
    # Allowing the full set removes that dependency. It does not make the
    # allowlist correct: the set is resolved once at container start, so an
    # address the provider rotates in later is still denied, and IPv6 is not
    # covered at all. See the tracking issue.
    getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | sort -u
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
    ips=$(resolve_domain "$domain")
    if [ -n "$ips" ]; then
        count=0
        for ip in $ips; do
            iptables -A OUTPUT -d "$ip" -j ACCEPT
            count=$((count + 1))
        done
        if [ "$count" -eq 1 ]; then
            ui_ok "Allowed: $domain ($ips)"
        else
            # Listing all of them would run to ~180 characters for a registry
            # with a dozen records; the count is what tells you the allowlist
            # widened as intended.
            ui_ok "Allowed: $domain ($count addresses)"
        fi
    else
        ui_warn "Could not resolve: $domain"
    fi
done

# Default deny.
#
# REJECT, not DROP: a dropped packet gives the client nothing to react to, so a
# blocked request sits until its own timeout expires. Measured in this image,
# curl took 12.0 s to fail with "Connection timed out" under DROP and 0.2 s to
# fail with "Couldn't connect to server" under REJECT. The agent that hangs for
# twelve seconds tells the user nothing; the immediate refusal at least says a
# connection was refused rather than lost.
#
# Deliberately no --reject-with tcp-reset: that option requires -p tcp, so on
# this catch-all rule `iptables` fails with "RULE_APPEND failed (Invalid
# argument)" and appends nothing — leaving the chain with no terminal rule and
# the policy at ACCEPT. Verified: with tcp-reset the script still printed
# "Firewall initialized" while every outbound request succeeded. The default
# icmp-port-unreachable works on a protocol-agnostic rule.
iptables -A OUTPUT -j REJECT

echo "" >&2
ui_ok "Firewall initialized. Outbound traffic restricted to whitelist."
echo "" >&2
ui_info "Allowed:"
ui_info "Docker internal networks (MCP Gateway, Docker Proxy, etc.)"
ui_info "Whitelisted domains (package registries, AI APIs, etc.)"
echo "" >&2
ui_info "To add domains at runtime:"
ui_info "iptables -I OUTPUT -d \$(getent hosts example.com | awk '{print \$1}') -j ACCEPT"
