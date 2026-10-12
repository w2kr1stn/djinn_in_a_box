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
list_dir='/var/lib/djinn-packages'
list_path='/var/lib/djinn-packages/packages.txt'
root_uid='0'
work_template='/run/djinn-apt-root.XXXXXX'
max_name='128'
max_names='256'
max_warnings='5'
max_bytes='1048576'
check_budget='60'
update_timeout='300'
stat_bin='/usr/bin/stat'
mktemp_bin='/usr/bin/mktemp'
mv_bin='/usr/bin/mv'
chmod_bin='/usr/bin/chmod'
sync_bin='/usr/bin/sync'
head_bin='/usr/bin/head'
tr_bin='/usr/bin/tr'
awk_bin='/usr/bin/awk'
dpkg_bin='/usr/bin/dpkg-query'
timeout_bin='/usr/bin/timeout'
date_bin='/usr/bin/date'
rm_bin='/usr/bin/rm'
tail_bin='/usr/bin/tail'
readonly id_bin env_bin apt_bin cache_bin flock_bin grep_bin lock_path lock_wait
readonly list_dir list_path root_uid work_template max_name max_names max_warnings max_bytes
readonly check_budget update_timeout stat_bin mktemp_bin mv_bin chmod_bin sync_bin head_bin
readonly tr_bin awk_bin dpkg_bin timeout_bin date_bin rm_bin tail_bin
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
[ "$#" -gt 0 ] || refuse 'expected update, install or replay'
shift
case "$operation" in
    replay)
        [ "$#" -eq 0 ] || refuse 'replay takes no arguments'
        [ -z "$quiet" ] || refuse 'replay takes no flags'
        ;;
    update) [ "$#" -eq 0 ] || refuse 'update takes no package names' ;;
    install) [ "$#" -gt 0 ] || refuse 'install needs package names' ;;
    *) refuse 'expected update, install or replay' "$operation" ;;
esac
for name do
    [ "${#name}" -le 128 ] || refuse 'package name longer than 128 bytes' "$name"
    case "$name" in
        ''|?|[!a-z0-9]*|*[!a-z0-9+.-]*|*.deb) refuse 'invalid package name' "$name" ;;
    esac
done


info() { printf 'I\t%s\n' "$1"; }
warn() { printf 'W\t%s\n' "$1"; }

# Invoked by the exit trap.
# shellcheck disable=SC2317
cleanup() {
    [ -z "$record_tmp" ] || run_clean "$rm_bin" -f -- "$record_tmp"
    [ -z "$work_dir" ] || run_clean "$rm_bin" -rf -- "$work_dir"
}

trust_directory() {
    reason='not trusted: list directory is not a real directory'
    [ -d "$list_dir" ] && [ ! -L "$list_dir" ] || return 1
    reason='not trusted: cannot stat list directory'
    metadata=$(run_clean "$stat_bin" -Lc '%u %a' -- "$list_dir") || return 1
    # stat supplies only numeric fields; split those fields deliberately.
    # shellcheck disable=SC2086
    set -- $metadata
    reason='not trusted: list directory owner'
    [ "$1" = "$root_uid" ] || return 1
    reason='not trusted: list directory is group- or world-writable'
    [ "$((0$2 & 0022))" -eq 0 ] || return 1
}

read_list() {
    trust_directory || return 1
    list_exists=false
    if [ -e "$list_path" ] || [ -L "$list_path" ]; then
        list_exists=true
        reason='not trusted: list is not a regular non-symlink file'
        [ -f "$list_path" ] && [ ! -L "$list_path" ] || return 1
        reason='not trusted: cannot open list'
        read_opened_list 3< "$list_path" || return 1
    else
        read_opened_list || return 1
    fi
}

read_opened_list() {
    if [ "$list_exists" = true ]; then
        reason='not trusted: cannot stat opened list'
        metadata=$(run_clean "$stat_bin" -Lc '%f %u %s' -- /proc/self/fd/3) || return 1
        # shellcheck disable=SC2086
        set -- $metadata
        reason='not trusted: opened list is not a regular file'
        [ "$((0x$1 & 0170000))" -eq "$((0100000))" ] || return 1
        reason='not trusted: list owner'
        [ "$2" = "$root_uid" ] || return 1
        reason='not trusted: list is group- or world-writable'
        [ "$((0x$1 & 0022))" -eq 0 ] || return 1
        reason='not trusted: list is larger than 1 MiB'
        [ "$3" -le "$max_bytes" ] || return 1
    fi
    reason='cannot create read workspace'
    work_dir=$(run_clean "$mktemp_bin" -d "$work_template") || { work_dir=''; return 1; }
    reason='not trusted: cannot read opened list'
    if [ "$list_exists" = true ]; then
        run_clean "$head_bin" -c 1048577 <&3 > "$work_dir/raw" || return 1
    else
        : > "$work_dir/raw" || return 1
    fi
    # Check the bytes actually read as well, and never hide head/tr failures in a pipeline.
    list_size=$(run_clean "$stat_bin" -c '%s' -- "$work_dir/raw") || return 1
    reason='not trusted: read exceeds 1 MiB'
    [ "$list_size" -le "$max_bytes" ] || return 1
    reason='not trusted: cannot convert list'
    run_clean "$tr_bin" '\000' '\001' < "$work_dir/raw" > "$work_dir/clean" || return 1
}

# One parser for replay and recording, including whitespace, NUL and validity rules.
# Recording reads the raw snapshot so existing bytes (including NUL) stay verbatim.
parse_list() {
    # The single-quoted program is awk, not shell.
    # shellcheck disable=SC2016
    run_clean "$env_bin" LC_ALL=C "$awk_bin" -v mode="$1" \
        -v max_name="$max_name" -v max_names="$max_names" -v max_warnings="$max_warnings" \
        -v max_bytes="$max_bytes" -v size="$list_size" -v exists="$list_exists" \
        -v counts="$work_dir/counts" '
    function parsed(line) {
        gsub(/\000/, "\001", line)
        sub(/#.*/, "", line)
        sub(/^[ \t\r\f\v]+/, "", line)
        sub(/[ \t\r\f\v]+$/, "", line)
        return line
    }
    function valid(name) {
        return length(name) <= max_name && name ~ /^[a-z0-9][a-z0-9+.-]+$/ && name !~ /\.deb$/
    }
    BEGIN {
        bytes = size
        if (mode == "record" && exists == "false") {
            header = "# Djinn runtime packages: one name per line; remove an entry with sudo (see README).\n"
            printf "%s", header
            bytes += length(header)
        }
    }
    FILENAME == ARGV[1] {
        if (mode == "record") {
            printf "%s", $0
            consumed += length($0) + 1
            unterminated = consumed > size
            if (!unterminated) printf "\n"
        }
        name = parsed($0)
        if (name == "") next
        if (!valid(name)) {
            invalid++
            if (mode == "replay" && invalid <= max_warnings) {
                shown = substr(name, 1, 32)
                gsub(/[^ -~]/, "?", shown)
                if (length(name) > 32) shown = shown "..."
                printf "I\t%d\t%s\t%d\n", FNR, shown, length(name)
            }
            next
        }
        if (seen[name]++) next
        count++
        if (mode == "replay" && count <= max_names) printf "V\t%s\n", name
        next
    }
    {
        name = $0
        if (seen[name]++) next
        extra = length(name) + 1 + unterminated
        if (count >= max_names || bytes + extra > max_bytes) { skipped++; next }
        if (unterminated) printf "\n"
        printf "%s\n", name
        unterminated = 0
        bytes += extra
        count++
    }
    END {
        if (mode == "record") print skipped + 0 > counts
        else {
            if (invalid > max_warnings) printf "S\t%d\n", invalid - max_warnings
            if (count > max_names) printf "O\t%d\n", count - max_names
        }
    }
    ' "$2" ${3+"$3"}
}

record_packages() {
    read_list || return 1
    reason='cannot stage names'
    printf '%s\n' "$@" > "$work_dir/names" || return 1
    reason='cannot create temporary list'
    record_tmp=$(run_clean "$mktemp_bin" "$list_dir/.packages.txt.XXXXXX") || { record_tmp=''; return 1; }
    reason='cannot merge list'
    parse_list record "$work_dir/raw" "$work_dir/names" > "$record_tmp" || return 1
    reason='cannot set list mode'
    run_clean "$chmod_bin" 0644 "$record_tmp" || return 1
    reason='cannot sync temporary list'
    run_clean "$sync_bin" -- "$record_tmp" || return 1
    reason='cannot replace list'
    run_clean "$mv_bin" -f -- "$record_tmp" "$list_path" || return 1
    record_tmp=''
    reason='cannot read recording count'
    IFS= read -r skipped < "$work_dir/counts" || return 1
    reason="$skipped names not recorded (256-name or 1 MiB limit)"
    [ "$skipped" -eq 0 ]
}

start_budget() {
    checks_started=$(run_clean "$date_bin" +%s) || { warn 'Cannot read check clock; replay skipped.'; return 1; }
}

within_budget() {
    now=$(run_clean "$date_bin" +%s) || { warn 'Cannot read check clock; replay skipped.'; return 1; }
    if [ "$((now - checks_started))" -gt "$check_budget" ]; then
        warn "Package checks exceeded $check_budget s; replay skipped."
        return 1
    fi
}

apt_install() {
    run_clean "$apt_bin" ${quiet:+"$quiet"} install -y --no-install-recommends --no-remove \
        -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -- "$@" </dev/null
}

warn_failure() {
    warn "Package $1 failed (status $2). Last output lines:"
    run_clean "$tail_bin" -n 3 "$work_dir/output" > "$work_dir/tail" || { warn 'Cannot read failure output.'; return; }
    while IFS= read -r line || [ -n "$line" ]; do warn "$line"; done < "$work_dir/tail"
}

replay_packages() {
    if ! read_list 2>/dev/null; then warn "$reason"; info 'none recorded (list unreadable)'; return; fi
    if ! parse_list replay "$work_dir/clean" > "$work_dir/records" 2>/dev/null; then
        warn "Cannot parse package list $list_path; replay skipped."
        return
    fi
    tab=$(printf '\t')
    set --
    while IFS="$tab" read -r tag value shown bytes; do
        case "$tag" in
            V) set -- "$@" "$value" ;;
            I) warn "Invalid package name \"$shown\" ($bytes bytes) in $list_path (line $value); skipped." ;;
            S) warn "$value more invalid lines in $list_path skipped." ;;
            O) warn "More than $max_names package names in $list_path; $value not replayed." ;;
        esac
    done < "$work_dir/records"
    total=$#
    if [ "$total" -eq 0 ]; then info 'none recorded'; return; fi
    start_budget || return
    # Shift each recorded name once; only missing names remain in argv.
    for name do
        shift
        # A sentinel preserves exact dpkg-query output, including trailing newlines.
        # shellcheck disable=SC2016
        if ! state=$(run_clean "$dpkg_bin" -W -f='${db:Status-Status}' -- "$name" </dev/null 2>/dev/null; result=$?; printf '.'; exit "$result"); then state=''; fi
        within_budget || return
        [ "$state" = installed. ] || set -- "$@" "$name"
    done
    if [ "$#" -eq 0 ]; then info "$total recorded, all installed"; return; fi
    info "installing $# of $total"
    if run_clean "$timeout_bin" "$update_timeout" "$apt_bin" -qq update </dev/null > "$work_dir/output" 2>&1; then
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in W:*|E:*) warn "$line" ;; esac
        done < "$work_dir/output"
    else
        result=$?
        if [ "$result" -eq 124 ]; then warn "Package list update timed out after $update_timeout s; replay skipped."
        else warn_failure update "$result"; fi
        return
    fi
    # Update has its own limit; restart the budget before the cache checks.
    start_budget || return
    for name do
        shift
        if ! available=$(run_clean "$cache_bin" pkgnames -- "$name" </dev/null 2>/dev/null); then
            warn "apt-cache failed for $name; package replay skipped."
            return
        fi
        within_budget || return
        if printf '%s\n' "$available" | run_clean "$grep_bin" -qxF -- "$name"; then
            set -- "$@" "$name"
        else
            warn "Unknown package $name; check apt-get update or edit $list_path with sudo; skipped."
        fi
    done
    [ "$#" -gt 0 ] || return 0
    quiet=-qq
    # No install timeout: killing apt/dpkg can leave half-configured packages.
    if apt_install "$@" > "$work_dir/output" 2>&1; then :
    else warn_failure install "$?"; fi
}

locked_apt() {
    if ! run_clean "$flock_bin" -w "$lock_wait" 9; then
        printf 'djinn-apt-root: cannot acquire %s within %s seconds.\n' "$lock_path" "$lock_wait" >&2
        return 100
    fi
    umask 022
    if [ "$operation" = replay ]; then replay_packages; return 0; fi
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
        apt_install "$@"
        result=$?
        if [ "$result" -eq 0 ]; then
            if ! record_packages "$@" >/dev/null 2>&1; then
                printf 'djinn-apt-root: installed, but not recorded: %s\n' "$reason" >&2
            fi
        fi
        return "$result"
    fi
    run_clean "$apt_bin" ${quiet:+"$quiet"} update </dev/null
}

# Keep fd 9 open through replay or install plus the atomic recording transaction.
umask 077
work_dir=''
record_tmp=''
trap cleanup EXIT
trap 'exit 100' HUP INT TERM
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
