#!/bin/bash
# djinn-verify: pulumi version
# Pulumi Infrastructure as Code CLI
set -e

PULUMI_VERSION="${PULUMI_VERSION:-latest}"
INSTALL_ROOT="${TOOLS_DIR:-$HOME/.cache/djinn-tools}"
INSTALL_BIN="${TOOLS_BIN:-$INSTALL_ROOT/bin}"

mkdir -p "$INSTALL_BIN"

# Guard the fetch of the install script so a stalled connection fails fast
# instead of holding container startup until the kernel's retransmit timeout.
CURL_GUARDS=(--connect-timeout 10 --max-time 60 --retry 3 --retry-delay 2)

if [[ "$PULUMI_VERSION" == "latest" ]]; then
    curl -fsSL "${CURL_GUARDS[@]}" https://get.pulumi.com \
        | sh -s -- --install-root "$INSTALL_ROOT"
else
    curl -fsSL "${CURL_GUARDS[@]}" https://get.pulumi.com \
        | sh -s -- --install-root "$INSTALL_ROOT" --version "$PULUMI_VERSION"
fi

"$INSTALL_BIN/pulumi" version
