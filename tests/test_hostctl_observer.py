"""Detached process acceptance with daemon-shaped Docker responses and blocked login."""

from __future__ import annotations

import json
import sys
import time

import pytest

from djinn_in_a_box.core import host_runtime, hostctl


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
import json, pathlib, sys, time
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
    state.write_text(json.dumps(data))
elif args[0] == 'logs': pass
else: sys.exit(2)
""")
    binary.chmod(0o700)
    monkeypatch.setattr(hostctl, "DOCKER_EXECUTABLE", str(binary))
    monkeypatch.setattr(hostctl, "HELPER_NAME", "fake-helper")
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
    state.write_text(json.dumps(data))
    sentinel = {"generation": "generation-b", "node_state": "owner-observation"}
    (root / "observation.json").write_text(json.dumps(sentinel))
    wait_for(lambda: host_runtime.process_token(observation["observer_pid"]) is None)
    assert json.loads((root / "observation.json").read_text()) == sentinel
    assert json.loads(state.read_text())["State"]["Running"]
