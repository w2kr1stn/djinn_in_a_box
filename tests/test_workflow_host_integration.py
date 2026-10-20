from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands import config as config_command
from djinn_in_a_box.commands import doctor
from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import config_workflow as workflow
from djinn_in_a_box.core import session
from djinn_in_a_box.core.config_sync import (
    CANONICAL_REMEDY,
    WORKFLOW_REMEDIES,
    audit_config_sync,
    sync_config,
    workflow_item_label,
    workflow_item_summary,
    workflow_remedy,
)
from djinn_in_a_box.core.docker import WorkflowImageCompatibility
from djinn_in_a_box.core.workflow_publisher import (
    PUBLISHER_DRIFT_ITEMS_PREFIX,
    RUNTIME_MANIFEST_NAME,
    DriftClass,
    DriftEntry,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def case(tmp_path, monkeypatch):
    project = tmp_path / "project"
    for tool in ("claude", "codex", "opencode"):
        (project / "config" / tool).mkdir(parents=True)
    (project / "config/claude/AGENTS.md").write_text("original\n")
    config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "runtime")
    config_path = tmp_path / "djinn.toml"
    save_config(config, config_path)
    monkeypatch.setattr(workflow, "HOST_WORKFLOW_TRUST_FILE", tmp_path / "trust.json")
    monkeypatch.setattr(config_command, "load_config", lambda: config)
    monkeypatch.setattr(config_command, "get_project_root", lambda: project)
    monkeypatch.setattr(
        config_command,
        "audit_workflow_config",
        lambda root: audit_config_sync(root, config_path=config_path),
    )
    monkeypatch.setattr(
        config_command,
        "synchronize_workflow_config",
        lambda root: sync_config(root, config_path=config_path),
    )
    return project, config_path, config


def prepare(case, *, host=False, confirm=lambda review: True):
    project, config_path, config = case
    root = config.code_dir / "host" if host else config.config_root / "codex"
    target = workflow.WorkflowDeliveryTarget("codex", root, provision=True)
    return workflow.prepare_config_workflow(
        project,
        (target,),
        config_path=config_path,
        confirm_host_workflow=confirm,
    )


def replace_file(path, content):
    path.unlink()
    path.write_text(content)


def tree(root):
    return {
        str(path.relative_to(root)): (path.stat().st_mode, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }


def test_start_root_set_and_compose_skip(case):
    project, _, config = case
    targets = workflow.start_workflow_targets(config)
    assert [(t.tool, t.destination_root, t.provision) for t in targets] == [
        ("claude", config.config_root / "claude", False),
        ("codex", config.config_root / "codex", False),
    ]
    inspections = workflow.inspect_workflow_targets(project, config)
    assert [i.target.tool for i in inspections] == ["codex"]
    assert inspections[0].clean
    assert not config.config_root.exists()


def test_check_before_auto_sync_writes_nothing(case):
    project, _, config = case
    assert prepare(case).success
    replace_file(config.config_root / "codex/AGENTS.md", "runtime edit\n")
    (project / "config/claude/AGENTS.md").write_text("source update\n")
    before = tree(project / "config")
    result = prepare(case)
    assert result.success is False
    assert result.problems[0].identifier == "target-drift"
    assert result.problems[0].message == (
        f"Workflow preflight blocked: target-drift in codex:AGENTS.md ({config.config_root}/codex)."
    )
    assert result.problems[0].remedy == WORKFLOW_REMEDIES["target-drift"].format(source="claude")
    assert result.notices == ()
    assert tree(project / "config") == before


def test_host_drift_precedes_confirmation(case):
    assert prepare(case, host=True).success
    root = case[2].code_dir / "host"
    replace_file(root / "AGENTS.md", "host edit\n")
    (case[0] / "config/claude/AGENTS.md").write_text("pending source\n")
    before = tree(case[0] / "config")
    confirm = Mock(side_effect=AssertionError("prompted"))
    result = prepare(case, host=True, confirm=confirm)
    assert not result.success
    confirm.assert_not_called()
    assert result.problems[0].remedy == WORKFLOW_REMEDIES["host-target-drift"].format(
        source="claude"
    )
    assert result.problems[0].message.endswith(
        f"codex:AGENTS.md ({root}): restore from config/codex/AGENTS.md"
    )
    assert tree(case[0] / "config") == before


def test_notice_is_kept_and_printed_on_later_refusal(case, monkeypatch):
    project, path, config = case
    # Missing, unprovisioned target refuses after the source-only auto-sync.
    result = workflow.prepare_config_workflow(
        project,
        (workflow.WorkflowDeliveryTarget("codex", config.config_root / "codex"),),
        config_path=path,
    )
    assert result.problems[0].identifier == "target-missing"
    assert result.notices == (
        "Synchronized source changes into the canonical workflow: 3 changed, 0 removed.",
    )
    output = Mock()
    monkeypatch.setattr("djinn_in_a_box.core.console.info", output)
    workflow.print_workflow_notices(result)
    output.assert_called_once_with(result.notices[0])


@pytest.mark.parametrize(
    ("tool", "path", "key", "kwargs", "expected"),
    [
        ("codex", "AGENTS.md", None, {}, "config/codex/AGENTS.md"),
        (None, None, None, {}, "config"),
        ("codex", "a.toml", ("hooks", "Stop"), {}, "config/codex/a.toml[hooks.Stop]"),
        (
            "codex",
            "AGENTS.md",
            None,
            {"root": "/runtime/codex"},
            "codex:AGENTS.md (/runtime/codex)",
        ),
        ("opencode", "AGENTS.md", None, {"opencode_runtime": True}, "opencode-runtime:AGENTS.md"),
        (
            "opencode",
            "AGENTS.md",
            None,
            {"origin": "seed", "opencode_runtime": True},
            "config/opencode/AGENTS.md (seed)",
        ),
        (
            None,
            ".djinn-config-sync.json",
            None,
            {"origin": "canonical-manifest"},
            "config/.djinn-config-sync.json",
        ),
        (
            "codex",
            RUNTIME_MANIFEST_NAME,
            None,
            {"root": "/runtime"},
            "codex:.djinn-workflow-state.json (/runtime)",
        ),
        (
            "opencode",
            RUNTIME_MANIFEST_NAME,
            None,
            {"opencode_runtime": True},
            "opencode-runtime:.djinn-workflow-state.json",
        ),
        (
            "co\ndex",
            "a[bad]\n",
            ("x.y", "[z]"),
            {"root": "/a (b)"},
            "co?dex:a?bad??[x.y.?z?] (/a??b?)",
        ),
    ],
)
def test_label_spellings(tool, path, key, kwargs, expected):
    assert workflow_item_label(tool, path, key, **kwargs) == expected


@pytest.mark.parametrize(
    ("count", "total", "expected"),
    [
        (0, None, ""),
        (5, None, "0, 1, 2, 3, 4"),
        (6, None, "0, 1, 2, 3, 4 and 1 more"),
        (5, 207, "0, 1, 2, 3, 4 and 202 more"),
        (1, 10, "0 and 9 more"),
        (1, 3, "0 and 2 more"),
    ],
)
def test_item_cap(count, total, expected):
    assert workflow_item_summary(tuple(map(str, range(count))), total=total) == expected


def test_component_length_limit():
    assert workflow_item_label("x" * 170, "y" * 170, ("z" * 170,), root="r" * 170) == (
        "x" * 160 + ":" + "y" * 160 + "[" + "z" * 160 + "] (" + "r" * 160 + ")"
    )


@pytest.mark.parametrize(
    ("kind", "scope", "expected"),
    [
        (DriftClass.SOURCE_CHANGED, "start", "Run `djinn config sync`, then retry."),
        (
            DriftClass.COLLISION,
            "start",
            "Move or remove the conflicting unmanaged workflow item, then retry.",
        ),
        (DriftClass.INVALID_OR_SEMANTIC, "start", CANONICAL_REMEDY),
        (
            DriftClass.TARGET_DRIFT,
            "opencode",
            "Keep wanted edits in the selected source config/codex, then recreate the container: "
            "`djinn clean`, then `djinn start`.",
        ),
    ],
)
def test_remedy_contract(kind, scope, expected):
    assert workflow_remedy(kind, "codex", scope=scope) == expected


def test_changed_during_publish_and_toml_remedies():
    changed = DriftEntry(
        DriftClass.TARGET_DRIFT, PurePosixPath("a"), reason="changed-during-publish"
    )
    assert (
        workflow_remedy(changed.kind, "claude", items=(changed,))
        == "changed while publishing; retry"
    )
    invalid = DriftEntry(DriftClass.INVALID_OR_SEMANTIC, PurePosixPath("a.toml"), ("hooks",))
    assert workflow_remedy(invalid.kind, "claude", items=(invalid,)) == (
        "Rewrite the named managed TOML key as a single-line value, or delete the named key "
        "and retry. Keep neighbouring personal keys."
    )


def test_status_and_plain_sync_inspect_runtime_without_repair(case):
    project, _, config = case
    assert prepare(case).success
    runtime = config.config_root / "codex"
    replace_file(runtime / "AGENTS.md", "edited\n")
    (project / "config/claude/AGENTS.md").write_text("new source\n")
    before = tree(runtime)
    status = CliRunner().invoke(app, ["config", "status"])
    assert status.exit_code == 1, status.output
    assert f"Drift: target-drift (codex:AGENTS.md ({runtime}))" in " ".join(status.output.split())
    assert "config/codex/AGENTS.md" in status.output
    synced = CliRunner().invoke(app, ["config", "sync"])
    assert synced.exit_code == 0, synced.output
    assert f"target-drift in codex:AGENTS.md ({runtime})" in " ".join(synced.output.split())
    assert "Run `djinn config status`" in synced.output
    assert tree(runtime) == before


def diagnostic(**changes):
    item = {
        "class": "target-drift",
        "origin": "target",
        "root": "/runtime",
        "path": "AGENTS.md",
        "key": None,
        "reason": None,
    }
    item.update(changes)
    return PUBLISHER_DRIFT_ITEMS_PREFIX + json.dumps(
        {"items": [item], "total": 1, "truncated": False}
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("class", 1),
        ("class", "bogus"),
        ("origin", 1),
        ("origin", "bogus"),
        ("root", []),
        ("path", None),
        ("key", "hooks"),
        ("key", [1]),
        ("reason", 1),
    ],
)
def test_parser_checks_every_item_field(field, value):
    assert session.parse_publisher_drift_items(diagnostic(**{field: value})) is None


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"items": [], "total": True, "truncated": False},
        {"items": [], "total": True, "truncated": True},
        {"items": [], "total": False, "truncated": False},
        {"items": {}, "total": 0, "truncated": False},
        {"items": [], "total": 0, "truncated": 0},
        {"items": [], "total": -1, "truncated": False},
        {"items": [], "total": 1, "truncated": False},
        {"items": [1], "total": 1, "truncated": False},
        {"items": [{}], "total": 1, "truncated": False},
    ],
)
def test_parser_checks_envelope(payload):
    assert (
        session.parse_publisher_drift_items(PUBLISHER_DRIFT_ITEMS_PREFIX + json.dumps(payload))
        is None
    )


def test_parser_last_line_sanitises_and_keeps_total():
    assert (
        session.parse_publisher_drift_items(
            diagnostic() + "\n" + PUBLISHER_DRIFT_ITEMS_PREFIX + "{"
        )
        is None
    )
    parsed = session.parse_publisher_drift_items(
        "noise\n" + diagnostic(path="x\n[bad]", key=["x\ny"])
    )
    assert parsed is not None
    assert parsed.labels == ("opencode-runtime:x??bad?[x?y]",)
    assert (parsed.total, parsed.truncated) == (1, False)
    assert session.parse_publisher_drift_items("noise") is None


def test_refresh_labels_seed_and_precedence():
    error = session._publisher_refresh_error
    assert error("workflow publisher: target-drift\n" + diagnostic(), source="codex") == (
        "OpenCode workflow refresh failed: target-drift in opencode-runtime:AGENTS.md. Remedy: "
        + WORKFLOW_REMEDIES["opencode-target-drift"].format(source="codex")
    )
    assert error("workflow publisher: target-drift\n" + diagnostic(origin="seed")) == (
        "OpenCode workflow refresh failed: source-changed in config/opencode/AGENTS.md (seed). "
        "Remedy: Run `djinn config sync`, then retry."
    )
    lock = 'workflow publisher lock-error: {"root":"/config","error":"busy"}'
    write = 'workflow publisher write-error: {"destination":"/runtime","error":"full"}'
    assert error(
        lock + "\n" + write + "\nworkflow publisher: target-drift\n" + diagnostic()
    ) == error(lock)


@pytest.mark.parametrize("check", [False, True])
def test_shared_argv(check):
    target = session.SessionTarget(container_id="fake")
    expected = [
        session.DOCKER_EXECUTABLE,
        "exec",
        "fake",
        "python3",
        "/home/dev/workflow-publisher.py",
        "--view",
        "/home/dev/.opencode/seed",
        "--canonical-root",
        "/home/dev/.djinn-canonical",
        "--target",
        "/home/dev/.config/opencode",
        "--manifest",
        "/home/dev/.config/opencode/.djinn-workflow-state.json",
        "--ignore",
        "opencode.json",
        "--profile",
        "opencode",
    ] + (["--check"] if check else [])
    assert session.opencode_publisher_argv(target, check=check) == expected


@pytest.mark.parametrize(
    ("exit_code", "stderr", "expected"),
    [
        (0, "", "clean; container-local; rebuilt at every djinn start"),
        (11, diagnostic(), "target-drift in opencode-runtime:AGENTS.md"),
        (10, diagnostic(), "source-changed in opencode-runtime:AGENTS.md"),
        (10, diagnostic(origin="seed"), "source-changed in config/opencode/AGENTS.md (seed)"),
        (12, diagnostic(**{"class": "collision"}), "collision in opencode-runtime:AGENTS.md"),
        (
            13,
            diagnostic(**{"class": "invalid-or-semantic"}),
            "invalid-or-semantic in opencode-runtime:AGENTS.md",
        ),
        (1, diagnostic(), "check unavailable"),
        (2, diagnostic(), "check unavailable"),
        (11, "", "check unavailable"),
        (12, PUBLISHER_DRIFT_ITEMS_PREFIX + "{", "check unavailable"),
        (124, "timeout", "check unavailable"),
    ],
)
def test_doctor_running_check(case, monkeypatch, exit_code, stderr, expected):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 0, "true\n", "")
        if exit_code == 124:
            raise subprocess.TimeoutExpired(argv, 10)
        return subprocess.CompletedProcess(argv, exit_code, "", stderr)

    monkeypatch.setattr(doctor.subprocess, "run", run)
    monkeypatch.setattr(
        session,
        "container_workflow_image_compatible",
        lambda container_id: WorkflowImageCompatibility.COMPATIBLE,
    )
    row = doctor._opencode_runtime_check(case[2], daemon=True)
    assert row.detail == expected
    if exit_code == 10:
        assert row.remedy == (
            "Run `djinn config sync`, then retry."
            " Then recreate the container (`djinn clean`, `djinn start`)."
        )
    assert row.status is (doctor.Status.PASS if exit_code == 0 else doctor.Status.WARN)
    assert calls[1][0] == session.opencode_publisher_argv(
        session.SessionTarget("djinn"), check=True
    )
    assert all(
        kwargs["stdin"] == subprocess.DEVNULL
        and kwargs["timeout"] == 10
        and kwargs["check"] is False
        and kwargs["capture_output"] is True
        and kwargs["text"] is True
        for _, kwargs in calls
    )


@pytest.mark.parametrize(
    ("code", "stdout", "stderr", "status", "detail"),
    [
        (
            1,
            "",
            "Error response from daemon: No such container: djinn\n",
            doctor.Status.PASS,
            "container-local; rebuilt at every djinn start",
        ),
        (0, "false\n", "", doctor.Status.PASS, "container-local; rebuilt at every djinn start"),
        (1, "", "Cannot connect to the Docker daemon", doctor.Status.WARN, "check unavailable"),
        (0, "garbage", "", doctor.Status.WARN, "check unavailable"),
    ],
)
def test_doctor_discovery(case, monkeypatch, code, stdout, stderr, status, detail):
    run = Mock(return_value=subprocess.CompletedProcess([], code, stdout, stderr))
    monkeypatch.setattr(doctor.subprocess, "run", run)
    row = doctor._opencode_runtime_check(case[2], daemon=True)
    assert (row.status, row.detail) == (status, detail)
    run.assert_called_once_with(
        [
            session.DOCKER_EXECUTABLE, "container", "inspect", "djinn",
            "--format", "{{json .State.Running}}",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_doctor_image_gate_before_exec(case, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "true", ""))
    monkeypatch.setattr(doctor.subprocess, "run", run)
    monkeypatch.setattr(
        session,
        "container_workflow_image_compatible",
        lambda container_id: WorkflowImageCompatibility.INCOMPATIBLE,
    )
    row = doctor._opencode_runtime_check(case[2], daemon=True)
    assert (row.status, row.detail) == (doctor.Status.WARN, "rebuild and recreate required")
    assert run.call_count == 1


def test_doctor_config_runtime_and_build_problem(case, monkeypatch):
    assert prepare(case).success
    runtime = case[2].config_root / "codex"
    replace_file(runtime / "AGENTS.md", "edited\n")
    monkeypatch.setattr(doctor, "get_project_root", lambda: case[0])
    monkeypatch.setattr(doctor, "audit_workflow_config", config_command.audit_workflow_config)
    row = doctor._config_workflow_check(case[2])
    assert row.status is doctor.Status.WARN
    assert (
        row.detail == "source=claude; Workflow preflight blocked: target-drift in "
        f"codex:AGENTS.md ({runtime})."
    )
    assert row.remedy == WORKFLOW_REMEDIES["target-drift"].format(source="claude")
    (case[0] / "config/claude/AGENTS.md").unlink()
    inspections = workflow.inspect_workflow_targets(case[0], case[2])
    assert inspections[0].problem is not None
    assert inspections[0].problem.message.startswith("not checked (")


@pytest.mark.parametrize(
    ("scenario", "advice_key"),
    [
        ("seed-mismatch", "entrypoint-generic"),
        ("canonical-malformed", "entrypoint-generic"),
        ("canonical-symlink", "entrypoint-generic"),
        ("seed-nonregular", "entrypoint-generic"),
        ("runtime-drift", "entrypoint-target-drift"),
        ("runtime-collision", "entrypoint-collision"),
    ],
)
def test_entrypoint_real_cli_output_and_origin_advice(case, tmp_path, scenario, advice_key):
    project, config_path, _ = case
    assert sync_config(project, config_path=config_path).success
    canonical = project / "config"
    seed = canonical / "opencode"
    target = tmp_path / "opencode-runtime"
    target.mkdir()
    publisher = ROOT / "src/djinn_in_a_box/core/workflow_publisher.py"
    argv = [
        sys.executable,
        str(publisher),
        "--view",
        str(seed),
        "--canonical-root",
        str(canonical),
        "--target",
        str(target),
        "--manifest",
        str(target / RUNTIME_MANIFEST_NAME),
        "--ignore",
        "opencode.json",
        "--profile",
        "opencode",
    ]
    if scenario != "runtime-collision":
        assert subprocess.run(argv, capture_output=True).returncode == 0
    if scenario == "seed-mismatch":
        (seed / "AGENTS.md").write_text("seed edit\n")
    elif scenario.startswith("canonical-"):
        manifest = canonical / ".djinn-config-sync.json"
        if scenario.endswith("symlink"):
            manifest.rename(canonical / "saved.json")
            manifest.symlink_to("saved.json")
        else:
            manifest.write_text("{")
    elif scenario == "seed-nonregular":
        (seed / "AGENTS.md").unlink()
        (seed / "AGENTS.md").symlink_to("missing")
    elif scenario == "runtime-drift":
        replace_file(target / "AGENTS.md", "runtime edit\n")
    else:
        (target / "AGENTS.md").write_text("unmanaged\n")
    raw = subprocess.run(argv, capture_output=True, text=True)
    assert (
        raw.returncode
        == {
            "seed-mismatch": 10,
            "canonical-malformed": 13,
            "canonical-symlink": 12,
            "seed-nonregular": 13,
            "runtime-drift": 11,
            "runtime-collision": 12,
        }[scenario]
    )
    script = (ROOT / "scripts/entrypoint.sh").read_text()
    start = script.index('ui_info "[workflow-delivery] opencode:"')
    end = script.index("# MCP Server Registration", start)
    harness = 'ui_info() { :; }; ui_err() { print -r -- "ADVICE:$1" >&2; }\n'
    harness += f"WORKFLOW_PUBLISHER={shlex.quote(str(publisher))}\n"
    harness += script[start:end]
    result = subprocess.run(
        ["zsh", "-c", harness],
        env={
            **os.environ,
            "DJINN_CANONICAL_ROOT": str(canonical),
            "OPENCODE_WORKFLOW_VIEW": str(seed),
            "OPENCODE_RUNTIME_ROOT": str(target),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
    assert result.returncode == 1
    assert (
        result.stderr
        == raw.stderr.rstrip("\n") + "\nADVICE:" + WORKFLOW_REMEDIES[advice_key] + "\n"
    )


def test_entrypoint_advice_equals_host_table():
    script = (ROOT / "scripts/entrypoint.sh").read_text()
    for variable, key in [
        ("generic", "entrypoint-generic"),
        ("drift", "entrypoint-target-drift"),
        ("collision", "entrypoint-collision"),
    ]:
        match = re.search(r"opencode_advice_" + variable + r"='([^']*)'", script)
        assert match is not None
        assert match[1] == WORKFLOW_REMEDIES[key]


@pytest.mark.parametrize(
    ("code", "origin", "extra", "key"),
    [
        (11, "target", "", "entrypoint-target-drift"),
        (11, "target-manifest", "", "entrypoint-target-drift"),
        (12, "target", "", "entrypoint-collision"),
        (11, "seed", "", "entrypoint-generic"),
        (12, "canonical-manifest", "", "entrypoint-generic"),
        (10, "target", "", "entrypoint-generic"),
        (13, "target", "", "entrypoint-generic"),
        (2, "target", "", "entrypoint-generic"),
        (11, "target", "workflow publisher write-error: {}", "entrypoint-generic"),
        (12, "target", "workflow publisher lock-error: {}", "entrypoint-generic"),
        (11, "target", PUBLISHER_DRIFT_ITEMS_PREFIX + "{", "entrypoint-generic"),
    ],
)
def test_entrypoint_advice_branches(code, origin, extra, key):
    text = (ROOT / "scripts/entrypoint.sh").read_text()
    start = text.index('ui_info "[workflow-delivery] opencode:"')
    end = text.index("# MCP Server Registration", start)
    raw = diagnostic(origin=origin) + ("\n" + extra if extra else "")
    harness = 'ui_info() { :; }; ui_err() { print -r -- "ADVICE:$1" >&2; }\n'
    harness += 'python3() { print -r -- "$DIAGNOSTIC" >&2; return "$PUBLISH_EXIT"; }\n'
    result = subprocess.run(
        ["zsh", "-c", harness + text[start:end]],
        env={**os.environ, "DIAGNOSTIC": raw, "PUBLISH_EXIT": str(code)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stderr == raw + "\nADVICE:" + WORKFLOW_REMEDIES[key] + "\n"


def test_source_build_problem_is_not_checked(case):
    (case[0] / "config/claude/AGENTS.md").unlink()
    result = CliRunner().invoke(app, ["config", "status"])
    assert result.exit_code == 1
    assert "Runtime: not checked (invalid-or-semantic)" in result.output


def test_doctor_reports_canonical_and_runtime_together(case, monkeypatch):
    assert prepare(case).success
    replace_file(case[2].config_root / "codex/AGENTS.md", "runtime drift\n")
    (case[0] / "config/claude/AGENTS.md").write_text("new source\n")
    monkeypatch.setattr(doctor, "get_project_root", lambda: case[0])
    monkeypatch.setattr(doctor, "audit_workflow_config", config_command.audit_workflow_config)
    row = doctor._config_workflow_check(case[2])
    assert row.status is doctor.Status.WARN
    assert (
        "drift=source-changed,target-drift (config/codex/AGENTS.md, config/opencode/AGENTS.md,"
        in row.detail
    )
    assert f"codex:AGENTS.md ({case[2].config_root}/codex)" in row.detail
    assert row.remedy == (
        WORKFLOW_REMEDIES["source-changed"]
        + " "
        + WORKFLOW_REMEDIES["target-drift"].format(source="claude")
    )


def test_delivery_transform_is_shared_by_inspection_and_delivery(case, monkeypatch):
    assert prepare(case, host=True).success
    transform = Mock(wraps=workflow._delivery_view)
    monkeypatch.setattr(workflow, "_delivery_view", transform)
    assert prepare(case, host=True).success
    assert transform.call_count == 2
    assert all(
        call.args[1].destination_root == case[2].code_dir / "host"
        for call in transform.call_args_list
    )
    build_view = transform.call_args.args[2]
    target = transform.call_args.args[1]
    assert workflow._delivery_view(case[2], target, build_view=build_view) is build_view


def test_missing_targets_skip_publisher_inspection(case, monkeypatch):
    inspect = Mock(side_effect=AssertionError("missing target inspected"))
    monkeypatch.setattr(workflow, "inspect_workflow_view", inspect)
    result = workflow.inspect_workflow_targets(case[0], case[2])
    assert len(result) == 1 and result[0].clean
    inspect.assert_not_called()


def test_doctor_unavailable_discovery_is_bounded(case, monkeypatch):
    for failure in [OSError("daemon unavailable"), subprocess.TimeoutExpired("docker", 10)]:
        run = Mock(side_effect=failure)
        monkeypatch.setattr(doctor.subprocess, "run", run)
        row = doctor._opencode_runtime_check(case[2], daemon=True)
        assert (row.status, row.detail) == (doctor.Status.WARN, "check unavailable")
    run = Mock(side_effect=AssertionError("Docker called without configuration/daemon"))
    monkeypatch.setattr(doctor.subprocess, "run", run)
    assert doctor._opencode_runtime_check(None, daemon=True).status is doctor.Status.WARN
    assert doctor._opencode_runtime_check(case[2], daemon=False).status is doctor.Status.WARN
    run.assert_not_called()


def test_refresh_cap_uses_total_and_last_class_line():
    raw = json.loads(diagnostic().removeprefix(PUBLISHER_DRIFT_ITEMS_PREFIX))
    raw["items"] *= 6
    raw["total"], raw["truncated"] = 207, True
    stderr = "workflow publisher: source-changed\nworkflow publisher: target-drift\n"
    stderr += PUBLISHER_DRIFT_ITEMS_PREFIX + json.dumps(raw)
    expected = ", ".join(["opencode-runtime:AGENTS.md"] * 5) + " and 202 more"
    assert session._publisher_refresh_error(stderr) == (
        f"OpenCode workflow refresh failed: target-drift in {expected}. Remedy: "
        + WORKFLOW_REMEDIES["opencode-target-drift"].format(source="claude")
    )


def test_publisher_argv_requires_container():
    with pytest.raises(ValueError, match="^OpenCode container unavailable$"):
        session.opencode_publisher_argv(session.SessionTarget())


def test_exact_shared_remedy_texts():
    assert WORKFLOW_REMEDIES == {
        "source-changed": "Run `djinn config sync`, then retry.",
        "target-drift": (
            "Keep wanted edits in the selected source config/{source}, "
            "stop running Djinn containers "
            "(`djinn clean`), then run `djinn config sync --discard-target-drift` "
            "(it backs up the edited files first)."
        ),
        "opencode-target-drift": (
            "Keep wanted edits in the selected source config/{source}, "
            "then recreate the container: "
            "`djinn clean`, then `djinn start`."
        ),
        "host-target-drift": (
            "Keep wanted edits in the selected source config/{source} and run `djinn config sync`. "
            "Then restore each named item from the copy named next to it, and retry."
        ),
        "collision": "Move or remove the conflicting unmanaged workflow item, then retry.",
        "invalid-or-semantic": CANONICAL_REMEDY,
        "carrier-key": (
            "Rewrite the named managed TOML key as a single-line value, or delete the named key "
            "and retry. Keep neighbouring personal keys."
        ),
        "changed-during-publish": "changed while publishing; retry",
        "entrypoint-generic": (
            "Run `djinn config status` on the host and follow its remedy, then recreate the "
            "container (`djinn clean`, `djinn start`)."
        ),
        "entrypoint-target-drift": (
            "Keep wanted edits in the selected source, then recreate the container "
            "(`djinn clean`, `djinn start`)."
        ),
        "entrypoint-collision": (
            "Move or remove the named item in ~/.config/opencode, or recreate the container."
        ),
        "clean": "",
    }


def test_runtime_drift_alone_makes_status_fail(case):
    assert prepare(case).success
    replace_file(case[2].config_root / "codex/AGENTS.md", "drift\n")
    assert audit_config_sync(case[0], config_path=case[1]).clean
    result = CliRunner().invoke(app, ["config", "status"])
    assert result.exit_code == 1
    assert f"Drift: target-drift (codex:AGENTS.md ({case[2].config_root}/codex))" in (
        " ".join(result.output.split())
    )


def test_status_and_sync_preserve_fragment_brackets(case, monkeypatch):
    from djinn_in_a_box.core.config_sync import ConfigSyncAudit, DriftItem
    from djinn_in_a_box.core.workflow_publisher import PublishResult

    audit = ConfigSyncAudit(
        "claude",
        "claude",
        (DriftItem(DriftClass.TARGET_DRIFT, "", "codex", PurePosixPath("a.toml"), ("error",)),),
    )
    monkeypatch.setattr(config_command, "audit_workflow_config", lambda root: audit)
    target = workflow.WorkflowDeliveryTarget("codex", case[2].config_root / "codex")
    inspected = workflow.WorkflowTargetInspection(
        target,
        PublishResult(
            DriftClass.TARGET_DRIFT,
            drift_items=(DriftEntry(DriftClass.TARGET_DRIFT, PurePosixPath("b.toml"), ("error",)),),
        ),
    )
    monkeypatch.setattr(
        config_command, "inspect_workflow_targets", lambda root, config: (inspected,)
    )
    status = CliRunner().invoke(app, ["config", "status"])
    assert status.exit_code == 1
    assert "config/codex/a.toml[error]" in status.output
    assert f"codex:b.toml[error] ({target.destination_root})" in status.output
    synced = CliRunner().invoke(app, ["config", "sync"])
    assert synced.exit_code == 0
    assert f"codex:b.toml[error] ({target.destination_root})" in synced.output


def test_doctor_caps_combined_canonical_and_runtime_items(case, monkeypatch):
    from djinn_in_a_box.core.config_sync import ConfigSyncAudit, DriftItem
    from djinn_in_a_box.core.workflow_publisher import PublishResult

    audit = ConfigSyncAudit(
        "claude",
        "claude",
        tuple(
            DriftItem(DriftClass.SOURCE_CHANGED, "", "codex", PurePosixPath(f"c{i}.md"))
            for i in range(4)
        ),
    )
    target = workflow.WorkflowDeliveryTarget("codex", case[2].config_root / "codex")
    inspection = workflow.WorkflowTargetInspection(
        target,
        PublishResult(
            DriftClass.TARGET_DRIFT,
            drift_items=tuple(
                DriftEntry(DriftClass.TARGET_DRIFT, PurePosixPath(f"r{i}.md")) for i in range(6)
            ),
        ),
    )
    monkeypatch.setattr(doctor, "get_project_root", lambda: case[0])
    monkeypatch.setattr(doctor, "audit_workflow_config", lambda root: audit)
    monkeypatch.setattr(doctor, "inspect_workflow_targets", lambda root, config: (inspection,))
    row = doctor._config_workflow_check(case[2])
    assert row.detail == (
        "source=claude; drift=source-changed,target-drift ("
        "config/codex/c0.md, config/codex/c1.md, config/codex/c2.md, config/codex/c3.md, "
        f"codex:r0.md ({target.destination_root}) and 5 more)"
    )


def test_carrier_remedy_conditions_and_reason_precedence():
    for path, key in [("a.json", ("x",)), ("a.toml", None)]:
        item = DriftEntry(DriftClass.INVALID_OR_SEMANTIC, PurePosixPath(path), key)
        assert workflow_remedy(item.kind, "claude", items=(item,)) == CANONICAL_REMEDY
    item = DriftEntry(
        DriftClass.INVALID_OR_SEMANTIC,
        PurePosixPath("a.toml"),
        ("x",),
        reason="changed-during-publish",
    )
    assert workflow_remedy(item.kind, "claude", items=(item,)) == "changed while publishing; retry"


def test_success_and_later_publish_refusal_keep_notices(case, monkeypatch):
    result = prepare(case)
    assert result.success
    assert result.notices == (
        "Synchronized source changes into the canonical workflow: 3 changed, 0 removed.",
    )
    (case[0] / "config/claude/AGENTS.md").write_text("changed source\n")
    blocked = workflow.WorkflowPreparationResult(
        False, (workflow.WorkflowPreparationProblem("late", "late refusal", "retry"),)
    )
    monkeypatch.setattr(workflow, "_deliver_target", lambda *args: blocked)
    result = prepare(case)
    assert not result.success
    assert result.notices == (
        "Synchronized source changes into the canonical workflow: 2 changed, 0 removed.",
    )


def test_canonical_publish_failure_keeps_key_and_reason(case, monkeypatch):
    from djinn_in_a_box.core import config_sync
    from djinn_in_a_box.core.workflow_publisher import PublishResult

    item = DriftEntry(
        DriftClass.TARGET_DRIFT,
        PurePosixPath("codex/config.toml"),
        ("key",),
        reason="changed-during-publish",
    )
    monkeypatch.setattr(
        config_sync,
        "publish_workflow_view",
        lambda *args, **kwargs: PublishResult(
            DriftClass.TARGET_DRIFT,
            drift_items=(item,),
        ),
    )
    result = config_sync.sync_config(case[0], config_path=case[1])
    assert not result.success
    assert [(d.tool, d.relative_path, d.key_path, d.reason) for d in result.audit.drifts] == [
        ("codex", PurePosixPath("config.toml"), ("key",), "changed-during-publish"),
    ]
    refusal = workflow._audit_failure(result.audit)
    assert refusal.problems[0].message == (
        "Workflow preflight blocked: target-drift in config/codex/config.toml[key]."
    )
    assert refusal.problems[0].remedy == "changed while publishing; retry"


def test_all_canonical_problems_named_with_cap():
    from djinn_in_a_box.core.config_sync import ConfigSyncAudit, SyncProblem

    problems = tuple(
        SyncProblem("invalid", "Invalid artifact", "codex", PurePosixPath(f"a{i}"))
        for i in range(7)
    )
    result = workflow._audit_failure(ConfigSyncAudit("claude", None, problems=problems))
    assert result.problems[0].message == (
        "Invalid artifact (config/codex/a0, config/codex/a1, config/codex/a2, config/codex/a3, "
        "config/codex/a4 and 2 more)"
    )
    assert result.problems[0].remedy == CANONICAL_REMEDY
    single = workflow._sync_problem_failure(problems[0])
    assert single.problems[0].message == "Invalid artifact (config/codex/a0)"


def test_doctor_canonical_carrier_uses_single_line_remedy(case, monkeypatch):
    from djinn_in_a_box.core.config_sync import ConfigSyncAudit, DriftItem

    audit = ConfigSyncAudit(
        "claude",
        "claude",
        (
            DriftItem(
                DriftClass.INVALID_OR_SEMANTIC, "", "codex", PurePosixPath("a.toml"), ("key",)
            ),
        ),
    )
    monkeypatch.setattr(doctor, "get_project_root", lambda: case[0])
    monkeypatch.setattr(doctor, "audit_workflow_config", lambda root: audit)
    monkeypatch.setattr(doctor, "inspect_workflow_targets", lambda root, config: ())
    row = doctor._config_workflow_check(case[2])
    assert row.remedy == WORKFLOW_REMEDIES["carrier-key"]
    assert row.detail == "source=claude; drift=invalid-or-semantic (config/codex/a.toml[key])"


def test_runtime_inspection_build_and_view_failures(case, monkeypatch):
    from djinn_in_a_box.core.config_sync import SyncProblem
    from djinn_in_a_box.core.workflow_publisher import PublishError, PublishResult

    assert prepare(case).success
    original_build = workflow._snapshot_build
    built = original_build(case[0], "claude")
    from dataclasses import replace

    monkeypatch.setattr(
        workflow,
        "_snapshot_build",
        lambda *args: replace(
            built,
            problems=(
                SyncProblem("invalid", "Invalid artifact", "codex", PurePosixPath("a.toml")),
            ),
        ),
    )
    inspection = workflow.inspect_workflow_targets(case[0], case[2])[0]
    assert inspection.problem is not None
    assert (
        inspection.problem.message
        == "not checked (invalid: Invalid artifact (config/codex/a.toml))"
    )
    monkeypatch.setattr(workflow, "_snapshot_build", original_build)
    monkeypatch.setattr(workflow, "_delivery_view", lambda *args: None)
    inspection = workflow.inspect_workflow_targets(case[0], case[2])[0]
    assert inspection.problem is not None and inspection.problem.identifier == "invalid-or-semantic"
    monkeypatch.setattr(
        workflow, "_snapshot_build", Mock(side_effect=PublishError(DriftClass.COLLISION))
    )
    inspection = workflow.inspect_workflow_targets(case[0], case[2])[0]
    assert inspection.problem is not None and inspection.problem.identifier == "collision"
    target = workflow.WorkflowDeliveryTarget("codex", case[2].config_root / "codex")
    inspection = workflow.WorkflowTargetInspection(
        target,
        PublishResult(
            DriftClass.INVALID_OR_SEMANTIC,
            lock_error=OSError("busy"),
        ),
    )
    assert (
        workflow.inspection_failure(inspection, case[2]).problems[0].identifier
        == "workflow-lock-failed"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("class", []),
        ("origin", "other"),
        ("root", 1),
        ("path", []),
        ("key", 1),
        ("key", [1]),
        ("reason", False),
    ],
)
def test_entrypoint_malformed_schema_uses_generic_advice(field, value):
    script = (ROOT / "scripts/entrypoint.sh").read_text()
    start = script.index('ui_info "[workflow-delivery] opencode:"')
    end = script.index("# MCP Server Registration", start)
    raw = diagnostic(**{field: value})
    harness = 'ui_info() { :; }; ui_err() { print -r -- "ADVICE:$1" >&2; }\n'
    harness += 'python3() { print -r -- "$DIAGNOSTIC" >&2; return 11; }\n'
    result = subprocess.run(
        ["zsh", "-c", harness + script[start:end]],
        env={**os.environ, "DIAGNOSTIC": raw},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stderr == raw + "\nADVICE:" + WORKFLOW_REMEDIES["entrypoint-generic"] + "\n"


def test_host_item_actions_are_capped(case):
    from djinn_in_a_box.core.workflow_publisher import PublishedFile, PublishResult, WorkflowView

    files = tuple(PublishedFile(PurePosixPath(f"f{i}"), b"wanted") for i in range(6))
    view = WorkflowView("claude", files, target_tool="codex")
    target = workflow.WorkflowDeliveryTarget("codex", case[2].code_dir / "host")
    result = PublishResult(
        DriftClass.TARGET_DRIFT,
        drift_items=tuple(DriftEntry(DriftClass.TARGET_DRIFT, f.relative_path) for f in files),
    )
    message = workflow._publish_failure(result, case[2], target, view).problems[0].message
    assert message.count("restore from") == 5
    assert "and 1 more" in message
    assert "f5" not in message


def test_canonical_status_caps_items_and_problems(case, monkeypatch):
    from djinn_in_a_box.core.config_sync import ConfigSyncAudit, DriftItem, SyncProblem

    audit = ConfigSyncAudit(
        "claude",
        "claude",
        tuple(
            DriftItem(DriftClass.TARGET_DRIFT, "", "codex", PurePosixPath(f"d{i}"))
            for i in range(7)
        ),
        tuple(SyncProblem("invalid", "", "opencode", PurePosixPath(f"p{i}")) for i in range(7)),
    )
    monkeypatch.setattr(config_command, "audit_workflow_config", lambda root: audit)
    monkeypatch.setattr(config_command, "inspect_workflow_targets", lambda root, config: ())
    result = CliRunner().invoke(app, ["config", "status"])
    assert result.exit_code == 1
    assert result.output.count("Drift:") == 5
    assert result.output.count("Problem:") == 5
    assert result.output.count("and 2 more") == 2
    assert "config/codex/d5" not in result.output
    assert "config/opencode/p5" not in result.output


def test_remedy_source_sanitised():
    assert workflow_remedy(DriftClass.TARGET_DRIFT, "co\ndex[error]", scope="opencode") == (
        "Keep wanted edits in the selected source config/co?dex?error?, "
        "then recreate the container: "
        "`djinn clean`, then `djinn start`."
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"items": {}, "total": 1, "truncated": False},
        {"items": [], "total": "1", "truncated": False},
        {"items": [], "total": 0, "truncated": "false"},
        {"items": [], "total": 0, "truncated": False},
        {"items": [], "total": -1, "truncated": False},
        {"items": [], "total": 1, "truncated": False},
    ],
)
def test_entrypoint_bad_envelope_uses_generic_advice(payload):
    text = (ROOT / "scripts/entrypoint.sh").read_text()
    start = text.index('ui_info "[workflow-delivery] opencode:"')
    end = text.index("# MCP Server Registration", start)
    raw = PUBLISHER_DRIFT_ITEMS_PREFIX + json.dumps(payload)
    harness = 'ui_info() { :; }; ui_err() { print -r -- "ADVICE:$1" >&2; }\n'
    harness += 'python3() { print -r -- "$DIAGNOSTIC" >&2; return 11; }\n'
    result = subprocess.run(
        ["zsh", "-c", harness + text[start:end]],
        env={**os.environ, "DIAGNOSTIC": raw},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stderr == raw + "\nADVICE:" + WORKFLOW_REMEDIES["entrypoint-generic"] + "\n"


def test_entrypoint_success_preserves_output_without_advice():
    text = (ROOT / "scripts/entrypoint.sh").read_text()
    start = text.index('ui_info "[workflow-delivery] opencode:"')
    end = text.index("# MCP Server Registration", start)
    harness = 'ui_info() { :; }; ui_err() { print -r -- "ADVICE:$1" >&2; }\n'
    harness += 'python3() { print -r -- "publisher success"; return 0; }\n'
    result = subprocess.run(
        ["zsh", "-c", harness + text[start:end]],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stderr == "publisher success\n"
