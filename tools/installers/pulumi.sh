#!/bin/bash
# Pulumi Infrastructure as Code CLI
set -e

PULUMI_VERSION="${PULUMI_VERSION:-latest}"
INSTALL_ROOT="${TOOLS_DIR:-$HOME/.cache/ai-dev-tools}"
INSTALL_BIN="${TOOLS_BIN:-$INSTALL_ROOT/bin}"

mkdir -p "$INSTALL_BIN"

if [[ "$PULUMI_VERSION" == "latest" ]]; then
    curl -fsSL https://get.pulumi.com | sh -s -- --install-root "$INSTALL_ROOT"
else
    curl -fsSL https://get.pulumi.com | sh -s -- --install-root "$INSTALL_ROOT" --version "$PULUMI_VERSION"
fi

"$INSTALL_BIN/pulumi" version
