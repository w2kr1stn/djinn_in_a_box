from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from djinn_in_a_box.core import workflow_publisher as publisher

SCRIPT = Path(publisher.__file__)
KINDS = ("opencode.json", "cli.json", "themes/night.json")
LIMIT = 1 << 20


def write(root: Path, relative: str, content: bytes = b"{}\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def call(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "personal-carrier", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


@pytest.mark.parametrize("relative", KINDS)
@pytest.mark.parametrize("raw", (b'{"bad":', b'// comment\n{"theme":"night",}\n'))
def test_copy_passes_content_without_syntax_check(tmp_path: Path, relative: str, raw: bytes):
    carrier, runtime = tmp_path / "carrier", tmp_path / "runtime"
    source = write(carrier, relative, raw)
    result = call("copy", "--carrier", carrier, "--runtime", runtime)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert (runtime / relative).read_bytes() == source.read_bytes() == raw
    assert stat.S_IMODE((runtime / relative).stat().st_mode) == 0o600
    assert result.stderr == ""


def test_exact_allowlist_ignores_every_other_name(tmp_path: Path):
    carrier, runtime = tmp_path / "carrier", tmp_path / "runtime"
    ignored = (
        "themes/.hidden.json",
        "themes/.json",
        "themes/a.json.tmp",
        "themes/plain",
        "themes/sub/nested.json",
        "themes/a.JSON",
        "other.json",
        "auth.json",
        "seed/x",
    )
    for relative in (*KINDS, *ignored):
        write(carrier, relative)
    result = call("copy", "--carrier", carrier, "--runtime", runtime)
    assert result.returncode == 0, result.stderr
    assert sorted(str(p.relative_to(runtime)) for p in runtime.rglob("*") if p.is_file()) == sorted(
        KINDS
    )
    for ignored_path in ignored:
        assert not (runtime / ignored_path).exists()


@pytest.mark.parametrize("raw", (b"{}", b"x" * (LIMIT + 1)), ids=("small", "oversize"))
def test_non_utf8_theme_is_not_on_allowlist(tmp_path: Path, raw: bytes):
    carrier, runtime, capture = (tmp_path / name for name in ("carrier", "runtime", "capture"))
    relative = os.fsdecode(b"themes/caf\xe9.json")
    target = write(carrier, relative, raw)
    result = call("copy", "--carrier", carrier, "--runtime", runtime)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []  # ignored, never protected
    assert result.stderr == ""
    assert not (runtime / relative).exists()
    source = write(runtime, relative, b'{"runtime":true}')
    result = call("snapshot", "--runtime", runtime, "--capture", capture)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert not (capture / relative).exists()
    result = call("install", "--root", carrier, "--relative", relative, "--source", source)
    assert result.returncode == 1
    assert target.read_bytes() == raw


@pytest.mark.parametrize("side", ("carrier", "runtime"))
@pytest.mark.parametrize(
    "relative,shape",
    [
        (rel, shape)
        for rel in (*KINDS, "themes")
        for shape in ("symlink", "dangling", "fifo", "directory")
        if (rel, shape) != ("themes", "directory")
    ],
)
def test_copy_refuses_unsafe_entries_without_blocking(tmp_path, side, relative, shape):
    carrier, runtime = tmp_path / "carrier", tmp_path / "runtime"
    carrier.mkdir()
    runtime.mkdir()
    root = carrier if side == "carrier" else runtime
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    outside = write(tmp_path, "outside", b"sentinel")
    if shape in {"symlink", "dangling"}:
        path.symlink_to(outside if shape == "symlink" else tmp_path / "absent")
    elif shape == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    result = call("copy", "--carrier", carrier, "--runtime", runtime)
    assert result.returncode == 1
    assert f"refused {relative}:" in result.stderr
    assert "workflow publisher:" not in result.stderr
    assert outside.read_bytes() == b"sentinel"
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("side", ("carrier", "runtime"))
@pytest.mark.parametrize("dangling", (False, True))
def test_copy_refuses_root_links(tmp_path, side, dangling):
    carrier, runtime, outside = (tmp_path / name for name in ("carrier", "runtime", "outside"))
    if not dangling:
        outside.mkdir()
    (carrier if side == "runtime" else runtime).mkdir()
    (carrier if side == "carrier" else runtime).symlink_to(outside, target_is_directory=True)
    result = call("copy", "--carrier", carrier, "--runtime", runtime)
    assert result.returncode == 1
    assert "root" in result.stderr and "refused" in result.stderr
    assert not outside.exists() if dangling else not list(outside.iterdir())


@pytest.mark.parametrize("relative", KINDS)
@pytest.mark.parametrize("extra", (0, 1))
def test_copy_size_boundary_protection_and_private_mode(tmp_path, relative, extra):
    carrier, runtime = tmp_path / "carrier", tmp_path / "runtime"
    raw = b" " * (LIMIT + extra)
    source = write(carrier, relative, raw)
    result = call("copy", "--carrier", carrier, "--runtime", runtime)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ([relative] if extra else [])
    assert source.read_bytes() == raw
    if extra:
        assert not (runtime / relative).exists()
        assert f"skipped {relative}: larger than 1 MiB" in result.stderr
    else:
        assert (runtime / relative).read_bytes() == raw
        assert stat.S_IMODE((runtime / relative).stat().st_mode) == 0o600


def test_size_is_checked_before_reopen(tmp_path, monkeypatch):
    write(tmp_path, "opencode.json", b"x" * (LIMIT + 1))

    def unexpected(*args, **kwargs):
        pytest.fail("oversize inode reopened for I/O")

    monkeypatch.setattr(publisher, "_read_pinned", unexpected)
    with (
        publisher._personal_opened_root(tmp_path) as root,
        pytest.raises(publisher._PersonalOversizeError),
    ):
        publisher._personal_read(root, PurePosixPath("opencode.json"))


def test_growth_after_fstat_is_bounded_to_cap_plus_one(tmp_path, monkeypatch):
    path = write(tmp_path, "opencode.json")
    original_reopen, original_read = publisher._read_pinned, os.read
    consumed = 0

    def grow(pin, *, limit=None):
        path.write_bytes(b"x" * (3 * LIMIT))
        return original_reopen(pin, limit=limit)

    def counted_read(fd, size):
        nonlocal consumed
        chunk = original_read(fd, size)
        consumed += len(chunk)
        assert consumed <= LIMIT + 1, "read allocated beyond cap+1 after inode growth"
        return chunk

    monkeypatch.setattr(publisher, "_read_pinned", grow)
    monkeypatch.setattr(os, "read", counted_read)
    with (
        publisher._personal_opened_root(tmp_path) as root,
        pytest.raises(publisher._PersonalOversizeError),
    ):
        publisher._personal_read(root, PurePosixPath("opencode.json"))
    assert consumed == LIMIT + 1


def test_snapshot_pins_inode_across_atomic_rename(tmp_path, monkeypatch):
    path = write(tmp_path, "cli.json", b'{"old":true}')
    original = publisher._read_pinned

    def swap(pin, *, limit=None):
        replacement = write(tmp_path, "cli.tmp", b'{"new":true}')
        replacement.replace(path)
        return original(pin, limit=limit)

    monkeypatch.setattr(publisher, "_read_pinned", swap)
    with publisher._personal_opened_root(tmp_path) as root:
        assert publisher._personal_read(root, PurePosixPath("cli.json")) == b'{"old":true}'
    assert path.read_bytes() == b'{"new":true}'


@pytest.mark.parametrize("relative", KINDS)
@pytest.mark.parametrize("shape", ("symlink", "fifo", "directory", "oversize"))
def test_snapshot_refuses_one_entry_and_still_captures_sibling(tmp_path, relative, shape):
    runtime, capture = tmp_path / "runtime", tmp_path / "capture"
    path = write(runtime, relative)
    path.unlink()
    if shape == "symlink":
        path.symlink_to(tmp_path / "missing")
    elif shape == "fifo":
        os.mkfifo(path)
    elif shape == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"x" * (LIMIT + 1))
    safe = next(name for name in KINDS if name != relative)
    write(runtime, safe, b'{"safe":true}')
    result = call("snapshot", "--runtime", runtime, "--capture", capture)
    assert result.returncode == 0, result.stderr
    records = json.loads(result.stdout)
    assert [r["relative"] for r in records] == [relative]
    assert [r["kind"] for r in records] == ["entry"]
    assert f"refused {relative}:" in result.stderr
    assert not (capture / relative).exists()
    assert (capture / safe).read_bytes() == b'{"safe":true}'


def test_snapshot_requires_fresh_capture_and_open_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    write(runtime, "cli.json")
    capture = tmp_path / "capture"
    assert call("snapshot", "--runtime", runtime, "--capture", capture).returncode == 0
    (runtime / "cli.json").unlink()
    result = call("snapshot", "--runtime", runtime, "--capture", capture)
    assert result.returncode == 1
    fresh = tmp_path / "fresh"
    assert call("snapshot", "--runtime", runtime, "--capture", fresh).returncode == 0
    assert not list(fresh.iterdir())
    assert (
        call("snapshot", "--runtime", tmp_path / "absent", "--capture", tmp_path / "new").returncode
        == 1
    )


@pytest.mark.parametrize("fault", ("write", "fsync", "replace"))
def test_failed_install_keeps_old_bytes_and_cleans_temporary(tmp_path, monkeypatch, fault):
    target = write(tmp_path, "cli.json", b"old")
    before = set(tmp_path.iterdir())

    def fail(*args, **kwargs):
        raise OSError("injected precommit failure")

    monkeypatch.setattr(os, fault, fail)
    with (
        publisher._personal_opened_root(tmp_path) as root,
        pytest.raises(OSError, match="injected"),
    ):
        publisher._personal_write(root, PurePosixPath("cli.json"), b"new")
    assert target.read_bytes() == b"old"
    assert set(tmp_path.iterdir()) == before


def test_install_creates_themes_fsyncs_and_uses_private_mode(tmp_path, monkeypatch):
    events = []
    original_fsync, original_replace = os.fsync, os.replace

    def fsync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original_fsync(fd)

    def replace(*args, **kwargs):
        events.append("replace")
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    with publisher._personal_opened_root(tmp_path) as root:
        publisher._personal_write(root, PurePosixPath("themes/night.json"), b"{}")
    assert events == ["file", "replace", "directory"]
    assert stat.S_IMODE((tmp_path / "themes/night.json").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "relative",
    (
        "../cli.json",
        "themes/.x.json",
        "themes/.json",
        "themes/sub/x.json",
        "auth.json",
        "themes/x.tmp",
        "themes//x.json",
    ),
)
def test_install_rejects_nonallowlisted_paths(tmp_path, relative):
    source = write(tmp_path, "capture")
    before = set(tmp_path.iterdir())
    result = call("install", "--root", tmp_path, "--relative", relative, "--source", source)
    assert result.returncode == 1
    assert "refused" in result.stderr
    assert set(tmp_path.iterdir()) == before


def test_personal_cli_does_not_relax_workflow_required_options():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "--canonical-root" in result.stderr
    assert (
        "--view" in result.stderr and "--manifest" in result.stderr and "--target" in result.stderr
    )


def test_credentials_backup_restore_then_personal_copy(tmp_path):
    from djinn_in_a_box.config.defaults import SYNC_PATHS
    from djinn_in_a_box.config.models import AppConfig
    from djinn_in_a_box.core.docker import backup_sync_path, clear_sync_path, restore_sync_path

    assert "opencode" in SYNC_PATHS["credentials"]
    config = AppConfig(code_dir=tmp_path, config_root=tmp_path / "config")
    carrier = config.config_root / "opencode"
    originals = dict.fromkeys(KINDS, b'{"personal":true}')
    originals["auth.json"] = b'{"fixture":"credential"}'
    for name, raw in originals.items():
        write(carrier, name, raw)
    archives = tmp_path / "archives"
    archives.mkdir()
    assert backup_sync_path(carrier, archives).returncode == 0
    assert clear_sync_path(carrier)
    assert carrier.is_dir() and not list(carrier.iterdir())
    assert restore_sync_path("opencode", archives, config).returncode == 0
    assert all((carrier / name).read_bytes() == raw for name, raw in originals.items())
    runtime = tmp_path / "runtime"
    result = call("copy", "--carrier", carrier, "--runtime", runtime)
    assert result.returncode == 0, result.stderr
    assert all((runtime / name).read_bytes() == originals[name] for name in KINDS)
    assert not (runtime / "auth.json").exists()


def test_runtime_admission_precedes_copying_even_a_safe_carrier_entry(tmp_path):
    carrier, runtime = tmp_path / "carrier", tmp_path / "runtime"
    write(carrier, "opencode.json", b'{"safe":true}')
    runtime.mkdir()
    (runtime / "cli.json").symlink_to(tmp_path / "absent")
    result = call("copy", "--carrier", carrier, "--runtime", runtime)
    assert result.returncode == 1
    assert not (runtime / "opencode.json").exists()


def test_install_error_reports_relative_and_keeps_old_file(tmp_path):
    target = tmp_path / "target"
    write(target, "cli.json", b"old")
    source = write(tmp_path, "payload", b"x" * (LIMIT + 1))
    result = call("install", "--root", target, "--relative", "cli.json", "--source", source)
    assert result.returncode == 1
    assert "refused cli.json: larger than 1 MiB" in result.stderr
    assert "workflow publisher:" not in result.stderr
    assert (target / "cli.json").read_bytes() == b"old"


def test_workflow_reads_remain_unbounded_and_write_modes_unchanged(tmp_path):
    raw = b"x" * (LIMIT + 1)
    write(tmp_path, "workflow", raw)
    with publisher._personal_opened_root(tmp_path) as root:
        assert publisher._read_at(root, "workflow").content == raw
        publisher._replace_at(root, "ordinary", raw, False)
        publisher._replace_at(root, "executable", raw, True)
    assert (tmp_path / "ordinary").read_bytes() == raw
    assert (tmp_path / "ordinary").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "executable").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("side", ("carrier", "runtime"))
def test_themes_directory_link_cannot_redirect_enumeration(tmp_path, side):
    carrier, runtime, outside = (tmp_path / name for name in ("carrier", "runtime", "outside"))
    carrier.mkdir()
    runtime.mkdir()
    write(outside, "night.json", b"sentinel")
    (carrier if side == "carrier" else runtime).joinpath("themes").symlink_to(
        outside, target_is_directory=True
    )
    result = call("copy", "--carrier", carrier, "--runtime", runtime)
    assert result.returncode == 1
    assert "refused themes:" in result.stderr
    assert (outside / "night.json").read_bytes() == b"sentinel"


@pytest.mark.parametrize("relative", KINDS)
def test_empty_file_is_copied_and_not_protected(tmp_path, relative):
    carrier, runtime = tmp_path / "carrier", tmp_path / "runtime"
    write(carrier, relative, b"")
    result = call("copy", "--carrier", carrier, "--runtime", runtime)
    assert result.returncode == 0
    assert json.loads(result.stdout) == []
    assert (runtime / relative).read_bytes() == b""
    capture = tmp_path / "capture"
    snapped = call("snapshot", "--runtime", runtime, "--capture", capture)
    assert snapped.returncode == 0, snapped.stderr
    assert (capture / relative).read_bytes() == b""


@pytest.mark.parametrize("shape", ("symlink", "directory", "fifo"))
def test_install_refuses_existing_unsafe_destination(tmp_path, shape):
    target = tmp_path / "target"
    target.mkdir()
    leaf = target / "cli.json"
    outside = write(tmp_path, "outside", b"sentinel")
    if shape == "symlink":
        leaf.symlink_to(outside)
    elif shape == "directory":
        leaf.mkdir()
    else:
        os.mkfifo(leaf)
    source = write(tmp_path, "source", b"{}")
    result = call("install", "--root", target, "--relative", "cli.json", "--source", source)
    assert result.returncode == 1
    assert "refused cli.json:" in result.stderr
    assert outside.read_bytes() == b"sentinel"
    assert leaf.is_symlink() if shape == "symlink" else leaf.exists()


def test_copy_write_failure_is_fatal_and_names_entry(tmp_path, monkeypatch, capsys):
    carrier, runtime = tmp_path / "carrier", tmp_path / "runtime"
    write(carrier, "cli.json", b"new")
    write(runtime, "cli.json", b"old")

    def fail(*args, **kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr(publisher, "_replace_at", fail)
    result = publisher.main(
        ["personal-carrier", "copy", "--carrier", str(carrier), "--runtime", str(runtime)]
    )
    assert result == 1
    assert "refused cli.json: injected write failure" in capsys.readouterr().err
    assert (runtime / "cli.json").read_bytes() == b"old"


def test_carrier_environment_names_match_the_current_shell_contract():
    from djinn_in_a_box.config.declarations import DJINN_ENV_KEYS

    assert "OPENCODE_PERSISTENT_SETTINGS" not in DJINN_ENV_KEYS
    assert {
        "OPENCODE_RUNTIME_ROOT",
        "OPENCODE_RUNTIME_SETTINGS",
        "WORKFLOW_PUBLISHER",
        "SETTINGS_COPY_HELPER",
        "OPENCODE_CREDENTIALS_HELPER",
    } <= DJINN_ENV_KEYS


@pytest.mark.parametrize("raw", (b"", b'{"partial":', b"{}"))
def test_install_is_a_contained_byte_writer_with_no_syntax_gate(tmp_path, raw):
    target = tmp_path / "target"
    target.mkdir()
    source = write(tmp_path, "capture", raw)
    result = call("install", "--root", target, "--relative", "cli.json", "--source", source)
    assert result.returncode == 0, result.stderr
    assert (target / "cli.json").read_bytes() == raw
    assert (target / "cli.json").stat().st_mode & 0o777 == 0o600


def test_install_missing_capture_is_fatal_without_changing_target(tmp_path):
    target = tmp_path / "target"
    write(target, "cli.json", b"old")
    result = call(
        "install", "--root", target, "--relative", "cli.json", "--source", tmp_path / "absent"
    )
    assert result.returncode == 1
    assert "refused cli.json: capture is missing" in result.stderr
    assert (target / "cli.json").read_bytes() == b"old"


@pytest.mark.parametrize("shape", ("oversize", "missing", "symlink", "fifo", "utf16"))
def test_validate_refuses_unsafe_or_non_utf8_capture(tmp_path, shape):
    source = tmp_path / "capture"
    if shape == "oversize":
        source.write_bytes(b" " * LIMIT + b"{}")
    elif shape == "symlink":
        source.symlink_to(write(tmp_path, "outside", b"{}"))
    elif shape == "fifo":
        os.mkfifo(source)
    elif shape == "utf16":
        source.write_bytes('{"value":true}'.encode("utf-16"))
    result = call("validate", "--source", source)
    assert result.returncode == 1
    assert "refused" in result.stderr


def test_validate_deeply_nested_json_refuses_without_traceback(tmp_path: Path) -> None:
    content = b"[" * 200_000 + b"]" * 200_000
    assert len(content) <= LIMIT
    source = write(tmp_path, "capture", content)
    result = call("validate", "--source", source)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("refused .: ")
    assert len(result.stderr.splitlines()) == 1
    assert "Traceback" not in result.stderr


def test_validate_accepts_exactly_at_limit(tmp_path):
    source = write(tmp_path, "capture", b" " * (LIMIT - 2) + b"{}")
    result = call("validate", "--source", source)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("fault", ("read", "enumerate", "capture", "capture-collision"))
def test_snapshot_io_refusal_kind_includes_read_and_capture_failures(
    tmp_path, monkeypatch, capsys, fault
):
    runtime, capture = tmp_path / "runtime", tmp_path / "capture"
    write(runtime, "cli.json")
    if fault == "enumerate":
        write(runtime, "themes/night.json")
        original = publisher._open_child_directory

        def fail_themes(parent, name, **kwargs):
            if name == "themes":
                raise OSError("injected enumeration failure")
            return original(parent, name, **kwargs)

        monkeypatch.setattr(publisher, "_open_child_directory", fail_themes)
        expected = "themes"
    else:
        name = "_personal_read" if fault == "read" else "_personal_write"
        original = getattr(publisher, name)

        def fail_cli(root, relative, *args):
            if str(relative) == "cli.json":
                if fault == "capture-collision":
                    raise publisher.PublishError(publisher.DriftClass.COLLISION)
                raise OSError("injected capture/read failure")
            return original(root, relative, *args)

        monkeypatch.setattr(publisher, name, fail_cli)
        expected = "cli.json"
    assert publisher._personal_snapshot(runtime, capture) == 0
    records = json.loads(capsys.readouterr().out)
    assert len(records) == 1
    assert records[0]["relative"] == expected
    assert records[0]["kind"] == "io"
