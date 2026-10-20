"""The passive trailing summary of `djinn hostctl status`."""

from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.commands.hostctl import summary_lines
from djinn_in_a_box.core import host_runtime, hostctl
from djinn_in_a_box.core import host_sealing as sealing

NOW = datetime(2030, 1, 1, 0, 0, tzinfo=UTC)
G1 = "g1"
OPEN_RELAY = "open (:1080; declared peers, TCP 22 only)"
PEERS = "host-a, host-b (2; frozen for this generation)"
LOGIN = "open the login link above in your own browser"


def base(**override):
    """Common fixture: a coherent open window with 8 minutes left."""
    value = {
        "state": "open",
        "helper": "helper-id",
        "generation": G1,
        "running": True,
        "window": {
            "generation": G1,
            "deadline": "2030-01-01T00:08:00Z",
            "boottime_deadline_ns": 1,
            "closed": False,
            "admission": True,
            "paused": False,
        },
        "node": {"BackendState": "Running"},
        "relay": OPEN_RELAY,
        "trust": {"generation": G1, "peers": {"host-a": {}, "host-b": {}}},
        "observation": {"generation": G1, "time": 0},
        "observation_gap": False,
        "sealing": "sealed",
        "sealing_causes": (),
        "sealing_errors": (),
    }
    value.update(override)
    return value


def absent(**override):
    value = {
        "state": "closed",
        "helper": "absent",
        "node": "unknown (helper is off)",
        "trust": {"generation": "old", "peers": {"host-a": {}}},
        "sealing": "sealed",
        "sealing_causes": (),
        "sealing_errors": (),
        "relay": "closed",
    }
    value.update(override)
    return value


def stopped(reason="off", **override):
    value = base(
        state="closed",
        running=False,
        relay="closed",
        node="unknown (helper is off)",
        window={**base()["window"], "closed": True, "reason": reason},
    )
    del value["observation"], value["observation_gap"]
    value.update(override)
    return value


def opening(**override):
    return base(state="opening", relay="closed pending readiness", **override)


NEEDS_LOGIN = {"BackendState": "NeedsLogin", "AuthURL": "https://example.invalid/login"}
UNKNOWN_CAPS = absent(
    sealing="unknown",
    sealing_errors=tuple(f"unknown bind {n}" for n in ("one", "two", "three", "four")),
    sealing_causes=tuple(f"cause bind {n}" for n in ("one", "two", "three", "four", "five")),
)

MATRIX = {
    "absent": (
        absent(),
        (
            "Window: closed (helper absent)",
            "Sealing: sealed",
            "Relay: closed; peers: unknown",
            "Next: djinn hostctl on",
        ),
    ),
    "stopped": (
        stopped(),
        (
            "Window: closed (off)",
            "Sealing: sealed",
            f"Relay: closed; peers: {PEERS}",
            "Next: djinn hostctl on",
        ),
    ),
    "opening": (
        opening(),
        (
            "Window: opening; about 8 min left",
            "Sealing: sealed",
            f"Relay: pending; peers: {PEERS}",
            "Next: wait and rerun djinn hostctl status",
        ),
    ),
    "needs-login": (
        opening(node=NEEDS_LOGIN),
        (
            "Window: opening; about 8 min left",
            "Sealing: sealed",
            f"Relay: pending; peers: {PEERS}",
            f"Next: {LOGIN}",
        ),
    ),
    "open": (
        base(),
        (
            "Window: open; about 8 min left",
            "Sealing: sealed",
            f"Relay: open; peers: {PEERS}",
            "Next: djinn hostctl check, then agents can use ssh <alias>",
        ),
    ),
    "paused": (
        opening(window={**base()["window"], "paused": True}),
        (
            "Window: paused; about 8 min left",
            "Sealing: sealed",
            f"Relay: paused; peers: {PEERS}",
            "Next: wait for the dev start",
        ),
    ),
    "conflict": (
        base(window={**base()["window"], "paused": True}),
        (
            "Window: unknown; about 8 min left; admission unknown",
            "Sealing: sealed",
            f"Relay: unknown; peers: {PEERS}",
            "Next: djinn doctor",
        ),
    ),
    "closed-unknown-caps": (
        UNKNOWN_CAPS,
        (
            "Window: closed (helper absent)",
            "Sealing: unknown (4 unknown groups, 5 cause groups)",
            "  Unknown: unknown bind one",
            "  Unknown: unknown bind two",
            "  Unknown: unknown bind three",
            "  Unknown: +1 more",
            "  Cause: cause bind one",
            "  Cause: cause bind two",
            "  Cause: cause bind three",
            "  Cause: +2 more",
            "Relay: closed; peers: unknown",
            "Next: djinn doctor first",
        ),
    ),
}


@pytest.mark.parametrize("case", sorted(MATRIX))
def test_summary_state_matrix(case):
    value, expected = MATRIX[case]
    assert summary_lines(value, now=NOW) == expected


def window_with(deadline, **extra):
    return base(window={**base()["window"], "deadline": deadline, **extra})


@pytest.mark.parametrize(
    "deadline,text",
    [
        ("2030-01-01T00:08:00Z", "about 8 min left"),
        ("2030-01-01T00:08:59.123456789Z", "about 8 min left"),
        ("2030-01-01T02:08:00+02:00", "about 8 min left"),  # offset normalized to UTC
        ("2029-12-31T19:08:00-05:00", "about 8 min left"),
        ("2030-01-01T00:00:30Z", "less than 1 min left"),
        ("2030-01-01T00:00:00Z", "deadline passed"),
        ("2029-12-31T23:00:00Z", "deadline passed"),
        ("2030-01-01T00:08:00", "time left unknown"),  # naive
        ("not a time", "time left unknown"),
        (None, "time left unknown"),
    ],
)
def test_summary_remaining_time(deadline, text):
    value = window_with(deadline) if deadline else base(window={"generation": G1})
    line = summary_lines(value, now=NOW)[0]
    assert line == f"Window: open; {text}"
    # The boot-clock fields never matter; the output is a pure function of the dict.
    value["window"]["boottime_deadline_ns"] = 999
    value["window"]["boot_id"] = "other"
    assert summary_lines(value, now=NOW)[0] == line


def test_summary_remaining_time_omitted_when_helper_not_running():
    for value in (absent(), stopped()):
        assert " left" not in summary_lines(value, now=NOW)[0]
        assert "deadline" not in summary_lines(value, now=NOW)[0]
    assert summary_lines(base(window_error="gone"), now=NOW)[0] == (
        "Window: open; time left unknown; window state unavailable"
    )


def last(lines):
    assert lines[-1].startswith("Next: ")
    assert sum(line.startswith("Next:") for line in lines) == 1
    return lines[-1][len("Next: ") :]


def snapshot_fixture(name, sealing_state="sealed"):
    value = {
        "needs-login": opening(node=NEEDS_LOGIN),
        "open": base(),
        "opening": opening(),
        "paused": opening(window={**base()["window"], "paused": True}),
        "closed": absent(),
    }[name]
    value["sealing"] = sealing_state
    value["sealing_causes"] = ("cause",) if sealing_state == "unsealed" else ()
    value["sealing_errors"] = ("unknown",) if sealing_state == "unknown" else ()
    return value


DOCTOR = "djinn doctor"
ON_UNSEALED = "djinn hostctl on --allow-unsealed (only if you accept the unsealed boundary)"
CHECK = "djinn hostctl check, then agents can use ssh <alias>"
WAIT = "wait and rerun djinn hostctl status"


@pytest.mark.parametrize(
    "build,expected",
    [
        # (1) incomplete state beats everything, including the login action.
        (lambda: opening(node=NEEDS_LOGIN, relay_error="boom"), DOCTOR),
        (lambda: opening(node=NEEDS_LOGIN, window_error="boom"), DOCTOR),
        (lambda: opening(node=NEEDS_LOGIN, observation_gap=True), DOCTOR),
        (lambda: opening(node={"BackendState": "NeedsLogin"}), DOCTOR),  # no link printed
        # (2) login beats paused, opening and open.
        (lambda: opening(node=NEEDS_LOGIN, window={**base()["window"], "paused": True}), LOGIN),
        (lambda: opening(node=NEEDS_LOGIN), LOGIN),
        # (3) paused beats opening.
        (lambda: opening(window={**base()["window"], "paused": True}), "wait for the dev start"),
        # (4) opening beats the sealing action.
        (lambda: {**opening(), **{"sealing": "unsealed", "sealing_causes": ("c",)}}, WAIT),
        # (5) open ignores sealing.
        (lambda: {**base(), "sealing": "unsealed", "sealing_causes": ("c",)}, CHECK),
        (lambda: {**base(), "sealing": "unknown", "sealing_errors": ("u",)}, CHECK),
        (lambda: {**base(), "sealing": "deferred"}, CHECK),
        # (6) closed follows sealing.
        (lambda: snapshot_fixture("closed", "unsealed"), ON_UNSEALED),
        (lambda: snapshot_fixture("closed", "unknown"), "djinn doctor first"),
        (lambda: snapshot_fixture("closed", "deferred"), "djinn hostctl on"),
        (lambda: snapshot_fixture("closed", "sealed"), "djinn hostctl on"),
        # An unexplained unknown window is incomplete.
        (lambda: base(state="surprise"), DOCTOR),
        # Right after `on`: a missing observation and a node error still count as starting.
        (lambda: _without(opening(), "observation", "observation_gap"), WAIT),
        (lambda: _without(opening(node_error="not up yet"), "node"), WAIT),
        (
            lambda: _without(opening(node_error="not up yet"), "observation", "observation_gap"),
            WAIT,
        ),
        (lambda: _without(opening(), "node"), WAIT),
        # A present but stale observation, or one reporting an error, is not "starting".
        (lambda: opening(observation_gap=True), DOCTOR),
        (lambda: opening(observation={"generation": G1, "error": "boom"}), DOCTOR),
        # The exception ends with the opening state.
        (lambda: _without(base(), "observation", "observation_gap"), DOCTOR),
        (lambda: _without(base(node_error="x"), "node"), DOCTOR),
        (lambda: base(trust=None), DOCTOR),
        (lambda: base(trust={"generation": "old", "peers": {"host-a": {}}}), DOCTOR),
    ],
)
def test_summary_next_precedence(build, expected):
    # Mutation S1: swap two adjacent Next rules; S9: drop the opening exception.
    assert last(summary_lines(build(), now=NOW)) == expected


@pytest.mark.parametrize(
    "value",
    [base(generation=None), base(generation=""), base(running="yes"), base(running=None)],
)
def test_summary_unidentifiable_helper_is_unknown(value):
    # Mutation S15: without the helper-identity override the window word stays derived.
    lines = summary_lines(value, now=NOW)
    assert lines[0].startswith("Window: unknown")
    assert "(" not in lines[0].split(";")[0]
    assert last(lines) == DOCTOR


@pytest.mark.parametrize(
    "value",
    [
        opening(relay_error="boom"),
        base(relay_error="boom"),
        stopped(relay_error="boom"),
        absent(relay_error="boom"),
    ],
)
def test_summary_relay_error_is_unknown_relay_whatever_the_text(value):
    # Mutation S13: a relay_error key alone makes the relay unknown, whatever the text says.
    lines = summary_lines(value, now=NOW)
    assert lines[-2].startswith("Relay: unknown")
    assert last(lines) == DOCTOR


def _without(value, *keys):
    for key in keys:
        value.pop(key, None)
    return value


def test_summary_opening_keeps_qualifiers_while_waiting():
    value = _without(opening(node_error="not up yet"), "observation", "observation_gap")
    lines = summary_lines(value, now=NOW)
    assert lines[0] == "Window: opening; about 8 min left; node state unknown; observation gap"
    assert last(lines) == WAIT


@pytest.mark.parametrize("unknown,cause", [(0, 0), (1, 1), (3, 3), (4, 3), (3, 5), (5, 4)])
def test_summary_unknowns_first_and_caps(unknown, cause):
    # Mutation S4: causes first; S5: cap [:3] -> [:4] or [:2].
    errors = tuple(f"unknown-{n}" for n in range(unknown))
    causes = tuple(f"cause-{n}" for n in range(cause))
    state = "unknown" if errors else "unsealed" if causes else "sealed"
    lines = summary_lines(
        absent(sealing=state, sealing_errors=errors, sealing_causes=causes), now=NOW
    )
    assert lines[1].startswith(f"Sealing: {state}")
    counts = []
    if errors:
        counts.append(f"{unknown} unknown group" + ("" if unknown == 1 else "s"))
    if causes:
        counts.append(f"{cause} cause group" + ("" if cause == 1 else "s"))
    assert lines[1] == f"Sealing: {state}" + (f" ({', '.join(counts)})" if counts else "")
    indented = [line for line in lines if line.startswith("  ")]
    expected = [f"  Unknown: {e}" for e in errors[:3]]
    if unknown > 3:
        expected.append(f"  Unknown: +{unknown - 3} more")
    expected += [f"  Cause: {c}" for c in causes[:3]]
    if cause > 3:
        expected.append(f"  Cause: +{cause - 3} more")
    assert indented == expected
    kinds = [line.split(":")[0].strip() for line in indented]
    assert kinds == sorted(kinds, key=("Unknown", "Cause").index)  # unknowns before causes
    assert sum("more" in line for line in indented) == (unknown > 3) + (cause > 3)


@pytest.mark.parametrize(
    "state,errors,causes,line",
    [
        ("sealed", (), (), "Sealing: sealed"),
        ("deferred", (), (), "Sealing: deferred (no dev to assess)"),
        ("unsealed", (), ("c1",), "Sealing: unsealed (1 cause group)"),
        ("unsealed", (), ("c1", "c2"), "Sealing: unsealed (2 cause groups)"),
        ("unknown", ("u1",), (), "Sealing: unknown (1 unknown group)"),
        ("unknown", ("u1",), ("c1", "c2"), "Sealing: unknown (1 unknown group, 2 cause groups)"),
        ("unknown", (), (), "Sealing: unknown"),
        ("surprise", (), (), "Sealing: unknown"),
        (None, (), (), "Sealing: unknown"),
    ],
)
def test_summary_sealing_line(state, errors, causes, line):
    value = absent(sealing=state, sealing_errors=errors, sealing_causes=causes)
    assert summary_lines(value, now=NOW)[1] == line


def test_summary_only_current_generation_peers():
    # Mutation S8: drop the trust.generation == snapshot.generation check.
    routes = {"routes": {"a": "1", "b": "1", "c": "1"}}
    current = {"generation": G1, "peers": {"host-a": {}}, **routes}
    assert summary_lines(base(trust=current), now=NOW)[-2] == (
        "Relay: open; peers: host-a (1; frozen for this generation)"
    )
    for stale in (
        {"generation": "old", "peers": {"stale-host": {}}},
        {"generation": "", "peers": {"stale-host": {}}},
        {"peers": {"stale-host": {}}},
        {"generation": G1, "peers": {}},
        None,
    ):
        lines = summary_lines(stopped(trust=stale), now=NOW)
        assert lines[-2] == "Relay: closed; peers: unknown"
        assert "stale-host" not in "".join(lines)
    # An empty generation never matches an empty trust generation.
    empty = absent(trust={"generation": "", "peers": {"a": {}}})
    assert "peers: unknown" in summary_lines(empty, now=NOW)[-2]


def test_summary_is_pure_and_literal():
    value = stopped(reason="[bold]off[/bold]\x1b[31m", sealing="unknown")
    value["sealing_errors"] = ("[red]x[/red]",)
    value["trust"]["peers"] = {"[b]host[/b]": {}}
    frozen = copy.deepcopy(value)
    lines = summary_lines(value, now=NOW)
    assert value == frozen  # nothing is mutated
    assert lines[0] == "Window: closed ([bold]off[/bold]\\x1b[31m)"
    assert "  Unknown: [red]x[/red]" in lines
    assert "[b]host[/b]" in lines[-2]
    assert summary_lines(value, now=NOW) == lines


# ---- the command ------------------------------------------------------------------------


def detail_snapshot(**override):
    value = absent(
        sealing="unknown",
        sealing_errors=("grouped unknown",),
        sealing_error_details=("detail one", "detail two"),
        sealing_causes=(),
        sealing_cause_details=(),
    )
    value.update(override)
    return value


def run_status(monkeypatch, value):
    calls = []

    def fake():
        calls.append(1)
        return value

    monkeypatch.setattr(hostctl, "snapshot", fake)
    result = CliRunner().invoke(app, ["hostctl", "status"])
    return result, calls


def test_status_appends_summary_preserving_details(monkeypatch):
    result, calls = run_status(monkeypatch, detail_snapshot())
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[:6] == [
        "Window: closed; helper: absent",
        "Node: unknown (helper is off)",
        "Observation: unavailable; journal/log gaps are unknown.",
        "Sealing: unknown; relay: closed",
        "grouped unknown",
        "djinn doctor lists each item.",
    ]
    assert lines[6:] == [
        "",
        "Summary",
        "Window: closed (helper absent)",
        "Sealing: unknown (1 unknown group)",
        "  Unknown: grouped unknown",
        "Relay: closed; peers: unknown",
        "Next: djinn doctor first",
    ]
    assert result.output.count("djinn doctor lists each item.") == 1
    assert calls == [1]  # no extra snapshot or probe


def test_status_without_doctor_hint_still_appends_summary(monkeypatch):
    value = detail_snapshot(
        sealing="sealed",
        sealing_errors=(),
        sealing_error_details=(),
    )
    result, _ = run_status(monkeypatch, value)
    assert "djinn doctor lists each item." not in result.output
    assert result.output.splitlines()[-6:-4] == ["", "Summary"]


def test_status_snapshot_failure_has_no_summary(monkeypatch):
    def boom():
        raise hostctl.HostctlError("helper ownership is unknown")

    monkeypatch.setattr(hostctl, "snapshot", boom)
    result = CliRunner().invoke(app, ["hostctl", "status"])
    assert result.exit_code == 1
    assert "helper ownership is unknown" in result.output
    assert "Summary" not in result.output


def test_status_prints_markup_looking_text_literally(monkeypatch):
    value = stopped(reason="[bold]off[/bold]")
    value["window"]["deadline"] = "2030-01-01T00:08:00Z"
    value["sealing_errors"] = ()
    result, _ = run_status(monkeypatch, value)
    assert result.exit_code == 0, result.output
    assert "Window: closed ([bold]off[/bold])" in result.output


# ---- the decision table against the real producer -----------------------------------------


@pytest.fixture
def real(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = hostctl.state_root()
    monkeypatch.setattr(sealing, "inspect_assessment", lambda: sealing.Assessment("dev-a"))
    state = {"helper": None, "window": None, "live": None, "node": {"BackendState": "Running"}}
    monkeypatch.setattr(hostctl, "inspect_helper", lambda: state["helper"])
    monkeypatch.setattr(hostctl, "read_window", lambda helper: state["window"])
    monkeypatch.setattr(hostctl, "node_status", lambda helper: state["node"])
    monkeypatch.setattr(hostctl, "command", lambda *args: json.dumps(state["live"]))
    monkeypatch.setattr(host_runtime, "process_token", lambda pid: "token")
    state["root"] = root

    def helper(gen=G1, running=True):
        return {
            "Id": "helper-id",
            "State": {"Running": running},
            "Config": {"Labels": {hostctl.GENERATION_LABEL: gen}},
        }

    def window(**extra):
        return {
            "generation": G1,
            "deadline": "2030-01-01T00:08:00Z",
            "boottime_deadline_ns": 1,
            "closed": False,
            "admission": True,
            "paused": False,
            **extra,
        }

    def observe():
        (root / "observation.json").write_text(
            json.dumps(
                {
                    "generation": G1,
                    "time": time.time(),
                    "observer_pid": 1,
                    "observer_token": "token",
                }
            )
        )

    def trust():
        peers = {"host-a": {}, "host-b": {}}
        (root / "trust.json").write_text(json.dumps({"generation": G1, "peers": peers}))

    state.update(make_helper=helper, make_window=window, observe=observe, trust=trust)
    return state


def test_summary_contract_with_real_snapshot(real):
    # The key/prefix contract of the decision table, against the real hostctl.snapshot().
    rows = {}

    value = hostctl.snapshot()
    rows["absent"] = summary_lines(value, now=NOW)

    real["helper"], real["window"] = real["make_helper"](), real["make_window"]()
    real["trust"]()
    real["live"] = {"generation": G1, "admission": False, "paused": False}
    rows["opening-no-observation"] = summary_lines(hostctl.snapshot(), now=NOW)
    real["observe"]()
    rows["opening"] = summary_lines(hostctl.snapshot(), now=NOW)

    real["live"] = {"generation": G1, "admission": True, "paused": False}
    rows["open"] = summary_lines(hostctl.snapshot(), now=NOW)

    real["live"] = {"generation": G1, "admission": True, "paused": True}
    real["window"] = real["make_window"](paused=True)
    rows["paused"] = summary_lines(hostctl.snapshot(), now=NOW)

    real["helper"] = real["make_helper"](running=False)
    real["window"] = real["make_window"](closed=True, reason="off")
    rows["stopped"] = summary_lines(hostctl.snapshot(), now=NOW)

    peers = "Relay: {}; peers: " + PEERS
    assert rows["absent"] == (
        "Window: closed (helper absent)",
        "Sealing: sealed",
        "Relay: closed; peers: unknown",
        "Next: djinn hostctl on",
    )
    # Right after `on` there is no observation yet: still starting, so wait.
    assert rows["opening-no-observation"][0] == (
        "Window: opening; about 8 min left; observation gap"
    )
    assert rows["opening-no-observation"][-1] == f"Next: {WAIT}"
    assert rows["opening"] == (
        "Window: opening; about 8 min left",
        "Sealing: sealed",
        peers.format("pending"),
        f"Next: {WAIT}",
    )
    assert rows["open"] == (
        "Window: open; about 8 min left",
        "Sealing: sealed",
        peers.format("open"),
        f"Next: {CHECK}",
    )
    assert rows["paused"] == (
        "Window: paused; about 8 min left",
        "Sealing: sealed",
        peers.format("paused"),
        "Next: wait for the dev start",
    )
    assert rows["stopped"] == (
        "Window: closed (off)",
        "Sealing: sealed",
        peers.format("closed"),
        "Next: djinn hostctl on",
    )


def test_summary_contract_real_snapshot_errors_are_incomplete(real):
    real["helper"], real["window"] = real["make_helper"](), real["make_window"]()
    real["observe"]()
    real["trust"]()
    real["live"] = {"generation": "other", "admission": True}  # malformed -> relay_error
    value = hostctl.snapshot()
    assert "relay_error" in value
    lines = summary_lines(value, now=NOW)
    assert lines[0] == "Window: opening; about 8 min left; admission unknown"
    assert lines[-2].startswith("Relay: unknown") and lines[-1] == "Next: djinn doctor"


@pytest.mark.parametrize(
    ("trust", "admitted", "action"),
    [
        (None, True, "djinn doctor"),
        ("stale", True, "djinn doctor"),
        (None, False, "wait for the dev start"),
    ],
)
def test_summary_admitted_window_needs_current_trust(real, trust, admitted, action):
    # Admission writes trust first: an admitted paused window without it is incomplete,
    # a window paused before admission is not.
    real["helper"] = real["make_helper"]()
    real["window"] = real["make_window"](paused=True, admission=admitted)
    real["live"] = {"generation": G1, "admission": admitted, "paused": True}
    real["observe"]()
    if trust == "stale":
        (real["root"] / "trust.json").write_text(
            json.dumps({"generation": "0" * 32, "peers": {"host-a": {}}})
        )
    lines = summary_lines(hostctl.snapshot(), now=NOW)
    assert lines[-1] == f"Next: {action}"
