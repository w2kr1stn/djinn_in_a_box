#!/usr/bin/env python3
"""Emit workspace AGENTS.md files above the repository as SessionStart context.

Registered in Codex's system config at /etc/codex/config.toml.
Fail open on input, decoding, or filesystem errors so sessions can still start.
"""

import json
import os
import sys
from typing import cast

workspace_variable = "DJINN_WORKSPACE_TARGET"
output_cap = 65536
pointer_reserve = 4096
stdin_limit = 1 << 20
marker = ".git"
instruction_file = "AGENTS.md"


def project_root(cwd: str) -> str:
    directory = cwd
    while True:
        if os.path.lexists(os.path.join(directory, marker)):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return cwd
        directory = parent


def chain_directories(root: str, cwd: str) -> list[str]:
    project = project_root(cwd)
    if project == root or os.path.commonpath([root, project]) != root:
        return []
    directories: list[str] = []
    directory = os.path.dirname(project)
    while True:
        directories.append(directory)
        if directory == root:
            return list(reversed(directories))
        directory = os.path.dirname(directory)


def qualifying_file(root: str, directory: str) -> str | None:
    path = os.path.join(directory, instruction_file)
    resolved = os.path.realpath(path)
    if os.path.commonpath([root, resolved]) == root and os.path.isfile(resolved):
        return path
    return None


def render(root: str, project: str, files: list[str]) -> str:
    text = (
        f"Workspace AGENTS.md files above the repository root {project}, "
        f"from the workspace root {root} down. "
        "Codex does not load these itself; follow them like any AGENTS.md."
    )
    budget = output_cap - pointer_reserve
    for path in files:
        size = os.stat(os.path.realpath(path)).st_size
        head = f"\n\nContents of {path}:\n\n"
        if len((text + head).encode()) + size <= budget:
            with open(os.path.realpath(path), encoding="utf-8", errors="replace") as stream:
                content = stream.read()
            block = head + content
            if len((text + block).encode()) <= budget:
                text += block
                continue
        text += f"\n\nNot included (size cap): {path} ({size} bytes). Read it before working."
    return text.encode()[:output_cap].decode("utf-8", errors="ignore")


def context_for(payload: object, workspace: str | None) -> str | None:
    if not workspace or not os.path.isabs(workspace):
        return None
    root = os.path.realpath(workspace)
    if not os.path.isdir(root) or not isinstance(payload, dict):
        return None
    cwd = cast(dict[str, object], payload).get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    cwd = os.path.realpath(cwd)
    if not os.path.isdir(cwd) or os.path.commonpath([root, cwd]) != root:
        return None
    files = [
        path
        for directory in chain_directories(root, cwd)
        if (path := qualifying_file(root, directory)) is not None
    ]
    if not files:
        return None
    return render(root, project_root(cwd), files)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read(stdin_limit))
        text = context_for(payload, os.environ.get(workspace_variable))
        if text is not None:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": text,
                        }
                    }
                )
            )
    except (OSError, ValueError, UnicodeError) as error:
        print(f"djinn-agents-chain: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
