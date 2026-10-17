#!/bin/bash
# PostgreSQL Client (psql, pg_dump, pg_restore, etc.)
# Uses official PostgreSQL apt repo for latest version (not Debian's older packages)
set -e

INSTALL_BIN="${TOOLS_BIN:-$HOME/.cache/djinn-tools/bin}"
INSTALL_LIB="${TOOLS_LIB:-$HOME/.cache/djinn-tools/lib}"

install_psql_binaries() {
    local pg_bin_dir="$1"
    local real_bin_dir="${TOOLS_DIR:-$HOME/.cache/djinn-tools}/postgresql/bin"
    local bin quoted_lib quoted_bin

    if [[ ! -d "$pg_bin_dir" ]]; then
        echo "Warning: PostgreSQL bin directory not found at $pg_bin_dir"
        return 1
    fi

    for bin in psql pg_dump pg_restore pg_dumpall; do
        if [[ ! -f "$pg_bin_dir/$bin" ]]; then
            echo "Error: PostgreSQL binary not found at $pg_bin_dir/$bin" >&2
            return 1
        fi
    done

    mkdir -p "$INSTALL_BIN" "$INSTALL_LIB" "$real_bin_dir"
    [[ "$INSTALL_LIB" == /* ]] || INSTALL_LIB="$PWD/$INSTALL_LIB"
    [[ "$real_bin_dir" == /* ]] || real_bin_dir="$PWD/$real_bin_dir"

    # Non-local: EXIT can run after the function's local scope has unwound.
    psql_staged=""
    trap 'rm -f "$psql_staged"' EXIT
    for bin in psql pg_dump pg_restore pg_dumpall; do
        # Stage beside each destination so readers never see partial files.
        psql_staged=$(mktemp "$real_bin_dir/.$bin.XXXXXX")
        cp "$pg_bin_dir/$bin" "$psql_staged"
        chmod 755 "$psql_staged"
        mv -fT "$psql_staged" "$real_bin_dir/$bin"
        psql_staged=""

        # Escape single quotes for literal, absolute paths in POSIX sh.
        quoted_lib=${INSTALL_LIB//\'/\'\\\'\'}
        quoted_bin=${real_bin_dir//\'/\'\\\'\'}
        psql_staged=$(mktemp "$INSTALL_BIN/.$bin.XXXXXX")
        cat > "$psql_staged" <<WRAPPER
#!/bin/sh
LD_LIBRARY_PATH='$quoted_lib'"\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH
exec '$quoted_bin/$bin' "\$@"
WRAPPER
        chmod 755 "$psql_staged"
        mv -fT "$psql_staged" "$INSTALL_BIN/$bin"
        psql_staged=""
    done
    trap - EXIT

    for bin in pg_dump pg_restore pg_dumpall; do
        "$INSTALL_BIN/$bin" --version > /dev/null
    done
    "$INSTALL_BIN/psql" --version
}

main() {
    mkdir -p "$INSTALL_LIB"

    # Add PostgreSQL official apt repo (latest stable packages)
    if [[ ! -f /etc/apt/sources.list.d/pgdg.list ]]; then
        echo "deb http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
            | sudo tee /etc/apt/sources.list.d/pgdg.list > /dev/null
        curl -fsSL -o /tmp/pgdg.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
        sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/pgdg.gpg /tmp/pgdg.asc
        rm -f /tmp/pgdg.asc
    fi

    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends postgresql-client
    sudo rm -rf /var/lib/apt/lists/*

    # /usr/bin/psql needs PgCommon; use the latest version's real binaries instead.
    # shellcheck disable=SC2012 # PostgreSQL version directories have numeric names.
    PG_VERSION=$(ls /usr/lib/postgresql/ | sort -V | tail -1)
    PG_BIN_DIR="/usr/lib/postgresql/${PG_VERSION}/bin"

    # Copy libpq to persistent volume before verifying through the wrapper.
    find /usr/lib/ -name "libpq.so*" -exec cp -P {} "$INSTALL_LIB/" \;
    install_psql_binaries "$PG_BIN_DIR"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
