"""Design theme — colors, styles, and icons for Rich console output."""

from rich.style import Style
from rich.theme import Theme

PRIMARY = "#69B9A1"
SECONDARY = "#226666"
SUCCESS = "#C1FF62"
ERROR = "#F53263"
WARNING = "#FAF870"
# Deliberately ANSI blue color 4, matching the old shell \033[0;34m rendering.
INFO = "blue"
PATH = "#8608B8"
MUTED = "#333676"
BORDER = "#29526D"

DJINN_THEME: Theme = Theme(
    {
        # Semantic Message Styles
        "success": Style(color=SUCCESS),
        "error": Style(color=ERROR),
        "warning": Style(color=WARNING),
        "info": Style(color=INFO),
        "info.bold": Style(color=INFO, bold=True),
        "path": Style(color=PATH),
        # Primary/Accent Styles
        "primary": Style(color=PRIMARY),
        "primary.bold": Style(color=PRIMARY, bold=True),
        "secondary": Style(color=SECONDARY),
        "secondary.bold": Style(color=SECONDARY, bold=True),
        # Text Styles
        "muted": Style(color=MUTED),
        "border": Style(color=BORDER),
        # Header Styles
        "header": Style(color=PRIMARY, bold=True),
        # Status Indicator Styles
        "status.enabled": Style(color=SUCCESS),
        "status.disabled": Style(color=WARNING),
        "status.error": Style(color=ERROR),
        # Table Styles
        "table.title": Style(color=PRIMARY, bold=True),
        "table.header": Style(color=PRIMARY, bold=True),
        "table.category": Style(color=SECONDARY),
        "table.value": Style(color=MUTED),
    }
)

ICONS: dict[str, str] = {
    "success": "\u2713",  # ✓
    "error": "\u2717",  # ✗
    "warning": "\u26a0",  # ⚠
    "info": "\u2139",  # ℹ
}
