#!/bin/bash
# djinn-verify: az version
# Microsoft Azure CLI (installed via uv for persistence)
set -e

INSTALL_BIN="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"
INSTALL_TOOLS="${TOOLS_DIR:-$HOME/.cache/djinn-tools}/uv-tools"

mkdir -p "$INSTALL_BIN" "$INSTALL_TOOLS"

# Install Azure CLI via uv tool
# - Requires Python 3.12 (3.14 not yet supported due to deprecated APIs)
# - Requires --prerelease=allow for beta dependencies (azure-batch etc.)
# - Use --force to overwrite existing installation
# - UV_PYTHON_INSTALL_DIR kept on tools-cache volume to avoid cross-volume symlink breakage
#   on container recreation (ephemeral ~/.local/share/uv/python would otherwise break the venv)
UV_TOOL_DIR="$INSTALL_TOOLS" UV_TOOL_BIN_DIR="$INSTALL_BIN" \
UV_PYTHON_INSTALL_DIR="$TOOLS_DIR/python" \
    uv tool install azure-cli --python 3.12 --prerelease=allow --force 2>&1 | tail -5

"$INSTALL_BIN/az" version --output tsv | head -1
