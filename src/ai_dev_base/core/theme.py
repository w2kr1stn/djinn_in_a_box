"""TodAI Design Theme — colors, styles, and icons for Rich console output."""

from rich.style import Style
from rich.theme import Theme

TODAI_THEME: Theme = Theme(
    {
        # Semantic Message Styles
        "success": Style(color="#03b971"),
        "error": Style(color="#9c0136", bold=True),
        "warning": Style(color="#f5b332"),
        "info": Style(color="#0e8ac8"),
        "info.bold": Style(color="#0e8ac8", bold=True),
        # Primary/Accent Styles
        "primary": Style(color="#69B9A1"),
        "primary.bold": Style(color="#69B9A1", bold=True),
        # Text Styles
        "muted": Style(color="#b2bec3"),
        # Header Styles
        "header": Style(color="#0e8ac8", bold=True),
        # Status Indicator Styles
        "status.enabled": Style(color="#03b971"),
        "status.disabled": Style(color="#f5b332"),
        "status.error": Style(color="#9c0136"),
        # Table Styles
        "table.title": Style(color="#0e8ac8", bold=True),
        "table.header": Style(bold=True),
        "table.category": Style(color="#f5b332"),
        "table.value": Style(color="#b2bec3"),
    }
)

ICONS: dict[str, str] = {
    "success": "\u2713",  # ✓
    "error": "\u2717",  # ✗
    "warning": "\u26a0",  # ⚠
    "info": "\u2139",  # ℹ
}
