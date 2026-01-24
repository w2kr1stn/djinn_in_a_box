# Repository Governance: Modular Monolith

## 1. Architectural Mandate
This project strictly follows the pattern of a **modular monolith** with a **layered architecture**. Any deviation must be explicitly approved by the owner.

It is part of the wber-ai-toolkit project (toolkit-backend: FastAPI, toolkit-frontend: React, toolkit-infra Pulumi (Python))

### Layer Definitions & Responsibilities (FastAPI Backend)
| Layer | Responsibility | Constraints |
| :--- | :--- | :--- |
| **API** | Request/Response Handling, Routing | No business logic, no DB calls. |
| **Service** | Core Business Logic, Orchestration | Calls repositories, knows no web details. |
| **Repository** | Data Access, CRUD Operations | Pure SQL/ORM logic. No business logic. |
| **Models** | Schemas, Domain Entities, DB Models | Pure definitions (Pydantic/SQLAlchemy). |

### Communication Rules
- **Intra-Module**: Communication follows the flow: `API -> Service -> Repository`.
- **Inter-Module**: Modules may ONLY communicate via the services of other modules. Direct access to external repositories or database tables is prohibited.

---

## 2. Development Workflow

### Planning & Execution
- **Hierarchy**: All work originates from `TASK.yml` (GitHub Issue -> TASK.yml, Sub-Issues).
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

## 3. Quality Gates (The “Hard” Rules)
Before a task is considered “Done,” the following criteria must be met:

| Tool | Requirement | Scope |
| :--- | :--- | :--- |
| **Ruff** | Clean (lint & format) | Python Files |
| **Pyright** | 0 Errors (Strict Type Checking) | Python Files |
| **Pytest** | All green, Coverage >= 85% | Business Logic |
| **Bandit** | No Medium/High issues | Security |
| **ESLint/Prettier** | Clean | TS/React Files |

---

## 4. Compliance & Security
- **Secrets Management**: SOPS (age).
- **Documentation**: Google-style docstrings for all public functions.
- **ISO 27001 Baseline**: Code reviews by the `qa-specialist` agent are mandatory for every PR.
