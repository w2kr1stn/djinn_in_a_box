"""Check live-test collection without contacting a Docker daemon."""

import runpy
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

_LIVE_MODULE = Path(__file__).with_name("test_zone_overlays_live_docker.py")


@pytest.mark.parametrize(
    ("flag", "reachable", "probe_expected", "skipped"),
    [
        (None, True, False, True),
        ("0", True, False, True),
        ("1", True, True, False),
        ("1", False, True, True),
    ],
    ids=["unset", "disabled", "enabled-reachable", "enabled-unreachable"],
)
def test_live_docker_collection_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
    reachable: bool,
    probe_expected: bool,
    skipped: bool,
) -> None:
    if flag is None:
        monkeypatch.delenv("DJINN_LIVE_DOCKER", raising=False)
    else:
        monkeypatch.setenv("DJINN_LIVE_DOCKER", flag)
    probe = Mock(
        return_value=subprocess.CompletedProcess(["docker", "info"], 0 if reachable else 1)
    )
    monkeypatch.setattr(subprocess, "run", probe)

    # Import definitions and collection marks; never invoke the live test body.
    try:
        module = runpy.run_path(str(_LIVE_MODULE))
    except pytest.skip.Exception:
        module_skipped = True
    else:
        mark = module["pytestmark"]
        assert mark.name == "skipif"
        module_skipped = mark.args[0]
    assert module_skipped == skipped
    if probe_expected:
        probe.assert_called_once_with(
            ["docker", "info"], capture_output=True, check=False, timeout=5
        )
    else:
        probe.assert_not_called()
