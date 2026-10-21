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
# NOTE: This script initializes the Djinn container firewall.
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

# -----------------------------------------------------------------------------
# Allowlist mode: `init-firewall.sh --allow "TOKENS"` (see djinn-egress-init)
# -----------------------------------------------------------------------------
# Runs in the root phase before any dev code: it executes only the absolute
# paths below, reads only /proc, sources no library and prints plain stderr.
# The applied rules are read back from the kernel and must equal the expected
# text exactly before a single `djinn-egress: allow` line is printed.
_djinn_allowlist_mode() {
    local iptables_bin='/usr/sbin/iptables'
    local ip6tables_bin='/usr/sbin/ip6tables'
    local if_inet6_path='/proc/net/if_inet6'
    local system_path='/usr/sbin:/usr/bin:/sbin:/bin'
    local resolver_rule='127.0.0.11/32'
    PATH=$system_path

    _djinn_egress_fail() { echo "[err] $1" >&2; }

    if [[ $# -ne 1 ]]; then
        _djinn_egress_fail "--allow takes exactly one argument: the space-separated token list"
        return 1
    fi
    local token_list=$1
    local octet='(0|[1-9][0-9]{0,2})'
    local token_re="^${octet}\.${octet}\.${octet}\.${octet}/([1-9][0-9]?)(:([1-9][0-9]{0,4}))?\$"
    if [[ ! $token_list =~ ^[0-9./:]+(\ [0-9./:]+)*$ ]]; then
        _djinn_egress_fail "malformed allowlist (expected A.B.C.D/P or A.B.C.D/P:PORT tokens separated by single spaces)"
        return 1
    fi
    local -a tokens
    IFS=' ' read -r -a tokens <<<"$token_list"

    local -a expected=(
        "-P OUTPUT ACCEPT"
        "-A OUTPUT -d $resolver_rule -j REJECT --reject-with icmp-port-unreachable"
        "-A OUTPUT -o lo -j ACCEPT"
        "-A OUTPUT -m state --state RELATED,ESTABLISHED -j ACCEPT"
    )
    local -a rule_args=()
    local -A seen=()
    local token network port prefix o1 o2 o3 o4 ip mask last line
    local -a fields
    for token in "${tokens[@]}"; do
        if [[ ! $token =~ $token_re ]]; then
            _djinn_egress_fail "malformed allowlist token: $token"
            return 1
        fi
        o1=${BASH_REMATCH[1]} o2=${BASH_REMATCH[2]} o3=${BASH_REMATCH[3]} o4=${BASH_REMATCH[4]}
        prefix=${BASH_REMATCH[5]}
        port=${BASH_REMATCH[7]}
        if ((o1 > 255 || o2 > 255 || o3 > 255 || o4 > 255 || prefix < 8 || prefix > 32 || ${port:-1} > 65535)); then
            _djinn_egress_fail "allowlist token out of range: $token"
            return 1
        fi
        ip=$(((o1 << 24) | (o2 << 16) | (o3 << 8) | o4))
        mask=$(((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF))
        last=$((ip | (~mask & 0xFFFFFFFF)))
        if ((ip & ~mask & 0xFFFFFFFF)); then
            _djinn_egress_fail "allowlist token has host bits set: $token"
            return 1
        fi
        # 0/8, 127/8, 169.254/16, 224/4 and 240/4 are never destinations.
        if ((ip <= 0x00FFFFFF || (ip <= 0x7FFFFFFF && last >= 0x7F000000) || (ip <= 0xA9FEFFFF && last >= 0xA9FE0000) || last >= 0xE0000000)); then
            _djinn_egress_fail "allowlist token overlaps a refused block: $token"
            return 1
        fi
        network="$o1.$o2.$o3.$o4/$prefix"
        if [[ -n ${seen[$token]-} ]]; then
            _djinn_egress_fail "duplicate allowlist token: $token"
            return 1
        fi
        seen[$token]=1
        if [[ -n $port ]]; then
            rule_args+=("-d $network -p tcp --dport $port")
            expected+=("-A OUTPUT -d $network -p tcp -m tcp --dport $port -j ACCEPT")
        else
            rule_args+=("-d $network")
            expected+=("-A OUTPUT -d $network -j ACCEPT")
        fi
    done
    expected+=("-A OUTPUT -j REJECT --reject-with icmp-port-unreachable")

    # IPv4: resolver first (the only address of the embedded Docker DNS), then
    # loopback, replies, the listed destinations, terminal REJECT. No DNS rule.
    local spec
    "$iptables_bin" -F OUTPUT || { _djinn_egress_fail "iptables: flush failed"; return 1; }
    "$iptables_bin" -A OUTPUT -d "$resolver_rule" -j REJECT || { _djinn_egress_fail "iptables: resolver rule failed"; return 1; }
    "$iptables_bin" -A OUTPUT -o lo -j ACCEPT || { _djinn_egress_fail "iptables: loopback rule failed"; return 1; }
    "$iptables_bin" -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT || { _djinn_egress_fail "iptables: state rule failed"; return 1; }
    for spec in "${rule_args[@]}"; do
        # Word splitting of the fixed rule text is intended.
        # shellcheck disable=SC2086
        "$iptables_bin" -A OUTPUT $spec -j ACCEPT || { _djinn_egress_fail "iptables: destination rule failed: $spec"; return 1; }
    done
    "$iptables_bin" -A OUTPUT -j REJECT || { _djinn_egress_fail "iptables: terminal rule failed"; return 1; }

    # IPv6: loopback only. A failure is tolerated only when the namespace has no
    # IPv6 interface besides lo (nothing can leave over IPv6 then).
    local v6_ok=1 v6_out=''
    "$ip6tables_bin" -F OUTPUT \
        && "$ip6tables_bin" -A OUTPUT -o lo -j ACCEPT \
        && "$ip6tables_bin" -A OUTPUT -j REJECT || v6_ok=0
    if ((v6_ok)); then
        v6_out=$("$ip6tables_bin" -S OUTPUT) || v6_ok=0
    fi
    if ((v6_ok)); then
        local v6_expected
        printf -v v6_expected '%s\n' \
            "-P OUTPUT ACCEPT" \
            "-A OUTPUT -o lo -j ACCEPT" \
            "-A OUTPUT -j REJECT --reject-with icmp6-port-unreachable"
        if [[ $v6_out != "${v6_expected%$'\n'}" ]]; then
            _djinn_egress_fail "ip6tables: readback differs from the expected rules"
            return 1
        fi
    else
        local inet6=''
        if [[ -e $if_inet6_path ]]; then
            if [[ ! -r $if_inet6_path ]]; then
                _djinn_egress_fail "ip6tables failed and $if_inet6_path is unreadable"
                return 1
            fi
            inet6=$(<"$if_inet6_path")
        fi
        while IFS= read -r line; do
            [[ -n $line ]] || continue
            read -r -a fields <<<"$line"
            if [[ ${fields[${#fields[@]} - 1]} != lo ]]; then
                _djinn_egress_fail "ip6tables failed while IPv6 is present on an interface other than lo"
                return 1
            fi
        done <<<"$inet6"
        echo "[warn] ip6tables unavailable; IPv6 exists only on lo" >&2
    fi

    local v4_out
    v4_out=$("$iptables_bin" -S OUTPUT) || { _djinn_egress_fail "iptables: readback failed"; return 1; }
    local expected_text
    printf -v expected_text '%s\n' "${expected[@]}"
    if [[ $v4_out != "${expected_text%$'\n'}" ]]; then
        _djinn_egress_fail "iptables: readback differs from the expected rules"
        return 1
    fi

    local -a allow_lines=()
    while IFS= read -r line; do
        if [[ $line =~ ^-A\ OUTPUT\ -d\ ([0-9.]+/[0-9]+)\ -p\ tcp\ -m\ tcp\ --dport\ ([0-9]+)\ -j\ ACCEPT$ ]]; then
            allow_lines+=("djinn-egress: allow ${BASH_REMATCH[1]} tcp/${BASH_REMATCH[2]}")
        elif [[ $line =~ ^-A\ OUTPUT\ -d\ ([0-9.]+/[0-9]+)\ -j\ ACCEPT$ ]]; then
            allow_lines+=("djinn-egress: allow ${BASH_REMATCH[1]} any")
        fi
    done <<<"$v4_out"
    if ((${#allow_lines[@]})); then
        printf '%s\n' "${allow_lines[@]}" >&2
    fi
}

if [[ "${1-}" == "--allow" ]]; then
    allowlist_status=0
    _djinn_allowlist_mode "${@:2}" || allowlist_status=$?
    exit "$allowlist_status"
fi

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
    "deb.debian.org"
    "registry.npmjs.org"
    "pypi.org"
    "files.pythonhosted.org"
    
    # AI APIs (for direct API usage, not via MCP)
    "api.anthropic.com"
    "api.openai.com"
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
    
    # Sign-in token endpoints (measured: Claude Code 2.1.304, Codex 0.163.0)
    "platform.claude.com"
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
# Allow Docker internal networks
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
ui_info "Docker internal networks"
ui_info "Whitelisted domains (package registries, AI APIs, etc.)"
echo "" >&2
ui_info "To add domains at runtime:"
ui_info "iptables -I OUTPUT -d \$(getent hosts example.com | awk '{print \$1}') -j ACCEPT"
