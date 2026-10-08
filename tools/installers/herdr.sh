#!/bin/bash
# herdr - terminal workspace manager for AI coding agents
#
# Keep the release binary in the tools cache volume so container restarts reuse
# it. Verify both its published SHA-256 and its version before replacing a
# working binary; an image rebuild only downloads when the version changes.
#
# Version and digest come from the GitHub releases API rather than herdr.dev's
# update manifest: the --firewall allowlist covers GitHub, not herdr.dev, and
# herdr's own self-update verifies the same release asset digest.
#
# Optional environment:
#   TOOLS_BIN  binary directory (default: $HOME/.cache/djinn-tools/bin)
set -e

INSTALL_DIR="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"
mkdir -p "$INSTALL_DIR"

# Same stall guards as rust.sh: abort a stalled transfer so --retry can start over.
# Only metadata has a total timeout; a slow binary download must still finish.
CURL_GUARDS=(--proto '=https' --tlsv1.2 --connect-timeout 10 --retry 4 --retry-delay 3
             --speed-limit 2048 --speed-time 30)

metadata_url="https://api.github.com/repos/herdrdev/herdr/releases/latest"
if ! release=$(curl -fsSL "${CURL_GUARDS[@]}" --max-time 30 "$metadata_url"); then
    echo "herdr: Failed to fetch release metadata from $metadata_url, skipping" >&2
    exit 1
fi

if ! tag=$(printf '%s\n' "$release" | jq -r '.tag_name // empty'); then
    echo "herdr: Invalid release JSON from $metadata_url, skipping" >&2
    exit 1
fi
if ! [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "herdr: Invalid release tag '$tag', skipping" >&2
    exit 1
fi
version=${tag#v}

if ! digest=$(printf '%s\n' "$release" | jq -r \
    '[.assets[]? | select(.name == "herdr-linux-x86_64") | .digest] | if length == 1 then .[0] // empty else empty end') \
    || ! [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "herdr: Invalid SHA-256 digest for release '$tag', skipping" >&2
    exit 1
fi
expected=${digest#sha256:}

if installed=$("$INSTALL_DIR/herdr" --version 2>/dev/null </dev/null) && [[ "$installed" == "herdr $version" ]]; then
    echo "herdr: Version $version already installed, skipping download" >&2
    echo "$installed"
    exit 0
fi

# Stage next to the target so any failure leaves the existing binary untouched.
staged=$(mktemp "$INSTALL_DIR/.herdr.XXXXXX")
trap 'rm -f "$staged"' EXIT
download_url="https://github.com/herdrdev/herdr/releases/download/v${version}/herdr-linux-x86_64"
echo "herdr: Installing version $version" >&2
curl -fsSL "${CURL_GUARDS[@]}" "$download_url" -o "$staged"

# Hash stdin: sha256sum escapes file names that contain a backslash or newline.
if ! actual=$(sha256sum < "$staged"); then
    echo "herdr: Failed to compute SHA-256 for release '$tag', skipping" >&2
    exit 1
fi
actual=${actual%% *}
if [[ "$expected" != "$actual" ]]; then
    echo "herdr: checksum mismatch (expected '$expected', got '$actual'), skipping" >&2
    exit 1
fi

chmod +x "$staged"
if ! staged_version=$("$staged" --version </dev/null) || [[ "$staged_version" != "herdr $version" ]]; then
    echo "herdr: Invalid binary version (expected 'herdr $version', got '$staged_version'), skipping" >&2
    exit 1
fi
# -T: never move into a directory that happens to sit at the target path.
mv -T "$staged" "$INSTALL_DIR/herdr"
trap - EXIT

echo "$staged_version"
