#!/bin/zsh

# Register MCP servers for the supported CLI tools from the canonical
# mcp-servers.json registry. This file is sourceable so the entrypoint logic can
# be tested without running the whole container startup sequence.

if ! whence -w ui_section >/dev/null 2>&1; then
    _djinn_define_plain_ui_fallbacks() {
        ui_section() { echo "[info] $1" >&2; }
        ui_ok() { echo "[ok] $1" >&2; }
        ui_warn() { echo "[warn] $1" >&2; }
        ui_err() { echo "[err] $1" >&2; }
        ui_info() { echo "[info] $1" >&2; }
        ui_boxed() {
            local title=$1
            local line
            local opened=0

            while IFS= read -r line || [[ -n "$line" ]]; do
                if (( opened == 0 )); then
                    echo "  +- $title --------------------------" >&2
                    opened=1
                fi
                echo "  | $line" >&2
            done

            if (( opened != 0 )); then
                echo "  +-----------------------------------" >&2
            fi
        }
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

    _OUTPUT_LIB_DEFAULT="${${(%):-%x}:A:h}/output-lib.sh"
    OUTPUT_LIB="${OUTPUT_LIB:-$_OUTPUT_LIB_DEFAULT}"
    if [[ -r "$OUTPUT_LIB" ]] && source "$OUTPUT_LIB"; then
        :
    else
        _djinn_define_plain_ui_fallbacks
        ui_warn "output library not found at $OUTPUT_LIB; using plain startup output."
    fi
fi

_mcp_run_boxed() {
    local title=$1
    shift

    local had_errexit=0
    case "$-" in
        *e*) had_errexit=1; set +e ;;
    esac

    local output
    output="$("$@" 2>&1)"
    local cmd_status=$?

    if (( had_errexit != 0 )); then
        set -e
    fi

    if [[ -n "$output" ]]; then
        printf '%s\n' "$output" | ui_boxed "$title"
    fi

    return "$cmd_status"
}

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
        ui_warn "${server}: Codex does not support ${transport}, skipped"
        return 0
    fi

    _codex_write_streamable_http_server "$server" "$url" "$tool_timeout_sec" "$startup_timeout_sec"
}

_codex_disable_server() {
    local server=$1

    if command -v codex &>/dev/null; then
        _mcp_run_boxed "codex mcp" codex mcp remove "$server" || true
    fi
    _codex_remove_server_from_toml "$server"
}

register_mcp_servers() {
    local mcp_config="${MCP_SERVERS_CONFIG:-$HOME/.config/mcp-servers.json}"
    MCP_REACHABLE=false

    if [[ ! -f "$mcp_config" ]] || ! command -v jq &>/dev/null; then
        ui_info "[mcp] No mcp-servers.json found, skipping global MCP registration"
        return 0
    fi

    local mcp_config_name=$(basename "$mcp_config")
    if ! jq -e 'type == "object"' "$mcp_config" >/dev/null 2>&1; then
        ui_err "[mcp] Error: $mcp_config_name must contain a top-level JSON object"
        return 0
    fi

    local server_count
    server_count=$(jq 'length' "$mcp_config")
    if [[ "$server_count" == "0" ]]; then
        ui_info "[mcp] No servers configured in $mcp_config_name"
        return 0
    fi

    ui_info "[mcp] Registering servers from $mcp_config_name..."

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
            ui_warn "mcp: Skipping invalid server name: $server"
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
            ui_warn "${server}: legacy 'type' key detected; migrate local mcp-servers.json to 'transport' + 'enabled'"
            (( ++legacy ))
        fi

        if [[ "$enabled" != "true" && "$enabled" != "false" ]]; then
            ui_warn "mcp: Skipping invalid enabled value for $server: $enabled"
            (( ++skipped ))
            continue
        fi
        if [[ ! "$transport" =~ ^(streamable-http|sse)$ ]]; then
            ui_warn "mcp: Skipping invalid transport for $server: $transport"
            (( ++skipped ))
            continue
        fi
        if [[ ! "$url" =~ ^https?://[a-zA-Z0-9./_:@-]+$ ]]; then
            ui_warn "mcp: Skipping invalid URL for $server: $url"
            (( ++skipped ))
            continue
        fi
        if [[ -n "$tool_timeout_sec" && ! "$tool_timeout_sec" =~ ^[0-9]+$ ]]; then
            ui_warn "mcp: Skipping invalid tool_timeout_sec for $server: $tool_timeout_sec"
            (( ++skipped ))
            continue
        fi
        if [[ -n "$startup_timeout_sec" && ! "$startup_timeout_sec" =~ ^[0-9]+$ ]]; then
            ui_warn "mcp: Skipping invalid startup_timeout_sec for $server: $startup_timeout_sec"
            (( ++skipped ))
            continue
        fi

        if [[ "$enabled" == "false" ]]; then
            if command -v claude &>/dev/null; then
                _mcp_run_boxed "claude mcp" claude mcp remove --scope user "$server" || true
            fi
            _codex_disable_server "$server"
            jq --arg name "$server" 'del(.mcpServers[$name])' \
                "$gemini_config" > "$gemini_config.tmp" && mv "$gemini_config.tmp" "$gemini_config"
            jq --arg name "$server" 'del(.mcpServers[$name])' \
                "$opencode_config" > "$opencode_config.tmp" && mv "$opencode_config.tmp" "$opencode_config"
            ui_item "-" "${server} (disabled)"
            (( ++disabled ))
            continue
        fi

        host=$(echo "$url" | sed -E 's|https?://([^/:]+).*|\1|')

        # Warn only: registration should survive peers that come up later.
        if curl -s --connect-timeout 2 "$url" >/dev/null 2>&1; then
            [[ "$server" == "docker-gateway" ]] && MCP_REACHABLE=true
        else
            ui_warn "${server}: not reachable (${host})"
        fi

        if command -v claude &>/dev/null; then
            claude_transport="$transport"
            [[ "$transport" == "streamable-http" ]] && claude_transport="http"
            _mcp_run_boxed "claude mcp" claude mcp remove --scope user "$server" || true
            _mcp_run_boxed "claude mcp" claude mcp add --transport "$claude_transport" --scope user "$server" "$url" || true
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

        ui_ok "${server} (${transport})"
        (( ++registered ))
    done < <(jq -c 'to_entries[]' "$mcp_config")

    ui_info "[mcp] Summary: ${registered} registered, ${disabled} disabled, ${skipped} skipped, ${legacy} legacy"
}
