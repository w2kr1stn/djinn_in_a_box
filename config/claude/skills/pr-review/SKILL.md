---
description: Comprehensive PR review using specialized agents for code quality analysis
argument-hint: "[review-aspects: comments|tests|errors|types|code|simplify|all] [parallel]"
---

# Comprehensive PR Review

Run a comprehensive pull request review using multiple specialized agents, each focusing on a different aspect of code quality.

**Review Aspects (optional):** $ARGUMENTS

## Review Workflow

### 1. Determine Review Scope
- Check git status to identify changed files
- Parse arguments to see if user requested specific review aspects
- Default: Run all applicable reviews

### 2. Available Review Aspects

- **comments** — Analyze code comment accuracy and maintainability
- **tests** — Review test coverage quality and completeness
- **errors** — Check error handling for silent failures
- **types** — Analyze type design and invariants (if new types added)
- **code** — General code review for project guidelines
- **simplify** — Simplify code for clarity and maintainability
- **all** — Run all applicable reviews (default)

### 3. Identify Changed Files
- Run `git diff --name-only` to see modified files
- Check if PR already exists: `gh pr view`
- Identify file types and what reviews apply

### 4. Determine Applicable Reviews

Based on changes:
- **Always applicable**: code-reviewer (general quality)
- **If test files changed**: pr-test-analyzer
- **If comments/docs added**: comment-analyzer
- **If error handling changed**: silent-failure-hunter
- **If types added/modified**: type-design-analyzer
- **After passing review**: code-simplifier (polish and refine)

### 5. Launch Review Agents

**Sequential approach** (one at a time):
- Easier to understand and act on
- Each report is complete before next
- Good for interactive review

**Parallel approach** (user can request via `parallel` argument):
- Launch all agents simultaneously
- Faster for comprehensive review
- Results come back together

### 6. Aggregate Results

After agents complete, summarize:

```markdown
# PR Review Summary

## Critical Issues (X found)
- [agent-name]: Issue description [file:line]

## Important Issues (X found)
- [agent-name]: Issue description [file:line]

## Suggestions (X found)
- [agent-name]: Suggestion [file:line]

## Strengths
- What's well-done in this PR

## Recommended Action
1. Fix critical issues first
2. Address important issues
3. Consider suggestions
4. Re-run review after fixes
```

### 7. Provide Action Plan

Organize findings by priority and present a clear action plan.

## Re-Review

After fixes have been applied, this skill can be invoked again to verify:
- All critical issues from the previous review are resolved
- No new issues were introduced by the fixes
- Focus the re-review on previously flagged areas unless `all` is specified

## Agent Descriptions

**comment-analyzer**:
- Verifies comment accuracy vs code
- Identifies comment rot
- Checks documentation completeness

**pr-test-analyzer**:
- Reviews behavioral test coverage
- Identifies critical gaps
- Evaluates test quality

**silent-failure-hunter**:
- Finds silent failures
- Reviews catch blocks
- Checks error logging

**type-design-analyzer**:
- Analyzes type encapsulation
- Reviews invariant expression
- Rates type design quality

**code-reviewer**:
- Checks CLAUDE.md compliance
- Detects bugs and issues
- Reviews general code quality

**code-simplifier**:
- Simplifies complex code
- Improves clarity and readability
- Applies project standards
- Preserves functionality

## Tips

- **Run early**: Before creating PR, not after
- **Focus on changes**: Agents analyze git diff by default
- **Address critical first**: Fix high-priority issues before lower priority
- **Re-run after fixes**: Verify issues are resolved
- **Use specific reviews**: Target specific aspects when you know the concern
- Agents run autonomously and return detailed reports
- Each agent focuses on its specialty for deep analysis
- Results are actionable with specific file:line references
