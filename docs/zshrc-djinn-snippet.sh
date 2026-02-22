# =============================================================================
# DEPRECATED: Old Shell Wrapper for dev.sh
# =============================================================================
#
# This file is no longer needed! The Python CLI is now available natively.
#
# ## Migration Guide
#
# 1. Install the CLI tools:
#    cd djinn-in-a-box
#    uv tool install .
#
# 2. Remove old shell wrappers from ~/.zshrc or ~/.zshrc.local:
#    - Delete the djinn() function
#    - Delete the mcpgateway() function
#    - Delete the _djinn_completion function
#
# 3. (Optional) Enable Typer shell completion:
#    djinn --install-completion
#    mcpgateway --install-completion
#
# The CLI commands are now directly available:
#    djinn start
#    djinn run claude "prompt"
#    mcpgateway start
#    mcpgateway enable duckduckgo
#
# =============================================================================

# If you still want a "here" shortcut, you can add this simple alias:
# alias ca-here="djinn start --here"
