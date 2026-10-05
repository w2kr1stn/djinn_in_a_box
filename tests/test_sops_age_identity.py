"""Tests for the optional, read-only SOPS age identity mount."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from djinn_in_a_box.commands.config import (
    _build_config,  # pyright: ignore[reportPrivateUsage]
    _format_config_value,  # pyright: ignore[reportPrivateUsage]
    _set_config_value,  # pyright: ignore[reportPrivateUsage]
)
from djinn_in_a_box.commands.doctor import (
    Status,
    _sops_age_key_check,  # pyright: ignore[reportPrivateUsage]
)
from djinn_in_a_box.config.loader import load_config, save_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core.docker import (
    SOPS_AGE_KEY_TARGET,
    ContainerMount,
    ContainerOptions,
    MountCollisionError,
    compose_run,
    compose_up_detached,
    get_sops_age_key_mount_args,
    sops_age_key_file_problem,
)
from djinn_in_a_box.core.exceptions import MountSpecificationError, SopsAgeKeyFileError

_EXPECTED_ENV = f"SOPS_AGE_KEY_FILE={SOPS_AGE_KEY_TARGET}"


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "machine-bound" / "keys.txt"
    path.parent.mkdir()
    path.write_text("# placeholder, not a real identity\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _with_key(config: AppConfig, key: Path | None) -> AppConfig:
    return config.model_copy(update={"sops_age_key_file": key})


class TestModel:
    def test_unset_by_default(self, mock_app_config: AppConfig) -> None:
        assert mock_app_config.sops_age_key_file is None

    def test_expands_home(
        self, mock_app_config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        config = AppConfig(
            code_dir=mock_app_config.code_dir, sops_age_key_file="~/keys.txt"
        )
        assert config.sops_age_key_file == tmp_path / "keys.txt"

    @pytest.mark.parametrize("value", ["relative/keys.txt", "/abs/with:colon/keys.txt"])
    def test_rejects_unmountable_paths(self, mock_app_config: AppConfig, value: str) -> None:
        with pytest.raises(ValidationError):
            AppConfig(code_dir=mock_app_config.code_dir, sops_age_key_file=value)


class TestPersistence:
    def test_round_trips_under_general(
        self, mock_app_config: AppConfig, key_file: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "config.toml"
        save_config(_with_key(mock_app_config, key_file), target)

        with target.open("rb") as handle:
            raw = tomllib.load(handle)
        assert raw["general"]["sops_age_key_file"] == str(key_file)
        assert load_config(target).sops_age_key_file == key_file

    def test_unset_is_not_written(self, mock_app_config: AppConfig, tmp_path: Path) -> None:
        target = tmp_path / "config.toml"
        save_config(mock_app_config, target)
        assert "sops_age_key_file" not in target.read_text(encoding="utf-8")


class TestConfigCommand:
    def test_set_and_show(self, mock_app_config: AppConfig, key_file: Path) -> None:
        updated = _set_config_value(mock_app_config, "general.sops_age_key_file", str(key_file))
        assert updated.sops_age_key_file == key_file
        assert _format_config_value(updated, "general.sops_age_key_file") == str(key_file)

    @pytest.mark.parametrize("value", ["", "none", "NULL"])
    def test_set_none_unsets(
        self, mock_app_config: AppConfig, key_file: Path, value: str
    ) -> None:
        configured = _with_key(mock_app_config, key_file)
        updated = _set_config_value(configured, "general.sops_age_key_file", value)
        assert updated.sops_age_key_file is None
        assert _format_config_value(updated, "general.sops_age_key_file") == "unset"

    def test_other_keys_keep_the_identity(
        self, mock_app_config: AppConfig, key_file: Path
    ) -> None:
        # `config set` rebuilds AppConfig field by field; a forgotten field is erased.
        configured = _with_key(mock_app_config, key_file)
        assert _build_config(configured, timezone="Europe/Berlin").sops_age_key_file == key_file


class TestProblem:
    def test_accepts_private_regular_file(self, key_file: Path) -> None:
        assert sops_age_key_file_problem(key_file) is None

    def test_missing(self, tmp_path: Path) -> None:
        assert sops_age_key_file_problem(tmp_path / "absent") == "does not exist"

    def test_directory(self, tmp_path: Path) -> None:
        assert sops_age_key_file_problem(tmp_path) == "is not a regular file"

    def test_group_or_world_access(self, key_file: Path) -> None:
        key_file.chmod(0o640)
        problem = sops_age_key_file_problem(key_file)
        assert problem is not None
        assert "group or others" in problem

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
    def test_unreadable(self, key_file: Path) -> None:
        key_file.chmod(0o000)
        try:
            assert sops_age_key_file_problem(key_file) == "is not readable by the current user"
        finally:
            key_file.chmod(0o600)


class TestMountArgs:
    def test_unset_yields_nothing(self, mock_app_config: AppConfig) -> None:
        assert get_sops_age_key_mount_args(mock_app_config) == []

    def test_mounts_read_only_and_points_sops_at_it(
        self, mock_app_config: AppConfig, key_file: Path
    ) -> None:
        args = get_sops_age_key_mount_args(_with_key(mock_app_config, key_file))
        assert args == ["-v", f"{key_file}:{SOPS_AGE_KEY_TARGET}:ro", "-e", _EXPECTED_ENV]

    def test_refuses_an_unsafe_identity(
        self, mock_app_config: AppConfig, key_file: Path
    ) -> None:
        key_file.chmod(0o644)
        with pytest.raises(SopsAgeKeyFileError, match="group or others"):
            get_sops_age_key_mount_args(_with_key(mock_app_config, key_file))

    def test_error_is_a_mount_specification_error(self) -> None:
        # `start` and `run` already report MountSpecificationError cleanly.
        assert issubclass(SopsAgeKeyFileError, MountSpecificationError)


@patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
@patch("djinn_in_a_box.core.docker.subprocess.run")
class TestComposeRun:
    def test_adds_the_mount_and_env(
        self, mock_run: MagicMock, _root: MagicMock, mock_app_config: AppConfig, key_file: Path
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        compose_run(
            _with_key(mock_app_config, key_file),
            ContainerOptions(),
            command="echo",
            interactive=False,
            shell_mount_args=[],
        )

        cmd = mock_run.call_args.args[0]
        assert f"{key_file}:{SOPS_AGE_KEY_TARGET}:ro" in cmd
        assert _EXPECTED_ENV in cmd

    def test_fails_closed_before_docker_runs(
        self, mock_run: MagicMock, _root: MagicMock, mock_app_config: AppConfig, tmp_path: Path
    ) -> None:
        with pytest.raises(SopsAgeKeyFileError, match="does not exist"):
            compose_run(
                _with_key(mock_app_config, tmp_path / "absent"),
                ContainerOptions(),
                command="echo",
                interactive=False,
                shell_mount_args=[],
            )
        mock_run.assert_not_called()

    def test_rejects_a_user_mount_over_the_identity(
        self, mock_run: MagicMock, _root: MagicMock, mock_app_config: AppConfig, key_file: Path
    ) -> None:
        options = ContainerOptions(
            mounts=(ContainerMount(key_file.parent, Path("/home/dev/.config/sops")),)
        )
        with pytest.raises(MountCollisionError):
            compose_run(
                _with_key(mock_app_config, key_file),
                options,
                command="echo",
                interactive=False,
                shell_mount_args=[],
            )
        mock_run.assert_not_called()


@patch("djinn_in_a_box.core.docker.get_project_root", return_value=Path("/project"))
@patch("djinn_in_a_box.core.docker.subprocess.run")
def test_detached_start_carries_mount_and_env_in_the_override(
    mock_run: MagicMock, _root: MagicMock, mock_app_config: AppConfig, key_file: Path
) -> None:
    payload: dict[str, object] = {}

    def _read_override(cmd: list[str], **_kwargs: object) -> MagicMock:
        override = Path(next(arg for arg in cmd if "djinn-detach-" in arg))
        payload.update(json.loads(override.read_text()))
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_run.side_effect = _read_override

    compose_up_detached(
        _with_key(mock_app_config, key_file),
        ContainerOptions(),
        shell_mount_args=[],
    )

    service = payload["services"]["dev"]  # type: ignore[index]
    assert f"{key_file}:{SOPS_AGE_KEY_TARGET}:ro" in service["volumes"]
    assert service["environment"]["SOPS_AGE_KEY_FILE"] == str(SOPS_AGE_KEY_TARGET)


class TestDoctor:
    def test_silent_when_unset(self, mock_app_config: AppConfig) -> None:
        assert _sops_age_key_check(mock_app_config, mock_app_config.config_root) is None

    def test_pass(self, mock_app_config: AppConfig, key_file: Path) -> None:
        config = _with_key(mock_app_config, key_file)
        check = _sops_age_key_check(config, mock_app_config.config_root)
        assert check is not None
        assert check.status is Status.PASS

    def test_fail_mirrors_the_start_refusal(
        self, mock_app_config: AppConfig, tmp_path: Path
    ) -> None:
        config = _with_key(mock_app_config, tmp_path / "absent")
        check = _sops_age_key_check(config, mock_app_config.config_root)
        assert check is not None
        assert check.status is Status.FAIL

    def test_warns_inside_the_mirrorable_config_root(
        self, mock_app_config: AppConfig, tmp_path: Path
    ) -> None:
        root = tmp_path / "config"
        inside = root / "age" / "keys.txt"
        inside.parent.mkdir(parents=True)
        inside.write_text("# placeholder\n", encoding="utf-8")
        inside.chmod(0o600)
        check = _sops_age_key_check(_with_key(mock_app_config, inside), root)
        assert check is not None
        assert check.status is Status.WARN
