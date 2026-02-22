#!/bin/bash
# =============================================================================
# Optional Tools Installer
# =============================================================================
# Reads tools.txt and installs missing tools with caching.
# Cache is invalidated on image rebuild (via ~/.build-timestamp).
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_FILE="${TOOLS_FILE:-$SCRIPT_DIR/tools.txt}"
CACHE_DIR="$HOME/.cache/djinn-tools"
INSTALLERS_DIR="$SCRIPT_DIR/installers"

# Persistent tool directories (available to all installers)
export TOOLS_DIR="$CACHE_DIR"
export TOOLS_BIN="$CACHE_DIR/bin"
export TOOLS_LIB="$CACHE_DIR/lib"

# Ensure tool paths are available (entrypoint doesn't source .zshrc)
export PATH="$TOOLS_BIN:$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$TOOLS_LIB:${LD_LIBRARY_PATH:-}"

if [[ ! -f "$TOOLS_FILE" ]]; then
    exit 0
fi

mkdir -p "$CACHE_DIR" "$TOOLS_BIN" "$TOOLS_LIB"

# Invalidate cache when image was rebuilt
BUILD_TS=$(cat ~/.build-timestamp 2>/dev/null || echo "0")
CACHE_TS="$CACHE_DIR/.build-timestamp"
if [[ ! -f "$CACHE_TS" ]] || [[ "$(cat "$CACHE_TS")" != "$BUILD_TS" ]]; then
    rm -f "$CACHE_DIR"/*.installed
    echo "$BUILD_TS" > "$CACHE_TS"
fi

# Read tools from file (ignore comments and empty lines)
tools=$(grep -v '^#' "$TOOLS_FILE" | grep -v '^[[:space:]]*$' | awk '{print $1}')

if [[ -z "$tools" ]]; then
    exit 0
fi

echo "[tools] Checking optional tools..."

installed=0
skipped=0

for tool in $tools; do
    installer="$INSTALLERS_DIR/${tool}.sh"
    cache_marker="$CACHE_DIR/${tool}.installed"

    if [[ ! -f "$installer" ]]; then
        echo "[tools] Unknown tool: $tool (no installer found)"
        continue
    fi

    # Check cache: marker exists AND binary is in PATH
    if [[ -f "$cache_marker" ]]; then
        binary="$tool"; [[ "$tool" == "azure-cli" ]] && binary="az"
        if command -v "$binary" &>/dev/null; then
            skipped=$((skipped + 1))
            continue
        fi
        rm -f "$cache_marker"
    fi

    echo "[tools] Installing $tool..."

    if version=$("$installer" 2>&1 | tail -1); then
        echo "$version" > "$cache_marker"
        echo "[tools] ✓ $tool installed ($version)"
        installed=$((installed + 1))
    else
        echo "[tools] ✗ Failed to install $tool"
    fi
done

if [[ $installed -gt 0 ]]; then
    echo "[tools] $installed tool(s) installed, $skipped already installed"
elif [[ $skipped -gt 0 ]]; then
    echo "[tools] $skipped tool(s) already installed (cached)"
fi
