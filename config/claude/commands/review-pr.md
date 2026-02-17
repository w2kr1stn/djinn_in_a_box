---
description: "Comprehensive PR review using specialized agents"
argument-hint: "[review-aspects: comments|tests|errors|types|code|simplify|all] [parallel]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Task"]
---

# PR Review

Trigger a comprehensive pull request review.

**Review aspects (optional):** $ARGUMENTS

Use the `pr-review` skill to execute the full review workflow.

Pass any specified review aspects to the skill for targeted analysis. Default: `all`.

## Quick Reference
- `/review-pr` — Full review (all aspects)
- `/review-pr tests errors` — Only test coverage + error handling
- `/review-pr code simplify` — Code quality + simplification
- `/review-pr all parallel` — All aspects, parallel execution
