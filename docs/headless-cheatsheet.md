# Headless Mode Cheatsheet

Quick reference for running CLI agents in headless mode via `djinn run`.

---

## Models

### Claude Code

| Alias | Full Model Name | Beschreibung |
|-------|-----------------|--------------|
| `opus` | `claude-opus-4-5-20250929` | Staerkstes Modell, tiefes Reasoning, langsamer |
| `sonnet` | `claude-sonnet-4-5-20250929` | Schnell, kosteneffizient, guter Default |

### Gemini CLI

| Flag-Wert | Beschreibung |
|-----------|--------------|
| `gemini-2.5-pro` | Pro-Modell, starkes Reasoning, 1M Token Context |
| `gemini-2.5-flash` | Flash-Modell, schnell und kosteneffizient |

> Verfuegbare Modelle pruefen: `gemini --model-list` oder im Container `gemini -m <model>`
> Die Gemini CLI README referenziert "Gemini 3 models" - bei neueren Versionen
> sind ggf. `gemini-3-pro` / `gemini-3-flash` verfuegbar.

---

## CLI Usage (djinn run)

```bash
# Claude mit Opus (read-only, Analyse)
djinn run claude "Erklaere die Architektur" --model opus

# Claude mit Sonnet (write, schneller)
djinn run claude "Fix den Bug in main.py" --write --model sonnet

# Gemini Pro (tiefes Reasoning)
djinn run gemini "Refactore die Auth-Logik" --write --model gemini-2.5-pro

# Gemini Flash (schnell)
djinn run gemini "Fasse die README zusammen" --model gemini-2.5-flash

# JSON-Output fuer Scripting
djinn run claude "Liste alle TODOs" --model sonnet --json | jq '.result'

# Anderes Workspace-Verzeichnis
djinn run claude "Analysiere dieses Projekt" --model opus --mount ~/anderes-projekt

# Mit Docker-Zugriff
djinn run claude "Build und teste das Projekt" --docker

# Verfuegbare Agents anzeigen
djinn agents
```

---

## Flags-Referenz

| Flag | Description |
|------|-------------|
| `<agent>` | Agent name: `claude`, `gemini`, `codex`, `opencode` |
| `<prompt>` | The prompt/task to execute |
| `--model <name>` | Model to use (e.g., `opus`, `sonnet`, `gemini-2.5-pro`) |
| `--write` | Enable write/edit mode |
| `--json` | Output as JSON for scripting |
| `--mount <path>` | Mount additional workspace directory |
| `--docker` | Enable Docker socket access |
| `--firewall` | Enable network firewall |
| `--timeout <sec>` | Timeout in seconds |

---

## Wann welches Modell?

| Use Case | Empfehlung | Warum |
|----------|-----------|-------|
| Architektur-Analyse | `opus` / `gemini-2.5-pro` | Tiefes Reasoning, komplexe Zusammenhaenge |
| Quick Fixes | `sonnet` / `gemini-2.5-flash` | Schnell, kosteneffizient |
| Code Review | `opus` / `gemini-2.5-pro` | Gruendliche Analyse |
| Refactoring | `sonnet` | Gute Balance Speed/Qualitaet |
| Batch-Scripting | `sonnet` / `gemini-2.5-flash` | Kosten bei vielen Aufrufen |
| Security Audit | `opus` | Maximale Gruendlichkeit |
