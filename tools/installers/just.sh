#!/bin/bash
# just - command runner (justfiles), as used by COSMIC and many Rust projects
set -eo pipefail

INSTALL_DIR="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"

mkdir -p "$INSTALL_DIR"

# Bound metadata requests, while allowing a slow artifact transfer to finish.
CURL_GUARDS=(--proto '=https' --proto-redir '=https' --tlsv1.2
             --connect-timeout 10 --retry 4 --retry-delay 3
             --speed-limit 2048 --speed-time 30)

# Resolve version: use JUST_VERSION env var, or fetch latest from GitHub
if [[ -z "${JUST_VERSION:-}" ]]; then
    JUST_VERSION=$(curl -fsSL "${CURL_GUARDS[@]}" --max-time 30 \
        "https://api.github.com/repos/casey/just/releases/latest" \
        | jq -r '.tag_name // empty')
fi

if ! [[ "$JUST_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "just: Invalid version format '${JUST_VERSION}', skipping" >&2
    exit 1
fi

version_line="just $JUST_VERSION"
if installed=$("$INSTALL_DIR/just" --version 2>/dev/null </dev/null) && [[ "$installed" == "$version_line" ]]; then
    echo "just: Version $JUST_VERSION already installed, skipping download" >&2
    echo "$installed"
    exit 0
fi

asset="just-${JUST_VERSION}-x86_64-unknown-linux-musl.tar.gz"
base="https://github.com/casey/just/releases/download/${JUST_VERSION}"

if ! expected=$(curl -fsSL "${CURL_GUARDS[@]}" --max-time 30 "$base/SHA256SUMS" \
        | awk -v f="$asset" '$2 == f || $2 == "*" f || $2 == "./" f {print $1}') \
        || ! [[ "$expected" =~ ^[0-9a-f]{64}$ ]]; then
    echo "just: Missing or invalid release checksum for $asset" >&2
    exit 1
fi

# Stage on the target filesystem so the final move replaces the binary atomically.
staging=$(mktemp -d "$INSTALL_DIR/.just.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
archive="$staging/$asset"
curl -fsSL "${CURL_GUARDS[@]}" "$base/$asset" > "$archive"
actual=$(sha256sum < "$archive")
if [[ "$expected" != "${actual%% *}" ]]; then
    echo "just: checksum mismatch for $asset" >&2
    exit 1
fi

(cd -- "$staging" && tar -xzf "$asset" just)
chmod +x "$staging/just"
if ! candidate=$("$staging/just" --version </dev/null) || [[ "$candidate" != "$version_line" ]]; then
    echo "just: Invalid binary version (expected '$version_line', got '$candidate')" >&2
    exit 1
fi

mv -T "$staging/just" "$INSTALL_DIR/just"
echo "$candidate"
