# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic
Versioning before and after the first stable release.

## [Unreleased]

### Added

- Optional tools `rust` and `just` (`tools/installers/rust.sh`, `just.sh`). `rust`
  installs rustup into the tools cache volume (`RUSTUP_HOME` and `CARGO_HOME` under
  `~/.cache/djinn-tools`), so the toolchain, the crate registry cache and
  `cargo install` binaries survive container restarts; a rebuild updates instead of
  downloading again. `rustup-init` is checked against its published SHA-256 before
  it runs. Each binary in `$CARGO_HOME/bin` gets a wrapper on the tools `bin`
  directory that sets both variables, because the image's shell setup does not
  know them. `RUST_TOOLCHAIN` (default `stable`) and `RUST_COMPONENTS` select the
  toolchain and extra components; `rustfmt` and `clippy` are always included.
  `just` installs the musl release binary after checking it against the release's
  `SHA256SUMS`. Building libcosmic/COSMIC projects additionally needs the Debian
  packages `cmake libexpat1-dev libfontconfig-dev libfreetype-dev libxkbcommon-dev
  pkgconf` in `packages.txt`.
- `general.sops_age_key_file`: an optional host path to a SOPS age identity.
  Every start (`djinn start`, `--detach`, `djinn run`) mounts that single file
  read-only at `~/.config/sops/age/keys.txt` and sets `SOPS_AGE_KEY_FILE`. It
  fails closed: a missing, non-regular, unreadable, or group/world-accessible file
  stops the start instead of leaving SOPS without a key — or letting Docker
  create an empty directory at the missing source. `djinn doctor` reports the
  same verdict and warns when the key lies inside the config root, which is meant
  to be mirrorable. Set with `djinn config set general.sops_age_key_file <path>`,
  unset with `none`; other `config set` calls keep it.
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
  signal into the container, producing tens of events per second. Two effects
  were measured: host load, and a continuously overflowing Docker event ring
  buffer — which is why the early container deaths left no diagnostic record at
  all. Whether the storm also ended the container was never established. `djinn start` now fails with an explanation and
  points at `--detach` or a foreground start. The check keys on stdout, which is
  what Compose derives TTY allocation from, so `djinn start > log &` stays allowed
  (no TTY is allocated, so nothing can storm) while `djinn start < /dev/null &` is
  refused. Absent controlling terminals (`setsid`) and headless `-T` runs are
  unaffected.
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

### Added

- `build.network` config key (`default` | `host`) selects the network for
  image-build steps, reaching compose as `DJINN_BUILD_NETWORK`. `host` is for a
  host whose container DNS server is reachable from one Docker network only — a
  VPN or split-DNS setup, where a resolver listens on one bridge while builds run
  on another and resolve nothing there. The build then resolves through the same
  resolver the host uses, instead of bypassing it; in exchange every build step
  shares the host's network namespace, so it is opt-in and documented as such.
  Default is unchanged. Buildkit's `none` is not offered (no layer of this image
  builds without a network) and a named network buildkit rejects itself.
- The image build now refuses a network it cannot resolve names on, checking from
  inside each network-dependent build step rather than in a layer of its own — a
  separate guard layer can stay cached while the download it protects re-runs. The
  error names `build.network` and states its trade-off. Previously the failure
  surfaced one timeout at a time: 70 minutes before npm gave up, since it retries
  every package six times with a backoff, and over two minutes for `apt-get
  update` alone.
- `DJINN_BUILD_PROGRESS` overrides the build's progress renderer for anyone who
  prefers the compact redrawing view; an unusable value falls back to `plain`
  with a warning.

### Changed

- Djinn now uses `AGENTS.md` as the sole instruction file for Claude Code, Codex,
  and OpenCode across workflow sources, synchronized views, host delivery, and
  container mounts. The seeded instructions explain the per-directory `.agents/`
  convention.
- Default zone assignments put machine-local Codex and Claude runtime directories
  (package cache, daemon and socket state, locks, local caches and drafts) in the
  local zone, and the doctor's known config-root entries cover current agent
  runtime files, including shell-style patterns.
- `djinn build` now shows the build log while the build runs. It previously
  captured the whole output and returned it only when the process exited, so a
  build that stopped making progress displayed nothing at all — and on failure
  only `stderr` was printed while `stdout` was discarded, which cost the lines
  that name the failing step's cause. The build now inherits stdout/stderr and
  forces `--progress plain`, keeping every stage line on screen so the stage a
  stalled build last entered stays visible. Deliberately without a timeout:
  killing the compose client would leave `docker-buildx` and the BuildKit solve
  in the daemon running, so the timeout would report a cancellation it cannot
  actually perform.

- Restructured `djinn start` output into Python-rendered banner,
  `Environment`, and `Container` sections followed by shell-rendered
  `Seed & Config`, `MCP`, `Tools`, and `Security` sections.
- Propagated `NO_COLOR` and terminal width into Compose startup so container
  shell output follows host plain-output and wrapping behavior.
- The startup banner leads with a blank line, so the djinn's top row no longer
  reads as clipped against the shell prompt. Plain mode (NO_COLOR, dumb, or
  non-UTF-8 terminals) still renders exactly one line.

### Removed

- The bundled Gemini CLI: the `gemini` built-in agent, its image install and
  `GEMINI_CLI_VERSION` pin, the `~/.gemini` config and seed mounts, settings
  seeding and MCP registration for it, its zone, config-root and credential
  entries, and its five Google endpoints in the firewall allowlist.

- The Docker MCP Gateway: the `mcpgateway` CLI, its Compose stack under `mcp/`,
  the gateway rows in startup output, `djinn status` and `djinn doctor`, and the
  `docker-gateway` special case in MCP registration.

- The `djinn auth` command, the `dev-auth` Compose service, and the `auth`
  Compose profile. They existed only for OAuth flows needing a loopback
  callback, which required host networking. Every bundled CLI can now sign in
  with a code pasted back from the host browser, so authentication happens
  inside a normal `djinn start` session. Codex needs `codex login --device-auth`
  to select that flow; the other CLIs pick it automatically. See "First
  Authentication" in the README.

- The doctor check for an outdated sync root location.
- MCP registration: the `type` key in `config/mcp-servers.json` (entries use
  `transport` and `enabled`), and the `rmcp_client` feature flag written to the
  Codex config.
- The shell wrapper guide `docs/zshrc-djinn-snippet.sh` and the devcontainer
  template `templates/devcontainer.json`.

### Fixed

- Host provisioning now creates zone overlay targets inside the config root, so
  Docker no longer creates them as root-owned directories.
- `djinn build` works again with `build.network host`. Since buildx 0.37.2, bake
  rejects an entitlement nobody granted instead of skipping the consent check, and
  `docker compose build` — which drives bake internally — never grants
  `network.host`, so every such build stopped with `additional privileges
  requested` before its first step. Compose has no flag to grant it, so the build
  now calls `docker buildx bake` on `docker-compose.yml` directly and adds
  `--allow network.host` exactly when the configured build network is `host`;
  other builds grant nothing. The image definition still lives in the compose
  file. Consequences: `docker buildx` is now a build requirement (`djinn doctor`
  warns when it is missing), and `DJINN_BUILD_PROGRESS` takes bake's renderer
  names — `rawjson` instead of compose's `json`.
- `compose down` now refuses to reap the container it is running inside. The
  docker socket is mounted into the dev container, and `docker-compose.yml` pins
  the project name, so a teardown started from *any* copy of the repo — including
  a throwaway one — selects the live session's container. That is not theoretical:
  a mutation test that disabled an unrelated guard let `clean_all` run through to
  a real teardown and killed the container it was executing in. Teardown from the
  host is unaffected; from inside, the command explains itself and points at
  `djinn clean`.
- A detached container no longer dies when its unused PID-1 shell receives EOF.
  `up -d` gives the container a TTY (the compose file sets `tty: true`) that
  nobody is attached to, and an interactive shell there made the whole session
  hostage to that terminal: one EOF — a stray attach, a closed pty master, a
  Ctrl-D — ended the shell with 0, PID 1 followed, and the container was gone
  with no signal and no error to point at. `djinn start --detach` now marks the
  container with `DJINN_DETACHED=true`, and the entrypoint runs its keeper
  instead of a shell. Consumers were never using that shell anyway — `djinn
  enter` brings its own TTY through `docker exec` — and `docker stop` still
  reaches the settings reverse-sync unchanged.
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

- Host provisioning secures config and credential roots with mode `0700` and
  creates every assigned zone overlay directory before Compose starts.
- Documented credential storage and access: credentials remain unencrypted at
  rest and readable by every agent in the container; backups are encrypted by
  default, with an explicit `--no-encrypt` option.

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
