#!/bin/bash
# just - command runner (justfiles), as used by COSMIC and many Rust projects
set -e

INSTALL_DIR="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"

mkdir -p "$INSTALL_DIR"

# Same stall guards as sops.sh: abort a stalled transfer so --retry can start over.
CURL_GUARDS=(--connect-timeout 10 --retry 4 --retry-delay 3
             --speed-limit 2048 --speed-time 30)

# Resolve version: use JUST_VERSION env var, or fetch latest from GitHub
if [[ -z "${JUST_VERSION:-}" ]]; then
    JUST_VERSION=$(curl -fsSL "${CURL_GUARDS[@]}" --max-time 30 \
        "https://api.github.com/repos/casey/just/releases/latest" \
        | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
fi

if ! [[ "$JUST_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "just: Invalid version format '${JUST_VERSION}', skipping" >&2
    exit 1
fi

asset="just-${JUST_VERSION}-x86_64-unknown-linux-musl.tar.gz"
base="https://github.com/casey/just/releases/download/${JUST_VERSION}"

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

curl -fsSL "${CURL_GUARDS[@]}" "$base/$asset" -o "$tmp_dir/$asset"
curl -fsSL "${CURL_GUARDS[@]}" --max-time 30 "$base/SHA256SUMS" -o "$tmp_dir/SHA256SUMS"

# The release publishes SHA256SUMS; refuse a download that does not match it.
expected=$(awk -v f="$asset" '$2 == f || $2 == "*" f || $2 == "./" f {print $1}' "$tmp_dir/SHA256SUMS")
actual=$(sha256sum "$tmp_dir/$asset" | awk '{print $1}')
if [[ -z "$expected" || "$expected" != "$actual" ]]; then
    echo "just: checksum mismatch for $asset (expected '${expected}', got '${actual}')" >&2
    exit 1
fi

tar -xzf "$tmp_dir/$asset" -C "$tmp_dir" just

# Stage next to the target, then move into place: a partial file must never be
# left behind as $INSTALL_DIR/just.
staged=$(mktemp "$INSTALL_DIR/.just.XXXXXX")
mv "$tmp_dir/just" "$staged"
chmod +x "$staged"
mv "$staged" "$INSTALL_DIR/just"

"$INSTALL_DIR/just" --version
