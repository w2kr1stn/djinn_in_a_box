**Initialisiere neues Projekt: [PROJEKTNAME]**

**Schritt 1**: Nutze den lead-architect, um eine umfassende PROJECT.md und die initiale CLAUDE.md zu erstellen. **Architektur-Vorgabe**: Modularer Monolith mit Layered Architecture.

**Struktur pro Modul**: `api/` (Routes) -> `services/` (Business Logic) -> `repositories/` (Data Access) -> `models/` (Schemas/DB).

**Kommunikation**: Module kommunizieren über Services, nicht direkt über Repositories oder Datenbanken.

**Schritt 2**: Erstelle die Grundstruktur des Repositories:

Backend: Python 3.14 (FastAPI) mit `uv` Struktur.

Frontend: Next.js (Vite/TS) im Unterordner `web/` oder `frontend/`.

Dokumentation:`GOVERNANCE.md` (Basis-Regeln laden) und `GUIDELINES.md` (SOLID, DRY, KISS).

**Schritt 3**: Nutze den `backend-engineer`, um das erste "Hello World" API-Modul (z.B. `health-check`) strikt nach der Layer-Struktur zu implementieren.

**Schritt 4**: Nutze den `qa-specialist`, um die Struktur zu validieren und sicherzustellen, dass die Hooks für Ruff und Pyright im Container korrekt greifen.

**Ziel**: Ein lauffähiges Skelett (inkludierte Basics: Input-Validation, Error-Handling, Logging), das die modulare Trennung vorlebt, damit zukünftige Features nur noch "eingehängt" werden müssen. Chatte mit mir auf Deutsch, erstelle alle Artefakte auf Englisch.