#!/bin/bash -p
saved_path=${PATH-}
PATH='/usr/sbin:/usr/bin:/sbin:/bin'
# =============================================================================
# Egress Allowlist Wrapper (installed as /usr/local/bin/djinn-egress-init)
# =============================================================================
# Entrypoint of an allowlist run (djinn run --allow), started as root. Applies
# the rules with init-firewall.sh, then drops NET_ADMIN and NET_RAW from the
# bounding set of the whole process tree and execs the regular entrypoint as
# the dev user. Until the drop it executes and reads only image content: fixed
# absolute paths, no library, no caller-selected path, a clean environment for
# the firewall child, `bash -p` (no BASH_ENV or exported functions).
# =============================================================================

set -euo pipefail

system_path=$PATH
root_uid='0'
env_bin='/usr/bin/env'
bash_bin='/bin/bash'
firewall_script='/usr/local/bin/init-firewall.sh'
setpriv_bin='/usr/bin/setpriv'
zsh_bin='/bin/zsh'
entrypoint_path='/home/dev/entrypoint.sh'
dev_user='dev'
dev_home='/home/dev'
dev_uid='1000'
dev_gid='1000'

refuse() {
    echo "djinn-egress-init: $1" >&2
    exit 1
}

[[ "$EUID" == "$root_uid" ]] || refuse "root is required"
[[ -n "${DJINN_EGRESS_ALLOW-}" ]] || refuse "DJINN_EGRESS_ALLOW is empty or unset"

if ! "$env_bin" -i PATH="$system_path" "$bash_bin" -p "$firewall_script" --allow "$DJINN_EGRESS_ALLOW"; then
    refuse "firewall setup failed; not starting"
fi

export HOME="$dev_home" USER="$dev_user" LOGNAME="$dev_user"
export PATH="$saved_path"
exec "$setpriv_bin" --bounding-set=-net_admin,-net_raw --inh-caps=-all \
    --reuid="$dev_uid" --regid="$dev_gid" --init-groups -- "$zsh_bin" -f "$entrypoint_path" "$@"
