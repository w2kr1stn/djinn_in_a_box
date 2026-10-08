"""The suite-wide signal guard: foreign targets are refused, own leaders are not.

Every refusal probe sends signal 0, which only checks existence. If the guard
regresses, the probe still harms nothing; it only stops raising.
"""

import os
import signal
import subprocess

import pytest

ABOVE_PID_MAX = 4194305


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: os.killpg(1, 0), id="killpg-init-group"),
        pytest.param(lambda: os.kill(1, 0), id="kill-init"),
        pytest.param(lambda: os.kill(0, 0), id="kill-own-group-shorthand"),
        pytest.param(lambda: os.kill(-1, 0), id="kill-everything"),
        pytest.param(lambda: os.killpg(os.getpgrp(), 0), id="killpg-own-group"),
        pytest.param(lambda: os.kill(-os.getpgrp(), 0), id="kill-negative-own-group"),
        pytest.param(lambda: os.kill(os.getpid(), 0), id="kill-self"),
        pytest.param(lambda: os.kill(os.getppid(), 0), id="kill-foreign-parent"),
    ],
)
def test_foreign_targets_are_refused(call):
    with pytest.raises(AssertionError, match="foreign process"):
        call()


def test_missing_targets_send_nothing():
    with pytest.raises(ProcessLookupError):
        os.kill(ABOVE_PID_MAX, 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(ABOVE_PID_MAX, 0)


def test_own_leader_and_its_group_may_be_signalled():
    process = subprocess.Popen(
        ["sleep", "30"], stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        os.kill(process.pid, 0)
        os.killpg(process.pid, signal.SIGKILL)
        assert process.wait(timeout=5) == -signal.SIGKILL
        # The reaped leader's group is still this test's own: no refusal, only ESRCH.
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
