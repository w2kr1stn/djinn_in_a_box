# =============================================================================
# Djinn in a Box Image
# Minimal base with: fnm (Node), uv (Python), Claude Code, Codex, Gemini CLI, OpenCode
# + Docker CLI for container management (optionally enabled)
# =============================================================================
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies (base)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg openssh-client git zsh jq python3 build-essential iptables sudo unzip locales \
    libpulse0 pulseaudio-utils alsa-utils libasound2-plugins sox \
    && rm -rf /var/lib/apt/lists/*

# Audio client config (routes ALSA through PulseAudio socket from host)
RUN printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' > /etc/asound.conf \
    && mkdir -p /etc/pulse \
    && printf 'autospawn = no\ndaemon-binary = /bin/true\nenable-shm = false\n' > /etc/pulse/client.conf

# Custom packages from packages.txt (optional)
# Copy packages.txt if it exists (wildcard allows missing file)
COPY packages.tx[t] /tmp/
RUN if [ -f /tmp/packages.txt ]; then \
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

RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
    | tar xz --strip-components=1 -C /usr/local/bin docker/docker \
    && chmod +x /usr/local/bin/docker

# Docker Compose Plugin
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
       -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

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
    && chmod 0440 /etc/sudoers.d/$USERNAME

COPY scripts/init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod +x /usr/local/bin/init-firewall.sh

USER $USERNAME
WORKDIR /home/$USERNAME

# Tooling Setup
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended
RUN mkdir -p ~/.local/bin && curl -fsSL https://ohmyposh.dev/install.sh | bash -s -- -d ~/.local/bin
RUN curl -fsSL https://fnm.vercel.app/install | bash -s -- --install-dir "$HOME/.local/share/fnm" --skip-shell

ENV PATH="/home/${USERNAME}/.local/bin:/home/${USERNAME}/.local/share/fnm:$PATH"
RUN eval "$(fnm env)" && fnm install --lts && fnm default lts-latest

# CLI Agent versions - update with: ./scripts/update-agents.sh
ARG CLAUDE_CODE_VERSION=2.1.220
ARG GEMINI_CLI_VERSION=0.53.0
ARG CODEX_VERSION=0.146.0
ARG OPENCODE_VERSION=1.18.9

# Claude Code via native installer (no npm/Node.js dependency)
# Installs to ~/.local/bin/claude (already in PATH via .zshrc)
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"

# Global NPM Packages (remaining agents + dev tools)
# Retry config is inline (build-scoped, not a persistent ENV) so it is also
# inherited by the npm subprocess opencode's postinstall spawns to fetch its
# platform binary — that postinstall hard-exits 1 on a failed fetch, and this
# layer re-runs on every `djinn update` (ARG bump). Hardens the unattended
# build against transient registry/CDN hiccups without slowing runtime npm.
RUN eval "$(fnm env --shell bash)" && \
    NPM_CONFIG_FETCH_RETRIES=5 NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000 \
    npm install -g \
    typescript \
    typescript-language-server \
    pyright \
    prettier \
    eslint \
    @google/gemini-cli@${GEMINI_CLI_VERSION} \
    @openai/codex@${CODEX_VERSION} \
    opencode-ai@${OPENCODE_VERSION} \
    && npm cache clean --force

RUN uv tool install ruff

# Non-interactive processes do not source fnm's shell initialization.
ENV PATH="/home/${USERNAME}/.local/share/fnm/aliases/default/bin:$PATH"

# Shell config (.zshrc)
RUN cat > ~/.zshrc << 'EOF'
export PATH="$HOME/.cache/djinn-tools/bin:$HOME/.local/bin:$HOME/.local/share/fnm/aliases/default/bin:$HOME/.local/share/fnm:$PATH"
export LD_LIBRARY_PATH="$HOME/.cache/djinn-tools/lib:${LD_LIBRARY_PATH:-}"
export ZSH="$HOME/.oh-my-zsh"
ZSH_THEME=""
plugins=(git zsh-autosuggestions docker)
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
RUN mkdir -p ~/.claude ~/.codex ~/.gemini ~/.config/gh ~/.config/uv ~/.config \
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
COPY --chown=dev:dev scripts/settings-copy.py /home/dev/settings-copy.py
COPY --chown=dev:dev scripts/opencode-credentials.sh /home/dev/opencode-credentials.sh
COPY --chown=dev:dev scripts/output-lib.sh /home/dev/output-lib.sh
COPY --chown=dev:dev scripts/seed-lib.sh /home/dev/seed-lib.sh
COPY --chown=dev:dev scripts/mcp-register.sh /home/dev/mcp-register.sh
RUN chmod +x ~/entrypoint.sh ~/mcp-register.sh

LABEL djinn.workflow.publisher="1"

ENV SHELL=/bin/zsh
ENTRYPOINT ["/home/dev/entrypoint.sh"]
