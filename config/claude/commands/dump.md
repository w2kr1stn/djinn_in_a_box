---
allowed-tools: Bash(*), Read, Write
description: Create log file of current conversation -> `logs/context_dump_[TIMESTAMP].md`
---

[AGENTS]: **all**

### `/dump` (Context Snapshot)
**Action**: Create a backup of the current conversation state.
1. Summarize the current discussion, agreed decisions, and pending steps.
2. Write this summary + the full conversation history (if possible) into a file: `logs/context_dump_[TIMESTAMP].md`.
3. Confirm with: "✅ Context dumped to logs/..."