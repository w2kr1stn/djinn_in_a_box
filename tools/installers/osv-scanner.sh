#!/bin/bash
# OSV Scanner - opt-in vulnerability scanner with pinned Linux release assets.
# djinn-verify: /home/dev/.tools/installers/osv-scanner.sh --verify
#
# Optional environment:
#   TOOLS_BIN  binary directory (default: $HOME/.cache/djinn-tools/bin)
set -e

VERSION=2.7.0
INSTALL_DIR="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"

verify_binary() {
    local output
    [[ -x "$1" ]] && output=$("$1" --version </dev/null) \
        && grep -Fxq "osv-scanner version: $VERSION" <<< "$output"
}

# The cache verifier must work offline, even on an unsupported architecture.
if [[ "${1-}" == --verify ]]; then
    if verify_binary "$INSTALL_DIR/osv-scanner"; then
        exit 0
    fi
    exit 1
fi

machine=$(uname -m)
case "$machine" in
    x86_64|amd64)
        arch=amd64
        expected=160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b
        ;;
    aarch64|arm64)
        arch=arm64
        expected=4061ef4c021ed3c47f203b818f337e351336cc5cd67c0274fc36bd1dfc728dd7
        ;;
    *)
        echo "osv-scanner: Unsupported architecture '$machine'" >&2
        exit 1
        ;;
esac

mkdir -p "$INSTALL_DIR"
CURL_GUARDS=(--proto '=https' --tlsv1.2 --connect-timeout 10 --retry 4 --retry-delay 3
             --speed-limit 2048 --speed-time 30)

# Stage beside the target so every failed check preserves the existing binary.
staged=$(mktemp "$INSTALL_DIR/.osv-scanner.XXXXXX")
trap 'rm -f "$staged"' EXIT
download_url="https://github.com/google/osv-scanner/releases/download/v${VERSION}/osv-scanner_linux_${arch}"
echo "osv-scanner: Installing version $VERSION" >&2
curl -fsSL "${CURL_GUARDS[@]}" "$download_url" -o "$staged"

# Hash stdin so special characters in the cache path cannot escape the digest.
if ! actual=$(sha256sum < "$staged"); then
    echo "osv-scanner: Failed to compute SHA-256" >&2
    exit 1
fi
actual=${actual%% *}
if [[ "$expected" != "$actual" ]]; then
    echo "osv-scanner: checksum mismatch (expected '$expected', got '$actual')" >&2
    exit 1
fi

chmod 0755 "$staged"
if ! verify_binary "$staged"; then
    echo "osv-scanner: Invalid binary version (expected '$VERSION')" >&2
    exit 1
fi
# -T refuses a directory at the target instead of moving the binary into it.
mv -T "$staged" "$INSTALL_DIR/osv-scanner"
trap - EXIT

echo "osv-scanner version: $VERSION"
