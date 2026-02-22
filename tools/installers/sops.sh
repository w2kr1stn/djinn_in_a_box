#!/bin/bash
# SOPS - Secrets OPerationS (encrypted secrets management)
set -e

INSTALL_DIR="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"

mkdir -p "$INSTALL_DIR"

# Resolve version: use SOPS_VERSION env var, or fetch latest from GitHub
if [[ -z "${SOPS_VERSION:-}" ]]; then
    SOPS_VERSION=$(curl -fsSL "https://api.github.com/repos/getsops/sops/releases/latest" \
        | grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')
fi

# Download SOPS binary directly to persistent volume
curl -fsSL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
    -o "$INSTALL_DIR/sops"
chmod +x "$INSTALL_DIR/sops"

"$INSTALL_DIR/sops" --version
