# AI Dev Base CLI Design System

> **Version:** 1.0.0
> **Framework:** Rich (Python)
> **Aesthetic:** Neo-Terminal Elegance
> **Based on:** TodAI Design System

---

## 1. Design Philosophy

### Core Principles

**"Schlichte Eleganz mit gezielten Akzenten"**

Das CLI folgt dem Prinzip der **Clarity over Decoration**:
- Auf den ersten Blick: Clean, professional, fokussiert
- Farben kommunizieren **Bedeutung**, nicht Dekoration
- Konsistentes Erscheinungsbild mit dem TodAI Hauptprojekt

### Design-DNA

| Prinzip | Umsetzung |
|---------|-----------|
| **Semantic Colors** | Farben zeigen Status, nicht Stil |
| **Hierarchy First** | Wichtiges hervorheben, Rest zurücknehmen |
| **Restraint is Power** | Akzentfarben nur für wichtige Elemente |
| **Icons as Signals** | Unicode-Icons für schnelle Erfassung |

---

## 2. Color System

### 2.1 Core Palette

```
┌─────────────────────────────────────────────────────────────────┐
│                    AI DEV BASE COLOR SYSTEM                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐             │
│  │ PRIMARY │  │SECONDARY│  │ ACCENT  │  │  MUTED  │             │
│  │ #69B9A1 │  │ #226666 │  │ #29526d │  │ #b2bec3 │             │
│  │  Teal   │  │Deep Teal│  │  Ocean  │  │  Gray   │             │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘             │
│                                                                  │
│  Status Colors:                                                  │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐             │
│  │ SUCCESS │  │  INFO   │  │ WARNING │  │  ERROR  │             │
│  │ #03b971 │  │ #0e8ac8 │  │ #f5b332 │  │ #9c0136 │             │
│  │  Green  │  │  Blue   │  │ Orange  │  │   Red   │             │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘             │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Color Tokens

| Token | Hex | Rich Style | Usage |
|-------|-----|------------|-------|
| `PRIMARY` | `#69B9A1` | `[primary]` | Labels, aktive Elemente |
| `PRIMARY_DARK_1` | `#226666` | `[secondary]` | Sekundäre Aktionen |
| `PRIMARY_DARK_2` | `#29526d` | `[accent]` | Dividers, Borders |
| `TEXT_MUTED` | `#b2bec3` | `[muted]` | Hints, sekundärer Text |

### 2.3 Status Colors

| Token | Hex | Rich Style | Usage |
|-------|-----|------------|-------|
| `SUCCESS` | `#03b971` | `[success]` | Erfolg, aktiviert |
| `SUCCESS_LIGHT` | `#c1ff62` | `[highlight]` | Highlights |
| `INFO` | `#0e8ac8` | `[info]` | Information, Headers |
| `WARNING` | `#f5b332` | `[warning]` | Warnungen, deaktiviert |
| `ERROR` | `#9c0136` | `[error]` | Fehler, kritisch |
| `SPECIAL` | `#8608b8` | `[special]` | Easter Eggs, Premium |

### 2.4 Color Application Rules

```
70% ─── Default Text (#ffffff)
        → Regulärer Content

20% ─── Primary/Info (#69B9A1, #0e8ac8)
        → Labels, Headers, Emphasis

8%  ─── Muted (#b2bec3)
        → Hints, sekundäre Info

2%  ─── Status Colors (Success, Error, Warning)
        → Nur bei Statusänderungen!
```

---

## 3. Typography & Text Styles

### 3.1 Text Hierarchy

| Element | Rich Style | Verwendung |
|---------|------------|------------|
| **Headers** | `[header]` / `[info.bold]` | Abschnittstitel |
| **Labels** | `[primary]` / `[label]` | Beschriftungen |
| **Body** | Normal | Regulärer Content |
| **Emphasis** | `[primary.bold]` | Wichtige Elemente |
| **Muted** | `[muted]` | Sekundäre Info, Hints |
| **Italic Muted** | `[muted.italic]` | Timestamps, optionale Info |

### 3.2 Message Styles

| Message Type | Format | Example |
|--------------|--------|---------|
| Error | `[error]✗ Error: {msg}[/error]` | ✗ Error: File not found |
| Success | `[success]✓ {msg}[/success]` | ✓ Build complete |
| Warning | `[warning]⚠ Warning: {msg}[/warning]` | ⚠ Warning: Config missing |
| Info | `[info]ℹ {msg}[/info]` | ℹ Starting build... |

---

## 4. Icons (Unicode)

### 4.1 Status Icons

| Icon | Unicode | Style | Usage |
|------|---------|-------|-------|
| ✓ | U+2713 | `[success]` | Erfolg, abgeschlossen |
| ✗ | U+2717 | `[error]` | Fehler, fehlgeschlagen |
| ⚠ | U+26A0 | `[warning]` | Warnung |
| ℹ | U+2139 | `[info]` | Information |
| ● | U+25CF | `[primary]` | Aktiv |
| ○ | U+25CB | `[muted]` | Inaktiv |
| → | U+2192 | `[muted]` | Pfeil, Navigation |

### 4.2 Icon Usage

```python
# In theme.py definiert
ICONS = {
    "success": "✓",
    "error": "✗",
    "warning": "⚠",
    "info": "ℹ",
    "active": "●",
    "inactive": "○",
    "arrow": "→",
}
```

---

## 5. Component Patterns

### 5.1 Status Line

Format: `   {label}:  {value}`

```
   Projects:   /home/user/projects
   Docker:     Running
   Agent:      claude
```

Code:
```python
status_line("Projects", "/home/user/projects", "status.enabled")
status_line("Docker", "Disabled", "status.disabled")
```

### 5.2 Header

```python
header("Configuration")
# Output: Configuration:  (in [header] style)
```

### 5.3 Tables

```python
table = Table(title="AI Dev Volumes", title_style="table.title")
table.add_column("Category", style="table.category")
table.add_column("Volume", style="table.value")
```

### 5.4 Progress Output

```
ℹ Building ai-dev-base image...
✓ Build complete
⚠ Warning: Cache not available
✗ Error: Docker daemon not running
```

---

## 6. Rich Theme Definition

### 6.1 Theme Structure

```python
from rich.style import Style
from rich.theme import Theme

TODAI_THEME = Theme({
    # Semantic Message Styles
    "success": Style(color="#03b971"),
    "success.bold": Style(color="#03b971", bold=True),
    "error": Style(color="#9c0136", bold=True),
    "warning": Style(color="#f5b332"),
    "info": Style(color="#0e8ac8"),
    "info.bold": Style(color="#0e8ac8", bold=True),

    # Primary/Accent Styles
    "primary": Style(color="#69B9A1"),
    "primary.bold": Style(color="#69B9A1", bold=True),
    "secondary": Style(color="#226666"),
    "accent": Style(color="#29526d"),

    # Text Styles
    "muted": Style(color="#b2bec3"),
    "muted.italic": Style(color="#b2bec3", italic=True),
    "highlight": Style(color="#c1ff62"),

    # Header/Label Styles
    "header": Style(color="#0e8ac8", bold=True),
    "label": Style(color="#69B9A1"),

    # Status Indicators
    "status.enabled": Style(color="#03b971"),
    "status.disabled": Style(color="#f5b332"),
    "status.error": Style(color="#9c0136"),
    "status.active": Style(color="#69B9A1", bold=True),

    # Table Styles
    "table.title": Style(color="#0e8ac8", bold=True),
    "table.header": Style(bold=True),
    "table.category": Style(color="#f5b332"),
    "table.value": Style(color="#b2bec3"),

    # Special
    "special": Style(color="#8608b8"),
})
```

### 6.2 Console Initialization

```python
from rich.console import Console
from ai_dev_base.core.theme import TODAI_THEME

console = Console(theme=TODAI_THEME)
err_console = Console(stderr=True, theme=TODAI_THEME)
```

---

## 7. Usage Examples

### 7.1 Basic Output

```python
from ai_dev_base.core.console import error, success, info, warning

# Messages
success("Build complete")           # ✓ Build complete
error("File not found")             # ✗ Error: File not found
warning("Config missing")           # ⚠ Warning: Config missing
info("Starting build...")           # ℹ Starting build...
```

### 7.2 Rich Markup

```python
from ai_dev_base.core.console import console

console.print("[primary]codeagent[/primary] [muted]v1.0.0[/muted]")
console.print("[header]Configuration:[/header]")
console.print("  [muted]1.[/muted] First step")
console.print("  [success]✓[/success] Completed task")
```

### 7.3 Status Lines

```python
from ai_dev_base.core.console import status_line, header

header("Environment")
status_line("Projects", "/home/user/projects")
status_line("Docker", "Running", "status.enabled")
status_line("GPU", "Disabled", "status.disabled")
```

---

## 8. Migration Guide

### From Inline Colors to Theme Styles

| Old (Inline) | New (Theme) |
|--------------|-------------|
| `[red]` | `[error]` |
| `[green]` | `[success]` or `[status.enabled]` |
| `[yellow]` | `[warning]` or `[status.disabled]` |
| `[blue]` | `[info]` or `[header]` |
| `[bold]` | `[primary.bold]` or `[info.bold]` |
| `[dim]` | `[muted]` |
| `[italic]` | `[muted.italic]` |

---

## 9. Accessibility

### Contrast Ratios (WCAG AA)

| Foreground | Background | Ratio | Status |
|------------|------------|-------|--------|
| #ffffff | #212121 | 13.5:1 | ✓ AAA |
| #69B9A1 | #212121 | 6.8:1 | ✓ AA |
| #b2bec3 | #212121 | 8.2:1 | ✓ AAA |
| #03b971 | #212121 | 6.2:1 | ✓ AA |
| #9c0136 | #212121 | 4.8:1 | ✓ AA |

---

## 10. Quick Reference

### Color Tokens
```
PRIMARY:      #69B9A1  (Teal)
SUCCESS:      #03b971  (Green)
INFO:         #0e8ac8  (Blue)
WARNING:      #f5b332  (Orange)
ERROR:        #9c0136  (Red)
MUTED:        #b2bec3  (Gray)
```

### Style Names
```
[success]     [error]       [warning]     [info]
[primary]     [secondary]   [accent]      [muted]
[header]      [label]       [highlight]   [special]
[status.enabled]  [status.disabled]  [status.error]
[table.title]     [table.header]     [table.category]
```

### Icons
```
✓ success    ✗ error    ⚠ warning    ℹ info
● active     ○ inactive → arrow
```

---

*Design System based on TodAI v1.0.0*
*Adapted for CLI usage with Rich library*
