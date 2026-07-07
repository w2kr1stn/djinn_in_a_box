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
CACHE_DIR="${CACHE_DIR:-$HOME/.cache/djinn-tools}"
INSTALLERS_DIR="${INSTALLERS_DIR:-$SCRIPT_DIR/installers}"
OUTPUT_LIB="${OUTPUT_LIB:-/home/dev/output-lib.sh}"

define_plain_ui_fallbacks() {
    ui_info() { echo "[info] $1" >&2; }
    ui_ok() { echo "[ok] $1" >&2; }
    ui_warn() { echo "[warn] $1" >&2; }
    ui_err() { echo "[err] $1" >&2; }
}

if [[ -r "$OUTPUT_LIB" ]] && source "$OUTPUT_LIB"; then
    :
else
    define_plain_ui_fallbacks
fi

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

ui_info "[tools] Checking optional tools..."

installed=0
skipped=0

for tool in $tools; do
    installer="$INSTALLERS_DIR/${tool}.sh"
    cache_marker="$CACHE_DIR/${tool}.installed"

    if [[ ! -f "$installer" ]]; then
        ui_warn "[tools] Unknown tool: $tool (no installer found)"
        continue
    fi

    # Check cache: marker exists AND the verify command succeeds.
    # Default convention: the installer name matches the binary name and the
    # binary supports --version. Installers with different semantics can declare
    # an override near the top of the installer script:
    #   # djinn-verify: <command>
    if [[ -f "$cache_marker" ]]; then
        verify_label=""
        verify_cmd=()
        if verify_line=$(grep -m 1 '^# djinn-verify:' "$installer"); then
            verify_label="${verify_line#\# djinn-verify:}"
            read -ra verify_cmd <<< "$verify_label"
            verify_label="${verify_cmd[*]}"
        else
            bin_path="$TOOLS_BIN/$tool"
            verify_cmd=("$bin_path" --version)
            verify_label="${verify_cmd[*]}"
        fi

        if [[ ${#verify_cmd[@]} -gt 0 ]] && "${verify_cmd[@]}" &>/dev/null; then
            skipped=$((skipped + 1))
            continue
        fi
        # Loud diagnostic: without it a stale or incorrect verifier can reinstall
        # silently on EVERY start.
        ui_warn \
            "[tools] Cache check failed for $tool (verify command failed: $verify_label) — reinstalling"
        rm -f "$cache_marker"
    fi

    ui_info "[tools] Installing $tool..."

    if output=$("$installer" 2>&1); then
        version=$(echo "$output" | tail -1)
        echo "$version" > "$cache_marker"
        ui_ok "[tools] $tool installed ($version)"
        installed=$((installed + 1))
    else
        ui_err "[tools] Failed to install $tool"
        echo "$output" | tail -3 | sed 's/^/    /'
    fi
done

if [[ $installed -gt 0 ]]; then
    ui_ok "[tools] $installed tool(s) installed, $skipped already installed"
elif [[ $skipped -gt 0 ]]; then
    ui_ok "[tools] $skipped tool(s) already installed (cached)"
fi
