#!/bin/bash
# SOPS - Secrets OPerationS (encrypted secrets management)
set -e

SOPS_VERSION="${SOPS_VERSION:-3.9.4}"
INSTALL_DIR="/usr/local/bin"

# Download and install SOPS binary
curl -fsSL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" -o /tmp/sops
chmod +x /tmp/sops
sudo mv /tmp/sops "$INSTALL_DIR/sops"

sops --version
