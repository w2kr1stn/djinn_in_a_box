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
(cd helper/hostctl && go test ./... && CGO_ENABLED=0 go build ./...)
```

CI runs these gates on pull requests and pushes to `master`.

Lint tracked shell scripts with Bash. Install zsh for its syntax checks:

```bash
set -euo pipefail
shellcheck_files=()
while IFS= read -r -d '' script; do
  if head -n 1 "$script" | grep -q '^#!.*zsh'; then
    zsh -n "$script"
  else
    shellcheck_files+=("$script")
  fi
done < <(git ls-files -z '*.sh')
uvx --from shellcheck-py==0.11.0.1 shellcheck "${shellcheck_files[@]}"
```

Audit every locked dependency group with the same flags as CI:

```bash
set -euo pipefail
requirements="$(mktemp)"
ignored=(
  # --ignore-vuln <ID>  # <package>: <reason>; revisit by <YYYY-MM-DD>
)
uv export --frozen --all-groups --no-emit-project --format requirements-txt -o "$requirements"
uvx pip-audit==2.10.1 --requirement "$requirements" --require-hashes --disable-pip --progress-spinner off "${ignored[@]}"
rm -f "$requirements"
```

The hostctl supervisor uses Go 1.27.1, matching Dockerfile.hostctl-helper.
Without local Go, run its gates in `golang:1.27.1-alpine` with the source mounted
at `/src` and working directory `/src`. Bind sources must be paths on the Docker host.

`djinn build` builds the supervisor for the Docker host's platform (BuildKit sets
`TARGETARCH`/`TARGETVARIANT`) and extracts the static binary to owner-only host
state storage, from where it is bound read-only into the pinned official
Tailscale image. Run it on the host. Deployment placement and security changes
must follow the [security model](SECURITY-MODEL.md), including its
[trusted controller requirements](SECURITY-MODEL.md#sealed-deployments-and-trusted-controller).

### Accepting a vulnerability temporarily

Add an entry to the `ignored` array in the CI step `Audit locked dependencies`.
Include the vulnerability ID, package, reason, and review date using the comment
template. Remove the entry as soon as a fixed version is locked.

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
the private reporting process described in [SECURITY.md](SECURITY.md).
