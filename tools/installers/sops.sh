#!/bin/bash
# SOPS - Secrets OPerationS (encrypted secrets management)
set -eo pipefail

INSTALL_DIR="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"

mkdir -p "$INSTALL_DIR"
staging=$(mktemp -d "$INSTALL_DIR/.sops.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT

# A stalled transfer would otherwise hold startup until the kernel's retransmit
# timeout fires (~15 min, observed at exactly half of the 52 MB binary).
# --speed-time aborts the stall so --retry can start over. No --max-time here:
# the binary is large and a slow line must not be mistaken for a hang.
CURL_GUARDS=(--proto '=https' --proto-redir '=https' --tlsv1.2
             --connect-timeout 10 --retry 4 --retry-delay 3
             --speed-limit 2048 --speed-time 30)

# Resolve version: use SOPS_VERSION env var, or fetch latest from GitHub
if [[ -z "${SOPS_VERSION:-}" ]]; then
    curl -fsSL "${CURL_GUARDS[@]}" --max-time 30 -o "$staging/release.json" "https://api.github.com/repos/getsops/sops/releases/latest"
    SOPS_VERSION=$(jq -r '.tag_name | strings | select(startswith("v")) | ltrimstr("v")' "$staging/release.json")
fi

if ! [[ "$SOPS_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "sops: Invalid version format '${SOPS_VERSION}', skipping" >&2
    exit 1
fi

version_line="sops $SOPS_VERSION"
if "$INSTALL_DIR/sops" --disable-version-check --version 2>/dev/null </dev/null |
        cmp -s - <(printf '%s\n' "$version_line"); then
    echo "sops: Version $SOPS_VERSION already installed, skipping download" >&2
    echo "$version_line"
    exit 0
fi

asset="sops-v${SOPS_VERSION}.linux.amd64"
base="https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}"
checksums="$staging/sops-v${SOPS_VERSION}.checksums.txt"
if ! curl -fsSL "${CURL_GUARDS[@]}" --max-time 30 -o "$checksums" "$base/sops-v${SOPS_VERSION}.checksums.txt" \
        || ! expected=$(awk -v f="$asset" 'NF == 2 && ($2 == f || $2 == "*" f || $2 == "./" f) {print $1}' "$checksums") \
        || ! [[ "$expected" =~ ^[0-9a-f]{64}$ ]]; then
    echo "sops: Missing or invalid release checksum for $asset" >&2
    exit 1
fi

staged="$staging/sops"
curl -fsSL "${CURL_GUARDS[@]}" -o "$staged" "$base/$asset"
actual=$(sha256sum < "$staged")
if [[ "$expected" != "${actual%% *}" ]]; then
    echo "sops: checksum mismatch for $asset" >&2
    exit 1
fi

chmod +x "$staged"
if ! "$staged" --disable-version-check --version </dev/null | cmp -s - <(printf '%s\n' "$version_line"); then
    echo "sops: Invalid binary version (expected '$version_line')" >&2
    exit 1
fi

mv -T "$staged" "$INSTALL_DIR/sops"
echo "$version_line"
