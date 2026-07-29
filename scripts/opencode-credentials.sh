#!/usr/bin/env zsh

# Reconcile OpenCode's volume-backed credential locations with the config-root
# credential files. The entrypoint sources output-lib.sh before this helper.
ensure_opencode_credentials() {
    local credential_name
    local volume_path
    local config_path
    local set_aside_path
    local set_aside_suffix

    mkdir -p "$HOME/.local/share/opencode" "$HOME/.opencode"

    for credential_name in auth.json mcp-auth.json; do
        volume_path="$HOME/.local/share/opencode/$credential_name"
        config_path="$HOME/.opencode/$credential_name"

        # A deliberate redirect can otherwise cause a silent logout. Refuse it
        # and return nonzero so the entrypoint's `set -e` stops this start intact.
        if [[ -L "$config_path" || ( -e "$config_path" && ! -f "$config_path" ) ]]; then
            ui_err "OpenCode credential $credential_name at $config_path must be a regular file or be absent; refusing to change it."
            return 1
        fi

        if [[ -L "$volume_path" ]]; then
            if [[ ! -f "$config_path" || ! "$volume_path" -ef "$config_path" ]]; then
                ui_err "OpenCode credential $credential_name at $volume_path must be a regular file or the canonical symlink to $config_path; refusing to change it."
                return 1
            fi
        elif [[ -e "$volume_path" && ! -f "$volume_path" ]]; then
            ui_err "OpenCode credential $credential_name at $volume_path must be a regular file or the canonical symlink to $config_path; refusing to change it."
            return 1
        fi

        if [[ -f "$volume_path" && ! -L "$volume_path" ]]; then
            if [[ -f "$config_path" ]]; then
                set_aside_path="$volume_path.pre-migration"
                set_aside_suffix=1
                while true; do
                    while [[ -e "$set_aside_path" || -L "$set_aside_path" ]]; do
                        set_aside_path="$volume_path.pre-migration.$set_aside_suffix"
                        (( set_aside_suffix += 1 ))
                    done
                    mv -n -- "$volume_path" "$set_aside_path"
                    [[ ! -e "$volume_path" && ! -L "$volume_path" ]] && break
                done
                chmod 0600 "$set_aside_path"
                ui_warn "OpenCode credential conflict for $credential_name; set aside ${set_aside_path:t}."
            else
                mv -- "$volume_path" "$config_path"
                ui_info "Migrated OpenCode credential $credential_name to the config root."
            fi
        elif [[ ! -f "$config_path" ]]; then
            printf '{}' > "$config_path"
        fi

        chmod 0600 "$config_path"

        if [[ -L "$volume_path" ]]; then
            continue
        fi

        [[ -e "$volume_path" ]] && rm -f "$volume_path"
        ln -s "$config_path" "$volume_path"
    done
}
