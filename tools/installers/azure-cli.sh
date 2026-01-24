#!/bin/bash
# Microsoft Azure CLI (installed via uv for persistence)
set -e

INSTALL_BIN="${TOOLS_BIN:-$HOME/.cache/ai-dev-tools/bin}"
INSTALL_TOOLS="${TOOLS_DIR:-$HOME/.cache/ai-dev-tools}/uv-tools"

mkdir -p "$INSTALL_BIN" "$INSTALL_TOOLS"

# Install Azure CLI via uv tool (persistent venv in volume)
UV_TOOL_DIR="$INSTALL_TOOLS" UV_TOOL_BIN_DIR="$INSTALL_BIN" \
    uv tool install azure-cli 2>&1 | tail -5

"$INSTALL_BIN/az" version --output tsv | head -1
