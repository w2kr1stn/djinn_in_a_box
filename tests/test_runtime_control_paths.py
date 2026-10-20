"""Keep runtime writers, diagnostic readers and the sealing map aligned."""

from __future__ import annotations

import ast
from pathlib import Path

from djinn_in_a_box.core import git_diagnostics, host_runtime, host_sealing, hostctl, post_mortem

CONTROL_FILES = {
    "STATE_FILE": "state.json",
    "STATE_TMP": "state.tmp",
    "CREATOR_LOCK": "creator.lock",
    "READY": "ready",
    "ATTACHED": "attached",
    "LOADED": "loaded",
    "OBSERVER_LOG": "observer.log",
}
SUBTREE_PATHS = {"public", "export", "private", "private/agent.sock", "export/auth.sock"}


def test_runtime_control_paths_use_constants():
    tree = ast.parse(Path(host_runtime.__file__).read_text())
    constants = {
        target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id in CONTROL_FILES
    }
    assert constants == CONTROL_FILES
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    seen = set()

    def check_component(component, path):
        if isinstance(component, ast.Name) and component.id in CONTROL_FILES:
            seen.add(component.id)
            return
        if isinstance(component, ast.Constant) and component.value in SUBTREE_PATHS:
            return
        ancestor = path
        while ancestor in parents:
            ancestor = parents[ancestor]
            if (
                isinstance(ancestor, ast.For)
                and isinstance(ancestor.target, ast.Name)
                and isinstance(component, ast.Name)
                and ancestor.target.id == component.id
                and isinstance(ancestor.iter, ast.Tuple | ast.List)
            ):
                for item in ancestor.iter.elts:
                    check_component(item, ancestor)
                return
        raise AssertionError(
            f"runtime path at line {path.lineno} must use a control-file constant: "
            f"{ast.unparse(component)}"
        )

    for scope in tree.body:
        if isinstance(scope, ast.FunctionDef) and any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "root" for target in node.targets)
            and ast.unparse(node.value) == "hostctl.state_root()"
            for node in ast.walk(scope)
        ):
            # This observer's root is hostctl state, not the Git runtime root.
            assert scope.name == "observe_hostctl"
            continue
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Div)
                and ast.unparse(node.left) in {"root", "self.root", "runtime_root()"}
            ):
                check_component(node.right, node)
    assert seen == CONTROL_FILES.keys()


def test_private_state_paths_match_runtime_controls(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    controller = tmp_path / "controller"
    records = tmp_path / "records"

    def controller_root(*, create):
        assert create is False
        return controller

    def records_root(*, create):
        assert create is False
        return records

    def no_io(*args, **kwargs):
        raise AssertionError("private_state_paths must only compute paths")

    monkeypatch.setattr(host_runtime, "runtime_root", lambda: runtime)
    monkeypatch.setattr(hostctl, "state_root", controller_root)
    monkeypatch.setattr(post_mortem, "records_root", records_root)
    with monkeypatch.context() as pure:
        pure.setattr(hostctl, "command", no_io)
        pure.setattr(host_runtime, "inspect_object", no_io)
        for name in ("mkdir", "open", "stat", "lstat"):
            pure.setattr(Path, name, no_io)
        paths = host_sealing.private_state_paths()

    assert paths["host controller/journal"] == controller
    assert paths["journal"] == controller / "journal.jsonl"
    assert paths["post-mortem records"] == records
    assert paths["private agent"] == runtime / "private"
    assert paths["private agent socket"] == runtime / "private/agent.sock"
    assert paths["host runtime control"] == runtime / host_runtime.STATE_FILE
    runtime_entries = {path for path in paths.values() if path.is_relative_to(runtime)}
    assert runtime_entries == {
        runtime / "private",
        runtime / "private/agent.sock",
        *(runtime / getattr(host_runtime, name) for name in CONTROL_FILES),
    }
    assert len(paths) == len(set(paths.values())) == len(CONTROL_FILES) + 5

    tree = ast.parse(Path(host_sealing.__file__).read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "private_state_paths"
    )
    used = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and ast.unparse(node.left) == "host_runtime.runtime_root()"
        ):
            if isinstance(node.right, ast.Constant):
                assert node.right.value in {"private", "private/agent.sock"}
            else:
                assert isinstance(node.right, ast.Attribute)
                assert ast.unparse(node.right.value) == "host_runtime"
                used.add(node.right.attr)
    assert used == CONTROL_FILES.keys()


def test_git_diagnostics_uses_runtime_state_constant():
    tree = ast.parse(Path(git_diagnostics.__file__).read_text())
    assignments = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "state_path" for target in node.targets
        )
    ]
    assert len(assignments) == 1
    assert ast.unparse(assignments[0]) == "root / host_runtime.STATE_FILE"
