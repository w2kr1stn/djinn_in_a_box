#!/bin/bash
# PostgreSQL Client (psql, pg_dump, pg_restore, etc.)
set -e

INSTALL_BIN="${TOOLS_BIN:-$HOME/.cache/ai-dev-tools/bin}"
INSTALL_LIB="${TOOLS_LIB:-$HOME/.cache/ai-dev-tools/lib}"

mkdir -p "$INSTALL_BIN" "$INSTALL_LIB"

# Install via APT to resolve dependencies
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends postgresql-client
sudo rm -rf /var/lib/apt/lists/*

# Copy binaries to persistent volume
for bin in psql pg_dump pg_restore pg_dumpall; do
    [[ -f "/usr/bin/$bin" ]] && cp "/usr/bin/$bin" "$INSTALL_BIN/"
done

# Copy libpq to persistent volume (required by psql)
find /usr/lib/ -name "libpq.so*" -exec cp -P {} "$INSTALL_LIB/" \;

"$INSTALL_BIN/psql" --version | head -1
