"""TodAI Design Theme for Rich console output.

Defines the central theme for the AI Dev Base CLI with consistent
colors, styles, and icons.

Usage:
    from ai_dev_base.core.theme import TODAI_THEME, ICONS
    from rich.console import Console

    console = Console(theme=TODAI_THEME)
    console.print(f"[success]{ICONS['success']} Build complete[/success]")
"""

from rich.style import Style
from rich.theme import Theme

# =============================================================================
# Color Tokens
# =============================================================================

PRIMARY = "#69B9A1"
TEXT_MUTED = "#b2bec3"
SUCCESS = "#03b971"
INFO = "#0e8ac8"
WARNING = "#f5b332"
ERROR = "#9c0136"

# =============================================================================
# TodAI Rich Theme
# =============================================================================

TODAI_THEME: Theme = Theme(
    {
        # Semantic Message Styles
        "success": Style(color=SUCCESS),
        "error": Style(color=ERROR, bold=True),
        "warning": Style(color=WARNING),
        "info": Style(color=INFO),
        "info.bold": Style(color=INFO, bold=True),
        # Primary/Accent Styles
        "primary": Style(color=PRIMARY),
        "primary.bold": Style(color=PRIMARY, bold=True),
        # Text Styles
        "muted": Style(color=TEXT_MUTED),
        # Header Styles
        "header": Style(color=INFO, bold=True),
        # Status Indicator Styles
        "status.enabled": Style(color=SUCCESS),
        "status.disabled": Style(color=WARNING),
        "status.error": Style(color=ERROR),
        # Table Styles
        "table.title": Style(color=INFO, bold=True),
        "table.header": Style(bold=True),
        "table.category": Style(color=WARNING),
        "table.value": Style(color=TEXT_MUTED),
    }
)

# =============================================================================
# Icon Constants
# =============================================================================

ICONS: dict[str, str] = {
    "success": "\u2713",  # ✓
    "error": "\u2717",  # ✗
    "warning": "\u26a0",  # ⚠
    "info": "\u2139",  # ℹ
}
