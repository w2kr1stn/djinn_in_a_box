#!/bin/sh
# Install a native Claude release only after authenticating its version manifest.
set -eu

if [ "$#" -ne 3 ]; then
    echo "Usage: install-claude.sh ARCH VERSION DEST" >&2
    exit 1
fi
case "$1" in
    amd64) platform=linux-x64 ;;
    arm64) platform=linux-arm64 ;;
    *) echo "Unsupported Claude architecture" >&2; exit 1 ;;
esac
version=$2
destination=$3
case "$version" in *[!0-9.]*) exit 1 ;; esac
printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'
fingerprint=31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
base=https://downloads.claude.ai
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM
GNUPGHOME="$scratch/gnupg"
export GNUPGHOME
mkdir -m 0700 "$GNUPGHOME"

curl --proto '=https' --proto-redir '=https' -fsSL "$base/keys/claude-code.asc" -o "$scratch/key.asc"
curl --proto '=https' --proto-redir '=https' -fsSL "$base/claude-code-releases/$version/manifest.json" -o "$scratch/manifest.json"
curl --proto '=https' --proto-redir '=https' -fsSL "$base/claude-code-releases/$version/manifest.json.sig" -o "$scratch/manifest.json.sig"
gpg --batch --no-autostart --import "$scratch/key.asc"
gpg --batch --no-autostart --status-fd 1 --verify "$scratch/manifest.json.sig" "$scratch/manifest.json" > "$scratch/status"
if ! awk -v fingerprint="$fingerprint" '
    $1 == "[GNUPG:]" && $2 == "GOODSIG" { good = 1 }
    $1 == "[GNUPG:]" && $2 == "VALIDSIG" && $NF == fingerprint { valid = 1 }
    $1 == "[GNUPG:]" && $2 ~ /^(EXPSIG|EXPKEYSIG|REVKEYSIG|BADSIG|ERRSIG)$/ { bad = 1 }
    END { exit !(good && valid && !bad) }
' "$scratch/status"; then
    echo "Pinned release key (fingerprint) not matched or signature rejected. If the vendor rotated its key, update fingerprint in install-claude.sh after checking the documented value." >&2
    exit 1
fi
checksum=$(jq -er --arg v "$version" --arg p "$platform" 'select(.version == $v) | .platforms[$p].checksum | select(type == "string" and length == 64)' "$scratch/manifest.json")
printf '%s\n' "$checksum" | grep -Eq '^[0-9a-f]{64}$'
curl --proto '=https' --proto-redir '=https' -fsSL "$base/claude-code-releases/$version/$platform/claude" -o "$scratch/claude"
printf '%s  %s\n' "$checksum" "$scratch/claude" | sha256sum -c -
install -D -m 0755 "$scratch/claude" "$destination"
