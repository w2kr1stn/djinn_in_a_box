#!/bin/bash
set -euo pipefail
umask 077

# File binds cannot be atomically replaced by a CLI. Give it an ephemeral copy,
# then write only refreshed credentials back through the rw bind on exit.
store=/run/djinn-credentials
case "$DJINN_ASSISTANT_AGENT" in
    claude) native="$HOME/.claude"; names=(.credentials.json);;
    codex) native="$HOME/.codex"; names=(auth.json);;
    opencode) native="$HOME/.local/share/opencode"; names=(auth.json mcp-auth.json);;
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
trap 'persist_credentials || exit 1' EXIT
child=
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
