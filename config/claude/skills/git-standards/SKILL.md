---
name: git-standards
description: Enforces branch naming, commit messages, and PR templates. Use when creating branches, committing, or drafting PRs.
---
# GIT STANDARDS

### Branch Naming
Format: `category/issue-[NUMBER]/kebab-case-description`
(e.g., `feature/issue-21/build-awesome-feature`)

### Commits
Format: `<type>[optional scope]: <description>`
Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`.
- Imperative style, max 50 chars, no period at end.
- Body: Explain What & Why (max 72 chars/line).
- Footer: `Refs: #X`.

### Pull Request Template
Title: `<Type>/issue <No.>/<description>`
Body must include:
- ## Sub-Issue [#No.]: [TITLE]
- ### Implementation (Checklist)
- ### Quality Assurance (CI Results)
- ### Architecture Impact
- Closes #X, Part of #Y