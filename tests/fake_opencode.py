#!/usr/bin/env python3
"""A fake ``opencode`` for the credential helper tests (stdlib only, no real credentials).

State: a JSON file named by ``FAKE_OPENCODE_STATE``; it plays the part of the database, so its
inode is the database inode and every write happens in place. Credentials live only below the
tests' tmp_path.

Implemented as measured on OpenCode 2.0.26 and 2.0.32:
  * ``auth export --standalone``: ``JSON.stringify(list, null, 2) + "\\n"``, an empty database
    prints ``[]``; every integration with entries has exactly one computed ``active``.
  * ``auth import --standalone`` (stdin): insert or skip by id, never update. A credential becomes
    active only for an integration that is new to the database. stderr reads
    ``Imported N credential(s)`` and, when it skipped some, ``, skipped M already present``.
  * ``api --standalone credential.remove|credential.activate --param credentialID=<id>``: an
    unknown id exits 0 silently; removing the active credential selects a sibling automatically;
    activate switches the selection without changing values.

Isolation (the helper must keep it): ``--standalone``, an empty ``XDG_CONFIG_HOME`` that is also
the cwd, no ``OPENCODE_CONFIG*``, no ``OPENCODE_DB``/``XDG_DATA_HOME``, the disable variables.

Test hooks (environment, all ``FAKE_*``; counted per call, 1-based, in ``<state>.count``):
  FAKE_OPENCODE_LOG        ordered log file; one value-free line per call (O_APPEND)
  FAKE_SENTINEL_FILE       JSON {"raw": [..], "pairs": [[client_id, client_secret], ..]}; the fake
                           counts the occurrences in its real argv and environment (raw, Base64,
                           Base64 of "<id>:<secret>") and logs only the count
  FAKE_FAIL_AT=k           call k exits 1 without effect
  FAKE_NOOP_AT=k           call k exits 0 without effect
  FAKE_KILL_BEFORE=k       call k SIGKILLs its parent (the helper) before its effect
  FAKE_KILL_AFTER=k        call k SIGKILLs its parent after its effect
  FAKE_CONCURRENT_AT=k     before call k the state is replaced by the JSON in FAKE_CONCURRENT_FILE
  FAKE_NO_AUTOSELECT=1     removing the active credential leaves its integration without active
  FAKE_STDIN_STALL=1       import reads 10000 bytes of stdin, then stalls for 60 seconds
  FAKE_SLEEP_AT=k          call k sleeps 60 seconds before its effect (timeout tests)
  FAKE_UNLINK_DB=1         export prints [] and removes the database file (a vanished database)
  FAKE_HUGE_EXPORT=1       export prints more than 16 MiB (size bound tests)
  FAKE_STDERR_FILE         the text of this file is written to stderr, and to stdout on failures
"""

from __future__ import annotations

import base64
import json
import os
import signal
import sys
import time

CONTROL = "FAKE_"


def log(line: str) -> None:
    path = os.environ.get("FAKE_OPENCODE_LOG")
    if path:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, (line + "\n").encode())
        finally:
            os.close(descriptor)


def refuse(reason: str) -> None:
    log(f"fake refused: {reason}")
    sys.stderr.write(f"isolation violated: {reason}\n")
    sys.exit(99)


def check_isolation(argv: list[str]) -> None:
    if "--standalone" not in argv:
        refuse("no --standalone")
    config = os.environ.get("XDG_CONFIG_HOME")
    if not config or not os.path.isdir(config):
        refuse("XDG_CONFIG_HOME missing")
    if os.listdir(config):
        refuse("XDG_CONFIG_HOME not empty")
    if os.path.realpath(os.getcwd()) != os.path.realpath(config):
        refuse("cwd is not XDG_CONFIG_HOME")
    if any(name.startswith("OPENCODE_CONFIG") for name in os.environ):
        refuse("OPENCODE_CONFIG* present")
    for name in ("OPENCODE_DB", "XDG_DATA_HOME"):
        if name in os.environ:
            refuse(f"{name} present")
    for name, value in (
        ("OPENCODE_DISABLE_PROJECT_CONFIG", "true"),
        ("OPENCODE_DISABLE_AUTOUPDATE", "true"),
        ("OPENCODE_DISABLE_MODELS_FETCH", "true"),
        ("DO_NOT_TRACK", "1"),
    ):
        if os.environ.get(name) != value:
            refuse(f"{name} missing")


def count_sentinels(argv: list[str]) -> None:
    path = os.environ.get("FAKE_SENTINEL_FILE")
    if not path:
        return
    with open(path, encoding="utf-8") as handle:
        spec = json.load(handle)
    needles: list[str] = list(spec.get("raw", []))
    needles += [base64.b64encode(item.encode()).decode() for item in spec.get("raw", [])]
    for client, secret in spec.get("pairs", []):
        needles += [client, secret, base64.b64encode(f"{client}:{secret}".encode()).decode()]
    haystack = (
        "\x00".join(argv)
        + "\x00"
        + "\x00".join(
            f"{key}={value}" for key, value in os.environ.items() if not key.startswith(CONTROL)
        )
    )
    log(f"fake sentinel count {sum(haystack.count(item) for item in needles)}")


def read_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"entries": [], "selection": {}}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_state(path: str, state: dict) -> None:
    mode = "r+" if os.path.exists(path) else "w"
    with open(path, mode, encoding="utf-8") as handle:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state))


def computed_export(state: dict) -> list[dict]:
    chosen: dict[str, str] = {}
    for entry in state["entries"]:
        integration = entry["integrationID"]
        selected = state["selection"].get(integration)
        if selected and any(item["id"] == selected for item in state["entries"]):
            chosen[integration] = selected
        elif integration not in chosen and not os.environ.get("FAKE_NO_AUTOSELECT"):
            chosen[integration] = entry["id"]
    return [
        {
            "id": entry["id"],
            "integrationID": entry["integrationID"],
            "label": entry["label"],
            "active": chosen.get(entry["integrationID"]) == entry["id"],
            "value": entry["value"],
        }
        for entry in state["entries"]
    ]


def next_call(state_path: str) -> int:
    counter = state_path + ".count"
    try:
        with open(counter, encoding="utf-8") as handle:
            number = int(handle.read() or "0") + 1
    except FileNotFoundError:
        number = 1
    with open(counter, "w", encoding="utf-8") as handle:
        handle.write(str(number))
    return number


def hook(name: str, number: int) -> bool:
    return os.environ.get(name) == str(number)


def kill_parent() -> None:
    os.kill(os.getppid(), signal.SIGKILL)


def do_export(state: dict, path: str) -> None:
    if os.environ.get("FAKE_HUGE_EXPORT"):
        block = "x" * 65536
        for _ in range(300):
            sys.stdout.write(block)
        return
    listing = computed_export(state)
    sys.stdout.write(json.dumps(listing, indent=2) + "\n")
    if os.environ.get("FAKE_UNLINK_DB"):
        sys.stdout.flush()
        os.unlink(path)


def do_import(state: dict, path: str) -> None:
    if os.environ.get("FAKE_STDIN_STALL"):
        os.read(0, 10000)
        time.sleep(60)
    incoming = json.loads(sys.stdin.read())
    flags = " ".join(f"{item['id']}={bool(item.get('active'))}" for item in incoming)
    log(f"fakeinfo import-active {flags}")
    known = {entry["integrationID"] for entry in state["entries"]}
    imported = skipped = 0
    activated: dict[str, str] = {}
    for item in incoming:
        if any(entry["id"] == item["id"] for entry in state["entries"]):
            skipped += 1
            continue
        state["entries"].append(
            {
                "id": item["id"],
                "integrationID": item["integrationID"],
                "label": item["label"],
                "value": item["value"],
            }
        )
        imported += 1
        integration = item["integrationID"]
        if integration not in known and (item.get("active") or integration not in activated):
            activated[integration] = item["id"]
    state["selection"].update(activated)
    write_state(path, state)
    text = f"Imported {imported} credential{'' if imported == 1 else 's'}"
    if skipped:
        text += f", skipped {skipped} already present"
    sys.stderr.write(text + "\n")


def do_remove(state: dict, path: str, identity: str) -> None:
    target = next((entry for entry in state["entries"] if entry["id"] == identity), None)
    if target is None:
        return
    before = computed_export(state)
    was_active = any(item["id"] == identity and item["active"] for item in before)
    state["entries"] = [entry for entry in state["entries"] if entry["id"] != identity]
    integration = target["integrationID"]
    siblings = [entry for entry in state["entries"] if entry["integrationID"] == integration]
    if was_active:
        if siblings and not os.environ.get("FAKE_NO_AUTOSELECT"):
            state["selection"][integration] = siblings[0]["id"]
        else:
            state["selection"].pop(integration, None)
    write_state(path, state)


def do_activate(state: dict, path: str, identity: str) -> None:
    target = next((entry for entry in state["entries"] if entry["id"] == identity), None)
    if target is None:
        return
    state["selection"][target["integrationID"]] = identity
    write_state(path, state)


def main() -> int:
    argv = sys.argv[1:]
    state_path = os.environ["FAKE_OPENCODE_STATE"]
    number = next_call(state_path)
    log(f"fakeinfo pid {os.getpid()}")
    count_sentinels(argv)
    check_isolation(argv)
    command = [item for item in argv if item != "--standalone"]
    stderr_file = os.environ.get("FAKE_STDERR_FILE")
    noise = ""
    if stderr_file:
        with open(stderr_file, encoding="utf-8") as handle:
            noise = handle.read()
        sys.stderr.write(noise)
    if command[:2] == ["auth", "export"]:
        operation, identity = "export", ""
    elif command[:2] == ["auth", "import"]:
        operation, identity = "import", ""
    elif command[:1] == ["api"] and len(command) == 4 and command[2] == "--param":
        operation = command[1]
        identity = command[3].removeprefix("credentialID=")
    else:
        refuse("unknown command")
        return 99
    log(f"fake {operation} {identity}".rstrip())
    if hook("FAKE_CONCURRENT_AT", number):
        with open(os.environ["FAKE_CONCURRENT_FILE"], encoding="utf-8") as handle:
            write_state(state_path, json.load(handle))
    if hook("FAKE_FAIL_AT", number):
        sys.stdout.write(noise)
        return 1
    if hook("FAKE_SLEEP_AT", number):
        time.sleep(60)
    if hook("FAKE_KILL_BEFORE", number):
        kill_parent()
        return 1
    state = read_state(state_path)
    if operation != "export" and not os.path.exists(state_path):
        write_state(state_path, state)
    if not hook("FAKE_NOOP_AT", number):
        if operation == "export":
            if not os.path.exists(state_path):
                write_state(state_path, state)
            do_export(state, state_path)
        elif operation == "import":
            do_import(state, state_path)
        elif operation == "credential.remove":
            do_remove(state, state_path, identity)
        elif operation == "credential.activate":
            do_activate(state, state_path, identity)
        else:
            refuse("unknown api method")
    sys.stdout.flush()
    if hook("FAKE_KILL_AFTER", number):
        kill_parent()
    return 0


if __name__ == "__main__":
    sys.exit(main())
