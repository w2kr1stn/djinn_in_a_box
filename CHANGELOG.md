# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic
Versioning before and after the first stable release.

## [Unreleased]

### Added

- `djinn start --detach` starts the container with `docker compose up -d` and
  returns, leaving no Compose client attached; attach afterwards with
  `djinn enter`. The detached path keeps the `--docker` proxy running, refuses to
  start when a Djinn container is already running, and passes the dynamic mounts
  to Compose through a generated override file — including the `-e` half of the
  audio/D-Bus pairs, without which the sockets are mounted but `PULSE_SERVER`
  and `DBUS_SESSION_BUS_ADDRESS` are unset and every client fails to find them.
  Because `up -d` exits 0 as soon as the container is created and says nothing
  about what the entrypoint did next, the command verifies the container is
  actually running before reporting success, and points at `docker logs djinn`
  when it is not. Captured Docker output is printed without markup interpretation
  rather than through Rich's parser, which would silently delete the bracketed
  BuildKit stage tags.
- Guard against starting an interactive container from a background process
  group. `djinn start ... &` left a TTY-attached Compose client in the
  background, where every `tcsetattr()` raises SIGTTOU; Compose forwarded the
  signal into the container, producing tens of events per second and flooding
  Docker's event ring buffer. `djinn start` now fails with an explanation and
  points at `--detach` or a foreground start. Non-TTY stdin, absent controlling
  terminals, and headless `-T` runs are unaffected.
- The entrypoint no longer exits when it has no terminal. An interactive shell
  cannot run without one — zsh reads EOF and returns immediately, which used to
  take the container down with exit code 0 and an empty log. Since
  `docker compose run` picks `-T` from the *client's stdout*, redirecting output
  was enough to trigger that, whatever stdin did. The container now stays up and
  says why, so `djinn enter` (which brings its own TTY) still works, and a later
  `docker stop` still reaches the reverse-sync.
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
- The startup banner leads with a blank line, so the djinn's top row no longer
  reads as clipped against the shell prompt. Plain mode (NO_COLOR, dumb, or
  non-UTF-8 terminals) still renders exactly one line.

### Removed

- The `djinn auth` command, the `dev-auth` Compose service, and the `auth`
  Compose profile. They existed only for OAuth flows needing a loopback
  callback, which required host networking. Every bundled CLI can now sign in
  with a code pasted back from the host browser, so authentication happens
  inside a normal `djinn start` session. Codex needs `codex login --device-auth`
  to select that flow; the other CLIs pick it automatically. See "First
  Authentication" in the README.

### Fixed

- Captured Docker output is no longer mangled by Rich. Every command that echoes
  a subprocess's stdout/stderr as its own output — `build`, `start`, `clean`,
  `run`, `session`, `mcp` — now routes through `print_captured()`, which disables
  markup, highlighting and re-wrapping. Previously Rich consumed anything shaped
  like a tag, so `djinn build`'s BuildKit log lost exactly the `[internal]` /
  `[dev 3/25]` stage markers that locate a failing step, and long paths were
  hard-wrapped mid-token. Note two limits: `backup`/`restore` still embed Docker's
  stderr inside a formatted `error(...)` line, where it stays subject to markup;
  and `print_captured` is not byte-verbatim — Rich still strips control characters
  (including the `\r` that redraw-in-place progress output relies on) and expands
  tabs.
- `djinn start --detach` no longer implies a readiness it cannot verify. `up -d`
  returns as soon as the container is created, while seeding runs for roughly
  another 30 seconds, so the check confirms only that the container did not die
  immediately; the message now says "started", points at `docker logs -f djinn`
  for the initialization, and reports an unverified container as "not running"
  rather than asserting it exited.
- Settings are no longer lost when the container is stopped rather than exited.
  `entrypoint.sh` ran its reverse-sync only after the interactive shell returned,
  so `docker stop` — the only shutdown a detached container ever gets — killed
  PID 1 with every in-session settings change unpersisted. The sync now lives in
  an idempotent `persist_session_state()` reached from both the normal exit and a
  TERM/INT trap, and the shell runs as a waited-on background job so the trap can
  fire at all — a foreground command defers every trap until it returns, which
  under `docker stop` never happens. Backgrounding would also reassign the
  shell's stdin to `/dev/null` (job control is off, and that reassignment happens
  before explicit redirections), leaving zsh non-interactive so it reads EOF and
  exits at once; the entrypoint therefore duplicates fd 0 beforehand and hands it
  back explicitly. Interactive behaviour (job control, Ctrl+C) is unchanged.
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
