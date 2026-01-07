# Project Memory: [PROJEKT_NAME]

## 🎯 Context & Phase
- **Current Phase**: MVP (Evolution: PoC -> **MVP** -> Production -> Scale)
- **Tech-Debt Tolerance**: **Minimal** (High-quality code required for ISO 27001)
- **Primary Mission**: [Kurze Beschreibung des Projektziels]

## 🔄 Workflow Lifecycle (Strict)
1. **Architect**: Analyze -> Specs (`PROJECT.md`/`FEATURE.md`) -> Plan (`DEV.json` with ~1h tasks).
2. **Dev (Back/Front)**: Analyze -> Implement (SOTA/DRY/KISS) -> Test -> Report (`SESSION.md`) -> Update Issue.
3. **QA**: Review (`SESSION.md` + Code) -> CI Check -> PR (Green) or Reject (Red).

## 🛠 Tech Stack
- **Backend**: Python 3.14, FastAPI, SQLAlchemy 2.0, PostgreSQL
- **Frontend**: React.js, TypeScript, Shadcn, Tailwind, Vite
- **Tooling**: uv, Ruff, Pyright, Prettier, ESLint, Bandit

## 🛢️ Database
- **Supabase**: For inspection of data and data models use the Supabase MCP (via Docker MCP Gateway) & the supabase-inspect skill (from weber-ai)

## 🏗 Architecture Principles
- **Pattern**: Modular Monolith -> Layer Architecture
- **Strict Separation**: Router -> Services -> Repositories -> Database
- **Persistence**: Repository pattern only. No direct DB access in services.
- **SOTA**: DRY, KISS, YAGNI, SOLID.

## 🔐 Compliance (ISO 27001)
- **Secrets**: Redact as `[PLACEHOLDER_UPPER_SNAKE_CASE]`.
- **Roadmap**: Q1-26 Istio mTLS, Q2-26 CrossGuard GDPR.

## 🤖 Agent Instructions
- **Communication**: Chat in **German**, Code & Docs in **English**.
- **QA Gate**: Tests >= 85%, Doc-coverage 100%. Fail on Bandit Medium/High.
- **PRs**: Base branch = Story-Branch. NEVER merge to `main`.

## 💻 Operational Commands
- **Backend**: `uv run ruff check --fix .`, `uv run pyright .`, `pytest`, `uv run testcov`
- **Frontend**: `npm run lint`, `npm run format`, `npm run test`
- **Infra**: `docker compose up -d`

## ⚡ Slash Command Triggers
If the user starts a message with one of these commands, strictly follow the associated protocol.

## ✔️ Git Commits
- **Exlude**: `TASKS.md`, `PROJECT.md` / `FEATURE.md`, `SESSION*.md` & this particular `CLAUDE.md` file
- **Avoid (stricly)**: AI-generated Footer in commit message:"""
    🤖 Generated with Claude Code

    Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"""
- or similar!