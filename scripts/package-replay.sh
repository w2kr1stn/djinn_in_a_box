#!/bin/bash
# Replay package intent as dev; the root helper independently validates each call.
list_path='/home/dev/.local/share/djinn-packages/packages.txt'
helper_path='/usr/local/libexec/djinn-apt-root'
sudo_target='root'
dpkg_bin='/usr/bin/dpkg-query'
cache_bin='/usr/bin/apt-cache'
max_name='128'
max_names='256'
max_warnings='5'
check_budget='60'
update_timeout='300'
read_timeout='10'
max_bytes='1048576'
readonly list_path helper_path sudo_target dpkg_bin cache_bin
readonly max_name max_names max_warnings check_budget update_timeout read_timeout max_bytes
checks_started=$SECONDS
LC_ALL=C
export LC_ALL
OUTPUT_LIB="${OUTPUT_LIB:-/home/dev/output-lib.sh}"

define_plain_ui_fallbacks() {
    ui_info() { echo "[info] $1" >&2; }
    ui_warn() { echo "[warn] $1" >&2; }
}

# OUTPUT_LIB is an optional runtime path; plain functions cover missing libraries.
# shellcheck disable=SC1090
if [[ -r "$OUTPUT_LIB" ]] && source "$OUTPUT_LIB"; then
    :
else
    define_plain_ui_fallbacks
fi

if [[ ! -e "$list_path" ]]; then
    ui_info 'none recorded'
    exit 0
fi
# The list is dev-controlled input: a FIFO would block the start, a huge file stall it.
if [[ ! -f "$list_path" ]]; then
    ui_warn "Cannot read package list $list_path: not a regular file."
    ui_info 'none recorded (list unreadable)'
    exit 0
fi
# The check above sees the path, not the object read: the list can be replaced in
# between. So the open and the read happen in a child with a time limit, capped one
# byte over the size limit. A NUL byte would vanish in the substitution and join its
# neighbours into another name; as \001 it fails validation and the line is skipped.
if ! snapshot=$(timeout "$read_timeout" head -c "$((max_bytes + 1))" -- "$list_path" 2>/dev/null \
        | tr '\000' '\001'; status=("${PIPESTATUS[@]}"); printf '.'; [[ "${status[*]}" == "0 0" ]]); then
    ui_warn "Cannot read package list $list_path within $read_timeout s."
    ui_info 'none recorded (list unreadable)'
    exit 0
fi
snapshot=${snapshot%.}
if (( ${#snapshot} > max_bytes )); then
    ui_warn "Package list $list_path is larger than 1 MiB; replay skipped."
    exit 0
fi

# Parse once in C locale: bounds apply to bytes, and diagnostics stay printable.
if ! records=$(printf '%s' "$snapshot" | awk -v max_name="$max_name" -v max_names="$max_names" -v max_warnings="$max_warnings" '
    {
        name = $0
        sub(/#.*/, "", name)
        sub(/^[ \t\r\f\v]+/, "", name)
        sub(/[ \t\r\f\v]+$/, "", name)
        if (name == "") next
        if (length(name) > max_name || name !~ /^[a-z0-9][a-z0-9+.-]+$/ || name ~ /\.deb$/) {
            invalid++
            if (invalid <= max_warnings) {
                shown = substr(name, 1, 32)
                gsub(/[^ -~]/, "?", shown)
                if (length(name) > 32) shown = shown "..."
                printf "I\t%d\t%s\t%d\n", NR, shown, length(name)
            }
            next
        }
        if (seen[name]++) next
        count++
        if (count <= max_names) printf "V\t%s\n", name
    }
    END {
        if (invalid > max_warnings) printf "S\t%d\n", invalid - max_warnings
        if (count > max_names) printf "O\t%d\n", count - max_names
    }
'); then
    ui_warn "Cannot parse package list $list_path; replay skipped."
    exit 0
fi
names=()
while IFS=$'\t' read -r tag value shown bytes; do
    case "$tag" in
        V) names+=("$value") ;;
        I) ui_warn "Invalid package name \"$shown\" ($bytes bytes) in $list_path (line $value); skipped." ;;
        S) ui_warn "$value more invalid lines in $list_path skipped." ;;
        O) ui_warn "More than $max_names package names in $list_path; $value not replayed." ;;
    esac
done <<< "$records"

if [[ ${#names[@]} -eq 0 ]]; then
    ui_info 'none recorded'
    exit 0
fi
within_budget() {
    if (( SECONDS - checks_started > check_budget )); then
        ui_warn "Package checks exceeded $check_budget s; replay skipped."
        return 1
    fi
    return 0
}
missing=()
for name in "${names[@]}"; do
    # The format belongs to dpkg-query; a sentinel preserves its exact output.
    # shellcheck disable=SC2016
    if ! state=$(LANG=C "$dpkg_bin" -W -f='${db:Status-Status}' -- "$name" 2>/dev/null; result=$?; printf '.'; exit "$result"); then
        state=''
    fi
    within_budget || exit 0
    [[ "$state" == installed. ]] && continue
    missing+=("$name")
done
if [[ ${#missing[@]} -eq 0 ]]; then
    ui_info "${#names[@]} recorded, all installed"
    exit 0
fi
ui_info "installing ${#missing[@]} of ${#names[@]}"

warn_failure() {
    ui_warn "Package $1 failed (status $2). Last output lines:"
    printf '%s\n' "$3" | tail -n 3 >&2
}

# Keep announcements outside captured helper output so each elevation is visible.
printf 'Packages: using sudo to run %s as root.\n' "$helper_path" >&2
if output=$(timeout "$update_timeout" sudo -n -u "$sudo_target" -- "$helper_path" -qq update 2>&1); then
    # apt-get update exits 0 when indexes cannot be fetched; surface its W:/E: lines.
    if problems=$(printf '%s\n' "$output" | grep -E '^[WE]: '); then
        ui_warn 'Package list update reported problems; listed packages may be skipped. Last lines:'
        printf '%s\n' "$problems" | tail -n 3 >&2
    fi
else
    result=$?
    if [[ "$result" -eq 124 ]]; then
        ui_warn "Package list update timed out after $update_timeout s; replay skipped."
        exit 0
    fi
    warn_failure update "$result" "$output"
    exit 0
fi
# The update has its own limit; the check budget restarts for the cache checks.
checks_started=$SECONDS
known=()
for name in "${missing[@]}"; do
    if ! available=$("$cache_bin" pkgnames -- "$name" 2>/dev/null); then
        ui_warn "apt-cache failed for $name; package replay skipped."
        exit 0
    fi
    within_budget || exit 0
    if printf '%s\n' "$available" | grep -qxF -- "$name"; then
        known+=("$name")
    else
        ui_warn "Unknown package $name; check apt-get update or edit $list_path; skipped."
    fi
done
[[ ${#known[@]} -gt 0 ]] || exit 0
printf 'Packages: using sudo to run %s as root.\n' "$helper_path" >&2
# No install timeout: killing apt/dpkg can leave half-configured packages dev cannot repair without sudo.
if output=$(sudo -n -u "$sudo_target" -- "$helper_path" -qq install "${known[@]}" 2>&1); then
    :
else
    result=$?
    warn_failure install "$result" "$output"
fi
exit 0
