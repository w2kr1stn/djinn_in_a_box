"""Every path recovering audit sessions or changing the OpenCode carrier runs under
the shared restore gate, inside the exclusive restore, or under hostctl.control_guard().
A new path must be classified here. Container helpers rely on restore refusing running
containers and holding the creation lock; primitives inherit their callers' exclusion.
Host state, conflict backups and session IO writes do not change the carrier.
"""

import ast
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

import pytest
from test_commands import test_container
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import container
from djinn_in_a_box.core import hostctl

_isolate_opencode_cleanup = test_container._isolate_opencode_cleanup
credential_cleanup = test_container.credential_cleanup

RULE = "OpenCode carrier changes and audit recovery require lifecycle exclusion"
CALLEES = {
    "merge_back",
    "tombstone_ids",
    "tombstone_all",
    "clear_sync_path",
    "restore_sync_path",
    "restore_volume",
    "park_for_restore",
    "recover_audit_sessions",
    "recover_leftover_sessions",
    "sync",
    "publish",
    "_add_tombstones",
    "write_atomic",
    "_logout_opencode",
    "_protect_opencode_volume",
    "clear_directory",
    "remove_copy",
    "remove_stale_own_temps",
    "_consume_copies",
    "_begin_write",
    "open_audit_session",
}
CLASSES = {"restore", "control", "gate", "container", "primitive", "not-carrier"}

# (package-relative path, dotted enclosing function, callee, exclusion class).
# Repeated rows preserve the number of calls, including separate branches.
INVENTORY = [
    ("commands/backup.py", "restore", "park_for_restore", "restore"),
    ("commands/backup.py", "restore", "park_for_restore", "restore"),
    ("commands/backup.py", "restore", "restore_sync_path", "restore"),
    ("commands/backup.py", "restore", "restore_volume", "restore"),
    ("commands/backup.py", "restore", "restore_volume", "restore"),
    ("commands/backup.py", "restore", "recover_leftover_sessions", "gate"),
    ("commands/container.py", "start", "recover_leftover_sessions", "gate"),
    ("commands/container.py", "_logout_opencode", "recover_leftover_sessions", "gate"),
    ("commands/container.py", "_logout_opencode", "merge_back", "control"),
    ("commands/container.py", "_logout_opencode", "tombstone_all", "control"),
    ("commands/container.py", "_logout_opencode", "clear_sync_path", "control"),
    ("commands/container.py", "_protect_opencode_volume", "merge_back", "control"),
    ("commands/container.py", "clean_volumes", "tombstone_ids", "control"),
    ("commands/container.py", "clean_volumes", "_protect_opencode_volume", "control"),
    ("commands/container.py", "clean_volumes", "_protect_opencode_volume", "control"),
    ("commands/container.py", "clean_volumes", "_logout_opencode", "control"),
    ("commands/container.py", "clean_volumes", "clear_sync_path", "control"),
    ("commands/container.py", "clean_all", "_logout_opencode", "control"),
    ("commands/container.py", "clean_all", "clear_sync_path", "control"),
    ("core/assistant.py", "recover_leftover_sessions", "recover_audit_sessions", "gate"),
    ("core/assistant.py", "finish_session", "recover_leftover_sessions", "gate"),
    # Admission takes control internally; the audit runtime only keeps its session lease.
    ("core/assistant.py", "run_audit", "open_audit_session", "control"),
    ("core/assistant.py", "run_audit", "recover_leftover_sessions", "gate"),
    ("core/assistant.py", "run_audit", "recover_leftover_sessions", "gate"),
    ("core/opencode_credentials.py", "write_host_state", "write_atomic", "not-carrier"),
    ("core/opencode_credentials.py", "publish", "write_atomic", "primitive"),
    ("core/opencode_credentials.py", "publish", "write_atomic", "primitive"),
    ("core/opencode_credentials.py", "backup_copy", "write_atomic", "not-carrier"),
    ("core/opencode_credentials.py", "sync", "publish", "container"),
    ("core/opencode_credentials.py", "merge_back", "publish", "primitive"),
    ("core/opencode_credentials.py", "_add_tombstones", "write_atomic", "primitive"),
    ("core/opencode_credentials.py", "tombstone_ids", "_add_tombstones", "primitive"),
    ("core/opencode_credentials.py", "tombstone_all", "_add_tombstones", "primitive"),
    ("core/opencode_credentials.py", "_write_io_file", "write_atomic", "not-carrier"),
    ("core/opencode_credentials.py", "_merge_pending", "merge_back", "gate"),
    ("core/opencode_credentials.py", "main", "sync", "container"),
    ("core/opencode_credentials.py", "sync", "remove_stale_own_temps", "container"),
    ("core/opencode_credentials.py", "sync", "_consume_copies", "container"),
    ("core/opencode_credentials.py", "_consume_copies", "remove_copy", "container"),
    ("core/opencode_credentials.py", "merge_back", "_begin_write", "primitive"),
    ("core/opencode_credentials.py", "_begin_write", "remove_stale_own_temps", "primitive"),
    ("core/opencode_credentials.py", "tombstone_ids", "remove_stale_own_temps", "primitive"),
    ("core/opencode_credentials.py", "tombstone_all", "remove_stale_own_temps", "primitive"),
    ("core/docker.py", "restore_sync_path", "clear_directory", "primitive"),
    # The serve helper writes its own records, the relay counter and the runtime
    # ~/.config/opencode/service.json; the credential carrier is ~/.opencode, which it never opens.
    ("core/serve_helper.py", "save_record", "write_atomic", "not-carrier"),
    ("core/serve_helper.py", "merge_password", "write_atomic", "not-carrier"),
    ("core/serve_helper.py", "run_relay.persist", "write_atomic", "not-carrier"),
    ("core/docker.py", "clear_sync_path", "clear_directory", "primitive"),
    ("core/docker.py", "clear_sync_path", "clear_directory", "primitive"),
]


def test_carrier_call_inventory():
    assert {row[3] for row in INVENTORY} <= CLASSES
    found = Counter()

    class Calls(ast.NodeVisitor):
        def __init__(self, path):
            self.path = path
            self.scope = []

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_AsyncFunctionDef(self, node):
            self.visit_FunctionDef(node)

        def visit_Call(self, node):
            callee = node.func
            name = (
                callee.id
                if isinstance(callee, ast.Name)
                else callee.attr
                if isinstance(callee, ast.Attribute)
                else None
            )
            if name in CALLEES:
                found[self.path, ".".join(self.scope), name] += 1
            self.generic_visit(node)

    package = Path(__file__).resolve().parents[1] / "src/djinn_in_a_box"
    for path in sorted(package.rglob("*.py")):
        Calls(path.relative_to(package).as_posix()).visit(ast.parse(path.read_text()))
    expected = Counter((path, function, callee) for path, function, callee, _ in INVENTORY)
    missing, extra = expected - found, found - expected
    assert found == expected, (
        f"{RULE}; classify every new path here. Missing calls: {dict(missing)}; "
        f"extra calls: {dict(extra)}"
    )


@pytest.mark.parametrize(
    "args,required",
    [
        (
            ["volumes", "--credentials", "--opencode-id", "X", "--force"],
            {"tombstone_ids"},
        ),
        (
            ["volumes", "--credentials", "--force"],
            {"merge_back", "tombstone_all", "clear_sync_path"},
        ),
        (["volumes", "--data", "--force"], {"merge_back"}),
        (["volumes", "djinn-opencode-data"], {"merge_back"}),
        (["all", "--force"], {"merge_back", "tombstone_all", "clear_sync_path"}),
    ],
    ids=["selected-logout", "logout-all", "data", "named-volume", "clean-all"],
)
def test_cleanup_carrier_primitives_hold_control_guard(
    credential_cleanup, monkeypatch, args, required
):
    _, _, _, oc = credential_cleanup
    recorded = []

    def recorder(name, result):
        def record(*args, **kwargs):
            recorded.append((name, getattr(hostctl._held, "active", False)))
            return result

        return record

    monkeypatch.setattr(
        oc, "merge_back", recorder("merge_back", oc.MergeBackSummary(0, 0, 0, False, False))
    )
    for name in ("tombstone_ids", "tombstone_all"):
        monkeypatch.setattr(oc, name, recorder(name, oc.TombstoneReport((), (), 0)))
    monkeypatch.setattr(container, "clear_sync_path", recorder("clear_sync_path", True))
    monkeypatch.setattr(container, "volume_exists", lambda _: True)
    monkeypatch.setattr(
        container, "get_existing_volumes_by_category", lambda *_: ["djinn-opencode-data"]
    )
    monkeypatch.setattr(container, "delete_volume", lambda _: True)
    monkeypatch.setattr(container, "delete_volumes", lambda names: dict.fromkeys(names, True))
    # The nested creation guard also takes control; isolate the command's own guard.
    monkeypatch.setattr(container.host_runtime, "creation_guard", nullcontext)
    monkeypatch.setattr(container.host_runtime, "inspect_dev", lambda *_: None)
    monkeypatch.setattr(hostctl, "delete_state_locked", lambda: None)
    result = CliRunner().invoke(app, ["clean", *args])
    assert result.exit_code == 0, result.output
    assert recorded, f"{RULE}: no carrier primitive reached by {args}"
    assert required <= {name for name, _ in recorded}
    assert all(held for _, held in recorded), f"{RULE}: {args}: {recorded}"
