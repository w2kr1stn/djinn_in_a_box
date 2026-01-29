# =============================================================================
# DEPRECATED: Old Shell Wrapper for dev.sh
# =============================================================================
#
# This file is no longer needed! The Python CLI is now available natively.
#
# ## Migration Guide
#
# 1. Install the CLI tools:
#    cd ai-dev-base
#    uv tool install .
#
# 2. Remove old shell wrappers from ~/.zshrc or ~/.zshrc.local:
#    - Delete the codeagent() function
#    - Delete the mcpgateway() function
#    - Delete the _codeagent_completion function
#
# 3. (Optional) Enable Typer shell completion:
#    codeagent --install-completion
#    mcpgateway --install-completion
#
# The CLI commands are now directly available:
#    codeagent start
#    codeagent run claude "prompt"
#    mcpgateway start
#    mcpgateway enable duckduckgo
#
# =============================================================================

# If you still want a "here" shortcut, you can add this simple alias:
# alias ca-here="codeagent start --here"
