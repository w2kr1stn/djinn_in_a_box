#!/bin/zsh

# Register MCP servers for the supported CLI tools from the canonical
# mcp-servers.json registry. This file is sourceable so the entrypoint logic can
# be tested without running the whole container startup sequence.

_codex_config_file() {
    echo "${CODEX_CONFIG:-$HOME/.codex/config.toml}"
}

_codex_remove_server_from_toml() {
    local server=$1
    local codex_config=$(_codex_config_file)

    [[ -f "$codex_config" ]] || return 0

    local tmp="${codex_config}.tmp"
    awk -v name="$server" '
        $0 == "[mcp_servers." name "]" { skip = 1; next }
        skip && $0 ~ /^\[/ { skip = 0 }
        !skip { print }
    ' "$codex_config" > "$tmp" && mv "$tmp" "$codex_config"
}

_codex_ensure_features() {
    local codex_config=$(_codex_config_file)
    mkdir -p "${codex_config:h}"

    if [[ ! -f "$codex_config" ]]; then
        printf '[features]\nrmcp_client = true\n' > "$codex_config"
        return
    fi

    if grep -q '^rmcp_client[[:space:]]*=' "$codex_config"; then
        return
    fi

    local tmp="${codex_config}.tmp"
    if grep -q '^\[features\]$' "$codex_config"; then
        awk '
            BEGIN { in_features = 0; inserted = 0 }
            $0 == "[features]" { in_features = 1; print; next }
            in_features && $0 ~ /^\[/ && !inserted {
                print "rmcp_client = true"
                print ""
                inserted = 1
                in_features = 0
            }
            { print }
            END {
                if (in_features && !inserted) {
                    print "rmcp_client = true"
                }
            }
        ' "$codex_config" > "$tmp" && mv "$tmp" "$codex_config"
    else
        {
            printf '[features]\nrmcp_client = true\n\n'
            cat "$codex_config"
        } > "$tmp" && mv "$tmp" "$codex_config"
    fi
}

_codex_write_streamable_http_server() {
    local server=$1
    local url=$2
    local tool_timeout_sec=${3:-}
    local startup_timeout_sec=${4:-}
    local codex_config=$(_codex_config_file)

    _codex_ensure_features
    _codex_remove_server_from_toml "$server"
    {
        printf '\n[mcp_servers.%s]\nurl = "%s"\nenabled = true\n' "$server" "$url"
        [[ -n "$startup_timeout_sec" ]] && printf 'startup_timeout_sec = %s\n' "$startup_timeout_sec"
        [[ -n "$tool_timeout_sec" ]] && printf 'tool_timeout_sec = %s\n' "$tool_timeout_sec"
    } >> "$codex_config"
}

_codex_register_server() {
    local server=$1
    local url=$2
    local transport=$3
    local tool_timeout_sec=${4:-}
    local startup_timeout_sec=${5:-}

    if [[ "$transport" != "streamable-http" ]]; then
        _codex_remove_server_from_toml "$server"
        echo "    ! ${server}: Codex does not support ${transport}, skipped"
        return 0
    fi

    _codex_write_streamable_http_server "$server" "$url" "$tool_timeout_sec" "$startup_timeout_sec"
}

_codex_disable_server() {
    local server=$1

    if command -v codex &>/dev/null; then
        codex mcp remove "$server" 2>/dev/null || true
    fi
    _codex_remove_server_from_toml "$server"
}

register_mcp_servers() {
    local mcp_config="${MCP_SERVERS_CONFIG:-$HOME/.config/mcp-servers.json}"
    MCP_REACHABLE=false

    if [[ ! -f "$mcp_config" ]] || ! command -v jq &>/dev/null; then
        echo "  [mcp] No mcp-servers.json found, skipping global MCP registration"
        return 0
    fi

    local mcp_config_name=$(basename "$mcp_config")
    if ! jq -e 'type == "object"' "$mcp_config" >/dev/null 2>&1; then
        echo "  [mcp] Error: $mcp_config_name must contain a top-level JSON object"
        return 0
    fi

    local server_count
    server_count=$(jq 'length' "$mcp_config")
    if [[ "$server_count" == "0" ]]; then
        echo "  [mcp] No servers configured in $mcp_config_name"
        return 0
    fi

    echo "  [mcp] Registering servers from $mcp_config_name..."

    local gemini_config=~/.gemini/settings.json
    local opencode_config=~/.config/opencode/.opencode.json
    mkdir -p "${gemini_config:h}" "${opencode_config:h}"
    [[ ! -f "$gemini_config" ]] && echo '{}' > "$gemini_config"
    [[ ! -f "$opencode_config" ]] && echo '{}' > "$opencode_config"

    local registered=0
    local disabled=0
    local skipped=0
    local legacy=0

    local entry server url transport enabled host claude_transport opencode_transport
    local tool_timeout_sec startup_timeout_sec
    while IFS= read -r entry; do
        server=$(jq -r '.key' <<< "$entry")

        if [[ ! "$server" =~ ^[a-zA-Z0-9_-]+$ ]]; then
            echo "    ! mcp: Skipping invalid server name: $server"
            (( ++skipped ))
            continue
        fi

        url=$(jq -r '.value.url // ""' <<< "$entry")
        transport=$(jq -r '
            if .value.transport then .value.transport
            elif .value.type == "http" then "streamable-http"
            elif .value.type == "sse" then "sse"
            else "" end
        ' <<< "$entry")
        enabled=$(jq -r 'if .value.enabled == null then "true" else (.value.enabled | tostring) end' <<< "$entry")
        tool_timeout_sec=$(jq -r '.value.tool_timeout_sec // ""' <<< "$entry")
        startup_timeout_sec=$(jq -r '.value.startup_timeout_sec // ""' <<< "$entry")

        if [[ "$(jq -r '(.value | has("type")) and (.value | has("transport") | not)' <<< "$entry")" == "true" ]]; then
            echo "    ⚠ ${server}: legacy 'type' key detected; migrate local mcp-servers.json to 'transport' + 'enabled'"
            (( ++legacy ))
        fi

        if [[ "$enabled" != "true" && "$enabled" != "false" ]]; then
            echo "    ! mcp: Skipping invalid enabled value for $server: $enabled"
            (( ++skipped ))
            continue
        fi
        if [[ ! "$transport" =~ ^(streamable-http|sse)$ ]]; then
            echo "    ! mcp: Skipping invalid transport for $server: $transport"
            (( ++skipped ))
            continue
        fi
        if [[ ! "$url" =~ ^https?://[a-zA-Z0-9./_:@-]+$ ]]; then
            echo "    ! mcp: Skipping invalid URL for $server: $url"
            (( ++skipped ))
            continue
        fi
        if [[ -n "$tool_timeout_sec" && ! "$tool_timeout_sec" =~ ^[0-9]+$ ]]; then
            echo "    ! mcp: Skipping invalid tool_timeout_sec for $server: $tool_timeout_sec"
            (( ++skipped ))
            continue
        fi
        if [[ -n "$startup_timeout_sec" && ! "$startup_timeout_sec" =~ ^[0-9]+$ ]]; then
            echo "    ! mcp: Skipping invalid startup_timeout_sec for $server: $startup_timeout_sec"
            (( ++skipped ))
            continue
        fi

        if [[ "$enabled" == "false" ]]; then
            if command -v claude &>/dev/null; then
                claude mcp remove --scope user "$server" 2>/dev/null || true
            fi
            _codex_disable_server "$server"
            jq --arg name "$server" 'del(.mcpServers[$name])' \
                "$gemini_config" > "$gemini_config.tmp" && mv "$gemini_config.tmp" "$gemini_config"
            jq --arg name "$server" 'del(.mcpServers[$name])' \
                "$opencode_config" > "$opencode_config.tmp" && mv "$opencode_config.tmp" "$opencode_config"
            echo "    - ${server} (disabled)"
            (( ++disabled ))
            continue
        fi

        host=$(echo "$url" | sed -E 's|https?://([^/:]+).*|\1|')

        # Warn only: registration should survive peers that come up later.
        if curl -s --connect-timeout 2 "$url" >/dev/null 2>&1; then
            [[ "$server" == "docker-gateway" ]] && MCP_REACHABLE=true
        else
            echo "    ⚠ ${server}: not reachable (${host})"
        fi

        if command -v claude &>/dev/null; then
            claude_transport="$transport"
            [[ "$transport" == "streamable-http" ]] && claude_transport="http"
            claude mcp remove --scope user "$server" 2>/dev/null || true
            claude mcp add --transport "$claude_transport" --scope user "$server" "$url" 2>/dev/null || true
        fi

        jq --arg name "$server" --arg url "$url" \
            '.mcpServers[$name] = {"httpUrl": $url}' \
            "$gemini_config" > "$gemini_config.tmp" && mv "$gemini_config.tmp" "$gemini_config"

        opencode_transport="$transport"
        [[ "$transport" == "streamable-http" ]] && opencode_transport="http"
        jq --arg name "$server" --arg url "$url" --arg transport "$opencode_transport" \
            '.mcpServers[$name] = {"type": $transport, "url": $url}' \
            "$opencode_config" > "$opencode_config.tmp" && mv "$opencode_config.tmp" "$opencode_config"

        _codex_register_server "$server" "$url" "$transport" "$tool_timeout_sec" "$startup_timeout_sec"

        echo "    ✓ ${server} (${transport})"
        (( ++registered ))
    done < <(jq -c 'to_entries[]' "$mcp_config")

    echo "  [mcp] Summary: ${registered} registered, ${disabled} disabled, ${skipped} skipped, ${legacy} legacy"
}
