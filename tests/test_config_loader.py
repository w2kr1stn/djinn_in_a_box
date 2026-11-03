"""Tests for djinn_in_a_box.config.loader module."""

from __future__ import annotations

import errno
import os
import signal
import socket
import stat
import tempfile
import tomllib
from pathlib import Path

import pytest

from djinn_in_a_box.config import loader
from djinn_in_a_box.config.loader import (
    ConfigNotFoundError,
    ConfigValidationError,
    load_agents,
    load_config,
    save_config,
)
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.console import escape_nonprintable


@pytest.fixture
def sample_agents_toml(tmp_path: Path) -> Path:
    """Create a sample agents.toml file."""
    agents_file = tmp_path / "agents.toml"
    agents_file.write_text(
        """
[agents.test-agent]
binary = "test-cli"
description = "Test Agent"
headless_flags = ["-h"]
read_only_flags = ["--readonly"]
write_flags = ["--write"]
json_flags = ["--json"]
model_flag = "-m"
default_model = "test-model"
prompt_template = "$PROMPT"
"""
    )
    return agents_file


@pytest.fixture
def sample_code_dir(tmp_path: Path) -> Path:
    """Create a sample code directory that exists."""
    code_dir = tmp_path / "projects"
    code_dir.mkdir()
    return code_dir


@pytest.fixture
def valid_config_toml(tmp_path: Path, sample_code_dir: Path) -> Path:
    """Create a valid config.toml with existing code_dir."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[general]
code_dir = "{sample_code_dir}"
timezone = "UTC"

[shell]
skip_mounts = false

[resources]
cpu_limit = 4
memory_limit = "8G"
cpu_reservation = 2
memory_reservation = "4G"
"""
    )
    return config_file


class TestLoadConfig:
    """Tests for load_config function."""

    def test_raises_config_not_found_for_missing_file(self, tmp_path: Path) -> None:
        """Should raise ConfigNotFoundError for missing file."""
        missing_path = tmp_path / "nonexistent.toml"

        with pytest.raises(ConfigNotFoundError) as exc_info:
            load_config(missing_path)

        assert exc_info.value.path == missing_path

    def test_raises_config_validation_error_for_invalid_toml(self, tmp_path: Path) -> None:
        """Should raise ConfigValidationError for invalid TOML syntax."""
        invalid_file = tmp_path / "invalid.toml"
        invalid_file.write_text("this is not valid TOML [[[")

        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(invalid_file)

        assert "Invalid TOML syntax" in str(exc_info.value)

    def test_loads_valid_config(self, valid_config_toml: Path) -> None:
        """Should load and return valid AppConfig."""
        config = load_config(valid_config_toml)

        assert isinstance(config, AppConfig)
        assert config.timezone == "UTC"
        assert config.resources.cpu_limit == 4
        assert config.config_sync.source == "claude"
        assert config.serve.opencode_host_port == 49380

    def test_rejects_old_workspace_mode(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(f'[general]\ncode_dir = "{tmp_path}"\nworkspace = "aios"\n')

        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(config_file)

        assert "workspace: Input should be 'aimi' or 'projects'" in str(exc_info.value)

    @pytest.mark.parametrize("port", ["1023", "65536", '"abc"'])
    def test_rejects_invalid_serve_port(self, valid_config_toml: Path, port: str) -> None:
        with valid_config_toml.open("a", encoding="utf-8") as config_file:
            config_file.write(f"\n[serve]\nopencode_host_port = {port}\n")

        with pytest.raises(ConfigValidationError, match="opencode_host_port"):
            load_config(valid_config_toml)

    def test_loads_selected_config_sync_source(self, valid_config_toml: Path) -> None:
        with valid_config_toml.open("a", encoding="utf-8") as config_file:
            config_file.write('\n[config_sync]\nsource = "opencode"\n')

        config = load_config(valid_config_toml)

        assert config.config_sync.source == "opencode"

    def test_raises_validation_error_for_invalid_values(self, tmp_path: Path) -> None:
        """Should raise ConfigValidationError for invalid field values."""
        invalid_file = tmp_path / "invalid_values.toml"
        invalid_file.write_text(
            """
[general]
code_dir = "/nonexistent/path/that/does/not/exist"
timezone = "Invalid"
"""
        )

        with pytest.raises(ConfigValidationError):
            load_config(invalid_file)


class TestLoadAgents:
    """Tests for load_agents function."""

    def test_raises_error_for_explicit_missing_path(self, tmp_path: Path) -> None:
        """Should raise ConfigNotFoundError for explicit missing path."""
        missing_path = tmp_path / "missing.toml"

        with pytest.raises(ConfigNotFoundError):
            load_agents(missing_path)

    def test_loads_agents_from_toml(self, sample_agents_toml: Path) -> None:
        """Should load agents from TOML file."""
        agents = load_agents(sample_agents_toml)

        assert "test-agent" in agents
        assert agents["test-agent"].binary == "test-cli"
        assert agents["test-agent"].description == "Test Agent"
        assert agents["test-agent"].default_model == "test-model"

    def test_falls_back_to_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Should fall back to DEFAULT_AGENTS when no files exist."""
        monkeypatch.setattr(
            "djinn_in_a_box.config.loader.AGENTS_FILE",
            Path("/nonexistent/agents.toml"),
        )

        agents = load_agents()

        assert set(agents) == {"claude", "codex", "opencode", "claude-isolated"}
        assert agents["codex"].read_only_flags == ["--sandbox", "read-only"]

    def test_raises_validation_error_for_invalid_agents(self, tmp_path: Path) -> None:
        """Should raise ConfigValidationError for invalid agent data."""
        invalid_file = tmp_path / "invalid_agents.toml"
        invalid_file.write_text(
            """
[agents.broken]
# Missing required 'binary' field
description = "Broken agent"
"""
        )

        with pytest.raises(ConfigValidationError):
            load_agents(invalid_file)


@pytest.mark.parametrize("port", [49380, 49381])
def test_serve_port_round_trip(tmp_path: Path, port: int) -> None:
    path = tmp_path / "config.toml"
    config = AppConfig.model_validate({
        "code_dir": tmp_path, "serve": {"opencode_host_port": port},
    })
    save_config(config, path)

    raw = tomllib.loads(path.read_text())
    assert raw["serve"] == {"opencode_host_port": port}
    assert "serve" not in raw["general"]
    assert load_config(path) == config


def assert_agents_refused(path: Path, rule: str, *, default: bool = False) -> str:
    with pytest.raises(ConfigValidationError) as exc_info:
        load_agents(None if default else path)
    message = str(exc_info.value)
    assert f"Agent definitions {str(path)!r} refused:" in message
    assert rule in message
    assert "The host runs these commands" in message
    assert "chmod go-w" in message
    assert "remove it to use the built-in agents" in message
    assert all(character.isprintable() for character in message)
    return message


def assert_agents_content_error(path: Path, detail: str) -> str:
    with pytest.raises(ConfigValidationError) as exc_info:
        load_agents(path)
    message = str(exc_info.value)
    assert message.splitlines()[0].startswith(detail)
    assert escape_nonprintable(str(path)) in message.splitlines()[0]
    assert "chmod go-w" not in message
    assert all(character.isprintable() for line in message.splitlines() for character in line)
    return message


class TestAgentFileSafety:
    @pytest.mark.parametrize("mode", [0o600, 0o644])
    def test_loads_safe_file(self, sample_agents_toml: Path, mode: int) -> None:
        sample_agents_toml.chmod(mode)
        assert set(load_agents(sample_agents_toml)) == {"test-agent"}

    def test_file_symlink(self, sample_agents_toml: Path) -> None:
        link = sample_agents_toml.with_name("link.toml")
        link.symlink_to(sample_agents_toml)
        assert_agents_refused(link, "file is a symlink")

    def test_dangling_symlink_is_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link = tmp_path / "agents.toml"
        link.symlink_to(tmp_path / "missing.toml")
        monkeypatch.setattr(loader, "AGENTS_FILE", link)
        assert_agents_refused(link, "file is a symlink", default=True)

    def test_directory_symlink(self, sample_agents_toml: Path) -> None:
        link = sample_agents_toml.parent / "directory-link"
        link.symlink_to(sample_agents_toml.parent, target_is_directory=True)
        assert_agents_refused(
            link / sample_agents_toml.name, "directory is a symlink or not a directory"
        )

    def test_parent_is_regular_file(self, sample_agents_toml: Path) -> None:
        message = assert_agents_refused(
            sample_agents_toml / "agents.toml", "entry cannot be inspected"
        )
        assert "Not a directory" in message

    def test_fifo_does_not_block(self, tmp_path: Path) -> None:
        path = tmp_path / "agents.toml"
        os.mkfifo(path, 0o600)

        def timed_out(_signum: int, _frame: object) -> None:
            raise TimeoutError("loading agent definitions blocked on a FIFO")

        previous = signal.signal(signal.SIGALRM, timed_out)
        signal.alarm(2)
        try:
            assert_agents_refused(path, "file is not a regular file")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def test_unix_socket(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="da-", dir=os.environ.get("TMPDIR", "/tmp")
        ) as root:
            path = Path(root) / "agents.toml"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                message = assert_agents_refused(path, "file is not a regular file")
                assert "No such device or address" in message

    @pytest.mark.skipif(os.getuid() == 0, reason="root can read mode 0000 files")
    def test_unreadable_file(self, sample_agents_toml: Path) -> None:
        sample_agents_toml.chmod(0o000)
        try:
            message = assert_agents_refused(sample_agents_toml, "file cannot be read")
            assert "Permission denied" in message
        finally:
            sample_agents_toml.chmod(0o600)

    def test_hard_link(self, sample_agents_toml: Path) -> None:
        sample_agents_toml.with_name("alias.toml").hardlink_to(sample_agents_toml)
        assert_agents_refused(sample_agents_toml, "file must have a single link")

    def test_unlinked_descriptor(self, sample_agents_toml: Path, monkeypatch) -> None:
        original = os.open

        def unlinked(path, flags, *args, **kwargs):
            fd = original(path, flags, *args, **kwargs)
            if kwargs.get("dir_fd") is not None:
                sample_agents_toml.unlink()
            return fd

        monkeypatch.setattr(loader.os, "open", unlinked)
        assert_agents_refused(sample_agents_toml, "file must have a single link")

    @pytest.mark.parametrize("mode", [0o620, 0o602], ids=["group", "world"])
    def test_writable_file(self, sample_agents_toml: Path, mode: int) -> None:
        sample_agents_toml.chmod(mode)
        assert_agents_refused(sample_agents_toml, "file is writable by group or others")

    @pytest.mark.parametrize("mode", [0o720, 0o702], ids=["group", "world"])
    def test_writable_directory(self, sample_agents_toml: Path, mode: int) -> None:
        sample_agents_toml.chmod(0o600)
        sample_agents_toml.parent.chmod(mode)
        try:
            assert_agents_refused(sample_agents_toml, "directory is writable by group or others")
        finally:
            sample_agents_toml.parent.chmod(0o700)

    @pytest.mark.parametrize("kind", ["file", "directory"])
    def test_foreign_owner(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch, kind: str
    ) -> None:
        original = os.fstat

        def foreign_owner(fd: int) -> os.stat_result:
            info = original(fd)
            selected = stat.S_ISREG(info.st_mode) if kind == "file" else stat.S_ISDIR(info.st_mode)
            if selected:
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(loader.os, "fstat", foreign_owner)
        assert_agents_refused(sample_agents_toml, f"{kind} is not owned by the invoking user")

    def test_present_then_gone_is_refused(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = os.open

        def vanished(path, flags, *args, **kwargs):
            if kwargs.get("dir_fd") is not None:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
            return original(path, flags, *args, **kwargs)

        monkeypatch.setattr(loader, "AGENTS_FILE", sample_agents_toml)
        monkeypatch.setattr(loader.os, "open", vanished)
        assert_agents_refused(sample_agents_toml, "file changed while loading", default=True)

    def test_reads_checked_descriptor(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = os.open

        def replaced(path, flags, *args, **kwargs):
            fd = original(path, flags, *args, **kwargs)
            if kwargs.get("dir_fd") is not None:
                sample_agents_toml.rename(sample_agents_toml.with_name("checked.toml"))
                replacement = sample_agents_toml.with_name("replacement.toml")
                replacement.write_text('[agents.replacement]\nbinary = "different-cli"\n')
                replacement.replace(sample_agents_toml)
            return fd

        monkeypatch.setattr(loader.os, "open", replaced)
        assert set(load_agents(sample_agents_toml)) == {"test-agent"}
        assert "replacement" in sample_agents_toml.read_text()

    def test_reads_checked_directory(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = sample_agents_toml.parent / "config"
        directory.mkdir(mode=0o700)
        path = sample_agents_toml.rename(directory / "agents.toml")
        original = os.open

        def replaced(parent, flags, *args, **kwargs):
            fd = original(parent, flags, *args, **kwargs)
            if kwargs.get("dir_fd") is None:
                directory.rename(directory.with_name("checked"))
                directory.mkdir(mode=0o700)
                path.write_text('[agents.replacement]\nbinary = "different-cli"\n')
            return fd

        monkeypatch.setattr(loader.os, "open", replaced)
        assert set(load_agents(path)) == {"test-agent"}

    def test_invalid_utf8(self, sample_agents_toml: Path) -> None:
        sample_agents_toml.write_bytes(b"\xff")
        assert_agents_content_error(sample_agents_toml, "Invalid UTF-8 in")

    @pytest.mark.parametrize(
        "content", ['agents = ["x"]', "[agents]\nx = 1", "[agents]", 'other = "x"'],
        ids=["array", "scalar-agent", "empty", "missing"],
    )
    def test_invalid_shape(self, sample_agents_toml: Path, content: str) -> None:
        sample_agents_toml.write_text(content)
        message = assert_agents_content_error(sample_agents_toml, "No agents defined in")
        assert message.endswith(". Expected [agents.<name>] sections.")

    def test_invalid_toml(self, sample_agents_toml: Path) -> None:
        sample_agents_toml.write_text("[agents.")
        assert_agents_content_error(sample_agents_toml, "Invalid TOML syntax in")

    @pytest.mark.parametrize(
        "value,parser_error",
        [("[" * 1200 + '"x"' + "]" * 1200, RecursionError), ("9" * 5000, ValueError)],
        ids=["nested-array", "large-integer"],
    )
    @pytest.mark.parametrize("via_cli", [False, True], ids=["loader", "cli"])
    def test_parser_limits_are_named_content_errors(
        self,
        sample_agents_toml: Path,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
        parser_error: type[Exception],
        via_cli: bool,
    ) -> None:
        from typer.testing import CliRunner

        from djinn_in_a_box.cli.djinn import app

        sample_agents_toml.chmod(0o600)
        sample_agents_toml.parent.chmod(0o700)
        sample_agents_toml.write_text('[agents.test]\nbinary = "claude"\nheadless_flags = ' + value)
        prefix = f"Invalid TOML syntax in {sample_agents_toml}:"
        if via_cli:
            monkeypatch.setattr(loader, "AGENTS_FILE", sample_agents_toml)
            result = CliRunner().invoke(app, ["agents", "--json"])
            assert result.exit_code == 1
            assert result.stdout == ""
            message = result.stderr
            assert prefix in message
        else:
            with pytest.raises(ConfigValidationError) as exc_info:
                load_agents(sample_agents_toml)
            message = str(exc_info.value)
            assert message.startswith(prefix)
            assert isinstance(exc_info.value.__cause__, parser_error)
        assert len(message) <= 400

    def test_long_toml_diagnostic_is_bounded(self, sample_agents_toml: Path) -> None:
        table = "[agents." + "x" * 5000 + "]\n"
        sample_agents_toml.write_text(table * 2)
        message = assert_agents_content_error(sample_agents_toml, "Invalid TOML syntax in")
        assert len(message) <= 400

    def test_invalid_model(self, sample_agents_toml: Path) -> None:
        sample_agents_toml.write_text('[agents.broken]\ndescription = "Missing binary"\n')
        message = assert_agents_content_error(sample_agents_toml, "Invalid agent configuration in")
        assert message.splitlines()[1] == "  - binary: Field required"

    @pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
    def test_missing_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
    ) -> None:
        missing = tmp_path / "missing" / "agents.toml"
        if explicit:
            with pytest.raises(ConfigNotFoundError) as exc_info:
                load_agents(missing)
            assert exc_info.value.path == missing
        else:
            monkeypatch.setattr(loader, "AGENTS_FILE", missing)
            assert set(load_agents()) == {"claude", "codex", "opencode", "claude-isolated"}

    def test_missing_default_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loader, "AGENTS_FILE", tmp_path / "agents.toml")
        assert set(load_agents()) == {"claude", "codex", "opencode", "claude-isolated"}

    def test_cli_refuses_writable_file(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from djinn_in_a_box.cli.djinn import app

        sample_agents_toml.chmod(0o620)
        monkeypatch.setattr(loader, "AGENTS_FILE", sample_agents_toml)
        result = CliRunner().invoke(app, ["agents"])
        assert result.exit_code == 1
        assert "Agent definitions" in result.stderr
        assert str(sample_agents_toml) in result.stderr
        assert "file is writable by group or others" in result.stderr
        assert "chmod go-w" in result.stderr
        assert result.stdout == ""

    @pytest.mark.parametrize(
        ("stage", "rule"),
        [
            ("lstat", "entry cannot be inspected"),
            ("directory-open", "directory open failed"),
            ("directory-stat", "directory metadata check failed"),
            ("file-open", "file cannot be opened"),
            ("file-stat", "file metadata check failed"),
            ("read", "file read failed"),
        ],
    )
    def test_os_errors_are_refusals(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch, stage: str, rule: str
    ) -> None:
        original_open, original_stat = os.open, os.fstat
        opened: list[int] = []

        def fail() -> None:
            raise OSError(errno.EIO, os.strerror(errno.EIO))

        def failing_open(path, flags, *args, **kwargs):
            kind = "file" if kwargs.get("dir_fd") is not None else "directory"
            if stage == f"{kind}-open":
                fail()
            fd = original_open(path, flags, *args, **kwargs)
            opened.append(fd)
            return fd

        def failing_stat(fd):
            info = original_stat(fd)
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
            if stage == f"{kind}-stat":
                fail()
            return info

        with monkeypatch.context() as patch:
            patch.setattr(loader, "AGENTS_FILE", sample_agents_toml)
            patch.setattr(loader.os, "open", failing_open)
            patch.setattr(loader.os, "fstat", failing_stat)
            if stage == "lstat":
                patch.setattr(loader.os, "lstat", lambda *_args, **_kwargs: fail())
            if stage == "read":
                patch.setattr(loader.os, "read", lambda *_args: fail())
            message = assert_agents_refused(sample_agents_toml, rule, default=True)
            assert "Input/output error" in message
        for fd in opened:
            with pytest.raises(OSError) as exc_info:
                original_stat(fd)
            assert exc_info.value.errno == errno.EBADF

    def test_directory_metadata_must_describe_directory(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = os.fstat

        def changed_type(fd):
            info = original(fd)
            if stat.S_ISDIR(info.st_mode):
                fields = list(info)
                fields[0] = stat.S_IFREG | 0o700
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(loader.os, "fstat", changed_type)
        assert_agents_refused(sample_agents_toml, "containing directory is not a directory")

    def test_directory_eloop_is_refused(self, sample_agents_toml: Path, monkeypatch) -> None:
        def loop(path, *_args, **_kwargs):
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), path)

        monkeypatch.setattr(loader.os, "open", loop)
        message = assert_agents_refused(
            sample_agents_toml, "directory is a symlink or not a directory"
        )
        assert "Too many levels of symbolic links" in message

    def test_rejection_closes_descriptors(self, sample_agents_toml: Path, monkeypatch) -> None:
        sample_agents_toml.chmod(0o620)
        original_open, original_stat = os.open, os.fstat
        opened: list[int] = []

        def tracked(path, flags, *args, **kwargs):
            fd = original_open(path, flags, *args, **kwargs)
            opened.append(fd)
            return fd

        with monkeypatch.context() as patch:
            patch.setattr(loader.os, "open", tracked)
            assert_agents_refused(sample_agents_toml, "file is writable by group or others")
        assert len(opened) == 2
        for fd in opened:
            with pytest.raises(OSError) as exc_info:
                original_stat(fd)
            assert exc_info.value.errno == errno.EBADF

    @pytest.mark.parametrize(
        ("kind", "flag"),
        [
            ("directory", os.O_RDONLY),
            ("directory", os.O_DIRECTORY),
            ("directory", os.O_CLOEXEC),
            ("file", os.O_RDONLY),
            ("file", os.O_CLOEXEC),
        ],
        ids=["directory-readonly", "directory-only", "directory-cloexec", "file-readonly",
             "file-cloexec"],
    )
    def test_open_flags(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch, kind: str, flag: int
    ) -> None:
        original = os.open
        seen = False

        def checked_open(path, flags, *args, **kwargs):
            nonlocal seen
            selected = "file" if kwargs.get("dir_fd") is not None else "directory"
            if selected == kind:
                seen = True
                if flag == os.O_RDONLY:
                    assert flags & os.O_ACCMODE == os.O_RDONLY
                else:
                    assert flags & flag
            return original(path, flags, *args, **kwargs)

        monkeypatch.setattr(loader.os, "open", checked_open)
        assert set(load_agents(sample_agents_toml)) == {"test-agent"}
        assert seen

    def test_descriptor_lifecycle(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_open, original_stat, original_read, original_close = (
            os.open, os.fstat, os.read, os.close
        )
        descriptors: dict[str, int] = {}
        events: list[str] = []

        def checked_open(path, flags, *args, **kwargs):
            if kwargs.get("dir_fd") is None:
                assert path == sample_agents_toml.parent
                kind = "directory"
            else:
                assert path == sample_agents_toml.name
                assert kwargs["dir_fd"] == descriptors["directory"]
                assert events == ["directory-open", "directory-stat"]
                kind = "file"
            fd = original_open(path, flags, *args, **kwargs)
            descriptors[kind] = fd
            events.append(f"{kind}-open")
            return fd

        def checked_stat(fd):
            kind = "directory" if fd == descriptors["directory"] else "file"
            events.append(f"{kind}-stat")
            return original_stat(fd)

        def checked_read(fd, size):
            assert fd == descriptors["file"]
            assert events[:4] == ["directory-open", "directory-stat", "file-open", "file-stat"]
            events.append("read")
            return original_read(fd, size)

        def checked_close(fd):
            kind = "directory" if fd == descriptors["directory"] else "file"
            events.append(f"{kind}-close")
            return original_close(fd)

        with monkeypatch.context() as patch:
            patch.setattr(loader.os, "open", checked_open)
            patch.setattr(loader.os, "fstat", checked_stat)
            patch.setattr(loader.os, "read", checked_read)
            patch.setattr(loader.os, "close", checked_close)
            assert set(load_agents(sample_agents_toml)) == {"test-agent"}
        assert events[-2:] == ["file-close", "directory-close"]
        for fd in descriptors.values():
            with pytest.raises(OSError) as exc_info:
                original_stat(fd)
            assert exc_info.value.errno == errno.EBADF

    def test_reads_until_eof(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = os.read
        reads = 0

        def short_read(fd, size):
            nonlocal reads
            reads += 1
            return original(fd, min(size, 7))

        monkeypatch.setattr(loader.os, "read", short_read)
        assert set(load_agents(sample_agents_toml)) == {"test-agent"}
        assert reads > 2

    def test_refusal_escapes_control_characters(
        self, sample_agents_toml: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = sample_agents_toml.with_name("agents\n\x1b.toml")
        sample_agents_toml.rename(path)

        def denied(*_args, **_kwargs):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))

        with monkeypatch.context() as patch:
            patch.setattr(loader.os, "open", denied)
            assert_agents_refused(path, "Permission denied")
        path.write_text('[agents.broken]\ndescription = "Missing binary"\nheadless_flags = 4\n')
        message = assert_agents_content_error(path, "Invalid agent configuration in")
        assert "\\n\\x1b.toml" in message.splitlines()[0]


class TestSaveConfig:
    """Tests for save_config function."""

    def test_saves_config_to_file(self, tmp_path: Path, sample_code_dir: Path) -> None:
        """Should save AppConfig to TOML file."""
        config = AppConfig(code_dir=sample_code_dir, workspace="aimi", timezone="UTC")
        output_path = tmp_path / "output.toml"

        save_config(config, output_path)

        assert output_path.exists()
        content = output_path.read_text()
        assert "[general]" in content
        assert "[config_sync]" in content
        assert 'source = "claude"' in content
        assert "UTC" in content
        data = tomllib.loads(content)
        assert data["general"]["workspace"] == "aimi"
        assert "workspace" not in data
        assert "workspace_target" not in content
        assert load_config(output_path) == config

    def test_creates_parent_directories(self, tmp_path: Path, sample_code_dir: Path) -> None:
        """Should create parent directories if they don't exist."""
        config = AppConfig(code_dir=sample_code_dir)
        output_path = tmp_path / "nested" / "dir" / "config.toml"

        save_config(config, output_path)

        assert output_path.exists()


@pytest.mark.parametrize(
    "kind", ["populated", "empty", "duplicate-table", "duplicate-key", "unquoted-dot"]
)
def test_declarations_round_trip(tmp_path, kind):
    import tomllib

    import tomli_w

    from djinn_in_a_box.core.exceptions import ConfigValidationError

    path = tmp_path / "config.toml"
    mounts = (
        {
            "archive.disk": {
                "source": "/offline", "target": "/archive", "marker": ".ready", "read_only": True,
            },
            "plain": {"source": "/offline2", "target": "/plain"},
            "explicit-rw": {"source": "/offline3", "target": "/rw", "read_only": False},
            "worker": {"volume": True, "target": "/worker", "backup": "none"},
        }
        if kind != "empty"
        else {}
    )
    environment = {"CDP_HOST": "$HOST\n[value]", "EMPTY": ""} if kind != "empty" else {}
    path.write_text(tomli_w.dumps({
        "general": {"code_dir": str(tmp_path)}, "mounts": mounts, "environment": environment,
    }))
    config = load_config(path)
    save_config(config, path)
    assert load_config(path) == config
    raw = tomllib.loads(path.read_text())
    assert raw["mounts"] == ({
        "archive.disk": {
            "source": "/offline", "target": "/archive", "marker": ".ready", "read_only": True,
        },
        "plain": {"source": "/offline2", "target": "/plain"},
        "explicit-rw": {"source": "/offline3", "target": "/rw"},
        "worker": {"volume": True, "target": "/worker", "backup": "none"},
    } if kind != "empty" else {})
    assert "read_only = false" not in path.read_text()
    if kind != "empty":
        reloaded = load_config(path)
        assert reloaded.mounts["archive.disk"].read_only is True
        assert reloaded.mounts["plain"].read_only is False
        assert reloaded.mounts["explicit-rw"].read_only is False
    assert raw["environment"] == environment
    assert "mounts" not in raw["general"] and "environment" not in raw["general"]
    if kind == "populated":
        assert "marker" not in raw["mounts"]["plain"]
        assert set(raw["mounts"]["worker"]) == {"volume", "target", "backup"}
    if kind == "duplicate-table":
        path.write_text(path.read_text() + '\n[environment]\nX="x"\n')
    elif kind == "duplicate-key":
        path.write_text(path.read_text() + '\nCDP_HOST="duplicate"\n')
    elif kind == "unquoted-dot":
        path.write_text(
            f'[general]\ncode_dir="{tmp_path}"\n[mounts.archive.disk]\nsource="/x"\ntarget="/y"\n'
        )
    else:
        return
    with pytest.raises(ConfigValidationError):
        load_config(path)
