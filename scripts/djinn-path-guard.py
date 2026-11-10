#!/usr/bin/env python3
"""Refuse Claude Code and Codex tool calls touching configured denied path prefixes."""

import json
import os
import posixpath
import re
import sys

denied_variable = "DJINN_DENIED_PATHS"
file_keys = dict.fromkeys(("Read", "Edit", "Write"), "file_path")
file_keys["NotebookEdit"] = "notebook_path"
file_keys["view_image"] = "path"
patch_headers = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")
shell_start = r"(?:^|[\s;&|()<>'\"`=])"
shell_end = r"(?=$|[/\s;&|()<>'\"`])"


class GuardError(ValueError):
    """A configuration or payload error with a fixed refusal reason."""


def refuse(message: str) -> None:
    output = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": message,
    }
    print(json.dumps({"hookSpecificOutput": output}))


def valid_entry(entry: object) -> bool:
    return (
        isinstance(entry, str)
        and entry.startswith("/")
        and not entry.startswith("//")
        and "\x00" not in entry
        and posixpath.normpath(entry) == entry
        and entry != "/"
    )


def resolve(path: str, base: str, home: str) -> str:
    for prefix in ("~", "$HOME", "${HOME}"):
        if path == prefix or path.startswith(prefix + "/"):
            path = home + path[len(prefix) :]
            break
    return os.path.realpath(os.path.join(base, path))


def under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def check(payload: object, entries: list[str], home: str) -> None:
    if not isinstance(payload, dict):
        raise GuardError("invalid payload")
    tool, args, cwd = (payload.get(key) for key in ("tool_name", "tool_input", "cwd"))
    if (
        not isinstance(tool, str)
        or not isinstance(args, dict)
        or not isinstance(cwd, str)
        or not os.path.isabs(cwd)
    ):
        raise GuardError("invalid payload")
    search = tool in ("Grep", "Glob")
    if tool not in file_keys and not search and tool not in ("Bash", "apply_patch"):
        return
    command = ""
    roots = []
    if tool in ("Bash", "apply_patch"):
        command = args.get("command")
        if not isinstance(command, str):
            raise GuardError("invalid payload")
        if tool == "apply_patch":
            roots = [
                resolve(line[len(header) :], cwd, home)
                for line in command.splitlines()
                for header in patch_headers
                if line.startswith(header)
            ]
    else:
        path = args.get("path", cwd) if search else args.get(file_keys[tool])
        if not isinstance(path, str):
            raise GuardError("invalid payload")
        roots = [resolve(path, cwd, home)]
    if search:
        pattern = args.get("pattern", "")
        if not isinstance(pattern, str):
            raise GuardError("invalid payload")
    if tool == "Glob":
        prefix = re.split(r"[*?\[{]", pattern, maxsplit=1)[0]
        if "/" in prefix:
            roots.append(resolve(prefix[: prefix.rfind("/") + 1], roots[0], home))
    for entry in entries:
        shell_hit = False
        if tool == "Bash":
            forms = (entry, "~" + entry, "$HOME" + entry, "${HOME}" + entry, home + entry)
            regex = (
                shell_start + "(?:" + "|".join(re.escape(form) for form in forms) + ")" + shell_end
            )
            shell_hit = re.search(regex, command) is not None
        for denied in (os.path.realpath(entry), os.path.realpath(home + entry)):
            if shell_hit or any(
                under(root, denied) or (search and under(denied, root)) for root in roots
            ):
                refuse(
                    f"Access to {entry} is denied by Djinn's denied_paths. "
                    "Reading or changing it needs the operator's explicit instruction "
                    "and happens on the host."
                )
                return


def main() -> int:
    try:
        raw = sys.stdin.buffer.read()
        configured = os.environ.get(denied_variable)
        if not configured:
            return 0
        try:
            entries = json.loads(configured)
        except ValueError as exc:
            raise GuardError("invalid DJINN_DENIED_PATHS") from exc
        if not isinstance(entries, list) or not all(valid_entry(entry) for entry in entries):
            raise GuardError("invalid DJINN_DENIED_PATHS")
        if not entries:
            return 0
        home = os.environ.get("HOME", "")
        if not os.path.isabs(home):
            raise GuardError("HOME is not absolute")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise GuardError("invalid payload") from exc
        check(payload, entries, home)
    except Exception as exc:
        # Fail closed: an unhandled hook exception would let the tool run.
        reason = str(exc) if isinstance(exc, GuardError) else type(exc).__name__
        refuse(f"Djinn path guard failed ({reason}); the tool call is refused.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
