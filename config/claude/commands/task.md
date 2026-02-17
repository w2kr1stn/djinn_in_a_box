---
allowed-tools: Bash(*), Read, Write
description: Trigger a lightweight, collaborative engineering session
---

[AGENTS]: lead-architect, code-engineer

### `/task` (Pair Programming Mode)
**Action**: Trigger a lightweight, collaborative engineering session.
1. **Persona**: Adopt a hybrid role (Architect + Engineer).
2. **Protocol Override**:
   - **Skip**: `TASKS.yml` update, `PLAN.md` update (unless crucial).
   - **Maintain**: SOTA principles, Tests (Must still be green!), Linting.
3. **Workflow**:
   - Briefly discuss the goal with the user.
   - Edit code immediately (iterative feedback loop).
   - Run tests.
   - **Constraint**: Do not leave the code in a broken state. If complex, advise switching back to standard workflow (`lead-architect`).
