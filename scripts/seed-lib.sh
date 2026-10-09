#!/bin/zsh

if ! whence -w ui_info >/dev/null 2>&1; then
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
fi

# -----------------------------------------------------------------------------
# merge_settings: Deep-merge two JSON settings files with selective replacement.
# Most keys are recursively merged (overlay wins on conflicts). Plugin-related
# keys (enabledPlugins, extraKnownMarketplaces) are fully replaced by the
# overlay to prevent stale entries from persisting in the persistent settings store.
#   $1 = base file    (e.g. persistent settings)
#   $2 = overlay file (e.g. seed settings — authoritative for plugin keys)
#   $3 = output file
# -----------------------------------------------------------------------------
merge_settings() {
    local base=$1 overlay=$2 output=$3
    jq -s '
      .[0] as $vol | .[1] as $seed |
      ($vol * $seed) |
      if $seed | has("enabledPlugins")
        then .enabledPlugins = $seed.enabledPlugins else . end |
      if $seed | has("extraKnownMarketplaces")
        then .extraKnownMarketplaces = $seed.extraKnownMarketplaces
      elif $vol | has("extraKnownMarketplaces")
        then del(.extraKnownMarketplaces)
      else . end
    ' "$base" "$overlay" > "$output"
}

# Claude workflow hook fragments owned by Djinn. The same filter is used in both
# directions so a generated registration can neither be overridden by the
# personal overlay nor be persisted back into it.
_claude_filter_managed_hooks() {
    local input=$1 output=$2 baseline=${3:-}
    local managed_keys='["SessionStart", "PreToolUse", "Stop"]'

    if [[ -n "$baseline" ]]; then
        jq --argjson managed_keys "$managed_keys" --slurpfile baseline "$baseline" '
          reduce $managed_keys[] as $key (
            .;
            if (($baseline[0].hooks // {}) | has($key))
              then .hooks[$key] = $baseline[0].hooks[$key]
              else del(.hooks[$key])
            end
          )
        ' "$input" > "$output"
    else
        jq --argjson managed_keys "$managed_keys" '
          reduce $managed_keys[] as $key (.; del(.hooks[$key]))
        ' "$input" > "$output"
    fi
}

# -----------------------------------------------------------------------------
# Claude Code: generic workflow seed → ~/.claude settings.json.
# skills/commands/agents/context/scripts/AGENTS.md + hooks are NESTED BIND-MOUNTS
# (docker-compose) — in-place editable, no copy. Only settings.json is merged here:
# the generic baseline (config/claude/settings.json, tracked) ⊕ the personal overlay
# (config/claude/settings.local.json, git-ignored) — local wins.
# -----------------------------------------------------------------------------
claude_settings_merge() {
    local seed_dir=$1 target_settings_file=$2

    if [[ ! -f "$seed_dir/AGENTS.md" || ! -f "$seed_dir/settings.json" ]]; then
        local missing=""
        [[ -f "$seed_dir/AGENTS.md" ]] || missing="AGENTS.md"
        [[ -f "$seed_dir/settings.json" ]] || missing="${missing:+${missing}, }settings.json"
        ui_err "[workflow] config/claude seed incomplete (missing: ${missing}) — skipping settings merge."
        ui_info "Run \`djinn init\` or \`djinn doctor --fix\` on the host."
    else
        local base="$seed_dir/settings.json" out="$target_settings_file"
        if [[ -f "$seed_dir/settings.local.json" ]]; then
            if merge_settings "$base" "$seed_dir/settings.local.json" "$out.tmp" \
                && _claude_filter_managed_hooks "$out.tmp" "$out.managed.tmp" "$base"; then
                rm -f "$out.tmp"
                mv "$out.managed.tmp" "$out" && ui_item "⊕" "settings.json (baseline ⊕ local)" "" "" "settings.json (baseline + local)"
            else
                rm -f "$out.tmp" "$out.managed.tmp"
                # jq parses BOTH inputs — pinpoint the actual offender instead of
                # blaming one unconditionally (the hand-edited baseline is at
                # least as likely to be malformed as the machine-written overlay).
                local bad=""
                jq -e . "$base" >/dev/null 2>&1 || bad="$base"
                jq -e . "$seed_dir/settings.local.json" >/dev/null 2>&1 \
                    || bad="${bad:+${bad}, }${seed_dir}/settings.local.json"
                ui_err "[workflow] settings merge failed — ${bad:-unknown input} is not valid JSON."
                ui_info "Fix it on the host under config/claude/. Keeping existing settings."
                # A fresh persistent settings store must still get a permissions baseline — but never
                # a malformed one.
                if [[ ! -f "$out" ]]; then
                    if jq -e . "$base" >/dev/null 2>&1; then
                        cp "$base" "$out"
                    else
                        ui_warn "[workflow] baseline itself is invalid — no settings.json initialised."
                    fi
                fi
            fi
        else
            ui_warn "[workflow] no settings.local.json — keeping existing settings; baseline only if none yet"
            # NEVER clobber an existing persistent settings.json with the bare baseline: a missing personal
            # overlay (e.g. a fresh git pull — settings.local.json is git-ignored) must not wipe the
            # user's prefs/marketplace. Only initialise from the baseline when no settings.json exists yet.
            [[ -f "$out" ]] || cp "$base" "$out"
        fi
    fi
}

# Warning state belongs to the worker, so scratch failures cannot reset it.
_session_sync_warn() {
    local runtime_file=$1 mode=$2 message=$3
    if [[ "$mode" == checkpoint ]]; then
        [[ -n "${djinn_checkpoint_warned[$runtime_file]:-}" ]] && return 0
        djinn_checkpoint_warned[$runtime_file]=1
    fi
    ui_warn "$message"
}

_capture_session_runtime() {
    cat -- "$1" > "$2" 2>/dev/null
}

# Validate, commit and acknowledge one capture; never re-read the live file.
_sync_session_carrier() {
    local runtime_file=$1 target_file=$2 acknowledged_file=$3 mode=$4 filter_kind=$5
    local seed_dir_path=${target_file:h} state_dir=${acknowledged_file:h}
    local capture='' filtered='' payload message
    message="could not persist ${runtime_file} → ${target_file}"
    [[ -f "$runtime_file" && -d "$seed_dir_path" ]] || return 0

    if ! capture=$(mktemp "$state_dir/capture.XXXXXXXX" 2>/dev/null); then
        _session_sync_warn "$runtime_file" "$mode" "$message"
        return 0
    fi
    if ! _capture_session_runtime "$runtime_file" "$capture" 2>/dev/null; then
        _session_sync_warn "$runtime_file" "$mode" "$message"
    elif ! jq -s -e 'length == 1' "$capture" >/dev/null 2>&1; then
        [[ "$mode" == final ]] && ui_warn "$message (settings are not valid JSON)"
    else
        payload=$capture
        if [[ "$filter_kind" == claude ]]; then
            if ! filtered=$(mktemp "$state_dir/filter.XXXXXXXX" 2>/dev/null); then
                _session_sync_warn "$runtime_file" "$mode" "$message"
                rm -f -- "$capture" 2>/dev/null || :
                return 0
            fi
            if ! _claude_filter_managed_hooks "$capture" "$filtered" 2>/dev/null; then
                [[ "$mode" == final ]] && ui_warn "$message (settings are not valid JSON)"
                rm -f -- "$capture" "$filtered" 2>/dev/null || :
                return 0
            fi
            payload=$filtered
        fi

        if [[ -f "$acknowledged_file" ]] && cmp -s "$capture" "$acknowledged_file"; then
            :
        elif [[ ! -w "$seed_dir_path" ]]; then
            _session_sync_warn "$runtime_file" "$mode" "$message (target directory not writable)"
        elif { [[ "$mode" == checkpoint ]] &&
            python3 "$SETTINGS_COPY_HELPER" --copy-settings "$payload" "$target_file" >/dev/null 2>&1; } ||
            { [[ "$mode" == final ]] &&
            python3 "$SETTINGS_COPY_HELPER" --copy-settings "$payload" "$target_file" >/dev/null; }; then
            if ! mv -f -- "$capture" "$acknowledged_file" 2>/dev/null; then
                _session_sync_warn "$runtime_file" "$mode" "$message"
            fi
        else
            if [[ "$mode" == final && "$runtime_file" == "${OPENCODE_RUNTIME_SETTINGS:-}" ]]; then
                message='could not persist OpenCode personal settings'
            fi
            _session_sync_warn "$runtime_file" "$mode" "$message"
        fi
    fi
    rm -f -- "$capture" ${filtered:+"$filtered"} 2>/dev/null || :
    return 0
}

reverse_sync_file() {
    _sync_session_carrier "$1" "$2" "$3" "$4" raw
}

reverse_sync_claude_settings() {
    _sync_session_carrier "$1" "$2" "$3" "$4" claude
}
