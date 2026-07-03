# Contributing

Thanks for helping improve Djinn in a Box. This project uses a small, direct
workflow: keep changes focused, add tests for behavior changes, and run the
same quality gates before opening a pull request.

## Development Setup

Install `uv`, clone the repository, then create the project environment:

```sh
uv sync --dev
```

For local CLI testing, install the project in editable mode:

```sh
uv tool install --editable .
```

After editable installation, verify the CLI is available:

```sh
djinn --version
```

## Quality Gates

Run these checks before opening a pull request:

```sh
uv run ruff check src/ tests/
uv run pyright src/
uv run pytest -q
uvx bandit -r src/ --severity-level medium --confidence-level medium
```

The CI workflow runs the same gates on pull requests and pushes to `master`.

## Commit Style

Use concise conventional commit subjects:

```text
docs: add contribution guide
fix: handle missing config file
test: cover session creation errors
```

Use one logical change per commit when practical. Avoid generated metadata or
tool-attribution footers.

## Pull Requests

Before opening a pull request:

- Describe the user-visible change and any compatibility impact.
- Link the issue when one exists.
- Include test notes with the exact commands you ran.
- Keep unrelated cleanup out of the pull request.

For general project questions, open a GitHub issue. For security reports, use
the private reporting process described in `SECURITY.md`; the fallback contact
there is maintained by w2kr1stn.
