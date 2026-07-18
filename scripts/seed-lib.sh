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
# overlay to prevent stale entries from persisting on the Docker volume.
#   $1 = base file    (e.g. volume settings)
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
# sync_seed: Clean-sync managed dirs/files from read-only seed to volume.
# Subdirectories are fully replaced (rm + cp). Root files are overwritten.
# A .seed-manifest tracks synced files to detect and remove stale entries.
#   $1 = label       (for logging)
#   $2 = seed_dir    (read-only bind mount from host)
#   $3 = target_dir  (persistent Docker volume)
#   $4 = config_file (path for settings.json deep-merge, optional)
# -----------------------------------------------------------------------------
sync_seed() {
    local label=$1 seed_dir=$2 target_dir=$3 config_file=${4:-}
    local manifest="${target_dir}/.seed-manifest"
    local tmp_manifest="${manifest}.tmp"

    if [[ ! -d "$seed_dir" ]] || [[ -z "$(ls -A "$seed_dir" 2>/dev/null)" ]]; then
        return
    fi

    ui_info "[seed-sync] ${label}:"
    : > "$tmp_manifest"

    # Phase 1: Clean-replace subdirectories (seed = source of truth)
    for dir in "$seed_dir"/*(N/); do
        local dirname=${dir:t}
        ui_item "↻" "${dirname}/"
        rm -rf "${target_dir}/${dirname}"
        cp -r "$dir" "${target_dir}/${dirname}"
        # No `|| true`: a swallowed manifest-append failure would poison Phase 3
        # into deleting freshly copied files as "stale" — fail loudly instead.
        find "${target_dir}/${dirname}" -type f -printf "${dirname}/%P\n" >> "$tmp_manifest"
    done

    # Phase 2: Overwrite root-level files (skip settings.json — handled via merge)
    for file in "$seed_dir"/*(N.); do
        local filename=${file:t}
        [[ "$filename" == "settings.json" ]] && continue
        ui_item "↻" "${filename}"
        cp "$file" "${target_dir}/${filename}"
        echo "$filename" >> "$tmp_manifest"
    done

    # Phase 3: Remove stale files tracked by previous manifest
    if [[ -f "$manifest" ]]; then
        while IFS= read -r entry; do
            [[ -z "$entry" ]] && continue
            if ! grep -qxF "$entry" "$tmp_manifest"; then
                if [[ -e "${target_dir}/${entry}" ]]; then
                    ui_item "✕" "${entry} (stale)"
                    rm -f "${target_dir}/${entry}"
                fi
            fi
        done < "$manifest"
        find "$target_dir" -mindepth 1 -type d -empty -delete 2>/dev/null || true
    fi

    # Phase 4: Persist new manifest
    sort -o "$manifest" "$tmp_manifest"
    rm -f "$tmp_manifest"

    # Phase 5: Deep-merge settings.json (seed wins; plugin keys fully replaced)
    if [[ -n "$config_file" ]] && [[ -f "$seed_dir/settings.json" ]]; then
        if [[ -f "$config_file" ]]; then
            ui_item "⊕" "settings.json (merged)"
            if merge_settings "$config_file" "$seed_dir/settings.json" "${config_file}.tmp"; then
                mv "${config_file}.tmp" "$config_file"
            else
                rm -f "${config_file}.tmp"
                local bad=""
                jq -e . "$config_file" >/dev/null 2>&1 || bad="$config_file"
                jq -e . "$seed_dir/settings.json" >/dev/null 2>&1 \
                    || bad="${bad:+${bad}, }${seed_dir}/settings.json"
                ui_err "settings merge failed — ${bad:-unknown input} is not valid JSON; keeping existing settings."
            fi
        else
            ui_item "+" "settings.json (init)"
            cp "$seed_dir/settings.json" "$config_file"
        fi
    fi

    # MCP servers are registered centrally from mcp-servers.json.
}

# -----------------------------------------------------------------------------
# Claude Code: generic workflow seed → ~/.claude settings.json.
# skills/commands/agents/context/scripts/CLAUDE.md + hooks are NESTED BIND-MOUNTS
# (docker-compose) — in-place editable, no copy. Only settings.json is merged here:
# the generic baseline (config/claude/settings.json, tracked) ⊕ the personal overlay
# (config/claude/settings.local.json, git-ignored) — local wins.
# -----------------------------------------------------------------------------
claude_settings_merge() {
    local seed_dir=$1 target_settings_file=$2

    if [[ ! -f "$seed_dir/CLAUDE.md" || ! -f "$seed_dir/settings.json" ]]; then
        local missing=""
        [[ -f "$seed_dir/CLAUDE.md" ]] || missing="CLAUDE.md"
        [[ -f "$seed_dir/settings.json" ]] || missing="${missing:+${missing}, }settings.json"
        ui_err "[workflow] config/claude seed incomplete (missing: ${missing}) — skipping settings merge."
        ui_info "Run \`djinn init\` on the host (or restart via \`djinn start\`, which reseeds automatically)."
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
                # A fresh volume must still get a permissions baseline — but never
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
            # NEVER clobber an existing volume settings.json with the bare baseline: a missing personal
            # overlay (e.g. a fresh git pull — settings.local.json is git-ignored) must not wipe the
            # user's prefs/marketplace. Only initialise from the baseline when no settings.json exists yet.
            [[ -f "$out" ]] || cp "$base" "$out"
        fi
    fi
}

# Reverse-sync: Copy config files from volume back to seed mounts so
# changes made inside the container are captured in the host repo.
reverse_sync_file() {
    local volume_file=$1 seed_file=$2
    local seed_dir_path
    seed_dir_path="$(dirname "$seed_file")"
    # Missing volume file / missing seed dir are normal states — skip silently.
    [[ -f "$volume_file" && -d "$seed_dir_path" ]] && {
        if [[ ! -w "$seed_dir_path" ]]; then
            # Same user outcome as a failed cp — same signal (not a silent skip).
            ui_warn "could not persist ${volume_file} → ${seed_file} (target directory not writable)"
        elif ! diff -q "$volume_file" "$seed_file" &>/dev/null; then
            # Best-effort with warning: a single failed persist must not abort the
            # session-end sync chain or clobber the shell's exit code (set -e).
            cp "$volume_file" "$seed_file" \
                || ui_warn "could not persist ${volume_file} → ${seed_file}"
        fi
    }
    return 0
}

# Claude settings need a narrow reverse-sync: workflow-owned hook fragments are
# generated from the baseline and must never become personal overlay state.
reverse_sync_claude_settings() {
    local volume_file=$1 seed_file=$2
    local seed_dir_path tmp
    seed_dir_path="$(dirname "$seed_file")"
    tmp="${seed_file}.tmp"

    # Missing volume file / missing seed dir are normal states — skip silently.
    [[ -f "$volume_file" && -d "$seed_dir_path" ]] || return 0

    if [[ ! -w "$seed_dir_path" ]]; then
        ui_warn "could not persist ${volume_file} → ${seed_file} (target directory not writable)"
        return 0
    fi

    if ! _claude_filter_managed_hooks "$volume_file" "$tmp"; then
        rm -f "$tmp"
        ui_warn "could not persist ${volume_file} → ${seed_file} (settings are not valid JSON)"
        return 0
    fi

    if [[ -f "$seed_file" ]] && diff -q "$tmp" "$seed_file" &>/dev/null; then
        rm -f "$tmp"
    elif ! mv "$tmp" "$seed_file"; then
        rm -f "$tmp"
        ui_warn "could not persist ${volume_file} → ${seed_file}"
    fi

    return 0
}
