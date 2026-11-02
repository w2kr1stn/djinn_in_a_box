#!/bin/bash
# =============================================================================
# Resolve CLI Agent Versions
# =============================================================================
# No arguments: bump upstream Dockerfile defaults in a maintainer checkout.
# --print: resolve ARG=x.y.z lines read-only for djinn update (diagnostics on stderr).
# =============================================================================

set -euo pipefail

print_mode=false
if (( $# == 1 )) && [[ "$1" == "--print" ]]; then
    print_mode=true
elif (( $# != 0 )); then
    printf 'Usage: %s [--print]\n' "$0" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE="$SCRIPT_DIR/../Dockerfile"
OUTPUT_LIB="${OUTPUT_LIB:-$SCRIPT_DIR/output-lib.sh}"

# The output library path is configurable at runtime.
# shellcheck disable=SC1090
if [[ -r "$OUTPUT_LIB" ]] && source "$OUTPUT_LIB"; then
    :
else
    UI_COLOR_SUCCESS=155
    UI_COLOR_ERROR=203
    UI_COLOR_WARNING=227
    UI_COLOR_INFO=4
    _djinn_ui_color_enabled() { return 1; }
    _djinn_ui_color() {
        local color=$1
        if (( color >= 0 && color <= 7 )); then
            printf '\033[0;3%sm' "$color"
        else
            printf '\033[38;5;%sm' "$color"
        fi
    }
fi

color_start() {
    local color=$1
    if _djinn_ui_color_enabled; then
        _djinn_ui_color "$color"
    fi
}

color_reset() {
    if _djinn_ui_color_enabled; then
        printf '\033[0m'
    fi
}

color_text() {
    local color=$1
    local text=$2
    printf '%b%s%b' "$(color_start "$color")" "$text" "$(color_reset)"
}

# Packages to update (ARG name -> npm package for version lookup)
# Note: Claude Code uses the native installer (not npm), but npm registry
# versions are in sync and used here for version discovery only.
declare -A PACKAGES=(
    ["CLAUDE_CODE_VERSION"]="@anthropic-ai/claude-code"
    ["CODEX_VERSION"]="@openai/codex"
    ["OPENCODE_VERSION"]="@opencode/cli"
)

if [[ "$print_mode" == "false" ]]; then
    printf '%bFetching latest CLI agent versions...%b\n' \
        "$(color_start "$UI_COLOR_INFO")" \
        "$(color_reset)"
    echo ""
fi

# Track if any updates were made
updates_made=false
lookup_failed=false
if [[ "$print_mode" == "true" ]]; then
    exec 3>&2
else
    exec 3>/dev/null
fi

for arg_name in "${!PACKAGES[@]}"; do
    package="${PACKAGES[$arg_name]}"

    # Get current version from Dockerfile
    if [[ "$print_mode" == "false" ]]; then
        current=$(grep -oP "ARG ${arg_name}=\K[0-9.]+" "$DOCKERFILE" 2>/dev/null || echo "unknown")
    fi

    # Fetch latest version from npm
    latest=$(npm view "$package" version 2>&3 || echo "error")

    if [[ "$latest" == "error" ]]; then
        if [[ "$print_mode" == "true" ]]; then
            printf '%s: Failed to fetch version\n' "$package" >&2
            lookup_failed=true
        else
            printf '  %b\n' "$(color_text "$UI_COLOR_ERROR" "$package: Failed to fetch version")"
        fi
        continue
    fi

    # Validate semver format to prevent sed injection
    if ! [[ "$latest" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        if [[ "$print_mode" == "true" ]]; then
            printf '%s: Invalid version format %s\n' "$package" "$latest" >&2
            lookup_failed=true
        else
            printf '  %b\n' \
                "$(color_text "$UI_COLOR_ERROR" "$package: Invalid version format '${latest}', skipping")"
        fi
        continue
    fi

    if [[ "$print_mode" == "true" ]]; then
        printf '%s=%s\n' "$arg_name" "$latest"
        continue
    fi

    if [[ "$current" == "$latest" ]]; then
        printf '  %b: %s (up to date)\n' \
            "$(color_text "$UI_COLOR_SUCCESS" "$package")" \
            "$current"
    else
        printf '  %b: %s -> %b\n' \
            "$(color_text "$UI_COLOR_WARNING" "$package")" \
            "$current" \
            "$(color_text "$UI_COLOR_SUCCESS" "$latest")"

        # Update Dockerfile
        sed -i "s/ARG ${arg_name}=.*/ARG ${arg_name}=${latest}/" "$DOCKERFILE"
        updates_made=true
    fi
done

if [[ "$print_mode" == "true" ]]; then
    if [[ "$lookup_failed" == "true" ]]; then
        exit 1
    fi
    exit 0
fi

# Resolve each tool pin pair completely before touching either Dockerfile line.
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM
for tool in oh-my-posh fnm oh-my-zsh; do
    case "$tool" in
        oh-my-posh)
            arg_name=OH_MY_POSH_VERSION
            hash_name=OH_MY_POSH_SHA256
            if release=$(curl --proto '=https' --proto-redir '=https' -fsSL \
                    https://api.github.com/repos/JanDeDobbeleer/oh-my-posh/releases/latest) \
                && tag=$(jq -er '.tag_name | select(type == "string" and test("\\Av[0-9]+\\.[0-9]+\\.[0-9]+\\z"))' <<< "$release") \
                && checksums=$(curl --proto '=https' --proto-redir '=https' -fsSL \
                    "https://github.com/JanDeDobbeleer/oh-my-posh/releases/download/$tag/checksums.txt") \
                && checksum=$(awk '$2 == "posh-linux-amd64" { count++; hash = $1; if (NF != 2) bad = 1 }
                    END { if (count != 1 || bad) exit 1; print hash }' <<< "$checksums"); then
                latest=${tag#v}
            else
                printf '%s: Failed to resolve pin pair; leaving both ARGs unchanged\n' "$tool" >&2
                continue
            fi
            ;;
        fnm)
            arg_name=FNM_VERSION
            hash_name=FNM_SHA256
            if release=$(curl --proto '=https' --proto-redir '=https' -fsSL \
                    https://api.github.com/repos/Schniz/fnm/releases/latest) \
                && tag=$(jq -er '.tag_name | select(type == "string" and test("\\Av[0-9]+\\.[0-9]+\\.[0-9]+\\z"))' <<< "$release") \
                && checksum=$(jq -er '[.assets[] | select(.name == "fnm-linux.zip")]
                    | select(length == 1) | .[0].digest
                    | select(type == "string" and test("\\Asha256:[0-9a-f]{64}\\z"))
                    | ltrimstr("sha256:")' <<< "$release"); then
                latest=${tag#v}
            else
                printf '%s: Failed to resolve pin pair; leaving both ARGs unchanged\n' "$tool" >&2
                continue
            fi
            ;;
        oh-my-zsh)
            arg_name=OH_MY_ZSH_COMMIT
            hash_name=OH_MY_ZSH_SHA256
            if release=$(curl --proto '=https' --proto-redir '=https' -fsSL \
                    https://api.github.com/repos/ohmyzsh/ohmyzsh/commits/master) \
                && latest=$(jq -er '.sha | select(type == "string" and test("\\A[0-9a-f]{40}\\z"))' <<< "$release") \
                && curl --proto '=https' --proto-redir '=https' -fsSL \
                    "https://codeload.github.com/ohmyzsh/ohmyzsh/tar.gz/$latest" -o "$scratch/source.tgz" \
                && checksum=$(sha256sum "$scratch/source.tgz"); then
                checksum=${checksum%% *}
            else
                printf '%s: Failed to resolve pin pair; leaving both ARGs unchanged\n' "$tool" >&2
                continue
            fi
            ;;
    esac
    if ! [[ "$checksum" =~ ^[0-9a-f]{64}$ ]]; then
        printf '%s: Invalid checksum; leaving both ARGs unchanged\n' "$tool" >&2
        continue
    fi
    if [[ $(grep -c "^ARG ${arg_name}=" "$DOCKERFILE" || true) != 1 \
        || $(grep -c "^ARG ${hash_name}=" "$DOCKERFILE" || true) != 1 ]]; then
        printf '%s: Expected exactly one canonical ARG for each pin; leaving both ARGs unchanged\n' "$tool" >&2
        continue
    fi
    current=$(grep "^ARG ${arg_name}=" "$DOCKERFILE")
    current_hash=$(grep "^ARG ${hash_name}=" "$DOCKERFILE")
    if [[ "$current" != "ARG ${arg_name}=${latest}" || "$current_hash" != "ARG ${hash_name}=${checksum}" ]]; then
        sed -i -e "s/^ARG ${arg_name}=.*/ARG ${arg_name}=${latest}/" \
            -e "s/^ARG ${hash_name}=.*/ARG ${hash_name}=${checksum}/" "$DOCKERFILE"
        printf '  %s: %s / %s\n' "$tool" "$latest" "$checksum"
        updates_made=true
    else
        printf '  %s (up to date)\n' "$tool"
    fi
done

echo ""

if [[ "$updates_made" == "true" ]]; then
    printf '%bDockerfile updated!%b\n' \
        "$(color_start "$UI_COLOR_SUCCESS")" \
        "$(color_reset)"
    echo ""
    echo "Changes:"
    git diff --no-color "$DOCKERFILE" | head -30
    echo ""
    echo "Next steps:"
    printf '  1. %bdjinn build%b   # Build the new agent defaults and tool pins alone: move a local agent-versions.toml aside first; commit only after this succeeds\n' \
        "$(color_start "$UI_COLOR_INFO")" \
        "$(color_reset)"
    printf '  2. %bdjinn start%b   # Start container\n' \
        "$(color_start "$UI_COLOR_INFO")" \
        "$(color_reset)"
else
    printf '%bAll CLI agents and tool pins are already up to date.%b\n' \
        "$(color_start "$UI_COLOR_SUCCESS")" \
        "$(color_reset)"
fi
