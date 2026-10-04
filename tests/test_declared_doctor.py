"""Doctor preserves independent declaration diagnostics, including schema failures."""

import pytest
import typer

from djinn_in_a_box.commands import doctor as doctor_module
from djinn_in_a_box.config.loader import save_config
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import docker
from djinn_in_a_box.core.exceptions import ConfigValidationError


@pytest.mark.parametrize("case", ["runtime", "schema", "optional-context", "syntax"])
def test_one_row_per_declaration(tmp_path, monkeypatch, case):
    path = tmp_path / "config.toml"
    path.write_text(f'''[general]
code_dir="{tmp_path}"
[mounts.archive]
source="{tmp_path / "missing"}"
target="/archive"
[mounts.worker]
volume=true
target="/worker"
backup="none"
[mounts.good]
source="{tmp_path}"
target="/good"
[environment]
CDP_HOST="literal"
''')
    if case == "schema":
        path.write_text(path.read_text() + 'DOCKER_HOST="reserved"\nBAD=123\n')
    elif case == "syntax":
        path.write_text(path.read_text() + "[\n")
    monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", path)
    monkeypatch.setattr(docker, "get_shell_mount_args", lambda config: [])
    monkeypatch.setattr(docker, "get_audio_mount_args", lambda: [])
    monkeypatch.setattr(docker, "get_dbus_mount_args", lambda: [])
    monkeypatch.setattr(docker, "get_sops_age_key_mount_args", lambda config: [])
    if case == "optional-context":

        def failed(config):
            raise ConfigValidationError("zones unavailable")

        monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", failed)
    else:
        monkeypatch.setattr(docker, "_zone_overlay_mount_args_and_targets", lambda config: ([], ()))
    collected = []

    def checks(config, error=None):
        rows = [
            doctor_module.Check(
                "Configuration",
                doctor_module.Status.FAIL if error else doctor_module.Status.PASS,
                error or "valid",
            )
        ]
        if config:
            rows += doctor_module.declaration_checks(config)
        collected.extend(rows)
        return rows

    monkeypatch.setattr(doctor_module, "run_checks", checks)
    original = doctor_module.declaration_checks

    def record(*args):
        rows = original(*args)
        # Invalid-config branch is appended by doctor, outside run_checks.
        if args[0] is None or len(args) == 2:
            collected.extend(rows)
        return rows

    monkeypatch.setattr(doctor_module, "declaration_checks", record)
    with pytest.raises(typer.Exit) as exc:
        doctor_module.doctor()
    assert exc.value.exit_code == 1
    entries = {
        row.name: row for row in collected if row.name.startswith(("mounts.", "environment."))
    }
    expected = {"mounts.archive", "mounts.worker", "mounts.good", "environment.CDP_HOST"}
    if case == "schema":
        expected |= {"environment.DOCKER_HOST", "environment.BAD"}
    elif case == "syntax":
        expected = set()
    assert entries.keys() == expected
    assert len([r for r in collected if r.name in expected]) == len(expected)
    if expected:
        assert entries["mounts.archive"].status is doctor_module.Status.FAIL
        assert (
            "archive" in entries["mounts.archive"].detail
            and "does not exist" in entries["mounts.archive"].detail
        )
        assert entries["mounts.worker"].status is doctor_module.Status.PASS
        assert entries["mounts.good"].status is doctor_module.Status.PASS
        assert entries["environment.CDP_HOST"].status is doctor_module.Status.PASS
    if case == "schema":
        assert entries["environment.DOCKER_HOST"].status is doctor_module.Status.FAIL
        assert entries["environment.BAD"].status is doctor_module.Status.FAIL


@pytest.mark.parametrize("case", ["source", "marker", "overlap"])
def test_declared_paths_never_provisioned(tmp_path, monkeypatch, case):
    root = tmp_path / "config-root"
    source = root / "codex" if case == "overlap" else tmp_path / "archive"
    if case == "marker":
        source.mkdir(mode=0o755)
    entry = {"source": str(source), "target": "/archive"}
    if case == "marker":
        entry["marker"] = ".ready"
    config = AppConfig(code_dir=tmp_path, config_root=root, mounts={"archive": entry})
    before = source.stat().st_mode if source.exists() else None
    # The real built-in provisioner must not gain a declaration-driven mkdir/chmod path.
    docker.ensure_host_env(config)
    doctor_module.declaration_checks(config)
    if case == "overlap":
        assert source.is_dir()  # Existing built-in provisioning is explicitly allowed.
    elif case == "source":
        assert not source.exists()
    else:
        assert not (source / ".ready").exists() and source.stat().st_mode == before
    # doctor --fix retains its existing repair boundary.
    path = tmp_path / "config.toml"
    save_config(config, path)
    monkeypatch.setattr("djinn_in_a_box.config.loader.CONFIG_FILE", path)
    monkeypatch.setattr(
        doctor_module, "run_checks", lambda config, error: doctor_module.declaration_checks(config)
    )
    monkeypatch.setattr(
        doctor_module, "_doctor_fix", lambda config: (docker.ensure_host_env(config), False)[1]
    )
    if case == "overlap":
        doctor_module.doctor(fix=True)
    else:
        with pytest.raises(typer.Exit):
            doctor_module.doctor(fix=True)
    if case == "source":
        assert not source.exists()
    if case == "marker":
        assert not (source / ".ready").exists() and source.stat().st_mode == before
