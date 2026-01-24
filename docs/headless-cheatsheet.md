# Headless Mode Cheatsheet

Quick reference for running CLI agents in headless mode via `dev.sh run` or `agent_runner.py`.

---

## Models

### Claude Code

| Alias | Full Model Name | Beschreibung |
|-------|-----------------|--------------|
| `opus` | `claude-opus-4-5-20250929` | Stärkstes Modell, tiefes Reasoning, langsamer |
| `sonnet` | `claude-sonnet-4-5-20250929` | Schnell, kosteneffizient, guter Default |

### Gemini CLI

| Flag-Wert | Beschreibung |
|-----------|--------------|
| `gemini-2.5-pro` | Pro-Modell, starkes Reasoning, 1M Token Context |
| `gemini-2.5-flash` | Flash-Modell, schnell und kosteneffizient |

> Verfugbare Modelle prüfen: `gemini --model-list` oder im Container `gemini -m <model>`
> Die Gemini CLI README referenziert "Gemini 3 models" - bei neueren Versionen
> sind ggf. `gemini-3-pro` / `gemini-3-flash` verfügbar.

---

## Bash (dev.sh)

```bash
# Claude mit Opus (read-only, Analyse)
./dev.sh run claude "Erkläre die Architektur" --model opus

# Claude mit Sonnet (write, schneller)
./dev.sh run claude "Fix den Bug in main.py" --write --model sonnet

# Gemini Pro (tiefes Reasoning)
./dev.sh run gemini "Refactore die Auth-Logik" --write --model gemini-2.5-pro

# Gemini Flash (schnell)
./dev.sh run gemini "Fasse die README zusammen" --model gemini-2.5-flash

# JSON-Output für Scripting
./dev.sh run claude "Liste alle TODOs" --model sonnet --json | jq '.result'

# Anderes Workspace-Verzeichnis
./dev.sh run claude "Analysiere dieses Projekt" --model opus --mount ~/anderes-projekt
```

---

## Python (agent_runner.py)

### CLI

```bash
# Claude Opus
python3 scripts/agent_runner.py run claude "Erkläre die Architektur" --model opus

# Claude Sonnet + Write
python3 scripts/agent_runner.py run claude "Fix den Bug" --write --model sonnet

# Gemini Pro + JSON
python3 scripts/agent_runner.py run gemini "Refactore main.py" --write --model gemini-2.5-pro --json

# Liste verfügbarer Agents
python3 scripts/agent_runner.py list
```

### Als Python-Modul

```python
from agent_runner import AgentRunner

runner = AgentRunner(workspace="/path/to/project")

# Claude Opus - tiefe Analyse
result = runner.run(
    "claude",
    "Erkläre die Architektur und identifiziere potentielle Probleme",
    model="opus",
)
print(result.stdout)

# Claude Sonnet - schnelle Fixes
result = runner.run(
    "claude",
    "Fix alle Type Errors",
    write=True,
    model="sonnet",
)

# Gemini Pro - mit JSON output
result = runner.run(
    "gemini",
    "Analysiere die Dependencies auf Vulnerabilities",
    model="gemini-2.5-pro",
    json_output=True,
)

import json
data = json.loads(result.stdout)
```

---

## Flags-Referenz

| Flag | Bash (`dev.sh run`) | Python CLI | Python Modul |
|------|---------------------|------------|--------------|
| Agent | `<agent>` (positional) | `<agent>` (positional) | `agent=` |
| Prompt | `"<prompt>"` | `"<prompt>"` | `prompt=` |
| Model | `--model <name>` | `--model <name>` | `model=` |
| Write | `--write` | `--write` | `write=True` |
| JSON | `--json` | `--json` | `json_output=True` |
| Workspace | `--mount <path>` | `--workspace <path>` | `workspace=` |
| Docker | `--docker` | `--docker` | `docker_enabled=True` |
| Firewall | `--firewall` | `--firewall` | `firewall_enabled=True` |
| Timeout | - | `--timeout <sec>` | `timeout=` |

---

## Wann welches Modell?

| Use Case | Empfehlung | Warum |
|----------|-----------|-------|
| Architektur-Analyse | `opus` / `gemini-2.5-pro` | Tiefes Reasoning, komplexe Zusammenhänge |
| Quick Fixes | `sonnet` / `gemini-2.5-flash` | Schnell, kosteneffizient |
| Code Review | `opus` / `gemini-2.5-pro` | Gründliche Analyse |
| Refactoring | `sonnet` | Gute Balance Speed/Qualität |
| Batch-Scripting | `sonnet` / `gemini-2.5-flash` | Kosten bei vielen Aufrufen |
| Security Audit | `opus` | Maximale Gründlichkeit |
