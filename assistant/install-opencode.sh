#!/bin/sh
# Install the same version-pinned native npm binary as the dev image, without Node.
set -eu

case "$1" in
    amd64) arch=x64-baseline ;;
    arm64) arch=arm64 ;;
    *) echo "Unsupported OpenCode architecture" >&2; exit 1 ;;
esac
version=$2
destination=$3
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM

curl --proto '=https' --proto-redir '=https' -fsSL "https://registry.npmjs.org/@opencode/cli-linux-$arch/$version" -o "$scratch/package.json"
tarball=$(jq -er '.dist.tarball | select(startswith("https://registry.npmjs.org/"))' "$scratch/package.json")
integrity=$(jq -er '.dist.integrity | select(startswith("sha512-")) | ltrimstr("sha512-")' "$scratch/package.json")
checksum=$(printf '%s' "$integrity" | base64 -d | od -An -v -tx1 | tr -d ' \n')
curl --proto '=https' --proto-redir '=https' -fsSL "$tarball" -o "$scratch/agent.tgz"
printf '%s  %s\n' "$checksum" "$scratch/agent.tgz" | sha512sum -c -
mkdir -p "$destination"
tar xzf "$scratch/agent.tgz" --strip-components=2 -C "$destination" package/bin/opencode
