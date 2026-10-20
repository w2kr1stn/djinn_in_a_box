"""Iteration-3 minor regressions: backup path reporting, refresh tail and admission hint."""

from __future__ import annotations

import errno

import test_workflow_discard as discard_tests

from djinn_in_a_box.core import config_workflow as workflow
from djinn_in_a_box.core import session

discard_case = discard_tests.discard_case


def test_backup_directory_is_not_named_when_it_was_never_created(discard_case, monkeypatch):
    case = discard_case
    discard_tests.drift(case)

    def refuse(admission):
        raise OSError(errno.EIO, "synthetic stamp creation failure")

    monkeypatch.setattr(workflow, "open_backup_root", refuse)
    result = case.invoke()

    assert result.exit_code == 1, result.output
    assert "synthetic stamp creation failure" in result.output
    assert "Backed up to" not in result.output
    assert "Backups were kept in" not in result.output
    assert not (case.home / ".local/state/djinn/workflow-drift").exists()


def test_refresh_keeps_the_last_non_empty_stderr_line():
    message = session._publisher_refresh_error(  # pyright: ignore[reportPrivateUsage]
        "Traceback (most recent call last):\nValueError: synthetic failure\n\n\n"
    )

    assert message == "OpenCode workflow refresh failed: ValueError: synthetic failure"


def test_admission_refusal_has_no_unrelated_status_hint(discard_case):
    case = discard_case
    case.config = case.config.model_copy(update={"code_dir": case.home})

    result = case.invoke()

    assert result.exit_code == 1
    assert "overlaps" in result.output
    assert "djinn config status" not in result.output
