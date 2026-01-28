#!/bin/bash
# =============================================================================
# AI Dev Base - CLI Wrapper
# =============================================================================

set -euo pipefail

# Speichere Original-PWD des Aufrufers BEVOR ins Script-Verzeichnis gewechselt wird
CALLER_PWD="$(pwd)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Wechsle ins Hauptverzeichnis (eine Ebene über scripts/)
cd "$SCRIPT_DIR/.."

# -----------------------------------------------------------------------------
# Farben für Output
# -----------------------------------------------------------------------------
source "$SCRIPT_DIR/colors.sh"

# -----------------------------------------------------------------------------
# Load .env file if it exists
# -----------------------------------------------------------------------------
load_env() {
    if [[ -f ".env" ]]; then
        # Export variables from .env (ignore comments and empty lines)
        set -a
        # shellcheck source=/dev/null
        source .env
        set +a
    fi
}

# -----------------------------------------------------------------------------
# Check required configuration
# -----------------------------------------------------------------------------
check_config() {
    if [[ -z "${CODE_DIR:-}" ]]; then
        echo -e "${RED}Error: CODE_DIR is not set.${NC}"
        echo ""
        echo "Please configure your environment:"
        echo "  1. Copy .env.example to .env:"
        echo "     cp .env.example .env"
        echo ""
        echo "  2. Edit .env and set CODE_DIR to your projects directory:"
        echo "     CODE_DIR=/path/to/your/code"
        echo ""
        echo "  Or set it directly:"
        echo "     export CODE_DIR=/path/to/your/code"
        exit 1
    fi

    # Expand tilde
    CODE_DIR="${CODE_DIR/#\~/$HOME}"
    export CODE_DIR

    if [[ ! -d "$CODE_DIR" ]]; then
        echo -e "${RED}Error: CODE_DIR does not exist: $CODE_DIR${NC}"
        exit 1
    fi
}

# -----------------------------------------------------------------------------
# Build optional shell mount arguments
# -----------------------------------------------------------------------------
get_shell_mount_args() {
    local args=""

    # Skip all shell mounts if SKIP_SHELL_MOUNTS is true
    if [[ "${SKIP_SHELL_MOUNTS:-false}" == "true" ]]; then
        echo ""
        return
    fi

    # ZSH config (mounted as .zshrc.local)
    if [[ -f "$HOME/.zshrc" ]]; then
        args="$args -v $HOME/.zshrc:/home/dev/.zshrc.local:ro"
    fi

    # Oh My Posh theme
    local omp_theme="${OMP_THEME_PATH:-$HOME/.oh-my-zsh/custom/themes/.zsh-theme-remote.omp.json}"
    omp_theme="${omp_theme/#\~/$HOME}"
    if [[ -f "$omp_theme" ]]; then
        args="$args -v $omp_theme:/home/dev/.zsh-theme.omp.json:ro"
    fi

    # Oh My ZSH custom directory (plugins, themes, etc.)
    if [[ -d "$HOME/.oh-my-zsh/custom" ]]; then
        args="$args -v $HOME/.oh-my-zsh/custom:/home/dev/.oh-my-zsh/custom:ro"
    fi

    echo "$args"
}

# -----------------------------------------------------------------------------
# Compose File Selection
# -----------------------------------------------------------------------------
get_compose_files() {
    local docker_enabled="${1:-false}"

    if [[ "$docker_enabled" == "true" ]]; then
        echo "-f docker-compose.yml -f docker-compose.docker.yml"
    else
        echo "-f docker-compose.yml"
    fi
}

# -----------------------------------------------------------------------------
# Parse common container arguments (--docker, --firewall, --here, --mount)
# Sets: PARSED_DOCKER, PARSED_FIREWALL, PARSED_MOUNT_PATH,
#        PARSED_EXTRA_VOLUME, PARSED_WORKDIR, PARSED_COMPOSE_FILES,
#        PARSED_SHELL_MOUNTS
# Remaining (non-parsed) args are stored in PARSED_REMAINING
# -----------------------------------------------------------------------------
parse_container_args() {
    PARSED_DOCKER="false"
    PARSED_FIREWALL="false"
    PARSED_MOUNT_PATH=""
    PARSED_EXTRA_VOLUME=""
    PARSED_WORKDIR=""
    PARSED_COMPOSE_FILES=""
    PARSED_SHELL_MOUNTS=""
    PARSED_REMAINING=()

    local skip_next="false"
    local args=("$@")
    for i in "${!args[@]}"; do
        if [[ "$skip_next" == "true" ]]; then
            skip_next="false"
            continue
        fi
        case "${args[$i]}" in
            --docker)   PARSED_DOCKER="true" ;;
            --firewall) PARSED_FIREWALL="true" ;;
            --here)     PARSED_MOUNT_PATH="$CALLER_PWD" ;;
            --mount)
                PARSED_MOUNT_PATH="${args[$((i+1))]:-}"
                skip_next="true"
                if [[ -z "$PARSED_MOUNT_PATH" ]]; then
                    echo -e "${RED}--mount requires a path argument${NC}"
                    exit 1
                fi
                ;;
            *)
                PARSED_REMAINING+=("${args[$i]}")
                ;;
        esac
    done

    # Validate and resolve mount path
    if [[ -n "$PARSED_MOUNT_PATH" ]]; then
        PARSED_MOUNT_PATH="${PARSED_MOUNT_PATH/#\~/$HOME}"
        if [[ -d "$PARSED_MOUNT_PATH" ]]; then
            PARSED_MOUNT_PATH="$(cd "$PARSED_MOUNT_PATH" && pwd)"
        else
            echo -e "${RED}Mount path does not exist or is not a directory: $PARSED_MOUNT_PATH${NC}"
            exit 1
        fi
        PARSED_EXTRA_VOLUME="-v ${PARSED_MOUNT_PATH}:/home/dev/workspace"
        PARSED_WORKDIR="--workdir /home/dev/workspace"
    fi

    # Compose files and shell mounts
    PARSED_COMPOSE_FILES=$(get_compose_files "$PARSED_DOCKER")
    PARSED_SHELL_MOUNTS=$(get_shell_mount_args)
}

# -----------------------------------------------------------------------------
# Build agent headless command via agent_runner.py (reads config/agents.json)
# The prompt is passed via $AGENT_PROMPT env var to avoid shell escaping issues
# Args: <agent> <write_enabled> <json_enabled>
# Returns: command string that references $AGENT_PROMPT
# -----------------------------------------------------------------------------
get_agent_command() {
    local agent="$1"
    local write_enabled="${2:-false}"
    local json_enabled="${3:-false}"
    local model="${4:-}"

    local flags=""
    [[ "$write_enabled" == "true" ]] && flags="$flags --write"
    [[ "$json_enabled" == "true" ]] && flags="$flags --json"
    [[ -n "$model" ]] && flags="$flags --model $model"

    # Delegate to Python script which reads config/agents.json
    # shellcheck disable=SC2086
    python3 "$SCRIPT_DIR/agent_runner.py" build-cmd "$agent" $flags
}

usage() {
    cat << EOF
${BLUE}AI Dev Base${NC} - Entwicklungsumgebung für Claude Code, Gemini CLI & Codex CLI

${YELLOW}Usage:${NC} ./dev.sh <command> [options]

${YELLOW}Commands:${NC}
  build       Build/rebuild the base image
  start       Start interactive shell in container
  run         Run CLI agent headless (ephemeral, non-interactive)
  auth        Start with host network for OAuth (claude, gemini, codex, gh auth login)
  status      Show container, volume and MCP status
  update      Fetch latest CLI agent versions and update Dockerfile
  clean       Manage containers and volumes (see below)
  help        Show this help message

${YELLOW}Options:${NC}
  --docker    Enable Docker access (via secure proxy)
  --firewall  Enable network firewall (restricts outbound to whitelist)
  --here      Mount current directory additionally under ~/workspace/
  --mount <path>  Mount specified path additionally under ~/workspace/

${YELLOW}Configuration:${NC}
  Copy .env.example to .env and set at minimum:
    CODE_DIR=/path/to/your/projects

  Optional settings in .env:
    SKIP_SHELL_MOUNTS=true   # Skip zshrc/oh-my-zsh mounts
    CPU_LIMIT=4              # Adjust resource limits
    MEMORY_LIMIT=8G
    TZ=America/New_York      # Timezone

${YELLOW}CLI Agent Updates:${NC}
  ./dev.sh update             # Fetch latest versions & update Dockerfile
  ./dev.sh build              # Rebuild image with new versions

${YELLOW}Security Modes:${NC}
  ${GREEN}Default${NC}        Kein Docker-Zugriff, kein Firewall
  ${GREEN}--docker${NC}       Docker via Proxy (gefährliche Ops blockiert)
  ${GREEN}--firewall${NC}     Ausgehender Traffic auf Whitelist beschränkt
  ${GREEN}--docker --firewall${NC}  Maximale Sicherheit + Docker-Zugriff

${YELLOW}Headless Agent Mode:${NC}
  ${GREEN}./dev.sh run <agent> "<prompt>" [options]${NC}

  Agents: claude, gemini, codex, opencode
  Der Container startet ephemer, führt den Agent im headless mode aus,
  gibt die Antwort auf stdout aus und beendet sich.

  Automatisch wird \$(pwd) als ~/workspace/ gemountet (implizites --here).
  Mit --mount kann ein anderer Pfad gewählt werden.

  ${YELLOW}Run Options:${NC}
    --write         Agent darf Dateien ändern (Standard: read-only/plan)
    --json          Maschinenlesbarer JSON-Output
    --model <name>  Spezifisches Modell wählen (z.B. sonnet, gemini-2.5-flash)
    --docker        Docker-Zugriff via Proxy
    --firewall      Netzwerk-Firewall
    --mount <path>  Alternativer Workspace-Pfad

  ${YELLOW}Run Examples:${NC}
    ./dev.sh run claude "Erkläre die Architektur dieses Projekts"
    ./dev.sh run claude "Fix the bug" --write --model sonnet
    ./dev.sh run gemini "Refactore main.py" --write --model gemini-2.5-flash
    ./dev.sh run codex "Fix all type errors" --write --json
    ./dev.sh run opencode "Analysiere Dependencies" --model anthropic/claude-sonnet-4-5-20250929

${YELLOW}Workspace Mount:${NC}
  ${GREEN}--here${NC}         Mountet \$(pwd) zusätzlich unter ~/workspace/
  ${GREEN}--mount /path${NC}  Mountet beliebigen Pfad unter ~/workspace/

  Im Container:
    ~/projects/   → Dein festes Code-Verzeichnis (immer)
    ~/workspace/  → Temporärer Mount (nur mit --here/--mount)

${YELLOW}Clean & Volume Management:${NC}
  ./dev.sh clean                      # Remove containers only
  ./dev.sh clean volumes              # List all volumes by category
  ./dev.sh clean volumes --credentials  # Delete auth tokens (claude, gemini, etc.)
  ./dev.sh clean volumes --tools      # Delete tool installations & configs (az, pulumi, psql, sops)
  ./dev.sh clean volumes --cache      # Delete caches (uv)
  ./dev.sh clean volumes <name>       # Delete specific volume
  ./dev.sh clean --all                # Remove EVERYTHING (containers, volumes, network)

${YELLOW}Volume Categories:${NC}
  ${GREEN}credentials${NC}  AI agent auth tokens (claude, gemini, codex, opencode, gh)
  ${GREEN}tools${NC}        Tool installations & configs (az, pulumi, psql, sops)
  ${GREEN}cache${NC}        Package caches (uv)
  ${GREEN}data${NC}         Application data (opencode-data)

${YELLOW}Examples:${NC}
  ./dev.sh build                      # Build the image
  ./dev.sh start                      # Sicher: Kein Docker-Zugriff
  ./dev.sh start --docker             # Mit Docker-Zugriff via Proxy
  ./dev.sh start --firewall           # Mit Netzwerk-Firewall
  ./dev.sh start --docker --firewall  # Beides kombiniert
  ./dev.sh start --here               # Mit aktuellem Verzeichnis als Workspace
  ./dev.sh start --mount ~/.config    # Mit spezifischem Pfad als Workspace
  ./dev.sh start --docker --here      # Docker + aktuelles Verzeichnis
  ./dev.sh auth                       # OAuth Authentifizierung

${YELLOW}Docker-Proxy Sicherheit:${NC}
  Wenn --docker aktiviert ist, läuft ein Docker Socket Proxy der:
  - Container starten/stoppen erlaubt
  - Image pulls erlaubt
  - ${RED}BLOCKIERT:${NC} exec, build, commit, swarm, secrets, auth

  Details: DOCKER-SOCKET-SECURITY.md

${YELLOW}Workflow:${NC}
  1. cp .env.example .env && edit .env # Konfigurieren
  2. ./dev.sh build                    # Image bauen
  3. cd mcp && ./mcp.sh start          # MCP Gateway starten
  4. ./mcp.sh enable duckduckgo        # MCP Server aktivieren
  5. cd .. && ./dev.sh auth            # Einmalig: OAuth
  6. ./dev.sh start --docker           # Täglich: Mit Docker-Zugriff

${YELLOW}Workspace Workflow:${NC}
  # An lokaler Config schrauben:
  cd ~/.config && /path/to/dev.sh start --here --docker
  # Im Container: Startet direkt in ~/workspace/
EOF
}

ensure_network() {
    if ! docker network inspect ai-dev-network &>/dev/null; then
        echo -e "${BLUE}Creating ai-dev-network...${NC}"
        docker network create ai-dev-network
    fi
}

cmd_build() {
    echo -e "${BLUE}Building ai-dev-base image...${NC}"
    docker compose build
    echo -e "${GREEN}Done! Run './dev.sh start' to begin.${NC}"
}

cmd_start() {
    load_env
    check_config
    ensure_network

    # Parse arguments
    local docker_enabled="false"
    local firewall_enabled="false"
    local mount_path=""
    local skip_next="false"

    local args=("$@")
    for i in "${!args[@]}"; do
        if [[ "$skip_next" == "true" ]]; then
            skip_next="false"
            continue
        fi
        case "${args[$i]}" in
            --docker)   docker_enabled="true" ;;
            --firewall) firewall_enabled="true" ;;
            --here)     mount_path="$CALLER_PWD" ;;
            --mount)
                # Nächstes Argument ist der Pfad
                mount_path="${args[$((i+1))]:-}"
                skip_next="true"
                if [[ -z "$mount_path" ]]; then
                    echo -e "${RED}--mount requires a path argument${NC}"
                    exit 1
                fi
                ;;
        esac
    done

    # Validiere Mount-Pfad falls angegeben
    local extra_volume_args=""
    local workdir_arg=""
    if [[ -n "$mount_path" ]]; then
        # Tilde expandieren
        mount_path="${mount_path/#\~/$HOME}"
        # Absoluten Pfad sicherstellen
        if [[ -d "$mount_path" ]]; then
            mount_path="$(cd "$mount_path" && pwd)"
        else
            echo -e "${RED}Mount path does not exist or is not a directory: $mount_path${NC}"
            exit 1
        fi
        extra_volume_args="-v ${mount_path}:/home/dev/workspace"
        workdir_arg="--workdir /home/dev/workspace"
    fi

    # Get optional shell mounts
    local shell_mount_args
    shell_mount_args=$(get_shell_mount_args)

    # Compose files basierend auf Flags
    local compose_files
    compose_files=$(get_compose_files "$docker_enabled")

    echo ""
    echo -e "${BLUE}Starting AI Dev environment...${NC}"
    echo ""
    echo -e "   ${GREEN}Projects:${NC}  $CODE_DIR"

    # Status anzeigen
    if [[ "$docker_enabled" == "true" ]]; then
        echo -e "   ${GREEN}Docker:${NC}    Enabled (via secure proxy)"
    else
        echo -e "   ${YELLOW}Docker:${NC}    Disabled (use --docker to enable)"
    fi

    if [[ "$firewall_enabled" == "true" ]]; then
        echo -e "   ${GREEN}Firewall:${NC}  Enabled (outbound restricted)"
    else
        echo -e "   ${YELLOW}Firewall:${NC}  Disabled (use --firewall to enable)"
    fi

    if [[ -n "$mount_path" ]]; then
        echo -e "   ${GREEN}Workspace:${NC} $mount_path"
    fi

    if [[ "${SKIP_SHELL_MOUNTS:-false}" == "true" ]]; then
        echo -e "   ${YELLOW}Shell:${NC}     Using container defaults (SKIP_SHELL_MOUNTS=true)"
    elif [[ -n "$shell_mount_args" ]]; then
        echo -e "   ${GREEN}Shell:${NC}     Host config mounted"
    else
        echo -e "   ${YELLOW}Shell:${NC}     No host config found"
    fi
    echo ""

    # Container starten
    # shellcheck disable=SC2086
    ENABLE_FIREWALL="$firewall_enabled" \
        docker compose $compose_files run --rm $workdir_arg $extra_volume_args $shell_mount_args dev

    # Cleanup: Docker-Proxy stoppen falls gestartet
    if [[ "$docker_enabled" == "true" ]]; then
        docker compose $compose_files stop docker-proxy 2>/dev/null || true
        docker compose $compose_files rm -f docker-proxy 2>/dev/null || true
    fi
}

cmd_run() {
    # First argument must be the agent name (validate before expensive checks)
    local agent="${1:-}"
    if [[ -z "$agent" ]]; then
        echo -e "${RED}Usage: ./dev.sh run <agent> \"<prompt>\" [options]${NC}"
        echo ""
        echo "Available agents: claude, gemini, codex, opencode"
        exit 1
    fi
    shift

    # Validate agent name early
    case "$agent" in
        claude|gemini|codex|opencode) ;;
        *)
            echo -e "${RED}Unknown agent: $agent${NC}"
            echo "Available agents: claude, gemini, codex, opencode"
            exit 1
            ;;
    esac

    load_env
    check_config
    ensure_network

    # Collect prompt (all non-flag arguments before options)
    local prompt=""
    local run_args=()
    local write_enabled="false"
    local json_enabled="false"
    local model=""
    local expect_model="false"

    for arg in "$@"; do
        if [[ "$expect_model" == "true" ]]; then
            model="$arg"
            expect_model="false"
            continue
        fi
        case "$arg" in
            --write)    write_enabled="true" ;;
            --json)     json_enabled="true" ;;
            --model)    expect_model="true" ;;
            --docker|--firewall|--here|--mount)
                run_args+=("$arg") ;;
            *)
                # If it looks like a path after --mount, pass it through
                if [[ ${#run_args[@]} -gt 0 && "${run_args[-1]}" == "--mount" ]]; then
                    run_args+=("$arg")
                elif [[ -z "$prompt" ]]; then
                    prompt="$arg"
                else
                    # Append additional words to prompt
                    prompt="$prompt $arg"
                fi
                ;;
        esac
    done

    if [[ -z "$prompt" ]]; then
        echo -e "${RED}Error: No prompt provided.${NC}"
        echo ""
        echo -e "Usage: ./dev.sh run $agent \"<prompt>\" [options]"
        exit 1
    fi

    # Implicit --here: mount caller's pwd as workspace if no --mount was specified
    local has_mount="false"
    for arg in "${run_args[@]}"; do
        if [[ "$arg" == "--here" || "$arg" == "--mount" ]]; then
            has_mount="true"
            break
        fi
    done
    if [[ "$has_mount" == "false" ]]; then
        run_args+=("--here")
    fi

    # Parse container arguments (--docker, --firewall, --here, --mount)
    parse_container_args "${run_args[@]}"

    # Build agent command (prompt is passed via AGENT_PROMPT env var)
    local agent_cmd
    agent_cmd=$(get_agent_command "$agent" "$write_enabled" "$json_enabled" "$model") || exit 1

    echo "" >&2
    echo -e "${BLUE}Running $agent (headless)...${NC}" >&2
    echo "" >&2
    echo -e "   ${GREEN}Agent:${NC}     $agent" >&2
    echo -e "   ${GREEN}Workspace:${NC} ${PARSED_MOUNT_PATH:-$CALLER_PWD}" >&2

    if [[ -n "$model" ]]; then
        echo -e "   ${GREEN}Model:${NC}     $model" >&2
    fi
    if [[ "$write_enabled" == "true" ]]; then
        echo -e "   ${YELLOW}Mode:${NC}      Read/Write (--write)" >&2
    else
        echo -e "   ${GREEN}Mode:${NC}      Read-only (plan/analysis)" >&2
    fi

    if [[ "$PARSED_DOCKER" == "true" ]]; then
        echo -e "   ${GREEN}Docker:${NC}    Enabled" >&2
    fi
    if [[ "$PARSED_FIREWALL" == "true" ]]; then
        echo -e "   ${GREEN}Firewall:${NC}  Enabled" >&2
    fi
    if [[ "$json_enabled" == "true" ]]; then
        echo -e "   ${GREEN}Output:${NC}    JSON" >&2
    fi
    echo "" >&2

    # Run container ephemerally without TTY
    # - AGENT_PROMPT is passed as env var to avoid shell escaping issues
    # - entrypoint.sh ends with 'exec /bin/zsh "$@"'
    #   so we pass '-c "cmd"' which becomes 'zsh -c "cmd"'
    # shellcheck disable=SC2086
    ENABLE_FIREWALL="$PARSED_FIREWALL" AGENT_PROMPT="$prompt" \
        docker compose $PARSED_COMPOSE_FILES run --rm -T \
        -e AGENT_PROMPT \
        $PARSED_WORKDIR $PARSED_EXTRA_VOLUME $PARSED_SHELL_MOUNTS \
        dev -c "$agent_cmd"

    local exit_code=$?

    # Cleanup: Docker-Proxy stoppen falls gestartet
    if [[ "$PARSED_DOCKER" == "true" ]]; then
        docker compose $PARSED_COMPOSE_FILES stop docker-proxy 2>/dev/null || true
        docker compose $PARSED_COMPOSE_FILES rm -f docker-proxy 2>/dev/null || true
    fi

    return $exit_code
}

cmd_auth() {
    load_env
    check_config

    # Parse arguments
    local docker_enabled="false"

    for arg in "$@"; do
        case "$arg" in
            --docker) docker_enabled="true" ;;
        esac
    done

    local compose_files
    compose_files=$(get_compose_files "$docker_enabled")

    # Get optional shell mounts
    local shell_mount_args
    shell_mount_args=$(get_shell_mount_args)

    echo -e "${BLUE}Starting AI Dev with host network for OAuth authentication...${NC}"
    echo ""
    echo "This mode uses network_mode: host so OAuth callbacks work."
    echo "After authenticating Claude Code, Gemini CLI and Codex, exit and use './dev.sh start'"
    echo ""

    if [[ "$docker_enabled" == "true" ]]; then
        echo -e "${YELLOW}Docker-Proxy wird im Host-Network-Modus separat gestartet...${NC}"
        # Proxy manuell starten wenn im auth-modus mit docker
        # shellcheck disable=SC2086
        docker compose $compose_files up -d docker-proxy 2>/dev/null || true
        sleep 2
    fi

    # shellcheck disable=SC2086
    docker compose $compose_files --profile auth run --rm $shell_mount_args dev-auth

    # Proxy wieder stoppen
    if [[ "$docker_enabled" == "true" ]]; then
        # shellcheck disable=SC2086
        docker compose $compose_files stop docker-proxy 2>/dev/null || true
        docker compose $compose_files rm -f docker-proxy 2>/dev/null || true
    fi
}

cmd_status() {
    load_env

    echo -e "${BLUE}Configuration:${NC}"
    if [[ -n "${CODE_DIR:-}" ]]; then
        echo "  CODE_DIR: $CODE_DIR"
    else
        echo "  CODE_DIR: (not set)"
    fi
    echo ""

    echo -e "${BLUE}Containers:${NC}"
    docker ps -a --filter "name=ai-dev" --filter "name=mcp-" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
    echo ""

    echo -e "${BLUE}Volumes:${NC}"
    docker volume ls --filter "name=ai-dev" --filter "name=mcp-" --format "table {{.Name}}\t{{.Driver}}"
    echo ""

    echo -e "${BLUE}Networks:${NC}"
    docker network ls --filter "name=ai-dev" --format "table {{.Name}}\t{{.Driver}}"
    echo ""

    # Docker Proxy Status
    if docker ps --format '{{.Names}}' | grep -q '^ai-dev-docker-proxy$'; then
        echo -e "${GREEN}Docker Proxy: Running${NC}"
        # Proxy-Statistiken
        local blocked
        blocked=$(docker logs ai-dev-docker-proxy 2>&1 | grep -c "blocked" || echo "0")
        echo "   Blocked requests: $blocked"
    else
        echo -e "${YELLOW}Docker Proxy: Not running${NC}"
        echo "   Start with: ./dev.sh start --docker"
    fi
    echo ""

    # MCP Status
    if docker ps --format '{{.Names}}' | grep -q '^mcp-gateway$'; then
        echo -e "${GREEN}MCP Gateway: Running${NC}"
        if command -v docker &>/dev/null && docker mcp server ls &>/dev/null 2>&1; then
            echo "   Enabled servers:"
            docker mcp server ls 2>/dev/null | sed 's/^/   /'
        fi
    else
        echo -e "${YELLOW}MCP Gateway: Not running${NC}"
        echo "   Start with: cd mcp && ./mcp.sh start"
    fi
}

# -----------------------------------------------------------------------------
# Volume Categories
# -----------------------------------------------------------------------------
declare -A VOLUME_CATEGORIES=(
    # AI Agent credentials & settings
    [ai-dev-claude-config]="credentials"
    [ai-dev-gemini-config]="credentials"
    [ai-dev-codex-config]="credentials"
    [ai-dev-opencode-config]="credentials"
    [ai-dev-gh-config]="credentials"
    # Optional tool configs & installations
    [ai-dev-azure-config]="tools"
    [ai-dev-pulumi-config]="tools"
    [ai-dev-tools-cache]="tools"
    # Cache & data
    [ai-dev-uv-cache]="cache"
    [ai-dev-opencode-data]="data"
)

get_volumes_by_category() {
    local category="$1"
    local volumes=""
    for vol in "${!VOLUME_CATEGORIES[@]}"; do
        if [[ "${VOLUME_CATEGORIES[$vol]}" == "$category" ]]; then
            # Only include if volume exists
            if docker volume inspect "$vol" &>/dev/null; then
                volumes="$volumes $vol"
            fi
        fi
    done
    echo "$volumes" | xargs
}

get_all_ai_dev_volumes() {
    docker volume ls -q --filter "name=ai-dev" 2>/dev/null | xargs
}

list_volumes() {
    echo -e "${BLUE}AI Dev Volumes:${NC}"
    echo ""
    
    local has_volumes=false
    
    for category in credentials tools cache data; do
        local vols
        vols=$(get_volumes_by_category "$category")
        if [[ -n "$vols" ]]; then
            has_volumes=true
            case "$category" in
                credentials) echo -e "  ${YELLOW}Credentials:${NC}" ;;
                tools)       echo -e "  ${YELLOW}Tool Configs:${NC}" ;;
                cache)       echo -e "  ${YELLOW}Cache:${NC}" ;;
                data)        echo -e "  ${YELLOW}Data:${NC}" ;;
            esac
            for vol in $vols; do
                local size
                size=$(docker system df -v 2>/dev/null | grep "$vol" | awk '{print $4}' || echo "?")
                printf "    %-30s %s\n" "${vol#ai-dev-}" "$size"
            done
            echo ""
        fi
    done
    
    if [[ "$has_volumes" == "false" ]]; then
        echo "  No volumes found."
    fi
}

delete_volumes() {
    local volumes="$1"
    local force="${2:-false}"
    
    if [[ -z "$volumes" ]]; then
        echo -e "${YELLOW}No volumes to delete.${NC}"
        return
    fi
    
    echo -e "${RED}Volumes to delete:${NC}"
    for vol in $volumes; do
        echo "  - $vol"
    done
    echo ""
    
    if [[ "$force" != "true" ]]; then
        read -p "Are you sure? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Aborted."
            return
        fi
    fi
    
    for vol in $volumes; do
        if docker volume rm "$vol" 2>/dev/null; then
            echo -e "  ${GREEN}Deleted:${NC} $vol"
        else
            echo -e "  ${RED}Failed:${NC} $vol (in use?)"
        fi
    done
}

cmd_clean() {
    local subcommand="${1:-containers}"
    shift 2>/dev/null || true
    
    case "$subcommand" in
        containers|"")
            echo -e "${BLUE}Stopping and removing containers...${NC}"
            docker compose -f docker-compose.yml -f docker-compose.docker.yml down 2>/dev/null || true
            echo -e "${GREEN}Containers removed.${NC}"
            ;;
        
        --all)
            echo -e "${BLUE}Stopping containers...${NC}"
            docker compose -f docker-compose.yml -f docker-compose.docker.yml down 2>/dev/null || true
            echo ""
            echo -e "${RED}Removing ALL volumes (including credentials)...${NC}"
            local all_vols
            all_vols=$(get_all_ai_dev_volumes)
            delete_volumes "$all_vols" "true"
            echo ""
            echo -e "${RED}Removing network...${NC}"
            docker network rm ai-dev-network 2>/dev/null || true
            echo -e "${GREEN}Complete cleanup done.${NC}"
            ;;
        
        volumes)
            local vol_arg="${1:-}"
            
            case "$vol_arg" in
                ""|--list)
                    list_volumes
                    echo -e "${BLUE}Usage:${NC}"
                    echo "  ./dev.sh clean volumes --credentials  # Delete auth tokens"
                    echo "  ./dev.sh clean volumes --tools        # Delete tool configs (az, pulumi)"
                    echo "  ./dev.sh clean volumes --cache        # Delete caches"
                    echo "  ./dev.sh clean volumes <name>         # Delete specific volume"
                    ;;
                
                --credentials)
                    echo -e "${BLUE}Deleting credential volumes...${NC}"
                    delete_volumes "$(get_volumes_by_category credentials)"
                    ;;
                
                --tools)
                    echo -e "${BLUE}Deleting tool config volumes...${NC}"
                    delete_volumes "$(get_volumes_by_category tools)"
                    ;;
                
                --cache)
                    echo -e "${BLUE}Deleting cache volumes...${NC}"
                    delete_volumes "$(get_volumes_by_category cache)"
                    ;;
                
                --data)
                    echo -e "${BLUE}Deleting data volumes...${NC}"
                    delete_volumes "$(get_volumes_by_category data)"
                    ;;
                
                *)
                    # Specific volume name - try with and without prefix
                    local vol_name="$vol_arg"
                    if ! docker volume inspect "$vol_name" &>/dev/null; then
                        vol_name="ai-dev-$vol_arg"
                    fi
                    if docker volume inspect "$vol_name" &>/dev/null; then
                        delete_volumes "$vol_name"
                    else
                        echo -e "${RED}Volume not found: $vol_arg${NC}"
                        echo ""
                        list_volumes
                    fi
                    ;;
            esac
            ;;
        
        *)
            echo -e "${RED}Unknown subcommand: $subcommand${NC}"
            echo "Usage: ./dev.sh clean [containers|volumes|--all]"
            ;;
    esac
}

# -----------------------------------------------------------------------------
# Update CLI Agent Versions
# -----------------------------------------------------------------------------
cmd_update() {
    echo -e "${BLUE}Updating CLI agent versions...${NC}"
    "$SCRIPT_DIR/update-agents.sh"
}

# -----------------------------------------------------------------------------
# Audit-Log Funktion (für Debugging)
# -----------------------------------------------------------------------------
cmd_audit() {
    if ! docker ps --format '{{.Names}}' | grep -q '^ai-dev-docker-proxy$'; then
        echo -e "${RED}Docker Proxy is not running.${NC}"
        echo "   Start with: ./dev.sh start --docker"
        exit 1
    fi

    echo -e "${BLUE}Docker Proxy Audit Log (last 50 lines):${NC}"
    echo ""
    docker logs --tail 50 ai-dev-docker-proxy 2>&1
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
case "${1:-help}" in
    build)      cmd_build ;;
    start)      shift; cmd_start "$@" ;;
    run)        shift; cmd_run "$@" ;;
    auth)       shift; cmd_auth "$@" ;;
    status)     cmd_status ;;
    update)     cmd_update ;;
    clean)      shift; cmd_clean "$@" ;;
    audit)      cmd_audit ;;
    help|*)     usage ;;
esac
