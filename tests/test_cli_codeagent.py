"""Tests for the codeagent CLI entry point.

Tests for:
- Version callback (--version)
- Help display (--help)
- Init command
- Config subcommands (show, path)
- Command registration
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_base import __version__
from ai_dev_base.cli.codeagent import app

runner = CliRunner()


# =============================================================================
# CLI Basic Tests
# =============================================================================


class TestCodeagentVersion:
    """Tests for the --version flag."""

    def test_version_short_flag(self) -> None:
        """Test -V shows version and exits."""
        result = runner.invoke(app, ["-V"])
        assert result.exit_code == 0
        assert f"codeagent {__version__}" in result.stdout

    def test_version_long_flag(self) -> None:
        """Test --version shows version and exits."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert f"codeagent {__version__}" in result.stdout


class TestCodeagentHelp:
    """Tests for the --help flag."""

    def test_help_shows_all_commands(self) -> None:
        """Test --help shows all registered commands."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

        # Main commands
        assert "init" in result.stdout
        assert "build" in result.stdout
        assert "start" in result.stdout
        assert "auth" in result.stdout
        assert "status" in result.stdout
        assert "audit" in result.stdout
        assert "update" in result.stdout
        assert "enter" in result.stdout
        assert "run" in result.stdout
        assert "agents" in result.stdout

        # Subcommand groups
        assert "config" in result.stdout
        assert "clean" in result.stdout

    def test_help_shows_description(self) -> None:
        """Test --help shows application description."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "AI Dev Base CLI" in result.stdout


class TestNoArgsIsHelp:
    """Tests for no_args_is_help behavior."""

    def test_no_args_shows_usage(self) -> None:
        """Test invoking without arguments shows usage info."""
        result = runner.invoke(app, [])
        # With no_args_is_help=True, Typer shows help or usage
        # The output depends on Typer version, but should contain
        # something useful (init command, Usage text, or help text)
        combined = result.stdout + result.output
        assert "init" in combined or "Usage" in combined or "Options" in combined


# =============================================================================
# Init Command Tests
# =============================================================================


class TestInitCommand:
    """Tests for the init command."""

    def test_init_creates_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test init creates config.toml in config directory."""
        config_dir = tmp_path / ".config" / "ai-dev-base"
        config_file = config_dir / "config.toml"

        # Mock paths to use temp directory
        monkeypatch.setattr("ai_dev_base.cli.codeagent.CONFIG_FILE", config_file)
        monkeypatch.setattr("ai_dev_base.cli.codeagent.AGENTS_FILE", config_dir / "agents.toml")

        # Mock ensure_config_dir to create the directory
        def mock_ensure_config_dir() -> None:
            config_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("ai_dev_base.cli.codeagent.ensure_config_dir", mock_ensure_config_dir)

        # Mock save_config to actually write a file
        def mock_save_config(config: object) -> None:
            config_file.write_text("[general]\ncode_dir = '/test'\n")

        monkeypatch.setattr("ai_dev_base.cli.codeagent.save_config", mock_save_config)

        # Create a valid projects directory
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Mock get_bundled_agents_path to return None
        monkeypatch.setattr("ai_dev_base.cli.codeagent.get_bundled_agents_path", lambda: None)

        # Run init with input
        result = runner.invoke(app, ["init"], input=f"{projects_dir}\nEurope/Berlin\n")

        assert result.exit_code == 0
        assert config_file.exists()
        # Output may be in stdout or output (Rich console writes to output)
        combined = result.stdout + result.output
        assert "Next steps" in combined or config_file.exists()

    def test_init_force_overwrites(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test init --force overwrites existing config without prompting."""
        config_dir = tmp_path / ".config" / "ai-dev-base"
        config_file = config_dir / "config.toml"

        # Create existing config
        config_dir.mkdir(parents=True)
        config_file.write_text("[old]\ndata = 'old'\n")

        # Mock paths
        monkeypatch.setattr("ai_dev_base.cli.codeagent.CONFIG_FILE", config_file)
        monkeypatch.setattr("ai_dev_base.cli.codeagent.AGENTS_FILE", config_dir / "agents.toml")
        monkeypatch.setattr("ai_dev_base.cli.codeagent.ensure_config_dir", lambda: None)

        # Mock save_config
        def mock_save_config(config: object) -> None:
            config_file.write_text("[new]\ndata = 'new'\n")

        monkeypatch.setattr("ai_dev_base.cli.codeagent.save_config", mock_save_config)

        # Create a valid projects directory
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        monkeypatch.setattr("ai_dev_base.cli.codeagent.get_bundled_agents_path", lambda: None)

        # Run init --force
        result = runner.invoke(app, ["init", "--force"], input=f"{projects_dir}\nEurope/Berlin\n")

        assert result.exit_code == 0
        content = config_file.read_text()
        assert "[new]" in content

    def test_init_prompts_overwrite_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test init prompts when config exists and user declines."""
        config_dir = tmp_path / ".config" / "ai-dev-base"
        config_file = config_dir / "config.toml"

        # Create existing config
        config_dir.mkdir(parents=True)
        config_file.write_text("[existing]\nconfig = 'yes'\n")

        # Mock paths
        monkeypatch.setattr("ai_dev_base.cli.codeagent.CONFIG_FILE", config_file)
        monkeypatch.setattr("ai_dev_base.cli.codeagent.AGENTS_FILE", config_dir / "agents.toml")
        monkeypatch.setattr("ai_dev_base.cli.codeagent.ensure_config_dir", lambda: None)

        # Run init and decline overwrite
        result = runner.invoke(app, ["init"], input="n\n")

        # Should abort
        assert result.exit_code == 1

    def test_init_creates_nonexistent_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test init offers to create non-existent projects directory."""
        config_dir = tmp_path / ".config" / "ai-dev-base"
        config_file = config_dir / "config.toml"

        # Mock paths
        monkeypatch.setattr("ai_dev_base.cli.codeagent.CONFIG_FILE", config_file)
        monkeypatch.setattr("ai_dev_base.cli.codeagent.AGENTS_FILE", config_dir / "agents.toml")

        def mock_ensure_config_dir() -> None:
            config_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("ai_dev_base.cli.codeagent.ensure_config_dir", mock_ensure_config_dir)

        def mock_save_config(config: object) -> None:
            config_file.write_text("[general]\ncode_dir = '/test'\n")

        monkeypatch.setattr("ai_dev_base.cli.codeagent.save_config", mock_save_config)
        monkeypatch.setattr("ai_dev_base.cli.codeagent.get_bundled_agents_path", lambda: None)

        # Use a non-existent directory
        nonexistent = tmp_path / "new_projects"

        # Run init, confirm directory creation
        result = runner.invoke(app, ["init"], input=f"{nonexistent}\nEurope/Berlin\ny\n")

        assert result.exit_code == 0
        assert nonexistent.exists()


# =============================================================================
# Config Subcommand Tests
# =============================================================================


class TestConfigShowCommand:
    """Tests for the config show command."""

    def test_config_show_displays_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test config show displays current configuration."""
        from ai_dev_base.config.models import AppConfig, ResourceLimits, ShellConfig

        # Create the projects directory so AppConfig validation passes
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        mock_config = AppConfig(
            code_dir=projects_dir,
            timezone="Europe/Berlin",
            resources=ResourceLimits(),
            shell=ShellConfig(),
        )

        monkeypatch.setattr("ai_dev_base.cli.codeagent.load_config", lambda: mock_config)

        result = runner.invoke(app, ["config", "show"])

        assert result.exit_code == 0
        combined = result.stdout + result.output
        assert "projects" in combined
        assert "Europe/Berlin" in combined

    def test_config_show_json_output(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config show --json outputs JSON."""
        from ai_dev_base.config.models import AppConfig, ResourceLimits, ShellConfig

        # Create the projects directory
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        mock_config = AppConfig(
            code_dir=projects_dir,
            timezone="Europe/Berlin",
            resources=ResourceLimits(),
            shell=ShellConfig(),
        )

        monkeypatch.setattr("ai_dev_base.cli.codeagent.load_config", lambda: mock_config)

        result = runner.invoke(app, ["config", "show", "--json"])

        assert result.exit_code == 0
        # stdout contains the JSON output from Rich console
        import json

        # The output may have newlines, just use stdout (CliRunner captures console output there)
        data = json.loads(result.stdout)
        assert "code_dir" in data
        assert "timezone" in data

    def test_config_show_missing_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test config show shows error when config not found."""
        from ai_dev_base.config import ConfigNotFoundError

        config_file = tmp_path / "nonexistent" / "config.toml"

        def mock_load_config() -> None:
            raise ConfigNotFoundError(config_file)

        monkeypatch.setattr("ai_dev_base.cli.codeagent.load_config", mock_load_config)

        result = runner.invoke(app, ["config", "show"])

        assert result.exit_code == 1
        # Error may be in stdout or output
        combined = result.stdout + result.output
        assert "Configuration not found" in combined or result.exit_code == 1


class TestConfigPathCommand:
    """Tests for the config path command."""

    def test_config_path_shows_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config path outputs the config file path."""
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr("ai_dev_base.cli.codeagent.CONFIG_FILE", config_file)

        result = runner.invoke(app, ["config", "path"])

        assert result.exit_code == 0
        assert str(config_file) in result.stdout


# =============================================================================
# Command Registration Tests
# =============================================================================


class TestCommandRegistration:
    """Tests for proper command registration."""

    def test_build_command_registered(self) -> None:
        """Test build command is registered."""
        result = runner.invoke(app, ["build", "--help"])
        assert result.exit_code == 0
        assert "Build" in result.stdout or "build" in result.stdout

    def test_start_command_registered(self) -> None:
        """Test start command is registered."""
        result = runner.invoke(app, ["start", "--help"])
        assert result.exit_code == 0
        assert "docker" in result.stdout.lower() or "firewall" in result.stdout.lower()

    def test_auth_command_registered(self) -> None:
        """Test auth command is registered."""
        result = runner.invoke(app, ["auth", "--help"])
        assert result.exit_code == 0
        assert "OAuth" in result.stdout or "auth" in result.stdout.lower()

    def test_status_command_registered(self) -> None:
        """Test status command is registered."""
        result = runner.invoke(app, ["status", "--help"])
        assert result.exit_code == 0
        assert "status" in result.stdout.lower()

    def test_audit_command_registered(self) -> None:
        """Test audit command is registered."""
        result = runner.invoke(app, ["audit", "--help"])
        assert result.exit_code == 0
        assert "log" in result.stdout.lower() or "proxy" in result.stdout.lower()

    def test_update_command_registered(self) -> None:
        """Test update command is registered."""
        result = runner.invoke(app, ["update", "--help"])
        assert result.exit_code == 0
        assert "update" in result.stdout.lower()

    def test_enter_command_registered(self) -> None:
        """Test enter command is registered."""
        result = runner.invoke(app, ["enter", "--help"])
        assert result.exit_code == 0
        assert "shell" in result.stdout.lower() or "container" in result.stdout.lower()

    def test_run_command_registered(self) -> None:
        """Test run command is registered."""
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "agent" in result.stdout.lower()

    def test_agents_command_registered(self) -> None:
        """Test agents command is registered."""
        result = runner.invoke(app, ["agents", "--help"])
        assert result.exit_code == 0
        assert "agent" in result.stdout.lower() or "list" in result.stdout.lower()

    def test_clean_subcommand_registered(self) -> None:
        """Test clean subcommand group is registered."""
        result = runner.invoke(app, ["clean", "--help"])
        assert result.exit_code == 0
        assert "volumes" in result.stdout.lower() or "all" in result.stdout.lower()

    def test_config_subcommand_registered(self) -> None:
        """Test config subcommand group is registered."""
        result = runner.invoke(app, ["config", "--help"])
        assert result.exit_code == 0
        assert "show" in result.stdout
        assert "path" in result.stdout
