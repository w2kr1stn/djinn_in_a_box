#!/bin/sh
# Root-only package boundary. No caller-selected paths, options or configuration.
id_bin='/usr/bin/id'
env_bin='/usr/bin/env'
apt_bin='/usr/bin/apt-get'
cache_bin='/usr/bin/apt-cache'
flock_bin='/usr/bin/flock'
grep_bin='/usr/bin/grep'
lock_path='/run/djinn-apt-root.lock'
lock_wait='300'
readonly id_bin env_bin apt_bin cache_bin flock_bin grep_bin lock_path lock_wait
LC_ALL=C
export LC_ALL

run_clean() {
    "$env_bin" -i PATH=/usr/sbin:/usr/bin:/sbin:/bin DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 HOME=/root "$@"
}

escape_token() {
    rest=$1
    while [ -n "$rest" ]; do
        char=${rest%"${rest#?}"}
        rest=${rest#?}
        case "$char" in
            [!\ -~]) printf '\\%03o' "'$char" ;;
            *) printf '%s' "$char" ;;
        esac
    done
}

refuse() {
    printf 'djinn-apt-root: %s' "$1" >&2
    if [ "$#" -gt 1 ]; then
        # Escaping is per byte; a long token is reported by its size only.
        if [ "${#2}" -gt 64 ]; then
            printf ' (%s-byte argument)' "${#2}" >&2
        else
            printf ' (' >&2
            escape_token "$2" >&2
            printf ')' >&2
        fi
    fi
    printf '; see apt-get --help and README section "Runtime Packages".\n' >&2
    exit 2
}

[ "$(run_clean "$id_bin" -u)" = 0 ] || refuse 'root is required'
quiet=''
case "${1-}" in -q|-qq) quiet=$1; shift ;; esac
operation=${1-}
[ "$#" -gt 0 ] || refuse 'expected update or install'
shift
case "$operation" in
    update) [ "$#" -eq 0 ] || refuse 'update takes no package names' ;;
    install) [ "$#" -gt 0 ] || refuse 'install needs package names' ;;
    *) refuse 'expected update or install' "$operation" ;;
esac
for name do
    case "$name" in
        ''|?|[!a-z0-9]*|*[!a-z0-9+.-]*|*.deb) refuse 'invalid package name' "$name" ;;
    esac
done

locked_apt() {
    if ! run_clean "$flock_bin" -w "$lock_wait" 9; then
        printf 'djinn-apt-root: cannot acquire %s within %s seconds.\n' "$lock_path" "$lock_wait" >&2
        return 100
    fi
    if [ "$operation" = install ]; then
        for name do
            if ! cache_output=$(run_clean "$cache_bin" pkgnames -- "$name" </dev/null); then
                printf 'djinn-apt-root: apt-cache failed for %s.\n' "$name" >&2
                return 100
            fi
            if ! printf '%s\n' "$cache_output" | run_clean "$grep_bin" -qxF -- "$name"; then
                printf 'E: Unable to locate package %s\nRun apt-get update and try again.\n' "$name" >&2
                return 100
            fi
        done
        set -- install -y --no-install-recommends --no-remove -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -- "$@"
    else
        set -- update
    fi
    [ -z "$quiet" ] || set -- "$quiet" "$@"
    run_clean "$apt_bin" "$@" </dev/null
}

# A group keeps fd 9 open through every cache check and the final apt child.
umask 077
lock_opened=false
{
    lock_opened=true
    locked_apt "$@"
} 9>"$lock_path"
result=$?
if [ "$lock_opened" = false ]; then
    printf 'djinn-apt-root: cannot open lock %s.\n' "$lock_path" >&2
    exit 100
fi
exit "$result"
