# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic
Versioning before and after the first stable release.

## [Unreleased]

### Added

- Persistent `age` encryption identity store: `${DJINN_CONFIG_ROOT}/age` is
  provisioned as a credential directory and bind-mounted at `~/.config/age`, so
  `age` keys survive container restarts and are captured by `djinn backup`
  (SOPS users set `SOPS_AGE_KEY_FILE` to a key under that path).
- Shared CLI output design system with a central palette, semantic Rich roles,
  path styling, and terminal-adaptive informational output.
- Startup banner with degraded wordmark and plain-text modes for narrow,
  colorless, dumb, or non-UTF-8 terminals.
- Shell startup UI helpers for section rules, status markers, boxed external
  command output, and zsh/bash-compatible sourcing.

### Changed

- Restructured `djinn start` output into Python-rendered banner,
  `Environment`, and `Container` sections followed by shell-rendered
  `Seed & Config`, `MCP`, `Tools`, and `Security` sections.
- Propagated `NO_COLOR` and terminal width into Compose startup so container
  shell output follows host plain-output and wrapping behavior.

### Removed

- The `djinn auth` command, the `dev-auth` Compose service, and the `auth`
  Compose profile. They existed only for OAuth flows needing a loopback
  callback, which required host networking. Every bundled CLI can now sign in
  with a code pasted back from the host browser, so authentication happens
  inside a normal `djinn start` session. Codex needs `codex login --device-auth`
  to select that flow; the other CLIs pick it automatically. See "First
  Authentication" in the README.

### Fixed

- `djinn clean` now removes the containers it reports as removed. Compose treats
  containers created by `compose run` as one-off and skips them on a plain
  `down`, which is how `djinn start` and `djinn run` create the dev container —
  so cleanup printed success while a live session survived and `djinn backup`
  kept refusing with its stop-all-containers guard. Teardown also reaps a Docker
  proxy left behind by `--docker`.
- A host path that cannot be provisioned (for example an unwritable or
  root-owned config root) now reports the failing path and a writability remedy
  instead of being classified as workflow drift, which advised making a workflow
  artifact portable.
- Optional tools no longer reinstall on every container start when their binary
  name differs from the installer name or they lack a `--version` flag (for
  example Azure CLI's `az` binary and Pulumi's `version` subcommand). Installer
  scripts can declare a `# djinn-verify: <command>` header; the cache check
  falls back to `<tool> --version` when the header is absent.

### Security

- Credential directories under the config root are created with mode `0700`,
  matching the `~/.ssh` precedent. The mode applies on creation only; existing
  directories are not tightened retroactively.
- Documented the credential trust model: credentials are stored unencrypted,
  are readable by every agent in the container, and `djinn backup` archives are
  unencrypted.

## [0.1.0] - 2026-07-03

### Added

- Initial public release of the Djinn in a Box CLI for managing Docker-based
  development containers for CLI coding agents.
- Commands for initialization, image builds, container startup, one-shot agent
  runs, reusable sessions, authentication shells, diagnostics, backups, and
  restores.
- Docker Compose modes for default isolated use, filtered Docker socket access,
  and direct Docker socket access.
- Neutral seed templates for local agent configuration, package lists, and
  container setup files.
