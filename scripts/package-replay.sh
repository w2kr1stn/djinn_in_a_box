#!/bin/bash
# Replay package intent as dev; the root helper independently validates each call.
list_path='/home/dev/.local/share/djinn-packages/packages.txt'
helper_path='/usr/local/libexec/djinn-apt-root'
sudo_target='root'
dpkg_bin='/usr/bin/dpkg-query'
cache_bin='/usr/bin/apt-cache'
readonly list_path helper_path sudo_target dpkg_bin cache_bin
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
if ! snapshot=$(cat -- "$list_path" 2>/dev/null && printf '.'); then
    ui_warn "Cannot read package list $list_path."
    ui_info 'none recorded (list unreadable)'
    exit 0
fi

declare -A seen=()
names=()
while IFS= read -r line || [[ -n "$line" ]]; do
    name=${line%%#*}
    name=${name#"${name%%[![:space:]]*}"}
    name=${name%"${name##*[![:space:]]}"}
    [[ -n "$name" ]] || continue
    case "$name" in
        ''|?|[!a-z0-9]*|*[!a-z0-9+.-]*|*.deb)
            printf -v escaped '%q' "$name"
            ui_warn "Invalid package name $escaped in $list_path; skipped."
            continue
            ;;
    esac
    [[ -z "${seen[$name]:-}" ]] || continue
    seen[$name]=1
    names+=("$name")
done < <(printf '%s' "${snapshot%.}")

if [[ ${#names[@]} -eq 0 ]]; then
    ui_info 'none recorded'
    exit 0
fi
missing=()
for name in "${names[@]}"; do
    # The format belongs to dpkg-query; a sentinel preserves its exact output.
    # shellcheck disable=SC2016
    if state=$(LANG=C "$dpkg_bin" -W -f='${db:Status-Status}' -- "$name" 2>/dev/null; result=$?; printf '.'; exit "$result") && [[ "$state" == installed. ]]; then
        continue
    fi
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
if output=$(sudo -n -u "$sudo_target" -- "$helper_path" -qq update 2>&1); then
    # apt-get update exits 0 when indexes cannot be fetched; surface its W:/E: lines.
    if problems=$(printf '%s\n' "$output" | grep -E '^[WE]: '); then
        ui_warn 'Package list update reported problems; listed packages may be skipped. Last lines:'
        printf '%s\n' "$problems" | tail -n 3 >&2
    fi
else
    result=$?
    warn_failure update "$result" "$output"
    exit 0
fi
known=()
for name in "${missing[@]}"; do
    if ! available=$("$cache_bin" pkgnames -- "$name" 2>/dev/null); then
        ui_warn "apt-cache failed for $name; package replay skipped."
        exit 0
    fi
    if printf '%s\n' "$available" | grep -qxF -- "$name"; then
        known+=("$name")
    else
        ui_warn "Unknown package $name; check apt-get update or edit $list_path; skipped."
    fi
done
[[ ${#known[@]} -gt 0 ]] || exit 0
printf 'Packages: using sudo to run %s as root.\n' "$helper_path" >&2
if output=$(sudo -n -u "$sudo_target" -- "$helper_path" -qq install "${known[@]}" 2>&1); then
    :
else
    result=$?
    warn_failure install "$result" "$output"
fi
exit 0
