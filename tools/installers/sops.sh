#!/bin/bash
# SOPS - Secrets OPerationS (encrypted secrets management)
set -e

SOPS_VERSION="${SOPS_VERSION:-3.9.4}"
INSTALL_DIR="${TOOLS_BIN:-$HOME/.cache/ai-dev-tools/bin}"

mkdir -p "$INSTALL_DIR"

# Download SOPS binary directly to persistent volume
curl -fsSL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
    -o "$INSTALL_DIR/sops"
chmod +x "$INSTALL_DIR/sops"

"$INSTALL_DIR/sops" --version
