"""Desktop discovery, delivery fragments and read-only provenance inspection.

Docker execution belongs to core/docker.py. The #76 sealed check consumes this
interface without preparing services or trusting stale filesystem listeners.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from djinn_in_a_box.config.defaults import DESKTOP_RUNTIME_VOLUMES
from djinn_in_a_box.core.host_runtime import GENERATION_LABEL
from djinn_in_a_box.core.paths import get_project_root

if TYPE_CHECKING:
    from djinn_in_a_box.core.docker import ComposeFragment

Channel = Literal["dbus", "audio"]
HELPER_IMAGE = "djinn-desktop-helper:1"
PROXY_VERSION_FLOOR = "0.1.6-1+deb13u3"
MANAGED_ENV = frozenset({"DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER", "PULSE_COOKIE"})
MANAGED_TARGETS = (Path("/run/djinn/dbus"), Path("/run/djinn/audio"))
HELPER_SECONDS = 15.0


@dataclass(frozen=True)
class DesktopEndpoint:
    channel: Channel
    upstream: Path
    available: bool | None
    error: str = ""
    cookie: Path | None = None

    @property
    def service(self) -> str:
        return f"{self.channel}-helper"

    @property
    def volume(self) -> str:
        return DESKTOP_RUNTIME_VOLUMES[0 if self.channel == "dbus" else 1]

    @property
    def target(self) -> str:
        return f"/run/djinn/{self.channel}"

    @property
    def environment(self) -> dict[str, str]:
        if self.channel == "dbus":
            return {"DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.target}/bus"}
        return {
            "PULSE_SERVER": f"unix:{self.target}/native",
            "PULSE_COOKIE": f"{self.target}/cookie",
        }


def discover_desktop_endpoints() -> tuple[DesktopEndpoint, DesktopEndpoint]:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    endpoints: list[DesktopEndpoint] = []
    for channel, leaf in (("dbus", runtime / "bus"), ("audio", runtime / "pulse/native")):
        available: bool | None = False
        error = ""
        cookie = None
        try:
            with contextlib.suppress(FileNotFoundError):
                available = stat.S_ISSOCK(leaf.stat().st_mode)
            if channel == "audio" and available:
                candidate = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/pulse/cookie"
                try:
                    info = candidate.lstat()
                    if (
                        stat.S_ISREG(info.st_mode)
                        and info.st_size == 256
                        and os.access(candidate, os.R_OK)
                    ):
                        cookie = candidate.resolve(strict=True)
                except FileNotFoundError:
                    pass
        except OSError as exc:
            available, error = None, str(exc)
        endpoints.append(
            DesktopEndpoint(cast(Channel, channel), leaf.absolute(), available, error, cookie)
        )
    return endpoints[0], endpoints[1]


def helper_fragment(
    endpoint: DesktopEndpoint, image_id: str, generation: str, generation_label: str
) -> ComposeFragment:
    upstream = endpoint.upstream if endpoint.channel == "dbus" else endpoint.upstream.parent
    target = "/upstream/bus" if endpoint.channel == "dbus" else "/upstream/pulse"
    mounts: list[str | dict[str, object]] = [
        {
            "type": "bind",
            "source": str(upstream).replace("$", "$$"),
            "target": target,
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    if endpoint.cookie is not None:
        mounts.append(
            {
                "type": "bind",
                "source": str(endpoint.cookie).replace("$", "$$"),
                "target": "/upstream-cookie",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        )
    return {
        "services": {
            endpoint.service: {
                "image": image_id,
                "volumes": mounts,
                "labels": {generation_label: generation},
            }
        }
    }


def add_delivery(fragment: ComposeFragment, endpoint: DesktopEndpoint) -> None:
    service = fragment["services"]["dev"]
    service.setdefault("volumes", []).append(
        {
            "type": "volume",
            "source": f"desktop-{endpoint.channel}",
            "target": endpoint.target,
            "read_only": True,
        }
    )
    service.setdefault("environment", {}).update(endpoint.environment)


def image_policy() -> dict[str, Any]:
    root = get_project_root() / "helpers/desktop"
    return {
        "policy": json.loads((root / "policy.json").read_text()),
        "relay": (root / "relay.pa").read_text(),
        "daemon": (root / "daemon.conf").read_text(),
        "client": (root / "client.conf").read_text(),
    }


def proxy_version_ok(evidence: Mapping[str, Any]) -> bool:
    # The helper uses dpkg's Debian comparison, including the security revision.
    # Independently reject contradictory/malformed evidence at the API seam.
    version = str(evidence.get("version", ""))
    match = re.fullmatch(r"(?:(\d+):)?(\d+)\.(\d+)\.(\d+)-(\d+)(?:\+deb(\d+)u(\d+))?", version)
    return bool(
        evidence.get("floor_ok") is True
        and evidence.get("floor") == PROXY_VERSION_FLOOR
        and match
        and tuple(int(p or 0) for p in match.groups()) >= (0, 0, 1, 6, 1, 13, 3)
    )


@dataclass(frozen=True)
class DesktopChannelInspection:
    channel: Channel
    state: Literal["off", "filtered", "locked", "missing", "raw", "unknown"]
    sealed_ok: bool | None
    reasons: tuple[str, ...]
    expected_host_available: bool | None
    observed_delivery: bool
    version: str = ""

    @property
    def detail(self) -> str:
        return "; ".join((self.state, *self.reasons))


@dataclass(frozen=True)
class DesktopInspection:
    channels: tuple[DesktopChannelInspection, ...]
    raw_sources: tuple[str, ...]
    raw_verified: bool

    @property
    def sealed_ok(self) -> bool | None:
        if any(row.sealed_ok is False for row in self.channels) or self.raw_sources:
            return False
        if not self.raw_verified or any(row.sealed_ok is None for row in self.channels):
            return None
        return True


def _path(value: str) -> Path:
    path = Path(value)
    if path.is_relative_to("/var/run"):
        path = Path("/run") / path.relative_to("/var/run")
    return path


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _capabilities(values: list[Any] | None) -> set[str]:
    # Compose reports capabilities as CAP_<NAME>; the plain Docker CLI keeps the given form.
    return {str(value).removeprefix("CAP_") for value in values or []}


def _env(obj: Mapping[str, Any]) -> dict[str, str]:
    return dict(item.split("=", 1) for item in obj.get("Config", {}).get("Env", []) if "=" in item)


def helper_verification_reasons(
    endpoint: DesktopEndpoint,
    helper: Mapping[str, Any] | None,
    image: Mapping[str, Any] | None,
    evidence: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> list[str]:
    if helper is None:
        return ["helper absent or inspection unavailable"]
    reasons: list[str] = []
    state = helper.get("State", {})
    cfg, hc = helper.get("Config", {}), helper.get("HostConfig", {})
    labels: dict[str, Any] = cfg.get("Labels") or {}
    if not state.get("Running") or state.get("Health", {}).get("Status") != "healthy":
        reasons.append("helper is not running and healthy")
    if (
        labels.get("com.docker.compose.project") != "djinn-in-a-box"
        or labels.get("com.docker.compose.service") != endpoint.service
    ):
        reasons.append("helper Compose ownership is unverified")
    if image is None or not image.get("Id") or image["Id"] != helper.get("Image"):
        reasons.append("helper image differs from the resolved local image")
    if (
        cfg.get("Cmd") != ["python3", "-I", "/etc/djinn/helper.py", endpoint.channel]
        or cfg.get("Entrypoint")
        or cfg.get("User", "") not in ("", "0", "root")
        or cfg.get("WorkingDir") != "/"
    ):
        reasons.append("helper startup differs from the image launcher")
    if (
        not hc.get("ReadonlyRootfs")
        or hc.get("NetworkMode") != "none"
        or hc.get("Privileged")
        or hc.get("Devices")
        or hc.get("PidMode")
        or hc.get("IpcMode") == "host"
        or "ALL" not in _capabilities(hc.get("CapDrop"))
        or _capabilities(hc.get("CapAdd")) != {"CHOWN", "SETUID", "SETGID", "SETPCAP"}
        or "no-new-privileges:true" not in hc.get("SecurityOpt", [])
    ):
        reasons.append("helper isolation is unverified")
    expected_targets = {
        "/out",
        "/upstream/bus" if endpoint.channel == "dbus" else "/upstream/pulse",
    }
    if endpoint.cookie is not None:
        expected_targets.add("/upstream-cookie")
    mounts = helper.get("Mounts", [])
    persistent = [m for m in mounts if m.get("Type") != "tmpfs"]
    if {m.get("Destination") for m in persistent} != expected_targets:
        reasons.append("helper has missing or unexpected mounts")
    for mount in persistent:
        target = mount.get("Destination")
        if target == "/out":
            if (
                mount.get("Type") != "volume"
                or mount.get("Name") != endpoint.volume
                or mount.get("RW") is not True
            ):
                reasons.append("helper output volume is unverified")
        else:
            expected = (
                endpoint.cookie
                if target == "/upstream-cookie"
                else (endpoint.upstream if endpoint.channel == "dbus" else endpoint.upstream.parent)
            )
            if (
                expected is None
                or mount.get("Type") != "bind"
                or mount.get("RW") is not False
                or Path(mount.get("Source", "")).resolve() != expected.resolve()
            ):
                reasons.append("helper upstream/auth mount is unverified")
    if not evidence.get("health_ok") or evidence.get("policy") != policy["policy"]:
        reasons.append("ordinary-client health or exact daemon policy is unverified")
    if endpoint.channel == "dbus":
        if not proxy_version_ok(evidence):
            reasons.append(f"Debian proxy version missing/below {PROXY_VERSION_FLOOR}")
    elif any(evidence.get(key) != policy[key] for key in ("relay", "daemon", "client")):
        reasons.append("audio startup configuration is unverified")
    return reasons


def inspect_desktop_endpoints(
    dev_inspect: Mapping[str, Any] | None,
    helper_inspects: Mapping[str, Mapping[str, Any] | None],
    image_inspects: Mapping[str, Mapping[str, Any] | None],
    host_endpoints: tuple[DesktopEndpoint, ...],
    helper_versions: Mapping[str, Mapping[str, Any]],
) -> DesktopInspection:
    """Inspect supplied actual objects; absent proof cannot certify delivery.

    `None` for dev means unavailable/no running dev, never proof of no raw mounts.
    Package-version results come from the bounded, read-only image health query.
    The remaining sealed-class checks (Docker authority and host execution chain)
    belong to #76 B.
    """
    if dev_inspect is None or not dev_inspect.get("State", {}).get("Running"):
        return DesktopInspection(
            tuple(
                DesktopChannelInspection(
                    e.channel, "unknown", None, ("no running dev inspection",), e.available, False
                )
                for e in host_endpoints
            ),
            (),
            False,
        )
    env = _env(dev_inspect)
    raw_mounts: Any = dev_inspect.get("Mounts")
    if not isinstance(raw_mounts, list):
        return DesktopInspection(
            tuple(
                DesktopChannelInspection(
                    e.channel, "unknown", None, ("dev mounts unavailable",), e.available, False
                )
                for e in host_endpoints
            ),
            (),
            False,
        )
    mounts = cast(list[dict[str, Any]], raw_mounts)
    protected = [e.upstream.resolve() for e in host_endpoints]
    protected.extend(e.upstream.parent.resolve() for e in host_endpoints if e.channel == "audio")
    protected.extend(e.cookie.resolve() for e in host_endpoints if e.cookie)
    # Include standard paths even if discovery used another runtime directory.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    protected.extend(
        (
            Path(f"/run/user/{os.getuid()}/bus"),
            Path(f"/run/user/{os.getuid()}/pulse"),
            home / ".config/pulse/cookie",
        )
    )
    helper_mounts = [m for h in helper_inspects.values() if h for m in h.get("Mounts", [])]
    output_sources = {m.get("Source") for m in helper_mounts if m.get("Destination") == "/out"}
    raw: list[str] = []
    for mount in mounts:
        source = Path(mount.get("Source", "")).resolve()
        target = _path(mount.get("Destination", "/"))
        if mount.get("Type") == "bind" and any(
            p == source or p.is_relative_to(source) for p in protected
        ):
            raw.append(f"{source} -> {target}")
        # A bind alias of output state or Docker's volume store is not managed delivery.
        if mount.get("Type") == "bind" and any(
            s and _overlap(source, Path(s).resolve()) for s in output_sources
        ):
            raw.append(f"output-state alias {source} -> {target}")
    policy = image_policy()
    rows: list[DesktopChannelInspection] = []
    for endpoint in host_endpoints:
        output = [m for m in mounts if _path(m.get("Destination", "/")) == Path(endpoint.target)]
        delivered = bool(output or any(key in env for key in endpoint.environment))
        evidence = helper_versions.get(endpoint.service, {})
        reasons: list[str] = []
        if raw:
            state: Literal["off", "filtered", "locked", "missing", "raw", "unknown"] = "raw"
            sealed = False
            reasons.extend(raw)
        elif not delivered:
            state, sealed = ("off" if endpoint.available is False else "missing"), True
            if endpoint.available is not False:
                reasons.append(endpoint.error or "host endpoint expected; no endpoint delivered")
        else:
            for key, value in endpoint.environment.items():
                if env.get(key) != value:
                    reasons.append(f"unrecognized or missing {key}")
            if (
                len(output) != 1
                or output[0].get("Type") != "volume"
                or output[0].get("Name") != endpoint.volume
                or output[0].get("RW") is not False
            ):
                reasons.append("dev output must be the read-only helper volume")
            if any(
                _overlap(_path(m.get("Destination", "/")), Path(endpoint.target))
                for m in mounts
                if m not in output
            ):
                reasons.append("dev output is shadowed by another mount")
            helper = helper_inspects.get(endpoint.service)
            dev_labels: dict[str, Any] = dev_inspect.get("Config", {}).get("Labels") or {}
            helper_labels: dict[str, Any] = (helper or {}).get("Config", {}).get("Labels") or {}
            generation = dev_labels.get(GENERATION_LABEL)
            if not generation or helper_labels.get(GENERATION_LABEL) != generation:
                reasons.append("helper/dev runtime generations differ or are missing")
            reasons.extend(
                helper_verification_reasons(
                    endpoint, helper, image_inspects.get(HELPER_IMAGE), evidence, policy
                )
            )
            if helper and output:
                helper_out = [m for m in helper.get("Mounts", []) if m.get("Destination") == "/out"]
                if not helper_out or output[0].get("Source") != helper_out[0].get("Source"):
                    reasons.append("helper/dev output sources differ")
            state = (
                "missing" if reasons else ("filtered" if endpoint.channel == "dbus" else "locked")
            )
            sealed = not reasons
        rows.append(
            DesktopChannelInspection(
                endpoint.channel,
                state,
                sealed,
                tuple(reasons),
                endpoint.available,
                delivered,
                str(evidence.get("version", "")),
            )
        )
    return DesktopInspection(tuple(rows), tuple(dict.fromkeys(raw)), True)
