@AGENTS.md

Djinn note: global instructions and workflow files are authored only in the selected workflow source,
`config/<source>/` in the Djinn installation (`djinn config show` names it). Every other copy Djinn delivers from it
is generated and may be read-only; an edit there is reported as drift and blocks the next delivery. Inside the Djinn
container with the default source `claude`, the source is mounted at `~/.claude/` (for example
`~/.claude/AGENTS.md`); `~/.codex/` and `~/.config/opencode/` hold generated workflow copies, and
`~/.djinn-canonical/` and `~/.opencode/seed/` are read-only views.
