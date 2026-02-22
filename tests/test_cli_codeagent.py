"""Tests for the codeagent CLI entry point.

Tests for:
- Version callback (--version)
- Init command
- Config subcommands (show, path)
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
        monkeypatch.setattr("ai_dev_base.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("ai_dev_base.commands.config.AGENTS_FILE", config_dir / "agents.toml")

        # Mock CONFIG_DIR so mkdir creates the temp directory
        monkeypatch.setattr("ai_dev_base.commands.config.CONFIG_DIR", config_dir)

        # Mock save_config to actually write a file
        def mock_save_config(config: object) -> None:
            config_file.write_text("[general]\ncode_dir = '/test'\n")

        monkeypatch.setattr("ai_dev_base.commands.config.save_config", mock_save_config)

        # Create a valid projects directory
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Mock get_project_root to avoid finding bundled agents.toml
        monkeypatch.setattr(
            "ai_dev_base.commands.config.get_project_root", lambda: tmp_path / "nonexistent"
        )

        # Run init with input
        result = runner.invoke(app, ["init"], input=f"{projects_dir}\nEurope/Berlin\n")

        assert result.exit_code == 0
        assert config_file.exists()
        # Output may be in stdout or output (Rich console writes to output)
        combined = result.stdout + result.output
        assert "Next steps" in combined

    def test_init_force_overwrites(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test init --force overwrites existing config without prompting."""
        config_dir = tmp_path / ".config" / "ai-dev-base"
        config_file = config_dir / "config.toml"

        # Create existing config
        config_dir.mkdir(parents=True)
        config_file.write_text("[old]\ndata = 'old'\n")

        # Mock paths
        monkeypatch.setattr("ai_dev_base.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("ai_dev_base.commands.config.AGENTS_FILE", config_dir / "agents.toml")
        monkeypatch.setattr("ai_dev_base.commands.config.CONFIG_DIR", config_dir)

        # Mock save_config
        def mock_save_config(config: object) -> None:
            config_file.write_text("[new]\ndata = 'new'\n")

        monkeypatch.setattr("ai_dev_base.commands.config.save_config", mock_save_config)

        # Create a valid projects directory
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        monkeypatch.setattr(
            "ai_dev_base.commands.config.get_project_root", lambda: tmp_path / "nonexistent"
        )

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
        monkeypatch.setattr("ai_dev_base.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("ai_dev_base.commands.config.AGENTS_FILE", config_dir / "agents.toml")
        monkeypatch.setattr("ai_dev_base.commands.config.CONFIG_DIR", config_dir)

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
        monkeypatch.setattr("ai_dev_base.commands.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("ai_dev_base.commands.config.AGENTS_FILE", config_dir / "agents.toml")
        monkeypatch.setattr("ai_dev_base.commands.config.CONFIG_DIR", config_dir)

        def mock_save_config(config: object) -> None:
            config_file.write_text("[general]\ncode_dir = '/test'\n")

        monkeypatch.setattr("ai_dev_base.commands.config.save_config", mock_save_config)
        monkeypatch.setattr(
            "ai_dev_base.commands.config.get_project_root", lambda: tmp_path / "nonexistent"
        )

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

        monkeypatch.setattr("ai_dev_base.commands.config.load_config", lambda: mock_config)

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

        monkeypatch.setattr("ai_dev_base.commands.config.load_config", lambda: mock_config)

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
        from ai_dev_base.config.loader import ConfigNotFoundError

        config_file = tmp_path / "nonexistent" / "config.toml"

        def mock_load_config() -> None:
            raise ConfigNotFoundError(config_file)

        monkeypatch.setattr("ai_dev_base.commands.config.load_config", mock_load_config)

        result = runner.invoke(app, ["config", "show"])

        assert result.exit_code == 1
        # Error may be in stdout or output
        combined = result.stdout + result.output
        assert "Configuration not found" in combined


class TestConfigPathCommand:
    """Tests for the config path command."""

    def test_config_path_shows_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config path outputs the config file path."""
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr("ai_dev_base.commands.config.CONFIG_FILE", config_file)

        result = runner.invoke(app, ["config", "path"])

        assert result.exit_code == 0
        assert str(config_file) in result.stdout
