#!/bin/bash
# Bun - JavaScript runtime & package manager
set -eo pipefail

INSTALL_DIR="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"

mkdir -p "$INSTALL_DIR"

# Bound metadata requests, while allowing a slow artifact transfer to finish.
CURL_GUARDS=(--proto '=https' --proto-redir '=https' --tlsv1.2
             --connect-timeout 10 --retry 4 --retry-delay 3
             --speed-limit 2048 --speed-time 30)

# Resolve version: use BUN_VERSION env var, or fetch latest from GitHub
if [[ -z "${BUN_VERSION:-}" ]]; then
    BUN_VERSION=$(curl -fsSL "${CURL_GUARDS[@]}" --max-time 30 \
        "https://api.github.com/repos/oven-sh/bun/releases/latest" \
        | jq -r '.tag_name | strings | select(startswith("bun-v")) | ltrimstr("bun-v")')
fi

if ! [[ "$BUN_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "bun: Invalid version format '${BUN_VERSION}', skipping" >&2
    exit 1
fi

version_line="$BUN_VERSION"
if installed=$("$INSTALL_DIR/bun" --version 2>/dev/null </dev/null) && [[ "$installed" == "$version_line" ]]; then
    echo "bun: Version $BUN_VERSION already installed, skipping download" >&2
    echo "$installed"
    exit 0
fi

asset="bun-linux-x64.zip"
base="https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}"
if ! expected=$(curl -fsSL "${CURL_GUARDS[@]}" --max-time 30 "$base/SHASUMS256.txt" \
        | awk -v f="$asset" '$2 == f || $2 == "*" f || $2 == "./" f {print $1}') \
        || ! [[ "$expected" =~ ^[0-9a-f]{64}$ ]]; then
    echo "bun: Missing or invalid release checksum for $asset" >&2
    exit 1
fi

staging=$(mktemp -d "$INSTALL_DIR/.bun.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
archive="$staging/$asset"
curl -fsSL "${CURL_GUARDS[@]}" "$base/$asset" > "$archive"
actual=$(sha256sum < "$archive")
if [[ "$expected" != "${actual%% *}" ]]; then
    echo "bun: checksum mismatch for $asset" >&2
    exit 1
fi

unzip -p "$archive" bun-linux-x64/bun > "$staging/bun"
chmod +x "$staging/bun"
if ! candidate=$("$staging/bun" --version </dev/null) || [[ "$candidate" != "$version_line" ]]; then
    echo "bun: Invalid binary version (expected '$version_line', got '$candidate')" >&2
    exit 1
fi

mv -T "$staging/bun" "$INSTALL_DIR/bun"
echo "$candidate"
