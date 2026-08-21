#!/bin/sh
# Refuse a build network that cannot resolve names, before anything tries to download.
#
# Without this the failure surfaces one timeout at a time. Measured on this project:
# a 70-minute build that ended in `EAI_AGAIN`, because npm retries every package six
# times with a backoff before giving up; `apt-get update` on a nameless network takes
# over two minutes on its own. Neither says what to do about it.
#
# `getent` ships with glibc, so this runs on the bare base image before any install.
set -eu

if getent hosts registry.npmjs.org > /dev/null 2>&1; then
    exit 0
fi

cat >&2 << 'MSG'

ERROR: this build's network cannot resolve registry.npmjs.org.

Every download below would retry for minutes and then fail.

If the container DNS server on this host is reachable from one Docker network
only -- a VPN or split-DNS setup, say, where a resolver listens on one bridge
while builds run on another -- then build on the host's network stack:

    djinn config set build.network host

That keeps DNS on the path the host itself uses instead of bypassing it. Note
the trade-off: build steps then share the host's network namespace, so they can
reach host-local services. Use it only with a Dockerfile you trust.

Otherwise check the Docker daemon's DNS configuration.

MSG
exit 1
