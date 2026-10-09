#!/bin/zsh
set -euo pipefail

# =============================================================================
# Djinn in a Box - Entrypoint Script
# =============================================================================

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
# Firewall & Permissions
# -----------------------------------------------------------------------------
if [[ "${ENABLE_FIREWALL:-false}" == "true" ]]; then
    ui_info "Initializing firewall..."
    sudo /usr/local/bin/init-firewall.sh
fi

# Fix ownership of volume-mounted directories (Docker creates them as root)
for dir in ~/.cache/uv ~/.cache/djinn-tools ~/.local/share/fnm ~/.vscode-server ~/workspaces; do
    if [[ -d "$dir" ]] && [[ ! -w "$dir" ]]; then
        sudo chown -R $(id -u):$(id -g) "$dir"
    fi
done

OWNERSHIP_REPAIR_HELPER="${OWNERSHIP_REPAIR_HELPER:-/home/dev/ownership-repair.py}"
python3 "$OWNERSHIP_REPAIR_HELPER" --targets "${DJINN_DECLARED_VOLUME_TARGETS:-[]}"

# =============================================================================
# Git Configuration (container-specific paths)
# =============================================================================
# Generate public signing/trust paths; no implicit first-key selection.
GIT_CONFIG_HELPER="${GIT_CONFIG_HELPER:-/home/dev/git-config.py}"
if [[ -n "${DJINN_GIT_MANIFEST:-}" ]]; then
    python3 "$GIT_CONFIG_HELPER"
fi

# =============================================================================
# Tool Configuration & Seed Sync
# =============================================================================
ui_section "Seed & Config"
mkdir -p ~/.claude/{agents,skills,commands} ~/.codex ~/.config/opencode/commands
SEED_LIB="${SEED_LIB:-/home/dev/seed-lib.sh}"
if [[ ! -r "$SEED_LIB" ]]; then
    ui_err "seed library not found at $SEED_LIB — the image is stale or broken."
    ui_info "Rebuild it with: djinn build"
    exit 1
fi
source "$SEED_LIB"

# Copy Claude's state from the persistent config-root store.
if [[ -f "$HOME/.claude/claude.json" ]]; then
    cp "$HOME/.claude/claude.json" "$HOME/.claude.json"
fi

ui_info "[seed-sync] claude:"
claude_settings_merge "$HOME/.claude_seed" "$HOME/.claude/settings.json" >&2

# OpenCode workflow files come from the canonical seed; personal settings live
# beside that seed on the persistent parent mount and never flow back into it.
OPENCODE_RUNTIME_ROOT="${OPENCODE_RUNTIME_ROOT:-$HOME/.config/opencode}"
OPENCODE_RUNTIME_SETTINGS="$OPENCODE_RUNTIME_ROOT/.opencode.json"
OPENCODE_PERSISTENT_SETTINGS="$HOME/.opencode/.opencode.json"
SETTINGS_COPY_HELPER="${SETTINGS_COPY_HELPER:-/home/dev/settings-copy.py}"
if [[ -e "$OPENCODE_PERSISTENT_SETTINGS" || -L "$OPENCODE_PERSISTENT_SETTINGS" ]]; then
    python3 "$SETTINGS_COPY_HELPER" \
        --copy-settings "$OPENCODE_PERSISTENT_SETTINGS" "$OPENCODE_RUNTIME_SETTINGS" >&2
fi

OPENCODE_CREDENTIALS_HELPER="${OPENCODE_CREDENTIALS_HELPER:-/home/dev/opencode-credentials.sh}"
source "$OPENCODE_CREDENTIALS_HELPER"
ensure_opencode_credentials

ui_info "[workflow-delivery] opencode:"
WORKFLOW_PUBLISHER="${WORKFLOW_PUBLISHER:-/home/dev/workflow-publisher.py}"
CANONICAL_CONFIG_ROOT="${DJINN_CANONICAL_ROOT:-/home/dev/.djinn-canonical}"
OPENCODE_WORKFLOW_VIEW="${OPENCODE_WORKFLOW_VIEW:-/home/dev/.opencode/seed}"
python3 "$WORKFLOW_PUBLISHER" \
    --view "$OPENCODE_WORKFLOW_VIEW" \
    --canonical-root "$CANONICAL_CONFIG_ROOT" \
    --target "$OPENCODE_RUNTIME_ROOT" \
    --manifest "$OPENCODE_RUNTIME_ROOT/.djinn-workflow-state.json" \
    --ignore .opencode.json \
    --profile opencode >&2

# =============================================================================
# MCP Server Registration (all CLI tools, from canonical config)
# =============================================================================
ui_section "MCP"
MCP_REGISTER="${MCP_REGISTER:-/home/dev/mcp-register.sh}"
source "$MCP_REGISTER"
register_mcp_servers >&2

# =============================================================================
# Optional Tools Installation (with caching)
# =============================================================================
ui_section "Tools"
if [[ -f ~/.tools/install.sh ]]; then
    ~/.tools/install.sh >&2
fi

# =============================================================================
# Security Summary
# =============================================================================
ui_section "Security"
if [[ "${ENABLE_FIREWALL:-false}" == "true" ]]; then
    ui_ok "Firewall:     Enabled"
else
    ui_warn "Firewall:     Disabled"
fi

if [[ "${DOCKER_DIRECT:-false}" == "true" ]]; then
    # Direct socket mode: fix permissions so dev user can access immediately
    if [[ -S /var/run/docker.sock ]]; then
        SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
        if ! id -G | grep -qw "$SOCK_GID"; then
            sudo chgrp "$(id -gn)" /var/run/docker.sock
            sudo chmod g+rw /var/run/docker.sock
        fi
    fi

    ui_ok "Docker Access: Host daemon (direct socket)"
    ui_info "Socket: /var/run/docker.sock"
    ui_warn "WARNING: Full host Docker authority!"
    ui_info "All operations allowed: build, exec, push, etc."

    if docker version &>/dev/null; then
        ui_ok "Status: Connected"
    else
        ui_err "Status: Connection failed"
        ui_info "Hint: Check socket permissions (host docker GID: ${SOCK_GID:-unknown})"
    fi
elif [[ -n "${DOCKER_HOST:-}" ]]; then
    unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
    docker context use default >/dev/null 2>&1 || true
    ui_info "Docker Access: Agent daemon"
    ui_info "Endpoint: $DOCKER_HOST"
    if docker info >/dev/null 2>&1; then
        ui_ok "Status: Connected"
        ui_info "Build, run and compose use the agent daemon"
        ui_info "Published ports: agent-docker:<port>"
    else
        ui_err "Status: Agent daemon connection failed"
    fi
else
    ui_warn "Docker Access: Disabled"
    ui_info "Enable with: djinn start --docker"
fi

echo "" >&2

# =============================================================================
# Interactive Shell (reverse-sync settings on exit)
# =============================================================================
# Settings persistence has to survive BOTH ways this container ends: the
# interactive shell exiting normally, and SIGTERM from `docker stop` — the only
# way a detached container (`djinn start --detach`) is ever shut down. The signal
# path flushes changes made since the latest checkpoint.

_DJINN_STATE_PERSISTED=0
djinn_state_dir=''
djinn_checkpoint_pid=''

initialize_session_state() {
    local runtime_file acknowledged_file i
    local -a runtime_files=("$HOME/.claude.json" "$HOME/.claude/settings.json" "$OPENCODE_RUNTIME_SETTINGS")
    local -a names=(claude-state claude-settings opencode-settings)
    if ! djinn_state_dir=$(mktemp -d -t djinn-session-state.XXXXXXXX 2>/dev/null); then
        return 1
    fi
    for i in 1 2 3; do
        runtime_file=${runtime_files[$i]}
        acknowledged_file="$djinn_state_dir/${names[$i]}.ack"
        if [[ -f "$runtime_file" ]]; then
            if ! _capture_session_runtime "$runtime_file" "$acknowledged_file" 2>/dev/null; then
                rm -rf -- "$djinn_state_dir" 2>/dev/null || :
                djinn_state_dir=''
                return 1
            fi
        fi
    done
    return 0
}

sync_session_state() {
    local mode=$1
    if [[ "$mode" == final && ! -d "$djinn_state_dir" ]]; then
        if ! djinn_state_dir=$(mktemp -d -t djinn-session-state.XXXXXXXX 2>/dev/null); then
            local i
            local -a runtime_files=("$HOME/.claude.json" "$HOME/.claude/settings.json" "$OPENCODE_RUNTIME_SETTINGS")
            local -a target_files=("$HOME/.claude/claude.json" "$HOME/.claude_seed/settings.local.json" "$OPENCODE_PERSISTENT_SETTINGS")
            for i in 1 2 3; do
                if [[ -f "${runtime_files[$i]}" && -d "${target_files[$i]:h}" ]]; then
                    ui_warn "could not persist ${runtime_files[$i]} → ${target_files[$i]}"
                fi
            done
            return 0
        fi
    fi
    [[ "$mode" == checkpoint && "${djinn_checkpoint_stopping:-0}" == 1 ]] && return 0
    reverse_sync_file "$HOME/.claude.json" "$HOME/.claude/claude.json" \
        "$djinn_state_dir/claude-state.ack" "$mode"
    [[ "$mode" == checkpoint && "${djinn_checkpoint_stopping:-0}" == 1 ]] && return 0
    reverse_sync_claude_settings "$HOME/.claude/settings.json" "$HOME/.claude_seed/settings.local.json" \
        "$djinn_state_dir/claude-settings.ack" "$mode"
    [[ "$mode" == checkpoint && "${djinn_checkpoint_stopping:-0}" == 1 ]] && return 0
    reverse_sync_file "$OPENCODE_RUNTIME_SETTINGS" "$OPENCODE_PERSISTENT_SETTINGS" \
        "$djinn_state_dir/opencode-settings.ack" "$mode"
    return 0
}

_session_checkpoint_on_stop() {
    djinn_checkpoint_stopping=1
    if [[ -n "$djinn_checkpoint_sleep_pid" ]]; then
        kill -TERM "$djinn_checkpoint_sleep_pid" 2>/dev/null || :
    fi
}

_session_checkpoint_loop() {
    set -euo pipefail
    local djinn_checkpoint_stopping=0 djinn_checkpoint_sleep_pid=''
    typeset -A djinn_checkpoint_warned
    # TERM only: a background job keeps SIGINT ignored, so a Ctrl-C sent to the whole
    # process group cannot kill an in-flight copy. The parent's INT trap stops us.
    trap '_session_checkpoint_on_stop' TERM
    while [[ "$djinn_checkpoint_stopping" == 0 ]]; do
        sleep 30 </dev/null >/dev/null 2>&1 &
        djinn_checkpoint_sleep_pid=$!
        # A stop can arrive between spawning the sleeper and recording its PID.
        if [[ "$djinn_checkpoint_stopping" == 1 ]]; then
            kill -TERM "$djinn_checkpoint_sleep_pid" 2>/dev/null || :
        fi
        wait "$djinn_checkpoint_sleep_pid" 2>/dev/null || :
        djinn_checkpoint_sleep_pid=''
        [[ "$djinn_checkpoint_stopping" == 1 ]] && break
        sync_session_state checkpoint || :
    done
    return 0
}

start_session_checkpointer() {
    if ! initialize_session_state; then
        ui_warn 'settings checkpoints disabled for this session'
        return 0
    fi
    _session_checkpoint_loop </dev/null 3<&- &
    djinn_checkpoint_pid=$!
}

stop_session_checkpointer() {
    [[ -n "$djinn_checkpoint_pid" ]] || return 0
    local job rc=0
    # Signal only a live job still owned by this shell, never a reused PID.
    for job in ${(v)jobstates}; do
        if [[ "$job" == *":${djinn_checkpoint_pid}=running"* ]]; then
            kill -TERM "$djinn_checkpoint_pid" 2>/dev/null || :
            break
        fi
    done
    wait "$djinn_checkpoint_pid" 2>/dev/null || rc=$?
    djinn_checkpoint_pid=''
    [[ "$rc" == 0 ]] || ui_warn 'settings checkpoints stopped unexpectedly'
    return 0
}

persist_session_state() {
    # Idempotent: the signal path and the normal path must never both run this.
    [[ "$_DJINN_STATE_PERSISTED" == "1" ]] && return 0
    _DJINN_STATE_PERSISTED=1

    stop_session_checkpointer || :
    sync_session_state final || :
    [[ -z "$djinn_state_dir" ]] || rm -rf -- "$djinn_state_dir" 2>/dev/null || :
    return 0
}

_djinn_on_termination_signal() {
    # Persist right away rather than signalling the shell and waiting for it: an
    # interactive zsh ignores SIGTERM, so waiting would burn the entire
    # `docker stop` grace period and end in SIGKILL with nothing persisted. The
    # agent CLIs write their settings as they change, not on exit, so there is
    # nothing to flush first.
    persist_session_state
    exit $((128 + $1))
}

trap '_djinn_on_termination_signal 15' TERM
trap '_djinn_on_termination_signal 2' INT

start_session_checkpointer

# The shell runs as a background job so that `wait` stays interruptible. As a
# foreground command it would defer every trap until it returned — which under
# `docker stop` never happens, so the traps above would be dead code.
#
# stdin must be handed over explicitly. With job control off (the default for a
# non-interactive script) a background job's stdin is reassigned to /dev/null
# BEFORE any explicit redirection is applied. zsh would then not be a terminal,
# would not be interactive, would read EOF immediately and exit 0 — killing the
# container milliseconds after start. `<&3` from a descriptor duplicated
# beforehand is what restores it; `<&0` cannot, because by the time it is
# evaluated fd 0 is already /dev/null. `3<&-` keeps the spare descriptor out of
# the child.
#
# Fence set -e around the interactive shell: a non-zero shell exit must NOT abort
# the script before EXIT_CODE capture + reverse-sync (else settings persistence is silently skipped).
set +e
if [[ $# -eq 0 ]] && { [[ "${DJINN_DETACHED:-}" == "true" ]] || [[ ! -t 0 ]]; }; then
    # Two shapes end up here, and neither wants an interactive shell as PID 1.
    #
    # 1. No terminal at all: zsh would read EOF and exit within milliseconds, PID 1
    #    would follow, and the container would vanish with exit code 0 and nothing
    #    in its log. `docker compose run` picks `-T` from the *client's stdout*, so
    #    merely redirecting output is enough to land here.
    # 2. Detached (`djinn start --detach`): a TTY exists, but nobody is on it.
    #    Consumers attach with `djinn enter`, which brings its own TTY via
    #    docker exec. Leaving an unused interactive shell as PID 1 makes the whole
    #    session hostage to that terminal — one EOF on it, from a stray attach, a
    #    closed pty master, or a Ctrl-D, and the container is gone with exit 0.
    #
    # Either way a keeper is strictly better: it cannot be ended by anything
    # happening on a terminal, and `docker stop` still reaches the reverse-sync
    # through the trap below.
    if [[ "${DJINN_DETACHED:-}" == "true" ]]; then
        ui_info "Detached container — PID 1 holds it open; attach with: djinn enter"
    else
        ui_warn "No TTY available — not starting an interactive shell."
        ui_info "The container stays up; attach with: djinn enter"
    fi
    sleep infinity &
    DJINN_SHELL_PID=$!
else
    exec 3<&0
    /bin/zsh "$@" <&3 3<&- &
    DJINN_SHELL_PID=$!
    exec 3<&-
fi
wait "$DJINN_SHELL_PID"
EXIT_CODE=$?
set -e

persist_session_state
exit $EXIT_CODE
