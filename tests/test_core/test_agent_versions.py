"""Shared numeric policy, strict defaults and atomic host record storage."""

import os
import tomllib
from pathlib import Path

import pytest

from djinn_in_a_box.core import agent_versions as versions
from djinn_in_a_box.core import paths
from djinn_in_a_box.core.agent_versions import AgentVersionError


def test_numeric_version_compares_integer_components():
    assert versions.numeric_version("2.1.10") > versions.numeric_version("2.1.9")
    assert versions.numeric_version("10.0.0") > versions.numeric_version("2.99.99")
    assert versions.numeric_version("2.10.0") > versions.numeric_version("2.9.99")
    assert versions.numeric_version("02.01.010") == versions.numeric_version("2.1.10")
    assert versions.numeric_version("2.1.10") == (2, 1, 10)


@pytest.mark.parametrize("value", ["1.2", "v1.2.3", "1.2.3-beta", "1.2.3+meta", "١.2.3",
                                  "1.2.3\n", " 1.2.3", "1.2.3 "])
def test_numeric_version_rejects_non_numeric_values(value):
    with pytest.raises(AgentVersionError, match="numeric x.y.z"):
        versions.numeric_version(value)


@pytest.mark.parametrize("name", ["CLAUDE_CODE_VERSION", "DOCKER_VERSION"])
@pytest.mark.parametrize("declaration", [
    "", "ARG {name}=1.2.3\nARG {name}=1.2.3", "ARG {name}=beta",
    "ARG {name}=1.2.3\nARG {name}=", "ARG {name}=1.2.3\nARG {name}",
    "ARG {name}=1.2.3\n  ARG {name}=1.2.3", "ARG {name}=1.2.3\narg {name}=1.2.3",
    "ARG {name}=1.2.3\nARG\t{name}=1.2.3", "ARG {name}=1.2.3\nARG A=1 {name}=2",
    "  ARG {name}=1.2.3", "arg {name}=1.2.3", "ARG\t{name}=1.2.3",
    "ARG {name}=1.2.3 # comment", "ARG {name}=1.2.3-beta",
])
def test_version_pin_requires_one_numeric_arg(tmp_path, name, declaration):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(declaration.format(name=name) + "\n")
    with pytest.raises(AgentVersionError) as exc:
        versions.version_pin(tmp_path, name)
    assert str(dockerfile) in str(exc.value)
    assert name in str(exc.value)


@pytest.mark.parametrize("name", ["CLAUDE_CODE_VERSION", "DOCKER_VERSION"])
def test_version_pin_ignores_other_names_and_accepts_trailing_space(tmp_path, name):
    (tmp_path / "Dockerfile").write_text(
        f"ARG {name}=1.2.3 \t\nARG MY_{name}=2.3.4\nARG {name}_EXTRA=2.3.4\n"
    )
    assert versions.version_pin(tmp_path, name) == "1.2.3"


@pytest.mark.parametrize("failure", ["missing", "read", "encoding"])
@pytest.mark.parametrize("name", ["CLAUDE_CODE_VERSION", "DOCKER_VERSION"])
def test_version_pin_unreadable_names_file_and_arg(tmp_path, monkeypatch, failure, name):
    dockerfile = tmp_path / "Dockerfile"
    if failure == "read":
        monkeypatch.setattr(
            Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")),
        )
    elif failure == "encoding":
        dockerfile.write_bytes(b"\xff")
    with pytest.raises(AgentVersionError) as exc:
        versions.version_pin(tmp_path, name)
    assert str(dockerfile) in str(exc.value)
    assert name in str(exc.value)


def test_load_versions_missing_empty_and_subset():
    record = paths.AGENT_VERSIONS_FILE
    assert versions.load_versions() == {}
    record.parent.mkdir(parents=True)
    record.write_text("")
    assert versions.load_versions() == {}
    record.write_text('CODEX_VERSION = "0.161.0"\n')
    assert versions.load_versions() == {"CODEX_VERSION": "0.161.0"}


@pytest.mark.parametrize("data", [b"[", b'DOCKER_VERSION = "1.2.3"', b'UNKNOWN = "1.2.3"',
                                  b"[CODEX_VERSION]\nx = 1", b"CODEX_VERSION = 123",
                                  b'CODEX_VERSION = "1.2.3-beta"', b'CODEX_VERSION = "1.2"',
                                  b"\xff"])
def test_load_versions_rejects_invalid_record(data):
    record = paths.AGENT_VERSIONS_FILE
    record.parent.mkdir(parents=True)
    record.write_bytes(data)
    with pytest.raises(AgentVersionError) as exc:
        versions.load_versions()
    assert str(record) in str(exc.value)


def test_load_versions_read_error_is_not_missing(monkeypatch):
    monkeypatch.setattr(Path, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(AgentVersionError) as exc:
        versions.load_versions()
    assert str(paths.AGENT_VERSIONS_FILE) in str(exc.value)


@pytest.mark.parametrize(("local", "expected"), [
    (None, "2.1.10"), ("2.1.9", "2.1.10"), ("2.1.10", "2.1.10"),
    ("02.01.010", "2.1.10"), ("2.1.11", "2.1.11"), ("3.0.0", "3.0.0"),
])
def test_effective_version_is_numeric_max(tmp_path, local, expected):
    (tmp_path / "Dockerfile").write_text("ARG CLAUDE_CODE_VERSION=2.1.10\n")
    record = {} if local is None else {"CLAUDE_CODE_VERSION": local}
    assert versions.effective_version(tmp_path, "CLAUDE_CODE_VERSION", record) == expected
    (tmp_path / "Dockerfile").write_text("ARG CLAUDE_CODE_VERSION=4.0.0\n")
    assert versions.effective_version(tmp_path, "CLAUDE_CODE_VERSION", record) == "4.0.0"


def test_bake_overrides_only_strictly_higher(tmp_path):
    assert versions.bake_overrides(tmp_path, {}) == {}
    (tmp_path / "Dockerfile").write_text(
        "ARG CLAUDE_CODE_VERSION=2.1.10\nARG CODEX_VERSION=0.160.0\nARG OPENCODE_VERSION=1.18.34\n"
    )
    assert versions.bake_overrides(tmp_path, {
        "CLAUDE_CODE_VERSION": "2.1.9", "CODEX_VERSION": "0.160.0", "OPENCODE_VERSION": "1.18.40",
    }) == {"OPENCODE_VERSION": "1.18.40"}


def test_save_versions_is_atomic(monkeypatch):
    record = paths.AGENT_VERSIONS_FILE
    original_replace = os.replace
    original_dump = versions.tomli_w.dump
    streams = []
    replacements = []

    def dump(data, stream):
        streams.append(stream)
        original_dump(data, stream)

    def replace(source, destination):
        assert Path(source).parent == record.parent
        assert streams[0].closed
        assert not record.exists()
        replacements.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr(versions.tomli_w, "dump", dump)
    monkeypatch.setattr(versions.os, "replace", replace)
    data = {"OPENCODE_VERSION": "1.18.40", "CODEX_VERSION": "0.161.0"}
    versions.save_versions(data)
    assert len(replacements) == 1
    assert tomllib.loads(record.read_text()) == data
    assert record.read_text().startswith("CODEX_VERSION")
    assert list(record.parent.iterdir()) == [record]


@pytest.mark.parametrize("failure", ["mkdir", "temp", "write", "replace"])
def test_save_failure_preserves_record_bytes(monkeypatch, failure):
    record = paths.AGENT_VERSIONS_FILE
    record.parent.mkdir(parents=True)
    old = b'# old record\nCODEX_VERSION = "0.160.0"\n'
    record.write_bytes(old)

    def fail(*args, **kwargs):
        raise OSError("storage unavailable")

    if failure == "mkdir":
        monkeypatch.setattr(Path, "mkdir", fail)
    elif failure == "temp":
        monkeypatch.setattr(versions.tempfile, "mkstemp", fail)
    elif failure == "write":
        monkeypatch.setattr(versions.tomli_w, "dump", fail)
    else:
        monkeypatch.setattr(versions.os, "replace", fail)
    with pytest.raises(AgentVersionError) as exc:
        versions.save_versions({"CODEX_VERSION": "0.161.0"})
    assert str(record) in str(exc.value)
    assert record.read_bytes() == old
    assert list(record.parent.iterdir()) == [record]


@pytest.mark.parametrize("data", [{"UNKNOWN": "1.2.3"}, {"CODEX_VERSION": 123},
                                  {"CODEX_VERSION": "beta"}])
def test_save_rejects_invalid_mapping_before_touching_disk(data):
    record = paths.AGENT_VERSIONS_FILE
    with pytest.raises(AgentVersionError) as exc:
        versions.save_versions(data)
    assert str(record) in str(exc.value)
    assert not record.parent.exists()


def test_record_path_is_isolated(tmp_path):
    assert tmp_path / "host-config/agent-versions.toml" == paths.AGENT_VERSIONS_FILE


def test_record_operations_follow_path_at_call_time(tmp_path, monkeypatch):
    first = paths.AGENT_VERSIONS_FILE
    second = tmp_path / "second-host/agent-versions.toml"
    original_open = Path.open

    def opened(self, *args, **kwargs):
        if self.name == "agent-versions.toml" and self not in {first, second}:
            raise AssertionError("record access escaped temporary isolation")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    versions.save_versions({"CODEX_VERSION": "0.161.0"})
    assert versions.load_versions() == {"CODEX_VERSION": "0.161.0"}
    monkeypatch.setattr(paths, "AGENT_VERSIONS_FILE", second)
    assert versions.load_versions() == {}
    versions.save_versions({"OPENCODE_VERSION": "1.18.40"})
    assert versions.load_versions() == {"OPENCODE_VERSION": "1.18.40"}
    assert tomllib.loads(first.read_text()) == {"CODEX_VERSION": "0.161.0"}
