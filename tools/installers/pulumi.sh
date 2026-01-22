#!/bin/bash
# Pulumi Infrastructure as Code CLI
set -e

PULUMI_VERSION="${PULUMI_VERSION:-latest}"
INSTALL_DIR="$HOME/.local/bin"

mkdir -p "$INSTALL_DIR"

if [[ "$PULUMI_VERSION" == "latest" ]]; then
    curl -fsSL https://get.pulumi.com | sh -s -- --install-root "$HOME/.local"
else
    curl -fsSL https://get.pulumi.com | sh -s -- --install-root "$HOME/.local" --version "$PULUMI_VERSION"
fi

pulumi version
