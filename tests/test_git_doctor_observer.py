from __future__ import annotations

import json
from pathlib import Path

import pytest

from djinn_in_a_box.core import git_diagnostics as diagnostics
from djinn_in_a_box.core import host_runtime
from djinn_in_a_box.core.ssh_delivery import GitSSHError, read_public_delivery

TIME = "2026-10-21T12:00:00+00:00"
REBINDS = ["2026-10-21T11:59:00+00:00", TIME]
MISMATCH = "key set/lifecycle not confirmed against current declarations"
REMEDY = (
    "See observer.log on the host; recreate dev from the host: "
    "djinn clean, then djinn start."
)


@pytest.fixture
def observer_doctor(git_inputs, monkeypatch):
    root = host_runtime.runtime_root(create=True)
    monkeypatch.setattr(diagnostics, "runtime_root", lambda: root)
    expected = read_public_delivery(git_inputs.git).blobs
    real_process_alive = host_runtime.process_alive
    probes = []

    def run(
        *, live=True, actual=("dev-1", True, "generation-1"), cause=None, rebinds=None,
        export_error=None, private_error=None, loaded=None, private_loaded=None, proc_error=None,
    ):
        state = {
            "observer_pid": 987654321,
            "observer_token": "birth-token",
            "container_name": "djinn",
            "dev_id": "dev-1",
            "generation": "generation-1",
        }
        if cause is not None:
            state["observer_exit"] = {
                "code": cause, "detail": "host-only error detail", "time": TIME,
                "rebinds": REBINDS,
            }
        if rebinds is not None:
            state["filter_rebinds"] = rebinds
        (root / host_runtime.STATE_FILE).write_text(json.dumps(state))

        def process_alive(pid, token):
            assert (pid, token) == (987654321, "birth-token")
            probes.append("liveness")
            if proc_error is not None:
                read_text = Path.read_text

                def read(path, *args, **kwargs):
                    if path == Path(f"/proc/{pid}/stat"):
                        raise proc_error
                    return read_text(path, *args, **kwargs)

                with monkeypatch.context() as scoped:
                    scoped.setattr(Path, "read_text", read)
                    return real_process_alive(pid, token)
            return live

        def inspect_dev(name):
            assert name == "djinn"
            probes.append("dev")
            if isinstance(actual, Exception):
                raise actual
            return actual

        def agent_keys(path):
            if path == root / "export" / "auth.sock":
                probes.append("export")
                assert probes[:2] == ["liveness", "dev"]
                if export_error is not None:
                    raise export_error
                return expected if loaded is None else loaded
            assert path == root / "private" / "agent.sock"
            probes.append("private")
            if private_error is not None:
                raise private_error
            return expected if private_loaded is None else private_loaded

        monkeypatch.setattr(host_runtime, "process_alive", process_alive)
        monkeypatch.setattr(diagnostics, "inspect_dev", inspect_dev)
        monkeypatch.setattr(diagnostics, "agent_keys", agent_keys)
        rows = diagnostics.git_diagnostics(git_inputs)
        agent_rows = [row for row in rows if row.name == "Git agent"]
        assert len(agent_rows) == 1
        return agent_rows[0], probes

    return run


@pytest.mark.parametrize("error", [PermissionError("proc denied"), OSError("proc unreadable")])
def test_unknown_observer_liveness_warns_before_socket_or_dev_probe(observer_doctor, error):
    row, probes = observer_doctor(proc_error=error)
    assert row.status == "warn"
    assert row.detail.startswith("observer liveness unknown: ")
    assert row.detail.removeprefix("observer liveness unknown: ")
    assert probes == ["liveness"]


@pytest.mark.parametrize("cause", [None, *host_runtime.OBSERVER_EXIT_CAUSES, "future-cause"])
def test_dead_attached_observer_fails_with_cause_or_missing_cause(observer_doctor, cause):
    row, probes = observer_doctor(live=False, cause=cause)
    assert row.status == "fail"
    if cause is None:
        assert row.detail == "host observer ended without a recorded cause"
    else:
        text = host_runtime.OBSERVER_EXIT_CAUSES.get(cause, cause)
        assert row.detail == f"host observer ended: {text} ({TIME})"
    assert row.remedy == REMEDY
    assert probes == ["liveness", "dev"]


@pytest.mark.parametrize("cause", [None, "agent-exited"])
@pytest.mark.parametrize(
    "actual",
    [None, ("dev-1", False, "generation-1")],
)
def test_dead_observer_with_no_running_dev_warns_of_reclaim(observer_doctor, actual, cause):
    row, probes = observer_doctor(live=False, actual=actual, cause=cause)
    assert row.status == "warn"
    assert row.detail == (
        "stale runtime state (observer not running, dev not running); the next start reclaims it"
    )
    assert row.remedy == ""
    assert probes == ["liveness", "dev"]


@pytest.mark.parametrize("cause", [None, "dev-replaced", "future-cause"])
@pytest.mark.parametrize(
    "actual",
    [("dev-2", True, "generation-1"), ("dev-1", True, "generation-2"),
     ("dev-2", False, "generation-1"), ("dev-1", False, "generation-2")],
)
def test_dead_observer_with_other_dev_id_or_generation_warns(observer_doctor, actual, cause):
    row, probes = observer_doctor(live=False, actual=actual, cause=cause)
    assert row.status == "warn"
    detail = MISMATCH
    if cause is not None:
        text = host_runtime.OBSERVER_EXIT_CAUSES.get(cause, cause)
        detail += f"; host observer ended: {text} ({TIME})"
    assert row.detail == detail
    assert row.remedy == ""
    assert probes == ["liveness", "dev"]


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("socket missing"), ConnectionRefusedError("socket refused"),
        TimeoutError("socket unresponsive"), EOFError(), GitSSHError("invalid agent reply"),
        ValueError("invalid response"), RuntimeError("probe failed"),
    ],
)
def test_live_attached_observer_with_failed_filter_fails_before_rebind_warning(
    observer_doctor, error,
):
    row, probes = observer_doctor(export_error=error, rebinds=REBINDS)
    assert row.status == "fail"
    reason = str(error) or type(error).__name__
    assert row.detail == f"Git agent filter not serving: {reason}"
    assert row.remedy == REMEDY
    assert probes == ["liveness", "dev", "export"]


@pytest.mark.parametrize("rebinds", [[TIME], REBINDS, [TIME] * 5])
def test_confirmed_delivery_after_rebind_warns(observer_doctor, rebinds):
    row, probes = observer_doctor(rebinds=rebinds)
    assert row.status == "warn"
    assert row.detail == f"filter rebound {len(rebinds)}× (last {rebinds[-1]})"
    assert probes == ["liveness", "dev", "export", "private"]


def test_filter_failure_precedes_private_agent_error(observer_doctor):
    row, probes = observer_doctor(
        export_error=ConnectionRefusedError("socket refused"),
        private_error=EOFError("private agent closed"),
    )
    assert row.status == "fail"
    assert row.detail == "Git agent filter not serving: socket refused"
    assert row.remedy == REMEDY
    assert probes == ["liveness", "dev", "export"]


@pytest.mark.parametrize("rebinds", [None, []])
def test_confirmed_delivery_without_rebinds_keeps_pass(observer_doctor, rebinds):
    row, probes = observer_doctor(rebinds=rebinds, cause="observer-error")
    assert row.status == "pass"
    assert row.detail == "declared key set; observer attached to dev dev-1"
    assert row.remedy == ""
    assert probes == ["liveness", "dev", "export", "private"]


@pytest.mark.parametrize(
    "actual",
    [None, ("dev-1", False, "generation-1"), ("dev-2", True, "generation-1"),
     ("dev-1", True, "generation-2")],
)
def test_live_observer_without_attached_running_dev_keeps_warning(observer_doctor, actual):
    row, probes = observer_doctor(actual=actual, rebinds=REBINDS)
    assert row.status == "warn"
    assert row.detail == MISMATCH
    assert probes == ["liveness", "dev"]


@pytest.mark.parametrize("socket", ["export", "private"])
def test_rebind_warning_requires_both_key_sets_confirmed(observer_doctor, socket):
    kwargs = {"loaded" if socket == "export" else "private_loaded": frozenset()}
    row, probes = observer_doctor(rebinds=REBINDS, **kwargs)
    assert row.status == "warn"
    assert row.detail == MISMATCH
    assert probes == ["liveness", "dev", "export", "private"]


def test_rebind_warning_requires_valid_declarations(observer_doctor, git_inputs):
    git_inputs.git.allowed_signers_file.unlink()
    row, _ = observer_doctor(rebinds=REBINDS)
    assert row.status == "warn"
    assert row.detail == MISMATCH


def test_private_agent_error_stays_unknown_warning(observer_doctor):
    row, probes = observer_doctor(private_error=EOFError("private agent closed"), rebinds=REBINDS)
    assert row.status == "warn"
    assert row.detail == "unknown/uninspected: private agent closed"
    assert probes == ["liveness", "dev", "export", "private"]


def test_dev_inspection_error_stays_unknown_warning(observer_doctor):
    error = GitSSHError(
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?"
    )
    row, probes = observer_doctor(actual=error)
    assert row.status == "warn"
    assert row.detail == f"unknown/uninspected: {error}"
    assert probes == ["liveness", "dev"]


@pytest.mark.parametrize(
    "stat, expected",
    [
        ("123 (observer with ) brackets) " + " ".join(["S"] + ["0"] * 18 + ["token"]), True),
        ("123 (observer) " + " ".join(["Z"] + ["0"] * 18 + ["token"]), False),
        ("123 (observer) " + " ".join(["S"] + ["0"] * 18 + ["other-token"]), False),
        ("123 (observer) S 0", None), ("123 (observer)", None), ("malformed", None),
        (FileNotFoundError(), False), (PermissionError(), None), (OSError(), None),
    ],
)
def test_process_alive_distinguishes_live_gone_and_unknown(monkeypatch, stat, expected):
    def read(path):
        assert path == Path("/proc/123/stat")
        if isinstance(stat, OSError):
            raise stat
        return stat

    monkeypatch.setattr(Path, "read_text", read)
    assert host_runtime.process_alive(123, "token") is expected


def test_shared_cause_texts_are_the_delivery_contract():
    assert host_runtime.OBSERVER_EXIT_CAUSES == {
        "dev-replaced": (
            "dev was replaced or relabelled outside this runtime; its Git delivery ended"
        ),
        "key-set-changed": (
            "the dedicated Git agent's key set changed; Git delivery stopped (fail closed)"
        ),
        "agent-exited": "the dedicated Git ssh-agent exited; Git delivery ended",
        "filter-failed": "the Git agent filter could not be kept serving; Git delivery ended",
        "companion-resume-failed": (
            "the agent Docker companion could not be resumed; "
            "a dev stop was attempted (fail closed)"
        ),
        "observer-error": "the host observer ended on an unexpected error",
    }
