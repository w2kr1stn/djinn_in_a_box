# OpenCode 2 in Djinn

The dev image pins [`@opencode/cli@2.0.26` on npm](https://www.npmjs.com/package/@opencode/cli/v/2.0.26)
(published 2026-10-08); the audit image verifies the native npm archive against
the registry's `sha512` integrity before extracting the same version.
`OPENCODE_VERSION` is the numeric Dockerfile pin; `djinn update` resolves
`@opencode/cli`, and a higher local `agent-versions.toml` pin wins for both images.

## Personal carrier

The host carrier `<config-root>/opencode/` is mounted at `~/.opencode/`.
It persists exactly `opencode.json`, `cli.json` and `themes/<name>.json`, with
non-empty UTF-8 theme names that do not start with `.` and no nested theme directories.
Each file must be regular and at most 1 MiB. Credentials use separate stores;
plugins, commands and agents belong to the workflow seed.

At startup, before workflow publication and MCP registration, Djinn checks the
runtime root and its existing allowlisted entries, then atomically copies the
carrier files byte for byte into `~/.config/opencode/`. Copy-in does not validate
JSON; OpenCode reports content errors. A symlinked root, symlink or non-regular
allowlisted entry (including `themes/`) in either tree, or a failed copy, stops
startup with the entry and reason. Nothing behind a refused entry is accessed.
An oversized carrier file is skipped with a warning naming it; that path is
protected from all write-back for the session, including degraded final sync.

Every 30 s, and at clean exit or `docker stop` (SIGTERM/SIGINT), Djinn enumerates
the runtime allowlist again, including newly created themes and `cli.json`.
Changed bytes are persisted only if they parse as exactly one strict JSON
document (UTF-8, no malformed numbers, `NaN` or `Infinity`/`-Infinity`), using
mode 0600, fsync and atomic replacement; acknowledgement follows
successful persistence. Unchanged runtime files preserve host edits; if both
sides changed, runtime wins. Invalid JSON is retried and warned about at final
sync. Symlinks, non-regular or oversized runtime entries are skipped, preserving
the carrier and acknowledgement. Refused or unwritable entries warn once during
checkpoints and again at final sync if unresolved. JSONC is not persisted.

An oversized runtime entry at initialization stays unacknowledged; safe siblings
keep checkpointing, and a later repair can persist. Initial capture I/O failures
disable checkpoints with a warning.
Final sync without references (failed capture or later loss of private state,
with no extra warning for that loss) writes every valid runtime file except
protected paths. This can overwrite host-only edits or restore a carrier file
deleted during the session.

Persistence is **upsert-only**: deletions do not propagate. A theme deleted only
at runtime returns after recreation. A carrier deletion remains absent after
recreation unless a later runtime edit or degraded final sync wrote it back.
To remove a theme reliably, stop the container, delete it in the carrier, then
recreate. Access uses opened directory descriptors with `O_NOFOLLOW`, pinned
regular files and bounded reads. Root ancestors are trusted; concurrent swaps
by another process of the same user are outside this boundary.

## TUI themes

Place a custom theme at `<config-root>/opencode/themes/<name>.json` and select
it in `<config-root>/opencode/cli.json`:

```json
{"$schema":"https://opencode.ai/v2/cli.json","theme":{"name":"<name>","mode":"dark"}}
```

A theme switch in the TUI rewrites runtime `cli.json` and is checkpointed back.
Use strict JSON in both files to persist subsequent edits.

## Local provider

The opt-in [`opencode.json` template](../templates/opencode/opencode.json.example)
declares provider `local`, model `primary` and `{env:LOCAL_MODEL_BASE_URL}` without
a credential or server address. Add its provider declaration to your personal
`opencode.json`, preserving unrelated settings. Adapt its capabilities, token
limits and reasoning-effort variants to your server; it does not select a
default model.

Set the endpoint through `djinn config edit`:

```toml
[environment]
LOCAL_MODEL_BASE_URL = "http://model-host.example:11434/v1"
```

The declaration applies at the next dev-container creation; `enter` inherits the
running environment. Host fallback needs the variable in the invoking host
environment. Authenticate separately with OpenCode.

```sh
opencode run -m local/primary "Reply with exactly: ready"
opencode run --standalone --print-logs -m local/primary#high "Reply with exactly: ready"
```

## Optional native plugins

Neutral seeds stay empty. Review the [reference plugins](../templates/opencode/plugins/)
and copy the desired files into local `config/opencode/plugins/`. Seeding never
overwrites authored files. Both default-export a stable `id` and `setup(ctx)`
without additional npm dependencies. The security reminder uses
`ctx.tool.hook("execute.before", ...)` with `event.input` and the `shell` tool;
it is a heuristic policy aid, not a sandbox. Session startup uses
`ctx.event.subscribe`, argument-vector Git inspection and abortable cleanup.

Publication requires `export default …` or `export { … as default }`; OpenCode
validates the JavaScript/API. Named-only exports are refused. After runtime-only
plugin edits cause `target-drift`, update the authored seed and recreate dev.
No manifest editing is needed. The reference plugins do not add notifications.

## MCP and audit

MCP registration atomically writes `mcp.servers.<name> = {"type":"remote","url":…}`
through the contained writer, preserving other settings. Disabled entries are
removed; SSE entries warn and lose their earlier managed registration.
OpenCode uses Streamable HTTP.

Audit launches `opencode --standalone --prompt <prompt>` in a fresh home, with v2
`permissions` asking before `edit` and `shell`. It imports credentials only;
the personal carrier and workflow are not imported.

References: [v2 configuration](https://opencode.ai/v2/docs/config),
[v2 CLI settings](https://opencode.ai/v2/docs/cli/config),
[v2 plugins](https://opencode.ai/v2/docs/build/plugins),
[v2 MCP](https://opencode.ai/v2/docs/mcp-servers).
