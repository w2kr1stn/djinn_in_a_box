#!/bin/bash
# Bun - JavaScript runtime & toolkit
set -e

INSTALL_DIR="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"

mkdir -p "$INSTALL_DIR"

# Resolve version: use BUN_VERSION env var, or fetch latest from GitHub
if [[ -z "${BUN_VERSION:-}" ]]; then
    BUN_VERSION=$(curl -fsSL "https://api.github.com/repos/oven-sh/bun/releases/latest" \
        | grep '"tag_name"' | sed -E 's/.*"bun-v([^"]+)".*/\1/')
fi

# Download and extract Bun binary
TMP_DIR=$(mktemp -d)
curl -fsSL "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64.zip" \
    -o "$TMP_DIR/bun.zip"
unzip -o "$TMP_DIR/bun.zip" -d "$TMP_DIR" >/dev/null
mv "$TMP_DIR/bun-linux-x64/bun" "$INSTALL_DIR/bun"
chmod +x "$INSTALL_DIR/bun"
rm -rf "$TMP_DIR"

"$INSTALL_DIR/bun" --version
