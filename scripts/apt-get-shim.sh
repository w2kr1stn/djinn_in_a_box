#!/bin/sh
id_bin='/usr/bin/id'
real_apt='/usr/bin/apt-get'

# Root callers retain the original argv, environment, streams and exit status.
if [ "$("$id_bin" -u)" = 0 ]; then
    exec "$real_apt" "$@"
fi

helper_path='/usr/local/libexec/djinn-apt-root'
sudo_target='root'
readonly id_bin real_apt helper_path sudo_target
LC_ALL=C
export LC_ALL

for token do
    case "$token" in
        -h|--help)
            cat <<'HELP'
Usage: apt-get [FLAG...] update
       apt-get [FLAG...] install [FLAG...] NAME [NAME...]
Flags may appear anywhere: -y, --yes, --assume-yes, -q, --quiet, -qq,
--no-install-recommends. -h or --help anywhere prints this help.
Names: ASCII [a-z0-9][a-z0-9+.-]*, at least two characters, at most 128 bytes; no .deb files.
-y and --no-install-recommends are always in force; installs never remove packages.
This shim elevates through sudo to /usr/local/libexec/djinn-apt-root as root.
The root helper records successful installs in /var/lib/djinn-packages/packages.txt.
Agents cannot edit it. A human stops an entry replaying with:
sudo sed -i '/^NAME$/d' /var/lib/djinn-packages/packages.txt
/usr/bin/apt-get is the unshimmed tool (unprivileged when run as dev).
See the README section "Runtime Packages".
HELP
            exit 0
            ;;
    esac
done

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
    printf 'apt-get: %s' "$1" >&2
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

quiet=0
operation=''
# The for-loop snapshots the original argv. Shift each original token, appending
# validated names to the remaining argv; only names remain when the loop ends.
for token do
    shift
    case "$token" in
        -y|--yes|--assume-yes|--no-install-recommends) ;;
        -q|--quiet) quiet=$((quiet + 1)) ;;
        -qq) quiet=$((quiet + 2)) ;;
        -*) refuse 'unsupported flag' "$token" ;;
        *)
            if [ -z "$operation" ]; then
                case "$token" in
                    update|install) operation=$token ;;
                    *) refuse 'expected update or install' "$token" ;;
                esac
            else
                [ "${#token}" -le 128 ] || refuse 'package name longer than 128 bytes' "$token"
                case "$token" in
                    ''|?|[!a-z0-9]*|*[!a-z0-9+.-]*|*.deb) refuse 'invalid package name' "$token" ;;
                esac
                set -- "$@" "$token"
            fi
            ;;
    esac
    [ "$quiet" -le 2 ] || quiet=2
done
[ -n "$operation" ] || refuse 'expected update or install'
[ "$operation" != update ] || [ "$#" -eq 0 ] || refuse 'update takes no package names'
[ "$operation" != install ] || [ "$#" -gt 0 ] || refuse 'install needs package names'

elevate() {
    set -- "$operation" "$@"
    case "$quiet" in 1) set -- -q "$@" ;; 2) set -- -qq "$@" ;; esac
    printf 'apt-get: using sudo to run %s as root.\n' "$helper_path" >&2
    sudo -n -u "$sudo_target" -- "$helper_path" "$@"
}

elevate "$@"
exit $?
