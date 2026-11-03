"""Verify the workspace instruction hook with isolated files and subprocesses."""

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "djinn-agents-chain.py"
spec = importlib.util.spec_from_file_location("djinn_agents_chain", SCRIPT)
assert spec and spec.loader
chain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chain)


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    project = root / "group" / "repo"
    (project / ".git").mkdir(parents=True)
    return root, project


def test_project_root_git_directory(workspace: tuple[Path, Path]) -> None:
    _, project = workspace
    cwd = project / "src"
    cwd.mkdir()
    assert chain.project_root(str(cwd)) == str(project)


def test_project_root_git_file(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /unused/worktree\n")
    cwd = tmp_path / "src"
    cwd.mkdir()
    assert chain.project_root(str(cwd)) == str(tmp_path)


def test_project_root_no_marker_returns_cwd(tmp_path: Path) -> None:
    cwd = tmp_path / "child"
    cwd.mkdir()
    assert chain.project_root(str(cwd)) == str(cwd)


def test_project_root_nearest_marker_wins(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    (root / ".git").mkdir()
    cwd = project / "src"
    cwd.mkdir()
    assert chain.project_root(str(cwd)) == str(project)


def test_project_root_dangling_git_marker(tmp_path: Path) -> None:
    (tmp_path / ".git").symlink_to(tmp_path / "missing")
    cwd = tmp_path / "src"
    cwd.mkdir()
    assert chain.project_root(str(cwd)) == str(tmp_path)


def test_project_root_includes_filesystem_root() -> None:
    with patch.object(chain.os.path, "lexists", side_effect={"/.git"}.__contains__):
        assert chain.project_root("/synthetic/child") == "/"


def test_chain_directories_top_down_excludes_project(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    assert chain.chain_directories(str(root), str(project)) == [str(root), str(project.parent)]


def test_chain_directories_project_equals_root(workspace: tuple[Path, Path]) -> None:
    _, project = workspace
    assert chain.chain_directories(str(project), str(project)) == []


def test_chain_directories_project_above_root(workspace: tuple[Path, Path]) -> None:
    _, project = workspace
    root = project / "src"
    root.mkdir()
    assert chain.chain_directories(str(root), str(root)) == []


def test_chain_directories_cwd_equals_root_without_repository(tmp_path: Path) -> None:
    assert chain.chain_directories(str(tmp_path), str(tmp_path)) == []


def test_chain_directories_deep_cwd_stops_above_project(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    cwd = project / "src" / "nested"
    cwd.mkdir(parents=True)
    assert chain.chain_directories(str(root), str(cwd)) == [str(root), str(project.parent)]


def test_qualifying_file_regular_inside_root(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("Workspace rules")
    assert chain.qualifying_file(str(tmp_path), str(tmp_path)) == str(path)


def test_qualifying_file_missing(tmp_path: Path) -> None:
    assert chain.qualifying_file(str(tmp_path), str(tmp_path)) is None


def test_qualifying_file_directory_refused(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").mkdir()
    assert chain.qualifying_file(str(tmp_path), str(tmp_path)) is None


def test_qualifying_file_symlink_inside_root(tmp_path: Path) -> None:
    target = tmp_path / "rules.md"
    target.write_text("Workspace rules")
    path = tmp_path / "AGENTS.md"
    path.symlink_to(target)
    assert chain.qualifying_file(str(tmp_path), str(tmp_path)) == str(path)


def test_qualifying_file_symlink_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "workspace-other"
    target.write_text("Outside rules")
    (root / "AGENTS.md").symlink_to(target)
    assert chain.qualifying_file(str(root), str(root)) is None


def test_qualifying_file_dangling_symlink(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").symlink_to(tmp_path / "missing")
    assert chain.qualifying_file(str(tmp_path), str(tmp_path)) is None


def test_render_exact_header(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    assert chain.render(str(root), str(project), []) == (
        f"Workspace AGENTS.md files above the repository root {project}, "
        f"from the workspace root {root} down. "
        "Codex does not load these itself; follow them like any AGENTS.md."
    )


def test_render_whole_files_in_order(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    first = root / "AGENTS.md"
    second = project.parent / "AGENTS.md"
    first.write_text("First rules\n", encoding="utf-8")
    second.write_text("Second rules\n", encoding="utf-8")
    header = chain.render(str(root), str(project), [])
    assert chain.render(str(root), str(project), [str(first), str(second)]) == (
        header + f"\n\nContents of {first}:\n\nFirst rules\n"
        f"\n\nContents of {second}:\n\nSecond rules\n"
    )


def test_render_oversized_file_not_read(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    first = root / "AGENTS.md"
    first.write_text("Earlier rules\n")
    path = project.parent / "AGENTS.md"
    before = chain.render(str(root), str(project), [str(first)])
    head = f"\n\nContents of {path}:\n\n"
    size = chain.output_cap - chain.pointer_reserve - len((before + head).encode()) + 1
    path.write_bytes(b"x" * size)
    path.chmod(0o000)
    try:
        with patch("builtins.open", wraps=open) as opened:
            text = chain.render(str(root), str(project), [str(first), str(path)])
            opened.assert_called_once_with(str(first), encoding="utf-8", errors="replace")
    finally:
        path.chmod(0o600)
    assert text == (
        before + f"\n\nNot included (size cap): {path} ({size} bytes). Read it before working."
    )


def test_render_invalid_utf8_expansion_exceeds_budget(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    path = root / "AGENTS.md"
    before = chain.render(str(root), str(project), [])
    head = f"\n\nContents of {path}:\n\n"
    size = chain.output_cap - chain.pointer_reserve - len((before + head).encode())
    path.write_bytes(b"\xff" * size)
    assert chain.render(str(root), str(project), [str(path)]) == (
        before + f"\n\nNot included (size cap): {path} ({size} bytes). Read it before working."
    )


def test_render_multibyte_content_counted_in_bytes(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    path = root / "AGENTS.md"
    before = chain.render(str(root), str(project), [])
    head = f"\n\nContents of {path}:\n\n"
    remaining = chain.output_cap - chain.pointer_reserve - len((before + head).encode())
    content = "\u754c" * (remaining // 3 + 1)
    assert len(content) < remaining < len(content.encode())
    path.write_text(content, encoding="utf-8")
    size = path.stat().st_size
    assert chain.render(str(root), str(project), [str(path)]) == (
        before + f"\n\nNot included (size cap): {path} ({size} bytes). Read it before working."
    )


@pytest.mark.parametrize("content_unit", ["x", "\u754c"])
def test_render_file_exactly_at_budget(workspace: tuple[Path, Path], content_unit: str) -> None:
    root, project = workspace
    path = root / "AGENTS.md"
    before = chain.render(str(root), str(project), [])
    head = f"\n\nContents of {path}:\n\n"
    remaining = chain.output_cap - chain.pointer_reserve - len((before + head).encode())
    count, padding = divmod(remaining, len(content_unit.encode()))
    content = content_unit * count + "x" * padding
    path.write_text(content, encoding="utf-8")
    text = chain.render(str(root), str(project), [str(path)])
    assert text == before + head + content
    assert len(text.encode()) == chain.output_cap - chain.pointer_reserve


def test_render_invalid_utf8_replaced_when_it_fits(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    path = root / "AGENTS.md"
    path.write_bytes(b"a\xffb")
    before = chain.render(str(root), str(project), [])
    assert chain.render(str(root), str(project), [str(path)]) == (
        before + f"\n\nContents of {path}:\n\na\ufffdb"
    )


def test_render_final_cap_stays_valid_utf8(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    path = root / ("\u754c" * 25 + ".md")
    path.write_bytes(b"x" * chain.output_cap)
    files = [str(path)] * 300
    header = chain.render(str(root), str(project), [])
    pointer = (
        f"\n\nNot included (size cap): {path} ({chain.output_cap} bytes). Read it before working."
    )
    encoded = (header + pointer * len(files)).encode()
    while True:
        try:
            encoded[: chain.output_cap].decode("utf-8")
        except UnicodeDecodeError:
            break
        project = project / "x"
        header = chain.render(str(root), str(project), [])
        encoded = (header + pointer * len(files)).encode()
    text = chain.render(str(root), str(project), files)
    assert len(text.encode()) <= chain.output_cap
    assert text == encoded[: chain.output_cap].decode("utf-8", errors="ignore")
    assert text.encode().decode("utf-8") == text


@pytest.mark.parametrize("workspace_value", [None, "", "relative/workspace"])
def test_context_for_invalid_workspace(
    workspace: tuple[Path, Path], workspace_value: str | None
) -> None:
    _, project = workspace
    assert chain.context_for({"cwd": str(project)}, workspace_value) is None


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_context_for_workspace_not_directory(tmp_path: Path, kind: str) -> None:
    root = tmp_path / kind
    if kind == "file":
        root.write_text("Not a directory")
    assert chain.context_for({"cwd": str(tmp_path)}, str(root)) is None


@pytest.mark.parametrize("payload", [None, [], "invalid", 7])
def test_context_for_payload_not_dict(workspace: tuple[Path, Path], payload: object) -> None:
    root, _ = workspace
    (root / "AGENTS.md").write_text("Workspace rules")
    assert chain.context_for(payload, str(root)) is None


@pytest.mark.parametrize("payload", [{}, {"cwd": None}, {"cwd": 7}, {"cwd": []}])
def test_context_for_cwd_not_string(workspace: tuple[Path, Path], payload: object) -> None:
    root, _ = workspace
    (root / "AGENTS.md").write_text("Workspace rules")
    assert chain.context_for(payload, str(root)) is None


@pytest.mark.parametrize("cwd", ["", "relative/repo"])
def test_context_for_cwd_not_absolute(workspace: tuple[Path, Path], cwd: str) -> None:
    root, _ = workspace
    (root / "AGENTS.md").write_text("Workspace rules")
    assert chain.context_for({"cwd": cwd}, str(root)) is None


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_context_for_cwd_not_directory(workspace: tuple[Path, Path], kind: str) -> None:
    root, _ = workspace
    (root / "AGENTS.md").write_text("Workspace rules")
    cwd = root / kind
    if kind == "file":
        cwd.write_text("Not a directory")
    assert chain.context_for({"cwd": str(cwd)}, str(root)) is None


def test_context_for_cwd_outside_root(workspace: tuple[Path, Path], tmp_path: Path) -> None:
    root, _ = workspace
    (root / "AGENTS.md").write_text("Workspace rules")
    cwd = tmp_path / "workspace-other"
    cwd.mkdir()
    assert chain.context_for({"cwd": str(cwd)}, str(root)) is None


def test_context_for_no_qualifying_files(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    (project / "AGENTS.md").write_text("Repository rules")
    assert chain.context_for({"cwd": str(project)}, str(root)) is None


def test_context_for_cwd_equal_to_root(workspace: tuple[Path, Path]) -> None:
    root, _ = workspace
    (root / "AGENTS.md").write_text("Workspace rules")
    assert chain.context_for({"cwd": str(root)}, str(root)) is None


def test_context_for_qualifying_chain_in_order(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    first = root / "AGENTS.md"
    second = project.parent / "AGENTS.md"
    first.write_text("Workspace rules")
    second.write_text("Group rules")
    (project / "AGENTS.md").write_text("Repository rules")
    cwd = project / "src"
    cwd.mkdir()
    assert chain.context_for({"cwd": str(cwd)}, str(root)) == chain.render(
        str(root), str(project), [str(first), str(second)]
    )


def test_context_for_workspace_symlink(workspace: tuple[Path, Path], tmp_path: Path) -> None:
    root, project = workspace
    path = root / "AGENTS.md"
    path.write_text("Workspace rules")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert chain.context_for({"cwd": str(project)}, str(alias)) == chain.render(
        str(root), str(project), [str(path)]
    )


def test_context_for_cwd_symlink(workspace: tuple[Path, Path], tmp_path: Path) -> None:
    root, project = workspace
    path = root / "AGENTS.md"
    path.write_text("Workspace rules")
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    assert chain.context_for({"cwd": str(alias)}, str(root)) == chain.render(
        str(root), str(project), [str(path)]
    )


def test_context_for_cwd_symlink_outside_root(workspace: tuple[Path, Path], tmp_path: Path) -> None:
    root, _ = workspace
    (root / "AGENTS.md").write_text("Workspace rules")
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = root / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    assert chain.context_for({"cwd": str(alias)}, str(root)) is None


def test_main_stdout_json_shape(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    path = root / "AGENTS.md"
    path.write_text("Workspace rules")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"cwd": str(project)}).encode(),
        env={"DJINN_WORKSPACE_TARGET": str(root)},
        capture_output=True,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": chain.render(str(root), str(project), [str(path)]),
        }
    }


def test_main_invalid_json_fails_open(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=b"{invalid json",
        env={"DJINN_WORKSPACE_TARGET": str(tmp_path)},
        capture_output=True,
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr.startswith(b"djinn-agents-chain:")
    assert len(result.stderr.splitlines()) == 1


def test_main_unset_workspace_is_silent(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"cwd": str(tmp_path)}).encode(),
        env={},
        capture_output=True,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""


def test_main_stdin_read_is_bounded(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    path = root / "AGENTS.md"
    path.write_text("Workspace rules")
    payload = json.dumps({"cwd": str(project)}).encode().ljust(chain.stdin_limit, b" ")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload + b"invalid trailing input",
        env={"DJINN_WORKSPACE_TARGET": str(root)},
        capture_output=True,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == chain.render(
        str(root), str(project), [str(path)]
    )


def test_main_invalid_unicode_fails_open(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=b"\xff",
        env={"DJINN_WORKSPACE_TARGET": str(tmp_path)},
        capture_output=True,
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr.startswith(b"djinn-agents-chain:")


def test_main_filesystem_error_fails_open(workspace: tuple[Path, Path]) -> None:
    root, project = workspace
    path = root / "AGENTS.md"
    path.write_text("Workspace rules")
    path.chmod(0o000)
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps({"cwd": str(project)}).encode(),
            env={"DJINN_WORKSPACE_TARGET": str(root)},
            capture_output=True,
        )
    finally:
        path.chmod(0o600)
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr.startswith(b"djinn-agents-chain:")


def test_config_toml_session_start_hook() -> None:
    config = tomllib.loads((SCRIPT.parent / "codex-config.toml").read_text())
    assert config == {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|clear|compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 /usr/local/libexec/djinn-agents-chain.py",
                            "additionalContextLimit": chain.output_cap,
                        }
                    ],
                }
            ]
        }
    }


def test_dockerfile_copy_lines_and_command_destination() -> None:
    dockerfile = (SCRIPT.parents[1] / "Dockerfile").read_text()
    anchor = "COPY --chmod=755 scripts/djinn-apt-root.sh /usr/local/libexec/djinn-apt-root\n"
    script_copy = (
        "COPY --chmod=755 scripts/djinn-agents-chain.py /usr/local/libexec/djinn-agents-chain.py\n"
    )
    config_copy = "COPY --chmod=644 scripts/codex-config.toml /etc/codex/config.toml\n"
    assert anchor + script_copy + config_copy in dockerfile
    config = tomllib.loads((SCRIPT.parent / "codex-config.toml").read_text())
    command = config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command.split() == ["python3", script_copy.split()[-1]]
