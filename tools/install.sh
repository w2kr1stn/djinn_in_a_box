#!/bin/bash
# =============================================================================
# Optional Tools Installer
# =============================================================================
# Reads tools.txt and installs missing tools with caching.
# Cache markers are stored in ~/.cache/ai-dev-tools/
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_FILE="${TOOLS_FILE:-$SCRIPT_DIR/tools.txt}"
CACHE_DIR="$HOME/.cache/ai-dev-tools"
INSTALLERS_DIR="$SCRIPT_DIR/installers"

# Persistent tool directories (available to all installers)
export TOOLS_DIR="$CACHE_DIR"
export TOOLS_BIN="$CACHE_DIR/bin"
export TOOLS_LIB="$CACHE_DIR/lib"

# Ensure tool paths are available (entrypoint doesn't source .zshrc)
export PATH="$TOOLS_BIN:$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$TOOLS_LIB:${LD_LIBRARY_PATH:-}"

# Map tool name to binary name for verification
get_tool_binary() {
    case "$1" in
        azure-cli) echo "az" ;;
        *) echo "$1" ;;
    esac
}

# Colors (if terminal supports it)
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'
else
    GREEN='' YELLOW='' BLUE='' NC=''
fi

log_info()  { echo -e "${BLUE}[tools]${NC} $1"; }
log_ok()    { echo -e "${GREEN}[tools]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[tools]${NC} $1"; }

# Check if tools.txt exists
if [[ ! -f "$TOOLS_FILE" ]]; then
    exit 0
fi

mkdir -p "$CACHE_DIR" "$TOOLS_BIN" "$TOOLS_LIB"

# Read tools from file (ignore comments and empty lines)
tools=$(grep -v '^#' "$TOOLS_FILE" | grep -v '^[[:space:]]*$' | awk '{print $1}')

if [[ -z "$tools" ]]; then
    exit 0
fi

log_info "Checking optional tools..."

installed=0
skipped=0

for tool in $tools; do
    installer="$INSTALLERS_DIR/${tool}.sh"
    cache_marker="$CACHE_DIR/${tool}.installed"
    
    # Check if installer exists
    if [[ ! -f "$installer" ]]; then
        log_warn "Unknown tool: $tool (no installer found)"
        continue
    fi
    
    # Check cache - skip only if marker exists AND binary is still present
    if [[ -f "$cache_marker" ]]; then
        binary=$(get_tool_binary "$tool")
        if command -v "$binary" &>/dev/null; then
            skipped=$((skipped + 1))
            continue
        fi
        # Cache marker exists but binary is gone - reinstall
        rm -f "$cache_marker"
    fi
    
    log_info "Installing $tool..."
    
    # Run installer and capture version output
    if version=$("$installer" 2>&1 | tail -1); then
        echo "$version" > "$cache_marker"
        log_ok "$tool installed ($version)"
        installed=$((installed + 1))
    else
        log_warn "Failed to install $tool"
    fi
done

if [[ $installed -gt 0 ]]; then
    log_ok "$installed tool(s) installed, $skipped cached"
elif [[ $skipped -gt 0 ]]; then
    log_info "$skipped tool(s) already installed (cached)"
fi
