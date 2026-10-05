from __future__ import annotations

import json
from pathlib import Path

from djinn_in_a_box.core.git_diagnostics import git_diagnostics
from djinn_in_a_box.core.host_runtime import runtime_root


def test_doctor_origins_repository_override_and_no_match_exec(git_inputs):
    home = Path.home()
    sentinel = home / "match-was-executed"
    ssh = home / ".ssh"
    (ssh / "config").write_text(
        "Host git-work\n    IdentityFile ~/.ssh/work_git.pub\n    Include other-config\n"
        f'Match exec "touch {sentinel}"\n    IdentityFile ~/.ssh/private_key\n'
    )
    (ssh / "other-config").write_text("Host other\n    IdentityFile ~/.ssh/old_key\n")
    (home / ".gitconfig").write_text(
        '[core]\n sshCommand = "ssh -F ~/.ssh/legacy-config"\n'
        "[user]\n signingkey = ~/.ssh/work_git.pub\n"
        '[gpg "ssh"]\n allowedSignersFile = ~/.ssh/allowed_signers\n'
    )
    repo = git_inputs.code_dir / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git/objects").mkdir()
    (repo / ".git/refs").mkdir()
    (repo / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (repo / ".git/config").write_text(
        "[core]\n repositoryformatversion = 0\n bare = false\n"
        ' sshCommand = "ssh -i ~/.ssh/repository_private_key"\n'
    )
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    rows = git_diagnostics(git_inputs)
    warnings = "\n".join(row.detail for row in rows if row.status == "warn")
    assert "repository_private_key" in warnings and str(repo / ".git/config") in warnings
    assert "legacy-config" in warnings and str(home / ".gitconfig") in warnings
    assert "aliases=git-work" in warnings and "include other-config" in warnings
    assert "aliases=other" in warnings and "old_key" in warnings
    assert "Match exec" in warnings and "not executed" in warnings
    assert "work_git.pub" not in warnings
    assert not sentinel.exists()
    assert before == {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}


def test_doctor_accepts_original_public_signing_selector_and_reports_idle(git_inputs):
    (Path.home() / ".gitconfig").write_text("[user]\n signingkey = ~/.ssh/work_git.pub\n")
    rows = git_diagnostics(git_inputs)
    assert any(row.name == "Git identities" and row.status == "pass" for row in rows)
    assert any(
        row.name == "Git agent" and row.status == "warn" and "not running" in row.detail
        for row in rows
    )
    assert not any(row.status != "pass" and "work_git.pub" in row.detail for row in rows)


def test_doctor_missing_data_is_unknown_and_never_starts_agent(git_inputs, monkeypatch):
    from djinn_in_a_box.core import git_agent

    monkeypatch.setattr(
        git_agent,
        "start_agent",
        lambda *args: (_ for _ in ()).throw(AssertionError("doctor must not load keys")),
    )
    root = runtime_root(create=True)
    (root / "state.json").write_text(json.dumps({"invalid": "state"}))
    (Path.home() / ".ssh/known_hosts").unlink()
    rows = git_diagnostics(git_inputs)
    assert any(row.name == "Git identities" and row.status == "fail" for row in rows)
    assert any(
        row.name == "Git agent" and row.status == "warn" and "unknown" in row.detail for row in rows
    )
    assert not (root / "private/agent.sock").exists()


def test_doctor_reports_uninspected_discovery_roots(git_inputs):
    directory = git_inputs.code_dir
    for _ in range(6):
        directory /= "nested"
        directory.mkdir()
    rows = git_diagnostics(git_inputs)
    assert any(row.name == "Git discovery" and "depth limit" in row.detail for row in rows)
