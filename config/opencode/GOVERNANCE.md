# Repository Governance: Modular Monolith

## 1. Architectural Mandate
Dieses Projekt folgt strikt dem Muster eines **Modularen Monolithen** mit einer **Layered Architecture**. Jede Abweichung muss explizit vom Owner genehmigt werden.



### Layer Definitions & Responsibilities
| Layer | Responsibility | Constraints |
| :--- | :--- | :--- |
| **API** | Request/Response Handling, Routing | Keine Business-Logik, keine DB-Aufrufe. |
| **Service** | Core Business Logic, Orchestrierung | Ruft Repositories auf, kennt keine Web-Details. |
| **Repository** | Data Access, CRUD Operations | Reine SQL/ORM Logik. Keine Business-Logik. |
| **Models** | Schemas, Domain Entities, DB-Models | Reine Definitionen (Pydantic/SQLAlchemy). |

### Communication Rules
- **Intra-Module**: Kommunikation folgt dem Flow: `API -> Service -> Repository`.
- **Inter-Module**: Module dürfen NUR über die Services anderer Module kommunizieren. Direkte Zugriffe auf fremde Repositories oder Datenbank-Tabellen sind verboten.

---

## 2. Development Workflow

### Planning & Execution
- **Hierarchy**: All work originates from `DEV.json` (Epic -> Story -> Task).
- **Task Size**: Sub-Issues must be scoped to ~1h execution time.
- **Reporting**: Every dev session MUST conclude with a `SESSION.md` artifact detailing the implementation and test results.

### Branching & Naming
- **Standard**: `category/issue-[NO]/description-kebab-case`.
- **Categories**: `feature/`, `bugfix/`, `refactor/`, `test/`.
- **Base Branch**: Pull Requests target the current Story-Branch (never direct to `main`).

### Commit Standards
- **Format**: Conventional Commits (`feat:`, `fix:`, `refactor:`, etc.).
- **Requirement**: Imperative, precise, reference Issue ID (`Refs: #X`).

---

## 3. Quality Gates (The "Hard" Rules)
Bevor eine Aufgabe als "Done" gilt, müssen folgende Kriterien erfüllt sein:

| Tool | Requirement | Scope |
| :--- | :--- | :--- |
| **Ruff** | Clean (lint & format) | Python Files |
| **Pyright** | 0 Errors (Strict Type Checking) | Python Files |
| **Pytest** | All green, Coverage >= 85% | Business Logic |
| **Bandit** | No Medium/High issues | Security |
| **ESLint/Prettier** | Clean | TS/React Files |

---

## 4. Compliance & Security
- **Secrets Management**: Absolutes Verbot von Plaintext-Secrets. Nutzung von `[PLACEHOLDER_UPPER_SNAKE_CASE]` in Plänen.
- **Documentation**: Google-Style Docstrings für alle öffentlichen Funktionen.
- **ISO 27001 Baseline**: Code-Reviews durch den `qa-specialist` Agent sind obligatorisch für jeden PR.