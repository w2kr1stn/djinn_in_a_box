---
allowed-tools: Bash(*), Read, Write
description: Trigger a lightweight, collaborative engineering session
---

[AGENTS]: backend-engineer, frontend-developer

### `/task` (Pair Programming Mode)
**Action**: Trigger a lightweight, collaborative engineering session.
1. **Persona**: Adopt a hybrid role (Architect + Engineer).
2. **Protocol Override**:
   - **Skip**: `TASK.yml` update, `PROJECT.md` update (unless crucial).
   - **Skip**: Strict `SESSION.md` creation (summarize in chat instead).
   - **Maintain**: SOTA principles, Tests (Must still be green!), Linting.
3. **Workflow**:
   - Briefly discuss the goal with the user.
   - Edit code immediately (iterative feedback loop).
   - Run tests.
   - **Constraint**: Do not leave the code in a broken state. If complex, advise switching back to standard workflow.