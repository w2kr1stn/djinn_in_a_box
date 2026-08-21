#!/bin/bash
# SOPS - Secrets OPerationS (encrypted secrets management)
set -e

INSTALL_DIR="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"

mkdir -p "$INSTALL_DIR"

# A stalled transfer would otherwise hold startup until the kernel's retransmit
# timeout fires (~15 min, observed at exactly half of the 52 MB binary).
# --speed-time aborts the stall so --retry can start over. No --max-time here:
# the binary is large and a slow line must not be mistaken for a hang.
CURL_GUARDS=(--connect-timeout 10 --retry 4 --retry-delay 3
             --speed-limit 2048 --speed-time 30)

# Resolve version: use SOPS_VERSION env var, or fetch latest from GitHub
if [[ -z "${SOPS_VERSION:-}" ]]; then
    SOPS_VERSION=$(curl -fsSL "${CURL_GUARDS[@]}" --max-time 30 \
        "https://api.github.com/repos/getsops/sops/releases/latest" \
        | grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')
fi

if ! [[ "$SOPS_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "sops: Invalid version format '${SOPS_VERSION}', skipping" >&2
    exit 1
fi

# Stage in a temporary file in the same directory, then move into place: a
# partial transfer must never be left behind as $INSTALL_DIR/sops.
staged=$(mktemp "$INSTALL_DIR/.sops.XXXXXX")
trap 'rm -f "$staged"' EXIT

curl -fsSL "${CURL_GUARDS[@]}" \
    "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
    -o "$staged"
chmod +x "$staged"
mv "$staged" "$INSTALL_DIR/sops"
trap - EXIT

"$INSTALL_DIR/sops" --version
