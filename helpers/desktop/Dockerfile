FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a

COPY scripts/check-build-dns.sh /usr/local/bin/check-build-dns.sh
RUN chmod 0755 /usr/local/bin/check-build-dns.sh \
    && check-build-dns.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       xdg-dbus-proxy pulseaudio pulseaudio-utils dbus-bin util-linux python3-minimal python3 \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' xdg-dbus-proxy)" ge 0.1.6-1+deb13u3 \
    && python3 -I -c "import json, subprocess, signal, pathlib" \
    && dpkg-query -W xdg-dbus-proxy \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 dev && useradd -u 1000 -g 1000 -d /runtime/home dev

COPY helpers/desktop/ /etc/djinn/
RUN chmod -R go-w /etc/djinn \
    && mv /etc/djinn/relay.pa /etc/djinn/relay.template \
    && ln -s /runtime/relay.pa /etc/djinn/relay.pa
WORKDIR /
