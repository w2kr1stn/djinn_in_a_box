---
allowed-tools: Bash(*), Read, Write
description: Initialize a new project with modular monolith architecture
argument-hint: [Project name]
---

[AGENTS]: lead-architect, backend-engineer, qa-specialist

### `/init-project [PROJEKTNAME]` (Project Bootstrap)

**Action**: Initialize a new project with modular monolith architecture.

**Step 1**: Use `lead-architect` to create a comprehensive `PROJECT.md` and initial `CLAUDE.md`.

**Architecture**: Modular Monolith with Layered Architecture.
- **Structure per module**: `api/` (Routes) -> `services/` (Business Logic) -> `repositories/` (Data Access) -> `models/` (Schemas/DB).
- **Communication**: Modules communicate via Services, not directly via Repositories or Databases.

**Step 2**: Create the repository base structure:
- Backend: Python 3.14 (FastAPI) with `uv` structure.
- Frontend: Next.js (Vite/TS) in `web/` or `frontend/` subdirectory.
- Documentation: `GOVERNANCE.md` (base rules) and `GUIDELINES.md` (SOLID, DRY, KISS).

**Step 3**: Use `backend-engineer` to implement the first "Hello World" API module (e.g., `health-check`) strictly following the Layer structure.

**Step 4**: Use `qa-specialist` to validate the structure and ensure Ruff and Pyright hooks work correctly.

**Goal**: A runnable skeleton (including basics: Input-Validation, Error-Handling, Logging) that demonstrates modular separation, so future features can simply be "plugged in".

**Language**: Chat in German, create all artifacts in English.
