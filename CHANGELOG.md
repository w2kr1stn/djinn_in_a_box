# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic
Versioning before and after the first stable release.

## [Unreleased]

### Added

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

### Fixed

- Optional tools no longer reinstall on every container start when their binary
  name differs from the installer name or they lack a `--version` flag (for
  example Azure CLI's `az` binary and Pulumi's `version` subcommand). Installer
  scripts can declare a `# djinn-verify: <command>` header; the cache check
  falls back to `<tool> --version` when the header is absent.

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
