#!/usr/bin/env python3
"""List names and metadata below a denied path on a configured hostctl host."""

import json
import os
import posixpath
import re
import shlex
import sys

denied_variable = "DJINN_DENIED_PATHS"


def main() -> int:
    try:
        if len(sys.argv) not in (4, 5):
            raise ValueError("usage: djinn-path-names HOST USER PATH [DEPTH]")
        host, user, path = sys.argv[1:4]
        depth = sys.argv[4] if len(sys.argv) == 5 else "1"
        with open(os.path.expanduser("~/.ssh/tailnet_config")) as config:
            hosts = re.findall(r"^Host (\S+)$", config.read(), re.MULTILINE)
        if host not in hosts:
            raise ValueError("HOST is not a configured hostctl alias")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
            raise ValueError("invalid USER")
        if not path.startswith("/") or posixpath.normpath(path) != path:
            raise ValueError("PATH must be normalized and absolute")
        if any(ord(char) < 32 or ord(char) == 127 for char in path):
            raise ValueError("PATH contains a control character")
        entries = json.loads(os.environ.get(denied_variable) or "[]")
        if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
            raise ValueError("invalid DJINN_DENIED_PATHS")
        if not any(path == entry or path.startswith(entry + "/") for entry in entries):
            raise ValueError("PATH is not below a denied path")
        if depth not in ("0", "1", "2", "3", "4", "5"):
            raise ValueError("DEPTH must be 0 through 5")
        command = (
            f"cd /tmp && sudo -n -u {user} find -- {shlex.quote(path)} "
            f"-maxdepth {depth} -printf '%y %s %T+ %p\\n'"
        )
        os.execvp("ssh", ["ssh", host, command])
    except (OSError, ValueError) as exc:
        print(f"djinn-path-names: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
