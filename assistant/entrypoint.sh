#!/bin/bash
set -euo pipefail
umask 077

# File binds cannot be atomically replaced by a CLI. Give it an ephemeral copy,
# then write only refreshed credentials back through the rw bind on exit.
store=/run/djinn-credentials
case "$DJINN_ASSISTANT_AGENT" in
    claude) native="$HOME/.claude"; names=(.credentials.json);;
    codex) native="$HOME/.codex"; names=(auth.json);;
    opencode) native="$HOME/.local/share/opencode"; names=();;
    *) printf 'Unknown assistant agent\n' >&2; exit 1;;
esac
mkdir -p "$native"
for name in "${names[@]}"; do
    if [[ -f "$store/$name" && ! -L "$store/$name" ]]; then
        cp "$store/$name" "$native/$name"
        chmod 0600 "$native/$name"
    fi
done
# Claude's account metadata shares a file with settings. Import only the account.
if [[ "$DJINN_ASSISTANT_AGENT" == claude && -f "$store/claude.json" ]]; then
    jq '{oauthAccount} | with_entries(select(.value != null))' "$store/claude.json" > "$HOME/.claude.json"
fi

# Runs from the EXIT trap, which shellcheck cannot see through.
# shellcheck disable=SC2329
persist_credentials() {
    local name failed=0
    for name in "${names[@]}"; do
        if [[ -f "$store/$name" ]]; then
            if [[ -L "$native/$name" || ! -f "$native/$name" ]]; then
                printf 'Refusing redirected or missing credential: %s\n' "$name" >&2
                failed=1
            elif ! cat "$native/$name" > "$store/$name"; then
                printf 'Could not persist credential: %s\n' "$name" >&2
                failed=1
            fi
        fi
    done
    return "$failed"
}

# OpenCode keeps its logins in a database. Credential commands run with an empty private
# config home and without the agent's inline config, so no plugin or MCP server is loaded.
opencode_credentials() {
    local config variable status=0
    local -a unset_config=()
    while read -r variable; do
        if [[ $variable == OPENCODE_CONFIG* ]]; then
            unset_config+=(-u "$variable")
        fi
    done < <(compgen -e)
    config=$(mktemp -d) || return 1
    (
        cd "$config" &&
        exec env "${unset_config[@]}" XDG_CONFIG_HOME="$config" \
            OPENCODE_DISABLE_PROJECT_CONFIG=true OPENCODE_DISABLE_AUTOUPDATE=true \
            OPENCODE_DISABLE_MODELS_FETCH=true DO_NOT_TRACK=1 \
            opencode auth "$1" --standalone 2>/dev/null
    ) || status=$?
    rm -rf "$config"
    return "$status"
}

import_opencode() {
    if [[ -f "$store/seed.json" && ! -L "$store/seed.json" ]]; then
        opencode_credentials import < "$store/seed.json" > /dev/null ||
            printf 'Could not import the credential seed\n' >&2
    fi
}

# Runs from the EXIT trap, which shellcheck cannot see through.
# shellcheck disable=SC2329
export_opencode() {
    local temporary
    temporary=$(mktemp "$store/.final.XXXXXX") || return 1
    # -T: final.json is the exact destination; a planted directory link must not receive the export.
    if opencode_credentials export > "$temporary" && mv -fT -- "$temporary" "$store/final.json"; then
        return 0
    fi
    rm -f "$temporary"
    printf 'Could not export credentials\n' >&2
    return 1
}

if [[ "$DJINN_ASSISTANT_AGENT" == opencode ]]; then
    import_opencode
    trap 'export_opencode || exit 1' EXIT
else
    trap 'persist_credentials || exit 1' EXIT
fi
child=
# Runs from the signal traps, which shellcheck cannot see through.
# shellcheck disable=SC2329
terminate() {
    trap '' TERM INT HUP
    if [[ -n "$child" ]]; then
        kill -s "$1" "$child" 2>/dev/null || true
        wait "$child" || true
    fi
    exit "$2"
}
trap 'terminate TERM 143' TERM
trap 'terminate INT 130' INT
trap 'terminate HUP 129' HUP
"$@" <&0 &
child=$!
status=0
wait "$child" || status=$?
exit "$status"
