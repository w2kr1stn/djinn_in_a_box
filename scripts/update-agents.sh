#!/bin/bash
# =============================================================================
# Update CLI Agent Versions in Dockerfile
# =============================================================================
# Fetches latest versions from npm and updates the Dockerfile ARG defaults.
# After running this script, rebuild the image with: ./dev.sh build
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE="$SCRIPT_DIR/../Dockerfile"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Packages to update
declare -A PACKAGES=(
    ["CLAUDE_CODE_VERSION"]="@anthropic-ai/claude-code"
    ["GEMINI_CLI_VERSION"]="@google/gemini-cli"
    ["CODEX_VERSION"]="@openai/codex"
)

echo -e "${BLUE}Fetching latest CLI agent versions...${NC}"
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
        echo -e "  ${RED}$package: Failed to fetch version${NC}"
        continue
    fi

    if [[ "$current" == "$latest" ]]; then
        echo -e "  ${GREEN}$package${NC}: $current (up to date)"
    else
        echo -e "  ${YELLOW}$package${NC}: $current -> ${GREEN}$latest${NC}"

        # Update Dockerfile
        sed -i "s/ARG ${arg_name}=.*/ARG ${arg_name}=${latest}/" "$DOCKERFILE"
        updates_made=true
    fi
done

echo ""

if [[ "$updates_made" == "true" ]]; then
    echo -e "${GREEN}Dockerfile updated!${NC}"
    echo ""
    echo "Changes:"
    git diff --no-color "$DOCKERFILE" | head -30
    echo ""
    echo -e "Next steps:"
    echo -e "  1. ${BLUE}./dev.sh build${NC}     # Rebuild image with new versions"
    echo -e "  2. ${BLUE}./dev.sh start${NC}    # Start container"
else
    echo -e "${GREEN}All CLI agents are already up to date.${NC}"
fi
