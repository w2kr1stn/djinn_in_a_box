#!/usr/bin/env zsh

# Reconcile OpenCode's volume-backed credential locations with the config-root
# credential files. The entrypoint sources output-lib.sh before this helper.
ensure_opencode_credentials() {
    local credential_name
    local volume_path
    local config_path

    mkdir -p "$HOME/.local/share/opencode" "$HOME/.opencode"

    for credential_name in auth.json mcp-auth.json; do
        volume_path="$HOME/.local/share/opencode/$credential_name"
        config_path="$HOME/.opencode/$credential_name"

        if [[ -f "$volume_path" && ! -L "$volume_path" ]]; then
            if [[ -e "$config_path" || -L "$config_path" ]]; then
                mv -- "$volume_path" "$volume_path.pre-migration"
                ui_warn "OpenCode credential conflict for $credential_name; set aside $credential_name.pre-migration."
            else
                mv -- "$volume_path" "$config_path"
                ui_info "Migrated OpenCode credential $credential_name to the config root."
            fi
        elif [[ ! -e "$config_path" && ! -L "$config_path" ]]; then
            printf '{}' > "$config_path"
        fi

        chmod 0600 "$config_path"

        if [[ -L "$volume_path" && "$volume_path" -ef "$config_path" ]]; then
            continue
        fi

        [[ -e "$volume_path" || -L "$volume_path" ]] && rm -f "$volume_path"
        ln -s "$config_path" "$volume_path"
    done
}
