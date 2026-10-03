#!/usr/bin/env zsh

# Reconcile OpenCode's volume-backed credential locations with the config-root
# credential files. The entrypoint sources output-lib.sh before this helper.

# Normalize the link target lexically. Relative and dangling canonical links
# remain valid after credentials are cleared; unrelated redirects are refused.
_opencode_link_target() {
    local target
    target="$(readlink -- "$1")"
    [[ "$target" != /* ]] && target="${1:h}/$target"
    printf '%s' "${target:a}"
}
ensure_opencode_credentials() {
    local credential_name
    local volume_path
    local config_path

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
            if [[ "$(_opencode_link_target "$volume_path")" != "${config_path:a}" ]]; then
                ui_err "OpenCode credential $credential_name at $volume_path must be absent or the canonical symlink to $config_path; refusing to change it."
                return 1
            fi
        elif [[ -e "$volume_path" ]]; then
            ui_err "OpenCode credential $credential_name at $volume_path must be absent or the canonical symlink to $config_path; refusing to change it."
            return 1
        fi

        if [[ ! -f "$config_path" ]]; then
            printf '{}' > "$config_path"
        fi

        chmod 0600 "$config_path"

        if [[ -L "$volume_path" ]]; then
            continue
        fi

        ln -s "$config_path" "$volume_path"
    done
}
