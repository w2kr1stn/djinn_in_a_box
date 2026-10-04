"""Tests for the djinn CLI entry point."""

import errno
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from djinn_in_a_box import __version__
from djinn_in_a_box.cli.djinn import app
from djinn_in_a_box.config.loader import load_config as load_config_file
from djinn_in_a_box.config.loader import save_config as save_config_file
from djinn_in_a_box.config.models import AppConfig, ResourceLimits, ShellConfig
from djinn_in_a_box.core import config_lock
from djinn_in_a_box.core.config_workflow import prepare_config_workflow
from djinn_in_a_box.core.docker import WorkflowImageCompatibility, ensure_host_env
from djinn_in_a_box.core.seeding import seed_config

runner = CliRunner()


def test_project_scripts_are_exact() -> None:
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)
    assert set(project["project"]["scripts"]) == {"fmt", "test", "clean", "djinn"}


def test_top_level_commands_are_exact() -> None:
    from typer.main import get_command

    assert set(get_command(app).commands) == {
        "init",
        "config",
        "build",
        "start",
        "status",
        "doctor",
        "audit",
        "update",
        "enter",
        "clean",
        "backup",
        "restore",
        "run",
        "agents",
        "session",
    }



def _patch_init_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    config_dir: Path,
    config_file: Path,
    saved_configs: list[AppConfig] | None = None,
) -> None:
    monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_FILE", config_file)
    monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("djinn_in_a_box.commands.config.ensure_host_env", MagicMock())
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: config_dir)
    monkeypatch.setattr(
        "djinn_in_a_box.commands.config.seed_config",
        MagicMock(return_value=[]),
    )
    monkeypatch.setattr("djinn_in_a_box.commands.config.detect_timezone", lambda: "UTC")
    monkeypatch.setattr(
        "djinn_in_a_box.commands.config.suggest_resources",
        lambda: ResourceLimits(),
    )

    def mock_save_config(config: AppConfig) -> None:
        if saved_configs is not None:
            saved_configs.append(config)
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("[general]\ncode_dir = '/test'\n")

    monkeypatch.setattr("djinn_in_a_box.commands.config.save_config", mock_save_config)


def _write_test_config(config_file: Path, projects_dir: Path) -> AppConfig:
    config = AppConfig(
        code_dir=projects_dir,
        timezone="UTC",
        resources=ResourceLimits(),
        shell=ShellConfig(),
    )
    save_config_file(config, config_file)
    return config


@pytest.fixture
def config_edit_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Provide the project-local config directory required by ``config edit``."""
    project_root = tmp_path / "project"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    get_project_root = MagicMock(return_value=project_root)
    monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", get_project_root)

    yield config_dir

    # Both halves must be load-bearing. Without the mkdir check the stubbed-lock
    # test would pass with no directory; without the call check, dropping the
    # patch would leave every test green locally — on the real ./config — which
    # is exactly how this bug reached CI. `assert_called` rather than
    # `assert_called_once`: the count carries no signal, only coupling.
    assert config_dir.is_dir()
    get_project_root.assert_called()


class TestDjinnVersion:
    """Tests for the --version flag."""

    def test_version_short_flag(self) -> None:
        result = runner.invoke(app, ["-V"])
        assert result.exit_code == 0
        assert "djinn" in result.stdout
        assert __version__ in result.stdout
        assert "\n" in result.stdout


class TestInitCommand:
    """Tests for the init command."""

    def test_fresh_init_seeds_regular_instruction_file_before_compose(
        self, mock_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = mock_home / "djinn"
        shutil.copytree(
            Path(__file__).resolve().parents[1] / "templates/seed",
            project / "templates/seed",
        )
        config_dir = mock_home / ".config/djinn_in_a_box"
        config_file = config_dir / "config.toml"
        _patch_init_dependencies(monkeypatch, config_dir, config_file)
        monkeypatch.delenv("DJINN_CONFIG_ROOT", raising=False)
        monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: project)
        monkeypatch.setattr("djinn_in_a_box.commands.config.seed_config", seed_config)
        monkeypatch.setattr("djinn_in_a_box.commands.config.ensure_host_env", ensure_host_env)
        monkeypatch.setattr(
            "djinn_in_a_box.commands.config.save_config",
            lambda config: save_config_file(config, config_file),
        )
        monkeypatch.setattr("djinn_in_a_box.config.zones.ZONES_FILE", config_dir / "zones.toml")
        projects_dir = mock_home / "projects"
        projects_dir.mkdir()

        result = runner.invoke(app, ["init"], input=f"projects\n{projects_dir}\nUTC\nn\n")

        assert result.exit_code == 0, result.output
        instructions = project / "config/claude/AGENTS.md"
        assert instructions.is_file()
        assert stat.S_ISREG(instructions.lstat().st_mode)
        content = instructions.read_text()
        assert "read the relevant material in its `.agents/` directory." in content
        assert not (instructions.parent / "CLAUDE.md").exists()
        prepared = prepare_config_workflow(
            project,
            config_path=config_file,
            require_compose_host_env=True,
            container_image_compatibility=WorkflowImageCompatibility.COMPATIBLE,
        )
        assert prepared.success
        assert stat.S_ISREG(instructions.lstat().st_mode)
        assert instructions.read_text() == content

    @pytest.mark.parametrize("workspace", ["projects", "aios", None])
    def test_init_creates_config_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        workspace: str | None,
    ) -> None:
        config_dir = tmp_path / ".config" / "djinn_in_a_box"
        config_file = config_dir / "config.toml"
        _patch_init_dependencies(monkeypatch, config_dir, config_file)

        selected = workspace or "projects"
        monkeypatch.setenv("HOME", str(tmp_path))
        projects_dir = tmp_path / selected
        projects_dir.mkdir()

        monkeypatch.setattr(
            "djinn_in_a_box.commands.config.save_config",
            lambda config: save_config_file(config, config_file),
        )
        result = runner.invoke(app, ["init"], input=f"AIOS\n{workspace or ''}\n\nUTC\nn\n")

        assert result.exit_code == 0
        assert config_file.exists()
        combined = result.stdout + result.output
        assert "'AIOS' is not one of" in combined
        root_label = "AIOS root" if selected == "aios" else "Projects directory"
        assert f"{root_label} (mounted as /home/dev/{selected})" in combined
        assert f"[{projects_dir}]" in combined
        assert combined.index("Workspace mode") < combined.index(root_label)
        saved = load_config_file(config_file)
        assert saved.workspace == selected
        assert saved.code_dir == projects_dir
        assert "Next steps" in combined
        assert "djinn build" in combined
        assert "djinn start" in combined
        assert "(optional)" not in combined
        assert combined.index("djinn build") < combined.index("djinn start")

    def test_init_reprompts_for_invalid_workspace_modes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / ".config" / "djinn_in_a_box"
        config_file = config_dir / "config.toml"
        _patch_init_dependencies(monkeypatch, config_dir, config_file)

        aios_root = tmp_path / "aios"
        aios_root.mkdir()
        monkeypatch.setattr(
            "djinn_in_a_box.commands.config.save_config",
            lambda config: save_config_file(config, config_file),
        )

        result = runner.invoke(
            app,
            ["init"],
            input=f"AIOS\nx\naios\n{aios_root}\nUTC\nn\n",
        )

        assert result.exit_code == 0, result.output
        combined = result.stdout + result.output
        assert combined.count("Workspace mode (aios/projects)") >= 3
        assert "'AIOS' is not one of: aios, projects." in combined
        assert "'x' is not one of: aios, projects." in combined
        assert load_config_file(config_file).workspace == "aios"

    def test_init_force_overwrites(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".config" / "djinn_in_a_box"
        config_file = config_dir / "config.toml"
        config_dir.mkdir(parents=True)
        config_file.write_text("[old]\ndata = 'old'\n")
        _patch_init_dependencies(monkeypatch, config_dir, config_file)

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        result = runner.invoke(
            app, ["init", "--force"], input=f"projects\n{projects_dir}\nUTC\nn\n"
        )

        assert result.exit_code == 0
        assert config_file.exists()

    def test_init_invalid_timezone_exits_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / ".config" / "djinn_in_a_box"
        config_file = config_dir / "config.toml"
        _patch_init_dependencies(monkeypatch, config_dir, config_file)

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        result = runner.invoke(app, ["init"], input=f"projects\n{projects_dir}\nBerlin\nn\n")

        assert result.exit_code == 1
        combined = result.stdout + result.output
        assert "Unknown timezone" in combined
        assert "Traceback" not in combined

    def test_init_config_dir_create_error_exits_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocked_parent = tmp_path / "blocked"
        blocked_parent.write_text("not a directory")
        config_dir = blocked_parent / "djinn_in_a_box"
        config_file = config_dir / "config.toml"
        _patch_init_dependencies(monkeypatch, config_dir, config_file)

        result = runner.invoke(app, ["init"])

        assert result.exit_code == 1
        combined = result.stdout + result.output
        assert "Failed to create configuration directory" in combined
        assert "writable" in combined
        assert "Traceback" not in combined

    def test_init_prompts_overwrite_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / ".config" / "djinn_in_a_box"
        config_file = config_dir / "config.toml"
        config_dir.mkdir(parents=True)
        config_file.write_text("[existing]\nconfig = 'yes'\n")
        _patch_init_dependencies(monkeypatch, config_dir, config_file)

        result = runner.invoke(app, ["init"], input="n\n")

        assert result.exit_code == 1

    def test_init_creates_nonexistent_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / ".config" / "djinn_in_a_box"
        config_file = config_dir / "config.toml"
        _patch_init_dependencies(monkeypatch, config_dir, config_file)

        nonexistent = tmp_path / "new_projects"

        result = runner.invoke(app, ["init"], input=f"projects\n{nonexistent}\nUTC\nn\ny\n")

        assert result.exit_code == 0
        assert nonexistent.exists()

    @pytest.mark.parametrize("workspace", ["projects", "aios"])
    def test_init_project_dir_create_error_exits_cleanly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        workspace: str,
    ) -> None:
        config_dir = tmp_path / ".config" / "djinn_in_a_box"
        config_file = config_dir / "config.toml"
        _patch_init_dependencies(monkeypatch, config_dir, config_file)
        blocked_parent = tmp_path / "blocked"
        blocked_parent.write_text("not a directory")
        projects_dir = blocked_parent / workspace

        result = runner.invoke(app, ["init"], input=f"{workspace}\n{projects_dir}\nUTC\nn\ny\n")

        assert result.exit_code == 1
        combined = result.stdout + result.output
        root_label = "AIOS root" if workspace == "aios" else "Projects directory"
        assert f"Failed to create {root_label}" in combined
        assert "writable" in combined
        assert "Traceback" not in combined

    def test_init_advanced_values_are_saved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / ".config" / "djinn_in_a_box"
        config_file = config_dir / "config.toml"
        saved_configs: list[AppConfig] = []
        _patch_init_dependencies(monkeypatch, config_dir, config_file, saved_configs)

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        result = runner.invoke(
            app,
            ["init"],
            input=f"projects\n{projects_dir}\nUTC\ny\n2\n4G\n1\n1G\ny\n",
        )

        assert result.exit_code == 0
        assert len(saved_configs) == 1
        saved = saved_configs[0]
        assert saved.resources.cpu_limit == 2
        assert saved.resources.memory_limit == "4G"
        assert saved.resources.cpu_reservation == 1
        assert saved.resources.memory_reservation == "1G"
        assert saved.shell.skip_mounts is True


class TestConfigShowCommand:
    """Tests for the config show command."""

    @pytest.mark.parametrize("workspace", ["projects", "aios"])
    def test_config_show_displays_values(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        workspace: str,
    ) -> None:
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        mock_config = AppConfig(
            code_dir=projects_dir,
            **{"workspace": workspace},
            timezone="UTC",
            resources=ResourceLimits(),
            shell=ShellConfig(),
        )

        monkeypatch.setattr("djinn_in_a_box.commands.config.load_config", lambda: mock_config)

        result = runner.invoke(app, ["config", "show"])

        assert result.exit_code == 0
        combined = result.stdout + result.output
        # Long paths soft-wrap inside Rich table cells with indented
        # continuation lines (CI runners have longer tmp paths) — strip all
        # whitespace so a break mid-word cannot split the expected token.
        unwrapped = "".join(combined.split())
        assert "projects" in unwrapped
        assert f"workspace{workspace}" in unwrapped
        assert "UTC" in unwrapped
        assert "ConfigSync" in unwrapped
        assert "claude" in unwrapped

    @pytest.mark.parametrize("workspace", ["projects", "aios"])
    def test_config_show_json_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        workspace: str,
    ) -> None:
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        mock_config = AppConfig(
            code_dir=projects_dir,
            **{"workspace": workspace},
            timezone="UTC",
            resources=ResourceLimits(),
            shell=ShellConfig(),
        )

        monkeypatch.setattr("djinn_in_a_box.commands.config.load_config", lambda: mock_config)

        result = runner.invoke(app, ["config", "show", "--json"])

        assert result.exit_code == 0
        import json

        data = json.loads(result.stdout)
        assert "code_dir" in data
        assert "timezone" in data
        assert data["workspace"] == workspace
        assert "workspace_target" not in data
        assert data["config_sync"] == {"source": "claude"}

    def test_config_show_missing_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from djinn_in_a_box.core.exceptions import ConfigNotFoundError

        config_file = tmp_path / "nonexistent" / "config.toml"

        def mock_load_config() -> AppConfig:
            raise ConfigNotFoundError(config_file)

        monkeypatch.setattr("djinn_in_a_box.commands.config.load_config", mock_load_config)

        result = runner.invoke(app, ["config", "show"])

        assert result.exit_code == 1
        combined = result.stdout + result.output
        assert "Configuration not found" in combined


class TestConfigSetCommand:
    """Tests for the config set command."""

    @pytest.mark.parametrize(
        "value", ["projects", "aios", "both", "", "AIOS", "PROJECTS", " aios "]
    )
    def test_config_set_workspace_round_trip_and_rejection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
    ) -> None:
        config_file = tmp_path / "config.toml"
        config = AppConfig.model_validate(
            {
                "code_dir": tmp_path,
                "workspace": "aios",
                "resources": {"cpu_limit": 6, "memory_limit": "8G"},
                "shell": {"skip_mounts": True},
                "config_sync": {"source": "codex"},
                "build": {"network": "host"},
            }
        )
        save_config_file(config, config_file)
        original = config_file.read_bytes()
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)
        result = runner.invoke(app, ["config", "set", "general.workspace", value])
        if value not in {"projects", "aios"}:
            assert result.exit_code == 1, result.output
            assert "Traceback" not in result.output
            assert config_file.read_bytes() == original
            return

        assert result.exit_code == 0, result.output
        assert f"general.workspace = {value}" in result.output
        data = tomllib.loads(config_file.read_text())
        assert data["general"]["workspace"] == value
        assert "workspace" not in data
        expected = {**config.model_dump(mode="json"), "workspace": value}
        assert load_config_file(config_file).model_dump(mode="json") == expected
        shown = runner.invoke(app, ["config", "show", "--json"])
        assert shown.exit_code == 0, shown.output
        import json

        assert json.loads(shown.stdout) == expected
        shown = runner.invoke(app, ["config", "show"])
        assert shown.exit_code == 0, shown.output
        assert f"workspace{value}" in "".join(shown.output.split())
        changed = runner.invoke(app, ["config", "set", "general.timezone", "Europe/Berlin"])
        assert changed.exit_code == 0, changed.output
        assert load_config_file(config_file).model_dump(mode="json") == {
            **expected,
            "timezone": "Europe/Berlin",
        }
        unknown = runner.invoke(app, ["config", "set", "unknown.key", "value"])
        assert unknown.exit_code == 1, unknown.output
        assert "general.workspace" in "".join(unknown.output.split())

    def test_config_set_round_trips_valid_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

        result = runner.invoke(app, ["config", "set", "resources.cpu_limit", "2"])

        assert result.exit_code == 0
        updated = load_config_file(config_file)
        assert updated.resources.cpu_limit == 2

    def test_config_set_round_trips_source_under_exclusive_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)
        project_root = tmp_path / "project"
        config_dir = project_root / "config"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: project_root)
        lock_calls: list[tuple[Path, bool]] = []

        @contextmanager
        def record_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
            lock_calls.append((path, exclusive))
            yield

        monkeypatch.setattr("djinn_in_a_box.commands.config.config_directory_lock", record_lock)

        result = runner.invoke(app, ["config", "set", "config_sync.source", "codex"])

        assert result.exit_code == 0, result.output
        assert load_config_file(config_file).config_sync.source == "codex"
        assert lock_calls == [(config_dir, True)]

    def test_config_set_reports_unlock_failure_after_writing_without_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)
        project_root = tmp_path / "project"
        config_dir = project_root / "config"
        config_dir.mkdir(parents=True)
        monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: project_root)
        original_flock = config_lock.fcntl.flock

        def fail_unlock(descriptor: int, operation: int) -> None:
            if operation == config_lock.fcntl.LOCK_UN:
                raise OSError(errno.EINTR, "Interrupted system call")
            original_flock(descriptor, operation)

        monkeypatch.setattr(config_lock.fcntl, "flock", fail_unlock)

        result = runner.invoke(app, ["config", "set", "config_sync.source", "codex"])

        assert result.exit_code == 1, result.output
        assert load_config_file(config_file).config_sync.source == "codex"
        assert "Configuration value was written, but releasing its lock failed." in result.output
        assert "Interrupted system call" in result.output
        assert "config_sync.source = codex" not in result.output
        assert "Traceback" not in result.output

    def test_config_set_rejects_unknown_source_without_writing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        original = config_file.read_bytes()
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)
        project_root = tmp_path / "project"
        (project_root / "config").mkdir(parents=True)
        monkeypatch.setattr("djinn_in_a_box.commands.config.get_project_root", lambda: project_root)

        result = runner.invoke(app, ["config", "set", "config_sync.source", "unknown"])

        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert config_file.read_bytes() == original

    @pytest.mark.parametrize("workspace", ["projects", "aios"])
    def test_config_set_code_dir_requires_existing_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        workspace: str,
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        save_config_file(
            AppConfig.model_validate({"code_dir": projects_dir, "workspace": workspace}),
            config_file,
        )
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

        missing = tmp_path / "missing-projects"

        result = runner.invoke(app, ["config", "set", "general.code_dir", str(missing)])

        assert result.exit_code == 1
        combined = result.stdout + result.output
        root_label = "AIOS root" if workspace == "aios" else "Projects directory"
        assert f"{root_label} does not exist" in combined
        assert "mkdir -p" in combined
        assert str(missing) in combined
        assert "djinn init" in combined
        updated = load_config_file(config_file)
        assert updated.code_dir == projects_dir

    def test_config_set_code_dir_accepts_existing_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        new_projects_dir = tmp_path / "new-projects"
        projects_dir.mkdir()
        new_projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

        result = runner.invoke(
            app,
            ["config", "set", "general.code_dir", str(new_projects_dir)],
        )

        assert result.exit_code == 0, result.output
        updated = load_config_file(config_file)
        assert updated.code_dir == new_projects_dir

    def test_config_set_config_root_warns_about_old_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        config = _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)
        new_root = tmp_path / "new-config-root"

        result = runner.invoke(
            app,
            ["config", "set", "general.config_root", str(new_root)],
        )

        assert result.exit_code == 0, result.output
        combined = result.stdout + result.output
        assert "Existing credentials/config remain" in combined
        assert str(config.config_root) in combined
        assert "new" in combined
        assert "empty directories will be provisioned" in combined
        updated = load_config_file(config_file)
        assert updated.config_root == new_root.resolve()

    def test_config_set_invalid_value_exits_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

        result = runner.invoke(app, ["config", "set", "resources.cpu_limit", "0"])

        assert result.exit_code == 1
        combined = result.stdout + result.output
        assert "Traceback" not in combined

    def test_config_set_unknown_key_lists_allowed_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

        result = runner.invoke(app, ["config", "set", "unknown.key", "value"])

        assert result.exit_code == 1
        combined = result.stdout + result.output
        assert "Allowed keys" in combined
        assert "general.code_dir" in combined

    def test_config_set_omp_theme_null_clears_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        config = AppConfig(
            code_dir=projects_dir,
            timezone="UTC",
            resources=ResourceLimits(),
            shell=ShellConfig(omp_theme_path=tmp_path / "theme.omp.json"),
        )
        save_config_file(config, config_file)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

        result = runner.invoke(app, ["config", "set", "shell.omp_theme_path", "null"])

        assert result.exit_code == 0, result.output
        updated = load_config_file(config_file)
        assert updated.shell.omp_theme_path is None
        assert "shell.omp_theme_path" in result.output
        assert "None" in result.output

    def test_config_set_save_error_exits_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        config = _write_test_config(config_file, projects_dir)

        monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("djinn_in_a_box.commands.config.load_config", lambda: config)

        def fail_save(_config: AppConfig) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr("djinn_in_a_box.commands.config.save_config", fail_save)

        result = runner.invoke(app, ["config", "set", "general.timezone", "UTC"])

        assert result.exit_code == 1
        combined = result.stdout + result.output
        assert "Failed to write configuration" in combined
        assert "config.toml" in combined
        assert "writable" in combined
        assert "Traceback" not in combined


@pytest.mark.usefixtures("config_edit_project")
class TestConfigEditCommand:
    """Tests for the config edit command.

    The class-level usefixtures is deliberate: `config edit` locks the
    project-local `config/`, which is git-ignored and therefore present on every
    developer machine and absent on a fresh checkout. A test that forgets the
    fixture locks the real one, passes locally and fails on CI — which is how
    this class went red for ten days. Applying it class-wide means a new test
    cannot reintroduce that by omission. Tests needing the path still request
    the fixture by name and get the same instance.
    """

    def test_config_edit_warns_when_editor_corrupts_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_edit_project: Path
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

        editor_script = tmp_path / "corrupt-config.sh"
        editor_script.write_text("#!/bin/sh\nprintf '%s\\n' 'not = [' > \"$1\"\n")
        editor_script.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(editor_script))

        result = runner.invoke(app, ["config", "edit"])

        assert result.exit_code == 0
        combined = result.stdout + result.output
        assert "Configuration problem after edit" in combined
        assert "ConfigValidationError" in combined
        assert "Invalid TOML" in combined

    def test_config_edit_splits_editor_command_with_arguments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_edit_project: Path
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

        editor_script = tmp_path / "append-comment.sh"
        editor_script.write_text(
            "#!/bin/sh\n"
            "test \"$1\" = --marker || exit 3\n"
            "printf '%s\\n' '# edited' >> \"$2\"\n"
        )
        editor_script.chmod(0o755)
        monkeypatch.setenv("EDITOR", f"{editor_script} --marker")

        result = runner.invoke(app, ["config", "edit"])

        assert result.exit_code == 0, result.output
        assert "# edited" in config_file.read_text()

    def test_config_edit_holds_exclusive_config_directory_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_edit_project: Path
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)
        events: list[str] = []

        @contextmanager
        def record_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
            assert path == config_edit_project
            assert exclusive is True
            events.append("lock")
            yield
            events.append("unlock")

        def run_editor(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            events.append("editor")
            return subprocess.CompletedProcess([], 0)

        monkeypatch.setattr("djinn_in_a_box.commands.config.config_directory_lock", record_lock)
        monkeypatch.setattr("djinn_in_a_box.commands.config.subprocess.run", run_editor)

        result = runner.invoke(app, ["config", "edit"])

        assert result.exit_code == 0, result.output
        assert events == ["lock", "editor", "unlock"]

    def test_config_edit_reports_lock_acquisition_failure_without_traceback(
        self, monkeypatch: pytest.MonkeyPatch, config_edit_project: Path
    ) -> None:
        def fail_acquisition(_descriptor: int, _operation: int) -> None:
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(config_lock.fcntl, "flock", fail_acquisition)

        result = runner.invoke(app, ["config", "edit"])

        assert result.exit_code == 1, result.output
        assert str(config_edit_project) in result.output
        normalized = " ".join(result.output.split())
        assert "No locks available" in normalized
        assert "djinn init" not in normalized
        assert "Traceback" not in normalized

    def test_config_edit_warns_when_editor_deletes_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_edit_project: Path
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)

        editor_script = tmp_path / "delete-config.sh"
        editor_script.write_text("#!/bin/sh\nrm \"$1\"\n")
        editor_script.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(editor_script))

        result = runner.invoke(app, ["config", "edit"])

        assert result.exit_code == 0, result.output
        combined = result.stdout + result.output
        assert "Configuration problem after edit" in combined
        assert "ConfigNotFoundError" in combined
        assert not config_file.exists()

    def test_config_edit_bad_editor_exits_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_edit_project: Path
    ) -> None:
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)
        monkeypatch.setenv("EDITOR", "'unterminated")

        result = runner.invoke(app, ["config", "edit"])

        assert result.exit_code == 1
        combined = result.stdout + result.output
        assert "Cannot run editor" in combined
        assert "$EDITOR" in combined
        assert "No closing quotation" in combined
        assert "Traceback" not in combined

class TestConfigEditWithoutProjectConfig:
    """`config edit` on a clone that never ran `djinn init`.

    Deliberately outside `TestConfigEditCommand`: that class applies
    `config_edit_project` to every test, and the whole point here is the
    directory's absence.
    """

    def test_config_edit_without_project_config_dir_is_actionable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clone that never ran `djinn init` must get guidance, not a traceback.

        `config/` is git-ignored blank space, so this is the state of every fresh
        clone.
        """
        config_file = tmp_path / "config.toml"
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        _write_test_config(config_file, projects_dir)
        monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", config_file)
        bare_root = tmp_path / "bare-project"
        bare_root.mkdir()
        monkeypatch.setattr(
            "djinn_in_a_box.commands.config.get_project_root", lambda: bare_root
        )

        result = runner.invoke(app, ["config", "edit"])

        assert result.exit_code == 1
        combined = result.stdout + result.output
        assert "config" in combined
        assert "djinn init" in combined
        assert "Traceback" not in combined


class TestConfigPathCommand:
    """Tests for the config path command."""

    def test_config_path_shows_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_FILE", config_file)

        result = runner.invoke(app, ["config", "path"])

        assert result.exit_code == 0
        assert str(config_file) in result.stdout


@pytest.mark.parametrize("format_name", ["text", "json"])
def test_show_declarations(tmp_path, monkeypatch, format_name):
    import json

    config = AppConfig(
        code_dir=tmp_path,
        mounts={
            "archive.disk": {"source": "/offline", "target": "/archive", "marker": ".ready"},
            "worker": {"volume": True, "target": "/worker", "backup": "none"},
        },
        environment={
            "CDP_HOST": "[bold]$HOST", "EMPTY": "", "LINES": "first\nsecond",
            "LONG_LITERAL": "x" * 240,
        },
    )
    monkeypatch.setattr("djinn_in_a_box.commands.config.load_config", lambda: config)
    result = runner.invoke(app, ["config", "show", *(["--json"] if format_name == "json" else [])])
    assert result.exit_code == 0, result.output
    if format_name == "json":
        data = json.loads(result.output)
        assert data["mounts"] == config.model_dump(mode="json")["mounts"]
        assert data["environment"] == config.environment
    else:
        for value in (
            "Mounts",
            "Environment",
            "archive.disk",
            "bind",
            ".ready",
            "worker",
            "backup=none",
            "[bold]$HOST",
            "EMPTY=",
            "first",
            "second",
        ):
            assert value in result.output


@pytest.mark.parametrize("case", ["offline", "schema", "environment"])
def test_edit_validates_declarations(tmp_path, monkeypatch, config_edit_project, case):
    path = tmp_path / "config.toml"
    save_config_file(AppConfig(code_dir=tmp_path), path)
    monkeypatch.setattr("djinn_in_a_box.commands.config.CONFIG_FILE", path)
    monkeypatch.setattr(
        "djinn_in_a_box.commands.config.load_config", lambda: load_config_file(path)
    )

    def editor(*args, **kwargs):
        suffix = '\n[mounts.archive]\nsource="/offline"\ntarget="/archive"\n'
        if case == "schema":
            suffix += 'backup="data"\n'
        elif case == "environment":
            suffix += "\n[environment]\nCDP_HOST=123\n"
        # Start from a file without empty collections, so the appended tables are unique.
        path.write_text(f'[general]\ncode_dir="{tmp_path}"\n' + suffix)
        return MagicMock(returncode=0)

    monkeypatch.setattr("djinn_in_a_box.commands.config.subprocess.run", editor)
    result = runner.invoke(app, ["config", "edit"])
    assert result.exit_code == 0, result.output
    assert ("Configuration problem" in result.output) is (case != "offline")
    assert not (tmp_path / "offline").exists()


def test_init_force_resets_declarations(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    save_config_file(
        AppConfig(
            code_dir=tmp_path,
            mounts={
                "worker": {"volume": True, "target": "/worker", "backup": "none"},
            },
            environment={"CDP_HOST": "old"},
        ),
        path,
    )
    _patch_init_dependencies(monkeypatch, tmp_path, path)
    monkeypatch.setattr(
        "djinn_in_a_box.commands.config.save_config", lambda config: save_config_file(config, path)
    )
    result = runner.invoke(app, ["init", "--force"], input=f"\n{tmp_path}\n\n\n\n\n\n\n\n\n")
    assert result.exit_code == 0, result.output
    config = load_config_file(path)
    assert config.mounts == {} and config.environment == {}
    assert "marker" not in result.output.lower() and "declaration" not in result.output.lower()
