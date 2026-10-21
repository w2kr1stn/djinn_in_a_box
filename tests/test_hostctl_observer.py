"""Detached process acceptance with daemon-shaped Docker responses and blocked login."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

import pytest

from djinn_in_a_box.core import host_runtime, hostctl, post_mortem


def wait_for(predicate):
    deadline = time.monotonic() + 6
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("detached observer did not reach expected state")
        time.sleep(0.02)


@pytest.fixture
def observed(tmp_path, monkeypatch):
    state = tmp_path / "helper.json"
    calls = tmp_path / "calls.jsonl"
    state.write_text(
        json.dumps(
            {
                "Id": "helper-id",
                "State": {"Running": True},
                "Config": {"Labels": {hostctl.GENERATION_LABEL: "generation-a"}},
            }
        )
    )
    binary = tmp_path / "docker"
    binary.write_text(f"""#!{sys.executable}
import json, os, pathlib, sys, tempfile, time
state = pathlib.Path({str(state)!r})
calls = pathlib.Path({str(calls)!r})
args = sys.argv[1:]
with calls.open('a') as stream: stream.write(json.dumps(args)+'\\n')
if args[:2] == ['container', 'inspect']:
    if args[2] not in ('helper-id', 'fake-helper') or not state.exists():
        print('Error response from daemon: No such container: '+args[2],file=sys.stderr)
        sys.exit(1)
    print(json.dumps([json.loads(state.read_text())]))
elif args[0] == 'exec' and 'up' in args:
    time.sleep(60)
elif args[0] == 'exec' and 'status' in args:
    print(json.dumps({{'BackendState':'NeedsLogin','AuthURL':'https://example.invalid/login'}}))
elif args[0] == 'stop':
    data=json.loads(state.read_text());data['State']['Running']=False
    fd, temporary = tempfile.mkstemp(dir=state.parent)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(data))
    os.replace(temporary, state)
elif args[0] == 'logs': pass
else: sys.exit(2)
""")
    binary.chmod(0o700)
    monkeypatch.setattr(hostctl, "DOCKER_EXECUTABLE", str(binary))
    monkeypatch.setattr(hostctl, "HELPER_NAME", "fake-helper")
    hostctl.save_private("opening.json", {"generation": "generation-a", "dev_id": None})
    host_runtime.start_hostctl_observer("helper-id", "generation-a", str(binary))
    root = hostctl.state_root()
    wait_for(lambda: (root / "observation.json").exists())
    observation = json.loads((root / "observation.json").read_text())
    try:
        yield state, calls, root, observation
    finally:
        host_runtime.stop_owned_process(observation["observer_pid"], observation["observer_token"])


def test_blocked_enrollment_does_not_hold_lock_or_delay_off(observed):
    _, calls, root, observation = observed
    wait_for(lambda: any("up" in json.loads(line) for line in calls.read_text().splitlines()))
    assert observation["node_state"] == "NeedsLogin"
    assert "AuthURL" not in (root / "observation.json").read_text()
    before = time.monotonic()
    hostctl.close_window()
    assert time.monotonic() - before < 2
    wait_for(lambda: host_runtime.process_token(observation["observer_pid"]) is None)
    up = next(json.loads(line) for line in calls.read_text().splitlines() if '"up"' in line)
    assert "--reset" in up and "--ssh=false" in up and "--accept-routes=false" in up
    assert not any("authkey" in arg or "--advertise-tags" in arg for arg in up)


def test_cancelled_or_replaced_generation_cannot_publish(observed):
    state, _, root, observation = observed
    data = json.loads(state.read_text())
    data["Config"]["Labels"][hostctl.GENERATION_LABEL] = "generation-b"
    fd, temporary = tempfile.mkstemp(dir=state.parent)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(data))
    os.replace(temporary, state)
    sentinel = {"generation": "generation-b", "node_state": "owner-observation"}
    (root / "observation.json").write_text(json.dumps(sentinel))
    wait_for(lambda: host_runtime.process_token(observation["observer_pid"]) is None)
    assert json.loads((root / "observation.json").read_text()) == sentinel
    assert json.loads(state.read_text())["State"]["Running"]


@pytest.mark.parametrize("observation", ["absent", "stopped", "replaced"])
@pytest.mark.parametrize("broken_recorder", [False, True])
def test_hostctl_teardown_after_guard(tmp_path, monkeypatch, capsys, observation, broken_recorder):
    from types import SimpleNamespace

    binary = tmp_path / "docker"
    binary.write_text(f"""#!{sys.executable}
import sys
print('Error response from daemon: No such container: '+sys.argv[-1], file=sys.stderr)
sys.exit(1)
""")
    binary.chmod(0o700)
    monkeypatch.setattr(hostctl, "DOCKER_EXECUTABLE", str(binary))
    monkeypatch.setattr(hostctl, "HELPER_NAME", "helper-id")
    helper = {
        "Id": "helper-id", "State": {"Running": True, "StartedAt": "helper-run"},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "generation-a"}},
    }
    hostctl.save_private("opening.json", {"generation": "generation-a", "dev_id": "expected-dev"})
    monkeypatch.setattr(hostctl, "inspect_helper", lambda **kw: helper)
    monkeypatch.setattr(hostctl, "node_status", lambda *a: {"BackendState": "NeedsLogin"})
    monkeypatch.setattr(hostctl, "reconcile", lambda *a: None)
    monkeypatch.setattr(host_runtime.signal, "signal", lambda *a: None)
    monkeypatch.setattr(host_runtime.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(
        poll=lambda: 1, returncode=1,
    ))
    actual = {"absent": None, "stopped": ("expected-dev", False, ""),
              "replaced": ("replacement-dev", True, "")}[observation]
    monkeypatch.setattr(host_runtime, "inspect_dev", lambda *a: actual)
    events = []
    journal = hostctl.journal
    records_root = tmp_path / "launcher-records"

    def command(*args, **kwargs):
        assert args == ("stop", "-t", "3", "helper-id")
        helper["State"]["Running"] = False
        events.append("stop")
        return ""

    def journaled(event, **kwargs):
        journal(event, **kwargs)
        events.append(event)

    def mark(*args, **kwargs):
        assert events == ["stop", "external-dev-teardown", "relay-closed"]
        assert kwargs == {"blocking": False, "root": records_root}
        assert not getattr(hostctl._held, "active", False)
        with hostctl.control_guard():
            pass
        events.append("marker")

    def record(trigger, **kwargs):
        assert events == ["stop", "external-dev-teardown", "relay-closed", "marker"]
        assert not getattr(hostctl._held, "active", False)
        with hostctl.control_guard():
            pass
        assert trigger == "hostctl-teardown"
        assert kwargs["root"] == records_root
        assert kwargs["hostctl_generation"] == "generation-a"
        assert kwargs["candidates"] == [post_mortem.Candidate("expected-dev", True, "expected-dev")]
        assert kwargs["observed"] == {
            "state": observation, "expected_id": "expected-dev",
            "actual_id": None if actual is None else actual[0],
        }
        events.append("record")
        if broken_recorder:
            raise RuntimeError("recorder unavailable")

    monkeypatch.setattr(hostctl, "command", command)
    monkeypatch.setattr(hostctl, "journal", journaled)
    monkeypatch.setattr(post_mortem, "mark_terminated", mark)
    monkeypatch.setattr(post_mortem, "record", record)
    host_runtime.observe_hostctl("helper-id", "generation-a", str(binary), records_root)
    assert events == ["stop", "external-dev-teardown", "relay-closed", "marker", "record"]
    assert "external-dev-teardown" in (hostctl.state_root() / "journal.jsonl").read_text()
    captured = capsys.readouterr()
    assert "recorder unavailable" not in captured.out + captured.err


def test_hostctl_observer_launcher_and_main_records_root(tmp_path, monkeypatch):
    import runpy
    from unittest.mock import patch

    root = tmp_path / "from-launcher"
    monkeypatch.setattr(hostctl, "DOCKER_EXECUTABLE", "/isolated/docker")
    monkeypatch.setattr(hostctl, "HELPER_NAME", "helper-id")
    monkeypatch.setattr(host_runtime.signal, "signal", lambda *a: None)
    launched = []
    monkeypatch.setattr(post_mortem, "records_root", lambda **kw: root)
    monkeypatch.setattr(host_runtime.subprocess, "Popen",
                        lambda argv, **kw: launched.append((argv, kw)))
    host_runtime.start_hostctl_observer("helper-id", "generation-a", "/isolated/docker")
    argv, kwargs = launched[0]
    assert argv[-5:] == ["--hostctl", "helper-id", "generation-a", "/isolated/docker", str(root)]
    assert kwargs["stdin"] == host_runtime.subprocess.DEVNULL
    assert "-I" in argv
    # Execute __main__ itself, stopping exactly at the initial owned helper inspection.
    monkeypatch.setattr(sys, "argv", ["host_runtime", *argv[-5:]])
    hostctl.save_private("opening.json", {"dev_id": None})
    # A profile hook observes the argv-bound function argument before its body executes.
    received = []

    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "observe_hostctl":
            received.append(frame.f_locals["records_root"])

    sys.setprofile(profile)
    try:
        with (
            patch.object(hostctl, "inspect_helper", return_value=None),
            pytest.warns(RuntimeWarning, match="found in sys.modules"),
        ):
            runpy.run_module("djinn_in_a_box.core.host_runtime", run_name="__main__")
    finally:
        sys.setprofile(None)
    assert received == [root]


@pytest.mark.parametrize("closure", ["relay-refused", "observation-gap"])
def test_hostctl_other_closures_use_launcher_root(tmp_path, monkeypatch, closure):
    from types import SimpleNamespace

    root = tmp_path / "records-from-parent"
    value = {
        "Id": "helper-a", "State": {"Running": True, "StartedAt": "helper-run"},
        "Config": {"Labels": {hostctl.GENERATION_LABEL: "generation-a"}},
    }
    monkeypatch.setattr(hostctl, "HELPER_NAME", "helper-a")
    monkeypatch.setattr(hostctl, "DOCKER_EXECUTABLE", "/isolated/docker")
    monkeypatch.setattr(hostctl, "inspect_helper", lambda **kw: value)
    monkeypatch.setattr(hostctl, "reconcile", lambda *a: None)
    monkeypatch.setattr(host_runtime.signal, "signal", lambda *a: None)
    monkeypatch.setattr(host_runtime.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(
        poll=lambda: 0, returncode=0,
    ))
    hostctl.save_private("opening.json", {"generation": "generation-a", "dev_id": None})
    monkeypatch.setattr(hostctl, "node_status", lambda *a: {"BackendState": "Running"})
    monkeypatch.setattr(hostctl, "prepare_trust", lambda *a: {})

    def failed(*args):
        raise RuntimeError("observation unavailable")

    monkeypatch.setattr(hostctl, "admission_assessment", failed if closure == "relay-refused"
                        else lambda *a: {"dev_id": None, "creator": None, "sealing": "sealed"})
    monkeypatch.setattr(hostctl, "commit_assessment", lambda *a: None)
    monkeypatch.setattr(hostctl, "admit_locked", lambda *a, **kw: None)
    monkeypatch.setattr(host_runtime, "inspect_dev", failed)
    markers = []

    def mark(*args, **kwargs):
        assert not getattr(hostctl._held, "active", False)
        with hostctl.control_guard():
            pass
        markers.append((args, kwargs))

    monkeypatch.setattr(post_mortem, "mark_terminated", mark)

    def stop(*args, **kwargs):
        assert args == ("stop", "-t", "3", "helper-a")
        value["State"]["Running"] = False
        return ""

    monkeypatch.setattr(hostctl, "command", stop)
    host_runtime.observe_hostctl("helper-a", "generation-a", "/isolated/docker", root)
    assert markers == [(("/isolated/docker", "helper-a", "helper-run"),
                        {"blocking": False, "root": root})]
