# Security Policy

## Supported Versions

Only the latest `0.x` release receives security fixes before the first stable
major release.

| Version | Supported |
| ------- | --------- |
| Latest 0.x | Yes |
| Older 0.x | No |

## Reporting a Vulnerability

Report vulnerabilities through GitHub Security Advisories for this repository.
Use private vulnerability reporting so details are not published before a fix
or mitigation is ready.

If GitHub private reporting is unavailable, use this fallback contact:
73987905+w2kr1stn@users.noreply.github.com.

Please include:

- Affected version or commit.
- Reproduction steps.
- Expected and observed impact.
- Any known mitigations.

## Response Expectations

You should receive an initial response within a reasonable maintainer window.
Confirmed vulnerabilities will be triaged by severity, fixed in the supported
release line when practical, and disclosed with appropriate credit unless you
request otherwise.

## Dual-Use Caveats

Djinn in a Box can start development containers for CLI coding agents. Some
operating modes deliberately expose powerful local capabilities and must be
enabled with care.

Docker socket access is documented in `DOCKER-SOCKET-SECURITY.md`, including
the default no-socket mode, filtered proxy mode, and direct socket mode. Direct
socket access should be treated as host-level control.

Write-enabled agent runs can modify mounted workspaces. Combining write mode
with agent bypass-permissions settings increases the risk that an automated
agent can change files or invoke tools beyond the operator's intent. Prefer the
least-capable mode that fits the task, review prompts and mounts before running
agents, and avoid combining direct Docker socket access with broad write access
unless you fully trust the command path.
