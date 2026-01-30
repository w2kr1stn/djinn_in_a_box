"""TodAI Design Theme for Rich console output.

This module defines the central theme system for the AI Dev Base CLI,
providing consistent colors, styles, and icons based on the TodAI
Design System.

Color Palette:
    - Primary: Teal (#69B9A1) for active elements and labels
    - Secondary: Deep Teal (#226666) for secondary actions
    - Accent: Ocean (#29526d) for borders and dividers
    - Status colors for semantic meaning (success, error, warning, info)

Usage:
    from ai_dev_base.core.theme import TODAI_THEME, ICONS
    from rich.console import Console

    console = Console(theme=TODAI_THEME)
    console.print(f"[success]{ICONS['success']} Build complete[/success]")
"""

from rich.style import Style
from rich.theme import Theme

# =============================================================================
# Color Token Constants
# =============================================================================

# Primary Palette
PRIMARY: str = "#69B9A1"
"""Teal - Active elements, focus, labels."""

PRIMARY_DARK_1: str = "#226666"
"""Deep Teal - Secondary actions."""

PRIMARY_DARK_2: str = "#29526d"
"""Ocean - Borders, dividers."""

PRIMARY_DARK_3: str = "#333676"
"""Indigo - Inactive elements."""

# Surface Palette
SURFACE: str = "#212121"
"""Background color."""

SURFACE_LIGHT_1: str = "#2a2a2a"
"""Elevated surface."""

SURFACE_LIGHT_2: str = "#333333"
"""Hover state."""

# Text Palette
TEXT: str = "#ffffff"
"""Primary text color."""

TEXT_MUTED: str = "#b2bec3"
"""Secondary text, hints."""

# Status Colors
SUCCESS: str = "#03b971"
"""Green - Success, enabled."""

SUCCESS_LIGHT: str = "#c1ff62"
"""Light green - Highlights."""

INFO: str = "#0e8ac8"
"""Blue - Information, headers."""

INFO_LIGHT: str = "#0ec1c8"
"""Cyan - Secondary info."""

WARNING: str = "#f5b332"
"""Orange - Warnings, disabled."""

WARNING_LIGHT: str = "#faf870"
"""Yellow - Caution."""

ERROR: str = "#9c0136"
"""Red - Errors, critical."""

SPECIAL: str = "#8608b8"
"""Purple - Special elements."""


# =============================================================================
# TodAI Rich Theme
# =============================================================================

TODAI_THEME: Theme = Theme(
    {
        # ---------------------------------------------------------------------
        # Semantic Message Styles
        # ---------------------------------------------------------------------
        "success": Style(color=SUCCESS),
        "success.bold": Style(color=SUCCESS, bold=True),
        "error": Style(color=ERROR, bold=True),
        "warning": Style(color=WARNING),
        "info": Style(color=INFO),
        "info.bold": Style(color=INFO, bold=True),
        # ---------------------------------------------------------------------
        # Primary/Accent Styles
        # ---------------------------------------------------------------------
        "primary": Style(color=PRIMARY),
        "primary.bold": Style(color=PRIMARY, bold=True),
        "secondary": Style(color=PRIMARY_DARK_1),
        "accent": Style(color=PRIMARY_DARK_2),
        # ---------------------------------------------------------------------
        # Text Styles
        # ---------------------------------------------------------------------
        "muted": Style(color=TEXT_MUTED),
        "muted.italic": Style(color=TEXT_MUTED, italic=True),
        "highlight": Style(color=SUCCESS_LIGHT),
        # ---------------------------------------------------------------------
        # Header/Label Styles
        # ---------------------------------------------------------------------
        "header": Style(color=INFO, bold=True),
        "label": Style(color=PRIMARY),
        "label.muted": Style(color=TEXT_MUTED),
        # ---------------------------------------------------------------------
        # Status Indicator Styles
        # ---------------------------------------------------------------------
        "status.enabled": Style(color=SUCCESS),
        "status.disabled": Style(color=WARNING),
        "status.error": Style(color=ERROR),
        "status.active": Style(color=PRIMARY, bold=True),
        # ---------------------------------------------------------------------
        # Table Styles
        # ---------------------------------------------------------------------
        "table.title": Style(color=INFO, bold=True),
        "table.header": Style(bold=True),
        "table.category": Style(color=WARNING),
        "table.value": Style(color=TEXT_MUTED),
        # ---------------------------------------------------------------------
        # Special
        # ---------------------------------------------------------------------
        "special": Style(color=SPECIAL),
    }
)
"""Rich Theme with TodAI semantic styles."""


# =============================================================================
# Icon Constants
# =============================================================================

ICONS: dict[str, str] = {
    "success": "\u2713",  # ✓
    "error": "\u2717",  # ✗
    "warning": "\u26a0",  # ⚠
    "info": "\u2139",  # ℹ
    "active": "\u25cf",  # ●
    "inactive": "\u25cb",  # ○
    "arrow": "\u2192",  # →
}
"""Unicode icons for status indicators and navigation."""


# =============================================================================
# Public API
# =============================================================================

__all__: list[str] = [
    # Color Tokens - Primary
    "PRIMARY",
    "PRIMARY_DARK_1",
    "PRIMARY_DARK_2",
    "PRIMARY_DARK_3",
    # Color Tokens - Surface
    "SURFACE",
    "SURFACE_LIGHT_1",
    "SURFACE_LIGHT_2",
    # Color Tokens - Text
    "TEXT",
    "TEXT_MUTED",
    # Color Tokens - Status
    "SUCCESS",
    "SUCCESS_LIGHT",
    "INFO",
    "INFO_LIGHT",
    "WARNING",
    "WARNING_LIGHT",
    "ERROR",
    "SPECIAL",
    # Theme & Icons
    "TODAI_THEME",
    "ICONS",
]
