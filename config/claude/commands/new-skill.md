---
allowed-tools: Bash(*), Read, Write
description: Create a new skill.
argument-hint: [Description of new skill to be created]
---

[AGENTS]: lead-architect
[SKILLS]: skill-creator

### `/new-skill [Description]` (Skill Factory)
**Action**: Trigger `lead-architect` to build a new tool.
1. Adopt the **Lead Architect** persona.
2. Analyze the `[Description]` to understand the desired capability.
3. Use the `skill-creator` tool to scaffold, implement, and package the new skill in `agents/skills/`.
4. Validate the skill using `quick_validate.py` (if available in skill-creator).