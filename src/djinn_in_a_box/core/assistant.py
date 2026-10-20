"""Standalone, temporary assistant sessions shared with guided setup."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast

from djinn_in_a_box.config.loader import load_config
from djinn_in_a_box.config.models import AppConfig, AssistantAgent, AssistantConfig
from djinn_in_a_box.config.zones import load_zone_assignments
from djinn_in_a_box.core import opencode_credentials as credentials
from djinn_in_a_box.core.agent_versions import (
    AgentVersionError,
    effective_version,
    load_versions,
    version_pin,
)
from djinn_in_a_box.core.console import warning
from djinn_in_a_box.core.docker import (
    DJINN_NETWORK,
    CredentialExportError,
    _run_captured,  # pyright: ignore[reportPrivateUsage]
    _run_streamed,  # pyright: ignore[reportPrivateUsage]
    ensure_network,
    export_opencode_credentials,
    get_config_root,
    is_background_process_group,
)
from djinn_in_a_box.core.docker_cli import DOCKER_EXECUTABLE
from djinn_in_a_box.core.exceptions import (
    ConfigNotFoundError,
    ConfigValidationError,
    ZoneConfigurationError,
    ZoneRootValidationError,
)
from djinn_in_a_box.core.paths import CONFIG_DIR, get_project_root

ASSISTANT_IMAGE = "djinn-assistant:latest"
CONTENT_LABEL = "djinn.assistant.content"
SESSION_LABEL = "djinn.assistant.session"
CREDENTIALS = {
    "claude": (".credentials.json", "claude.json"),
    "codex": ("auth.json",),
    # OpenCode logins travel through a private per-session directory, not single-file binds.
    "opencode": (),
}
OPENCODE_CREDENTIAL_TARGET = Path("/run/djinn-credentials")
OPENCODE_DATA_TARGET = Path("/home/dev/.local/share/opencode")
STOP_GRACE_SECONDS = 10
STOP_TIMEOUT_SECONDS = 40
REMOVAL_WAIT_SECONDS = 20
REMOVAL_POLL_SECONDS = 0.2
# Docker's answer when `--rm` already removes the container, for example right after a stop.
_REMOVAL_IN_PROGRESS = "is already in progress"
PIN_NAMES = {
    "claude": "CLAUDE_CODE_VERSION",
    "codex": "CODEX_VERSION",
    "opencode": "OPENCODE_VERSION",
}


class AssistantError(RuntimeError):
    """An actionable failure before or during an assistant session."""


def capture(*args: str) -> str:
    result = _run_captured([DOCKER_EXECUTABLE, *args], timeout=10)
    if not result.success:
        raise AssistantError(result.stderr.strip() or f"Docker exited {result.returncode}")
    return result.stdout


def inspect(resource: str, name: str) -> dict[str, Any] | None:
    result = _run_captured([DOCKER_EXECUTABLE, resource, "inspect", name], timeout=10)
    if not result.success:
        if f"No such {resource}: {name}" in result.stderr:
            capture("info")
            return None
        raise AssistantError(result.stderr.strip() or "Docker inspection failed")
    try:
        value: Any = json.loads(result.stdout)
        if (
            not isinstance(value, list)
            or len(cast(list[Any], value)) != 1
            or not isinstance(value[0], dict)
        ):
            raise ValueError("expected one Docker object")
        obj = cast(dict[str, Any], value[0])
        if not isinstance(obj.get("Id"), str) or not isinstance(obj.get("Config"), dict):
            raise ValueError("missing Docker identity/config")
        labels: Any = obj["Config"].get("Labels")
        if labels is not None and not isinstance(labels, dict):
            raise ValueError("invalid Docker labels")
        return obj
    except (ValueError, TypeError) as exc:
        raise AssistantError(f"Invalid Docker {resource} inspection") from exc


def label(obj: dict[str, Any] | None, name: str) -> str | None:
    if obj is None:
        return None
    configuration: dict[str, Any] = obj["Config"]
    labels: dict[str, Any] = configuration.get("Labels") or {}
    return labels.get(name)


def audit_config() -> tuple[AppConfig | None, str]:
    """Keep diagnosis reachable when normal dev configuration cannot load."""
    try:
        config = load_config()
        load_zone_assignments(config)
        return config, ""
    except (
        ConfigNotFoundError,
        ConfigValidationError,
        ZoneConfigurationError,
        ZoneRootValidationError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        return None, str(exc)


def local_daemon() -> tuple[Path, str]:
    if not sys.platform.startswith("linux"):
        raise AssistantError("Assistant sessions require Linux and a local Unix Docker socket")
    endpoint = os.environ.get("DOCKER_HOST", "")
    if os.environ.get("DOCKER_CONTEXT") or not endpoint:
        context = os.environ.get("DOCKER_CONTEXT") or capture("context", "show").strip()
        try:
            endpoint = json.loads(capture("context", "inspect", context))[0]["Endpoints"]["docker"][
                "Host"
            ]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AssistantError("Could not resolve the Docker context endpoint") from exc
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise AssistantError(
            "Assistant sessions require local Docker; TCP/SSH contexts are unsupported"
        )
    socket = Path(endpoint.removeprefix("unix://")).resolve()
    if not stat.S_ISSOCK(socket.stat().st_mode):
        raise AssistantError(f"Docker endpoint is not a Unix socket: {socket}")
    platform = capture("info", "--format", "{{.OSType}}/{{.Architecture}}").strip()
    platform = platform.replace("x86_64", "amd64").replace("aarch64", "arm64")
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise AssistantError(f"Unsupported assistant host platform: {platform}")
    return socket, platform


def ensure_image(project: Path, agent: AssistantAgent, platform: str, network: str) -> str:
    try:
        versions = load_versions()
        version = effective_version(project, PIN_NAMES[agent], versions)
        docker_version = version_pin(project, "DOCKER_VERSION")
    except AgentVersionError as exc:
        raise AssistantError(str(exc)) from exc
    context = project / "assistant"
    content = json.dumps(
        [agent, version, docker_version, platform, network, os.getuid(), os.getgid()]
    )
    digest = hashlib.sha256(content.encode())
    for name in ("Dockerfile", "entrypoint.sh", "install-opencode.sh"):
        digest.update((context / name).read_bytes())
    fingerprint = digest.hexdigest()
    image = inspect("image", ASSISTANT_IMAGE)
    if label(image, CONTENT_LABEL) != fingerprint:
        argv = [
            DOCKER_EXECUTABLE,
            "buildx",
            "build",
            "--load",
            "--progress",
            "plain",
            "--platform",
            platform,
            "--network",
            network,
            "--file",
            str(context / "Dockerfile"),
            "--tag",
            ASSISTANT_IMAGE,
            "--label",
            f"{CONTENT_LABEL}={fingerprint}",
            "--build-arg",
            f"AGENT={agent}",
            "--build-arg",
            f"AGENT_VERSION={version}",
            "--build-arg",
            f"DOCKER_VERSION={docker_version}",
            "--build-arg",
            f"USER_UID={os.getuid()}",
            "--build-arg",
            f"USER_GID={os.getgid()}",
        ]
        if network == "host":
            argv.extend(["--allow", "network.host"])
        result = _run_streamed([*argv, str(context)])
        if not result.success:
            raise AssistantError(
                result.stderr or f"Assistant image build failed ({result.returncode})"
            )
        image = inspect("image", ASSISTANT_IMAGE)
    if (
        image is None
        or not isinstance(image.get("Id"), str)
        or label(image, CONTENT_LABEL) != fingerprint
    ):
        raise AssistantError("Assistant image content label could not be verified")
    return image["Id"]


def bind(source: Path, target: Path) -> list[str]:
    # Docker parses --mount as CSV, including fields containing commas or quotes.
    output = io.StringIO()
    csv.writer(output, lineterminator="").writerow(["type=bind", f"src={source}", f"dst={target}"])
    return ["--mount", output.getvalue()]


def session_mounts(
    project: Path, config: AppConfig | None, agent: AssistantAgent, socket: Path
) -> list[tuple[Path, Path]]:
    state = (Path.home() / ".djinn").resolve()
    directory = CONFIG_DIR.resolve()
    for path in (state, directory):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mounts = [
        (project, project),
        (directory, directory),
        (state, state),
        (socket, Path("/var/run/docker.sock")),
    ]
    root = get_config_root(config).expanduser().resolve() / agent
    # Existing credentials only. Missing/broken installations can still be inspected.
    for name in CREDENTIALS[agent]:
        credential = root / name
        if credential.is_symlink() or (credential.exists() and not credential.is_file()):
            raise AssistantError(f"Credential must be a regular file: {credential}")
        if credential.is_file():
            mounts.append((credential, Path("/run/djinn-credentials") / name))
    return mounts


def agent_argv(agent: AssistantAgent, prompt: str) -> list[str]:
    if agent == "claude":
        return ["claude", "--permission-mode", "manual", "--safe-mode", prompt]
    if agent == "codex":
        return [
            "codex",
            "--sandbox",
            "danger-full-access",
            "--ask-for-approval",
            "on-request",
            prompt,
        ]
    return ["opencode", "--standalone", "--prompt", prompt]


def session_argv(
    image: str,
    name: str,
    agent: AssistantAgent,
    prompt: str,
    project: Path,
    socket: Path,
    mounts: list[tuple[Path, Path]],
    timezone: str,
) -> list[str]:
    argv = [
        DOCKER_EXECUTABLE,
        "run",
        "--rm",
        "--interactive",
        "--tty",
        "--name",
        name,
        "--label",
        f"{SESSION_LABEL}={name}",
        "--network",
        DJINN_NETWORK,
        "--workdir",
        str(project),
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--group-add",
        str(socket.stat().st_gid),
    ]
    for source, target in mounts:
        argv.extend(bind(source, target))
    environment = {
        "HOME": "/home/dev",
        "TZ": timezone,
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "OPENCODE_CONFIG_CONTENT": (
            '{"permissions":[{"action":"edit","resource":"*","effect":"ask"},'
            '{"action":"shell","resource":"*","effect":"ask"}]}'
        ),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
    }
    for key in ("TERM", "NO_COLOR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    for key, value in environment.items():
        argv.extend(["--env", f"{key}={value}"])
    return [*argv, image, *agent_argv(agent, prompt)]


def cleanup(name: str) -> None:
    container = inspect("container", name)
    if container is None:
        return
    if label(container, SESSION_LABEL) != name:
        raise AssistantError(f"Assistant cleanup refused unknown ownership: {name}")
    identity = container.get("Id")
    if not isinstance(identity, str) or not identity:
        raise AssistantError("Assistant cleanup could not verify the container ID")
    try:
        capture("rm", "--force", identity)
    except AssistantError as error:
        if _REMOVAL_IN_PROGRESS not in str(error):
            raise
    deadline = time.monotonic() + REMOVAL_WAIT_SECONDS
    while inspect("container", name) is not None:
        if time.monotonic() >= deadline:
            raise AssistantError(f"Assistant container cleanup failed: {name}")
        time.sleep(REMOVAL_POLL_SECONDS)


def _reason(error: Exception) -> str:
    """A value-free reason: credential errors are value-free by contract, OS errors name no path."""
    if isinstance(error, credentials.CredentialsError):
        return str(error)
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return type(error).__name__


def credential_error_reason(error: Exception) -> str:
    """Reuse the audit error renderer, including the value-free export adapter reason."""
    return str(error) if isinstance(error, CredentialExportError) else _reason(error)


def stop_container(name: str) -> None:
    """Stop with a grace period and wait; a container that is already gone is fine."""
    result = _run_captured(
        [DOCKER_EXECUTABLE, "stop", "--time", str(STOP_GRACE_SECONDS), name],
        timeout=STOP_TIMEOUT_SECONDS,
    )
    gone = f"No such container: {name}" in result.stderr or _REMOVAL_IN_PROGRESS in result.stderr
    if not result.success and not gone:
        raise AssistantError(result.stderr.strip() or f"Could not stop container {name}")


def container_exists(name: str) -> bool:
    """By name and by the session label, so a renamed or half-created container still counts."""
    if inspect("container", name) is not None:
        return True
    labelled = capture("ps", "--all", "--quiet", "--filter", f"label={SESSION_LABEL}={name}")
    return bool(labelled.strip())


def opencode_carrier(config: AppConfig | None) -> Path:
    return get_config_root(config).expanduser().resolve() / "opencode"


def recover_leftover_sessions(
    project: Path, config: AppConfig | None, own_session: credentials.Session | None = None
) -> None:
    """Merge leftover OpenCode audit sessions back. Every failure is a warning, never an error.

    The export always runs in the OpenCode assistant image, whatever agent is audited, and that
    image is resolved (and built, if needed) only when a session actually holds a database.
    """
    image: str | None = None

    def export_db(data_directory: str) -> bytes:
        nonlocal image
        try:
            if image is None:
                _, platform = local_daemon()
                network = config.build.network if config else "default"
                image = ensure_image(project, "opencode", platform, network)
            return export_opencode_credentials(image=image, source=data_directory, kind="bind")
        except (AssistantError, RuntimeError) as error:
            warning(f"OpenCode audit session export failed: {error}")
            raise

    carrier = opencode_carrier(config)
    try:
        if Path(credentials.default_audit_root()).is_dir():
            # A session can only be merged into a carrier directory that exists.
            carrier.mkdir(parents=True, exist_ok=True, mode=0o700)
        report = credentials.recover_audit_sessions(
            None,
            carrier,
            export_db,
            container_exists,
            own_session,
            stop_container=stop_container,
        )
    except Exception as error:  # noqa: BLE001 - recovery must never stop an audit or a start
        warning(f"OpenCode audit session recovery failed: {_reason(error)}")
        return
    for identity, reason in report.kept:
        warning(f"OpenCode audit session {identity} was kept: {reason}")


def audit_session_mounts(session: credentials.Session) -> list[tuple[Path, Path]]:
    """Exactly two mounts: the session's io/ and data/, never the session or a parent."""
    return [
        (Path(session.io_path), OPENCODE_CREDENTIAL_TARGET),
        (Path(session.data_path), OPENCODE_DATA_TARGET),
    ]


def open_audit_session(config: AppConfig | None) -> credentials.Session:
    """Create the session and write its seed. Without a usable carrier the audit has no seed."""
    try:
        session = credentials.create_audit_session()
    except (credentials.CredentialsError, OSError) as error:
        raise AssistantError(
            f"Could not create the OpenCode audit session: {_reason(error)}"
        ) from error
    carrier = opencode_carrier(config)
    try:
        carrier.mkdir(parents=True, exist_ok=True, mode=0o700)
        with credentials.carrier_lock(carrier) as lease:
            credentials.write_seed(session, credentials.materialize_seed(carrier, lease))
    except (credentials.CredentialsError, OSError) as error:
        warning(f"OpenCode audit starts without a login seed: {_reason(error)}")
    except BaseException:
        session.close()
        raise
    return session


def finish_session(
    name: str,
    session: credentials.Session,
    project: Path,
    config: AppConfig | None,
    *,
    launched: bool,
) -> None:
    """Stop, clean up, merge back, release: in this order, each step even if one before fails."""
    try:
        if launched:
            try:
                stop_container(name)
            finally:
                cleanup(name)
    finally:
        try:
            recover_leftover_sessions(project, config, session)
        finally:
            session.close()


def run_audit(occasion: str | None, selected: str | None = None) -> int:
    config, config_error = audit_config()
    agent = AssistantConfig.model_validate(
        {"agent": selected or (config.assistant.agent if config else "claude")}
    ).agent
    if not sys.stdin.isatty() or not sys.stdout.isatty() or is_background_process_group():
        raise AssistantError("Run djinn audit in a foreground terminal")
    if os.getuid() == 0:
        raise AssistantError("Run djinn audit as your regular host user")
    project = get_project_root().resolve()
    socket, platform = local_daemon()
    mounts = session_mounts(project, config, agent, socket)
    briefing = (project / "assistant" / "audit-briefing.md").read_text(encoding="utf-8")
    recover_leftover_sessions(project, config)
    session = open_audit_session(config) if agent == "opencode" else None
    launched = False
    try:
        if session is not None:
            mounts.extend(audit_session_mounts(session))
        layout = "\n".join(f"- {source} -> {target} (rw)" for source, target in mounts)
        prompt = f"{briefing}\n\nInstallation mounts:\n{layout}\n"
        if config_error:
            prompt += f"\nConfiguration loader error (diagnose this):\n{config_error}\n"
        prompt += f"\nUser occasion:\n{occasion or 'Perform a general installation health check.'}"
        image = ensure_image(
            project, agent, platform, config.build.network if config else "default"
        )
        if not ensure_network(DJINN_NETWORK):
            raise AssistantError(f"Could not ensure {DJINN_NETWORK}; see the Docker error above")
        name = session.container_name if session else f"djinn-assistant-{uuid.uuid4().hex}"
        argv = session_argv(
            image,
            name,
            agent,
            prompt,
            project,
            socket,
            mounts,
            config.timezone if config else "UTC",
        )
        launched = True
        try:
            return subprocess.run(argv, check=False).returncode
        except KeyboardInterrupt:
            return 130
        finally:
            if session is None:
                cleanup(name)
    finally:
        if session is not None:
            finish_session(session.container_name, session, project, config, launched=launched)
