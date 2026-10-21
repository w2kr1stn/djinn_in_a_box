# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic
Versioning before and after the first stable release.

## [Unreleased]

### Added

- `djinn run --allow HOST[/PREFIX][:PORT]` (repeatable) gives one headless run
  exactly the listed outbound destinations and refuses everything else at once.
  Names are resolved once on the host and pinned; the run has no DNS. A root step
  applies and reads back the rules, then removes `NET_ADMIN` and `NET_RAW` from
  the run's process tree; `djinn-egress:` lines on stderr show the applied rules
  (#170).
- `djinn serve <claude|opencode ...>`, `--status`, `--stop [NAME ...]` and
  `--pair opencode` run the agent harnesses' server modes inside the running
  detached container. OpenCode serves its web UI and API on host
  `127.0.0.1:<port>` (`serve.opencode_host_port`, default 49380) behind a
  password that `djinn serve` generates once on the host; expose it to your
  tailnet with your own `tailscale serve` entry (Djinn only reads `tailscale
  serve status`). Claude Code runs its Remote Control server mode. `djinn
  serve opencode` refuses Docker Engine older than 28.3.3, networks that allow
  direct routing to containers, a Tailscale Funnel on the port, and relocated
  or extra OpenCode service settings. The detached container must be recreated
  once to publish the port. Codex Remote Control is not part of this (#164).

### Fixed

- `--firewall` now allows Claude's current sign-in token host
  `platform.claude.com` instead of the unused `console.anthropic.com`, so a
  subscription token can be refreshed under the firewall (#178).
- `djinn serve --status` shows the OpenCode endpoint, tailnet and relay details on
  separate lines instead of one line with literal `\n`, which narrow terminals
  truncated (#167).
- Keep Git delivery through observer lock conflicts and transient inspection or
  filter errors; bound filter rebinds and record terminal causes in host state,
  post-mortem records and a dev-visible `observer-exit` file. Doctor fails when
  the attached running dev loses its observer or filter, and warns after rebinds
  (#199).
- Hostctl observation publication survives a busy control lock; every observer
  closure verifies the pinned helper stopped, with an ID-bound fallback and
  retries when the lock or daemon is unavailable; repeated failures are coalesced
  in the journal. Doctor reports pending helper closures from the host journal,
  including when Docker is unreachable (#199).
- An OpenCode `djinn audit` started while a lifecycle command runs is refused
  after at most 15 s, naming the holding process; recovering old audit sessions
  can additionally wait up to 30 s for the carrier (#162).
- `djinn run` works alongside a running dev and other runs without taking the
  dev runtime or blocking `djinn start` (#179).
- Run cleanup verifies and removes exactly its own container on completion,
  failure, timeout, Ctrl-C, SIGTERM and SIGHUP; signals exit 130 (#179).
- `djinn clean` ends in-flight runs and removes their project-default network
  in both normal and agent-Docker teardown (#179).
- Sealing skips peers that the daemon reports as “No such container” during
  inspection instead of becoming uncertain (#179).
- The private sealing map uses the real `state.json` and covers all runtime
  control files without treating public/export delivery as private (#179).
- The sealing peer loop skips the assessed dev by its full Docker ID (#193).
- Raise the default OpenCode version to 2.1.1; a fresh build with the previous
  default 2.0.26 failed in OpenCode's npm postinstall. Maintainers now build
  once with the defaults alone after any default bump (#176).
- Doctor no longer reports Claude's `mcp-needs-auth-cache.json` as zone drift;
  it is a known config-root entry (#177).
- Name offending workflow roots, files and carrier keys in delivery refusals.
  `djinn config status` and doctor's `Config workflow` check now inspect start
  runtime roots read-only; runtime drift makes status exit `1`. Plain sync
  warns about runtime drift after a successful canonical sync (#166).
- Check every delivery target before preflight changes workflow content, and
  print canonical auto-sync counts even when a later step refuses (#166).
- Serialize `djinn clean volumes` with restore, backup, clean all, and dev
  start/stop; a concurrent run fails after at most 15 s with "host control is busy",
  naming the holding process.
- Persist and sync OpenCode 2 logins, including MCP OAuth, through
  `credentials.json` in the config root; audits import them and merge refreshes
  back, global logout refuses while an audit runs, and deleting or restoring
  the OpenCode data volume exports them first
  (#162).
- Require `DJINN_LIVE_DOCKER=1` before probing or running live Docker overlay
  tests; CI opts in on its test step.
- Persist OpenCode `opencode.json`, `cli.json` and custom themes through the
  allowlisted personal carrier, with contained access, strict JSON write-back
  and 30 s/final checkpoints.
- Register OpenCode MCP servers in the 2.x shape, preserve personal settings,
  and remove disabled or unsupported SSE registrations.
- Launch OpenCode audits with standalone v2 permissions and verify the native
  npm archive's integrity before extraction; admit v2 default-export plugins
  and provide optional reference plugins and a neutral local-provider template.
- Removal of a large stale dev container no longer fails after 5 s. Runtime
  container and volume removals use the named 60 s `REMOVE_TIMEOUT_SECONDS`
  timeout; a timeout names the object and says retrying is safe (#156).
- `djinn start --detach` now delivers `--mount` and `--here` paths containing
  `$` literally instead of letting Compose interpolate them. A `--mount` or
  `--here` source whose resolved path contains `:` is refused instead of being
  split by Compose.
- `djinn start --detach` delivers zone overlay and read-only shell/SOPS mount
  paths containing `$` literally. Compose receives `HOME` explicitly so
  `~/.djinn/sessions` is the directory checked against host execution inputs.
- `djinn hostctl on --allow-unsealed` opens again for a dev with a Docker socket
  or relocated socket bind, a host-root bind or an unverified `DOCKER_HOST`:
  sealing skips the nested socket/hard-link inventory for such a dev instead of
  refusing on its uncertainty. The cause still requires `--allow-unsealed`, and
  other inspection errors still refuse. The inventory no longer follows symlinks
  and skips entries that vanish during the scan; a missing bind root and
  unreadable directories still refuse.
- Checkpoint Claude state and Claude/OpenCode personal settings every 30 s with
  atomic writes: changes older than about 30 s survive a crash, plus checkpoint
  duration and scheduling delay, given valid JSON and healthy writable storage.
  Host-only overlay edits survive clean stop; runtime wins when both changed.
  Missing checkpoint references can let final sync overwrite host-only edits.
- Optional tools from the tools volume are now on the image `PATH`, so
  `docker exec`, `zsh -c`, headless agents and `djinn session` find them without
  an interactive shell. `djinn session` starts a non-login `bash -c`, so a login
  profile no longer resets that `PATH`. psql, pg_dump, pg_restore and pg_dumpall
  run through wrappers that add the tools library directory only for themselves;
  the image and `.zshrc` no longer set `LD_LIBRARY_PATH`.

### Security

- Accept `~/.config/djinn_in_a_box/agents.toml` only as a regular, single-link
  file owned by the invoking user and not writable by group or others, in a
  non-symlink directory with the same rules; any other state stops the command
  instead of falling back to the built-in agents (#171).
- Bind managed OpenCode to container loopback behind a gateway-only relay;
  ordinary bridge peers are refused before reaching the API. Check inspected
  container/endpoint `route_localnet` settings and actual values before start.
  Status reports relay refusals since start or why the count is unknown. Stop
  OpenCode before updating; legacy servers require stopping or container
  recreation. Document direct-routing, source-address and ARP-spoofing residuals;
  the password remains a second barrier (#167).
- Refuse managed writable binds selected by configuration that overlap host
  execution inputs or whose path contains `..` or `:`: the workspace root (also
  delivered to the agent-Docker companion), config-root tool directories (`claude`, `codex`,
  `opencode`, `gh`, `age`), assigned shared/local zone overlay sources, and
  `~/.djinn/sessions`. `djinn start` and `djinn run` check immediately after loading
  config, before host provisioning, in every Docker mode, and fail closed when
  inputs or a source cannot be determined. `djinn doctor` reports the same check
  in `Managed bind sources`. Existing setups: move an overlapping installation,
  Python environment or PATH directory out of the root, or choose another root,
  before the next start; `djinn doctor` lists them.
- Mount the OpenCode seed view read-only alongside the canonical view. Claude
  workflow binds remain writable (#166).
- Seal whole managed runtime files as `0444` (`0555` executable) and runtime
  manifests as `0444`, resealing writable copies at delivery. Carriers and the
  canonical tree keep their mode handling. Rename-based tools can still replace
  files; named drift checks and backed-up discard provide recovery (#166).
- At container creation, refuse writable declared binds and writable invocation
  `--mount`/`--here` sources that overlap host execution inputs, including
  `djinn run`'s implicit `--here`. Read-only binds, `--mount ...:ro`, and
  declared volumes are not affected. Existing setups: switch a refused writable
  bind to read-only (or remove it) before the next start; `djinn doctor` lists
  affected `[mounts.*]` entries. Sealing treats an execution input that cannot be
  inspected as uncertain unless a Docker-authority cause already prevents
  sealing, and both checks cover the location of every symlink on an execution
  input's path. The separate guest-run protected-source check (#179) also
  covers read-only binds.
- Protect host-mode `djinn session` from automatic loading of container-writable
  agent and Git configuration. Claude starts with user-only settings; OpenCode
  disables project config and refuses its remaining entry points; Codex
  workspaces with `.codex` and unsupported agents are refused. Unsafe Git
  repositories, including workspaces containing nested clones, are refused
  before launch with exit 1 and review guidance. Container mode is unchanged.
- Host-side provisioning, doctor repair, Claude baseline seeding, session
  creation and sync-path clearing refuse symlinked or non-directory components.
  Derived roots that are symlinks are refused.
- Route agent package installs through a narrow root-owned helper with its own
  sudoers line, exact apt-cache package-name validation, fixed environment and
  options, and an announcement for every elevation. The helper alone records
  installs in the root-owned `/var/lib/djinn-packages/packages.txt`, checks trust
  on the opened descriptor, and performs bounded startup replay. The broad existing sudo
  rule (`dev ALL=(ALL) NOPASSWD:ALL`) is unchanged.
- Require host-side confirmation before `djinn session` publishes a new or
  changed managed workflow to a native host agent root. Confirmation defaults
  to No at a terminal; non-TTY callers refuse with exit 1 and review instructions.
  Approved item sets are stored separately in
  `~/.config/djinn_in_a_box/host-workflow-trust.json` and revalidated before
  publication. Existing roots require one first confirmation after updating;
  container changes to published content require another, while personal
  `settings.local.json` write-back does not. Container workflow editing and
  delivery remain unchanged.
- The host-side workflow publisher and config sync no longer follow symlinks in
  dev-writable workflow trees. A symlinked or non-directory path component, a
  symlinked file and any special file are refused as a collision instead of being
  written, removed or read through; a symlinked directory in the selected source
  is refused instead of skipped. Symlinked canonical and target roots are refused
  (`Not a directory` or `Too many levels of symbolic links`).

### Changed

- Raise the default Claude Code version in the dev image to 2.1.304, the version the
  `claude-isolated` agent was measured with (#171).
- `djinn agents --json` writes raw JSON with every field, including
  `prompt_template`, and sorted keys, independent of the terminal width (#171).
- `djinn run` is a guest on `djinn-in-a-box_default`, separate from dev and its
  companions. Runs receive no Git/SSH, hostctl alias or desktop delivery (#179).
- Require Docker Compose 2.24.4 or later for the run overlay's `!override`;
  preflight refuses a run if Compose does not keep its separate network (#179).
- Refuse `djinn run --docker` before preparation; use `--docker-direct` for host
  Docker authority, or work in a dev started with `djinn start --docker` (#179).
- Refuse `--docker-direct` for runs while the hostctl helper is running (#179).
- Refuse every run bind source, even read-only and through symlinks, that
  overlaps protected host state, Docker storage, configured Git keys or Docker
  sockets; refuse other socket sources too. Checks cover resolved Compose binds
  and command-line mounts, with only the approved DIRECT socket excepted; refuse
  hard-linked file sources and host carriers outside the resolved Compose allowlist;
  verify named-volume names and storage provenance (#179).
- Refuse `djinn run` from `$HOME` without `--mount` when its implicit workspace
  contains private state. Start from a subdirectory or mount a directory outside
  private state read-only; `general.code_dir` must also avoid protected state (#179).
- Keep Codex SQLite stores per host with
  `CODEX_SQLITE_HOME=/home/dev/.codex/sqlite` and a new `sqlite` local-zone default.
  Session-ID resume works across machines; picker/`--last` discovery and
  goals, queued messages and preferences remain local. SQLite is excluded from
  config-root backups; an explicit Codex `sqlite_home` still takes precedence
  (#164).
- Reserve `CODEX_HOME`, `CODEX_SQLITE_HOME` and the Codex daemon policy helper
  override in environment declarations (#164).
- Require image label `djinn.workflow.publisher=2` for workflow delivery;
  older or unlabeled images refuse with `Rebuild/recreate required.` (#166).
- Move dev and audit OpenCode installs to npm
  [`@opencode/cli@2.1.1`](https://www.npmjs.com/package/@opencode/cli/v/2.1.1)
  (published 2026-10-20).
- Restore refuses unknown sync-path names. Backup and restore block dev container
  creation while they run. Clearing fails when an entry other than an empty mount
  placeholder cannot be removed.
- Desktop helpers and the agent Docker companion run their healthcheck every 2 s
  only while starting and every 30 s afterwards, instead of every 2 s for their
  whole lifetime; each probe is an exec and filled the Docker event stream.
  **Requires Docker Engine 25 or later and Docker Compose 2.20.2 or later**
  (`start_interval`). After startup, Docker reports a failed helper as
  `unhealthy` after about 60–90 s instead of about 6 s. A desktop helper that
  never passes its probe now fails creation at the 15 s deadline with
  `helper health starting (readiness timeout)`. This `start_interval`
  requirement predates guest runs; the current Compose minimum is 2.24.4 above.

### Removed

- Docker socket proxy service, Compose file and `djinn logs proxy` command.
- The `auth.json`/`mcp-auth.json` symlink bridge for OpenCode; remove those
  config-root files by hand (#162).

### Added

- `claude-isolated` agent: Claude Code with the stored subscription login but
  without tools, MCP servers, `CLAUDE.md`/`AGENTS.md`, settings, hooks or
  auto-memory, for headless model-only steps (#171).
- `scripts/probe-claude-isolated.py` re-probes that isolation after a Claude Code
  version bump (#171).
- `djinn hostctl check [HOST]` shows target SSH approval banners in the owner's
  host terminal through the gated helper relay, with frozen peer trust and
  isolated temporary OpenSSH configuration. `on` adds a check hint, and `status`
  ends with a passive summary of window time, sealing, relay peers and the next
  action (#130).
- Best-effort `Codex daemon` policy on dev-container starts: disable only the
  daemon updater, align selected releases with the image's pinned Codex CLI via
  `update --from-cli --yes`, and prune old releases. Other settings, including
  remote-control preference, are preserved; one-shot commands skip the policy
  (#164).
- Read-only `Codex daemon updater` and `Codex daemon releases` doctor rows,
  showing updater/remote-control settings and release count, size and selection
  without a fix action (#164).
- `djinn config sync --discard-target-drift`: back up managed target edits in
  the canonical tree and start runtime roots, then restore wanted state. It
  requires stopped Djinn containers, holds lifecycle exclusion through the last
  publish, and makes all backups durable before overwriting any target.
  Private records live under the passwd home's
  `~/.local/state/djinn/workflow-drift/<UTC>-<pid>/`, outside backup, sync zones
  and dev mounts (#166).
- Doctor's `OpenCode runtime` check describes the container-local delivery
  rebuilt at each start and, for running dev, checks the image gate and inspects
  drift read-only through the publisher (#166).
- Host-private post-mortem records before recovery removes unexpectedly stopped
  containers, plus hostctl teardown observations. Records keep bounded log tails
  and final state under `~/.local/state/djinn/post-mortem/` (last 10 / 8 MiB);
  commands print only the path and doctor reports the latest record (#135).
- Runtime package installs for agents, with a root-owned persistent package list and
  missing-only start replay in the `djinn-packages` data volume. The firewall
  allowlist includes `deb.debian.org` for the Debian archive.
- Optional bind-only `read_only` declarations, delivered to dev and the agent
  Docker workspace with matching modes and shown by `config show` and `doctor`.
- Rootless agent Docker runtime for `--docker`, with generation-owned Unix endpoint,
  matching workspace mounts, persistent cache, resource limits and firewall gating.
- Checksum-verified Buildx 0.37.2 in the dev image.
- Optional tool `herdr` (`tools/installers/herdr.sh`), a terminal workspace manager
  for AI coding agents. Installs the latest stable GitHub release into the tools
  cache volume after checking its published SHA-256 and version; image rebuilds
  reuse an installed binary whose version still matches.
- Optional tool `pnpm` (`tools/installers/pnpm.sh`) via Corepack, respecting a
  project's `packageManager` pin. Corepack is installed from npm into the tools
  cache volume because Node >= 25 no longer bundles it; `COREPACK_HOME` also
  defaults to the volume, preserving downloaded package managers and the
  known-good release for new containers. The `pnpm` and `pnpx` wrappers
  call the volume's Corepack and respect an explicitly set `COREPACK_HOME`.
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
  `djinn enter`. The detached path keeps the `--docker` companion running, refuses to
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

- `djinn hostctl` groups sealing causes and unknowns by bind in status, refusal
  messages, dev-start warnings, journal records and saved assessments;
  `djinn doctor` retains every individual finding.
- `tools/install.sh` now runs cache verification and installers from `/`, so
  project files in the container's starting directory (`packageManager` pins,
  `rust-toolchain.toml`, uv configuration) cannot influence them. Relative
  `TOOLS_FILE`, `CACHE_DIR`, and `INSTALLERS_DIR` overrides still refer to the
  starting directory.
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

- `djinn update` leaves the checkout clean and records agent versions locally,
  so dev and assistant builds use the newer of local versions and upstream defaults.
- `djinn start` and `djinn run` refuse invocation mounts at or below Djinn-managed
  roots, including the five recursively repaired paths, before container creation.
  Read-only mounts follow the same shared managed-root rule as declared mounts.
- Git keys that share a passphrase now ask for it once at `djinn start` and
  `djinn run`, instead of once per key. One `ssh-add` call loads all declared
  keys; a different passphrase prompts again, so declare keys that share one
  next to each other. With guest runs (#179), this key-loading behavior applies
  only to dev starts.
- Host provisioning creates every nested bind-mount target inside the config
  root, so Docker no longer creates root-owned directories or files there.
- Claude Code loads the global `AGENTS.md` again: Djinn provides
  `~/.claude/CLAUDE.md` as a read-only bridge that imports it, in the container and
  on the host fallback.
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
  proxy left behind by `--docker`. Guest runs (#179) now have distinct
  identities and their own cleanup.
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

- A passphrase piped to `djinn start` or `djinn run` no longer unlocks encrypted
  Git keys; without a terminal on stdin, encrypted keys refuse startup. Unlock
  them at a host terminal. With guest runs (#179), only dev starts load keys;
  runs receive no Git/SSH delivery.
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
