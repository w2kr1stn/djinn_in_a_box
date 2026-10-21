"""Credential safety regressions using only temporary homes and runtime dummy values."""

import os
import shlex
import stat
from pathlib import Path

import pytest

from djinn_in_a_box.core import opencode_credentials as oc


def test_remove_tree_handles_1500_levels(tmp_path: Path) -> None:
    parent = oc.open_dir(tmp_path)
    descriptors: list[int] = []
    try:
        current = parent
        for _ in range(1500):
            os.mkdir("d", mode=0o700, dir_fd=current)
            current = os.open("d", oc._DIRECTORY_FLAGS, dir_fd=current)
            descriptors.append(current)
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        descriptors.clear()
        oc._remove_tree(parent, "d")
        assert "d" not in os.listdir(parent)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        # A failed removal must also leave pytest's recursive cleanup a shallow tree.
        current = parent
        cleanup: list[int] = []
        try:
            while "d" in os.listdir(current):
                current = os.open("d", oc._DIRECTORY_FLAGS, dir_fd=current)
                cleanup.append(current)
            for index in range(len(cleanup) - 1, -1, -1):
                os.close(cleanup[index])
                os.rmdir("d", dir_fd=cleanup[index - 1] if index else parent)
        finally:
            os.close(parent)


@pytest.mark.parametrize(
    "holder", ["Entry", "FileRead", "ConflictCopy", "CarrierRead", "_Pending", "Step"]
)
def test_credential_holder_repr_redacts_values(holder: str) -> None:
    secret = os.urandom(32).hex()
    entry = oc.validate_entry(
        {
            "id": "account",
            "integrationID": "provider",
            "label": "account",
            "active": True,
            "value": {"type": "key", "key": secret},
        }
    )
    raw = secret.encode()
    copy = oc.ConflictCopy("credentials.sync-conflict-copy.json", 1, raw, (entry,), ())
    holders = {
        "Entry": entry,
        "FileRead": oc.FileRead(raw, 1, 2, len(raw)),
        "ConflictCopy": copy,
        "CarrierRead": oc.CarrierRead(raw, [entry], raw, [], [copy], []),
        "_Pending": oc._Pending("session", 1, True, raw),
        "Step": oc.Step("update", "account", entry, entry),
    }
    assert secret in entry.canonical
    assert secret not in repr(holders[holder])


@pytest.mark.parametrize(
    "name",
    [
        "credentials.json",
        "credentials.removed.json",
        "credentials.sync-conflict-20260101-120000-ABCDEFG.json",
        "credentials.removed.sync-conflict-20260101-120000-ABCDEFG.json",
    ],
)
def test_doctor_reports_loose_files_without_changing_modes(tmp_path: Path, name: str) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    carrier = home / "carrier"
    carrier.mkdir(mode=0o700)
    target = carrier / name
    target.write_bytes(b"[]")
    target.chmod(0o644)

    findings = oc.carrier_findings(carrier, home / "sessions", policy=oc.RootPolicy(home=str(home)))

    assert oc.Finding("invalid-file", name, "file permissions must be 0600") in findings
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert target.read_bytes() == b"[]"


@pytest.mark.parametrize("loose", ["state", "root", "both"])
def test_doctor_does_not_repair_private_directories(tmp_path: Path, loose: str) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = home / ".local/state/djinn/opencode-audit"
    root.mkdir(parents=True, mode=0o700)
    root.parent.chmod(0o700)
    if loose in {"state", "both"}:
        root.parent.chmod(0o755)
    if loose in {"root", "both"}:
        root.chmod(0o755)
    before = {path: path.stat().st_mode for path in (root.parent, root)}

    findings = oc.carrier_findings(home / "absent", root, policy=oc.RootPolicy(home=str(home)))

    assert findings == [oc.Finding("audit-root", "", "private directory permissions must be 0700")]
    assert {path: path.stat().st_mode for path in before} == before


@pytest.mark.parametrize("ancestor", [".", ".local", ".local/state"])
def test_writable_ancestor_refusal_names_path_and_remedy(tmp_path: Path, ancestor: str) -> None:
    home = tmp_path / "home with spaces"
    home.mkdir(mode=0o700)
    offending = home / ancestor
    offending.mkdir(parents=True, exist_ok=True)
    offending.chmod(0o775)
    root = home / ".local/state/djinn/opencode-audit"

    with pytest.raises(oc.Refused) as caught:
        oc.create_audit_session(root, policy=oc.RootPolicy(home=str(home)))

    assert f"an ancestor {offending} is writable by group or others" in caught.value.reason
    assert f"chmod g-w,o-w {shlex.quote(str(offending))}" in caught.value.reason
    assert stat.S_IMODE(offending.stat().st_mode) == 0o775
