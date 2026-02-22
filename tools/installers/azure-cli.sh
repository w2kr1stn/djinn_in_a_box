#!/bin/bash
# Microsoft Azure CLI (installed via uv for persistence)
set -e

INSTALL_BIN="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"
INSTALL_TOOLS="${TOOLS_DIR:-$HOME/.cache/djinn-tools}/uv-tools"

mkdir -p "$INSTALL_BIN" "$INSTALL_TOOLS"

# Install Azure CLI via uv tool
# - Requires Python 3.12 (3.14 not yet supported due to deprecated APIs)
# - Requires --prerelease=allow for beta dependencies (azure-batch etc.)
# - Use --force to overwrite existing installation
UV_TOOL_DIR="$INSTALL_TOOLS" UV_TOOL_BIN_DIR="$INSTALL_BIN" \
    uv tool install azure-cli --python 3.12 --prerelease=allow --force 2>&1 | tail -5

"$INSTALL_BIN/az" version --output tsv | head -1
