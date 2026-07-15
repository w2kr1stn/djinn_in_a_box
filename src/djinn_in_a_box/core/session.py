"""Session management for AI agent sessions via docker exec."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from djinn_in_a_box.config.loader import load_agents
from djinn_in_a_box.config.models import AgentConfig
from djinn_in_a_box.core.docker import _decode_timeout_output

log = logging.getLogger(__name__)

_SESSION_ENV: dict[str, str] = {
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
}
_CONTAINER_SESSIONS_BASE = "/home/dev/sessions"
_HOST_SESSIONS_BASE = Path.home() / ".djinn" / "sessions"
_DJINN_CONTAINER_NAME = "djinn"
_EXEC_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class SessionResult:
    """Result of an agent session execution."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    workspace_dir: Path | None = None

    @property
    def success(self) -> bool:
        return self.returncode == 0


class SessionManager:
    """Manages AI agent sessions via docker exec or host-mode fallback."""

    def __init__(self, project_name: str) -> None:
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", project_name):
            msg = (
                f"Invalid project name: {project_name!r}."
                " Use only alphanumeric, underscore, dash, or dot."
            )
            raise ValueError(msg)
        self._project_name = project_name
        self._agents = load_agents()
        self._container_workdir = f"{_CONTAINER_SESSIONS_BASE}/{project_name}"

    @property
    def container_mode(self) -> bool:
        return self._find_container() is not None

    def preflight_check(self) -> None:
        if self._find_container() is not None:
            return
        if shutil.which("claude") is not None:
            return
        msg = "No running Djinn container found and no agent CLI available on PATH"
        raise RuntimeError(msg)

    def run_interactive(
        self,
        *,
        workspace_dir: Path,
        agent: str = "claude",
        model: str | None = None,
        initial_prompt: str | None = None,
    ) -> SessionResult:
        agent_config = self._resolve_agent(agent)
        container_id = self._find_container()

        if container_id is not None:
            cwd = self._resolve_container_workdir(workspace_dir)
            agent_cmd = self._build_interactive_command(agent_config, model, initial_prompt)
            full_cmd = f"cd {shlex.quote(cwd)} && git init -q 2>/dev/null; {agent_cmd}"

            cmd: list[str] = ["docker", "exec", "-it"]
            for key, value in _SESSION_ENV.items():
                cmd.extend(["-e", f"{key}={value}"])
            cmd.extend(["-w", cwd])
            cmd.append(container_id)
            cmd.extend(["bash", "-lc", full_cmd])

            log.debug("Running interactive container command: %s", cmd)
            try:
                result = subprocess.run(cmd, check=False)
            except FileNotFoundError:
                return SessionResult(returncode=127, stderr="Docker command not found")
            except PermissionError as e:
                return SessionResult(returncode=126, stderr=f"Permission denied: {e}")
            return SessionResult(returncode=result.returncode, workspace_dir=workspace_dir)

        # Host mode fallback
        self._git_init_workspace(workspace_dir)
        cmd = self._build_host_interactive_command(agent_config, model, initial_prompt)

        log.debug("Running interactive host command: %s", cmd)
        try:
            result = subprocess.run(
                cmd,
                cwd=workspace_dir,
                env={**os.environ, **_SESSION_ENV},
                check=False,
            )
        except FileNotFoundError:
            return SessionResult(
                returncode=127, stderr=f"Agent binary not found: {agent_config.binary}",
            )
        except PermissionError as e:
            return SessionResult(returncode=126, stderr=f"Permission denied: {e}")
        return SessionResult(returncode=result.returncode, workspace_dir=workspace_dir)

    def run_headless(
        self,
        *,
        workspace_dir: Path,
        prompt: str,
        agent: str = "claude",
        model: str | None = None,
        timeout: int = 300,
    ) -> SessionResult:
        from djinn_in_a_box.commands.agent import build_agent_command

        agent_config = self._resolve_agent(agent)
        container_id = self._find_container()

        if container_id is not None:
            cwd = self._resolve_container_workdir(workspace_dir)
            agent_cmd = build_agent_command(agent_config, model=model)
            full_cmd = f"cd {shlex.quote(cwd)} && git init -q 2>/dev/null; {agent_cmd}"

            cmd: list[str] = ["docker", "exec"]
            cmd.extend(["-e", f"AGENT_PROMPT={prompt}"])
            for key, value in _SESSION_ENV.items():
                cmd.extend(["-e", f"{key}={value}"])
            cmd.extend(["-w", cwd])
            cmd.append(container_id)
            cmd.extend(["bash", "-lc", full_cmd])

            log.debug("Running headless container command: %s", cmd)
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                return SessionResult(
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    workspace_dir=workspace_dir,
                )
            except subprocess.TimeoutExpired as e:
                stdout, stderr = _decode_timeout_output(e, timeout)
                return SessionResult(
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr,
                    workspace_dir=workspace_dir,
                )
            except FileNotFoundError:
                return SessionResult(
                    returncode=127,
                    stdout="",
                    stderr="Docker command not found",
                    workspace_dir=workspace_dir,
                )
            except PermissionError as e:
                return SessionResult(
                    returncode=126,
                    stdout="",
                    stderr=f"Permission denied: {e}",
                    workspace_dir=workspace_dir,
                )

        # Host mode fallback
        self._git_init_workspace(workspace_dir)
        cmd = self._build_host_headless_command(agent_config, model, prompt)

        log.debug("Running headless host command: %s", cmd)
        try:
            result = subprocess.run(
                cmd,
                cwd=workspace_dir,
                env={**os.environ, **_SESSION_ENV},
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return SessionResult(
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                workspace_dir=workspace_dir,
            )
        except subprocess.TimeoutExpired as e:
            stdout, stderr = _decode_timeout_output(e, timeout)
            return SessionResult(
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                workspace_dir=workspace_dir,
            )
        except FileNotFoundError:
            return SessionResult(
                returncode=127,
                stdout="",
                stderr=f"Agent binary not found: {agent_config.binary}",
                workspace_dir=workspace_dir,
            )
        except PermissionError as e:
            return SessionResult(
                returncode=126,
                stdout="",
                stderr=f"Permission denied: {e}",
                workspace_dir=workspace_dir,
            )

    def _resolve_container_workdir(self, workspace_dir: Path) -> str:
        """Resolve the container-side working directory from a host workspace path.

        If ``workspace_dir`` is under ``~/.djinn/sessions/``, the relative path
        is mapped to ``/home/dev/sessions/<relative>``.  Otherwise falls back to
        the fixed per-project path ``/home/dev/sessions/<project_name>``.
        """
        try:
            relative = workspace_dir.resolve().relative_to(_HOST_SESSIONS_BASE.resolve())
            return f"{_CONTAINER_SESSIONS_BASE}/{relative}"
        except ValueError:
            return self._container_workdir

    def _find_container(self) -> str | None:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--filter",
                    f"name=^{_DJINN_CONTAINER_NAME}$",
                    "--format",
                    "{{.ID}}",
                ],
                capture_output=True,
                text=True,
                timeout=_EXEC_TIMEOUT,
                check=False,
            )
        except FileNotFoundError:
            log.debug("Docker CLI not found, using host mode")
            return None
        except subprocess.TimeoutExpired:
            log.warning("Docker command timed out — falling back to host mode")
            return None
        except OSError as e:
            log.warning("Docker command failed: %s — falling back to host mode", e)
            return None
        if result.returncode != 0:
            return None
        for line in result.stdout.strip().splitlines():
            container_id = line.strip()
            if container_id:
                return container_id
        return None

    def _git_init_workspace(self, workspace_dir: Path) -> None:
        if (workspace_dir / ".git").exists():
            return
        result = subprocess.run(
            ["git", "init", "-q"],
            cwd=workspace_dir,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            log.warning("Failed to initialize git in %s", workspace_dir)

    def _resolve_agent(self, agent: str) -> AgentConfig:
        if agent not in self._agents:
            available = ", ".join(sorted(self._agents))
            msg = f"Unknown agent: {agent}. Available: {available}"
            raise ValueError(msg)
        return self._agents[agent]

    def _build_interactive_command(
        self,
        agent_config: AgentConfig,
        model: str | None,
        initial_prompt: str | None,
    ) -> str:
        parts: list[str] = [shlex.quote(agent_config.binary)]
        effective_model = model if model is not None else agent_config.default_model
        if effective_model:
            parts.extend(
                [shlex.quote(agent_config.model_flag), shlex.quote(effective_model)]
            )
        parts.extend(shlex.quote(f) for f in agent_config.write_flags)
        if initial_prompt is not None:
            parts.append(shlex.quote(initial_prompt))
        return " ".join(parts)

    def _build_host_interactive_command(
        self,
        agent_config: AgentConfig,
        model: str | None,
        initial_prompt: str | None,
    ) -> list[str]:
        cmd: list[str] = [agent_config.binary]
        effective_model = model if model is not None else agent_config.default_model
        if effective_model:
            cmd.extend([agent_config.model_flag, effective_model])
        cmd.extend(agent_config.write_flags)
        if initial_prompt is not None:
            cmd.append(initial_prompt)
        return cmd

    def _build_host_headless_command(
        self,
        agent_config: AgentConfig,
        model: str | None,
        prompt: str,
    ) -> list[str]:
        cmd: list[str] = [agent_config.binary, *agent_config.headless_flags]
        effective_model = model if model is not None else agent_config.default_model
        if effective_model:
            cmd.extend([agent_config.model_flag, effective_model])
        cmd.append(prompt)
        return cmd
