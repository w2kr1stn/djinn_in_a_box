"""Declaration runtime boundary: no Docker or host provisioning is performed."""

from pathlib import Path

import pytest

from djinn_in_a_box.config.declarations import RESERVED_ENVIRONMENT
from djinn_in_a_box.config.defaults import VOLUME_CATEGORIES
from djinn_in_a_box.config.models import AppConfig
from djinn_in_a_box.core import docker
from djinn_in_a_box.core.exceptions import DeclarationSpecificationError


def resolve(tmp_path, mounts=None, environment=None, targets=None, invocation=(), caller=None):
    # Bypass only AppConfig validation, to verify the core boundary rechecks pure rules.
    config = AppConfig(code_dir=tmp_path).model_copy(
        update={
            "mounts": mounts or {},
            "environment": environment or {},
        }
    )
    return docker.resolve_declared_entries(
        config,
        docker.ContainerOptions(mounts=invocation),
        runtime_targets=targets or [],
        caller_env=caller,
    )


@pytest.mark.parametrize("kind", ["bind", "volume"])
def test_resolved_declaration_defaults(kind):
    assert docker.ResolvedDeclaration("entry", kind, "source", Path("/target")).read_only is False


@pytest.mark.parametrize(
    "case", ["missing", "file", "relative", "colon", "symlink", "resolved-colon", "loop", "denied"]
)
@pytest.mark.parametrize("read_only", [True, False, None], ids=["ro", "rw", "default"])
def test_bind_source_checks(tmp_path, monkeypatch, case, read_only):
    source = tmp_path / "source"
    cause = None
    if case == "missing":
        cause = "does not exist"
    elif case == "file":
        source.write_text("sentinel")
        cause = "not a directory"
    elif case == "relative":
        source = Path("relative")
        cause = "absolute"
    elif case == "colon":
        source = tmp_path / "bad:source"
        source.mkdir()
        cause = "contain"
    elif case in ("symlink", "resolved-colon"):
        real = tmp_path / ("bad:real" if case == "resolved-colon" else "real")
        real.mkdir()
        source.symlink_to(real, target_is_directory=True)
        if case == "resolved-colon":
            cause = "resolved source"
    elif case == "loop":
        source.symlink_to(source)
        cause = "resolved"
    else:
        source.mkdir()
        cause = "inspect"

        def denied(_):
            raise PermissionError("denied")

        monkeypatch.setattr(docker, "resolve_mount_path", denied)
    entry = {"source": str(source), "target": "/archive"}
    if read_only is not None:
        entry["read_only"] = read_only
    result = resolve(tmp_path, {"archive": entry})
    if cause:
        with pytest.raises(DeclarationSpecificationError) as exc:
            result.require_valid()
        assert "archive" in str(exc.value) and cause in str(exc.value)
    else:
        result.require_valid()
        assert result.mounts[0].source == str(real)
        assert result.mounts[0].read_only is (read_only is True)
        fragment = result.compose_fragment()["services"]["dev"]
        assert fragment["volumes"] == [{
            "type": "bind", "source": str(real), "target": "/archive",
            "bind": {"create_host_path": False},
            **({"read_only": True} if read_only is True else {}),
        }]
        assert fragment["environment"]["DJINN_DECLARED_VOLUME_TARGETS"] == "[]"
    if case == "missing":
        assert not source.exists()
    if case == "file":
        assert source.read_text() == "sentinel"


@pytest.mark.parametrize(
    "case", ["optional", "missing", "directory", "empty", "content", "symlink", "denied"]
)
def test_marker_guard(tmp_path, monkeypatch, case):
    marker = tmp_path / ".ready"
    if case == "directory":
        marker.mkdir()
    elif case in ("empty", "content"):
        marker.write_text("" if case == "empty" else "contents are ignored")
    elif case == "symlink":
        real = tmp_path / "real"
        real.touch()
        marker.symlink_to(real)
    elif case == "denied":
        original = Path.lstat

        def denied(path, *args, **kwargs):
            if path == marker:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", denied)
    entry = {"source": str(tmp_path), "target": "/archive"}
    if case != "optional":
        entry["marker"] = ".ready"
    result = resolve(tmp_path, {"archive": entry})
    if case in ("missing", "directory", "symlink", "denied"):
        error = result.diagnostics[0].error
        assert error and "archive" in error and ".ready" in error
        assert (
            "missing"
            if case == "missing"
            else "inspect"
            if case == "denied"
            else "not a regular file"
        ) in error
    else:
        result.require_valid()
    if case == "missing":
        assert not marker.exists()


@pytest.mark.parametrize(
    ("target", "reserved", "fails"),
    [
        ("/home/dev/.codex", "/home/dev/.codex", True),
        ("/home/dev", "/home/dev/.codex", True),
        ("/home/dev/.codex/worker", "/home/dev/.codex", False),
        ("/home/dev/aios", "/home/dev/aios", True),
        ("/home/dev/projects", "/home/dev/aios", False),
        ("/home/dev/.codex/sessions", "/home/dev/.codex/sessions", True),
        ("/var/run/docker.sock", "/run/docker.sock", True),
        ("/home/dev/.config/sops/age/keys.txt", "/home/dev/.config/sops/age/keys.txt", True),
        ("/home/dev/.zshrc.local", "/home/dev/.zshrc.local", True),
        ("/home/dev/.config/claude/../claude", "/home/dev/.claude", True),
        ("//home/dev//.codex/./", "/home/dev/.codex", True),
        *[(str(root / "child"), str(root), True) for root in docker.MANAGED_VOLUME_REPAIR_TARGETS],
        *[(str(root), str(root), True) for root in docker.MANAGED_VOLUME_REPAIR_TARGETS],
        *[(root, "/different", True) for root in ("/proc", "/sys/x", "/dev/x")],
    ],
)
def test_declared_target_matrix(tmp_path, target, reserved, fails):
    result = resolve(
        tmp_path,
        {"worker": {"volume": True, "target": target, "backup": "none"}},
        targets=[Path(reserved)],
    )
    error = result.diagnostics[0].error
    assert bool(error) is fails
    if fails:
        assert "worker" in error
        assert "conflict" in error or "not allowed" in error
    else:
        result.require_valid()


@pytest.mark.parametrize("order", ["forward", "reverse"])
@pytest.mark.parametrize(
    "case",
    ["equal", "ancestor", "invocation-equal", "invocation-ancestor", "invocation-child", "derived"],
)
def test_declared_and_invocation_targets(tmp_path, order, case):
    mounts = {"archive": {"source": str(tmp_path), "target": "/extra"}}
    invocation = ()
    if case in ("equal", "ancestor"):
        mounts["worker"] = {
            "volume": True,
            "target": "/extra" if case == "equal" else "/extra/child",
            "backup": "none",
        }
    elif case == "derived":
        invocation = docker.resolve_container_mounts([str(tmp_path)])
        mounts["archive"]["target"] = str(invocation[0].target)
    else:
        target = (
            "/extra"
            if case == "invocation-equal"
            else "/extra/child"
            if case == "invocation-ancestor"
            else "/"
        )
        invocation = (docker.ContainerMount(tmp_path, Path(target)),)
    if order == "reverse":
        mounts = dict(reversed(list(mounts.items())))
    result = resolve(tmp_path, mounts, invocation=invocation)
    if case == "invocation-child":
        result.require_valid()
    else:
        errors = "\n".join(d.error or "" for d in result.diagnostics)
        assert "archive" in errors and "conflict" in errors
        assert ("worker" if case in ("equal", "ancestor") else "--mount/--here") in errors
        with pytest.raises(DeclarationSpecificationError):
            result.require_valid()


@pytest.mark.parametrize(
    "actual", [v for values in VOLUME_CATEGORIES.values() for v in values] + ["djinn-worker"]
)
def test_volume_name_collision(tmp_path, actual):
    result = resolve(
        tmp_path,
        {actual.removeprefix("djinn-"): {"volume": True, "target": "/worker", "backup": "none"}},
    )
    if actual == "djinn-worker":
        result.require_valid()
        assert result.mounts[0].source == actual
    else:
        assert result.diagnostics, "built-in volume must produce a collision diagnostic"
        assert (
            actual in result.diagnostics[0].error
            and "built-in volume" in result.diagnostics[0].error
        )


@pytest.mark.parametrize(
    "key",
    sorted(RESERVED_ENVIRONMENT)
    + ["bad-key", "1KEY", "NONSTRING", "NUL_VALUE", "CALLER", "LITERAL"],
)
def test_literal_environment(tmp_path, monkeypatch, key):
    literal = ' spaces = "quote"\n[value] $HOST ${HOST:-fallback}'
    monkeypatch.setenv("HOST", "must not appear")
    value = 3 if key == "NONSTRING" else "x\0" if key == "NUL_VALUE" else literal
    result = resolve(tmp_path, environment={key: value}, caller={"CALLER": "caller"})
    if key == "LITERAL":
        result.require_valid()
        assert result.environment == {key: literal}
    else:
        assert result.diagnostics, "invalid environment must produce a diagnostic"
        error = result.diagnostics[0].error
        assert error and key in error
        assert literal not in error
        assert (
            "reserved"
            if key in RESERVED_ENVIRONMENT or key == "CALLER"
            else "string"
            if key == "NONSTRING"
            else "NUL"
            if key == "NUL_VALUE"
            else "match"
        ) in error


@pytest.mark.parametrize(
    "case", ["zones", "socket", "sops", "shell", "active-workspace", "inactive-workspace"]
)
def test_declared_dynamic_reservations(tmp_path, monkeypatch, case):
    config = AppConfig(code_dir=tmp_path, workspace="aios", config_root=tmp_path / "config")
    docker.ensure_host_env(config)
    for name in (
        "get_shell_mount_args",


        "get_sops_age_key_mount_args",
    ):
        monkeypatch.setattr(docker, name, lambda *args: [])
    # The real zone resolver reserves assigned directories even before they exist.
    zone_target = "/home/dev/.codex/sessions"
    if case == "shell":
        monkeypatch.setattr(
            docker, "get_shell_mount_args", lambda config: ["-v", "/host:/home/dev/.zshrc.local"]
        )
    elif case == "sops":
        monkeypatch.setattr(
            docker,
            "get_sops_age_key_mount_args",
            lambda config: ["-v", "/key:/home/dev/.config/sops/age/keys.txt:ro"],
        )
    targets, warnings = docker.declaration_reservation_context(config)
    assert warnings == []
    selected = {
        "zones": zone_target,
        "socket": "/run/docker.sock",
        "sops": str(docker.SOPS_AGE_KEY_TARGET),
        "shell": "/home/dev/.zshrc.local",
        "active-workspace": "/home/dev/aios",
        "inactive-workspace": "/home/dev/projects",
    }[case]
    if case == "zones":
        assert Path(zone_target) in targets
        zone_source = docker.resolve_zone_roots(config).local_root / "codex/sessions"
        if zone_source.exists():
            zone_source.rmdir()
        assert not zone_source.exists()
        targets, warnings = docker.declaration_reservation_context(config)
    config = config.model_copy(
        update={
            "mounts": {
                "worker": {"volume": True, "target": selected, "backup": "none"},
            }
        }
    )
    result = docker.resolve_declared_entries(
        config, docker.ContainerOptions(), runtime_targets=targets, caller_env=None
    )
    assert bool(result.diagnostics[0].error) is (case != "inactive-workspace")
