"""Report-only Git/SSH configuration inspection; never execute SSH Match commands."""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from djinn_in_a_box.config.declarations import BindDeclaration
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.git_agent import agent_keys
from djinn_in_a_box.core.host_runtime import inspect_dev, process_token, runtime_root
from djinn_in_a_box.core.ssh_delivery import (
    AGENT_SOCKET,
    SSH_TARGET,
    GitSSHError,
    read_public_delivery,
)


@dataclass(frozen=True)
class GitDiagnostic:
    name: str
    status: str
    detail: str
    remedy: str = ""


def public_selector(value: str, names: set[str]) -> bool:
    path = value.replace("${HOME}", "/home/dev").replace("$HOME", "/home/dev")
    if path.startswith("~/"):
        path = "/home/dev/" + path[2:]
    return str(Path(path).parent) == str(SSH_TARGET) and Path(path).name in names


def repository_roots(config: AppConfig) -> tuple[list[Path], list[str]]:
    roots = [config.code_dir, Path.home() / ".djinn" / "sessions"]
    roots.extend(
        Path(m.source).expanduser()
        for m in config.mounts.values()
        if isinstance(m, BindDeclaration)
    )
    repositories: list[Path] = []
    unknown: list[str] = []
    visited = 0
    for root in dict.fromkeys(roots):
        if not root.is_dir():
            unknown.append(f"{root}: missing/uninspected")
            continue
        errors: list[OSError] = []
        for directory, children, _ in os.walk(root, followlinks=False, onerror=errors.append):
            visited += 1
            current = Path(directory)
            if (current / ".git").exists() or (
                (current / "HEAD").is_file() and (current / "objects").is_dir()
            ):
                repositories.append(current)
            children[:] = [c for c in children if c not in {".git", ".venv", "node_modules"}]
            if len(current.relative_to(root).parts) >= 4:
                if children:
                    unknown.append(f"{current}: depth limit (4)")
                children.clear()
            if visited >= 1000 or len(repositories) >= 100:
                unknown.append(f"{root}: discovery limit (1000 directories/100 repositories)")
                break
        unknown.extend(str(error) for error in errors)
    return list(dict.fromkeys(repositories)), unknown


def git_references(
    repository: Path | None, public_names: set[str], allowed_signers: bool
) -> list[GitDiagnostic]:
    label = str(repository) if repository else "global Git configuration"
    command = ["git"]
    if repository:
        command.extend(["-C", str(repository)])
    command.extend(["config", "--includes", "--show-origin", "--null"])
    if repository is None:
        command.append("--global")
    command.append("--list")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return [GitDiagnostic(f"Git references: {label}", "warn", f"uninspected: {exc}")]
    if result.returncode:
        return [
            GitDiagnostic(
                f"Git references: {label}",
                "warn",
                "configuration could not be inspected: " + result.stderr.strip(),
            )
        ]
    records = result.stdout.split("\0")
    rows: list[GitDiagnostic] = []
    for index in range(0, len(records) - 1, 2):
        origin = records[index]
        if repository and origin.startswith("file:"):
            source = Path(origin.removeprefix("file:"))
            if not source.is_absolute():
                origin = "file:" + str(repository / source)
        key, _, value = records[index + 1].partition("\n")
        references: list[tuple[str, str]] = []
        if key.lower() == "core.sshcommand":
            try:
                words = shlex.split(value)
            except ValueError:
                rows.append(
                    GitDiagnostic(
                        f"Git references: {label}", "warn", f"{origin}: invalid core.sshCommand"
                    )
                )
                continue
            for i, word in enumerate(words):
                for flag in ("-i", "-F"):
                    if word == flag:
                        references.append(
                            (flag, words[i + 1] if i + 1 < len(words) else "<missing>")
                        )
                    elif word.startswith(flag) and len(word) > 2:
                        references.append((flag, word[2:]))
                if word == "-o" and i + 1 < len(words):
                    option = words[i + 1]
                    if option.lower().startswith(
                        ("identityfile=", "identityagent=", "userknownhostsfile=")
                    ):
                        references.append((option.partition("=")[0], option.partition("=")[2]))
                elif word.startswith("-o"):
                    option = word[2:]
                    if option.lower().startswith(("identityfile=", "identityagent=")):
                        references.append((option.partition("=")[0], option.partition("=")[2]))
        elif key.lower() in {"user.signingkey", "gpg.ssh.allowedsignersfile"}:
            references.append((key, value))
        elif key.lower().startswith("include"):
            expanded = Path(value).expanduser()
            if ".ssh" in expanded.parts:
                references.append((key, value))
        for kind, path in references:
            valid = public_selector(path, public_names)
            if kind == "user.signingkey" and path.startswith("key::"):
                valid = True
            if kind.lower() in {"gpg.ssh.allowedsignersfile"}:
                valid = allowed_signers and public_selector(path, {"allowed_signers"})
            if kind == "-F":
                valid = public_selector(path, {"config"})
            if kind.lower() == "identityagent":
                valid = path == AGENT_SOCKET
            if not valid:
                rows.append(
                    GitDiagnostic(
                        f"Git references: {label}",
                        "warn",
                        f"{origin}: {kind} references {path}",
                        "Use the generated ~/.ssh/config and an original-name "
                        "declared ~/.ssh/<key>.pub selector; "
                        "use /home/dev/.ssh/allowed_signers for copied signing trust. "
                        "Edit repository/global config manually.",
                    )
                )
    return rows or [
        GitDiagnostic(
            f"Git references: {label}", "pass", "no incompatible inspected key-file references"
        )
    ]


def ssh_references(
    path: Path, names: set[str], seen: set[Path] | None = None, aliases: str = "*"
) -> list[GitDiagnostic]:
    visited: set[Path] = set() if seen is None else seen
    if path in visited:
        return []
    if len(visited) >= 50:
        return [
            GitDiagnostic(
                "SSH references",
                "warn",
                "Include discovery limit (50 files); remaining configuration uninspected",
            )
        ]
    visited.add(path)
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        return [GitDiagnostic("SSH references", "warn", f"{path}: uninspected: {exc}")]
    rows: list[GitDiagnostic] = []
    for number, line in enumerate(lines, 1):
        try:
            parts = shlex.split(line, comments=True)
        except ValueError:
            rows.append(
                GitDiagnostic(
                    "SSH references",
                    "warn",
                    f"{path}:{number}: malformed/uninspected SSH directive",
                )
            )
            continue
        if not parts:
            continue
        directive = re.fullmatch(r"([A-Za-z]+)=(.*)", parts[0])
        if directive:
            parts = [directive[1], directive[2], *parts[1:]]
        if len(parts) > 1 and parts[1] == "=":
            parts.pop(1)
        key = parts[0].lower()
        if key == "host":
            aliases = " ".join(parts[1:])
        if key == "match":
            aliases = "Match " + " ".join(parts[1:]) + " (not executed)"
        if key in {"identityfile", "include"}:
            for value in parts[1:]:
                valid = value.lower() == "none" or public_selector(
                    value, names if key == "identityfile" else {"config"}
                )
                if not valid:
                    rows.append(
                        GitDiagnostic(
                            "SSH references",
                            "warn",
                            f"{path}:{number}: aliases={aliases}: {key} {value}",
                            "Declare each Git alias/key in config.toml. "
                            "Host SSH config/Includes are not delivered; "
                            "use generated config and /home/dev/.ssh/<original-name>.pub.",
                        )
                    )
                if key == "include":
                    expanded = Path(value).expanduser()
                    pattern = str(
                        expanded if expanded.is_absolute() else Path.home() / ".ssh" / expanded
                    )
                    matches = glob.glob(pattern)
                    if len(matches) > 50:
                        rows.append(
                            GitDiagnostic(
                                "SSH references",
                                "warn",
                                f"{path}:{number}: Include {value}: matches beyond 50 uninspected",
                            )
                        )
                    matches = matches[:50]
                    if not matches:
                        rows.append(
                            GitDiagnostic(
                                "SSH references",
                                "warn",
                                f"{path}:{number}: Include {value} uninspected/missing",
                            )
                        )
                    for match in matches:
                        rows.extend(ssh_references(Path(match), names, visited, aliases))
    return rows


def git_diagnostics(config: AppConfig) -> list[GitDiagnostic]:
    rows: list[GitDiagnostic] = []
    names: set[str] = set()
    expected: frozenset[bytes] | None = None
    try:
        delivery = read_public_delivery(config.git)
        expected = delivery.blobs
        names = {i.public_key_file.name for i in config.git.identities.values()}
        rows.append(
            GitDiagnostic(
                "Git identities",
                "pass",
                f"{len(config.git.identities)} declared aliases; public keys and host trust valid",
            )
        )
        for alias, identity in config.git.identities.items():
            if not identity.key_file.is_file():
                rows.append(
                    GitDiagnostic(
                        f"Git identity: {alias}",
                        "fail",
                        "declared host private key is missing",
                        "Fix the host key_file; doctor does not load or repair keys.",
                    )
                )
    except (OSError, GitSSHError, subprocess.SubprocessError) as exc:
        rows.append(
            GitDiagnostic(
                "Git identities",
                "fail",
                str(exc),
                "Fix the declarations/public trust on the host; doctor never loads keys.",
            )
        )
    try:
        root = runtime_root()
        state_path = root / "state.json"
        if not state_path.exists():
            rows.append(
                GitDiagnostic(
                    "Git agent", "warn", "not running; loaded keys and dev lifecycle uninspected"
                )
            )
        else:
            state = json.loads(state_path.read_text())
            live = process_token(state["observer_pid"]) == state["observer_token"]
            loaded = agent_keys(root / "export" / "auth.sock")
            private_loaded = agent_keys(root / "private" / "agent.sock")
            actual = inspect_dev(state["container_name"])
            ok = (
                live
                and expected is not None
                and loaded == expected
                and private_loaded == expected
                and actual is not None
                and actual[1]
                and actual[0] == state.get("dev_id")
                and actual[2] == state["generation"]
            )
            rows.append(
                GitDiagnostic(
                    "Git agent",
                    "pass" if ok else "warn",
                    "declared key set; observer attached to dev " + state["dev_id"]
                    if ok
                    else "key set/lifecycle not confirmed against current declarations",
                )
            )
    except (OSError, ValueError, KeyError, GitSSHError, EOFError) as exc:
        rows.append(GitDiagnostic("Git agent", "warn", f"unknown/uninspected: {exc}"))
    rows.extend(git_references(None, names, config.git.allowed_signers_file is not None))
    repos, unknown = repository_roots(config)
    for repository in repos:
        rows.extend(git_references(repository, names, config.git.allowed_signers_file is not None))
    for message in unknown:
        rows.append(GitDiagnostic("Git discovery", "warn", message))
    rows.extend(ssh_references(Path.home() / ".ssh" / "config", names))
    return rows
