# =============================================================================
# AI Dev Base Image
# Minimal base with: fnm (Node), uv (Python), Claude Code, Codex, Gemini CLI
# + Docker CLI für Container-Management (optional aktivierbar)
# =============================================================================
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies (base)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl openssh-client git zsh jq build-essential iptables sudo unzip \
    && rm -rf /var/lib/apt/lists/*

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
# Docker CLI Installation (nur CLI, kein Daemon)
# Wird nur aktiv wenn DOCKER_HOST gesetzt ist (via --docker Flag)
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

ENV PATH="/home/${USERNAME}/.local/share/fnm:$PATH"
RUN eval "$(fnm env)" && fnm install --lts && fnm default lts-latest

# CLI Agent versions - update with: ./scripts/update-agents.sh
ARG CLAUDE_CODE_VERSION=2.1.20
ARG GEMINI_CLI_VERSION=0.25.2
ARG CODEX_VERSION=0.92.0
ARG OPENCODE_VERSION=1.1.36

# Claude Code via native installer (no npm/Node.js dependency)
# Installs to ~/.local/bin/claude (already in PATH via .zshrc)
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"

# Global NPM Packages (remaining agents + dev tools)
RUN eval "$(fnm env --shell bash)" && npm install -g \
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

# Shell config (.zshrc)
RUN cat > ~/.zshrc << 'EOF'
export PATH="$HOME/.cache/ai-dev-tools/bin:$HOME/.local/bin:$HOME/.local/share/fnm:$PATH"
export LD_LIBRARY_PATH="$HOME/.cache/ai-dev-tools/lib:${LD_LIBRARY_PATH:-}"
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
# SOPS convenience functions
_sops_filetype() {
    local name="${1%.enc}"
    case "$name" in
        *.yaml|*.yml) echo "yaml" ;;
        *.json) echo "json" ;;
        *.ini) echo "ini" ;;
        *) echo "dotenv" ;;
    esac
}
sops-view() {
    local file="${1:-.env.enc}" ft=$(_sops_filetype "${1:-.env.enc}")
    sops decrypt --input-type "$ft" --output-type "$ft" "$file"
}
sops-edit() {
    local file="${1:-.env.enc}" ft=$(_sops_filetype "${1:-.env.enc}")
    sops edit --input-type "$ft" --output-type "$ft" "$file"
}
sops-dec() {
    local src="${1:-.env.enc}" dst="${2:-${1:-.env.enc}}" ft
    dst="${dst%.enc}"
    ft=$(_sops_filetype "$src")
    sops decrypt --input-type "$ft" --output-type "$ft" "$src" > "$dst" && echo "Decrypted: $src -> $dst"
}
sops-enc() {
    local src="${1:-.env}" dst="${2:-${1:-.env}.enc}" ft
    ft=$(_sops_filetype "$src")
    sops encrypt --input-type "$ft" --output-type "$ft" "$src" > "$dst" && echo "Encrypted: $src -> $dst"
}

# Docker Status Hinweis beim Shell-Start
if [[ -n "${DOCKER_HOST:-}" ]]; then
    echo "🐳 Docker: Connected via proxy (${DOCKER_HOST})"
fi
EOF

# Prepare persistent config directories
RUN mkdir -p ~/.claude ~/.codex ~/.gemini ~/.config/gh ~/.config/uv ~/.config \
    ~/.opencode ~/.local/share/opencode \
    && echo '{"name": "opencode-workspace", "private": true}' > ~/.opencode/package.json \
    && ln -s ~/.claude ~/.config/claude \
    && ln -s ~/.claude/claude.json ~/.claude.json \
    && ln -s ~/.gemini/settings.json ~/.gemini-settings.json

# Optional tools installer (runtime installation with caching)
COPY --chown=dev:dev tools/ /home/dev/.tools/
RUN chmod +x ~/.tools/install.sh ~/.tools/installers/*.sh 2>/dev/null || true

COPY --chown=dev:dev scripts/entrypoint.sh /home/dev/entrypoint.sh
RUN chmod +x ~/entrypoint.sh

ENV SHELL=/bin/zsh
ENTRYPOINT ["/home/dev/entrypoint.sh"]
