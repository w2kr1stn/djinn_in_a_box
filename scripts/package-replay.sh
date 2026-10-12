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

ui_info 'Packages: using sudo to run /usr/local/libexec/djinn-apt-root replay as root.'
sudo -n -u root -- /usr/local/libexec/djinn-apt-root replay 2>&1 | while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
        I$'\t'*) ui_info "${line:2}" ;;
        W$'\t'*) ui_warn "${line:2}" ;;
        *) ui_info "$line" ;;
    esac
done
result=${PIPESTATUS[0]}
if [[ "$result" -ne 0 ]]; then
    ui_warn "Package replay failed (status $result); continuing startup."
fi
exit 0
