FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git curl jq ripgrep bash libstdc++6 libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH
ARG DOCKER_VERSION
RUN case "$TARGETARCH" in amd64) arch=x86_64;; arm64) arch=aarch64;; *) exit 1;; esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/$arch/docker-${DOCKER_VERSION}.tgz" -o /tmp/docker.tgz \
    && tar xzf /tmp/docker.tgz --strip-components=1 -C /usr/local/bin docker/docker \
    && rm /tmp/docker.tgz

ARG AGENT
ARG AGENT_VERSION
RUN set -eu; mkdir -p /opt/djinn-agent; \
    case "$AGENT" in \
      claude) \
        curl -fsSL https://claude.ai/install.sh -o /tmp/install.sh; \
        DISABLE_AUTOUPDATER=1 bash /tmp/install.sh "$AGENT_VERSION"; \
        cp "/root/.local/share/claude/versions/$AGENT_VERSION" /opt/djinn-agent/claude; \
        ln -s /opt/djinn-agent/claude /usr/local/bin/claude; \
        rm -rf /root/.claude /root/.local /tmp/install.sh;; \
      codex) \
        case "$TARGETARCH" in amd64) arch=x86_64;; arm64) arch=aarch64;; esac; \
        asset="codex-package-$arch-unknown-linux-musl.tar.gz"; \
        base="https://github.com/openai/codex/releases/download/rust-v$AGENT_VERSION"; \
        curl -fsSL "$base/$asset" -o "/tmp/$asset"; \
        curl -fsSL "$base/codex-package_SHA256SUMS" -o /tmp/checksums; \
        cd /tmp; rg "  $asset\$" checksums > selected-checksum; \
        sha256sum -c selected-checksum; \
        tar xzf "$asset" -C /opt/djinn-agent; \
        ln -s /opt/djinn-agent/bin/codex /usr/local/bin/codex; \
        rm "/tmp/$asset" /tmp/checksums /tmp/selected-checksum;; \
      opencode) \
        case "$TARGETARCH" in amd64) arch=x64-baseline;; arm64) arch=arm64;; esac; \
        asset="opencode-linux-$arch.tar.gz"; \
        curl -fsSL "https://api.github.com/repos/anomalyco/opencode/releases/tags/v$AGENT_VERSION" -o /tmp/release.json; \
        digest=$(jq -er --arg name "$asset" '.assets[] | select(.name == $name) | .digest | select(startswith("sha256:")) | ltrimstr("sha256:")' /tmp/release.json); \
        curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v$AGENT_VERSION/$asset" -o /tmp/agent.tgz; \
        printf '%s  /tmp/agent.tgz\n' "$digest" | sha256sum -c -; \
        tar xzf /tmp/agent.tgz -C /opt/djinn-agent; \
        ln -s /opt/djinn-agent/opencode /usr/local/bin/opencode; \
        rm /tmp/release.json /tmp/agent.tgz;; \
      *) exit 1;; \
    esac; "$AGENT" --version

ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid "$USER_GID" dev && useradd --uid "$USER_UID" --gid "$USER_GID" -m dev
COPY entrypoint.sh /usr/local/bin/djinn-assistant-entrypoint
RUN chmod 0755 /usr/local/bin/djinn-assistant-entrypoint
ENV HOME=/home/dev DISABLE_AUTOUPDATER=1 OPENCODE_DISABLE_AUTOUPDATE=true
ENV DJINN_ASSISTANT_AGENT=$AGENT
USER dev
WORKDIR /home/dev
ENTRYPOINT ["/usr/local/bin/djinn-assistant-entrypoint"]
