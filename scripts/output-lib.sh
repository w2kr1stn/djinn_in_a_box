#!/bin/zsh

# Sourceable shell UI helpers for container startup output.
#
# Color tiers:
# - COLORTERM=truecolor/24bit emits exact RGB escapes for the Python palette.
# - Other color terminals use ANSI-256 approximations chosen as nearest distinct
#   xterm indices.
# - info deliberately stays terminal-adaptive ANSI blue in both tiers.
#
# role      truecolor RGB     ANSI-256 fallback
# primary   #69B9A1          73  (#5fafaf)
# secondary #226666          23  (#005f5f)
# success   #C1FF62          155 (#afff5f)
# error     #F53263          203 (#ff5f5f)
# warning   #FAF870          227 (#ffff5f)
# info      ANSI blue        4   (terminal-adaptive basic color)
# path      #8608B8          91  (#8700af)
# muted     #333676          60  (#5f5f87, nearest dark indigo)
# border    #29526D          24  (#005f87; next distinct after secondary collision)
#
# DJINN_FORCE_UI_COLOR=1 is a test hook for non-tty subprocess tests. Per
# no-color.org, NO_COLOR disables color only when present with a non-empty value.
#
# Sourced by BOTH zsh (container entrypoint) and bash (host-side scripts like
# update-agents.sh) — keep every construct valid in both shells.

# Idempotent-source guard: the readonly constants below would error on re-source.
if [[ -n "${_DJINN_OUTPUT_LIB_LOADED:-}" ]]; then
    return 0
fi
_DJINN_OUTPUT_LIB_LOADED=1

readonly UI_COLOR_PRIMARY=73
readonly UI_COLOR_SECONDARY=23
readonly UI_COLOR_SUCCESS=155
readonly UI_COLOR_ERROR=203
readonly UI_COLOR_WARNING=227
readonly UI_COLOR_INFO=4
readonly UI_COLOR_PATH=91
readonly UI_COLOR_MUTED=60
readonly UI_COLOR_BORDER=24

_djinn_ui_color_enabled() {
    [[ -z "${NO_COLOR:-}" ]] && { [[ -t 2 ]] || [[ "${DJINN_FORCE_UI_COLOR:-}" == "1" ]]; }
}

_djinn_ui_truecolor_enabled() {
    [[ "${COLORTERM:-}" == "truecolor" || "${COLORTERM:-}" == "24bit" ]]
}

_djinn_ui_color() {
    local color=$1
    if _djinn_ui_truecolor_enabled; then
        case "$color" in
            "$UI_COLOR_PRIMARY") printf '\033[38;2;105;185;161m'; return ;;
            "$UI_COLOR_SECONDARY") printf '\033[38;2;34;102;102m'; return ;;
            "$UI_COLOR_SUCCESS") printf '\033[38;2;193;255;98m'; return ;;
            "$UI_COLOR_ERROR") printf '\033[38;2;245;50;99m'; return ;;
            "$UI_COLOR_WARNING") printf '\033[38;2;250;248;112m'; return ;;
            "$UI_COLOR_PATH") printf '\033[38;2;134;8;184m'; return ;;
            "$UI_COLOR_MUTED") printf '\033[38;2;51;54;118m'; return ;;
            "$UI_COLOR_BORDER") printf '\033[38;2;41;82;109m'; return ;;
        esac
    fi

    if (( color >= 0 && color <= 7 )); then
        printf '\033[0;3%sm' "$color"
    else
        printf '\033[38;5;%sm' "$color"
    fi
}

_djinn_ui_terminal_width() {
    local width="${DJINN_TERM_WIDTH:-}"

    if [[ -z "$width" ]] || ! [[ "$width" =~ ^[0-9]+$ ]] || (( width <= 0 )); then
        width="${COLUMNS:-}"
    fi

    if [[ -z "$width" ]] || ! [[ "$width" =~ ^[0-9]+$ ]] || (( width <= 0 )); then
        width="$(tput cols 2>/dev/null || true)"
    fi

    if [[ -z "$width" ]] || ! [[ "$width" =~ ^[0-9]+$ ]] || (( width <= 0 )); then
        width="$(stty size 2>/dev/null | awk '{print $2}' || true)"
    fi

    if [[ -z "$width" ]] || ! [[ "$width" =~ ^[0-9]+$ ]] || (( width <= 0 )); then
        width=80
    fi

    echo "$width"
}

_djinn_ui_emit() {
    local marker=$1
    local plain_marker=$2
    local color=$3
    local message=$4
    local plain_message=${5:-$message}

    if _djinn_ui_color_enabled; then
        printf '  %s%s\033[0m %s\n' "$(_djinn_ui_color "$color")" "$marker" "$message" >&2
    else
        printf '  %s %s\n' "$plain_marker" "$plain_message" >&2
    fi
}

_djinn_ui_plain_item_marker() {
    local marker=$1

    case "$marker" in
        "↻") echo "[sync]" ;;
        "⊕") echo "[merge]" ;;
        "✕") echo "[stale]" ;;
        "+") echo "[init]" ;;
        "-") echo "[off]" ;;
        "!") echo "[warn]" ;;
        *) echo "[info]" ;;
    esac
}

ui_section() {
    local title=$1
    local width
    width="$(_djinn_ui_terminal_width)"
    local prefix="─ "
    local suffix_len=$(( width - ${#title} - 3 ))
    (( suffix_len < 0 )) && suffix_len=0
    local suffix=""
    local plain_suffix=""
    local i
    for (( i = 0; i < suffix_len; i++ )); do
        suffix="${suffix}─"
        plain_suffix="${plain_suffix}-"
    done

    printf '\n' >&2
    if _djinn_ui_color_enabled; then
        printf '%s%s%s%s\033[0m %s\033[0m\n' \
            "$(_djinn_ui_color "$UI_COLOR_BORDER")" \
            "$prefix" \
            "$(_djinn_ui_color "$UI_COLOR_PRIMARY")" \
            "$title" \
            "$(_djinn_ui_color "$UI_COLOR_BORDER")$suffix" >&2
    else
        printf -- '- %s %s\n' "$title" "$plain_suffix" >&2
    fi
}

ui_ok() {
    local message=$1
    _djinn_ui_emit "✓" "[ok]" "$UI_COLOR_SUCCESS" "$message"
}

ui_warn() {
    local message=$1
    _djinn_ui_emit "⚠" "[warn]" "$UI_COLOR_WARNING" "$message"
}

ui_err() {
    local message=$1
    _djinn_ui_emit "✗" "[err]" "$UI_COLOR_ERROR" "$message"
}

ui_info() {
    local message=$1
    _djinn_ui_emit "ℹ" "[info]" "$UI_COLOR_INFO" "$message"
}

ui_boxed() {
    local title=$1
    local width
    width="$(_djinn_ui_terminal_width)"
    local frame_width=$(( width - 2 ))
    (( frame_width > 40 )) && frame_width=40
    (( frame_width < 1 )) && frame_width=1

    local color=""
    local reset=""
    local head_open
    local foot_prefix
    local rule_char
    local body_prefix

    if _djinn_ui_color_enabled; then
        color="$(_djinn_ui_color "$UI_COLOR_MUTED")"
        reset=$'\033[0m'
        head_open="╭─"
        foot_prefix="╰"
        rule_char="─"
        body_prefix="│"
    else
        head_open="+-"
        foot_prefix="+"
        rule_char="-"
        body_prefix="|"
    fi

    # Head is deliberately one rule char LONGER than the foot (user-approved
    # look). Visible head = open(2) + " title " + fill; frame chars are muted,
    # the title and body text keep the terminal default color.
    local head_fill_len=$(( frame_width - ${#title} - 4 + 2 ))
    (( head_fill_len < 0 )) && head_fill_len=0
    local foot_fill_len=$(( frame_width - ${#foot_prefix} ))
    (( foot_fill_len < 0 )) && foot_fill_len=0
    local head_fill=""
    local foot_fill=""
    local i
    for (( i = 0; i < head_fill_len; i++ )); do
        head_fill="${head_fill}${rule_char}"
    done
    for (( i = 0; i < foot_fill_len; i++ )); do
        foot_fill="${foot_fill}${rule_char}"
    done

    local line
    local opened=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if (( opened == 0 )); then
            printf '  %s%s%s %s %s%s%s\n' \
                "$color" "$head_open" "$reset" "$title" "$color" "$head_fill" "$reset" >&2
            opened=1
        fi
        printf '  %s%s%s %s\n' "$color" "$body_prefix" "$reset" "$line" >&2
    done

    if (( opened != 0 )); then
        printf '  %s%s%s%s\n' "$color" "$foot_prefix" "$foot_fill" "$reset" >&2
    fi
}

ui_item() {
    local marker=$1
    local message=$2
    local plain_marker=${3:-}
    local color=${4:-$UI_COLOR_INFO}
    local plain_message=${5:-$message}

    [[ -n "$plain_marker" ]] || plain_marker=$(_djinn_ui_plain_item_marker "$marker")
    _djinn_ui_emit "$marker" "$plain_marker" "$color" "$message" "$plain_message"
}
