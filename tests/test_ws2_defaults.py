"""Static pins for WS-2 default generalization."""

from __future__ import annotations

from pathlib import Path

import yaml

from djinn_in_a_box.config.defaults import SYNC_PATHS
from djinn_in_a_box.config.models import AppConfig, ResourceLimits


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_dockerfile_contains_no_german_locale() -> None:
    dockerfile = project_root() / "Dockerfile"
    # Token built dynamically so this test file itself never contains the
    # literal it forbids (the milestone acceptance scan covers tests/ too).
    german_locale = "de_" + "DE"
    assert german_locale not in dockerfile.read_text()


def test_compose_defaults_are_generic() -> None:
    compose_file = project_root() / "docker-compose.yml"
    compose_text = compose_file.read_text()
    assert "${TZ:-UTC}" in compose_text
    assert "azure" not in compose_text
    assert "pulumi" not in compose_text
    assert "sops" not in compose_text
    assert "DBUS" not in compose_text


def test_compose_common_environment_forwards_terminal_ui_env() -> None:
    compose_file = project_root() / "docker-compose.yml"
    compose = yaml.safe_load(compose_file.read_text())

    common_env = compose["x-common-environment"]
    assert common_env["NO_COLOR"] == "${NO_COLOR:-}"
    assert common_env["DJINN_TERM_WIDTH"] == "${DJINN_TERM_WIDTH:-}"


def test_app_config_default_timezone_is_utc(tmp_path: Path) -> None:
    assert AppConfig(code_dir=tmp_path).timezone == "UTC"


def test_resource_limit_defaults_are_conservative() -> None:
    limits = ResourceLimits()
    assert limits.cpu_limit == 4
    assert limits.memory_limit == "8G"
    assert limits.cpu_reservation == 1
    assert limits.memory_reservation == "2G"


def test_credentials_sync_paths_are_decoupled_from_work_tools() -> None:
    assert SYNC_PATHS["credentials"] == [
        "claude",
        "gemini",
        "codex",
        "opencode",
        "gh",
        "age",
    ]


def test_compose_mounts_age_credential_store() -> None:
    """The age identity store is bind-mounted so age keys persist across runs."""
    compose_file = project_root() / "docker-compose.yml"
    compose = yaml.safe_load(compose_file.read_text())
    dev_volumes = compose["services"]["dev"]["volumes"]
    assert "${DJINN_CONFIG_ROOT}/age:/home/dev/.config/age" in dev_volumes
