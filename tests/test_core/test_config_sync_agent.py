from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import cast

import pytest

from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core import config_sync_agent
from djinn_in_a_box.core.config_sync_adapters import (
    ADAPTER_REVISION,
    AllowedSettingsFragment,
    ArtifactOutputContract,
    UnresolvedItem,
    allowed_outputs_for_unresolved,
)
from djinn_in_a_box.core.config_sync_agent import (
    ITEM_TIMEOUT_SECONDS,
    TOTAL_TIMEOUT_SECONDS,
    SemanticFailure,
    resolve_unresolved_item,
    semantic_artifact_fingerprint,
    start_semantic_deadline,
)
from djinn_in_a_box.core.docker import RunResult


@dataclass
class _Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


@dataclass
class _SequenceClock:
    values: list[float]

    def __call__(self) -> float:
        return self.values.pop(0)


@dataclass
class _Executor:
    result: RunResult
    clock: _Clock | None = None
    advance: float = 0.0
    calls: list[tuple[str, str, bool, bool, int | None]] = field(
        default_factory=lambda: []
    )

    def __call__(
        self,
        agent: str,
        prompt: str,
        *,
        write: bool,
        json_output: bool,
        timeout: int | None,
    ) -> RunResult:
        self.calls.append((agent, prompt, write, json_output, timeout))
        if self.clock is not None:
            self.clock.value += self.advance
        return self.result


class _FailingExecutor:
    def __call__(
        self,
        agent: str,
        prompt: str,
        *,
        write: bool,
        json_output: bool,
        timeout: int | None,
    ) -> RunResult:
        del agent, prompt, write, json_output, timeout
        raise OSError("private workflow body")


def _item(
    target: ConfigSyncSource = "codex",
    *,
    content: bytes = b"source body\n",
    metadata: bytes | None = b"source metadata",
    executable: bool = False,
) -> UnresolvedItem:
    path = PurePosixPath("skills/convergence-loop/SKILL.md")
    return UnresolvedItem(
        "skill:convergence-loop:skills/convergence-loop/SKILL.md",
        "Runtime-specific skill variant needs adaptation.",
        path,
        content,
        metadata,
        target,
        executable,
    )


def _hook_item(target: ConfigSyncSource = "claude") -> UnresolvedItem:
    path = PurePosixPath("plugins/ready-notify.js")
    return UnresolvedItem(
        "hook:ready:plugins/ready-notify.js",
        "Python/JavaScript hook gap needs adaptation.",
        path,
        b"export const Ready = {}\n",
        None,
        target,
    )


def _resolved_payload(
    item: UnresolvedItem,
    *,
    path: str | None = None,
    fragments: list[object] | None = None,
) -> dict[str, object]:
    contract = allowed_outputs_for_unresolved(item)
    target_path = contract.file_paths[0].as_posix() if path is None else path
    return {
        "schema_version": 1,
        "artifact_id": item.identifier,
        "target_tool": item.target_tool,
        "status": "resolved",
        "files": [
            {
                "path": target_path,
                "content_base64": base64.b64encode(b"adapted body\n").decode(),
                "executable": False,
            }
        ],
        "fragments": [] if fragments is None else fragments,
    }


def _envelope(provider: ConfigSyncSource, payload: dict[str, object]) -> str:
    body = json.dumps(payload, separators=(",", ":"))
    if provider == "claude":
        return json.dumps(
            {"type": "result", "is_error": False, "structured_output": payload}
        )
    if provider == "codex":
        return "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "thread"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": body},
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {}}),
            )
        )
    midpoint = len(body) // 2
    return "\n".join(
        (
            json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
            json.dumps({"type": "text", "part": {"type": "text", "text": body[:midpoint]}}),
            json.dumps({"type": "text", "part": {"type": "text", "text": body[midpoint:]}}),
            json.dumps({"type": "step_finish", "part": {"type": "step-finish"}}),
        )
    )


@pytest.mark.parametrize("source_tool", ["claude", "codex", "opencode"])
def test_all_provider_envelopes_resolve_one_item(source_tool: ConfigSyncSource) -> None:
    target: ConfigSyncSource = "opencode" if source_tool != "opencode" else "codex"
    item = _item(target)
    executor = _Executor(RunResult(0, _envelope(source_tool, _resolved_payload(item)), "secret"))

    result = resolve_unresolved_item(
        source_tool,
        item,
        deadline=TOTAL_TIMEOUT_SECONDS,
        executor=executor,
        clock=_Clock(),
    )

    assert result.success
    assert result.failure is None
    assert len(result.files) == 1
    assert result.files[0].content == b"adapted body\n"
    assert executor.calls[0][0] == source_tool
    assert executor.calls[0][2:] == (False, True, ITEM_TIMEOUT_SECONDS)


def test_claude_result_json_string_is_supported() -> None:
    item = _item("codex")
    body = json.dumps(_resolved_payload(item))
    executor = _Executor(
        RunResult(0, json.dumps({"type": "result", "is_error": False, "result": body}))
    )

    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=_Clock()
    )

    assert result.success


def test_prompt_is_canonical_and_contains_only_the_single_artifact_contract() -> None:
    item = _item("codex", executable=True)
    executor = _Executor(RunResult(0, _envelope("claude", _resolved_payload(item))))

    resolve_unresolved_item("claude", item, deadline=300, executor=executor, clock=_Clock())

    prompt = json.loads(executor.calls[0][1])
    assert prompt == {
        "allowed_files": ["skills/convergence-loop/SKILL.md"],
        "allowed_fragments": [],
        "artifact_id": item.identifier,
        "instruction": (
            "Adapt this single workflow artifact to the target tool. Return only schema-v1 "
            "JSON: resolved with files and fragments, or unresolved with reason."
        ),
        "metadata_base64": base64.b64encode(item.metadata or b"").decode(),
        "schema_version": 1,
        "source_content_base64": base64.b64encode(item.source_bytes).decode(),
        "source_executable": True,
        "source_path": item.source_path.as_posix(),
        "source_tool": "claude",
        "target_tool": "codex",
    }


def test_hook_contract_accepts_only_its_script() -> None:
    item = _hook_item()
    contract = allowed_outputs_for_unresolved(item)
    payload = _resolved_payload(item)
    executor = _Executor(RunResult(0, _envelope("opencode", payload)))

    result = resolve_unresolved_item(
        "opencode", item, deadline=300, executor=executor, clock=_Clock()
    )

    assert result.success
    assert result.files[0].relative_path == PurePosixPath("ready_notify_hook.py")
    assert contract.settings_fragments == ()
    assert result.settings_fragments == ()


@pytest.mark.parametrize(
    ("stdout", "returncode", "failure"),
    [
        ("", 0, SemanticFailure.MALFORMED_RESPONSE),
        ("not json", 0, SemanticFailure.MALFORMED_RESPONSE),
        ("ignored", 17, SemanticFailure.EXECUTION_FAILED),
        ("ignored", 124, SemanticFailure.TIMED_OUT),
    ],
)
def test_missing_malformed_nonzero_and_timeout_block_without_partial_output(
    stdout: str, returncode: int, failure: SemanticFailure
) -> None:
    item = _item("codex")
    executor = _Executor(RunResult(returncode, stdout, "private stderr body"))

    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=_Clock()
    )

    assert result.failure is failure
    assert result.files == ()
    assert result.settings_fragments == ()


def test_runner_exception_is_sanitized() -> None:
    result = resolve_unresolved_item(
        "claude",
        _item("codex"),
        deadline=300,
        executor=_FailingExecutor(),
        clock=_Clock(),
    )

    assert result.failure is SemanticFailure.EXECUTION_FAILED
    assert "private workflow body" not in str(result)


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        ({"extra": True}, SemanticFailure.MALFORMED_RESPONSE),
        ({"schema_version": 2}, SemanticFailure.RESPONSE_MISMATCH),
        ({"artifact_id": "skill:other:skills/other/SKILL.md"}, SemanticFailure.RESPONSE_MISMATCH),
        ({"target_tool": "opencode"}, SemanticFailure.RESPONSE_MISMATCH),
        ({"files": [], "fragments": []}, SemanticFailure.UNRESOLVED),
    ],
)
def test_strict_response_schema_rejects_mismatch_and_remaining_ambiguity(
    change: dict[str, object], failure: SemanticFailure
) -> None:
    item = _item("codex")
    payload = _resolved_payload(item)
    payload.update(change)
    executor = _Executor(RunResult(0, _envelope("claude", payload)))

    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=_Clock()
    )

    assert result.failure is failure
    assert result.files == ()
    assert result.settings_fragments == ()


def test_unresolved_response_allows_only_a_nonempty_reason() -> None:
    item = _item("codex")
    payload: dict[str, object] = {
        "schema_version": 1,
        "artifact_id": item.identifier,
        "target_tool": "codex",
        "status": "unresolved",
        "reason": "Cannot preserve target semantics.",
    }
    executor = _Executor(RunResult(0, _envelope("claude", payload)))

    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=_Clock()
    )
    assert result.failure is SemanticFailure.UNRESOLVED

    payload["files"] = []
    executor.result = RunResult(0, _envelope("claude", payload))
    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=_Clock()
    )
    assert result.failure is SemanticFailure.MALFORMED_RESPONSE


def test_nested_extra_fields_and_invalid_base64_are_rejected() -> None:
    item = _item("codex")
    payload = _resolved_payload(item)
    files = cast(list[object], payload["files"])
    file_value = cast(dict[str, object], files[0])
    file_value["extra"] = "not allowed"
    executor = _Executor(RunResult(0, _envelope("claude", payload)))

    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=_Clock()
    )
    assert result.failure is SemanticFailure.MALFORMED_RESPONSE

    file_value.pop("extra")
    file_value["content_base64"] = "not base64!"
    executor.result = RunResult(0, _envelope("claude", payload))
    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=_Clock()
    )
    assert result.failure is SemanticFailure.INVALID_OUTPUT


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "skills/other/SKILL.md",
    ],
)
def test_output_path_must_be_safe_owned_and_artifact_specific(path: str) -> None:
    item = _item("codex")
    executor = _Executor(
        RunResult(0, _envelope("claude", _resolved_payload(item, path=path)))
    )

    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=_Clock()
    )

    assert result.failure is SemanticFailure.INVALID_OUTPUT
    assert result.files == ()


def test_agent_may_not_supply_even_the_hooks_deterministic_fragment() -> None:
    item = _hook_item()
    hook_fragment = {
        "carrier_path": "settings.json",
        "key_path": ["hooks", "Stop"],
        "value_json": "[]",
    }
    executor = _Executor(
        RunResult(0, _envelope("opencode", _resolved_payload(item, fragments=[hook_fragment])))
    )

    result = resolve_unresolved_item(
        "opencode", item, deadline=300, executor=executor, clock=_Clock()
    )

    assert result.failure is SemanticFailure.INVALID_OUTPUT
    assert result.files == ()
    assert result.settings_fragments == ()


def test_invalid_second_output_discards_the_valid_first_output() -> None:
    item = _item("codex")
    payload = _resolved_payload(item)
    files = payload["files"]
    assert isinstance(files, list)
    file_values = cast(list[object], files)
    file_values.append(
        {
            "path": "skills/other/SKILL.md",
            "content_base64": "c2VjcmV0",
            "executable": False,
        }
    )
    executor = _Executor(RunResult(0, _envelope("claude", payload)))

    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=_Clock()
    )

    assert result.failure is SemanticFailure.INVALID_OUTPUT
    assert result.files == ()


@pytest.mark.parametrize(
    ("provider", "stdout"),
    [
        ("claude", json.dumps({"is_error": True, "result": "{}"})),
        ("claude", json.dumps({"type": "result", "result": "{}"})),
        ("claude", json.dumps({"type": "result", "is_error": 0, "result": "{}"})),
        ("claude", json.dumps({"type": "message", "is_error": False, "result": "{}"})),
        (
            "codex",
            "\n".join(
                (
                    json.dumps({"type": "turn.failed", "error": "private"}),
                    json.dumps({"type": "turn.completed"}),
                )
            ),
        ),
        (
            "codex",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "{}"},
                }
            ),
        ),
        (
            "opencode",
            "\n".join(
                (
                    json.dumps({"type": "text", "part": {"text": "{}"}}),
                    json.dumps({"type": "error", "error": "private"}),
                )
            ),
        ),
        ("opencode", json.dumps({"type": "text", "part": {"text": "{}"}})),
        (
            "opencode",
            "\n".join(
                (
                    json.dumps({"type": "text", "part": {"text": "{}"}}),
                    json.dumps({"type": "message", "part": {"type": "step-finish"}}),
                )
            ),
        ),
    ],
)
def test_provider_envelopes_fail_closed(
    provider: ConfigSyncSource, stdout: str
) -> None:
    target: ConfigSyncSource = "opencode" if provider != "opencode" else "codex"
    result = resolve_unresolved_item(
        provider,
        _item(target),
        deadline=300,
        executor=_Executor(RunResult(0, stdout, "private stderr")),
        clock=_Clock(),
    )

    assert result.failure is SemanticFailure.MALFORMED_RESPONSE
    assert result.files == ()


def test_selected_source_is_never_substituted() -> None:
    item = _item("opencode")
    executor = _Executor(RunResult(17, "", "unavailable"))

    result = resolve_unresolved_item(
        "codex", item, deadline=300, executor=executor, clock=_Clock()
    )

    assert result.failure is SemanticFailure.EXECUTION_FAILED
    assert [call[0] for call in executor.calls] == ["codex"]


def test_per_item_and_total_deadlines_are_enforced_monotonically() -> None:
    item = _item("codex")
    clock = _Clock(10)
    assert start_semantic_deadline(clock=clock) == 10 + TOTAL_TIMEOUT_SECONDS

    executor = _Executor(RunResult(0, _envelope("claude", _resolved_payload(item))))
    resolve_unresolved_item(
        "claude", item, deadline=310, executor=executor, clock=clock
    )
    assert executor.calls[0][-1] == ITEM_TIMEOUT_SECONDS

    clock.value = 260
    executor.calls.clear()
    resolve_unresolved_item(
        "claude", item, deadline=310, executor=executor, clock=clock
    )
    assert executor.calls[0][-1] == 50

    clock.value = 310
    executor.calls.clear()
    result = resolve_unresolved_item(
        "claude", item, deadline=310, executor=executor, clock=clock
    )
    assert result.failure is SemanticFailure.DEADLINE_EXCEEDED
    assert executor.calls == []


def test_executor_overrun_blocks_without_returning_its_valid_partial_result() -> None:
    item = _item("codex")
    clock = _Clock()
    executor = _Executor(
        RunResult(0, _envelope("claude", _resolved_payload(item))),
        clock=clock,
        advance=121,
    )

    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=clock
    )

    assert result.failure is SemanticFailure.TIMED_OUT
    assert result.files == ()


def test_total_deadline_overrun_blocks_a_valid_response_without_partial_output() -> None:
    item = _item("codex")
    clock = _Clock(260)
    executor = _Executor(
        RunResult(0, _envelope("claude", _resolved_payload(item))),
        clock=clock,
        advance=51,
    )

    result = resolve_unresolved_item(
        "claude", item, deadline=310, executor=executor, clock=clock
    )

    assert executor.calls[0][-1] == 50
    assert result.failure is SemanticFailure.DEADLINE_EXCEEDED
    assert result.files == ()


@pytest.mark.parametrize(
    "clock_values",
    [
        [0, 0, 300],
        [0, 0, 0, 300],
    ],
)
def test_deadline_is_rechecked_after_envelope_and_response_validation(
    clock_values: list[float],
) -> None:
    item = _item("codex")
    result = resolve_unresolved_item(
        "claude",
        item,
        deadline=300,
        executor=_Executor(RunResult(0, _envelope("claude", _resolved_payload(item)))),
        clock=_SequenceClock(clock_values),
    )

    assert result.failure is SemanticFailure.DEADLINE_EXCEEDED
    assert result.files == ()
    assert result.settings_fragments == ()


@pytest.mark.parametrize(
    "clock_values",
    [
        [0, 0, 121],
        [0, 0, 100, 121],
    ],
)
def test_item_timeout_is_rechecked_after_envelope_and_response_validation(
    clock_values: list[float],
) -> None:
    item = _item("codex")
    result = resolve_unresolved_item(
        "claude",
        item,
        deadline=300,
        executor=_Executor(RunResult(0, _envelope("claude", _resolved_payload(item)))),
        clock=_SequenceClock(clock_values),
    )

    assert result.failure is SemanticFailure.TIMED_OUT
    assert result.files == ()
    assert result.settings_fragments == ()


def test_fingerprint_is_deterministic_length_prefixed_and_contract_sensitive() -> None:
    item = _item("codex", content=b"a", metadata=b"bc")
    first = semantic_artifact_fingerprint("claude", item)

    assert semantic_artifact_fingerprint("claude", item) == first
    assert (
        semantic_artifact_fingerprint(
            "claude", item, adapter_revision=ADAPTER_REVISION + 1
        )
        != first
    )
    assert semantic_artifact_fingerprint(
        "claude", replace(item, source_bytes=b"ab", metadata=b"c")
    ) != first
    assert semantic_artifact_fingerprint("claude", replace(item, target_tool="opencode")) != first
    assert semantic_artifact_fingerprint("claude", replace(item, executable=True)) != first
    assert ADAPTER_REVISION == 3


def test_recursion_errors_at_provider_and_fragment_json_boundaries_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _hook_item()
    original_loads = json.loads

    def recursive_loads(
        value: str,
        *,
        object_pairs_hook: Callable[[list[tuple[str, object]]], object] | None = None,
        parse_constant: Callable[[str], object] | None = None,
    ) -> object:
        if value == "RECURSE":
            raise RecursionError
        return original_loads(
            value,
            object_pairs_hook=object_pairs_hook,
            parse_constant=parse_constant,
        )

    monkeypatch.setattr(config_sync_agent.json, "loads", recursive_loads)

    result = resolve_unresolved_item(
        "opencode",
        _item("codex"),
        deadline=300,
        executor=_Executor(RunResult(0, "RECURSE")),
        clock=_Clock(),
    )
    assert result.failure is SemanticFailure.MALFORMED_RESPONSE

    contract = ArtifactOutputContract(
        (),
        (
            AllowedSettingsFragment(
                PurePosixPath("settings.json"), ("hooks", "Stop")
            ),
        ),
    )
    def contract_for(_item: UnresolvedItem) -> ArtifactOutputContract:
        return contract

    monkeypatch.setattr(
        config_sync_agent,
        "allowed_outputs_for_unresolved",
        contract_for,
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "artifact_id": item.identifier,
        "target_tool": item.target_tool,
        "status": "resolved",
        "files": [],
        "fragments": [
            {
                "carrier_path": "settings.json",
                "key_path": ["hooks", "Stop"],
                "value_json": "RECURSE",
            }
        ],
    }
    result = resolve_unresolved_item(
        "opencode",
        item,
        deadline=300,
        executor=_Executor(RunResult(0, _envelope("opencode", payload))),
        clock=_Clock(),
    )
    assert result.failure is SemanticFailure.INVALID_OUTPUT


def test_invalid_item_contract_blocks_before_executor() -> None:
    item = replace(_item("codex"), source_path=PurePosixPath("../outside"))
    executor = _Executor(RunResult(0, ""))

    result = resolve_unresolved_item(
        "claude", item, deadline=300, executor=executor, clock=_Clock()
    )

    assert result.failure is SemanticFailure.INVALID_REQUEST
    assert result.fingerprint is None
    assert executor.calls == []
