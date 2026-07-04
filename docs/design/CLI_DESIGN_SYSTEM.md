# Djinn CLI Design System

This document describes the shipped CLI output system for `djinn` and
`mcpgateway`. The source of truth for Python styling is
`src/djinn_in_a_box/core/theme.py`; the source of truth for container-startup
shell styling is `scripts/output-lib.sh`.

## Palette

All Python color values are defined once as constants in `theme.py`. Command
modules use Rich role names, never literal colors.

| Role | Value | Usage |
|------|-----|-------|
| `primary` | `#69B9A1` | Brand color, rule titles, table titles and headers, command highlights, emphasized values. |
| `secondary` | `#226666` | Depth color for table categories and the lower banner logo gradient; avoid small unbolded body text. |
| `success` | `#C1FF62` | Success icon and enabled/running/connected states. |
| `error` | `#F53263` | Error icon, failures, aborts, and failed states. |
| `warning` | `#FAF870` | Warning icon, disabled states, gaps, and non-fatal missing optional tools. |
| `info` | ANSI blue color 4 | Info icon and informational messages; terminal-adaptive and plain weight. |
| `path` | `#8608B8` | Filesystem paths in status output and config displays. |
| `muted` | `#333676` | Secondary labels, hints, timestamps, and table values. |
| `border` | `#29526D` | Rule lines, table borders, and separators. |

Terminal default text is intentionally not a palette color. It remains unset so
the CLI respects the user's terminal theme.

## Derived Roles

`DJINN_THEME` exposes semantic roles used by commands:

| Derived role | Mapping |
|--------------|---------|
| `header` | `primary`, bold |
| `table.title` | `primary`, bold |
| `table.header` | `primary`, bold |
| `table.category` | `secondary` |
| `table.value` | `muted` |
| `path` | `#8608B8` |
| `status.enabled` | `success` |
| `status.disabled` | `warning` |
| `status.error` | `error` |
| `primary.bold`, `secondary.bold`, `info.bold` | Bold variants of their base roles |

The status icons live in `ICONS`: success `✓`, error `✗`, warning `⚠`, and
info `ℹ`. Command code should use `success()`, `error()`, `warning()`, `info()`,
`status_line()`, `rule()`, or table roles instead of embedding color names.

## Python Components

### Rules

Section structure uses `rule(title)` to render `─ Title ─...`. The title uses
`primary.bold`; the line uses `border`. `rule()` owns section spacing by writing
exactly one blank line before the rule, and the rendered rule line spans the
current Rich console width.

Examples of shipped rule sections include `djinn start` (`Environment`,
`Container`), `djinn status`, `djinn doctor`, `djinn clean volumes`, `djinn
agents`, and `mcpgateway status`.

### Status Lines

`status_line(label, value, style="status.enabled", value_style=None)` writes
aligned operational state to stderr. Use `value_style="path"` only for
filesystem path values such as Projects, Workspace, CODE_DIR, and sync roots.
Use the `status.*` roles for state labels and message roles for message helpers.

### Tables

Rich tables use the central table roles:

```python
table = Table(
    title="Djinn Volumes",
    title_style="table.title",
    header_style="table.header",
    border_style="border",
)
table.add_column("Category", style="table.category")
table.add_column("Volume", style="table.value")
```

Shipped table users include `config show`, `status`, `doctor`, `agents`, and
resource lists used by `clean volumes`.

## Banner

`src/djinn_in_a_box/core/banner.py` renders the startup banner for `djinn start`.
It never raises to the caller; if rendering fails it falls back to the plain
title.

The banner has three modes:

| Mode | Predicate | Output |
|------|-----------|--------|
| Full | UTF-8 output, color enabled, terminal width at least 70 columns | Braille djinn logo with a `primary` to `secondary` vertical gradient plus the block `DJINN` wordmark and muted `in a box` tagline. |
| Wordmark | UTF-8 output, but narrow or colorless | Block `DJINN` wordmark and muted tagline. |
| Plain | Non-empty `NO_COLOR`, dumb terminal, non-UTF-8 output, or fallback path | `Djinn in a Box`. |

Font support for Braille glyphs is best-effort and is not auto-detected.

## Startup Structure

`djinn start` uses a two-world output model:

| Producer | Sections |
|----------|----------|
| Python/Rich | Banner, `Environment`, `Container`. |
| Container shell | `Seed & Config`, `MCP`, `Tools`, `Security`. |

Python writes startup UI through `err_console`, so UI goes to stderr. The
interactive Docker run inherits host stdio; shell-side startup messages also
write to stderr.

## Shell Output Library

`scripts/output-lib.sh` is sourced by the container entrypoint and MCP
registration script. It provides:

| Function | Purpose |
|----------|---------|
| `ui_section "Title"` | Rule-like section line. |
| `ui_ok "Message"` | Success marker. |
| `ui_warn "Message"` | Warning marker. |
| `ui_err "Message"` | Error marker. |
| `ui_info "Message"` | Info marker. |
| `ui_boxed "Title"` | Reads body lines from stdin and renders indented external output in an open-right muted frame. |
| `ui_item "Marker" "Message"` | Generic info-colored item marker unless a caller explicitly passes another role. |

Shell sections print exactly one blank line before the rule. `ui_section`
resolves rule width from `COLUMNS`, then `tput cols`, then the second field of
`stty size`, and finally falls back to 80 columns. The visible rule line spans
that resolved width exactly.

The library uses exact Python palette RGB values when `COLORTERM` is
`truecolor` or `24bit`. Otherwise it uses ANSI-256 approximations:

| Role | Truecolor | ANSI-256 fallback |
|------|-----------|-------------------|
| `primary` | `38;2;105;185;161` | `73` |
| `secondary` | `38;2;34;102;102` | `23` |
| `success` | `38;2;193;255;98` | `155` |
| `error` | `38;2;245;50;99` | `203` |
| `warning` | `38;2;250;248;112` | `227` |
| `info` | ANSI blue `0;34` | ANSI blue `0;34` |
| `path` | `38;2;134;8;184` | `91` |
| `muted` | `38;2;51;54;118` | `60` |
| `border` | `38;2;41;82;109` | `24` |

Color is enabled only when stderr is a TTY, unless the test hook
`DJINN_FORCE_UI_COLOR=1` is set. `NO_COLOR` wins only when it is present with a
non-empty value.

`ui_boxed "Title"` is for captured output from external tools inside startup
sections. It writes nothing for empty stdin. When body text is present, it
renders a short frame indented two spaces, colors the frame and body with
`muted`, and leaves the right side open so long tool output such as URLs cannot
overrun a right border. Plain mode uses ASCII-only `+-`, `|`, and `+---` frame
lines.

## NO_COLOR And Plain Output

Python Rich output follows Rich's `no_color` behavior. The banner uses plain mode
when color is disabled or the terminal is dumb.

Shell output uses plain ASCII-compatible markers when `NO_COLOR` is non-empty
or when stderr is not a TTY:

| Color marker | Plain marker |
|--------------|--------------|
| `✓` | `[ok]` |
| `⚠` | `[warn]` |
| `✗` | `[err]` |
| `ℹ` | `[info]` |

Plain shell sections render as `- Title ...` and contain no ANSI escape
sequences.

## Stream Discipline

Operational UI belongs on stderr. This keeps stdout available for command data
and agent output. Existing examples:

| Surface | stdout | stderr |
|---------|--------|--------|
| `djinn run` | Agent stdout | Run status and agent stderr |
| `djinn session --prompt` | Agent stdout | Agent stderr and errors |
| `djinn config path` | Raw config path | Errors only |
| `djinn start` | Interactive container stdout | Startup UI |
| Shell startup | None for UI | Section lines, markers, and startup diagnostics |

When a command intentionally returns data, keep that data on stdout and put
status/progress on stderr.
