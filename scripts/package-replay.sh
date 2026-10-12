#!/bin/bash
# The root helper owns the list and replay; this dev wrapper only formats output.
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


printf 'Packages: using sudo to run /usr/local/libexec/djinn-apt-root replay as root.\n' >&2
output=$(sudo -n -u root -- /usr/local/libexec/djinn-apt-root replay 2>&1)
result=$?
while IFS= read -r line; do
    case "$line" in
        I$'\t'*) ui_info "${line:2}" ;;
        W$'\t'*) ui_warn "${line:2}" ;;
        *) ui_info "$line" ;;
    esac
done < <(printf '%s' "$output"; [[ -z "$output" ]] || printf '\n')
if [[ "$result" -ne 0 ]]; then
    ui_warn "Package replay failed (status $result); continuing startup."
fi
exit 0
