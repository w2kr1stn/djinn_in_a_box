from __future__ import annotations

import json
import os
from pathlib import Path

from djinn_in_a_box.config.declarations import BindDeclaration
from djinn_in_a_box.core.git_diagnostics import git_diagnostics
from djinn_in_a_box.core.host_runtime import runtime_root


def _create_repo(path: Path, config: str = "[core]\n repositoryformatversion = 0\n") -> Path:
    git_dir = path / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "config").write_text(config)
    return path


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
    repo = _create_repo(
        git_inputs.code_dir / "repo",
        "[core]\n repositoryformatversion = 0\n bare = false\n"
        ' sshCommand = "ssh -i ~/.ssh/repository_private_key"\n'
    )
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    rows = git_diagnostics(git_inputs)
    warnings = "\n".join(row.detail for row in rows if row.status == "warn")
    assert "repository_private_key" in warnings and str(repo / ".git/config") in warnings
    assert "legacy-config" in warnings and str(home / ".gitconfig") in warnings
    assert sum("legacy-config" in row.detail for row in rows) == 1
    assert sum("repository_private_key" in row.detail for row in rows) == 1
    assert sum("old_key" in row.detail for row in rows) == 1
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


def test_doctor_reports_uninspected_discovery_roots(git_inputs, monkeypatch):
    repo = _create_repo(git_inputs.code_dir / "repo")
    unreadable = git_inputs.code_dir / "unreadable"
    unreadable.mkdir()
    directory = git_inputs.code_dir
    for index in range(5):
        directory /= f"nested-{index}"
        directory.mkdir()
    _create_repo(directory / "too-deep")

    scandir = os.scandir

    def deny_unreadable(path):
        if not isinstance(path, int) and Path(path) == unreadable:
            raise PermissionError(13, "Permission denied", str(path))
        return scandir(path)

    import djinn_in_a_box.core.git_diagnostics as diagnostics

    monkeypatch.setattr(diagnostics.os, "scandir", deny_unreadable)
    rows = git_diagnostics(git_inputs)

    discovery = [row for row in rows if row.name == "Git discovery"]
    assert len(discovery) == 1
    assert discovery[0].status == "pass"
    assert "1 repositories scanned" in discovery[0].detail
    assert "2 directories not scanned" in discovery[0].detail
    assert str(repo) not in "\n".join(row.detail for row in rows if row.status == "warn")


def test_doctor_reports_unreadable_scan_root_as_warning(git_inputs, monkeypatch):
    import djinn_in_a_box.core.git_diagnostics as diagnostics

    scandir = os.scandir

    def deny_root(path):
        if not isinstance(path, int) and Path(path) == git_inputs.code_dir:
            raise PermissionError(13, "Permission denied", str(path))
        return scandir(path)

    monkeypatch.setattr(diagnostics.os, "scandir", deny_root)
    rows = git_diagnostics(git_inputs)
    discovery = [row for row in rows if row.name == "Git discovery"]
    assert len(discovery) == 1
    assert discovery[0].status == "warn"
    assert "1 directories not scanned" in discovery[0].detail


def test_global_git_reference_is_reported_once_for_multiple_repositories(git_inputs):
    home = Path.home()
    included = home / "global-included.gitconfig"
    included.write_text(
        '[gpg "ssh"]\n allowedSignersFile = ~/.ssh/not_allowed_signers\n'
    )
    (home / ".gitconfig").write_text(f"[include]\n path = {included}\n")
    (home / ".ssh" / "config").write_text(
        "Host legacy\n IdentityFile ~/.ssh/not_allowed_ssh_key\n"
    )
    for name in ("one", "two", "three"):
        _create_repo(git_inputs.code_dir / name)

    rows = git_diagnostics(git_inputs)
    global_findings = [
        row
        for row in rows
        if row.name == "Git references: global Git configuration"
        and "not_allowed_signers" in row.detail
    ]
    assert len(global_findings) == 1
    assert global_findings[0].status == "warn"
    assert sum("not_allowed_ssh_key" in row.detail for row in rows) == 1
    assert not any(
        row.name.startswith("Git references: ")
        and row.name != "Git references: global Git configuration"
        for row in rows
    )


def test_doctor_does_not_scan_declared_mount_sources(git_inputs):
    source = git_inputs.code_dir.parent / "mounted-archive"
    repo = _create_repo(
        source / "repo",
        "[core]\n repositoryformatversion = 0\n"
        ' sshCommand = "ssh -i ~/.ssh/mounted_repository_key"\n',
    )
    config = git_inputs.model_copy(
        update={
            "mounts": {
                "archive": BindDeclaration(source=str(source), target="/mnt/archive")
            }
        }
    )

    rows = git_diagnostics(config)
    assert not any("mounted_repository_key" in row.detail for row in rows)
    assert not any(str(repo) in row.name or str(repo) in row.detail for row in rows)
    discovery = [row for row in rows if row.name == "Git discovery"]
    assert len(discovery) == 1
    assert discovery[0].detail.startswith("0 repositories scanned")


def test_repository_reads_ignore_global_host_path_includeif(git_inputs):
    home = Path.home()
    _create_repo(git_inputs.code_dir / "repo")
    host_config = home / "host-path.gitconfig"
    host_config.write_text("[user]\n signingkey = ~/.ssh/host_only_private_key\n")
    (home / ".gitconfig").write_text(
        f'[includeIf "gitdir:{git_inputs.code_dir}/"]\n path = {host_config}\n'
    )

    rows = git_diagnostics(git_inputs)
    assert not any("host_only_private_key" in row.detail for row in rows)
