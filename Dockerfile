# =============================================================================
# Djinn in a Box Image
# Minimal base with: fnm (Node), uv (Python), Claude Code, Codex, OpenCode
# + Docker CLI for container management (optionally enabled)
# =============================================================================
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies (base)
# The DNS guard runs *inside* the network-dependent layers rather than in one of its
# own. A separate guard layer can stay cached while the download below re-runs — only
# sharing a RUN instruction shares the cache decision. It guards the first download
# here and the agent installs further down; the curl-based steps in between fail in
# seconds with their own resolver error, so they are left alone.
COPY scripts/check-build-dns.sh /usr/local/bin/check-build-dns.sh
RUN chmod +x /usr/local/bin/check-build-dns.sh

RUN check-build-dns.sh && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg openssh-client git zsh jq python3 build-essential iptables sudo unzip locales \
    libpulse0 pulseaudio-utils alsa-utils libasound2-plugins sox libnotify-bin dbus-bin \
    && rm -rf /var/lib/apt/lists/*

# Audio client config (routes ALSA through the locked desktop relay)
RUN printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > /etc/asound.conf \
    && mkdir -p /etc/pulse \
    && printf 'autospawn = no\ndaemon-binary = /bin/true\nenable-shm = false\n' > /etc/pulse/client.conf

# Custom packages from packages.txt (optional)
# Copy packages.txt if it exists (wildcard allows missing file)
COPY packages.tx[t] /tmp/
RUN if [ -f /tmp/packages.txt ]; then \
        check-build-dns.sh && \
        apt-get update && \
        sed 's/#.*//' /tmp/packages.txt | grep -v '^[[:space:]]*$' | \
        xargs -r apt-get install -y --no-install-recommends && \
        rm -rf /var/lib/apt/lists/*; \
    fi && rm -f /tmp/packages.txt

# -----------------------------------------------------------------------------
# Docker CLI only (no daemon). Access depends on --docker or --docker-direct flags at runtime.
# -----------------------------------------------------------------------------
ARG DOCKER_VERSION=27.4.1
ARG COMPOSE_VERSION=2.32.4
ARG BUILDX_VERSION=0.37.2
ARG BUILDX_SHA256=982ca20490b45ed1ec8d99795974d3d874a358f75938c9c237305010e6b7e548

RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
    | tar xz --strip-components=1 -C /usr/local/bin docker/docker \
    && chmod +x /usr/local/bin/docker

# Docker Compose Plugin
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
       -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Docker Buildx Plugin (verified before installation; no legacy builder fallback)
RUN curl -fsSL "https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/buildx-v${BUILDX_VERSION}.linux-amd64" \
       -o /tmp/docker-buildx \
    && echo "${BUILDX_SHA256}  /tmp/docker-buildx" | sha256sum -c - \
    && install -m 0755 /tmp/docker-buildx /usr/local/lib/docker/cli-plugins/docker-buildx \
    && rm /tmp/docker-buildx

# -----------------------------------------------------------------------------
# GitHub CLI Installation
# -----------------------------------------------------------------------------
ARG GH_VERSION=2.85.0

RUN curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    | tar xz --strip-components=2 -C /usr/local/bin gh_${GH_VERSION}_linux_amd64/bin/gh \
    && chmod +x /usr/local/bin/gh

# Install uv (Python)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Create non-root user
ARG USERNAME=dev
ARG USER_UID=1000
ARG USER_GID=$USER_UID
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m -s /bin/zsh $USERNAME \
    && echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/$USERNAME \
    && chmod 0440 /etc/sudoers.d/$USERNAME \
    && echo "$USERNAME ALL=(root) NOPASSWD: /usr/local/libexec/djinn-apt-root" > /etc/sudoers.d/djinn-apt \
    && chmod 0440 /etc/sudoers.d/djinn-apt \
    && visudo -cf /etc/sudoers.d/djinn-apt \
    && install -d -o root -g root -m 0755 /var/lib/djinn-packages

COPY scripts/init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod +x /usr/local/bin/init-firewall.sh
COPY scripts/egress-init.sh /usr/local/bin/djinn-egress-init
RUN chmod 0755 /usr/local/bin/djinn-egress-init

USER $USERNAME
WORKDIR /home/$USERNAME

# Tool pins: maintainers bump with ./scripts/update-agents.sh, then build the
# defaults alone once before committing, as for the agent defaults below.
ARG OH_MY_ZSH_COMMIT=17a2b4c3a5855524ad4772a17ada0dc474e2fdf3
ARG OH_MY_ZSH_SHA256=eb6f2788316ff75f9be60c00372da0da97d4bcab202d64a9229e1f6365166932
ARG OH_MY_POSH_VERSION=31.9.1
ARG OH_MY_POSH_SHA256=574074a4e84797180e0bda0d133da5f01fbda89ecdb450134c7e566b9a389212
ARG FNM_VERSION=1.39.0
ARG FNM_SHA256=7807664f39d39fc518da1c35ba0181e4b3267603c4b1dedeb4b5fc6ae440a224

# Tooling Setup: check pinned bytes before extraction or installation.
RUN scratch=$(mktemp -d) \
    && trap 'rm -rf "$scratch"' EXIT HUP INT TERM \
    && curl --proto '=https' --proto-redir '=https' -fsSL "https://codeload.github.com/ohmyzsh/ohmyzsh/tar.gz/$OH_MY_ZSH_COMMIT" -o "$scratch/source.tgz" \
    && printf '%s  %s\n' "$OH_MY_ZSH_SHA256" "$scratch/source.tgz" | sha256sum -c - \
    && mkdir -p ~/.oh-my-zsh \
    && tar -xzf "$scratch/source.tgz" --strip-components=1 -C ~/.oh-my-zsh \
    && rm -rf "$scratch"
RUN scratch=$(mktemp -d) \
    && trap 'rm -rf "$scratch"' EXIT HUP INT TERM \
    && curl --proto '=https' --proto-redir '=https' -fsSL "https://github.com/JanDeDobbeleer/oh-my-posh/releases/download/v$OH_MY_POSH_VERSION/posh-linux-amd64" -o "$scratch/oh-my-posh" \
    && printf '%s  %s\n' "$OH_MY_POSH_SHA256" "$scratch/oh-my-posh" | sha256sum -c - \
    && install -D -m 0755 "$scratch/oh-my-posh" ~/.local/bin/oh-my-posh \
    && rm -rf "$scratch"
RUN scratch=$(mktemp -d) \
    && trap 'rm -rf "$scratch"' EXIT HUP INT TERM \
    && curl --proto '=https' --proto-redir '=https' -fsSL "https://github.com/Schniz/fnm/releases/download/v$FNM_VERSION/fnm-linux.zip" -o "$scratch/fnm.zip" \
    && printf '%s  %s\n' "$FNM_SHA256" "$scratch/fnm.zip" | sha256sum -c - \
    && unzip -q "$scratch/fnm.zip" -d "$scratch/unpacked" \
    && install -D -m 0755 "$scratch/unpacked/fnm" ~/.local/share/fnm/fnm \
    && rm -rf "$scratch"

ENV PATH="/home/${USERNAME}/.local/bin:/home/${USERNAME}/.local/share/fnm:$PATH"
RUN eval "$(fnm env)" && fnm install --lts && fnm default lts-latest

# Upstream CLI agent defaults - maintainers bump with: ./scripts/update-agents.sh
# djinn update records local versions; builds pass higher versions as ARG overrides.
# After bumping any default, build once with the defaults alone (no local
# agent-versions.toml record) and commit the bump only after that build succeeds.
ARG CLAUDE_CODE_VERSION=2.1.304
ARG CODEX_VERSION=0.160.0
ARG OPENCODE_VERSION=2.1.1

# Claude Code from a signed version manifest (native layout, no npm dependency).
# dev owns the copy so the RUN below can remove it from the sticky /tmp.
COPY --chown=dev:dev assistant/install-claude.sh /tmp/install-claude.sh
RUN sh /tmp/install-claude.sh amd64 "$CLAUDE_CODE_VERSION" "$HOME/.local/share/claude/versions/$CLAUDE_CODE_VERSION" \
    && mkdir -p ~/.local/bin \
    && ln -sfn ~/.local/share/claude/versions/$CLAUDE_CODE_VERSION ~/.local/bin/claude \
    && rm /tmp/install-claude.sh

# Global NPM Packages (remaining agents + dev tools)
# Retry config is inline (build-scoped, not a persistent ENV) so it is also
# inherited by the npm subprocess opencode's postinstall spawns to fetch its
# platform binary — that postinstall hard-exits 1 on a failed fetch, and this
# layer re-runs whenever an effective agent ARG version changes. Hardens the unattended
# build against transient registry/CDN hiccups without slowing runtime npm.
# The guard shares this RUN on purpose: an effective agent ARG bump re-runs this layer
# (like every RUN after the ARG block) while the layers above stay cached, and its failure mode is
# pathological -- six retries per package with a backoff, measured at 70 minutes
# before npm gave up. In the same instruction it cannot be cached away from the
# install it protects.
RUN check-build-dns.sh && \
    eval "$(fnm env --shell bash)" && \
    NPM_CONFIG_FETCH_RETRIES=5 NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000 \
    npm install -g \
    typescript \
    typescript-language-server \
    pyright \
    prettier \
    eslint \
    @openai/codex@${CODEX_VERSION} \
    @opencode/cli@${OPENCODE_VERSION} \
    && npm cache clean --force

RUN uv tool install ruff

# Tools precede Node for every process, without shell initialization.
ENV PATH="/home/${USERNAME}/.cache/djinn-tools/bin:/home/${USERNAME}/.local/share/fnm/aliases/default/bin:$PATH"

# Shell config (.zshrc)
RUN cat > ~/.zshrc << 'EOF'
export ZSH="$HOME/.oh-my-zsh"
ZSH_THEME=""
plugins=(git zsh-autosuggestions docker)
zstyle ':omz:update' mode disabled
[[ -f $ZSH/oh-my-zsh.sh ]] && source $ZSH/oh-my-zsh.sh
eval "$(fnm env --use-on-cd --shell zsh)"
eval "$(uv generate-shell-completion zsh)"
export UV_PROJECT_ENVIRONMENT=".venv"
if command -v oh-my-posh &> /dev/null; then
    [[ -f ~/.zsh-theme.omp.json ]] && eval "$(oh-my-posh init zsh --config ~/.zsh-theme.omp.json)"
fi
[[ -f ~/.zshrc.local ]] && source ~/.zshrc.local
export EDITOR=vim
export VISUAL=vim

# Session environment
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TERM=xterm-256color
export COLORTERM=truecolor
EOF

# Prepare persistent config directories
RUN mkdir -p ~/.claude ~/.codex ~/.config/gh ~/.config/uv ~/.config \
    ~/.opencode ~/.local/share/opencode \
    && echo '{"name": "opencode-workspace", "private": true}' > ~/.opencode/package.json \
    && ln -sfn ~/.claude ~/.config/claude \
    && echo '{}' > ~/.config/mcp-servers.json

# Optional tools installer (runtime installation with caching)
# Build timestamp used by install.sh to invalidate cache on image rebuild
COPY --chown=dev:dev tools/ /home/dev/.tools/
RUN chmod +x ~/.tools/install.sh ~/.tools/installers/*.sh 2>/dev/null || true \
    && date +%s > ~/.build-timestamp

COPY --chown=dev:dev scripts/entrypoint.sh /home/dev/entrypoint.sh
COPY --chown=dev:dev src/djinn_in_a_box/core/workflow_publisher.py /home/dev/workflow-publisher.py
COPY --chown=dev:dev scripts/git-config.py /home/dev/git-config.py
# Root-owned and executable; the build already runs as dev here, so set the mode at copy time.
COPY --chmod=755 scripts/hostctl-connect.py /usr/local/bin/djinn-hostctl-connect
COPY --chmod=755 scripts/apt-get-shim.sh /usr/local/bin/apt-get
COPY --chmod=755 scripts/djinn-apt-root.sh /usr/local/libexec/djinn-apt-root
COPY --chown=dev:dev scripts/settings-copy.py /home/dev/settings-copy.py
COPY --chown=dev:dev scripts/codex-daemon-policy.py /home/dev/codex-daemon-policy.py
COPY --chown=dev:dev scripts/ownership-repair.py /home/dev/ownership-repair.py
COPY --chown=dev:dev src/djinn_in_a_box/core/opencode_credentials.py /home/dev/opencode-credentials.py
COPY --chown=dev:dev scripts/output-lib.sh /home/dev/output-lib.sh
COPY --chown=dev:dev scripts/seed-lib.sh /home/dev/seed-lib.sh
COPY --chown=dev:dev scripts/mcp-register.sh /home/dev/mcp-register.sh
COPY --chown=dev:dev --chmod=755 scripts/package-replay.sh /home/dev/package-replay.sh
RUN chmod +x ~/entrypoint.sh ~/mcp-register.sh

LABEL djinn.workflow.publisher="2"

ENV SHELL=/bin/zsh
ENTRYPOINT ["/home/dev/entrypoint.sh"]
