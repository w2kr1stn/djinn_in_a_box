#!/bin/bash
# PostgreSQL Client (psql, pg_dump, pg_restore, etc.)
# Uses official PostgreSQL apt repo for latest version (not Debian's older packages)
set -e

INSTALL_BIN="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"
INSTALL_LIB="${TOOLS_LIB:-$HOME/.cache/djinn-tools/lib}"

mkdir -p "$INSTALL_BIN" "$INSTALL_LIB"

# Add PostgreSQL official apt repo (latest stable packages)
if [[ ! -f /etc/apt/sources.list.d/pgdg.list ]]; then
    echo "deb http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        | sudo tee /etc/apt/sources.list.d/pgdg.list > /dev/null
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/pgdg.gpg
fi

sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends postgresql-client
sudo rm -rf /var/lib/apt/lists/*

# Find the latest PostgreSQL version installed and get real binaries
# /usr/bin/psql is a Perl wrapper that needs PgCommon — use the real binary instead
PG_VERSION=$(ls /usr/lib/postgresql/ | sort -V | tail -1)
PG_BIN_DIR="/usr/lib/postgresql/${PG_VERSION}/bin"

if [[ -d "$PG_BIN_DIR" ]]; then
    for bin in psql pg_dump pg_restore pg_dumpall; do
        [[ -f "$PG_BIN_DIR/$bin" ]] && cp "$PG_BIN_DIR/$bin" "$INSTALL_BIN/"
    done
else
    echo "Warning: PostgreSQL bin directory not found at $PG_BIN_DIR"
    exit 1
fi

# Copy libpq to persistent volume (required by psql)
find /usr/lib/ -name "libpq.so*" -exec cp -P {} "$INSTALL_LIB/" \;

"$INSTALL_BIN/psql" --version | head -1
