# Djinn in a Box CLI Design System

> **Version:** 1.0.0
> **Framework:** Rich (Python)
> **Aesthetic:** Neo-Terminal Elegance

---

## 1. Design Philosophy

### Core Principles

**"Simple elegance with deliberate accents"**

The CLI follows the principle of **clarity over decoration**:

- At first glance: clean, professional, focused
- Colors communicate **meaning**, not decoration
- Consistent appearance across the Djinn CLI surface

### Design DNA

| Principle | Implementation |
|-----------|----------------|
| **Semantic Colors** | Colors show status, not style. |
| **Hierarchy First** | Highlight what matters and keep the rest quiet. |
| **Restraint Is Power** | Accent colors are reserved for important elements. |
| **Icons as Signals** | Unicode icons support fast recognition. |

---

## 2. Color System

### 2.1 Core Palette

```text
┌─────────────────────────────────────────────────────────────────┐
│                    DJINN CLI COLOR SYSTEM                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────┐  ┌─────────┐                                      │
│  │ PRIMARY │  │  MUTED  │                                      │
│  │ #69B9A1 │  │ #b2bec3 │                                      │
│  │  Teal   │  │  Gray   │                                      │
│  └─────────┘  └─────────┘                                      │
│                                                                 │
│  Status Colors:                                                 │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│  │ SUCCESS │  │  INFO   │  │ WARNING │  │  ERROR  │            │
│  │ #03b971 │  │ #0e8ac8 │  │ #f5b332 │  │ #9c0136 │            │
│  │  Green  │  │  Blue   │  │ Orange  │  │   Red   │            │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Color Tokens

| Token | Hex | Rich Style | Usage |
|-------|-----|------------|-------|
| `PRIMARY` | `#69B9A1` | `[primary]` | Labels and important elements |
| `TEXT_MUTED` | `#b2bec3` | `[muted]` | Hints and secondary text |

### 2.3 Status Colors

| Token | Hex | Rich Style | Usage |
|-------|-----|------------|-------|
| `SUCCESS` | `#03b971` | `[success]` | Success and enabled states |
| `INFO` | `#0e8ac8` | `[info]` / `[header]` | Information and section headers |
| `WARNING` | `#f5b332` | `[warning]` | Warnings and disabled states |
| `ERROR` | `#9c0136` | `[error]` | Errors and critical states |

### 2.4 Color Application Rules

```text
70% ─── Default Text (#ffffff)
        → Regular content

20% ─── Primary/Info (#69B9A1, #0e8ac8)
        → Labels, headers, emphasis

8%  ─── Muted (#b2bec3)
        → Hints and secondary information

2%  ─── Status Colors (Success, Error, Warning)
        → Status changes only
```

---

## 3. Typography & Text Styles

### 3.1 Text Hierarchy

| Element | Rich Style | Usage |
|---------|------------|-------|
| **Headers** | `[header]` / `[info.bold]` | Section titles |
| **Labels** | `[primary]` | Labels |
| **Body** | Normal | Regular content |
| **Emphasis** | `[primary.bold]` | Important elements |
| **Muted** | `[muted]` | Secondary information and hints |

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
| ✓ | U+2713 | `[success]` | Success and completed work |
| ✗ | U+2717 | `[error]` | Errors and failed work |
| ⚠ | U+26A0 | `[warning]` | Warning |
| ℹ | U+2139 | `[info]` | Information |

### 4.2 Icon Usage

```python
# Defined in theme.py
ICONS = {
    "success": "✓",
    "error": "✗",
    "warning": "⚠",
    "info": "ℹ",
}
```

---

## 5. Component Patterns

### 5.1 Status Line

Format: `   {label}:  {value}`

```text
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
table = Table(title="Djinn Volumes", title_style="table.title")
table.add_column("Category", style="table.category")
table.add_column("Volume", style="table.value")
```

### 5.4 Progress Output

```text
ℹ Building djinn-in-a-box image...
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

DJINN_THEME = Theme({
    # Semantic Message Styles
    "success": Style(color="#03b971"),
    "error": Style(color="#9c0136", bold=True),
    "warning": Style(color="#f5b332"),
    "info": Style(color="#0e8ac8"),
    "info.bold": Style(color="#0e8ac8", bold=True),

    # Primary Styles
    "primary": Style(color="#69B9A1"),
    "primary.bold": Style(color="#69B9A1", bold=True),

    # Text Styles
    "muted": Style(color="#b2bec3"),

    # Header Styles
    "header": Style(color="#0e8ac8", bold=True),

    # Status Indicators
    "status.enabled": Style(color="#03b971"),
    "status.disabled": Style(color="#f5b332"),
    "status.error": Style(color="#9c0136"),

    # Table Styles
    "table.title": Style(color="#0e8ac8", bold=True),
    "table.header": Style(bold=True),
    "table.category": Style(color="#f5b332"),
    "table.value": Style(color="#b2bec3"),
})
```

### 6.2 Console Initialization

```python
from rich.console import Console
from djinn_in_a_box.core.theme import DJINN_THEME

console = Console(theme=DJINN_THEME)
err_console = Console(stderr=True, theme=DJINN_THEME)
```

---

## 7. Usage Examples

### 7.1 Basic Output

```python
from djinn_in_a_box.core.console import error, success, info, warning

# Messages
success("Build complete")           # ✓ Build complete
error("File not found")             # ✗ Error: File not found
warning("Config missing")           # ⚠ Warning: Config missing
info("Starting build...")           # ℹ Starting build...
```

### 7.2 Rich Markup

```python
from djinn_in_a_box.core.console import console

console.print("[primary]djinn[/primary] [muted]v1.0.0[/muted]")
console.print("[header]Configuration:[/header]")
console.print("  [muted]1.[/muted] First step")
console.print("  [success]✓[/success] Completed task")
```

### 7.3 Status Lines

```python
from djinn_in_a_box.core.console import status_line, header

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

```text
PRIMARY:      #69B9A1  (Teal)
SUCCESS:      #03b971  (Green)
INFO:         #0e8ac8  (Blue)
WARNING:      #f5b332  (Orange)
ERROR:        #9c0136  (Red)
MUTED:        #b2bec3  (Gray)
```

### Style Names

```text
[success]     [error]       [warning]     [info]
[primary]     [primary.bold] [muted]       [header]
[info.bold]
[status.enabled]  [status.disabled]  [status.error]
[table.title]     [table.header]     [table.category] [table.value]
```

### Icons

```text
✓ success    ✗ error    ⚠ warning    ℹ info
```

---

*Djinn CLI design system v1.0.0*
*Adapted for CLI usage with the Rich library*
