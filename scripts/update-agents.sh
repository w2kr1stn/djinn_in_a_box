#!/bin/bash
# =============================================================================
# Update CLI Agent Versions in Dockerfile
# =============================================================================
# Fetches latest versions from npm and updates the Dockerfile ARG defaults.
# After running this script, rebuild the image with: djinn build
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE="$SCRIPT_DIR/../Dockerfile"
OUTPUT_LIB="${OUTPUT_LIB:-$SCRIPT_DIR/output-lib.sh}"

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
    ["GEMINI_CLI_VERSION"]="@google/gemini-cli"
    ["CODEX_VERSION"]="@openai/codex"
    ["OPENCODE_VERSION"]="opencode-ai"
)

printf '%bFetching latest CLI agent versions...%b\n' \
    "$(color_start "$UI_COLOR_INFO")" \
    "$(color_reset)"
echo ""

# Track if any updates were made
updates_made=false

for arg_name in "${!PACKAGES[@]}"; do
    package="${PACKAGES[$arg_name]}"

    # Get current version from Dockerfile
    current=$(grep -oP "ARG ${arg_name}=\K[0-9.]+" "$DOCKERFILE" 2>/dev/null || echo "unknown")

    # Fetch latest version from npm
    latest=$(npm view "$package" version 2>/dev/null || echo "error")

    if [[ "$latest" == "error" ]]; then
        printf '  %b\n' "$(color_text "$UI_COLOR_ERROR" "$package: Failed to fetch version")"
        continue
    fi

    # Validate semver format to prevent sed injection
    if ! [[ "$latest" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        printf '  %b\n' \
            "$(color_text "$UI_COLOR_ERROR" "$package: Invalid version format '${latest}', skipping")"
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
    printf '  1. %bdjinn build%b   # Rebuild image with new versions\n' \
        "$(color_start "$UI_COLOR_INFO")" \
        "$(color_reset)"
    printf '  2. %bdjinn start%b   # Start container\n' \
        "$(color_start "$UI_COLOR_INFO")" \
        "$(color_reset)"
else
    printf '%bAll CLI agents are already up to date.%b\n' \
        "$(color_start "$UI_COLOR_SUCCESS")" \
        "$(color_reset)"
fi
