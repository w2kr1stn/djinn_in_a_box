"""Bounded semantic adaptation for one unresolved workflow artifact."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol, cast

from djinn_in_a_box.config.models import ConfigSyncSource
from djinn_in_a_box.core.agent_runner import run_headless_agent
from djinn_in_a_box.core.config_sync_adapters import (
    ADAPTER_REVISION,
    ArtifactOutputContract,
    RenderedFile,
    SettingsFragment,
    UnresolvedItem,
    allowed_outputs_for_unresolved,
    fragment_is_owned,
    is_safe_relative_path,
    path_is_owned,
)
from djinn_in_a_box.core.docker import RunResult

SEMANTIC_CONTRACT_VERSION = 1
ITEM_TIMEOUT_SECONDS = 120
TOTAL_TIMEOUT_SECONDS = 300


class SemanticFailure(StrEnum):
    INVALID_REQUEST = "invalid-request"
    DEADLINE_EXCEEDED = "deadline-exceeded"
    EXECUTION_FAILED = "execution-failed"
    TIMED_OUT = "timed-out"
    MALFORMED_RESPONSE = "malformed-response"
    RESPONSE_MISMATCH = "response-mismatch"
    UNRESOLVED = "unresolved"
    INVALID_OUTPUT = "invalid-output"


@dataclass(frozen=True, slots=True)
class SemanticAgentResult:
    fingerprint: str | None
    files: tuple[RenderedFile, ...] = ()
    settings_fragments: tuple[SettingsFragment, ...] = ()
    failure: SemanticFailure | None = None

    @property
    def success(self) -> bool:
        return self.failure is None


class HeadlessExecutor(Protocol):
    def __call__(
        self,
        agent: str,
        prompt: str,
        *,
        write: bool,
        json_output: bool,
        timeout: int | None,
    ) -> RunResult: ...


class _InvalidEnvelopeError(ValueError):
    pass


class _InvalidResponseError(ValueError):
    pass


def start_semantic_deadline(
    *, clock: Callable[[], float] = time.monotonic
) -> float:
    """Return the shared monotonic deadline for one explicit synchronization."""
    return clock() + TOTAL_TIMEOUT_SECONDS


def semantic_artifact_fingerprint(
    source_tool: ConfigSyncSource,
    item: UnresolvedItem,
    *,
    adapter_revision: int = ADAPTER_REVISION,
) -> str:
    """Hash raw artifact input and its closed output contract without retaining bodies."""
    if type(adapter_revision) is not int or adapter_revision < 1:
        raise ValueError("Adapter revision must be a positive integer.")
    target_tool = item.target_tool
    if target_tool is None or target_tool == source_tool:
        raise ValueError("Semantic adaptation requires a distinct target tool.")
    contract = allowed_outputs_for_unresolved(item)
    allowed = _canonical_allowed_outputs(contract)
    metadata = b"\x00" if item.metadata is None else b"\x01" + item.metadata
    values = (
        str(SEMANTIC_CONTRACT_VERSION).encode(),
        str(adapter_revision).encode(),
        source_tool.encode(),
        target_tool.encode(),
        item.identifier.encode(),
        item.source_path.as_posix().encode(),
        item.source_bytes,
        metadata,
        b"\x01" if item.executable else b"\x00",
        allowed,
    )
    digest = hashlib.sha256()
    for value in values:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def resolve_unresolved_item(
    source_tool: ConfigSyncSource,
    item: UnresolvedItem,
    *,
    deadline: float,
    executor: HeadlessExecutor = run_headless_agent,
    clock: Callable[[], float] = time.monotonic,
    adapter_revision: int = ADAPTER_REVISION,
) -> SemanticAgentResult:
    """Resolve exactly one item through its configured source provider."""
    try:
        contract = allowed_outputs_for_unresolved(item)
        fingerprint = semantic_artifact_fingerprint(
            source_tool, item, adapter_revision=adapter_revision
        )
    except (TypeError, ValueError):
        return SemanticAgentResult(None, failure=SemanticFailure.INVALID_REQUEST)

    started = clock()
    if not math.isfinite(deadline) or deadline <= started:
        return SemanticAgentResult(fingerprint, failure=SemanticFailure.DEADLINE_EXCEEDED)
    timeout = min(ITEM_TIMEOUT_SECONDS, int(deadline - started))
    if timeout < 1:
        return SemanticAgentResult(fingerprint, failure=SemanticFailure.DEADLINE_EXCEEDED)

    prompt = _build_prompt(source_tool, item, contract)
    try:
        execution = executor(
            source_tool,
            prompt,
            write=False,
            json_output=True,
            timeout=timeout,
        )
    except Exception:
        return SemanticAgentResult(fingerprint, failure=SemanticFailure.EXECUTION_FAILED)

    finished = clock()
    if finished >= deadline:
        return SemanticAgentResult(fingerprint, failure=SemanticFailure.DEADLINE_EXCEEDED)
    if finished - started > timeout or execution.returncode == 124:
        return SemanticAgentResult(fingerprint, failure=SemanticFailure.TIMED_OUT)
    if execution.returncode != 0:
        return SemanticAgentResult(fingerprint, failure=SemanticFailure.EXECUTION_FAILED)

    try:
        response = _extract_provider_response(source_tool, execution.stdout)
        failure = _deadline_failure(clock(), started, deadline, timeout)
        if failure is not None:
            return SemanticAgentResult(fingerprint, failure=failure)
        parsed = _parse_response(response, item, contract)
        failure = _deadline_failure(clock(), started, deadline, timeout)
        if failure is not None:
            return SemanticAgentResult(fingerprint, failure=failure)
    except _InvalidEnvelopeError:
        return SemanticAgentResult(fingerprint, failure=SemanticFailure.MALFORMED_RESPONSE)
    except _InvalidResponseError as error:
        failure = error.args[0]
        assert isinstance(failure, SemanticFailure)
        return SemanticAgentResult(fingerprint, failure=failure)
    return SemanticAgentResult(
        fingerprint,
        files=parsed[0],
        settings_fragments=parsed[1],
    )


def _deadline_failure(
    now: float, started: float, deadline: float, timeout: int
) -> SemanticFailure | None:
    if now >= deadline:
        return SemanticFailure.DEADLINE_EXCEEDED
    if now - started > timeout:
        return SemanticFailure.TIMED_OUT
    return None


def _build_prompt(
    source_tool: ConfigSyncSource,
    item: UnresolvedItem,
    contract: ArtifactOutputContract,
) -> str:
    request = {
        "schema_version": SEMANTIC_CONTRACT_VERSION,
        "instruction": (
            "Adapt this single workflow artifact to the target tool. Return only schema-v1 "
            "JSON: resolved with files and fragments, or unresolved with reason."
        ),
        "source_tool": source_tool,
        "target_tool": item.target_tool,
        "artifact_id": item.identifier,
        "source_path": item.source_path.as_posix(),
        "source_content_base64": base64.b64encode(item.source_bytes).decode("ascii"),
        "source_executable": item.executable,
        "metadata_base64": (
            None if item.metadata is None else base64.b64encode(item.metadata).decode("ascii")
        ),
        "allowed_files": [path.as_posix() for path in contract.file_paths],
        "allowed_fragments": [
            {
                "carrier_path": fragment.carrier_path.as_posix(),
                "key_path": list(fragment.key_path),
            }
            for fragment in contract.settings_fragments
        ],
    }
    return json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_allowed_outputs(contract: ArtifactOutputContract) -> bytes:
    value = {
        "files": [path.as_posix() for path in contract.file_paths],
        "fragments": [
            {
                "carrier_path": fragment.carrier_path.as_posix(),
                "key_path": list(fragment.key_path),
            }
            for fragment in contract.settings_fragments
        ],
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _extract_provider_response(
    source_tool: ConfigSyncSource, stdout: str
) -> Mapping[str, object]:
    if not stdout.strip():
        raise _InvalidEnvelopeError
    if source_tool == "claude":
        return _extract_claude(stdout)
    if source_tool == "codex":
        return _extract_codex(stdout)
    if source_tool == "opencode":
        return _extract_opencode(stdout)
    raise _InvalidEnvelopeError


def _extract_claude(stdout: str) -> Mapping[str, object]:
    envelope = _json_mapping(stdout, _InvalidEnvelopeError)
    if envelope.get("type") != "result" or type(envelope.get("is_error")) is not bool:
        raise _InvalidEnvelopeError
    if envelope["is_error"] is not False:
        raise _InvalidEnvelopeError
    if "structured_output" in envelope:
        structured = envelope["structured_output"]
        if not isinstance(structured, dict):
            raise _InvalidEnvelopeError
        return cast(dict[str, object], structured)
    result = envelope.get("result")
    if not isinstance(result, str):
        raise _InvalidEnvelopeError
    return _json_mapping(result, _InvalidEnvelopeError)


def _extract_codex(stdout: str) -> Mapping[str, object]:
    events = _json_lines(stdout)
    messages: list[str] = []
    completed = 0
    for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise _InvalidEnvelopeError
        if event_type == "error" or event_type.endswith(".failed"):
            raise _InvalidEnvelopeError
        if event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                raise _InvalidEnvelopeError
            item_mapping = cast(dict[str, object], item)
            if item_mapping.get("type") == "agent_message":
                text = item_mapping.get("text")
                if not isinstance(text, str):
                    raise _InvalidEnvelopeError
                messages.append(text)
        elif event_type == "turn.completed":
            completed += 1
    if completed != 1 or len(messages) != 1 or events[-1].get("type") != "turn.completed":
        raise _InvalidEnvelopeError
    return _json_mapping(messages[0], _InvalidEnvelopeError)


def _extract_opencode(stdout: str) -> Mapping[str, object]:
    events = _json_lines(stdout)
    text_parts: list[str] = []
    for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise _InvalidEnvelopeError
        if (
            event_type == "error"
            or event_type.endswith(".failed")
            or event_type.endswith("_failed")
        ):
            raise _InvalidEnvelopeError
        if event_type == "text":
            part = event.get("part")
            if not isinstance(part, dict):
                raise _InvalidEnvelopeError
            part_mapping = cast(dict[str, object], part)
            text = part_mapping.get("text")
            if not isinstance(text, str):
                raise _InvalidEnvelopeError
            text_parts.append(text)
    terminal = events[-1]
    terminal_type = terminal.get("type")
    if terminal_type not in {"step_finish", "step-finish"} or not text_parts:
        raise _InvalidEnvelopeError
    return _json_mapping("".join(text_parts), _InvalidEnvelopeError)


def _json_lines(stdout: str) -> list[Mapping[str, object]]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise _InvalidEnvelopeError
    return [_json_mapping(line, _InvalidEnvelopeError) for line in lines]


def _json_mapping(
    value: str,
    error_type: type[_InvalidEnvelopeError] | type[_InvalidResponseError],
) -> Mapping[str, object]:
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, RecursionError):
        raise error_type from None
    if not isinstance(decoded, dict):
        raise error_type
    return cast(dict[str, object], decoded)


def _unique_object(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("Non-finite JSON number.")


def _parse_response(
    response: Mapping[str, object],
    item: UnresolvedItem,
    contract: ArtifactOutputContract,
) -> tuple[tuple[RenderedFile, ...], tuple[SettingsFragment, ...]]:
    common = {"schema_version", "artifact_id", "target_tool", "status"}
    if type(response.get("schema_version")) is not int or response.get(
        "schema_version"
    ) != SEMANTIC_CONTRACT_VERSION:
        raise _InvalidResponseError(SemanticFailure.RESPONSE_MISMATCH)
    if response.get("artifact_id") != item.identifier or response.get(
        "target_tool"
    ) != item.target_tool:
        raise _InvalidResponseError(SemanticFailure.RESPONSE_MISMATCH)

    status = response.get("status")
    if status == "unresolved":
        if set(response) != common | {"reason"}:
            raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)
        reason = response.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)
        raise _InvalidResponseError(SemanticFailure.UNRESOLVED)
    if status != "resolved" or set(response) != common | {"files", "fragments"}:
        raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)

    files_value = response.get("files")
    fragments_value = response.get("fragments")
    if not isinstance(files_value, list) or not isinstance(fragments_value, list):
        raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)
    if not files_value and not fragments_value:
        raise _InvalidResponseError(SemanticFailure.UNRESOLVED)

    allowed_paths = set(contract.file_paths)
    allowed_fragments = {
        (fragment.carrier_path, fragment.key_path) for fragment in contract.settings_fragments
    }
    files: list[RenderedFile] = []
    seen_paths: set[PurePosixPath] = set()
    for value in cast(list[object], files_value):
        if not isinstance(value, dict):
            raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)
        file_value = cast(dict[str, object], value)
        if set(file_value) != {
            "path",
            "content_base64",
            "executable",
        }:
            raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)
        raw_path = file_value.get("path")
        encoded = file_value.get("content_base64")
        executable = file_value.get("executable")
        if not isinstance(raw_path, str) or not isinstance(encoded, str):
            raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)
        if type(executable) is not bool:
            raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)
        path = PurePosixPath(raw_path)
        if (
            raw_path != path.as_posix()
            or not is_safe_relative_path(path)
            or not path_is_owned(cast(ConfigSyncSource, item.target_tool), path)
            or path not in allowed_paths
            or path in seen_paths
        ):
            raise _InvalidResponseError(SemanticFailure.INVALID_OUTPUT)
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeEncodeError):
            raise _InvalidResponseError(SemanticFailure.INVALID_OUTPUT) from None
        seen_paths.add(path)
        files.append(RenderedFile(path, content, item.identifier, executable))

    fragments: list[SettingsFragment] = []
    seen_fragments: set[tuple[PurePosixPath, tuple[str, ...]]] = set()
    for value in cast(list[object], fragments_value):
        if not isinstance(value, dict):
            raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)
        fragment_value = cast(dict[str, object], value)
        if set(fragment_value) != {
            "carrier_path",
            "key_path",
            "value_json",
        }:
            raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)
        raw_carrier = fragment_value.get("carrier_path")
        raw_keys = fragment_value.get("key_path")
        raw_json = fragment_value.get("value_json")
        if (
            not isinstance(raw_carrier, str)
            or not isinstance(raw_keys, list)
            or not raw_keys
            or any(
                not isinstance(key, str) or not key for key in cast(list[object], raw_keys)
            )
            or not isinstance(raw_json, str)
        ):
            raise _InvalidResponseError(SemanticFailure.MALFORMED_RESPONSE)
        carrier = PurePosixPath(raw_carrier)
        keys = tuple(cast(list[str], raw_keys))
        fragment_key = (carrier, keys)
        if (
            raw_carrier != carrier.as_posix()
            or not is_safe_relative_path(carrier)
            or not fragment_is_owned(cast(ConfigSyncSource, item.target_tool), carrier, keys)
            or fragment_key not in allowed_fragments
            or fragment_key in seen_fragments
        ):
            raise _InvalidResponseError(SemanticFailure.INVALID_OUTPUT)
        try:
            parsed_json = cast(
                object,
                json.loads(
                    raw_json,
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_json_constant,
                ),
            )
            value_json = json.dumps(
                parsed_json,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
            raise _InvalidResponseError(SemanticFailure.INVALID_OUTPUT) from None
        seen_fragments.add(fragment_key)
        fragments.append(SettingsFragment(carrier, keys, value_json, item.identifier))

    return (
        tuple(sorted(files, key=lambda value: value.relative_path)),
        tuple(sorted(fragments, key=lambda value: (value.carrier_path, value.key_path))),
    )
